"""The mark a change of where storage and models run leaves in the data directory while it runs (#339).

The workspace carries such a change out through a helper process (setup_change.py). That helper stops the knowledge
service on purpose, so nothing may start it again until the change has finished: every automatic start (a panel, the
broker, an MCP server, a command) refuses with a sentence that names the change. The mark is one file of the data
directory holding the helper's pid, read by upgrade_mark.mark_state like the installer's mark. It is per data directory
and not per user because the change rewrites one data directory's config.json and stops one data directory's service;
the services of other data directories keep starting. A mark whose process is gone blocks nothing by itself and
doctor says the change did not finish; but while the copy of config.json from before the change is there (the change
ended, or was killed, with settings nobody checked), nothing starts the service either, with or without the mark,
until a person has checked config.json and removed the copy. The helper itself may start the service (its own pid is not refused), and it holds a lock
file for its whole life, so two changes never run at once.
The English sentences are the source; MESSAGES is their Polish for the terminal catalog.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path

from . import upgrade_mark
from .service_version import Sentence

MARK, LOCK, BEFORE = "setup-in-progress", "setup-change.lock", "setup-before.json"
MODULE = "cairnmind.setup_change"

NOTE_RUNNING = ("A change of where storage and models run is in progress for this data directory (process {pid}), or "
                "that process cannot be identified as the change, so the knowledge service is not started until it "
                "has finished. If no change is running, remove {path}.")
NOTE_STALE = ("A change of where storage and models run did not finish: the process that carried it out (process "
              "{pid}) no longer runs. `cairnmind setup` shows where storage and models run now, and config.json may "
              "hold part of the change. When you have checked it, remove {path}.")
NOTE_STALE_COPY = ("A change of where storage and models run did not finish: the process that carried it out (process "
                   "{pid}) no longer runs. `cairnmind setup` shows where storage and models run now, and config.json "
                   "may hold part of the change; the settings from before it are in {copy}. The knowledge service is "
                   "not started until you have checked config.json and removed {path} and {copy}.")
NOTE_COPY = ("A change of where storage and models run ended with settings nobody checked: config.json may hold part "
             "of the change, and the settings from before it are in {copy}. The knowledge service is not started "
             "until you have checked config.json (`cairnmind setup` shows where storage and models run now) and "
             "removed {copy}.")
NOTE_UNREADABLE = ("The mark {path} of a change of where storage and models run cannot be read, so the knowledge "
                   "service is not started. If no change is running, remove that file.")
BUSY = ("Another change of where storage and models run is in progress for this data directory; wait until it has "
        "finished. Nothing was changed.")
BRIDGE_SETUP = "{reason} Call the tool again when the change has finished."
# What a broker may send as a 503 detail while the change runs (terminal_i18n_messages translates by template).
BROKER_TEMPLATES = (NOTE_RUNNING, NOTE_UNREADABLE, NOTE_STALE_COPY, NOTE_COPY)

MESSAGES = {
    NOTE_RUNNING: ("Trwa zmiana miejsca, w którym działają pamięć i modele, dla tego katalogu danych (proces {pid}) "
                   "albo ten proces nie daje się rozpoznać jako ta zmiana, więc usługa wiedzy nie jest uruchamiana, "
                   "dopóki się nie skończy. Jeśli żadna zmiana nie trwa, usuń {path}."),
    NOTE_STALE: ("Zmiana miejsca, w którym działają pamięć i modele, nie została dokończona: proces, który ją "
                 "wykonywał (proces {pid}), już nie działa. `cairnmind setup` pokazuje, gdzie pamięć i modele "
                 "działają teraz, a config.json może zawierać część zmiany. Gdy go sprawdzisz, usuń {path}."),
    NOTE_STALE_COPY: ("Zmiana miejsca, w którym działają pamięć i modele, nie została dokończona: proces, który ją "
                      "wykonywał (proces {pid}), już nie działa. `cairnmind setup` pokazuje, gdzie pamięć i modele "
                      "działają teraz, a config.json może zawierać część zmiany; ustawienia sprzed niej są w {copy}. "
                      "Usługa wiedzy nie jest uruchamiana, dopóki nie sprawdzisz config.json i nie usuniesz {path} "
                      "oraz {copy}."),
    NOTE_COPY: ("Zmiana miejsca, w którym działają pamięć i modele, skończyła się ustawieniami, których nikt nie "
                "sprawdził: config.json może zawierać część zmiany, a ustawienia sprzed niej są w {copy}. Usługa "
                "wiedzy nie jest uruchamiana, dopóki nie sprawdzisz config.json (`cairnmind setup` pokazuje, gdzie "
                "pamięć i modele działają teraz) i nie usuniesz {copy}."),
    NOTE_UNREADABLE: ("Nie można odczytać znacznika {path} zmiany miejsca, w którym działają pamięć i modele, więc "
                      "usługa wiedzy nie jest uruchamiana. Jeśli żadna zmiana nie trwa, usuń ten plik."),
    BUSY: ("Inna zmiana miejsca, w którym działają pamięć i modele, trwa dla tego katalogu danych; poczekaj, aż się "
           "skończy. Nic nie zmieniono."),
    BRIDGE_SETUP: "{reason} Wywołaj narzędzie ponownie, gdy zmiana się skończy.",
}


class ChangeBusy(ValueError):
    """Another change of this data directory runs (args[0] is a Sentence)."""


def mark_path(data_dir) -> Path:
    return Path(data_dir) / MARK


def state(data_dir) -> tuple[str, int | None]:
    """("none" | "running" | "stale" | "unreadable", the helper's pid or None); a process that runs and cannot be
    identified counts as the helper."""
    return upgrade_mark.mark_state(mark_path(data_dir), lambda arguments: MODULE in arguments,
                                   lambda program: Path(program).name.lower().startswith("python"))


def _sentence(kind: str, pid: int | None, data_dir) -> Sentence | None:
    path = mark_path(data_dir)
    if kind == "running":
        return Sentence(NOTE_RUNNING, pid=pid, path=path)
    if kind == "unreadable":
        return Sentence(NOTE_UNREADABLE, path=path)
    copy = Path(data_dir) / BEFORE
    if kind == "stale":
        if copy.exists():
            return Sentence(NOTE_STALE_COPY, pid=pid, path=path, copy=copy)
        return Sentence(NOTE_STALE, pid=pid, path=path)
    if copy.exists():  # no mark, but a copy: the change ended with settings nobody checked
        return Sentence(NOTE_COPY, copy=copy)
    return None


def note(data_dir) -> Sentence | None:
    """doctor's line for the mark or the kept copy, None without either."""
    return _sentence(*state(data_dir), data_dir)


def unchecked(data_dir) -> Sentence | None:
    """The note for settings nobody checked (a kept copy, no change running now): doctor counts it as missing."""
    kind, pid = state(data_dir)
    return note(data_dir) if kind != "running" and (Path(data_dir) / BEFORE).exists() else None


def refusal(data_dir) -> Sentence | None:
    """Why the service of this data directory must not be started now, None when it may be. The helper that holds
    the mark may start it. A kept copy blocks like a running change: the settings it belongs to were never checked
    (a stale mark without a copy blocks nothing: the change had touched nothing yet or had been put back)."""
    kind, pid = state(data_dir)
    if pid == os.getpid():
        return None
    if kind in upgrade_mark.BLOCKING or (Path(data_dir) / BEFORE).exists():
        return _sentence(kind, pid, data_dir)
    return None


@contextmanager
def held(data_dir):
    """Hold the lock of changes and the mark for this process's life; only this process's own mark is removed."""
    data_dir = Path(data_dir)
    descriptor = os.open(data_dir / LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ChangeBusy(Sentence(BUSY)) from None
        # The lock is held, so a mark that is there belongs to a holder that is gone: it is replaced.
        with upgrade_mark.own_mark(mark_path(data_dir)):
            yield
    finally:
        os.close(descriptor)
