"""Fast, read-only project memory consistency micro-audit (`cairnmind pulse`).

Performs four read-only checks without starting the engine/service, without creating
lock or temporary files, and without running full-text reindexing:
1. Git & fingerprint drift check (targeted to dirty/cited paths, never scanning 50k files).
2. Test freshness check (mirrors `verification.validate_completion` rules).
3. Degraded evidence audit (`origin_evidence: degraded` / external & third-party writes).
4. Snapshot & reference integrity (`.memory/cairnmind/`, DB references, lock/migration marks).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from typing import Any

from .arcade import ArcadeError
from .config import Settings
from .database_mode import describe as describe_storage
from .evidence import evidence_states, third_party_origin
from .memory_paths import (
    MEMORY_DIRECTORIES,
    MEMORY_RELATIVE,
    git_pathspec,
    is_reserved_below,
    is_reserved_memory,
    nested_reserved,
    snapshot_directory,
)
from .store import Store, degraded_origin
from .store_readonly import StoreNotReady
from .terminal_safe import safe_text

# Client/Cairnmind-managed config files that must never trigger false code drift.
MANAGED_CONFIG_FILES = frozenset({
    ".mcp.json",
    ".cursor/mcp.json",
    ".vscode/mcp.json",
    ".roo/mcp.json",
    ".codex/config.toml",
    ".codex/hooks.json",
    ".claude/settings.local.json",
    ".claude/agents/cairnmind-recorder.md",
    ".gemini/settings.json",
    ".zed/settings.json",
    "AGENTS.md",
    "CLAUDE.md",
})

MANAGED_CONFIG_PREFIXES = (".cairnmind/", ".devbrain/")
MANAGED_BACKUP_MARKERS = (".cairnmind.bak", ".cairnmind-backup-", ".devbrain-backup-")

_FILE_KINDS = ("code", "document", "file")
_ACTIVE_TASK_STATES = ("in_progress", "needs_test", "needs_review")
_SMALL_REPO_FILE_LIMIT = 500
_NO_GIT_SOURCE_LIMIT = 2000


def _strip_dot_slash(path: str) -> str:
    norm = path.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm.rstrip("/")


def is_managed_config_path(relative: str) -> bool:
    """Whether a repo-relative POSIX path is a Cairnmind-managed config or backup file."""
    norm = _strip_dot_slash(relative)
    if norm in MANAGED_CONFIG_FILES or norm in (".cairnmind", ".devbrain"):
        return True
    if any(norm.startswith(prefix) for prefix in MANAGED_CONFIG_PREFIXES):
        return True
    name = Path(norm).name
    return any(marker in name for marker in MANAGED_BACKUP_MARKERS)


def _git_run(root: Path, *args: str, timeout: float = 5.0) -> subprocess.CompletedProcess[bytes] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _file_sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _git_info(root: Path) -> dict[str, Any]:
    """Inspect Git state cheaply without acquiring optional index locks."""
    inside = _git_run(root, "rev-parse", "--is-inside-work-tree")
    if inside is None or inside.returncode != 0 or inside.stdout.strip() != b"true":
        return {"available": False, "has_commits": False, "toplevel": None, "head": None, "branch": None}
    top_proc = _git_run(root, "rev-parse", "--show-toplevel")
    toplevel = (
        str(Path(os.fsdecode(top_proc.stdout).strip()).resolve())
        if top_proc is not None and top_proc.returncode == 0 and top_proc.stdout.strip()
        else None
    )
    head_proc = _git_run(root, "rev-parse", "--verify", "-q", "HEAD")
    if head_proc is None or head_proc.returncode != 0 or not head_proc.stdout.strip():
        # Fresh `git init` without commits -> fallback mode as required.
        return {"available": False, "has_commits": False, "toplevel": toplevel, "head": None, "branch": None}
    head = head_proc.stdout.decode("ascii", errors="replace").strip()
    branch_proc = _git_run(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    branch = (
        branch_proc.stdout.decode("utf-8", errors="replace").strip()
        if branch_proc is not None and branch_proc.returncode == 0
        else None
    )
    return {
        "available": True,
        "has_commits": True,
        "toplevel": toplevel,
        "head": head,
        "branch": branch or "detached",
    }


def _parse_porcelain_v2(raw: bytes, root: Path, toplevel: Path | None, nested: tuple[Path, ...]) -> list[str]:
    """Parse `git status --porcelain=v2 -z` into root-relative POSIX paths, ignoring managed/reserved paths."""
    tokens = raw.split(b"\0")
    changed: list[str] = []
    index = 0
    prefix = ""
    if toplevel is not None:
        try:
            rel_root = root.resolve().relative_to(toplevel.resolve())
            if str(rel_root) != ".":
                prefix = rel_root.as_posix().rstrip("/") + "/"
        except ValueError:
            prefix = ""

    def normalize(raw_path: str) -> str | None:
        rel = raw_path
        if prefix:
            if not rel.startswith(prefix):
                return None
            rel = rel[len(prefix):]
        rel = rel.rstrip("/")
        if not rel or is_reserved_memory(rel) or is_reserved_below(rel, nested) or is_managed_config_path(rel):
            return None
        return rel

    while index < len(tokens):
        entry = tokens[index]
        index += 1
        if not entry:
            continue
        text = os.fsdecode(entry)
        kind = text[:1]
        if kind == "1":
            # 1 XY sub mH mI mW hH hI path
            parts = text.split(" ", 8)
            if len(parts) == 9:
                norm = normalize(parts[8])
                if norm:
                    changed.append(norm)
        elif kind == "2":
            # 2 XY sub mH mI mW hH hI Xscore path\0origPath
            parts = text.split(" ", 9)
            if len(parts) == 10:
                norm = normalize(parts[9])
                if norm:
                    changed.append(norm)
            if index < len(tokens):
                orig = normalize(os.fsdecode(tokens[index]))
                index += 1
                if orig:
                    changed.append(orig)
        elif kind == "u":
            # u XY sub m1 m2 m3 mW h1 h2 h3 path
            parts = text.split(" ", 10)
            if len(parts) == 11:
                norm = normalize(parts[10])
                if norm:
                    changed.append(norm)
        elif kind == "?":
            # ? path
            norm = normalize(text[2:])
            if norm:
                changed.append(norm)
    return sorted(set(changed))


def _tracked_file_count(root: Path, nested: tuple[Path, ...]) -> int | None:
    proc = _git_run(root, "ls-files", "-z", *git_pathspec(nested))
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout.count(b"\0")


def _resolve_project_and_root(
    store: Store,
    project: str | None,
    worktree: Path | str | None,
) -> tuple[dict[str, Any], Path]:
    """Resolve the target project and exact worktree without mixing other worktrees."""
    from .cli import actor_scope
    from .verification import _root

    if project is not None:
        selected = store.resolve_project(project)
        scope = actor_scope(store, selected["id"], worktree)
        if scope is None:
            # Prefer the registered worktree containing cwd when running inside a linked worktree.
            cwd = Path.cwd().resolve()
            git_top = _git_info(cwd).get("toplevel")
            candidate = Path(git_top).resolve() if git_top else cwd
            rows = store.query("SELECT path FROM project_roots WHERE project_id=:project", {"project": selected["id"]})
            roots = [Path(r["path"]).resolve() for r in rows if isinstance(r.get("path"), str)]
            inside = [r for r in roots if candidate == r or candidate.is_relative_to(r)]
            if inside:
                scope = str(max(inside, key=lambda p: len(str(p))))
        root = _root(store, selected["id"], scope)
        return selected, root

    # Project omitted: infer from --worktree or current working directory / git toplevel.
    base = Path(worktree).expanduser().resolve() if worktree else Path.cwd().resolve()
    git_top = _git_info(base).get("toplevel")
    probe_paths = [base]
    if git_top and Path(git_top).resolve() != base:
        probe_paths.append(Path(git_top).resolve())

    rows = store.query("SELECT project_id, path FROM project_roots")
    matches: list[tuple[int, str, Path]] = []
    for row in rows:
        raw_path = row.get("path")
        pid = row.get("project_id")
        if not isinstance(raw_path, str) or not isinstance(pid, str):
            continue
        reg = Path(raw_path).resolve()
        for probe in probe_paths:
            if probe == reg or probe.is_relative_to(reg):
                matches.append((len(str(reg)), pid, reg))
                break
    if not matches:
        raise ValueError(
            f"No registered Cairnmind project contains {base}. "
            "Pass PROJECT explicitly or run `cairnmind project add NAME FOLDER`."
        )
    _, pid, matched_root = max(matches, key=lambda item: item[0])
    selected = store.resolve_project(pid)
    scope = actor_scope(store, selected["id"], worktree or matched_root)
    root = _root(store, selected["id"], scope)
    return selected, root


def _latest_baseline(store: Store, project_id: str, root: Path) -> dict[str, Any] | None:
    """Latest recorded Git (head, branch) for this exact worktree from check runs or resume packets."""
    worktree_str = str(root)
    runs = store.query(
        "SELECT ended_at, code_after FROM check_runs "
        "WHERE project_id=:project AND worktree=:worktree AND origin='local_runner' "
        "ORDER BY ended_at DESC LIMIT 5",
        {"project": project_id, "worktree": worktree_str},
    )
    for row in runs:
        code = row.get("code_after")
        if isinstance(code, dict) and isinstance(code.get("head"), str) and code["head"]:
            return {
                "source": "check_run",
                "head": code["head"],
                "branch": code.get("branch"),
                "at": row.get("ended_at"),
            }
    packets = store.query(
        "SELECT created_at, payload FROM resume_packets "
        "WHERE project_id=:project AND worktree=:worktree "
        "ORDER BY created_at DESC LIMIT 3",
        {"project": project_id, "worktree": worktree_str},
    )
    for row in packets:
        payload = row.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = None
        if isinstance(payload, dict):
            code = payload.get("code")
            if isinstance(code, dict) and isinstance(code.get("head"), str) and code["head"]:
                return {
                    "source": "resume_packet",
                    "head": code["head"],
                    "branch": code.get("branch"),
                    "at": row.get("created_at"),
                }
    return None


def _source_relative_path(source: dict[str, Any], root: Path) -> str | None:
    """Resolve a file source's path relative to `root` without escaping `root`."""
    meta = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
    raw_path = meta.get("path") or source.get("uri")
    if not isinstance(raw_path, str) or not raw_path or "://" in raw_path:
        return None
    candidate = Path(raw_path)
    if candidate.is_absolute():
        try:
            rel = candidate.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return None
    else:
        rel = _strip_dot_slash(candidate.as_posix())
    if not rel or rel.startswith("../") or is_reserved_memory(rel) or is_managed_config_path(rel):
        return None
    return rel


