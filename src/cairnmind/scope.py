"""Scope drift: files changed outside what a task said it would touch. A warning, never a block.

A plan step may declare ``metadata.scope``, a list of path patterns in the form of an ignore
file (``src/auth/**``, ``docs/login.md``). When the task enters ``in_progress``, the state of
every file of the worktree is noted. ``scope_drift`` compares that note with the worktree as
it is now and names the files that changed and match no pattern. When the task is done, the
comparison is made once more and only its result is kept, so later work of other tasks is not
laid at its door. A task that is taken up again after it was done starts from a new note.

It looks at the tree and not at a conversation, so it works for every client. A file that
was already modified before the step started is not reported, because the comparison is with
the tree as it stood then and not with the last commit. Staging or committing a file changes
nothing about it here: only what is in the worktree counts. Files that Git ignores are not
seen, and neither are the files Cairnmind manages itself in a project.

The note is an observation of one machine: absolute paths and file digests of one worktree.
It is kept in the local data directory, outside the database, so it is in no portable
snapshot, no backup, no record and no hash that another machine or a session compares.

Reading is cheap on purpose, because it happens whenever a task is shown: a file whose size,
times, mode and inode are what the note holds is taken as unchanged without being read. A
file that was written within two seconds of being read is never trusted that way, as in Git.
What this cannot see is a change that leaves all of those untouched, such as a store through
a shared memory mapping. For a warning that is an accepted limit; a check run reads every file.
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
import threading
import time

import pathspec

MAX_REPORTED = 50
ACTIVE_STATES = ("in_progress", "needs_test", "needs_review", "blocked")
SILENT = ("in_scope", "not_started", "not_recorded")
_RACY_NS = 2_000_000_000
_WAIT_SECONDS = 5
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


def _roots(store, project_id: str, first: Path | None = None) -> list[Path]:
    """Every registered root of a project, the given one first."""
    rows = store.query("SELECT path FROM project_roots WHERE project_id=:project", {"project": project_id})
    found = [first] if first is not None else []
    for row in rows:
        path = Path(row["path"]).resolve()
        if path not in found:
            found.append(path)
    return found


def _entry(root: Path, name: str, known=None):
    """``[size, mtime_ns, mode, digest, ctime_ns, inode]`` of one file as it is in the worktree, or None."""
    from cairnmind.verification import _file_hash
    path = root / name
    try:
        status = path.lstat()
    except OSError:
        return None
    mode = stat.S_IMODE(status.st_mode)
    if known is not None and [known[0], known[1], known[2], known[4], known[5]] == [
            status.st_size, status.st_mtime_ns, mode, status.st_ctime_ns, status.st_ino]:
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
    return [status.st_size, status.st_mtime_ns, mode, digest, status.st_ctime_ns, status.st_ino]


def _settled(entry: list, now_ns: int) -> bool:
    """Whether a file was last written long enough before it was read that a later write must move its time."""
    return 0 <= entry[1] < now_ns - _RACY_NS


def path_states(store, project_id: str, worktree=None, *, known: dict | None = None) -> dict:
    """``{status, worktree, head, reasons, paths}`` for every tracked or unignored file. Read-only.

    ``known`` is the map of an earlier observation: a file whose size, times, mode and inode equal
    what it holds is not read again.
    """
    from cairnmind.memory_paths import is_reserved_memory
    from cairnmind.verification import _git, _root
    root = _root(store, project_id, worktree)
    result = {"status": "unknown", "worktree": str(root), "head": None, "reasons": [], "paths": {}}
    try:
        if Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel")).strip()).resolve() != root:
            raise ValueError("the registered root is not the root of a Git worktree")
        try:
            head = _git(root, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
        except ValueError:
            head = None  # no first commit yet: the files can still be compared
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


def _whole(entry) -> bool:
    return (isinstance(entry, list) and len(entry) == 6 and isinstance(entry[3], str)
            and all(type(entry[index]) is int for index in (0, 1, 2, 4, 5)))


def _read_note(path: Path, task_id: str, worktree: str) -> dict | None:
    """The note of a task and worktree when it is there and whole, else None."""
    try:
        note = json.loads(path.read_text(encoding="utf-8"))
        if (isinstance(note, dict) and note.get("task_id") == task_id and note.get("worktree") == worktree
                and isinstance(note.get("paths"), dict) and all(_whole(entry) for entry in note["paths"].values())):
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
    cutoff = time.time() - 86400
    for old in folder.glob(".note-*"):  # what an interrupted write left behind
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass


def record_baseline(store, task: dict, worktree=None) -> bool:
    """Note the worktree as a scoped task starts. Returns whether a note was written.

    A task that goes back to work keeps the note of its first start: taking a new one would
    forget what had already changed. Only after the task was finished does a start begin anew.
    """
    from cairnmind.store import _one
    from cairnmind.verification import _root
    if declared_scope(task) is None:
        return False
    root = _root(store, task["project_id"], worktree)
    path = _file(store, task["project_id"], str(root), task["id"])
    existing = _read_note(path, task["id"], str(root))
    if existing is not None and "final" not in existing:
        return False
    started = time.time_ns()
    state = path_states(store, task["project_id"], root)
    if state["status"] != "fresh":
        return False  # no note is better than one that cannot be compared with
    # A file written just before it was read may be written again without its time moving:
    # its time is not kept, so it is read again until it has settled.
    paths = {name: entry if _settled(entry, started) else [entry[0], -1, *entry[2:]]
             for name, entry in state["paths"].items()}
    _write(store, path, {"project_id": task["project_id"], "task_id": task["id"], "worktree": state["worktree"],
                         "head": state["head"], "captured_at": datetime.now(timezone.utc).isoformat(),
                         "captured_ns": started, "paths": paths})
    current = _one(store, "SELECT FROM records WHERE id=:id", {"id": task["id"]}, False)
    if current and current.get("metadata", {}).get("state") == "done":
        close_note(store, current, root)  # it was finished while the tree was being read
    return True


def close_note(store, task: dict, worktree=None) -> bool:
    """Compare once more as the task is done, in every worktree it was started in, and keep only the result."""
    from cairnmind.verification import _root
    scope = declared_scope(task)
    try:
        first = _root(store, task["project_id"], worktree)
    except ValueError:
        first = None
    closed = False
    for root in _roots(store, task["project_id"], first):
        path = _file(store, task["project_id"], str(root), task["id"])
        note = _read_note(path, task["id"], str(root))
        if note is None or "final" in note:
            continue
        if scope is None:
            path.unlink(missing_ok=True)  # nothing to compare with any more
            continue
        try:
            final = _compare(store, task, scope, root, note, refresh=False)
        except Exception as exc:  # noqa: BLE001 - one unreadable worktree must not keep the others open
            final = {**_blank(scope), "reasons": [_shown(f"the final comparison failed: {type(exc).__name__}", 300)]}
        # The file map has served its purpose: a finished note is a few hundred bytes.
        _write(store, path, {**note, "paths": {}, "final": {**final, "finished_at": datetime.now(timezone.utc).isoformat()}})
        closed = True
    return closed


def note_progress(store, task: dict, worktree, change: str) -> None:
    """Called after a committed write that started or finished a scoped task. Never raises.

    Reading a large worktree takes time and the write has already succeeded, so the caller waits
    only a few seconds: the note is finished behind it, and until then the answer is ``unknown``.
    """
    def work():
        try:
            (record_baseline if change == "start" else close_note)(store, task, worktree)
        except Exception:  # noqa: BLE001 - an advisory note must not turn a successful write into a failure
            pass
    try:
        thread = threading.Thread(target=work, name="cairnmind-scope-note")
        thread.start()
        thread.join(_WAIT_SECONDS)
    except Exception:  # noqa: BLE001
        pass


def _blank(scope) -> dict:
    return {"status": "unknown", "scope": scope, "since": None, "changed": 0, "out_of_scope": [],
            "out_of_scope_total": 0, "reasons": []}


def _compare(store, task: dict, scope: list[str], root: Path, note: dict, *, refresh: bool = True) -> dict:
    result = {**_blank(scope), "since": note.get("captured_at")}
    try:
        spec = pathspec.GitIgnoreSpec.from_lines(scope)
    except Exception as exc:  # noqa: BLE001 - a pattern the library cannot read is not a ValueError
        result["reasons"] = [_shown(f"a scope pattern cannot be read: {exc}", 300)]
        return result
    now_ns = time.time_ns()
    now = path_states(store, task["project_id"], root, known=note["paths"])
    if now["status"] != "fresh":
        result["reasons"] = now["reasons"] or ["the worktree could not be read"]
        return result
    before, after = note["paths"], dict(now["paths"])
    captured = note.get("captured_ns") if type(note.get("captured_ns")) is int else None
    for name in before.keys() - after.keys():
        # No longer listed. When it is still on disk, only Git's view of it changed (it became ignored).
        entry = _entry(root, name, before[name])
        if entry is not None:
            after[name] = entry
    changed = sorted(
        name for name in before.keys() | after.keys()
        if (before[name][2:4] != after[name][2:4] if name in before and name in after
            # Newly listed. A file that was here before the note was taken only stopped being ignored.
            else name in before or captured is None or after[name][4] >= captured))
    outside = [name for name in changed if not spec.match_file(name)]
    result.update(status="out_of_scope" if outside else "in_scope", changed=len(changed),
                  out_of_scope=[_shown(name) for name in outside[:MAX_REPORTED]], out_of_scope_total=len(outside))
    if refresh:
        # A file that was rewritten with the same content keeps being read until its new times are noted.
        moved = {name: after[name] for name in before.keys() & after.keys()
                 if before[name] != after[name] and before[name][2:4] == after[name][2:4] and _settled(after[name], now_ns)}
        if moved:
            try:
                path = _file(store, task["project_id"], str(root), task["id"])
                latest = _read_note(path, task["id"], str(root))
                if latest is not None and "final" not in latest and latest.get("captured_ns") == note.get("captured_ns"):
                    _write(store, path, {**latest, "paths": {**latest["paths"], **moved}})
            except Exception:  # noqa: BLE001 - a read never fails because a note could not be refreshed
                pass
    return result


def _kept(final) -> bool:
    return (isinstance(final, dict) and final.get("status") in ("in_scope", "out_of_scope", "unknown")
            and type(final.get("changed")) is int and type(final.get("out_of_scope_total")) is int
            and all(isinstance(final.get(key), list) and all(isinstance(item, str) for item in final[key])
                    for key in ("out_of_scope", "reasons")))


def scope_drift(store, task: dict, worktree=None) -> dict | None:
    """What changed outside a task's scope since it started, or None for a task that declares no scope.

    ``status`` is ``in_scope``, ``out_of_scope``, ``not_started``, ``not_recorded`` or ``unknown``.
    Unknown is said with its reasons and is never read as "nothing changed". For a task that is
    done, the answer is the one that was kept when it was finished; ``not_recorded`` is a finished
    task of which this machine holds no note.
    """
    from cairnmind.verification import _root
    scope = declared_scope(task)
    if scope is None:
        return None
    state = task.get("metadata", {}).get("state", "todo")
    result = _blank(scope)
    try:
        root = _root(store, task["project_id"], worktree)
    except ValueError as exc:
        result["reasons"] = [_shown(exc, 300)]
        return result
    note = _read_note(_file(store, task["project_id"], str(root), task["id"]), task["id"], str(root))
    final = note.get("final") if note else None
    if note is not None:
        result["since"] = note.get("captured_at")
    if state == "todo" and (note is None or final is not None):
        result.update(status="not_started", reasons=["the task has not been started"])
    elif state == "done" and note is None:
        result.update(status="not_recorded", reasons=["the task was finished and this machine holds no note of it"])
    elif state == "done":
        if _kept(final):
            return {**result, **{key: final[key] for key in ("status", "changed", "out_of_scope", "out_of_scope_total", "reasons")},
                    "finished_at": final.get("finished_at")}
        result["reasons"] = ["the task was finished without a final comparison"]
    elif note is None:
        result["reasons"] = ["the state of this worktree was not noted when the task started here"]
    elif final is not None:
        result["reasons"] = ["the task was taken up again and the state of this worktree was not noted again"]
    else:
        return _compare(store, task, scope, root, note)
    return result


def drift_warning(drift: dict | None) -> str | None:
    """One line for a person or a model, or None when there is nothing to say."""
    if not drift or drift["status"] in SILENT:
        return None
    if drift["status"] == "unknown":
        return _shown("Scope drift is unknown: " + "; ".join(str(reason) for reason in drift["reasons"]), 400)
    listed = ", ".join(drift["out_of_scope"][:5])
    more = drift["out_of_scope_total"] - min(5, len(drift["out_of_scope"]))
    return _shown(f"{drift['out_of_scope_total']} changed file{'' if drift['out_of_scope_total'] == 1 else 's'} outside the "
                  f"task's scope: {listed}" + (f" and {more} more" if more > 0 else "") + ". This is a warning, not a block.", 1400)
