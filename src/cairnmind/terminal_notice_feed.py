"""A private, bounded copy of a panel's notices, for the activity panel to show (#144).

The notice history itself stays in each panel's memory (terminal_notices, #130). Each UI panel
(sidebar, inspector, graph) also keeps its newest notices in its own file under
`workspaces/<id>/notices/<role>.json`, replaced when the panel starts, so another process can
show them. One process writes each file. Nothing else reads these files: they never reach the
store, a record, a review or a model. The reader trusts nothing in them: every field is checked
and every string is made terminal-safe again.
"""
from __future__ import annotations

import json
import os
import re
import stat
from datetime import datetime

from .cockpit_state import _atomic
from .terminal_notices import KINDS
from .terminal_safe import safe_text

ROLES = ("sidebar", "inspector", "graph")
KEEP = 30               # newest entries kept per panel
TEXT_CHARS = 500        # one notice text is cut here
MAX_BYTES = 64 * 1024   # a larger file is not read
_WORKSPACE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


def directory(settings, workspace_id):
    if not isinstance(workspace_id, str) or not _WORKSPACE.fullmatch(workspace_id):
        raise ValueError("Invalid workspace id")
    return settings.data_dir / "workspaces" / workspace_id / "notices"


class NoticeFeed:
    def __init__(self, settings, workspace_id, role):
        if role not in ROLES:
            raise ValueError("Only a UI panel keeps a notice copy")
        self.role = role
        self.path = directory(settings, workspace_id) / (role + ".json")

    def publish(self, history) -> None:
        """Replace this panel's copy. Without a workspace folder there is no activity panel to read it."""
        workspace = self.path.parent.parent
        if workspace.is_symlink() or not workspace.is_dir():
            return  # never creates the workspace folders: their owner gives them their private mode
        entries = list(getattr(history, "entries", []))[-KEEP:]
        rows = [{"seq": entry.seq, "when": entry.when.isoformat(timespec="seconds"), "kind": entry.kind,
                 "text": safe_text(entry.text, limit=TEXT_CHARS), "project_id": entry.project_id,
                 "project_name": entry.project_name, "worktree": entry.worktree, "count": entry.count}
                for entry in entries]
        if self.path.parent.is_symlink():
            raise ValueError("The notice folder cannot be a link")
        # Private folder (0700) and file (0600), a temporary file renamed over the old one.
        _atomic(self.path, {"version": 1, "role": self.role, "entries": rows})


def _text(value, limit):
    return safe_text(value, limit=limit) if isinstance(value, str) and value else None


def _entry(role, row):
    if not isinstance(row, dict):
        return None
    text, when = _text(row.get("text"), TEXT_CHARS), row.get("when")
    try:
        when = datetime.fromisoformat(when) if isinstance(when, str) and len(when) <= 40 else None
    except ValueError:
        when = None
    if not text or not text.strip() or when is None:
        return None
    count, seq = row.get("count"), row.get("seq")
    return {"role": role, "when": when.replace(tzinfo=None), "text": text,
            "kind": row.get("kind") if row.get("kind") in KINDS else "info",
            "project_id": _text(row.get("project_id"), 200), "project_name": _text(row.get("project_name"), 200),
            "worktree": _text(row.get("worktree"), 1000),
            "count": count if type(count) is int and 0 < count < 10**6 else 1,
            "seq": seq if type(seq) is int and 0 <= seq < 10**12 else 0}


def _read(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None  # missing, or a link
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            return None
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None


def signature(settings, workspace_id):
    """What changed since the last read, from one stat per file; nothing is opened."""
    folder = directory(settings, workspace_id)
    result = []
    for role in ROLES:
        try:
            info = os.lstat(folder / (role + ".json"))
            result.append((role, info.st_mtime_ns, info.st_size))
        except OSError:
            result.append((role, None, None))
    return tuple(result)


def read_all(settings, workspace_id, limit=KEEP):
    """The entries of every panel, newest first. A bad file or entry is skipped; nothing raises."""
    folder = directory(settings, workspace_id)
    if folder.is_symlink():
        return []
    found = []
    for role in ROLES:
        value = _read(folder / (role + ".json"))
        rows = value.get("entries") if isinstance(value, dict) and value.get("version") == 1 else None
        if isinstance(rows, list):
            found.extend(entry for row in rows[-KEEP:] if (entry := _entry(role, row)) is not None)
    found.sort(key=lambda entry: (entry["when"], entry["seq"]), reverse=True)
    return found[:limit]