def _check_file_drift(
    store: Store,
    project_id: str,
    root: Path,
    git: dict[str, Any],
    active_records: list[dict[str, Any]],
    active_plan: dict[str, Any] | None,
    nested: tuple[Path, ...],
) -> tuple[list[str], list[str], list[str], dict[str, Any] | None]:
    """Check targeted code/evidence drift. Returns (drift_files, dirty_git_files, broken_db_refs, divergence)."""
    drift_files: set[str] = set()
    dirty_git_files: list[str] = []
    broken_refs: list[str] = []
    divergence: dict[str, Any] | None = None

    # 1. Branch / HEAD divergence against latest recorded baseline for this worktree.
    if git["available"]:
        baseline = _latest_baseline(store, project_id, root)
        if baseline and baseline.get("head") and baseline["head"] != git["head"]:
            divergence = {
                "previous_head": baseline["head"],
                "current_head": git["head"],
                "previous_branch": baseline.get("branch"),
                "current_branch": git.get("branch"),
                "baseline_source": baseline.get("source"),
            }
        # 2. Fast Git status for dirty/untracked files in this worktree (-uall lists exact untracked paths so
        #    managed files like .cursor/mcp.json are filtered cleanly while other files in .cursor/ are still caught).
        proc = _git_run(root, "status", "--porcelain=v2", "-z", "-uall", *git_pathspec(nested))
        if proc is not None and proc.returncode == 0:
            toplevel = Path(git["toplevel"]) if git.get("toplevel") else None
            dirty_git_files = _parse_porcelain_v2(proc.stdout, root, toplevel, nested)
            drift_files.update(dirty_git_files)

    # 3. Targeted check of file sources cited by active plans, tasks, and records.
    cited_ids: set[str] = set()
    for record in active_records:
        for sid in record.get("source_ids") or []:
            if isinstance(sid, str):
                cited_ids.add(sid)
    if active_plan:
        for sid in active_plan.get("source_ids") or []:
            if isinstance(sid, str):
                cited_ids.add(sid)

    sources_by_id: dict[str, dict[str, Any]] = {}
    if cited_ids:
        rows = store.query(
            "SELECT id, project_id, uri, kind, sha256, is_current, metadata FROM sources WHERE id IN :ids",
            {"ids": sorted(cited_ids)},
        )
        sources_by_id = {r["id"]: r for r in rows if r.get("project_id") == project_id}

    # Check missing source references in DB for active records/plan.
    for record in active_records:
        for sid in record.get("source_ids") or []:
            if isinstance(sid, str) and sid not in sources_by_id:
                broken_refs.append(f"record:{record['id']} -> missing source:{sid}")
    if active_plan:
        for sid in active_plan.get("source_ids") or []:
            if isinstance(sid, str) and sid not in sources_by_id:
                broken_refs.append(f"plan:{active_plan['id']} -> missing source:{sid}")

    # Check cited file sources against current disk state.
    checked_paths: set[str] = set()
    for source in sources_by_id.values():
        if source.get("kind") not in _FILE_KINDS:
            continue
        rel = _source_relative_path(source, root)
        if not rel:
            continue
        checked_paths.add(rel)
        disk_path = root / rel
        if not disk_path.exists():
            drift_files.add(rel)
            continue
        expected_sha = source.get("sha256")
        if isinstance(expected_sha, str) and expected_sha:
            actual_sha = _file_sha256(disk_path)
            if actual_sha != expected_sha:
                drift_files.add(rel)

    # 4. No-Git fallback: check current indexed file sources via fast (mtime_ns, size) + SHA-256.
    if not git["available"]:
        current_files = store.query(
            "SELECT id, uri, kind, sha256, metadata FROM sources "
            f"WHERE project_id=:project AND is_current=true AND kind IN :kinds LIMIT {_NO_GIT_SOURCE_LIMIT}",
            {"project": project_id, "kinds": list(_FILE_KINDS)},
        )
        for source in current_files:
            rel = _source_relative_path(source, root)
            if not rel or rel in checked_paths:
                continue
            checked_paths.add(rel)
            disk_path = root / rel
            if not disk_path.exists():
                drift_files.add(rel)
                continue
            meta = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
            try:
                st = disk_path.stat()
            except OSError:
                drift_files.add(rel)
                continue
            stored_mtime = meta.get("mtime_ns")
            stored_size = meta.get("size")
            if (
                isinstance(stored_mtime, int)
                and isinstance(stored_size, int)
                and st.st_mtime_ns == stored_mtime
                and st.st_size == stored_size
            ):
                continue
            expected_sha = source.get("sha256")
            if isinstance(expected_sha, str) and expected_sha:
                if _file_sha256(disk_path) != expected_sha:
                    drift_files.add(rel)

    return sorted(drift_files), dirty_git_files, sorted(set(broken_refs)), divergence


