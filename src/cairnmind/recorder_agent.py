"""The cairnmind-recorder subagent that `cairnmind install` writes for Claude Code (#376).

It runs its own MCP server process, so it is its own external session: it reads and acknowledges its own
packet in its own context, stores the words it was given and answers the main conversation in one line.
Nothing here changes the acknowledgement gate or any stored row.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

RECORDER_PATH = ".claude/agents/cairnmind-recorder.md"
RECORDER_SERVER = "cairnmind-recorder"
RECORDER_TOOLS = tuple(f"mcp__{RECORDER_SERVER}__{name}" for name in ("resume_context", "ack_resume", "remember", "checkpoint"))
# The version since which Claude Code asks to trust the folder before it starts an inline server of a project agent.
MINIMUM_CLAUDE = (2, 1, 238)
RECORDER_REPLY = "Recorded {kind} {id} v{version} as proposed, reported by external session {session}, not verified."
NOT_RECORDED = "Not recorded: {reason}"

RECORDER_DESCRIPTION = (
    "Records a decision, constraint, preference, fact, question or checkpoint in Cairnmind without spending this "
    "conversation's context. Use it instead of remember or checkpoint when anything changed since your last "
    "ack_resume (your own decision, checkpoint or task update included): writing yourself would first need the "
    "whole packet again. Give one kind and the exact words, quoted; it records nothing else and answers in one line. "
    "It writes as its own external session: kept as reported, never verified, never confirmed. "
    "Save plans and task progress yourself.")

_KINDS = "decision, constraint, preference, fact, question or checkpoint"
RECORDER_INSTRUCTIONS = f"""You are cairnmind-recorder. You store, in Cairnmind project `{{project}}`, exactly the words the delegating conversation quoted to you, through the cairnmind-recorder tools and nothing else. You never decide, summarise, verify, confirm or add anything.

0. If the cairnmind-recorder tools are not available, reply `{NOT_RECORDED.format(reason="the cairnmind-recorder tools are not available in this session.")}` and stop.
1. The request names one kind ({_KINDS}) and quotes the words to record. If it names no kind or another kind, or quotes nothing, reply `{NOT_RECORDED.format(reason=f"name one kind ({_KINDS}) and quote the words.")}` and stop.
2. Call resume_context and read the whole packet. If its status is not ready, reply `{NOT_RECORDED.format(reason="the packet is <status>: <its first unknown>")}` and stop. Otherwise call ack_resume with the packet's exact id and hash and the readback {{"plan": {{"id": ..., "version": ...}} or null, "task": {{"id": ..., "version": ...}} or null}}, read from active_plan and active_task.
3. For a decision, constraint, preference, fact or question, call remember with project "{{project}}", record {{"kind": <the kind>, "content": <the quoted words>}} and sources []. For a checkpoint, call checkpoint with project "{{project}}" and state {{"summary": <the quoted words>}}. Never send a status, a key, metadata or anything the request did not quote.
4. If the write answers resume_required, do step 2 once more and retry once. On any other refusal, or a second one, reply `{NOT_RECORDED.format(reason="<the first sentence of the refusal>")}` and stop.
5. Reply with exactly one line: `{RECORDER_REPLY.format(kind="<kind>", id="<id>", version="<version>", session="<origin_session_id>")}`
"""

RECORDER_NOT_WRITTEN = ("Claude Code {version} is older than 2.1.238, so the cairnmind-recorder subagent was not written: "
                        "older versions start a subagent's own MCP server without asking you to trust the folder.")
RECORDER_REMOVED = ("Claude Code {version} is older than 2.1.238, so the cairnmind-recorder subagent written earlier was "
                    "removed ({path}; a backup is beside it): older versions start a subagent's own MCP server without "
                    "asking you to trust the folder.")
RECORDER_KEPT = ("{path} is not the file cairnmind install writes (it has no Cairnmind markers), so it was left unchanged "
                 "and the cairnmind-recorder subagent was not set up.")

MESSAGES = {
    RECORDER_NOT_WRITTEN: ("Claude Code {version} jest starszy niż 2.1.238, więc nie zapisano subagenta cairnmind-recorder: "
                           "starsze wersje uruchamiają własny serwer MCP subagenta, nie pytając, czy ufasz folderowi."),
    RECORDER_REMOVED: ("Claude Code {version} jest starszy niż 2.1.238, więc usunięto zapisanego wcześniej subagenta "
                       "cairnmind-recorder ({path}; kopia leży obok): starsze wersje uruchamiają własny serwer MCP "
                       "subagenta, nie pytając, czy ufasz folderowi."),
    RECORDER_KEPT: ("{path} nie jest plikiem, który zapisuje cairnmind install (nie ma znaczników Cairnmind), więc "
                    "zostawiono go bez zmian i nie skonfigurowano subagenta cairnmind-recorder."),
}


def agent_file(settings, project: dict[str, Any]) -> str:
    """The whole file. Each scalar is a JSON string, which is valid YAML."""
    from .integrations import BEGIN, DATA_NOT_INSTRUCTIONS, END, SESSION_ENV_VARS, _command

    command = _command(settings, "mcp", "--client", "external", "--project", project["id"])
    dump = lambda value: json.dumps(value, ensure_ascii=False)  # noqa: E731
    server = (f"  - {RECORDER_SERVER}:\n      type: stdio\n      command: {dump(command[0])}\n"
              f"      args: {dump(command[1:])}\n      env: {dump({name: '' for name in SESSION_ENV_VARS})}\n")
    body = RECORDER_INSTRUCTIONS.replace("{project}", project["id"]) + "\n" + DATA_NOT_INSTRUCTIONS + "\n"
    body += ("cairnmind install writes this file and replaces it whole on the next install; "
             "remove the two Cairnmind marker lines to keep your own version.\n")
    return (f"---\nname: {RECORDER_SERVER}\ndescription: {dump(RECORDER_DESCRIPTION)}\ntools: {', '.join(RECORDER_TOOLS)}\n"
            f"mcpServers:\n{server}---\n{BEGIN}\n{body}{END}\n")


def merge_recorder(root: Path, settings, project: dict[str, Any], version: str | None) -> tuple[bool, str | None]:
    """Write, update, remove or leave the file; returns (changed, a sentence for the person or None)."""
    from .integrations import BEGIN, END, _version_tuple, _write_changed

    path = root / RECORDER_PATH
    try:
        text = path.read_text(encoding="utf-8") if path.is_file() and not path.is_symlink() else ""
    except UnicodeDecodeError:
        text = ""
    ours = bool(text) and text.count(BEGIN) == 1 and text.count(END) == 1
    semver = _version_tuple(version or "")
    if semver is None or semver < MINIMUM_CLAUDE:
        shown = {"version": version or "unknown", "path": RECORDER_PATH}
        if not ours:
            return False, RECORDER_NOT_WRITTEN.format(**shown)
        shutil.copy2(path, path.with_name(f"{path.name}.cairnmind-backup-{time.time_ns()}"))
        path.unlink()
        return True, RECORDER_REMOVED.format(**shown)
    if path.exists() or path.is_symlink():
        if not ours:
            return False, RECORDER_KEPT.format(path=RECORDER_PATH)
    return _write_changed(path, agent_file(settings, project), new_mode=0o600), None
