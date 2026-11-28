"""Entries a person wrote by hand that still start the removed `devbrain` command (#314)."""

from __future__ import annotations

import json
import shlex
import tomllib
from pathlib import Path
from typing import Any


def _is_devbrain(command: Any) -> bool:
    try:
        words = shlex.split(command) if isinstance(command, str) else []
    except ValueError:
        return False
    return bool(words) and Path(words[0]).name == "devbrain"


def _servers(document: Any, key: str) -> list[tuple[str, Any]]:
    table = document.get(key) if isinstance(document, dict) else None
    return [(f"{key}.{name}", value.get("command")) for name, value in (table or {}).items()
            if isinstance(value, dict)] if isinstance(table, dict) else []


def _hooks(document: Any) -> list[tuple[str, Any]]:
    table = document.get("hooks") if isinstance(document, dict) else None
    found = []
    for event, groups in (table.items() if isinstance(table, dict) else ()):
        for group in groups if isinstance(groups, list) else ():
            for handler in (group.get("hooks") if isinstance(group, dict) else None) or ():
                if isinstance(handler, dict):
                    found.append((f"hooks.{event}", handler.get("command")))
    return found


def bare_devbrain_entries(root: Path, client: str) -> list[tuple[Path, str]]:
    """(file, entry) for each MCP server or hook of the client's project files that runs `devbrain`.

    A file that cannot be read is skipped: the merge of that same file has already said why.
    """
    if client == "codex":
        files = ((root / ".codex" / "config.toml", lambda d: _servers(d, "mcp_servers"), "toml"),
                 (root / ".codex" / "hooks.json", _hooks, "json"))
    else:
        files = ((root / ".mcp.json", lambda d: _servers(d, "mcpServers"), "json"),
                 (root / ".claude" / "settings.local.json", _hooks, "json"))
    found = []
    for path, entries, kind in files:
        try:
            text = path.read_bytes().decode("utf-8")
            document = tomllib.loads(text) if kind == "toml" else json.loads(text)
        except (OSError, ValueError):
            continue
        found += [(path, entry) for entry, command in entries(document) if _is_devbrain(command)]
    return found
