"""The mark scripts/install-wsl.sh leaves while it replaces the installed Cairnmind (#298).

The installer writes its own process id to one file per user before it stops anything and removes it when the new
version is installed or when it ends early. While the mark names a running installer, or cannot be read, nothing
starts the knowledge service: a service started now would run the version being replaced. A mark whose installer
is gone (a killed installer) blocks nothing and doctor says the upgrade did not finish.
The English sentences are the source; MESSAGES is their Polish for the terminal catalog.
"""
from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import re
import sys
import time

from cairnmind.pal import process_identity

INSTALLER = "install-wsl.sh"
INSTALLERS = frozenset({"install-wsl.sh", "install-windows.ps1"})
BLOCKING = frozenset({"running", "unreadable"})
_SHELLS = frozenset({"bash", "sh", "dash", "zsh", "powershell", "powershell.exe", "pwsh", "pwsh.exe"})
_READS, _PAUSE = 3, 0.05
_MARK = re.compile(rb"([0-9]{1,10})\r?\n?")

NOTE_RUNNING = ("An upgrade of Cairnmind is in progress (installer process {pid}), or that process cannot be "
                "identified as the installer, so the knowledge service is not started until it has finished. If no "
                "installer is running, remove ~/.local/state/cairnmind/upgrade-in-progress.")
NOTE_STALE = ("An upgrade of Cairnmind did not finish: the installer that began it (process {pid}) no longer runs. "
              "Run the installer again; it removes ~/.local/state/cairnmind/upgrade-in-progress when it finishes.")
NOTE_UNREADABLE = ("The upgrade mark ~/.local/state/cairnmind/upgrade-in-progress cannot be read, so the knowledge "
                   "service is not started. If no installer is running, remove that file.")
UPGRADE_STOP_FAILED = ("An upgrade of Cairnmind began while the knowledge service was starting, and the service did "
                       "not stop when asked. Stop it with `{command}`, and try again when the installer has finished.")
BROKER_STOP_FAILED = ("An upgrade of Cairnmind began while the local broker was starting, and the broker did not stop "
                      "when asked. Stop it with `{command}`, and open the workspace again when the installer has "
                      "finished.")
UPGRADE_ENDED = ("An upgrade of Cairnmind happened while the knowledge service was starting, so that start was "
                 "ended. The upgrade has finished: call again.")
BRIDGE_UPGRADE ="{reason} Call the tool again when the installer has finished."
CODE_UNREADABLE = ("The installed files of Cairnmind could not be read when this MCP server started, so it cannot tell "
                   "whether they changed. Restart this MCP server in your editor, then call the tool again.")
CODE_REPLACED = ("The installed Cairnmind changed after this MCP server started (an upgrade or a reinstall), so this "
                 "server still runs the code of version {version} it started with. When the upgrade has finished, "
                 "restart this MCP server in your editor, or the editor, then call the tool again.")

