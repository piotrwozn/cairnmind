"""What the person sees about the built-in agent's own file writes before they approve anything (#176).

Two protections, both on the host side of the tool executor:

* ``ChangeLedger`` keeps the files the agent created, changed or deleted since the person's last approval
  of a command or a third-party tool call. Every such approval request carries the list, so a write that
  a later command would run (a ``conftest.py``, a ``Makefile``) is in front of the person when they decide.
  This is the general protection: it covers every path.
* ``configures_execution`` names the files that make another program run something without any later
  prompt: coding clients' configuration and hooks, MCP configuration, repository instruction files, CI
  workflows, editor tasks, dev containers, shell environment files and Git hook managers. A write, patch
  or delete of such a file needs the same one-use approval as a command. No such list can be complete.
"""
from __future__ import annotations

import difflib
import unicodedata

MAX_LISTED = 20            # files named in one approval request; the rest are counted
MAX_PATH_CHARS = 240       # a longer path is shown with its middle cut
MAX_DIFF_LINES = 60        # lines of a gated change shown in its approval request
MAX_DIFF_CHARS = 18_000    # characters of those lines; a line is shown whole or not at all, never cut
CHANGES = ("created", "changed", "deleted")
WRITE_TOOLS = ("write_file", "apply_patch", "delete_file")

# Compared case-insensitively: a worktree on a case-insensitive file system reads `.Claude` as `.claude`.
# A directory name anywhere in the path: its files configure a client, an editor or a container.
EXECUTION_DIRECTORIES = frozenset({
    ".claude", ".codex", ".cursor", ".gemini", ".windsurf", ".clinerules", ".continue", ".amazonq",
    ".devcontainer", ".husky", ".circleci"})
# A file name anywhere in the path: instruction files coding clients read in every directory, MCP and
# environment files that tools load on their own.
EXECUTION_FILES = frozenset({
    "agents.md", "agents.override.md", "claude.md", "claude.local.md", "gemini.md", ".mcp.json", ".cursorrules", ".windsurfrules",
    ".clinerules", ".devcontainer.json", ".envrc", ".pre-commit-config.yaml", "lefthook.yml", ".lefthook.yml",
    ".gitlab-ci.yml"})
# A directory and what below it: only these parts of `.github` and `.vscode` run or steer anything.
EXECUTION_NESTED = {
    ".github": (("workflows",), ("actions",), ("instructions",), ("prompts",), ("copilot-instructions.md",)),
    ".vscode": (("tasks.json",), ("settings.json",), ("launch.json",), ("mcp.json",)),
}


def configures_execution(parts) -> bool:
    """True for a project-relative path (its parts) on the list above."""
    folded = [str(part).casefold() for part in parts]
    if not folded:
        return False
    if folded[-1] in EXECUTION_FILES or any(part in EXECUTION_DIRECTORIES for part in folded[:-1]):
        return True
    for index, part in enumerate(folded[:-1]):
        below = tuple(folded[index + 1:])
        if any(below[:len(prefix)] == prefix for prefix in EXECUTION_NESTED.get(part, ())):
            return True
    return False


