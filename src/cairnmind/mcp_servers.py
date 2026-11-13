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
# Variables that change which code a program loads or where it looks for it. A server gets the
# worker's own HOME, PATH and SHELL; configuring them, or any of these, is refused outright.
LOADER_ENV = ("PATH", "HOME", "SHELL", "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "PYTHONPATH",
              "PYTHONSTARTUP", "PYTHONHOME", "PYTHONUSERBASE", "PYTHONPLATLIBDIR", "NODE_PATH", "NODE_OPTIONS",
              "PERL5LIB", "PERL5OPT", "PERLLIB", "RUBYLIB", "RUBYOPT", "GEM_HOME", "GEM_PATH", "BASH_ENV",
              "ENV", "CLASSPATH", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS", "XDG_CONFIG_HOME",
              "XDG_DATA_HOME")
LOADER_PREFIXES = ("LD_", "DYLD_")


def secret_name(name: str) -> bool:
    from .api_credentials import credential_environment_names
    return bool(_SECRET_NAME.search(name)) or name.upper().startswith(ENV_PREFIXES) or name in credential_environment_names()


def loader_name(name: str) -> bool:
    return name.upper() in LOADER_ENV or name.upper().startswith(LOADER_PREFIXES)


def _text(value) -> bool:
    """Literal text that can be written to a process: bounded, no NUL, encodable as UTF-8."""
    if not isinstance(value, str) or len(value) > MAX_TEXT or "\0" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def validate_server(value) -> dict:
    """One server definition; unknown fields, relative commands and secret-looking names are refused."""
    if not isinstance(value, dict) or set(value) - _FIELDS or not {"id", "command"} <= set(value):
        raise ValueError("A server has an id and an absolute command, and may have args and env.")
    identifier, command = value["id"], value["command"]
    if not isinstance(identifier, str) or not SERVER_ID.fullmatch(identifier):
        raise ValueError("A server id has 1 to 24 lowercase letters, digits or hyphens.")
    if not _text(command) or not command.startswith("/") or _CONTROL.search(command):
        raise ValueError("A server command must be an absolute path.")
    args = value.get("args", [])
    if not isinstance(args, list) or len(args) > MAX_ARGS or any(not _text(arg) for arg in args):
        raise ValueError("A server has at most 32 arguments of at most 4096 characters.")
    env = value.get("env", {})
    if not isinstance(env, dict) or len(env) > MAX_ENV:
        raise ValueError("A server has at most 16 environment values.")
    for name, text in env.items():
        if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):
            raise ValueError("An environment name has letters, digits and underscores.")
        if secret_name(name):
            raise ValueError("A server gets no secrets: this environment name is refused.")
        if loader_name(name):
            raise ValueError("This environment name changes which code a program loads and is not allowed.")
        if not _text(text):
            raise ValueError("An environment value is literal text of at most 4096 characters.")
    return {"id": identifier, "command": command, "args": list(args), "env": dict(env)}


_SEPARATORS = re.compile(r"[=:,;\s]+")
_PATH_CHARACTER = re.compile(r"[A-Za-z0-9._-]")


def server_directory():
    """What a relative path in a server definition is relative to: the server's working directory, a private
    directory created directly under the system temporary directory when it starts (mcp_host)."""
    import tempfile
    from pathlib import Path
    return Path(tempfile.gettempdir()) / "cairnmind-mcp-server"


def _normal(text: str) -> str:
    text = re.sub(r"/+", "/", text.replace("\\", "/"))
    while "/./" in text:
        text = text.replace("/./", "/")
    return text


def _tokens(text: str):
    """Every piece of a value that can be a path: split on '=', ':', ',', ';' and whitespace, and in each
    piece also the part from its first '/' on (an option such as '-f/path')."""
    for token in _SEPARATORS.split(text):
        token = token.strip("'\"()[]{}")
        if token:
            yield token
            slash = token.find("/")
            if slash > 0:
                yield token[slash:]


def _names_root(text: str, roots) -> bool:
    """Whether a spelling of the worktree root appears in a value as a path prefix: followed by the end, a
    '/' or a character that cannot continue a file name, so 'app-mcp' or 'app.venv' next to 'app' do not."""
    value = _normal(text)
    for root in roots:
        start = value.find(root)
        while start >= 0:
            after = value[start + len(root):start + len(root) + 1]
            if not after or not _PATH_CHARACTER.match(after):
                return True
            start = value.find(root, start + 1)
    return False


def inside_worktree(definition: dict, worktree) -> bool:
    """Whether the program, an argument or an environment value of a server names a path in the worktree.

    A coding session edits the worktree without an approval, so code there would let it choose what runs
    at the next start. Two checks, either refuses:

    - every token of every value that can be a path is resolved (a relative one against the server's
      working directory, ``server_directory``; links are followed, so a link outside that points in is
      seen) and compared with the worktree component by component;
    - the worktree root, as given and as its real path, must not appear as a path prefix anywhere in a
      value once separators are normalised, which also catches a path inside a shell command, a link inside
      the worktree that points out (a virtual environment's interpreter) and an alias spelled in full.

    A sibling that merely shares a name prefix is not inside. A value that only becomes a path inside the
    server (a name it looks up, a configuration file it reads, ``$HOME`` expanded by a shell) cannot be seen.
    """
    import os
    from pathlib import Path
    given = Path(os.path.abspath(worktree))
    root = given.resolve()
    roots = {_normal(str(given)), _normal(str(root))}
    base = server_directory()
    values = (definition["command"], *definition.get("args", []), *(definition.get("env") or {}).values())
    for text in values:
        if _names_root(text, roots):
            return True
        for token in _tokens(text):
            try:
                path = Path(os.path.expanduser(token))
                path = (path if path.is_absolute() else base / path).resolve()
                if path.is_relative_to(root):
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
            raise ValueError("Invalid MCP server lists.")
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


def apply_project_edit(stored: dict, edit) -> dict:
    """One project's list replaced, only if it is still the list the editor showed (compare and swap).

    The editor sends the list it read and the list it wants; the other projects' lists are taken from the
    file under the preferences lock, so an edit made elsewhere meanwhile is neither undone nor lost.
    """
    if not isinstance(edit, dict) or set(edit) != {"project", "expected", "servers"}:
        raise ValueError("Invalid MCP server lists.")
    project = edit["project"]
    if not isinstance(project, str) or not 1 <= len(project) <= 160 or "\0" in project:
        raise ValueError("Invalid project identifier.")
    if edit["expected"] != stored.get(project, []):
        raise ValueError("The MCP servers of this project changed meanwhile. Open the list again.")
    servers = validate_servers(edit["servers"])
    result = {key: rows for key, rows in stored.items() if key != project}
    if servers:
        result[project] = servers
    return result


def validate_project_mcp_servers(value) -> dict:
    if not isinstance(value, dict) or len(value) > 1000:
        raise ValueError("Invalid MCP server lists.")
    result = {}
    for project_id, servers in value.items():
        if not isinstance(project_id, str) or not 1 <= len(project_id) <= 160 or "\0" in project_id:
            raise ValueError("Invalid project identifier.")
        result[project_id] = validate_servers(servers)
    return result