def _check_test_freshness(
    store: Store,
    project: dict[str, Any],
    root: Path,
    tasks: list[dict[str, Any]],
    active_plan: dict[str, Any] | None,
    git: dict[str, Any],
    dirty_git_files: list[str],
    nested: tuple[Path, ...],
) -> tuple[list[str], bool, list[str], list[str], list[str]]:
    """Verify active/closing tasks have fresh test results matching current code.

    Returns (stale_test_tasks, would_block_done, recommended_actions, warnings, integrity_errors).
    """
    from .verification import (
        _verification_status,
        capture_code_state,
        load_profiles,
    )

    project_id = project["id"]
    project_label = project.get("name") or project_id
    stale_tasks: list[str] = []
    would_block_done = False
    actions: list[str] = []
    warnings: list[str] = []
    integrity_errors: list[str] = []

    try:
        profiles = load_profiles(store, project_id, root)
    except ValueError as exc:
        integrity_errors.append(f"Invalid check profiles configuration: {exc}")
        return [], True, [], warnings, integrity_errors

    active_plan_steps = set(active_plan.get("step_ids") or []) if active_plan else set()
    relevant_tasks = [
        t
        for t in tasks
        if t.get("status") != "superseded"
        and (
            (t.get("metadata") or {}).get("state", "todo") in _ACTIVE_TASK_STATES
            or (
                t["id"] in active_plan_steps
                and (t.get("metadata") or {}).get("state", "todo") != "done"
                and (
                    (t.get("metadata") or {}).get("requires_test")
                    or (t.get("metadata") or {}).get("required_profiles")
                )
            )
        )
    ]

    if not relevant_tasks:
        return [], False, [], warnings, integrity_errors

    # Warn (not error) when active tasks have no configured test profiles.
    if not profiles:
        warnings.append("unverified_no_test_profile")

    # Decide between exact capture (small/medium repos) and fast Git check (large monorepos).
    file_count = _tracked_file_count(root, nested) if git["available"] else None
    use_exact = bool(git["available"] and file_count is not None and file_count <= _SMALL_REPO_FILE_LIMIT)

    observed: dict[tuple[str, ...], dict[str, Any]] = {}

    def memo_capture(st, pid, wt=None, extra_inputs=None):
        key = tuple(sorted(extra_inputs or ()))
        if key not in observed:
            observed[key] = capture_code_state(st, pid, wt, extra_inputs)
        return observed[key]

    # Bulk-load check runs for relevant tasks when using fast path.
    runs_by_task: dict[str, list[dict[str, Any]]] = {}
    if not use_exact:
        task_ids = [t["id"] for t in relevant_tasks]
        rows = store.query(
            "SELECT FROM check_runs WHERE project_id=:project AND task_id IN :tasks ORDER BY ended_at DESC, id",
            {"project": project_id, "tasks": task_ids},
        )
        for row in rows:
            if row.get("worktree") == str(root):
                runs_by_task.setdefault(row["task_id"], []).append(row)

    for task in relevant_tasks:
        meta = task.get("metadata") or {}
        requires_test = bool(meta.get("requires_test"))
        required_profiles = [p for p in (meta.get("required_profiles") or []) if isinstance(p, str) and p]
        linked_profiles = [
            p for p in ((meta.get("criterion_checks") or {}).values()) if isinstance(p, str) and p
        ] if isinstance(meta.get("criterion_checks"), dict) else []
        needed_profiles = list(dict.fromkeys(required_profiles or linked_profiles))

        if not requires_test and not needed_profiles:
            continue

        if not profiles:
            if requires_test:
                stale_tasks.append(task["id"])
                would_block_done = True
                actions.append(
                    f"Configure a test profile in .cairnmind/checks.toml and run "
                    f"`cairnmind check run {shlex.quote(project_label)} <profile> --task {task['id']}`"
                )
            continue

        if use_exact:
            status = _verification_status(store, store, task, root, capture=memo_capture)
            is_fresh = status.get("status") == "fresh"
        else:
            is_fresh = _fast_task_fresh(task, profiles, runs_by_task.get(task["id"], []), git, dirty_git_files)

        if not is_fresh:
            stale_tasks.append(task["id"])
            if requires_test:
                would_block_done = True
            target_profiles = needed_profiles or list(profiles.keys())[:1]
            for prof in target_profiles:
                if prof in profiles:
                    actions.append(
                        f"cairnmind check run {shlex.quote(project_label)} {shlex.quote(prof)} --task {task['id']}"
                    )
                else:
                    actions.append(
                        f"Declare missing check profile '{prof}' in .cairnmind/checks.toml for task {task['id']}"
                    )

    return sorted(set(stale_tasks)), would_block_done, list(dict.fromkeys(actions)), sorted(set(warnings)), integrity_errors