_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def inert_path(path: str) -> str:
    """A file name on one line: line breaks, tabs, other control and format characters and line or paragraph
    separators are written as escapes, so a name cannot start a line of its own in a dialog. Idempotent."""
    return "".join(
        _ESCAPES.get(char) or (f"\\u{ord(char):04x}" if ord(char) <= 0xFFFF else f"\\U{ord(char):08x}")
        if unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"} else char for char in path)


def shown_path(path: str) -> str:
    path = inert_path(path)
    if len(path) <= MAX_PATH_CHARS:
        return path
    keep = (MAX_PATH_CHARS - 1) // 2
    return path[:keep] + "…" + path[-(MAX_PATH_CHARS - 1 - keep):]


def change_of(name, arguments, result=None):
    """created, changed or deleted for a file tool, from its result when it has one."""
    if name == "delete_file":
        return "deleted"
    if name == "write_file":
        created = result.get("created") if isinstance(result, dict) and "created" in result else (
            arguments.get("expected_sha256") is None)
        return "created" if created else "changed"
    return "changed"


class ChangeLedger:
    """Files changed since the last approval the person gave, merged per path."""

    def __init__(self):
        self._rows = {}
        self.complete = True  # False when earlier changes could not be read back from the journal

    def record(self, path, change):
        if not isinstance(path, str) or change not in CHANGES:
            return
        before = self._rows.get(path)
        if before == "created" and change == "deleted":
            del self._rows[path]  # nothing is left of it
        elif before == "created":
            pass                  # still a new file
        elif before == "deleted" and change == "created":
            self._rows[path] = "changed"
        else:
            self._rows[path] = change

    def approved(self):
        self._rows.clear()
        self.complete = True

    def fields(self) -> dict:
        rows = [{"path": shown_path(path), "change": change} for path, change in sorted(self._rows.items())]
        value = {"changed_files": rows[:MAX_LISTED], "changed_files_more": max(0, len(rows) - MAX_LISTED)}
        if not self.complete:
            value["changed_files_complete"] = False
        return value

    def replay(self, events, normalise):
        """Rebuild the list from journal events, so a restart does not forget an unapproved write.

        An interrupted write counts only after its action_started: one cancelled while the person was
        still being asked never started, as the live list knows."""
        started = set()
        for event in events:
            data = event.get("data") if isinstance(event, dict) else None
            if not isinstance(data, dict):
                continue
            kind = event.get("type")
            if kind == "approval_decided":
                if data.get("approved") is True and data.get("kind") != "file_write":
                    self.approved()
                continue
            name, arguments = data.get("name"), data.get("arguments")
            if kind == "action_started":
                started.add(data.get("action_id"))
                continue
            if kind not in {"action_completed", "action_interrupted"} or name not in WRITE_TOOLS:
                continue
            if kind == "action_interrupted" and data.get("action_id") not in started:
                continue
            if not isinstance(arguments, dict):
                continue
            result = data.get("result")
            if kind == "action_completed" and not (isinstance(result, dict) and result.get("ok") is True):
                continue
            try:
                path = normalise(arguments.get("path"))
            except ValueError:
                continue
            self.record(path, change_of(name, arguments, result if kind == "action_completed" else None))


def change_preview(before, after):
    """A unified diff of a gated change, bounded; returns (text, count of lines not shown).

    A line is shown whole or not at all: a cut line would hide its end behind a harmless start while the
    count said nothing was left out. The first line that does not fit ends the preview."""
    old = [] if before is None else before.splitlines()
    new = [] if after is None else after.splitlines()
    lines = [line for line in difflib.unified_diff(old, new, lineterm="", n=2)][2:]  # without the ---/+++ header
    shown, used = [], 0
    for line in lines[:MAX_DIFF_LINES]:
        if used + len(line) > MAX_DIFF_CHARS:
            break
        shown.append(line)
        used += len(line)
    return "\n".join(shown), len(lines) - len(shown)


# The approval dialog ---------------------------------------------------------------------------------------

_LINE = {"created": "created: {path}", "changed": "changed: {path}", "deleted": "deleted: {path}"}


def valid_changed_files(request) -> bool:
    """A request without the list is valid (older journals); a present list must be well formed."""
    if "changed_files" not in request:
        return "changed_files_more" not in request
    rows, more = request.get("changed_files"), request.get("changed_files_more", 0)
    if not isinstance(rows, list) or len(rows) > MAX_LISTED or type(more) is not int or more < 0:
        return False
    if request.get("changed_files_complete", False) is not False:
        return False
    return all(isinstance(row, dict) and set(row) == {"path", "change"} and isinstance(row["path"], str)
               and row["path"] and len(row["path"]) <= MAX_PATH_CHARS and row["change"] in CHANGES for row in rows)


def without_changed_files(request) -> dict:
    return {key: value for key, value in request.items()
            if key not in {"changed_files", "changed_files_more", "changed_files_complete"}}


def changed_files_text(request, t) -> str:
    """The translated list for a command or third-party call dialog; the caller makes it inert."""
    if "changed_files" not in request:
        return ""
    rows, more = request["changed_files"], request.get("changed_files_more", 0)
    lines = []
    if not rows and not more:
        lines.append(t("No files were created, changed or deleted since your last approval."))
    else:
        lines.append(t("Files the agent created, changed or deleted since your last approval ({count}):",
                       count=len(rows) + more))
        lines.extend("  " + t(_LINE[row["change"]], path=shown_path(row["path"])) for row in rows)
        if more:
            lines.append("  " + t("…and {count} more.", count=more))
        lines.append(t("What you approve here can run code from these files."))
    if request.get("changed_files_complete") is False:
        lines.append(t("Earlier changes could not be read from the journal; the list may be incomplete."))
    return "\n".join(lines)


def valid_file_write(request) -> bool:
    return (request.get("tool") in WRITE_TOOLS and isinstance(request.get("path"), str) and bool(request["path"])
            and request.get("change") in CHANGES and isinstance(request.get("diff"), str)
            and type(request.get("diff_more_lines")) is int and request["diff_more_lines"] >= 0)


def file_write_text(request, t) -> str:
    lines = [t(_LINE[request["change"]], path=shown_path(request["path"])), "", request["diff"]]
    if request["diff_more_lines"]:
        lines.append(t("{count} more lines of the change are not shown.", count=request["diff_more_lines"]))
    return "\n".join(lines)
