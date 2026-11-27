"""A third-party MCP server of the built-in agent that did not start, beyond the agent pane (#209).

The agent worker reports a fixed code and the id the person gave the server to the broker, which keeps
them with the run (RuntimeSupervisor.record_mcp_failure); the sidebar reads them with the runs it polls
and posts one notice per server per run, on the notice line for the project shown and otherwise only in
Notices, under the run's project. Nothing the server said travels: its error output is discarded by the
host, and only a code from the fixed set below and a well-formed id are accepted on either side.
"""
from __future__ import annotations

import re

from cairnmind.terminal.terminal_notices import about

# The same rule as mcp_host.SERVER_ID; kept here so the sidebar does not import the host.
SERVER_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")
# Every code McpHost.start reports for a server that did not start (mcp_host._START_CODES and start()).
START_FAILURE_CODES = frozenset({"start_failed", "start_timeout", "protocol_unsupported", "catalogue_invalid",
                                 "definition_refused", "program_unavailable"})
# The reason in words, English source. configuration_invalid is the launcher's (mcp_servers.frozen_servers),
# shown in the agent pane only.
REASONS = {
    "start_failed": "its program did not start or stopped during its start",
    "start_timeout": "it did not answer within its start time",
    "protocol_unsupported": "it uses an MCP protocol version that is not supported",
    "catalogue_invalid": "its list of tools cannot be used",
    "definition_refused": "its definition was refused",
    "configuration_invalid": "the list of servers of this project cannot be read",
    "program_unavailable": "its program does not exist or cannot be run on this computer",
}
NOTICE = "Third-party MCP server {server} did not start: {reason}. The agent works without its tools."
MESSAGES = {
    REASONS["start_failed"]: "jego program nie uruchomił się albo zakończył się w trakcie startu",
    REASONS["start_timeout"]: "nie odpowiedział w czasie przewidzianym na start",
    REASONS["protocol_unsupported"]: "używa nieobsługiwanej wersji protokołu MCP",
    REASONS["catalogue_invalid"]: "jego listy narzędzi nie da się użyć",
    REASONS["definition_refused"]: "jego definicję odrzucono",
    REASONS["configuration_invalid"]: "listy serwerów tego projektu nie da się odczytać",
    REASONS["program_unavailable"]: "jego programu nie ma albo nie da się go uruchomić na tym komputerze",
    NOTICE: "Zewnętrzny serwer MCP {server} nie uruchomił się: {reason}. Agent pracuje bez jego narzędzi.",
}
MAX_SHOWN = 1000  # (run, generation, server) keys remembered by one sidebar; a run has at most 4 servers
_LIVE = {"starting", "running"}


def valid_report(server_id, code) -> bool:
    """A report the broker can keep: a code of the fixed set and a well-formed id, nothing else."""
    return (isinstance(code, str) and code in START_FAILURE_CODES
            and isinstance(server_id, str) and SERVER_ID.fullmatch(server_id) is not None)


def reason(code, translate) -> str:
    """The translated reason of a fixed code; a code outside the set stays "unknown", never its text."""
    return translate(REASONS[code]) if isinstance(code, str) and code in REASONS else "unknown"


def notice_text(server_id, code, translate) -> str:
    return translate(NOTICE, server=server_id, reason=reason(code, translate))


def failures(run) -> list[tuple[str, str]]:
    """The validated (server id, code) pairs a run record carries; anything else in it is ignored."""
    rows = run.get("mcp_failures") if isinstance(run, dict) else None
    if not isinstance(rows, list):
        return []
    found = []
    for row in rows[:16]:
        if isinstance(row, dict) and valid_report(row.get("server_id"), row.get("code")):
            pair = (row["server_id"], row["code"])
            if pair[0] not in {server for server, _ in found}:
                found.append(pair)
    return found


def announce(app, runs) -> None:
    """Post the failures of live agent runs not posted yet by this sidebar: one notice per server per run.

    A run of the project shown gets the notice line; a run of another project or folder only Notices,
    under that project. Ended runs are not announced: what they reported was for their own time.
    """
    shown = app.__dict__.setdefault("_mcp_failures_shown", set())
    current = set()
    workspace = getattr(app, "workspace_data", None) or {}
    scope = (workspace.get("project_id"), workspace.get("worktree"))
    for run in runs if isinstance(runs, list) else []:
        if (not isinstance(run, dict) or run.get("client") != "api"
                or run.get("lifecycle", run.get("state")) not in _LIVE
                or not isinstance(run.get("id"), str) or type(run.get("generation")) is not int):
            continue
        for server_id, code in failures(run):
            key = (run["id"], run["generation"], server_id)
            current.add(key)
            if key in shown or len(shown) >= MAX_SHOWN:
                continue
            shown.add(key)
            project_id, worktree = run.get("project_id"), run.get("worktree")
            project = (getattr(app, "projects", None) or {}).get(project_id) or {}
            subject = about(project_id, project.get("name"), worktree)
            text = notice_text(server_id, code, app.t)
            if (project_id, worktree) == scope:
                app.notice_for(subject, text, "warn")
            else:
                app.notice_elsewhere(subject, text, "warn")
    shown.intersection_update(current)  # a run that ended or left the list is forgotten
