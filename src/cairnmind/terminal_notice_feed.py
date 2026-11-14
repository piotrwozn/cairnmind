"""The newest notices of each panel, for the activity panel to show (#144). Never on disk.

The notice history stays in each panel's memory (terminal_notices, #130). Each UI panel (sidebar,
inspector, graph) also puts a bounded copy of its newest notices in an option of its own tmux pane
(`@cairnmind_notices`) on the workspace's private tmux server. It lives in that server's memory and
goes with the pane; a panel that starts again replaces it. The activity panel reads the options of
the workspace's panes. Nothing else reads them: they never reach the store, a record, a review or a
model. The reader trusts nothing in them: every field is checked, an entry dated in the future is
dropped, and every string is made terminal-safe again.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
from datetime import datetime, timedelta

from .terminal_notices import KINDS
from .terminal_safe import safe_text

ROLES = ("sidebar", "inspector", "graph")
OPTION = "@cairnmind_notices"
KEEP = 30               # newest entries kept per panel
TEXT_CHARS = 200        # one notice text is cut here: the panel shows one row of it
MAX_BYTES = 8000        # the encoded copy, well inside one tmux command
AHEAD = timedelta(minutes=2)
_PANE = re.compile(r"^%[0-9]{1,9}$")


def snapshot(history) -> list:
    """The rows to publish, taken where the history lives (the panel's event loop)."""
    return [{"seq": entry.seq, "when": entry.when.isoformat(timespec="seconds"), "kind": entry.kind,
             "text": safe_text(entry.text, limit=TEXT_CHARS), "project_id": entry.project_id,
             "project_name": entry.project_name, "worktree": entry.worktree, "count": entry.count}
            for entry in list(getattr(history, "entries", []))[-KEEP:]]


def encode(rows) -> str:
    """JSON in URL-safe base64, so no character of a notice means anything to tmux; oldest dropped to fit."""
    rows = list(rows)[-KEEP:]
    while True:
        value = base64.urlsafe_b64encode(json.dumps({"version": 1, "entries": rows}).encode()).decode()
        if len(value) <= MAX_BYTES or not rows:
            return value
        rows = rows[1:]


def _text(value, limit):
    return safe_text(value, limit=limit) if isinstance(value, str) and value else None


def _entry(role, row, latest):
    if not isinstance(row, dict):
        return None
    text, when = _text(row.get("text"), TEXT_CHARS), row.get("when")
    try:
        when = datetime.fromisoformat(when).replace(tzinfo=None) if isinstance(when, str) and len(when) <= 40 else None
    except ValueError:
        when = None
    if not text or not text.strip() or when is None or when > latest:
        return None
    count, seq = row.get("count"), row.get("seq")
    return {"role": role, "when": when, "text": text,
            "kind": row.get("kind") if row.get("kind") in KINDS else "info",
            "project_id": _text(row.get("project_id"), 200), "project_name": _text(row.get("project_name"), 200),
            "worktree": _text(row.get("worktree"), 1000),
            "count": count if type(count) is int and 0 < count < 10**6 else 1,
            "seq": seq if type(seq) is int and 0 <= seq < 10**12 else 0}


def decode(role, value, now=None) -> list:
    """The entries of one copy; a bad copy or entry is skipped, nothing raises."""
    if not isinstance(value, str) or not value or len(value) > MAX_BYTES:
        return []
    try:
        data = json.loads(base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8"))
    except (UnicodeError, ValueError, binascii.Error, RecursionError):
        return []
    rows = data.get("entries") if isinstance(data, dict) and data.get("version") == 1 else None
    latest = (now or datetime.now()) + AHEAD
    return [entry for row in rows[-KEEP:] if (entry := _entry(role, row, latest))] if isinstance(rows, list) else []


class TmuxChannel:
    """The pane options of the workspace's private tmux server."""

    def __init__(self, settings, workspace_id):
        from .workspace import WorkspaceManager
        self.manager = WorkspaceManager(settings, workspace_id)

    def publish(self, role, value) -> None:
        # Only from inside the workspace's own server, and only into the pane tagged with this role.
        pane, server = os.environ.get("TMUX_PANE", ""), os.environ.get("TMUX", "").split(",")[0]
        if not _PANE.fullmatch(pane) or server != str(self.manager.socket):
            return
        tags = self.manager.tmux("display-message", "-p", "-t", pane, "#{@cairnmind_workspace}\t#{@cairnmind_role}",
                                 check=False).stdout.strip()
        if tags == f"{self.manager.workspace_id}\t{role}":
            self.manager.tmux("set-option", "-p", "-t", pane, OPTION, value, check=False)

    def read(self) -> dict:
        if not self.manager.binary or not self.manager.socket.exists():
            return {}
        result = self.manager.tmux("list-panes", "-a", "-F",
                                   "#{@cairnmind_workspace}\t#{@cairnmind_role}\t#{" + OPTION + "}", check=False)
        found = {}
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[0] == self.manager.workspace_id and parts[1] in ROLES and parts[2]:
                found[parts[1]] = parts[2]
        return found


_channels = {}


def channel(settings, workspace_id):
    key = (str(settings.data_dir), workspace_id)
    if key not in _channels:
        _channels[key] = TmuxChannel(settings, workspace_id)
    return _channels[key]


def publish(settings, workspace_id, role, rows) -> None:
    """Replace this panel's copy (it blocks on tmux: call it off the event loop)."""
    if role not in ROLES:
        raise ValueError("Only a UI panel keeps a notice copy")
    channel(settings, workspace_id).publish(role, encode(rows))


def read_all(settings, workspace_id, limit=KEEP) -> list:
    """The entries of every panel, newest first. Nothing raises."""
    try:
        values = channel(settings, workspace_id).read()
    except Exception:
        return []
    found = [entry for role in ROLES for entry in decode(role, values.get(role))]
    found.sort(key=lambda entry: (entry["when"], entry["seq"]), reverse=True)
    return found[:limit]
