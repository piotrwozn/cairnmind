"""Third-party MCP servers a person allowed for the built-in agent of one project (#126).

The list lives only in the owner-local cockpit preferences, under ``project_mcp_servers``, keyed by
project. Only a person changes it, through the authenticated settings route; no command, memory tool,
agent tool or worktree file can define a server. A server is a program the person chose to run under
their own account: Cairnmind is not a sandbox for it. What is checked here is the shape, and that no
secret is handed to it: environment values are literal and a name that looks like a credential, or one
of Cairnmind's own variables, is refused. No server receives a credential in this version.
"""
from __future__ import annotations

import json
import re

from .config import ENV_PREFIXES

MAX_SERVERS = 4
MAX_LIST_CHARS = 64 * 1024
MAX_ARGS = 32
MAX_ENV = 16
MAX_TEXT = 4096
SERVER_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
# The secret pattern of the built-in agent's command environment (agent_tools._SECRET_ENV), widened by
# the other shapes credentials are commonly passed in. A refused name cannot be renamed into a server.
_SECRET_NAME = re.compile(r"(?:TOKEN|PASSWORD|PASSWD|SECRET|API_?KEY|AUTHORIZATION|CREDENTIAL|AUTH(?!OR)|"
                          r"PRIVATE_?KEY|ACCESS_?KEY|COOKIE|SESSION)", re.I)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_FIELDS = {"id", "command", "args", "env"}


def secret_name(name: str) -> bool:
    from .api_credentials import credential_environment_names
    return bool(_SECRET_NAME.search(name)) or name.upper().startswith(ENV_PREFIXES) or name in credential_environment_names()


def validate_server(value) -> dict:
    """One server definition; unknown fields, relative commands and secret-looking names are refused."""
    if not isinstance(value, dict) or set(value) - _FIELDS or not {"id", "command"} <= set(value):
        raise ValueError("A server has an id and an absolute command, and may have args and env.")
    identifier, command = value["id"], value["command"]
    if not isinstance(identifier, str) or not SERVER_ID.fullmatch(identifier):
        raise ValueError("A server id has 1 to 24 lowercase letters, digits or hyphens.")
    if (not isinstance(command, str) or not command.startswith("/") or len(command) > MAX_TEXT
            or _CONTROL.search(command)):
        raise ValueError("A server command must be an absolute path.")
    args = value.get("args", [])
    if (not isinstance(args, list) or len(args) > MAX_ARGS
            or any(not isinstance(arg, str) or len(arg) > MAX_TEXT or "\0" in arg for arg in args)):
        raise ValueError("A server has at most 32 arguments of at most 4096 characters.")
    env = value.get("env", {})
    if not isinstance(env, dict) or len(env) > MAX_ENV:
        raise ValueError("A server has at most 16 environment values.")
    for name, text in env.items():
        if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):
            raise ValueError("An environment name has letters, digits and underscores.")
        if secret_name(name):
            raise ValueError("A server gets no secrets: this environment name is refused.")
        if not isinstance(text, str) or len(text) > MAX_TEXT or "\0" in text:
            raise ValueError("An environment value is literal text of at most 4096 characters.")
    return {"id": identifier, "command": command, "args": list(args), "env": dict(env)}


def inside_worktree(definition: dict, worktree) -> bool:
    """Whether the program or a path argument of a server lies in the worktree.

    A coding session edits the worktree without an approval, so a server program or script there
    would let it choose code that runs at the next start. Such a server is not started.
    """
    from pathlib import Path
    root = Path(worktree).resolve()
    for text in (definition["command"], *definition.get("args", [])):
        if str(root) in text:
            return True
        if text.startswith("/"):
            try:
                if Path(text).resolve().is_relative_to(root):
                    return True
            except (OSError, RuntimeError, ValueError):
                return True
    return False


def validate_servers(value) -> list[dict]:
    if not isinstance(value, list) or len(value) > MAX_SERVERS:
        raise ValueError("A project has at most 4 MCP servers.")
    servers = [validate_server(row) for row in value]
    if len({row["id"] for row in servers}) != len(servers):
        raise ValueError("Server ids of a project must be unique.")
    # The list is copied into the run's private configuration file, which is read with a size limit.
    if len(json.dumps(servers, ensure_ascii=False)) > MAX_LIST_CHARS:
        raise ValueError("The server list of a project is too large.")
    return servers


def frozen_servers(preferences: dict, project_id: str, worktree) -> tuple[list[dict], list[dict]]:
    """The servers a run of this project gets, fixed at launch, and fixed codes for what was left out.

    An invalid stored list gives no servers and the run still starts; so does a server whose program
    or path argument lies in the worktree.
    """
    try:
        stored = preferences.get("project_mcp_servers", {})
        if not isinstance(stored, dict):
            raise ValueError("invalid")
        servers = validate_servers(stored.get(project_id, []))
    except (ValueError, TypeError, AttributeError):
        return [], [{"code": "configuration_invalid"}]
    kept, problems = [], []
    for row in servers:
        if inside_worktree(row, worktree):
            problems.append({"server_id": row["id"], "code": "definition_refused"})
        else:
            kept.append(row)
    return kept, problems


def validate_project_mcp_servers(value) -> dict:
    if not isinstance(value, dict) or len(value) > 1000:
        raise ValueError("Invalid MCP server lists.")
    result = {}
    for project_id, servers in value.items():
        if not isinstance(project_id, str) or not 1 <= len(project_id) <= 160 or "\0" in project_id:
            raise ValueError("Invalid project identifier.")
        result[project_id] = validate_servers(servers)
    return result
