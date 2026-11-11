"""Scope drift: files changed outside what a task said it would touch. A warning, never a block.

A plan step may declare ``metadata.scope``, a list of path patterns in the form of an ignore
file (``src/auth/**``, ``docs/login.md``). When the task enters ``in_progress``, the state of
every file of the worktree is noted. ``scope_drift`` compares that note with the worktree as
it is now and names the files that changed and match no pattern.

It looks at the tree and not at a conversation, so it works for every client. A file that
was already modified before the step started is not reported, because the comparison is with
the tree as it stood then and not with the last commit. Files that Git ignores are not seen.

The note is an observation of one machine: absolute paths and file digests of one worktree.
It is kept in the local data directory, outside the database, so it is in no portable
snapshot, no backup, no record and no hash that another machine or a session compares.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

import pathspec

MAX_REPORTED = 50
_KEEP_SECONDS = 90 * 86400


def _folder(store) -> Path:
    return Path(store.data_dir) / "scope-baselines"


def _file(store, project_id: str, worktree: str, task_id: str) -> Path:
    name = hashlib.sha256(json.dumps([project_id, worktree, task_id]).encode()).hexdigest()
    return _folder(store) / f"{name}.json"


def path_states(store, project_id: str, worktree=None) -> dict:
    """``{status, worktree, head, unknowns, paths}`` with one digest per tracked or unignored file. Read-only."""
    from cairnmind.verification import _git, _hash, _manifest, _root
    root = _root(store, project_id, worktree)
    result = {"status": "unknown", "worktree": str(root), "head": None, "unknowns": [], "paths": {}}
    try:
        if Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel")).strip()).resolve() != root:
            raise ValueError("Registered root is not the Git worktree root")
        head = _git(root, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
        entries, unknowns = _manifest(root, [])
        # Shortened: the digest tells one state of a file from another, it proves nothing by itself.
        result.update(head=head, unknowns=unknowns, status="fresh" if not unknowns else "unknown",
                      paths={entry["path"]: _hash({key: value for key, value in entry.items() if key != "path"})[:24]
                             for entry in entries})
    except (OSError, ValueError, RuntimeError) as exc:
        result["unknowns"] = [f"cannot read the worktree: {type(exc).__name__}"]
    return result


def record_baseline(store, task: dict, worktree=None) -> bool:
    """Note the state of the worktree as a scoped task starts. Returns whether a note was written."""
    scope = task.get("metadata", {}).get("scope")
    if not isinstance(scope, list) or not scope:
        return False
    state = path_states(store, task["project_id"], worktree)
    folder = _folder(store)
    folder.mkdir(mode=0o700, exist_ok=True)
    note = {"project_id": task["project_id"], "task_id": task["id"], "captured_at": datetime.now(timezone.utc).isoformat(), **state}
    descriptor, name = tempfile.mkstemp(prefix=".baseline-", dir=folder)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(note, handle)
        os.chmod(name, 0o600)
        os.replace(name, _file(store, task["project_id"], state["worktree"], task["id"]))
    finally:
        Path(name).unlink(missing_ok=True)
    cutoff = time.time() - _KEEP_SECONDS
    for old in folder.glob("*.json"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass
    return True


def scope_drift(store, task: dict, worktree=None, *, current: dict | None = None) -> dict | None:
    """What changed outside a task's scope since it started, or None for a task that declares no scope.

    ``status`` is ``in_scope``, ``out_of_scope`` or ``unknown``. Unknown is said with its reasons
    and is never read as "nothing changed".
    """
    from cairnmind.verification import _root
    scope = task.get("metadata", {}).get("scope")
    if not isinstance(scope, list) or not scope:
        return None
    result = {"status": "unknown", "scope": scope, "since": None, "changed": 0, "out_of_scope": [],
              "out_of_scope_total": 0, "reasons": []}
    try:
        root = _root(store, task["project_id"], worktree)
        path = _file(store, task["project_id"], str(root), task["id"])
        if not path.is_file():
            result["reasons"] = ["the state of this worktree was not noted when the task started here"]
            return result
        note = json.loads(path.read_text(encoding="utf-8"))
        if note.get("task_id") != task["id"] or note.get("worktree") != str(root) or not isinstance(note.get("paths"), dict):
            raise ValueError("the note does not belong to this task and worktree")
    except (OSError, ValueError) as exc:
        result["reasons"] = [f"the note taken when the task started cannot be used: {exc}"[:300]]
        return result
    result["since"] = note.get("captured_at")
    now = current or path_states(store, task["project_id"], worktree)
    reasons = [*(note.get("unknowns") or []), *now["unknowns"]]
    if note.get("status") != "fresh" or now["status"] != "fresh":
        result["reasons"] = sorted(set(reasons))[:10] or ["the worktree could not be read completely"]
        return result
    before, after = note["paths"], now["paths"]
    changed = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
    spec = pathspec.GitIgnoreSpec.from_lines(scope)
    outside = [name for name in changed if not spec.match_file(name)]
    result.update(status="out_of_scope" if outside else "in_scope", changed=len(changed),
                  out_of_scope=outside[:MAX_REPORTED], out_of_scope_total=len(outside))
    return result


def drift_warning(drift: dict | None) -> str | None:
    """One line for a person or a model, or None when there is nothing to say."""
    if not drift or drift["status"] == "in_scope":
        return None
    if drift["status"] == "unknown":
        return "Scope drift is unknown: " + "; ".join(drift["reasons"])[:300]
    listed = ", ".join(drift["out_of_scope"][:5])
    more = drift["out_of_scope_total"] - min(5, len(drift["out_of_scope"]))
    return (f"{drift['out_of_scope_total']} changed file{'' if drift['out_of_scope_total'] == 1 else 's'} outside the "
            f"task's scope: {listed}" + (f" and {more} more" if more > 0 else "") + ". This is a warning, not a block.")
