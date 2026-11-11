"""Scope drift: files changed outside what a task said it would touch. A warning, never a block.

A plan step may declare ``metadata.scope``, a list of path patterns in the form of an ignore
file (``src/auth/**``, ``docs/login.md``). When the task enters ``in_progress``, the state of
every file of the worktree is noted. ``scope_drift`` compares that note with the worktree as
it is now and names the files that changed and match no pattern. When the task is done, the
comparison is made once more and kept, so later work of other tasks is not laid at its door.

It looks at the tree and not at a conversation, so it works for every client. A file that
was already modified before the step started is not reported, because the comparison is with
the tree as it stood then and not with the last commit. Staging or committing a file changes
nothing about it here: only what is in the worktree counts. Files that Git ignores are not
seen, and neither are the files Cairnmind manages itself in a project.

The note is an observation of one machine: absolute paths and file digests of one worktree.
It is kept in the local data directory, outside the database, so it is in no portable
snapshot, no backup, no record and no hash that another machine or a session compares.

Reading is cheap on purpose, because it happens whenever a task is shown: a file whose size,
modification time and mode are what the note holds is taken as unchanged without being read.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time

import pathspec

MAX_REPORTED = 50
ACTIVE_STATES = ("in_progress", "needs_test", "needs_review", "blocked")
_KEEP_FINISHED_SECONDS = 30 * 86400
# Written by Cairnmind itself, some of them on every launch: never somebody's drift.
_MANAGED = pathspec.GitIgnoreSpec.from_lines([
    "/.cairnmind/", "/.devbrain/", "/.mcp.json", "/.codex/config.toml", "/.codex/hooks.json",
    "/.claude/settings.local.json", "/AGENTS.md", "/CLAUDE.md", "*.cairnmind-backup-*", "*.devbrain-backup-*"])


def _shown(value, limit: int = 200) -> str:
    """A file name or a reason as it may be printed or given to a model: one line, bounded, no control text."""
    from cairnmind.brief import _line
    return _line(str(value).encode("utf-8", "backslashreplace").decode("utf-8"), limit)


def declared_scope(task: dict) -> list[str] | None:
    """The scope of a task when it is a usable list of patterns, else None."""
    from cairnmind.store import validate_scope
    scope = task.get("metadata", {}).get("scope") if isinstance(task.get("metadata"), dict) else None
    try:
        validate_scope(scope)
    except ValueError:
        return None
    return scope or None


def _folder(store) -> Path:
    return Path(store.data_dir) / "scope-baselines"


def _file(store, project_id: str, worktree: str, task_id: str) -> Path:
    name = hashlib.sha256(json.dumps([project_id, worktree, task_id]).encode()).hexdigest()
    return _folder(store) / f"{name}.json"


def _entry(root: Path, name: str, known=None):
    """``[size, mtime_ns, mode, digest]`` of one file as it is in the worktree, or None when it is not there."""
    from cairnmind.verification import _file_hash
    path = root / name
    try:
        status = path.lstat()
    except OSError:
        return None
    identity = [status.st_size, status.st_mtime_ns, stat.S_IMODE(status.st_mode)]
    if known is not None and list(known[:3]) == identity:
        return list(known)  # what the note holds: taken as unchanged without reading the file
    try:
        if stat.S_ISLNK(status.st_mode):
            digest = "link:" + hashlib.sha256(os.readlink(path).encode("utf-8", "surrogateescape")).hexdigest()[:24]
        elif stat.S_ISREG(status.st_mode):
            digest = _file_hash(path)[:24]
        else:
            digest = "other"
    except (OSError, ValueError, RuntimeError):
        digest = "unreadable"
    return [*identity, digest]


def path_states(store, project_id: str, worktree=None, *, known: dict | None = None) -> dict:
    """``{status, worktree, head, reasons, paths}`` for every tracked or unignored file. Read-only.

    ``known`` is the map of an earlier observation: a file whose size, time and mode equal what
    it holds is not read again.
    """
    from cairnmind.memory_paths import is_reserved_memory
    from cairnmind.verification import _git, _root
    root = _root(store, project_id, worktree)
    result = {"status": "unknown", "worktree": str(root), "head": None, "reasons": [], "paths": {}}
    try:
        if Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel")).strip()).resolve() != root:
            raise ValueError("the registered root is not the root of a Git worktree")
        head = _git(root, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
        names = set()
        for arguments in (("ls-files", "-z"), ("ls-files", "--others", "--exclude-standard", "-z")):
            names.update(os.fsdecode(item) for item in _git(root, *arguments).split(b"\0") if item)
        paths = {}
        for name in sorted(names):
            if is_reserved_memory(name) or _MANAGED.match_file(name):
                continue
            entry = _entry(root, name, (known or {}).get(name))
            if entry is not None:
                paths[name] = entry
        result.update(status="fresh", head=head, paths=paths)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        result["reasons"] = [_shown(f"the worktree cannot be read: {type(exc).__name__}: {exc}", 300)]
    return result


def _read_note(path: Path, task_id: str, worktree: str) -> dict | None:
    """The note of a task and worktree when it is there and whole, else None."""
    try:
        note = json.loads(path.read_text(encoding="utf-8"))
        if (isinstance(note, dict) and note.get("task_id") == task_id and note.get("worktree") == worktree
                and isinstance(note.get("paths"), dict)
                and all(isinstance(entry, list) and len(entry) == 4 for entry in note["paths"].values())):
            return note
    except (OSError, ValueError):
        pass
    return None


def _write(store, path: Path, note: dict) -> None:
    folder = _folder(store)
    folder.mkdir(mode=0o700, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".note-", dir=folder)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(note, handle)
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
    cutoff = time.time() - _KEEP_FINISHED_SECONDS
    for old in folder.iterdir():
        try:
            # Only what is over: the note of a task that is still open is kept however old it is.
            stale = old.stat().st_mtime < cutoff
            if stale and (old.name.startswith(".note-") or (old.suffix == ".json" and '"final"' in old.read_text(encoding="utf-8")[-4000:])):
                old.unlink()
        except OSError:
            pass


def record_baseline(store, task: dict, worktree=None) -> bool:
    """Note the worktree as a scoped task starts. Returns whether a note was written.

    A task that goes back to work keeps the note of its first start: taking a new one would
    forget what had already changed. Only after the task was finished does a start begin anew.
    """
    from cairnmind.verification import _root
    if declared_scope(task) is None:
        return False
    root = _root(store, task["project_id"], worktree)
    path = _file(store, task["project_id"], str(root), task["id"])
    existing = _read_note(path, task["id"], str(root))
    if existing is not None and "final" not in existing:
        return False
    state = path_states(store, task["project_id"], root)
    if state["status"] != "fresh":
        return False  # no note is better than one that cannot be compared with
    _write(store, path, {"project_id": task["project_id"], "task_id": task["id"], "worktree": state["worktree"],
                         "head": state["head"], "captured_at": datetime.now(timezone.utc).isoformat(),
                         "paths": state["paths"]})
    return True


def close_note(store, task: dict, worktree=None) -> bool:
    """Compare once more as the task is done, and keep the result with the note."""
    from cairnmind.verification import _root
    if declared_scope(task) is None:
        return False
    root = _root(store, task["project_id"], worktree)
    path = _file(store, task["project_id"], str(root), task["id"])
    note = _read_note(path, task["id"], str(root))
    if note is None or "final" in note:
        return False
    final = scope_drift(store, {**task, "metadata": {**task["metadata"], "state": "in_progress"}}, root)
    _write(store, path, {**note, "final": {**final, "finished_at": datetime.now(timezone.utc).isoformat()}})
    return True


def note_progress(store, task: dict, worktree, change: str) -> None:
    """Called after a committed write that started or finished a scoped task. Never raises."""
    try:
        (record_baseline if change == "start" else close_note)(store, task, worktree)
    except Exception:  # noqa: BLE001 - an advisory note must not turn a successful write into a failure
        pass


def scope_drift(store, task: dict, worktree=None) -> dict | None:
    """What changed outside a task's scope since it started, or None for a task that declares no scope.

    ``status`` is ``in_scope``, ``out_of_scope``, ``not_started`` or ``unknown``. Unknown is said
    with its reasons and is never read as "nothing changed". For a task that is done, the answer
    is the one that was kept when it was finished.
    """
    from cairnmind.verification import _root
    scope = declared_scope(task)
    if scope is None:
        return None
    state = task.get("metadata", {}).get("state", "todo")
    result = {"status": "unknown", "scope": scope, "since": None, "changed": 0, "out_of_scope": [],
              "out_of_scope_total": 0, "reasons": []}
    try:
        root = _root(store, task["project_id"], worktree)
    except ValueError as exc:
        result["reasons"] = [_shown(exc, 300)]
        return result
    note = _read_note(_file(store, task["project_id"], str(root), task["id"]), task["id"], str(root))
    if note is None:
        if state == "todo":
            result.update(status="not_started", reasons=["the task has not been started"])
        else:
            result["reasons"] = ["the state of this worktree was not noted when the task started here"]
        return result
    result["since"] = note.get("captured_at")
    final = note.get("final")
    if state == "done" or isinstance(final, dict):
        if (isinstance(final, dict) and final.get("status") in ("in_scope", "out_of_scope", "unknown")
                and type(final.get("changed")) is int and type(final.get("out_of_scope_total")) is int
                and all(isinstance(final.get(key), list) and all(isinstance(item, str) for item in final[key])
                        for key in ("out_of_scope", "reasons"))):
            return {**result, **{key: final[key] for key in result if key in final}, "finished_at": final.get("finished_at")}
        result["reasons"] = ["the task was finished without a final comparison"]
        return result
    now = path_states(store, task["project_id"], root, known=note["paths"])
    if now["status"] != "fresh":
        result["reasons"] = now["reasons"] or ["the worktree could not be read"]
        return result
    before, after = note["paths"], now["paths"]
    changed = sorted(name for name in before.keys() | after.keys()
                     if (before.get(name) or [None] * 4)[2:] != (after.get(name) or [None] * 4)[2:])
    spec = pathspec.GitIgnoreSpec.from_lines(scope)
    outside = [name for name in changed if not spec.match_file(name)]
    result.update(status="out_of_scope" if outside else "in_scope", changed=len(changed),
                  out_of_scope=[_shown(name) for name in outside[:MAX_REPORTED]], out_of_scope_total=len(outside))
    return result


def drift_warning(drift: dict | None) -> str | None:
    """One line for a person or a model, or None when there is nothing to say."""
    if not drift or drift["status"] in ("in_scope", "not_started"):
        return None
    if drift["status"] == "unknown":
        return _shown("Scope drift is unknown: " + "; ".join(str(reason) for reason in drift["reasons"]), 400)
    listed = ", ".join(drift["out_of_scope"][:5])
    more = drift["out_of_scope_total"] - min(5, len(drift["out_of_scope"]))
    return _shown(f"{drift['out_of_scope_total']} changed file{'' if drift['out_of_scope_total'] == 1 else 's'} outside the "
                  f"task's scope: {listed}" + (f" and {more} more" if more > 0 else "") + ". This is a warning, not a block.", 1400)
