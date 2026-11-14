"""More → MCP servers: the third-party servers of one project's built-in agent (#126).

Only a person changes the list, here, through the authenticated settings route; a coding session and the
built-in agent have no tool, command or file for it. The list applies from the next agent run of the
project. Stored text is shown as literal text, never as terminal control codes.
"""
from __future__ import annotations

from .mcp_servers import MAX_SERVERS, validate_server
from .terminal_safe import safe_text

NOTE = ("A server is a program you choose to run under your own account. Cairnmind is not a sandbox for it: "
        "it can read and change what your account can. Only the built-in API agent of this project offers its "
        "tools, and every call needs your approval of the exact arguments. Text a server returns can still "
        "steer the agent into editing files in this folder, as text in the repository can. Do not enter "
        "secrets: values are stored as plain text and names that look like secrets are refused. The change "
        "applies from the next agent run.")
LOADER_NOTE = ("Not allowed as environment names, since they change which code a program loads: PATH, HOME, SHELL, "
               "LD_* and DYLD_*, PYTHONPATH, PYTHONSTARTUP, PYTHONHOME, PYTHONUSERBASE, PYTHONPLATLIBDIR, "
               "NODE_PATH, NODE_OPTIONS, PERL5LIB, PERL5OPT, PERLLIB, RUBYLIB, RUBYOPT, GEM_HOME, GEM_PATH, "
               "BASH_ENV, ENV, CLASSPATH, JAVA_TOOL_OPTIONS, _JAVA_OPTIONS, JDK_JAVA_OPTIONS, XDG_CONFIG_HOME, "
               "XDG_DATA_HOME. A server whose program, an argument or a value names a path inside this project "
               "folder is not started.")
SAVED = "MCP server saved. The built-in agent of this project uses it from its next run."
REMOVED = "MCP server removed. The built-in agent of this project stops using it from its next run."
SAVED_NOT_RUNNABLE = ("MCP server saved, but its program does not exist or cannot be run on this computer. Until it "
                      "can, the built-in agent reports the server off and works without its tools.")


def runnable(command: str) -> bool:
    """Whether the program can be started here now; it may still be installed later, so this only warns."""
    import os
    return os.path.isfile(command) and os.access(command, os.X_OK)


def parse_form(values: dict) -> dict:
    """A server definition from the form: one argument per line, one NAME=value per line."""
    args = [line.rstrip("\r") for line in values.get("args", "").splitlines() if line.strip()]
    env = {}
    for line in values.get("env", "").splitlines():
        if not line.strip():
            continue
        name, separator, value = line.rstrip("\r").partition("=")
        if not separator:
            raise ValueError("Write each environment value as NAME=value.")
        env[name.strip()] = value
    return validate_server({"id": values.get("id", ""), "command": values.get("command", ""),
                            "args": args, "env": env})


def server_line(row: dict) -> str:
    return safe_text(row.get("id", "") + " · " + " ".join([row.get("command", ""), *row.get("args", [])]), limit=160)


def server_details(row: dict, translate) -> str:
    return "\n".join([
        translate("Server") + ": " + safe_text(row.get("id", "")),
        translate("Program") + ": " + safe_text(row.get("command", "")),
        translate("Arguments") + ": " + safe_text(" ".join(row.get("args", [])), limit=2000),
        translate("Environment names") + ": " + safe_text(", ".join(sorted(row.get("env", {})))),
    ])


async def _save(app, project_id: str, shown: list, servers: list) -> None:
    """Replace only this project's list, and only while it is still the list shown (checked under the lock)."""
    await app.request("PUT", "/api/settings", json={"project_mcp_server_edit": {
        "project": project_id, "expected": shown, "servers": servers}})


async def edit_mcp_servers(app):
    from .terminal_panels import ConfirmModal
    from .terminal_ui import ChoiceModal, Field, FormModal
    project_id = app.workspace_data.get("project_id")
    if not project_id:
        raise ValueError(app.t("Select a visible project first."))
    stored = app.data.get("settings", {}).get("project_mcp_servers", {}) or {}
    current = list(stored.get(project_id, []))
    choices = [(server_line(row), row["id"]) for row in current]
    if len(current) < MAX_SERVERS:
        choices.append(("Add server", "+"))
    choice = await app.push_screen_wait(ChoiceModal("MCP servers", choices))
    if choice is None:
        return
    if choice == "+":
        async def submit(values):
            row = parse_form(values)
            if any(existing["id"] == row["id"] for existing in current):
                raise ValueError("Server ids of a project must be unique.")
            await _save(app, project_id, current, [*current, row])

        saved = await app.push_screen_wait(FormModal("Add MCP server", [
            Field("id", "Server id (lowercase letters, digits, hyphens)", required=True),
            Field("command", "Program (absolute path)", required=True),
            Field("args", "Arguments, one per line", multiline=True),
            Field("env", "Environment values, NAME=value per line (no secrets)", multiline=True),
        ], note=app.t(NOTE) + "\n\n" + app.t(LOADER_NOTE), submit="Save", on_submit=submit))
        if saved:
            await app.load_state()
            command = saved.get("command", "") if isinstance(saved, dict) else ""
            if command and not runnable(command):
                app.notice(SAVED_NOT_RUNNABLE, "warn")
            else:
                app.notice(SAVED)
        return
    row = next((existing for existing in current if existing["id"] == choice), None)
    if row is None:
        return
    if await app.push_screen_wait(ConfirmModal("Remove MCP server", server_details(row, app.t), approve="Remove")):
        await _save(app, project_id, current, [existing for existing in current if existing["id"] != choice])
        await app.load_state()
        app.notice(REMOVED)