MESSAGES = {
    NOTE_RUNNING: ("Trwa aktualizacja Cairnmind (proces instalatora {pid}) albo ten proces nie daje się rozpoznać "
                   "jako instalator, więc usługa wiedzy nie jest uruchamiana, dopóki się nie skończy. Jeśli żaden "
                   "instalator nie działa, usuń ~/.local/state/cairnmind/upgrade-in-progress."),
    BROKER_STOP_FAILED: ("Aktualizacja Cairnmind zaczęła się w trakcie uruchamiania lokalnego brokera, a broker nie "
                         "zatrzymał się na wezwanie. Zatrzymaj go poleceniem `{command}` i otwórz środowisko ponownie, "
                         "gdy instalator skończy."),
    UPGRADE_ENDED: ("Aktualizacja Cairnmind odbyła się w trakcie uruchamiania usługi wiedzy, więc to uruchomienie "
                    "zakończono. Aktualizacja się skończyła: wywołaj ponownie."),
    NOTE_STALE: ("Aktualizacja Cairnmind nie została dokończona: instalator, który ją zaczął (proces {pid}), już nie "
                 "działa. Uruchom instalator ponownie; usunie ~/.local/state/cairnmind/upgrade-in-progress, gdy skończy."),
    NOTE_UNREADABLE: ("Nie można odczytać znacznika aktualizacji ~/.local/state/cairnmind/upgrade-in-progress, więc "
                      "usługa wiedzy nie jest uruchamiana. Jeśli żaden instalator nie działa, usuń ten plik."),
    UPGRADE_STOP_FAILED: ("Aktualizacja Cairnmind zaczęła się w trakcie uruchamiania usługi wiedzy, a usługa nie "
                          "zatrzymała się na wezwanie. Zatrzymaj ją poleceniem `{command}` i spróbuj ponownie, gdy "
                          "instalator skończy."),
    BRIDGE_UPGRADE: "{reason} Wywołaj narzędzie ponownie, gdy instalator skończy.",
    CODE_UNREADABLE: ("Nie udało się odczytać zainstalowanych plików Cairnmind, gdy ten serwer MCP startował, więc nie "
                      "wiadomo, czy się zmieniły. Uruchom ponownie ten serwer MCP w edytorze, a potem wywołaj narzędzie "
                      "ponownie."),
    CODE_REPLACED: ("Zainstalowany Cairnmind zmienił się po uruchomieniu tego serwera MCP (aktualizacja albo ponowna "
                    "instalacja), więc ten serwer nadal działa na kodzie wersji {version}, z którym wystartował. Gdy "
                    "aktualizacja się zakończy, uruchom ponownie ten serwer MCP w edytorze albo cały edytor, a potem "
                    "wywołaj narzędzie ponownie."),
}


def mark_path() -> Path:
    """The file upgrade_mark names in scripts/install-wsl.sh and scripts/install-windows.ps1: one per user."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else (Path.home() / "AppData" / "Local")
        return root / "cairnmind" / "state" / "upgrade-in-progress"
    return Path.home() / ".local" / "state" / "cairnmind" / "upgrade-in-progress"


def mark_state(path: Path, holds: Callable[[list[str]], bool], may_hold: Callable[[str], bool]
               ) -> tuple[str, int | None]:
    """("none" | "running" | "stale" | "unreadable", pid) of a mark file that holds one process id. `holds(arguments)`
    says whether a command line is the holder's; `may_hold(executable)` whether a process whose command line cannot be
    read may be it. Never guesses: a running process that cannot be identified counts as the holder."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(33)
    except FileNotFoundError:
        return "none", None
    except OSError:
        return "unreadable", None
    found = _MARK.fullmatch(raw)
    if found is None:
        return "unreadable", None
    pid = int(found.group(1))
    if process_identity._pid(pid) is None:  # 0 or beyond pid_t: the holder never writes it, so the file is malformed
        return "unreadable", None
    for read in range(_READS):
        if read:
            time.sleep(_PAUSE)  # a read of a living process can fail once (macOS, around exec): ask again
        arguments = process_identity.command_line(pid)
        if arguments is not None:
            return ("running" if holds(arguments) else "stale"), pid
        program = process_identity.executable(pid)
        if program is None or may_hold(program):
            return ("running" if process_identity.alive(pid) else "stale"), pid
        if not process_identity.alive(pid):
            return "stale", pid
    return "stale", pid  # alive, its command line unreadable every time, and a readable program that cannot be the holder


def state() -> tuple[str, int | None]:
    """("none" | "running" | "stale" | "unreadable", the installer's pid or None). Never guesses: a process that runs
    and cannot be identified counts as the installer."""
    try:
        path = mark_path()
    except RuntimeError:  # Path.home() found no home directory
        return "unreadable", None
    return mark_state(path, lambda arguments: any(Path(argument).name.lower() in INSTALLERS for argument in arguments),
                      lambda program: Path(program).name.lower() in _SHELLS)


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