def _fast_task_fresh(
    task: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
    scoped_runs: list[dict[str, Any]],
    git: dict[str, Any],
    dirty_git_files: list[str],
) -> bool:
    """Fast staleness check for large monorepos (> 500 tracked files) or non-Git folders."""
    from .verification import task_definition_hash

    if not git["available"] or dirty_git_files:
        # Either Git is unavailable (machine verification needs Git) or uncommitted changes exist since HEAD.
        return False
    meta = task.get("metadata") or {}
    required = meta.get("required_profiles") or []
    if any(p not in profiles for p in required):
        return False
    definition = task_definition_hash(task)
    target_profiles = required or list(profiles.keys())
    found: set[str] = set()
    for profile_id in target_profiles:
        profile = profiles[profile_id]
        candidates = [
            r
            for r in scoped_runs
            if r.get("profile_id") == profile_id
            and r.get("origin") == "local_runner"
            and r.get("task_definition_hash") == definition
            and r.get("profile_hash") == profile["hash"]
        ]
        if not candidates:
            continue
        latest = candidates[0]
        before = latest.get("code_before") or {}
        after = latest.get("code_after") or {}
        if (
            latest.get("passed") is True
            and latest.get("outcome") == "completed"
            and latest.get("returncode") == 0
            and before.get("status") == "fresh"
            and after.get("status") == "fresh"
            and before.get("fingerprint") == after.get("fingerprint")
            and after.get("head") == git["head"]
        ):
            found.add(profile_id)
            if not required:
                return True
    return bool(found) and (not required or set(required) <= found)


