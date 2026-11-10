"""CLI-only checks and evidence tied to the actual Git working tree.

Nothing in this module grants an agent execution permissions.  The runner is called
only by the local CLI; the knowledge/MCP surfaces expose the read functions alone.
Fingerprints compare stable observations before and after a check, not an immutable
execution snapshot: an edit followed by a revert between observations is not detected.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import time
import tomllib
import threading
from uuid import UUID, uuid4

from .store import _clean, _insert, _json, _now
from .memory_paths import is_reserved_memory

MAX_OUTPUT_BYTES = 10 * 1024 * 1024
_PROFILE_PATH = Path(".devbrain/checks.toml")


class _RunReferencesGone(ValueError):
    """Concurrent deletion won over recording check evidence."""


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _root(store, project_id, worktree=None) -> Path:
    project = store.resolve_project(project_id)
    root = Path(worktree if worktree is not None else project["root"]).expanduser().resolve()
    registered = store.query("SELECT path FROM project_roots WHERE project_id=:project", {"project": project["id"]})
    if str(root) not in {str(Path(row["path"]).resolve()) for row in registered}:
        raise ValueError("Check worktree must be a registered project root")
    return root


def _git(root, *args) -> bytes:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                            timeout=20, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    if result.returncode:
        raise ValueError("Cannot inspect Git worktree")
    return result.stdout


def _input_patterns(extra_inputs):
    if extra_inputs is None:
        return []
    if not isinstance(extra_inputs, list) or not all(isinstance(x, str) and x for x in extra_inputs):
        raise ValueError("extra_inputs must be a list of relative paths/globs")
    for pattern in extra_inputs:
        path = Path(pattern)
        if path.is_absolute() or ".." in path.parts or ".git" in path.parts or "\\" in pattern or "\0" in pattern:
            raise ValueError("extra_inputs must stay inside the registered worktree")
        if is_reserved_memory(path):
            raise ValueError("extra_inputs cannot name reserved .memory/devbrain artifacts")
    return sorted(set(extra_inputs))


def _file_hash(path):
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    after = path.stat()
    def identity(s):
        return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode)
    if identity(before) != identity(after):
        raise ValueError("input changed while being read")
    return digest.hexdigest()


def _entry(root, relative, index_mode, unknowns):
    path = root / relative
    entry = {"path": relative, "index_mode": index_mode}
    try:
        mode = path.lstat().st_mode
        entry["mode"] = stat.S_IMODE(mode)
        if stat.S_ISLNK(mode):
            entry.update(kind="symlink", target=os.readlink(path))
            target = path.resolve()
            if not target.is_relative_to(root):
                unknowns.append(f"external symlink input: {relative}")
            elif target.is_file():
                entry["target_sha256"] = _file_hash(target)
                entry["target_mode"] = stat.S_IMODE(target.stat().st_mode)
            else:
                unknowns.append(f"unsupported or missing symlink target: {relative}")
        elif stat.S_ISREG(mode):
            # A parent directory can itself be a symlink outside the worktree.
            if not path.resolve().is_relative_to(root):
                unknowns.append(f"external input: {relative}")
            else:
                entry.update(kind="file", sha256=_file_hash(path))
        else:
            entry["kind"] = "unsupported"
            unknowns.append(f"unsupported input: {relative}")
    except FileNotFoundError:
        entry["kind"] = "missing"
    except (OSError, ValueError, RuntimeError) as exc:
        entry["kind"] = "unreadable"
        unknowns.append(f"unstable or unreadable input: {relative} ({type(exc).__name__})")
    return entry


def _manifest(root, patterns):
    unknowns, modes = [], {}
    for item in _git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if not item:
            continue
        header, raw_path = item.split(b"\t", 1)
        mode, _, stage = header.decode("ascii").split()
        relative = os.fsdecode(raw_path)
        if is_reserved_memory(relative):
            continue
        modes[relative] = mode
        if mode == "160000":
            unknowns.append(f"submodule input requires unsupported recursive capture: {relative}")
        if stage != "0":
            unknowns.append(f"unmerged Git input: {relative}")
    paths = set(modes)
    paths.update(os.fsdecode(p) for p in _git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
                 if p and not is_reserved_memory(os.fsdecode(p)))
    for pattern in patterns:
        # Path's recursive glob does not descend symlink directories.  glob's
        # recursive=True does, including cycles and external trees.
        matches = [p.relative_to(root).as_posix() for p in root.glob(pattern)]
        if not matches:
            unknowns.append(f"extra input has no matches: {pattern}")
        for match in matches:
            if ".git" in Path(match).parts or is_reserved_memory(match):
                continue
            path = root / match
            if not path.resolve().is_relative_to(root):
                unknowns.append(f"external extra input: {match}")
            elif path.is_dir() and not path.is_symlink():
                for child in path.rglob("*"):
                    if (".git" not in child.relative_to(root).parts
                            and not is_reserved_memory(child.relative_to(root))
                            and (child.is_file() or child.is_symlink())):
                        paths.add(child.relative_to(root).as_posix())
            else:
                paths.add(Path(match).as_posix())
    entries = [_entry(root, p, modes.get(p), unknowns) for p in sorted(paths)]
    return entries, sorted(set(unknowns))


def capture_code_state(store, project_id, worktree=None, extra_inputs=None) -> dict:
    root = _root(store, project_id, worktree)
    patterns = _input_patterns(extra_inputs)
    result = {"status": "unknown", "worktree": str(root), "head": None, "branch": None,
              "fingerprint": None, "manifest_hash": None, "unknowns": []}
    try:
        if Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel")).strip()).resolve() != root:
            raise ValueError("Registered root is not the Git worktree root")
        head = _git(root, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
        branch = subprocess.run(["git", "-C", str(root), "symbolic-ref", "--quiet", "--short", "HEAD"],
                                capture_output=True, timeout=20).stdout.decode(errors="replace").strip() or None
        first, unknowns = _manifest(root, patterns)
        second, later_unknowns = _manifest(root, patterns)
        later_head = _git(root, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
        if first != second or head != later_head:
            unknowns.append("worktree changed during code-state capture")
        unknowns = sorted(set(unknowns + later_unknowns))
        manifest_hash = _hash(second)
        result.update(head=head, branch=branch, manifest_hash=manifest_hash, unknowns=unknowns,
                      fingerprint=_hash({"head": head, "manifest": manifest_hash, "extra_inputs": patterns}))
        if not unknowns:
            result["status"] = "fresh"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        result["unknowns"] = [str(exc)]
    return result


def task_definition_hash(task: dict) -> str:
    metadata = task.get("metadata", {})
    return _hash({"content": task.get("content"), **{key: metadata.get(key, default) for key, default in (
        ("acceptance_criteria", []), ("depends_on", []), ("requires_test", False),
        ("requires_review", False), ("required_profiles", []), ("plan_context_hash", None))}})


def load_profiles(store, project_id, worktree=None) -> dict:
    root = _root(store, project_id, worktree)
    config_path = root / _PROFILE_PATH
    if not config_path.exists():
        return {}
    if not config_path.resolve().is_relative_to(root):
        raise ValueError("Check profile configuration must stay inside its worktree")
    try:
        document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("Cannot read check profiles") from exc
    checks = document.get("checks", {})
    if not isinstance(checks, dict):
        raise ValueError("checks must contain named profiles")
    profiles = {}
    for name, raw in checks.items():
        if not isinstance(raw, dict) or set(raw) - {"argv", "cwd", "timeout_seconds", "output_limit_bytes", "extra_inputs"}:
            raise ValueError(f"Invalid check profile: {name}")
        argv = raw.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x and "\0" not in x for x in argv):
            raise ValueError(f"Check profile {name} requires an argument array")
        cwd = raw.get("cwd", ".")
        if not isinstance(cwd, str) or Path(cwd).is_absolute() or not (root / cwd).resolve().is_relative_to(root):
            raise ValueError("Check cwd must stay inside its worktree")
        timeout = raw.get("timeout_seconds", 600)
        limit = raw.get("output_limit_bytes", MAX_OUTPUT_BYTES)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 86400:
            raise ValueError("Check timeout must be between 0 and 86400 seconds")
        if type(limit) is not int or not 0 < limit <= MAX_OUTPUT_BYTES:
            raise ValueError("Check output limit must be between 1 and 10485760 bytes")
        profile = {"id": name, "argv": argv, "cwd": cwd, "timeout_seconds": timeout,
                   "output_limit_bytes": limit, "extra_inputs": _input_patterns(raw.get("extra_inputs", []))}
        profile["hash"] = _hash(profile)
        profiles[name] = profile
    return profiles


def _stop_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=0.3)
    except subprocess.TimeoutExpired:
        pass
    # A parent may already have exited while its children still hold the pipes.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=3)


def _execute(profile, root, output_dir, on_start=None):
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    process = None
    outcome, error, code = "completed", None, None
    started = time.monotonic()
    old_term = None
    if threading.current_thread() is threading.main_thread():
        old_term = signal.getsignal(signal.SIGTERM)

        def cancelled(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, cancelled)
    logs = {}
    try:
        for name in buffers:
            logs[name] = (output_dir / name).open("wb", buffering=0)
            (output_dir / name).chmod(0o600)
        cwd = (root / profile["cwd"]).resolve()
        if not cwd.is_relative_to(root):
            raise ValueError("Check cwd escaped its worktree")
        process = subprocess.Popen(profile["argv"], cwd=cwd, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, shell=False)
        if on_start is not None:
            on_start(process.pid)
        with selectors.DefaultSelector() as selector:
            for name in buffers:
                selector.register(getattr(process, name), selectors.EVENT_READ, name)
            while selector.get_map():
                if time.monotonic() - started >= profile["timeout_seconds"]:
                    outcome = "timeout"
                    break
                for key, _ in selector.select(timeout=min(0.05, profile["timeout_seconds"])):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    remaining = profile["output_limit_bytes"] - sum(len(b) for b in buffers.values())
                    bounded = chunk[:remaining]
                    buffers[key.data].extend(bounded)
                    logs[key.data].write(bounded)
                    if len(chunk) > remaining:
                        outcome = "output_limit"
                        break
                if outcome != "completed":
                    break
            if outcome != "completed":
                _stop_group(process)
            else:
                remaining_time = max(0.001, profile["timeout_seconds"] - (time.monotonic() - started))
                try:
                    process.wait(timeout=remaining_time)
                except subprocess.TimeoutExpired:
                    outcome = "timeout"
                    _stop_group(process)
        code = process.returncode
    except KeyboardInterrupt:
        outcome = "cancelled"
        if process is not None:
            _stop_group(process)
            code = process.returncode
    except (OSError, ValueError) as exc:
        outcome, error = "error", str(exc)
        if process is not None:
            _stop_group(process)
            code = process.returncode
    finally:
        if old_term is not None:
            signal.signal(signal.SIGTERM, old_term)
        if process is not None:
            for name in buffers:
                getattr(process, name).close()
        for handle in logs.values():
            os.fsync(handle.fileno())
            handle.close()
    return outcome, code, error, buffers


def _insert_run(store, run):
    # The only insertion path is the local runner.  Public/model inputs can only
    # refer to immutable IDs, never supply a completed machine-run payload.
    with store.transaction() as tx:
        if not tx.query("SELECT id FROM projects WHERE id=:id", {"id": run["project_id"]}):
            raise _RunReferencesGone("Project removed before check evidence could be recorded")
        if not tx.query("SELECT id FROM records WHERE id=:id AND project_id=:project AND kind='task'",
                        {"id": run["task_id"], "project": run["project_id"]}):
            raise _RunReferencesGone("Task removed before check evidence could be recorded")
        source_ids = [run["profile_source_id"], *run["source_ids"].values()]
        for source_id in source_ids:
            if not tx.query("SELECT id FROM sources WHERE id=:id AND project_id=:project",
                            {"id": source_id, "project": run["project_id"]}):
                raise _RunReferencesGone("Source removed before check evidence could be recorded")
        existing = tx.query("SELECT FROM check_runs WHERE id=:id", {"id": run["id"]})
        if existing:
            return _clean(existing[0])
        _insert(tx, "check_runs", run)
        return run


def _cancelled_without_evidence(run, error):
    now = _now()
    return {**run, "outcome": "cancelled", "passed": False, "recorded": False,
            "source_ids": {}, "profile_source_id": None, "ended_at": now, "error": error,
            "reasons": [error], "events": [*run.get("events", []), {"state": "cancelled", "at": now}]}


def _cleanup_run_artifacts(settings, active):
    """Remove only our known files in an owned UUID directory, never recurse."""
    run_id = active.get("id")
    if not isinstance(run_id, str) or str(UUID(run_id)) != run_id:
        return
    output_root = (Path(settings.data_dir) / "checks").resolve()
    directory = output_root / run_id
    if directory.is_symlink() or directory.resolve().parent != output_root:
        return
    worktree = Path(active["worktree"]).resolve()
    if directory.is_relative_to(worktree) or worktree.is_relative_to(directory):
        return
    descriptor = directory / "active.json"
    if descriptor.is_symlink() or not descriptor.is_file():
        return
    saved = json.loads(descriptor.read_text(encoding="utf-8"))
    if saved.get("id") != run_id or saved.get("origin") != "local_runner" or saved.get("project_id") != active.get("project_id"):
        return
    for name in ("stdout", "stderr", "active.tmp", "active.json"):
        path = directory / name
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
    try:
        directory.rmdir()
    except OSError:
        # Unexpected files belong to the user until proven otherwise.
        pass


def _process_identity(pid):
    """Linux/WSL process birth token prevents treating a reused PID as the runner."""
    try:
        # comm may contain spaces and parentheses; the suffix starts at field 3.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19] if fields[0] != "Z" else None
    except (OSError, IndexError):
        return None


def _write_active(path, state):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=True)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _output_source(store, project_id, run_id, name, data, remaining, **metadata):
    # Invalid UTF-8 expands when replaced.  Bound the persisted UTF-8 text too,
    # rather than accidentally storing up to three times the raw-output limit.
    rendered = bytes(data).decode("utf-8", errors="replace").encode("utf-8")
    text = rendered[:remaining].decode("utf-8", errors="ignore")
    source = store.snapshot(project_id, f"check://{run_id}/{name}", text, "check_output",
        {"run_id": run_id, "stream": name, "raw_bytes": len(data),
         "raw_sha256": hashlib.sha256(data).hexdigest(),
         "rendered_truncated": len(rendered) > remaining, **metadata})
    return source["id"], len(text.encode("utf-8"))


def recover_interrupted_runs(store, settings) -> list[dict]:
    """Recover dead local CLI runs without changing existing immutable evidence."""
    recovered = []
    for path in sorted((Path(settings.data_dir) / "checks").glob("*/active.json")):
        active, owner_dead = None, False
        try:
            if path.is_symlink() or path.parent.is_symlink() or str(UUID(path.parent.name)) != path.parent.name:
                continue
            active = json.loads(path.read_text(encoding="utf-8"))
            if active.get("id") != path.parent.name or active.get("origin") != "local_runner":
                continue
            pid = active.get("runner_pid")
            if type(pid) is not int or pid <= 1 or not active.get("runner_identity"):
                continue
            if _process_identity(pid) == active["runner_identity"]:
                continue
            owner_dead = True
            if store.query("SELECT id FROM check_runs WHERE id=:id", {"id": active["id"]}):
                path.unlink(missing_ok=True)
                continue
            store._require_project(store, active["project_id"])
            process_pid = active.get("process_pid")
            if (type(process_pid) is int and process_pid > 1 and active.get("process_identity")
                    and _process_identity(process_pid) == active["process_identity"]):
                # This is the original group leader; do not signal a reused PID.
                try:
                    os.killpg(process_pid, signal.SIGTERM)
                    os.killpg(process_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            source_ids, total, rendered_total = {}, 0, 0
            for name in ("stdout", "stderr"):
                log = path.parent / name
                with log.open("rb") if log.exists() else open(os.devnull, "rb") as stream:
                    data = stream.read(MAX_OUTPUT_BYTES - total)
                total += len(data)
                source_ids[name], rendered_size = _output_source(store, active["project_id"], active["id"],
                    name, data, MAX_OUTPUT_BYTES - rendered_total, interrupted=True)
                rendered_total += rendered_size
            ended_at = _now()
            run = {key: active[key] for key in ("id", "project_id", "task_id", "worktree", "origin",
                   "profile_id", "profile_hash", "task_definition_hash", "profile_source_id", "started_at", "code_before")}
            run.update(ended_at=ended_at, outcome="interrupted", returncode=None, passed=False,
                       source_ids=source_ids, output_bytes=total, error="runner interrupted before recording its result",
                       reasons=["runner interrupted; execution result and code state unknown"],
                       code_after={"status": "unknown", "fingerprint": None, "unknowns": ["runner interrupted"]},
                       events=[{"state": "started", "at": run["started_at"]}]
                              + ([{"state": "running", "at": active["running_at"]}] if active.get("running_at") else [])
                              + [{"state": "interrupted", "at": ended_at}])
            recovered.append(_insert_run(store, run))
            path.unlink(missing_ok=True)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if (owner_dead and active and (isinstance(exc, _RunReferencesGone)
                    or not store.query("SELECT id FROM projects WHERE id=:id", {"id": active.get("project_id")}))):
                recovered.append(_cancelled_without_evidence(active, str(exc)))
                try:
                    _cleanup_run_artifacts(settings, active)
                except (OSError, ValueError, KeyError):
                    pass
            # Corrupt spools are retained for repair; never converted into success.
            continue
    return recovered


def run_check(store, settings, project_id, profile_id, task_id, worktree=None) -> dict:
    recover_interrupted_runs(store, settings)
    root = _root(store, project_id, worktree)
    task = next((r for r in store.query("SELECT FROM records WHERE id=:id AND project_id=:project AND kind='task' AND status!='superseded'",
                                      {"id": task_id, "project": project_id})), None)
    if task is None:
        raise ValueError("Check task must be an active task in the project")
    profiles = load_profiles(store, project_id, root)
    if profile_id not in profiles:
        raise ValueError("Unknown check profile")
    profile = profiles[profile_id]
    output_root = (Path(settings.data_dir) / "checks").resolve()
    if output_root.is_relative_to(root):
        raise ValueError("Check output directory must be outside the working tree")
    run_id, started_at = str(uuid4()), _now()
    output_dir = output_root / run_id
    output_dir.mkdir(parents=True, mode=0o700)
    before = capture_code_state(store, project_id, root, profile["extra_inputs"])
    profile_source = store.snapshot(project_id, f"check://{run_id}/profile", _json(profile), "check_profile", {"run_id": run_id})
    definition_hash = task_definition_hash(task)
    active = {"id": run_id, "project_id": project_id, "task_id": task_id, "worktree": str(root),
              "origin": "local_runner", "profile_id": profile_id, "profile_hash": profile["hash"],
              "task_definition_hash": definition_hash, "profile_source_id": profile_source["id"],
              "started_at": started_at, "code_before": before,
              "runner_pid": os.getpid(), "runner_identity": _process_identity(os.getpid())}
    active_path = output_dir / "active.json"
    _write_active(active_path, active)

    def on_start(pid):
        active.update(process_pid=pid, process_identity=_process_identity(pid), running_at=_now())
        _write_active(active_path, active)

    outcome, returncode, error, buffers = _execute(profile, root, output_dir, on_start)
    try:
        run = _finish_run(store, root, profile, active, outcome, returncode, error, buffers)
    except ValueError as exc:
        if not isinstance(exc, _RunReferencesGone) and store.query("SELECT id FROM projects WHERE id=:id", {"id": project_id}):
            raise
        run = _cancelled_without_evidence({**active, "returncode": returncode,
            "output_bytes": sum(len(b) for b in buffers.values())}, str(exc))
        try:
            _cleanup_run_artifacts(settings, active)
        except (OSError, ValueError, KeyError):
            pass
        return run
    active_path.unlink(missing_ok=True)
    return run


def _finish_run(store, root, profile, active, outcome, returncode, error, buffers):
    project_id, task_id, run_id = active["project_id"], active["task_id"], active["id"]
    before, definition_hash, started_at = active["code_before"], active["task_definition_hash"], active["started_at"]
    profile_id = active["profile_id"]
    after = capture_code_state(store, project_id, root, profile["extra_inputs"])
    reasons = []
    if before["status"] != "fresh" or after["status"] != "fresh":
        reasons.append("code state unknown")
    if before["fingerprint"] != after["fingerprint"]:
        reasons.append("code changed during check")
    try:
        current_profile = load_profiles(store, project_id, root).get(profile_id)
    except ValueError:
        current_profile = None
    if current_profile is None or current_profile["hash"] != profile["hash"]:
        reasons.append("profile changed during check")
    current_task = store.query("SELECT FROM records WHERE id=:id AND project_id=:project AND status!='superseded'", {"id": task_id, "project": project_id})
    if not current_task or task_definition_hash(current_task[0]) != definition_hash:
        reasons.append("task definition changed during check")
    source_ids, rendered_total = {}, 0
    for name, data in buffers.items():
        source_ids[name], rendered_size = _output_source(store, project_id, run_id, name, data,
            profile["output_limit_bytes"] - rendered_total, truncated=outcome == "output_limit")
        rendered_total += rendered_size
    ended_at = _now()
    run = {"id": run_id, "project_id": project_id, "task_id": task_id, "worktree": str(root),
           "origin": "local_runner", "profile_id": profile_id, "profile_hash": profile["hash"],
           "task_definition_hash": definition_hash, "profile_source_id": active["profile_source_id"],
           "source_ids": source_ids, "code_before": before, "code_after": after,
           "started_at": started_at, "ended_at": ended_at, "outcome": outcome, "returncode": returncode,
           "passed": outcome == "completed" and returncode == 0 and not reasons,
           "reasons": reasons, "error": error, "output_bytes": sum(len(b) for b in buffers.values()),
           "events": [{"state": "started", "at": started_at}]
                     + ([{"state": "running", "at": active["running_at"]}] if active.get("running_at") else [])
                     + [{"state": outcome, "at": ended_at}]}
    run = _insert_run(store, run)
    return run


def _verification_status(store, reader, task, worktree=None, capture=None):
    # A display-only caller may pass a memoizing capture; the completion gate never does.
    capture = capture or capture_code_state
    metadata = task.get("metadata", {})
    project_id = task["project_id"]
    root = _root(store, project_id, worktree)
    runs = [_clean(r) for r in reader.query("SELECT FROM check_runs WHERE project_id=:project AND task_id=:task ORDER BY ended_at DESC,id",
                                           {"project": project_id, "task": task["id"]})]
    scoped = [r for r in runs if r.get("worktree") == str(root)]
    try:
        profiles = load_profiles(store, project_id, root)
    except ValueError:
        return {"status": "unknown", "reasons": ["check profiles unavailable"], "run_ids": []}
    required = metadata.get("required_profiles", [])
    if not isinstance(required, list) or not all(isinstance(p, str) and p for p in required):
        return {"status": "unknown", "reasons": ["invalid required profiles"], "run_ids": []}
    missing = [p for p in required if p not in profiles]
    if missing:
        return {"status": "unknown", "reasons": [f"required profile unavailable: {p}" for p in missing], "run_ids": []}
    definition = task_definition_hash(task)
    eligible = []
    unknowns = []
    # Required checks must describe one stable observed input horizon.  Reading
    # unit inputs on tree A and integration inputs later on tree B cannot certify
    # the combination.  A single check already has a stable double capture.
    horizon = None
    horizon_inputs = sorted({p for name in required for p in profiles[name]["extra_inputs"]})
    if len(required) > 1:
        horizon = capture(store, project_id, root, horizon_inputs)
        if horizon["status"] != "fresh":
            return {"status": "unknown", "reasons": horizon["unknowns"], "run_ids": []}
    for profile_id in required or profiles:
        profile = profiles[profile_id]
        candidates = [r for r in scoped if r.get("profile_id") == profile_id and r.get("origin") == "local_runner"
                      and r.get("task_definition_hash") == definition and r.get("profile_hash") == profile["hash"]]
        if not candidates:
            continue
        current = capture(store, project_id, root, profile["extra_inputs"])
        if current["status"] != "fresh":
            unknowns.extend(current["unknowns"])
            continue
        # The latest matching attempt wins; a newer failure must not be hidden
        # by an older success on an otherwise identical tree.
        run = next((r for r in candidates if r.get("code_before", {}).get("fingerprint") == current["fingerprint"]), None)
        if (run and run.get("passed") is True and run.get("outcome") == "completed" and run.get("returncode") == 0
                and run.get("code_before", {}).get("status") == "fresh"
                and run.get("code_after", {}).get("status") == "fresh"
                and run["code_after"].get("fingerprint") == current["fingerprint"]):
            eligible.append(run)
            if not required:
                break
    found = {r["profile_id"] for r in eligible}
    if eligible and (not required or set(required) <= found):
        if horizon is not None:
            after = capture(store, project_id, root, horizon_inputs)
            if after["status"] != "fresh":
                return {"status": "unknown", "reasons": after["unknowns"], "run_ids": []}
            if after["fingerprint"] != horizon["fingerprint"]:
                return {"status": "stale", "reasons": ["inputs changed while checking required evidence"], "run_ids": []}
        try:
            current_profiles = load_profiles(store, project_id, root)
        except ValueError:
            current_profiles = {}
        if any(current_profiles.get(r["profile_id"], {}).get("hash") != r["profile_hash"] for r in eligible):
            return {"status": "stale", "reasons": ["profile changed while checking evidence"], "run_ids": []}
        return {"status": "fresh", "reasons": [], "run_ids": [r["id"] for r in eligible]}
    if unknowns:
        return {"status": "unknown", "reasons": sorted(set(unknowns)), "run_ids": []}
    if any(r.get("origin") != "portable_import" for r in scoped):
        return {"status": "stale", "reasons": ["no successful matching run for current code, profile and task definition"], "run_ids": []}
    if any(r.get("origin") == "portable_import" for r in runs):
        return {"status": "reported", "reasons": ["imported machine checks are historical; a new local run is required"],
                "run_ids": []}
    if metadata.get("test_passed") or metadata.get("test_source_ids"):
        return {"status": "reported", "reasons": ["historical/client-reported evidence is not a machine check"], "run_ids": []}
    return {"status": "unknown", "reasons": ["no machine checks for this task and worktree"], "run_ids": []}


def verification_status(store, task, worktree=None, *, capture=None) -> dict:
    return _verification_status(store, store, task, worktree, capture)


def validate_completion(store, tx, project_id, task: dict) -> None:
    if task.get("project_id") != project_id:
        raise ValueError("Machine verification task belongs to another project")
    if not task.get("metadata", {}).get("requires_test"):
        return
    # Session/manual completion scope is transient: plans and task progress are
    # shared across roots.  It must not become a permanent task definition field.
    worktree = task.get("_completion_worktree")
    state = _verification_status(store, tx, task, worktree)
    if state["status"] != "fresh":
        raise ValueError("Task completion requires fresh machine check evidence: " + "; ".join(state["reasons"]))
