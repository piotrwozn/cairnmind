"""The notices a workspace panel showed, kept in memory so a replaced one can be read again (#130).

Nothing here touches the disk, the workspace state, the journal or a log: the history lives as long as the
panel does. Every text is stored the way it was shown (already terminal-safe) and never evaluated.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime

from .terminal_safe import safe_text

MAX_ENTRIES = 200
MAX_CHARS = 60_000          # total size of the stored texts
MAX_ENTRY_CHARS = 4_000     # one notice is cut here, so a single huge one cannot push out the rest
KINDS = ("info", "warn", "error")


def _now() -> datetime:
    return datetime.now()


@dataclass
class Notice:
    text: str
    when: datetime
    project_id: str | None
    project_name: str | None
    worktree: str | None
    count: int = 1
    seq: int = 0
    kind: str = "info"  # "info", "warn" or "error": the style the line had, shown as a word elsewhere


class NoticeHistory:
    def __init__(self, max_entries: int = MAX_ENTRIES, max_chars: int = MAX_CHARS) -> None:
        self.max_entries, self.max_chars = max_entries, max_chars
        self.entries: deque[Notice] = deque()
        self._chars = 0
        self._seq = 0
        self._seen = 0        # highest seq the person has opened the history at
        self._showing = False  # the line currently shows the newest entry

    def add(self, text: object, scope: dict | None, kind: str = "info") -> None:
        kind = kind if kind in KINDS else "info"
        shown = safe_text(text, limit=MAX_ENTRY_CHARS)
        if not shown.strip():
            self._showing = False  # the line was cleared: whatever it showed is now replaced
            return
        scope = scope if isinstance(scope, dict) else {}
        project = (scope.get("project_id"), scope.get("worktree"))
        last = self.entries[-1] if self.entries else None
        if (last is not None and last.text == shown and (last.project_id, last.worktree) == project
                and last.kind == kind):
            last.when, last.count = _now(), last.count + 1
            self._showing = True
            return
        self._seq += 1
        name = scope.get("project_name")
        self.entries.append(Notice(shown, _now(), project[0], safe_text(name, limit=200) if name else None,
                                   project[1], 1, self._seq, kind))
        self._chars += len(shown)
        self._showing = True
        while len(self.entries) > 1 and (len(self.entries) > self.max_entries or self._chars > self.max_chars):
            self._chars -= len(self.entries.popleft().text)

    def replaced_unseen(self) -> int:
        """Notices added since the history was last opened that are no longer on the line."""
        fresh = sum(1 for entry in self.entries if entry.seq > self._seen)
        return max(0, fresh - 1) if self._showing else fresh

    def mark_seen(self) -> None:
        self._seen = self._seq

    def mark(self, translate) -> str:
        count = self.replaced_unseen()
        return f"[{translate('Notices')}: {count}]" if count else ""

    def lines(self, scope: dict | None, translate) -> str:
        """Newest first: time, the project when it is not the one shown now, the text.

        The head of an entry (time, project, folder, count) is one line; a stored text can never start a
        line of its own, because every continuation line of a notice is indented.
        """
        from .brief import _line  # the one-line helper of the other panels; imported when the list is opened
        scope = scope if isinstance(scope, dict) else {}
        now = _now()
        out = []
        for entry in reversed(self.entries):
            stamp = entry.when.strftime("%H:%M:%S" if entry.when.date() == now.date() else "%Y-%m-%d %H:%M:%S")
            label = ""
            shown_folder = scope.get("worktree")
            differs = (entry.project_id != scope.get("project_id")
                       or bool(entry.worktree and shown_folder and entry.worktree != shown_folder))
            if differs:
                if entry.project_id is None:
                    label = translate("No project")
                else:
                    label = _line(entry.project_name or entry.project_id, 120)
                    if entry.worktree:
                        label += " · " + _line(entry.worktree, 200)
                label = "[" + label + "] "
            repeat = f" (×{entry.count})" if entry.count > 1 else ""
            body = entry.text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029").replace("\n", "\n    ")
            out.append(f"{stamp}  {label}{body}{repeat}")
        return "\n".join(out)


def about(project_id, name=None, worktree=None) -> dict:
    """What a notice is about, when that is not the project the panel shows at the moment of the call."""
    return {"project_id": project_id, "project_name": name, "worktree": worktree}