def _check_degraded(
    store: Store,
    project_id: str,
    active_records: list[dict[str, Any]],
    active_plan: dict[str, Any] | None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Audit active records and active plan for `origin_evidence: degraded`."""
    degraded_ids: set[str] = set()
    details: list[dict[str, Any]] = []
    states = evidence_states(store, project_id, active_records, quotes=False) if active_records else {}
    for record in active_records:
        meta = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        st = states.get(record["id"], {})
        if st.get("evidence_state") == "degraded" or degraded_origin(meta):
            degraded_ids.add(record["id"])
            details.append({
                "id": record["id"],
                "kind": record.get("kind"),
                "origin_client": meta.get("origin_client"),
                "degraded_by": "third_party_tool" if third_party_origin(meta) else st.get("degraded_by", "external"),
            })
    if active_plan and degraded_origin(active_plan):
        degraded_ids.add(active_plan["id"])
        details.append({
            "id": active_plan["id"],
            "kind": "plan",
            "origin_client": active_plan.get("origin_client"),
            "degraded_by": "third_party_tool" if third_party_origin(active_plan) else "external",
        })
    return sorted(degraded_ids), details


def _check_snapshot_and_locks(
    settings: Settings,
    root: Path,
    active_plan: dict[str, Any] | None,
    tasks_by_id: dict[str, dict[str, Any]],
    git: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Verify `.memory/cairnmind/` integrity, plan-step refs, and lock/migration marks."""
    broken_refs: list[str] = []
    integrity_errors: list[str] = []

    # 1. Blocked migration lock or unfinished operational state.
    for lock_candidate in (
        settings.data_dir / "migration.lock",
        root / ".cairnmind" / "migration.lock",
        root / MEMORY_RELATIVE / "migration.lock",
    ):
        if lock_candidate.exists() or lock_candidate.is_symlink():
            integrity_errors.append(f"Blocked migration lock present: {lock_candidate}")
            broken_refs.append(f"lock:{lock_candidate}")

    from . import setup_mark, upgrade_mark

    if reason := setup_mark.unchecked(settings.data_dir):
        integrity_errors.append(str(reason))
    elif reason := setup_mark.refusal(settings.data_dir):
        integrity_errors.append(str(reason))
    if reason := upgrade_mark.refusal():
        integrity_errors.append(str(reason))

    # 2. Active plan step reference integrity.
    if active_plan:
        for step_id in active_plan.get("step_ids") or []:
            if step_id not in tasks_by_id:
                broken_refs.append(f"plan:{active_plan['id']} -> missing task:{step_id}")

    # 3. Portable snapshot integrity in `.memory/cairnmind/` (or `.memory/devbrain/`).
    snap_dir = snapshot_directory(root)
    if snap_dir is None:
        # Check if `.memory/cairnmind` exists with a broken or missing manifest.
        for candidate_rel in MEMORY_DIRECTORIES:
            candidate = root / candidate_rel
            if candidate.is_dir() and any(candidate.iterdir()):
                manifest = candidate / "manifest.json"
                if not manifest.exists() and not manifest.is_symlink():
                    broken_refs.append(f"snapshot:{candidate_rel.as_posix()}/manifest.json missing")
                break
    else:
        _verify_snapshot_dir(snap_dir, root, git, broken_refs)

    return sorted(set(broken_refs)), integrity_errors


def _verify_snapshot_dir(snap_dir: Path, root: Path, git: dict[str, Any], broken_refs: list[str]) -> None:
    """Fast structural verification of a `.memory/cairnmind/` snapshot directory."""
    from .memory import FORMATS, VERSION

    manifest_path = snap_dir / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        broken_refs.append(f"snapshot:{manifest_path.relative_to(root).as_posix()} is missing or symlink")
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        broken_refs.append(f"snapshot:{manifest_path.relative_to(root).as_posix()} invalid JSON ({exc})")
        return
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") not in FORMATS
        or manifest.get("version") != VERSION
        or not isinstance(manifest.get("objects"), dict)
    ):
        broken_refs.append(f"snapshot:{manifest_path.relative_to(root).as_posix()} invalid manifest schema")
        return

    snap_resolved = snap_dir.resolve()
    missing_objects = 0
    for table, descriptors in manifest["objects"].items():
        if not isinstance(descriptors, list):
            broken_refs.append(f"snapshot:invalid object list for {table}")
            continue
        for desc in descriptors:
            rel_obj = desc.get("path") if isinstance(desc, dict) else None
            if not isinstance(rel_obj, str) or not rel_obj:
                broken_refs.append(f"snapshot:{table} entry missing object path")
                continue
            obj_path = snap_dir / rel_obj
            try:
                if obj_path.is_symlink() or not obj_path.is_file() or not obj_path.resolve().is_relative_to(snap_resolved):
                    missing_objects += 1
                    if missing_objects <= 10:
                        broken_refs.append(f"snapshot:missing object {rel_obj}")
            except OSError:
                missing_objects += 1
                if missing_objects <= 10:
                    broken_refs.append(f"snapshot:unreadable object {rel_obj}")
    if missing_objects > 10:
        broken_refs.append(f"snapshot:{missing_objects - 10} more missing objects")

    # Check stale commit reference in manifest if recorded.
    commit = manifest.get("git_head") or (manifest.get("code") or {}).get("head") if isinstance(manifest.get("code"), dict) else manifest.get("git_head")
    if isinstance(commit, str) and commit and git["available"]:
        check = _git_run(root, "cat-file", "-e", f"{commit}^{{commit}}")
        if check is None or check.returncode != 0:
            broken_refs.append(f"snapshot:commit {commit[:12]} not found in Git history")


