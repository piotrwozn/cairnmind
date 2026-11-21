"""What Cairnmind says when the workspace cannot open, and when the service could not be started (#273, #258).

tmux 3.4+ is needed only by the terminal workspace: `cairnmind`, `cairnmind tui` and `cairnmind workspace
takeover`. Every other command works without it, and doctor reports the workspace as unavailable instead of
Cairnmind as broken. The English sentences are the source; MESSAGES is their Polish for the terminal catalog.
"""
from __future__ import annotations

from . import platform_hints
from .terminal_safe import safe_text

WORKSPACE_NEEDS = "The terminal workspace needs tmux 3.4 or newer."
TMUX_NOT_FOUND = "tmux was not found."
TMUX_FOUND = "Found {version}."
TMUX_SILENT = "tmux did not report its version."
WITHOUT_WORKSPACE = ("Without it, these commands work: cairnmind serve, cairnmind run, cairnmind mcp and "
                     "cairnmind brief.")
COCKPIT_MISSING = "unavailable (tmux 3.4+ not found)"
COCKPIT_OLD = "unavailable (tmux 3.4+ not found; found {version})"
COCKPIT_OTHER = "unavailable (tmux 3.4+ not found; {code})"
SERVICE_NOT_STARTED = ("The Cairnmind service is not running and could not be started: {reason} Start it with "
                       "`{command}`, then call the tool again.")

MESSAGES = {
    WORKSPACE_NEEDS: "Środowisko terminalowe wymaga tmux 3.4 lub nowszego.",
    TMUX_NOT_FOUND: "Nie znaleziono tmux.",
    TMUX_FOUND: "Znaleziono {version}.",
    TMUX_SILENT: "tmux nie podał swojej wersji.",
    WITHOUT_WORKSPACE: ("Bez niego działają polecenia: cairnmind serve, cairnmind run, cairnmind mcp i "
                        "cairnmind brief."),
    COCKPIT_MISSING: "niedostępny (nie znaleziono tmux 3.4+)",
    COCKPIT_OLD: "niedostępny (nie znaleziono tmux 3.4+; znaleziono {version})",
    COCKPIT_OTHER: "niedostępny (nie znaleziono tmux 3.4+; {code})",
    SERVICE_NOT_STARTED: ("Usługa Cairnmind nie działa i nie udało się jej uruchomić: {reason} Uruchom ją "
                          "poleceniem `{command}`, a potem wywołaj narzędzie ponownie."),
}


def _version(found: dict) -> str:
    """The first line `tmux -V` printed, on one line and short (it is another program's output)."""
    return " ".join(str(found.get("version") or "").split())[:80]


def cockpit_status(tmux: dict) -> str | None:
    """What doctor says about the workspace for a tmux block of NativeRuntime.diagnostics; None when ready."""
    if tmux.get("code") == "ready":
        return None
    version = _version(tmux)
    if version:
        return COCKPIT_OLD.format(version=version)
    if tmux.get("code") == "tmux_missing":
        return COCKPIT_MISSING
    return COCKPIT_OTHER.format(code=tmux.get("code") or "unknown")


def workspace_refusal(found: dict) -> str:
    """The one message opening the workspace prints without tmux 3.4+ (found: WorkspaceManager.diagnostics)."""
    version = safe_text(_version(found))
    if version:
        state = TMUX_FOUND.format(version=version)
    elif found.get("code") in {"tmux_unavailable", "tmux_unsupported"}:
        state = TMUX_SILENT
    else:
        state = TMUX_NOT_FOUND
    hint = platform_hints.hint("tmux_launch").rstrip(".") + "."
    return " ".join((WORKSPACE_NEEDS, state, hint, WITHOUT_WORKSPACE))
