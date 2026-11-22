"""The mark scripts/install-wsl.sh leaves while it replaces the installed Cairnmind (#298).

The installer writes its own process id to one file per user before it stops anything and removes it when the new
version is installed or when it ends early. While the mark names a running installer, or cannot be read, nothing
starts the knowledge service: a service started now would run the version being replaced. A mark whose installer
is gone (a killed installer) blocks nothing and doctor says the upgrade did not finish.
The English sentences are the source; MESSAGES is their Polish for the terminal catalog.
"""
from __future__ import annotations

from pathlib import Path
import re

from . import process_identity

INSTALLER = "install-wsl.sh"
BLOCKING = frozenset({"running", "unreadable"})
_MARK = re.compile(rb"([0-9]{1,10})\n?")

NOTE_RUNNING = ("An upgrade of Cairnmind is in progress (installer process {pid}), so the knowledge service is not "
                "started until the installer has finished. If no installer is running, remove "
                "~/.local/state/cairnmind/upgrade-in-progress.")
NOTE_STALE = ("An upgrade of Cairnmind did not finish: the installer that began it (process {pid}) no longer runs. "
              "Run the installer again; it removes ~/.local/state/cairnmind/upgrade-in-progress when it finishes.")
NOTE_UNREADABLE = ("The upgrade mark ~/.local/state/cairnmind/upgrade-in-progress cannot be read, so the knowledge "
                   "service is not started. If no installer is running, remove that file.")
UPGRADE_STOP_FAILED = ("An upgrade of Cairnmind began while the knowledge service was starting, and the service did "
                       "not stop when asked. Stop it with `{command}`, and try again when the installer has finished.")
BRIDGE_UPGRADE = ("{reason} When the installer has finished, restart this MCP server in your editor so that it runs "
                  "the new version, then call the tool again.")
CODE_REPLACED = ("The installed Cairnmind changed after this MCP server started (an upgrade or a reinstall), so this "
                 "server still runs the code of version {version} it started with. When the upgrade has finished, "
                 "restart this MCP server in your editor, or the editor, then call the tool again.")

MESSAGES = {
    NOTE_RUNNING: ("Trwa aktualizacja Cairnmind (proces instalatora {pid}), więc usługa wiedzy nie jest uruchamiana, "
                   "dopóki instalator nie skończy. Jeśli żaden instalator nie działa, usuń "
                   "~/.local/state/cairnmind/upgrade-in-progress."),
    NOTE_STALE: ("Aktualizacja Cairnmind nie została dokończona: instalator, który ją zaczął (proces {pid}), już nie "
                 "działa. Uruchom instalator ponownie; usunie ~/.local/state/cairnmind/upgrade-in-progress, gdy skończy."),
    NOTE_UNREADABLE: ("Nie można odczytać znacznika aktualizacji ~/.local/state/cairnmind/upgrade-in-progress, więc "
                      "usługa wiedzy nie jest uruchamiana. Jeśli żaden instalator nie działa, usuń ten plik."),
    UPGRADE_STOP_FAILED: ("Aktualizacja Cairnmind zaczęła się w trakcie uruchamiania usługi wiedzy, a usługa nie "
                          "zatrzymała się na wezwanie. Zatrzymaj ją poleceniem `{command}` i spróbuj ponownie, gdy "
                          "instalator skończy."),
    BRIDGE_UPGRADE: ("{reason} Gdy instalator skończy, uruchom ponownie ten serwer MCP w edytorze, aby działał w nowej "
                     "wersji, a potem wywołaj narzędzie ponownie."),
    CODE_REPLACED: ("Zainstalowany Cairnmind zmienił się po uruchomieniu tego serwera MCP (aktualizacja albo ponowna "
                    "instalacja), więc ten serwer nadal działa na kodzie wersji {version}, z którym wystartował. Gdy "
                    "aktualizacja się zakończy, uruchom ponownie ten serwer MCP w edytorze albo cały edytor, a potem "
                    "wywołaj narzędzie ponownie."),
}


def mark_path() -> Path:
    """The file upgrade_mark names in scripts/install-wsl.sh: one per user, for every data directory."""
    return Path.home() / ".local" / "state" / "cairnmind" / "upgrade-in-progress"


def state() -> tuple[str, int | None]:
    """("none" | "running" | "stale" | "unreadable", the installer's pid or None). Never guesses: a process that runs
    and cannot be identified counts as the installer."""
    try:
        with open(mark_path(), "rb") as handle:
            raw = handle.read(33)
    except FileNotFoundError:
        return "none", None
    except (OSError, RuntimeError):  # RuntimeError: Path.home() found no home directory
        return "unreadable", None
    found = _MARK.fullmatch(raw)
    if found is None:
        return "unreadable", None
    pid = int(found.group(1))
    arguments = process_identity.command_line(pid)
    if arguments is None:
        return ("running" if process_identity.alive(pid) else "stale"), pid
    return ("running" if any(Path(argument).name == INSTALLER for argument in arguments) else "stale"), pid


def _text(kind: str, pid: int | None) -> str | None:
    template = {"running": NOTE_RUNNING, "stale": NOTE_STALE, "unreadable": NOTE_UNREADABLE}.get(kind)
    return template.format(pid=pid) if template else None


def note() -> str | None:
    """doctor's line for the mark, None without one."""
    return _text(*state())


def refusal() -> str | None:
    """Why the service must not be started now, None when it may be."""
    kind, pid = state()
    return _text(kind, pid) if kind in BLOCKING else None