def _active_plan(store: Store, project_id: str, root: Path) -> dict[str, Any] | None:
    """Read the active plan row for this exact worktree without failing on missing step references."""
    rows = store.query(
        "SELECT active_plan_id FROM worktree_states WHERE project_id=:project AND worktree=:worktree",
        {"project": project_id, "worktree": str(root)},
    )
    plan_id = rows[0].get("active_plan_id") if rows else None
    if not isinstance(plan_id, str) or not plan_id:
        return None
    plans = store.query(
        "SELECT FROM plans WHERE id=:id AND project_id=:project",
        {"id": plan_id, "project": project_id},
    )
    return plans[0] if plans else None


def _backend_info(settings: Settings) -> tuple[str, str]:
    info = describe_storage(settings)
    mode = info.get("mode", "managed")
    if mode == "managed":
        engine = info.get("engine", "native")
        return "arcadedb", f"managed_{engine}"
    return "arcadedb", "connect"


def run_pulse(
    settings: Settings,
    project: str | None = None,
    worktree: Path | str | None = None,
    *,
    store: Store | None = None,
) -> dict[str, Any]:
    """Execute the read-only memory pulse micro-audit and return a structured report."""
    started = time.perf_counter()
    backend, backend_mode = _backend_info(settings)

    def finalize(payload: dict[str, Any]) -> dict[str, Any]:
        payload["latency_ms"] = max(1, int(round((time.perf_counter() - started) * 1000)))
        return payload

    def broken(errors: list[str], *, broken_refs: list[str] | None = None, actions: list[str] | None = None) -> dict[str, Any]:
        return finalize({
            "status": "broken",
            "latency_ms": 0,
            "backend": backend,
            "backend_mode": backend_mode,
            "project": None,
            "worktree": str(Path(worktree).expanduser().resolve()) if worktree else None,
            "git_available": False,
            "branch_or_tree_diverged": False,
            "drift_files": [],
            "stale_test_tasks": [],
            "degraded_entities": [],
            "broken_refs": broken_refs or [],
            "warnings": [],
            "integrity_errors": errors,
            "would_block_done": True,
            "recommended_actions": actions or [],
        })

    # Fast pre-check for top-level data_dir migration lock even if DB is unreachable.
    top_lock = settings.data_dir / "migration.lock"
    if top_lock.exists() or top_lock.is_symlink():
        return broken(
            [f"Blocked migration lock present: {top_lock}"],
            broken_refs=[f"lock:{top_lock}"],
            actions=[f"Verify no migration is active, then remove {top_lock}"],
        )

    owns_store = store is None
    if store is None:
        try:
            store = Store.open_readonly(settings)
        except (StoreNotReady, ArcadeError, OSError, ValueError) as exc:
            return broken([str(exc)], actions=["Run `cairnmind doctor` to inspect storage readiness"])

    try:
        from .brief import _scoped_records
        from .cli import READ_KINDS

        def read_all():
            selected, root = _resolve_project_and_root(store, project, worktree)
            pid = selected["id"]
            records = _scoped_records(store, pid, root)
            active_records = [r for r in records if r.get("status") != "superseded"]
            tasks = [r for r in records if r.get("kind") == "task"]
            active_plan = _active_plan(store, pid, root)
            git = _git_info(root)
            nested = nested_reserved(store, root) if git["available"] else ()

            drift_files, dirty_git_files, db_broken_refs, divergence = _check_file_drift(
                store, pid, root, git, active_records, active_plan, nested
            )
            stale_tasks, would_block_done, test_actions, warnings, check_errors = _check_test_freshness(
                store, selected, root, tasks, active_plan, git, dirty_git_files, nested
            )
            degraded_entities, degraded_details = _check_degraded(store, pid, active_records, active_plan)
            snap_broken_refs, lock_errors = _check_snapshot_and_locks(
                settings, root, active_plan, {t["id"]: t for t in tasks}, git
            )

            all_broken_refs = sorted(set(db_broken_refs + snap_broken_refs))
            all_integrity_errors = lock_errors + check_errors
            recommended = list(test_actions)
            if drift_files and not test_actions:
                proj_label = selected.get("name") or pid
                recommended.append(f"cairnmind index {shlex.quote(proj_label)}")
            if degraded_entities:
                proj_label = selected.get("name") or pid
                recommended.append(f"cairnmind memory orphans {shlex.quote(proj_label)}")
            if all_broken_refs or all_integrity_errors:
                recommended.append("cairnmind doctor")

            has_broken = bool(all_broken_refs or all_integrity_errors)
            has_drift = bool(
                drift_files
                or stale_tasks
                or degraded_entities
                or divergence is not None
                or would_block_done
            )
            status = "broken" if has_broken else ("drift" if has_drift else "healthy")

            report: dict[str, Any] = {
                "status": status,
                "latency_ms": 0,
                "backend": backend,
                "backend_mode": backend_mode,
                "project": {"id": pid, "name": selected.get("name")},
                "worktree": str(root),
                "git_available": git["available"],
                "branch_or_tree_diverged": divergence is not None,
                "drift_files": drift_files,
                "stale_test_tasks": stale_tasks,
                "degraded_entities": degraded_entities,
                "broken_refs": all_broken_refs,
                "warnings": warnings,
                "integrity_errors": all_integrity_errors,
                "would_block_done": would_block_done or has_broken,
                "recommended_actions": list(dict.fromkeys(recommended)),
            }
            if divergence is not None:
                report["divergence"] = divergence
            if degraded_details:
                report["degraded_details"] = degraded_details
            return pid, report

        if getattr(store, "read_only", False) and hasattr(store, "read_consistently"):
            result = store.read_consistently(read_all, READ_KINDS["pulse"])
        else:
            _, result = read_all()
        return finalize(result)
    except (StoreNotReady, ArcadeError, OSError, ValueError) as exc:
        return broken([str(exc)])
    finally:
        if owns_store and hasattr(store, "client") and hasattr(store.client, "close"):
            try:
                store.client.close()
            except Exception:  # noqa: BLE001
                pass


def exit_code_for_pulse(report: dict[str, Any], *, strict: bool = False) -> int:
    """Deterministic exit code: 0 healthy (or soft drift), 1 drift with --strict, 2 broken."""
    status = report.get("status")
    if status == "broken":
        return 2
    if status == "drift" and strict:
        return 1
    return 0


def _use_color(tty: bool | None = None) -> bool:
    is_tty = sys.stdout.isatty() if tty is None else tty
    if not is_tty:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return True


def render_pulse(report: dict[str, Any], *, color: bool | None = None) -> str:
    """Render concise, terminal-safe human output for `cairnmind pulse`."""
    use_color = _use_color() if color is None else color
    status = str(report.get("status", "broken")).upper()
    colors = {"HEALTHY": "\x1b[32m", "DRIFT": "\x1b[33m", "BROKEN": "\x1b[31m"}
    reset = "\x1b[0m"
    label = f"{colors.get(status, '')}PULSE: {status}{reset}" if use_color else f"PULSE: {status}"

    drift = report.get("drift_files") or []
    stale = report.get("stale_test_tasks") or []
    degraded = report.get("degraded_entities") or []
    broken_refs = report.get("broken_refs") or []
    errors = report.get("integrity_errors") or []
    latency = report.get("latency_ms", 0)

    summary_parts = [
        f"{latency} ms",
        f"{len(drift)} drift",
        f"{len(stale)} stale tests",
        f"{len(degraded)} degraded",
    ]
    if broken_refs:
        summary_parts.append(f"{len(broken_refs)} broken refs")
    lines = [f"{label} ({', '.join(summary_parts)})"]

    if report.get("branch_or_tree_diverged") and isinstance(report.get("divergence"), dict):
        div = report["divergence"]
        prev_h = safe_text(str(div.get("previous_head") or "unknown")[:12])
        curr_h = safe_text(str(div.get("current_head") or "unknown")[:12])
        prev_b = safe_text(str(div.get("previous_branch") or "detached"))
        curr_b = safe_text(str(div.get("current_branch") or "detached"))
        lines.append(f"  branch_or_tree_diverged: {prev_b}@{prev_h} -> {curr_b}@{curr_h}")
    if drift:
        shown = ", ".join(safe_text(path, limit=120) for path in drift[:10])
        more = f" (+{len(drift) - 10} more)" if len(drift) > 10 else ""
        lines.append(f"  drift files: {shown}{more}")
    if stale:
        lines.append(f"  stale test tasks: {', '.join(safe_text(t, limit=80) for t in stale[:10])}")
    if degraded:
        lines.append(f"  degraded entities: {', '.join(safe_text(e, limit=80) for e in degraded[:10])}")
    for ref in broken_refs[:10]:
        lines.append(f"  broken ref: {safe_text(ref, limit=160)}")
    for err in errors[:5]:
        lines.append(f"  integrity error: {safe_text(err, limit=240)}")
    if not report.get("git_available") and status != "BROKEN":
        lines.append("  note: git_available=false (using file mtime/size + SHA-256 fallback)")
    for warning in report.get("warnings") or []:
        lines.append(f"  warning: {safe_text(warning, limit=160)}")
    actions = report.get("recommended_actions") or []
    if actions:
        lines.append("  next:")
        for action in actions[:5]:
            lines.append(f"    {safe_text(action, limit=240)}")
    return "\n".join(lines)
