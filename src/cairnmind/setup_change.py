"""The change of where storage and models run, carried out for the workspace (#339).

`cairnmind setup PROFILE` refuses while the knowledge service runs, because the service cannot change itself. The
workspace runs on that service, so it hands the change to a helper process of its own session, started with
`python -I -m cairnmind.setup_change --data-dir DIR` and given the request on standard input (paths and a credential
reference, never a password or a key). Closing the workspace or the terminal does not reach the helper; SIGTERM, SIGHUP
and SIGINT turn into KeyboardInterrupt, so setup's own put-back of config.json still runs. The helper holds the setup
mark (setup_mark.py), so nothing starts the service meanwhile, stops the service, runs setup_profiles.run (the code the
command runs), starts the service again and, when that fails, puts config.json back and starts it on the old settings.
Every step says what it left; a helper killed outright leaves a mark that doctor names.
The English sentences are the source; MESSAGES is their Polish for the terminal catalog.
"""
from __future__ import annotations

import argparse
import dataclasses
import fcntl
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

import httpx

from . import (database_mode, engine_docker, memory_models, project_removal, service_version, setup_mark,
               setup_profiles, store_writer, topology, upgrade_mark)
from .config import SESSION_NAMES, Settings, drop_env, session_value
from .setup_profiles import SetupFailed, SetupRefused, render_one

PROGRESS, LOG = "setup-change.json", "setup-change.log"
STOP_SECONDS = 30
EXIT = {"changed": 0, "failed": 1, "refused": 2, "service_down": 3, "interrupted": 130}
SAY_TEMPLATES = (setup_profiles.BACKING_UP, setup_profiles.ENGINE_STARTING, setup_profiles.RESTORING,
                 engine_docker.CHOSE_DOCKER, engine_docker.CHOSE_NATIVE, engine_docker.KEPT_NATIVE)
_MAX_INPUT, _MAX_TEXT, _MAX_ITEMS = 65536, 4096, 200

STOPPING = "Stopping the knowledge service of this workspace for the change."
STOPPED = "The knowledge service stopped."
STARTING = "Starting the knowledge service again."
CHANGED = ("The change is made and the knowledge service runs again with the new settings; the panels reconnect by "
           "themselves.")
AGAIN_OLD = "The knowledge service runs again with the settings from before the change."
RUNS_LIVE_ANY = ("A session runs in the workspace ({runs}); end it first (in the workspace, or with `cairnmind "
                 "workspace stop --clients`), then change the setup again. Nothing was changed.")
RUNS_UNKNOWN_ANY = "Whether a session runs in the workspace could not be read ({reason}), so nothing was changed."
JOBS_IN_WORK_ANY = ("Jobs are in work now ({jobs}); wait until they finish (`cairnmind jobs list PROJECT --state "
                    "running` shows them), then change the setup again. Nothing was changed.")
JOBS_UNKNOWN = ("Whether a job is in work could not be read from the store ({reason}), so nothing was changed. If that "
                "store is gone, close the workspace, run `cairnmind stop`, then `cairnmind setup local --empty --yes` in "
                "a terminal: it uses the store on this machine without the data of that one.")
JOBS_UNKNOWN_LOCAL = ("Whether a job is in work could not be read from the store ({reason}), so nothing was changed. "
                      "With the workspace closed and the service stopped (`cairnmind stop`), `cairnmind setup PROFILE` "
                      "changes the setup from a terminal.")
READER_HERE = ("This installation only reads its store: {reason} It runs no knowledge service for the workspace to stop "
               "and start, so the setup is changed from a terminal: `cairnmind setup PROFILE`. Nothing was changed.")
WRITER_UNKNOWN = ("Whether this installation writes its store could not be established ({reason}), so nothing was "
                  "changed. If that store is gone, close the workspace, run `cairnmind stop`, then `cairnmind setup "
                  "local --empty --yes` in a terminal: it uses the store on this machine without the data of that one.")
WRITER_UNKNOWN_LOCAL = ("Whether this installation writes its store could not be established ({reason}), so nothing "
                        "was changed. With the workspace closed and the service stopped (`cairnmind stop`), "
                        "`cairnmind setup PROFILE` changes the setup from a terminal.")
READER_TARGET = ("Another installation writes the store at {url}, so after this change this installation would only "
                 "read it and run no knowledge service, which the workspace needs. Nothing was changed. To make this "
                 "installation a reader of that store, close the workspace, run `cairnmind stop`, then `cairnmind "
                 "setup {profile}` with its options and --yes in a terminal.")
STOP_DECLINED = ("The knowledge service answered but did not stop, so nothing was changed. `cairnmind status` shows its "
                 "state.")
STOP_FAILED = "The knowledge service could not be asked to stop ({reason}), so nothing was changed."
STOP_TIMEOUT = ("The knowledge service did not stop within {seconds} seconds of being asked, so nothing was changed. "
                "`cairnmind status` shows its state; once it has stopped, the next command starts it again.")
SERVICE_BACK = ("A knowledge service of this data directory started again before the change could begin (not from the "
                "workspace; `cairnmind serve` in a terminal?), so nothing was changed.")
COPY_FAILED = "The settings could not be copied before the change ({reason}), so nothing was changed."
PLAN_CHANGED = ("What the change would do is no longer what you agreed to (a store, a model server or this "
                "installation's settings answered differently just now), so nothing was changed.{notes} Open the "
                "change again to see the new plan.")
UNEXPECTED = "The change could not be made: {error} config.json was not changed."
START_FAILED_PUT_BACK = ("The change was made, but the knowledge service did not start with the new settings: {reason} "
                         "config.json was put back as it was.{notes}")
STEP_START = "starting the knowledge service"
COPY_KEPT = "The settings from before the change are kept in {copy}."
NEW_STORE_CLAIMED = ("The store at {url} now names this installation as its writer (the start that failed opened it); "
                     "another installation that should write it runs `cairnmind store takeover --yes`.")
SERVICE_DOWN = ("The knowledge service could not be started again: {reason} Nothing in the workspace answers from "
                "memory until it runs. Start it with `{command}` in a terminal, or close the workspace (`cairnmind "
                "workspace stop`) and open it again (`cairnmind`).")
INTERRUPTED = ("The change was interrupted (signal {signal}); the lines above say what was done and what was put "
               "back. The knowledge service was not started by the change; the next command that needs it starts it.")
INTERRUPTED_KEPT = ("The change was interrupted (signal {signal}) where config.json could not be put back. Nothing "
                    "starts the knowledge service until config.json is checked and the copy is removed.")
REQUEST_INVALID = "The request for the change could not be read ({reason}); nothing was changed."
STILL_ON_NEW = "a knowledge service started on the new settings is still running and could not be ended"

MESSAGES = {
    STOPPING: "Zatrzymywanie usługi wiedzy tego środowiska na czas zmiany.",
    STOPPED: "Usługa wiedzy zatrzymana.",
    STARTING: "Ponowne uruchamianie usługi wiedzy.",
    CHANGED: ("Zmiana jest wprowadzona, a usługa wiedzy działa znowu z nowymi ustawieniami; panele łączą się "
              "ponownie same."),
    AGAIN_OLD: "Usługa wiedzy działa znowu z ustawieniami sprzed zmiany.",
    RUNS_LIVE_ANY: ("W środowisku działa sesja ({runs}); zakończ ją najpierw (w środowisku albo poleceniem "
                    "`cairnmind workspace stop --clients`), a potem zmień ustawienia ponownie. Nic nie zostało "
                    "zmienione."),
    RUNS_UNKNOWN_ANY: ("Nie udało się odczytać, czy w środowisku działa sesja ({reason}), więc nic nie zostało "
                       "zmienione."),
    JOBS_IN_WORK_ANY: ("Trwają zadania ({jobs}); poczekaj, aż się skończą (`cairnmind jobs list PROJEKT --state "
                       "running` je pokazuje), a potem zmień ustawienia ponownie. Nic nie zostało zmienione."),
    JOBS_UNKNOWN: ("Nie udało się odczytać z bazy, czy trwa jakieś zadanie ({reason}), więc nic nie zostało zmienione. "
                   "Jeśli ta baza zniknęła, zamknij środowisko, uruchom `cairnmind stop`, a potem w terminalu "
                   "`cairnmind setup local --empty --yes`: użyje bazy na tym komputerze bez danych tamtej."),
    JOBS_UNKNOWN_LOCAL: ("Nie udało się odczytać z bazy, czy trwa jakieś zadanie ({reason}), więc nic nie zostało "
                         "zmienione. Przy zamkniętym środowisku i zatrzymanej usłudze (`cairnmind stop`) `cairnmind "
                         "setup PROFIL` zmienia ustawienia z terminala."),
    READER_HERE: ("Ta instalacja tylko czyta swoją bazę: {reason} Nie uruchamia usługi wiedzy, którą środowisko mogłoby "
                  "zatrzymać i uruchomić, więc ustawienia zmienia się z terminala: `cairnmind setup PROFIL`. Nic nie "
                  "zostało zmienione."),
    WRITER_UNKNOWN: ("Nie udało się ustalić, czy ta instalacja zapisuje swoją bazę ({reason}), więc nic nie zostało "
                     "zmienione. Jeśli ta baza zniknęła, zamknij środowisko, uruchom `cairnmind stop`, a potem w "
                     "terminalu `cairnmind setup local --empty --yes`: użyje bazy na tym komputerze bez danych tamtej."),
    WRITER_UNKNOWN_LOCAL: ("Nie udało się ustalić, czy ta instalacja zapisuje swoją bazę ({reason}), więc nic nie "
                           "zostało zmienione. Przy zamkniętym środowisku i zatrzymanej usłudze (`cairnmind stop`) "
                           "`cairnmind setup PROFIL` zmienia ustawienia z terminala."),
    READER_TARGET: ("Bazę pod adresem {url} zapisuje inna instalacja, więc po tej zmianie ta instalacja tylko by ją "
                    "czytała i nie uruchamiała usługi wiedzy, której środowisko potrzebuje. Nic nie zostało zmienione. "
                    "Aby ta instalacja czytała tę bazę, zamknij środowisko, uruchom `cairnmind stop`, a potem w "
                    "terminalu `cairnmind setup {profile}` z jego opcjami i --yes."),
    STOP_DECLINED: ("Usługa wiedzy odpowiedziała, ale się nie zatrzymała, więc nic nie zostało zmienione. `cairnmind "
                    "status` pokazuje jej stan."),
    STOP_FAILED: "Nie udało się poprosić usługi wiedzy o zatrzymanie ({reason}), więc nic nie zostało zmienione.",
    STOP_TIMEOUT: ("Usługa wiedzy nie zatrzymała się w ciągu {seconds} sekund od wezwania, więc nic nie zostało "
                   "zmienione. `cairnmind status` pokazuje jej stan; gdy się zatrzyma, następne polecenie uruchomi ją "
                   "ponownie."),
    SERVICE_BACK: ("Usługa wiedzy tego katalogu danych uruchomiła się ponownie, zanim zmiana mogła się zacząć (nie ze "
                   "środowiska; `cairnmind serve` w terminalu?), więc nic nie zostało zmienione."),
    COPY_FAILED: "Nie udało się skopiować ustawień przed zmianą ({reason}), więc nic nie zostało zmienione.",
    PLAN_CHANGED: ("To, co zrobiłaby zmiana, nie jest już tym, na co się zgodzono (baza, serwer modeli albo ustawienia "
                   "tej instalacji odpowiedziały teraz inaczej), więc nic nie zostało zmienione.{notes} Otwórz zmianę "
                   "ponownie, aby zobaczyć nowy plan."),
    UNEXPECTED: "Nie udało się wprowadzić zmiany: {error} config.json nie został zmieniony.",
    START_FAILED_PUT_BACK: ("Zmiana została wprowadzona, ale usługa wiedzy nie uruchomiła się z nowymi ustawieniami: "
                            "{reason} config.json przywrócono do poprzedniego stanu.{notes}"),
    STEP_START: "uruchamianiu usługi wiedzy",
    COPY_KEPT: "Ustawienia sprzed zmiany zostają w {copy}.",
    NEW_STORE_CLAIMED: ("Baza pod adresem {url} wskazuje teraz tę instalację jako zapisującą (otworzył ją start, który "
                        "się nie udał); inna instalacja, która ma ją zapisywać, uruchamia `cairnmind store takeover "
                        "--yes`."),
    SERVICE_DOWN: ("Nie udało się ponownie uruchomić usługi wiedzy: {reason} Nic w środowisku nie odpowiada z pamięci, "
                   "dopóki nie działa. Uruchom ją poleceniem `{command}` w terminalu albo zamknij środowisko "
                   "(`cairnmind workspace stop`) i otwórz je ponownie (`cairnmind`)."),
    INTERRUPTED: ("Zmianę przerwano (sygnał {signal}); powyższe wiersze mówią, co zrobiono i co przywrócono. Zmiana "
                  "nie uruchomiła usługi wiedzy; następne polecenie, które jej potrzebuje, ją uruchomi."),
    INTERRUPTED_KEPT: ("Zmianę przerwano (sygnał {signal}) w miejscu, w którym nie dało się przywrócić config.json. "
                       "Nic nie uruchamia usługi wiedzy, dopóki config.json nie zostanie sprawdzony, a kopia usunięta."),
    REQUEST_INVALID: "Nie udało się odczytać prośby o zmianę ({reason}); nic nie zostało zmienione.",
    STILL_ON_NEW: "usługa wiedzy uruchomiona z nowymi ustawieniami nadal działa i nie dała się zakończyć",
}

_signal = [None]  # the signal that interrupted the helper, for INTERRUPTED


# --- What refuses the change ----------------------------------------------------------------------------------------

def _item(sentence) -> tuple[str, dict]:
    return sentence.template, sentence.values  # a service_version.Sentence


def _names(settings) -> dict:
    """The names of the projects of the store, for the sentences that name a run or a job; empty when unreadable."""
    from .store import Store
    try:
        store = Store.open_readonly(settings)
    except (ValueError, OSError, RuntimeError):
        return {}
    try:
        return {row["id"]: row.get("name") or row["id"] for row in store.query("SELECT id, name FROM projects")}
    except (ValueError, OSError, RuntimeError, KeyError):
        return {}
    finally:
        store.client.close()


def _unknown(settings, remote: str, local: str, reason: str) -> tuple[str, dict]:
    """`setup local --empty` is the way out of a store this installation only connects to; on a store of this machine it
    refuses, so there the setup is changed with `cairnmind setup PROFILE`."""
    return (remote if database_mode.connect_only(settings) else local), {"reason": reason}


def _jobs(settings) -> tuple[str, dict] | None:
    from .store import Store
    try:
        store = Store.open_readonly(settings)
    except (ValueError, OSError, RuntimeError) as exc:
        return _unknown(settings, JOBS_UNKNOWN, JOBS_UNKNOWN_LOCAL, str(exc)[:200])
    try:
        jobs = store.query("SELECT project_id, kind FROM jobs WHERE state='running' AND lease_until>=:now",
                           {"now": time.time()})
        names = {row["id"]: row.get("name") or row["id"] for row in store.query("SELECT id, name FROM projects")}
    except (ValueError, OSError, RuntimeError, KeyError) as exc:
        return _unknown(settings, JOBS_UNKNOWN, JOBS_UNKNOWN_LOCAL, str(exc)[:200])
    finally:
        store.client.close()
    if not jobs:
        return None
    kinds: dict = {}
    for job in jobs:
        kinds.setdefault(names.get(job.get("project_id"), job.get("project_id")), set()).add(str(job.get("kind")))
    return JOBS_IN_WORK_ANY, {"jobs": "; ".join(f"{project} ({', '.join(sorted(found))})"
                                                for project, found in sorted(kinds.items(), key=str))}


def refusal(settings) -> tuple[str, dict] | None:
    """Why the workspace may not change the setup now, as an item (template, values); None when it may. First match
    wins, and nothing is ended to make room."""
    if session_value("SESSION_ID"):
        return setup_profiles.IN_SESSION, {}
    problem = setup_profiles.environment_problem(settings)
    if problem:
        return problem.template, problem.values
    other = setup_mark.refusal(settings.data_dir)
    if other:
        return _item(other)
    kind, pid = upgrade_mark.state()
    if kind == "running":
        return upgrade_mark.NOTE_RUNNING, {"pid": pid}
    if kind == "unreadable":
        return upgrade_mark.NOTE_UNREADABLE, {}
    version = service_version.refusal(settings)
    if version:
        return _item(version)
    found = store_writer.inspect(settings)
    if found.get("role") in ("reader", "lost"):
        return READER_HERE, {"reason": found.get("reason", "")}
    if found.get("role") != "writer" or found.get("problem"):  # fail closed: the writer cannot be established
        return _unknown(settings, WRITER_UNKNOWN, WRITER_UNKNOWN_LOCAL,
                        found.get("problem") or found.get("reason") or f"role: {found.get('role')}")
    return _runs(settings) or _jobs(settings)


def _runs(settings) -> tuple[str, dict] | None:
    """A run of any project that is not known to have ended, or a registry that cannot be read."""
    try:
        live = project_removal.live_runs(settings)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        return RUNS_UNKNOWN_ANY, {"reason": str(exc)[:200]}
    if not live:
        return None
    names = _names(settings)
    return RUNS_LIVE_ANY, {"runs": ", ".join(
        f"{names.get(run.get('project_id'), run.get('project_id'))}: {run.get('client')} "
        f"{run.get('lifecycle') or run.get('state')}" for run in live)}


def target_refusal(result: dict, request) -> tuple[str, dict] | None:
    """After the dry run: a store another installation writes would make this installation a reader, and a reader
    runs no knowledge service, which the workspace needs."""
    for template, values in result["writer"]:
        if template == setup_profiles.WRITER_OTHER:
            return READER_TARGET, {"url": values["url"], "profile": request.profile}
    # setup itself refuses this run with --yes and creates nothing: stop no service for it.
    return next((item for item in result["tests"] if item[0] == setup_profiles.NO_RIGHTS), None)


# --- The plan that was agreed -----------------------------------------------------------------------------------------

# What the dry run says --admin-password-file will create, as the sentence the change says when it did (NO_RIGHTS: nothing).
# The user and the database are two entries: a database that appeared since the consent is not the same plan (#390).
_CREATES = {setup_profiles.CREATE_BOTH: [setup_profiles.USER_CREATED, setup_profiles.DATABASE_CREATED],
            setup_profiles.CREATE_USER: [setup_profiles.USER_CREATED],
            setup_profiles.CREATE_DB: [setup_profiles.DATABASE_CREATED], setup_profiles.NO_RIGHTS: [setup_profiles.NO_RIGHTS]}


def agreed_plan(result: dict) -> dict:
    """What a person agrees to: the changes, what is to rebuild and what is left behind, in English, and which sentences
    of the server's side came with them: the writer's, then what the administrator's file will create (or that the user
    has no rights). The other tests are not part of it: their wording changes when a user is created."""
    return {"plan": [render_one(item) for key in ("changes", "rebuild", "left_behind") for item in result[key]],
            "writer": [item[0] for item in result["writer"]]
            + [made for item in result["tests"] for made in _CREATES.get(item[0], ())]}


def _writer_same(agreed: list, now: list) -> bool:
    """The writer's sentences of the dry run and of the change are the same, or the writer could not be read at the dry
    run because the user did not sign in yet; and the change created exactly what the dry run said it would."""
    made = (*setup_profiles.CREATED, setup_profiles.NO_RIGHTS)
    if [item for item in agreed if item in made] != [item for item in now if item in made]:
        return False
    agreed, now = [item for item in agreed if item not in made], [item for item in now if item not in made]
    if setup_profiles.WRITER_OTHER in now:
        return False
    if agreed == now:
        return True
    # The user did not exist at the dry run (--admin-password-file creates it with the change): not read then. A database
    # the change just made has no schema yet, so it is not read after it either: the store decides at the first open.
    return bool(agreed and now and agreed[0] in (setup_profiles.WRITER_NOT_READ, setup_profiles.WRITER_AFTER_USER)
                and now[0] in (setup_profiles.WRITER_KEEP, setup_profiles.WRITER_NEW, setup_profiles.WRITER_NOT_READ)
                and agreed[1:] == now[1:])


def _made(items: list) -> list:
    return [item for item in items if item in (*setup_profiles.CREATED, setup_profiles.NO_RIGHTS)]


def same_creation(expected: dict) -> Callable[[tuple], None]:
    """The before_create hook of setup_profiles.run: refuses before the administrator creates anything when the server
    would now create something other than the user and database agreed to; the notes name what is there now (a user
    that signs in now is TEST_STORAGE_OK, which creates nothing)."""
    def check(found: tuple) -> None:
        if _made(expected["writer"]) != _CREATES.get(found[0], []):
            raise SetupRefused(PLAN_CHANGED, notes=[found])
    return check


def same_plan(expected: dict) -> Callable[[dict], None]:
    """The before_change hook of setup_profiles.run: refuses, with config.json untouched, when the plan is not the one
    agreed to."""
    def check(result: dict) -> None:
        got = agreed_plan(result)  # the server's side as the change found it: what it created stands in "done"
        for item in result["done"]:
            if item[0] in setup_profiles.CREATED:
                got["writer"].append(item[0])
                if item[0] == setup_profiles.USER_CREATED and item[1].get("database_created"):
                    got["writer"].append(setup_profiles.DATABASE_CREATED)
        if got["plan"] != expected["plan"] or not _writer_same(expected["writer"], got["writer"]):
            raise SetupRefused(PLAN_CHANGED, notes=[item for item in result["done"]
                                                    if item[0] in setup_profiles.CREATED])
    return check


def said_in(text: str, language: str) -> str:
    """A progress sentence setup said in English (a template of SAY_TEMPLATES, filled), in ``language``."""
    if language == "en":
        return text
    from .terminal_i18n import tr
    from .terminal_i18n_messages import from_templates
    return from_templates(text, lambda template: tr(template, language), SAY_TEMPLATES) or text


# --- The helper process ----------------------------------------------------------------------------------------------

def begin(settings, request, expected: dict, language: str) -> subprocess.Popen:
    """Start the helper in a session of its own, with the request on standard input. Nothing secret is in it."""
    payload = json.dumps({"request": dataclasses.asdict(request), "expected": expected, "language": language}).encode()
    env = dict(os.environ)
    drop_env(env, *SESSION_NAMES)  # a helper is no child actor of a coding session
    for name in ("TMUX", "TMUX_PANE"):  # the workspace's key is found from the data directory, not from the pane
        env.pop(name, None)
    log = os.open(settings.data_dir / LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        process = subprocess.Popen([sys.executable, "-I", "-m", "cairnmind.setup_change", "--data-dir",
                                    str(settings.data_dir)], cwd=settings.data_dir, stdin=subprocess.PIPE,
                                   stdout=log, stderr=log, start_new_session=True, env=env)
    finally:
        os.close(log)
    try:
        process.stdin.write(payload)
    finally:
        process.stdin.close()
    return process


def _write_progress(data_dir: Path, state: str, language: str, lines: list[str], other: list[str]) -> None:
    """``other`` is the same lines in the other language, so a workspace opened later in that language can show them."""
    memory_models._write_json(data_dir / PROGRESS, {"pid": os.getpid(), "state": state, "language": language,
                                                    "lines": lines[-_MAX_ITEMS:], "other": other[-_MAX_ITEMS:]})


def read_progress(data_dir: Path, pid: int) -> dict | None:
    """What the helper with process id ``pid`` has reported, None when nothing from that process can be read."""
    try:
        with open(Path(data_dir) / PROGRESS, "rb") as handle:
            found = json.loads(handle.read(1 << 20))
    except (OSError, ValueError):
        return None
    if (isinstance(found, dict) and found.get("pid") == pid and found.get("state") in ("running", *EXIT)
            and found.get("language") in ("en", "pl") and isinstance(found.get("lines"), list)
            and all(isinstance(line, str) for line in found["lines"])):
        return found
    return None


def _unseen_file(data_dir: Path) -> dict | None:
    """The progress file when it holds the end of a change (any final state) that no person has been shown."""
    try:
        with open(Path(data_dir) / PROGRESS, "rb") as handle:
            found = json.loads(handle.read(1 << 20))
    except (OSError, ValueError):
        return None
    if (isinstance(found, dict) and type(found.get("pid")) is int and found.get("state") in EXIT
            and found.get("shown") is not True):
        return found
    return None


def unseen_outcome(data_dir: Path, language: str) -> dict | None:
    """The finished outcome of the last change, in ``language``, when its helper no longer runs and no person has seen
    it ({"pid", "state", "lines"}); None otherwise, also for a file that cannot be read (#339). A change still running
    (its mark) shows nothing here. The workspace shows it once at its next opening, and the progress dialog of the
    session that started the change counts as having shown it (mark_shown)."""
    found = _unseen_file(data_dir)
    if found is None or setup_mark.state(data_dir)[0] in ("running", "unreadable"):
        return None
    try:  # the helper writes this file last: a newer config.json is a later change (`cairnmind setup`), now out of date
        if (Path(data_dir) / "config.json").stat().st_mtime > (Path(data_dir) / PROGRESS).stat().st_mtime:
            return None
    except OSError:
        pass
    lines = found.get("lines") if found.get("language") == language else found.get("other")
    if not isinstance(lines, list) or not lines or not all(isinstance(line, str) for line in lines):
        return None
    return {"pid": found["pid"], "state": found["state"], "lines": lines}


def mark_shown(data_dir: Path, pid: int) -> None:
    """Record, in the progress file itself (replaced atomically; the next change writes a file without it), that the
    outcome of the helper ``pid`` was shown. Nothing is recorded in the store. Nothing is recorded when the file is
    another change's or cannot be read: the outcome may then be shown again, never lost."""
    found = _unseen_file(data_dir)
    if found is not None and found["pid"] == pid:
        try:
            memory_models._write_json(Path(data_dir) / PROGRESS, {**found, "shown": True})
        except OSError:
            pass


class _Report:
    def __init__(self, report, language):
        self.report, self.language = report, language
        self.texts = {"en": [], "pl": []}  # every line in both languages: the workspace may be opened in the other one

    @property
    def lines(self) -> list[str]:
        return self.texts[self.language]

    def _report(self, state: str) -> None:
        self.report(state, self.lines, self.texts["pl" if self.language == "en" else "en"])

    def add(self, *items) -> None:
        self.extend(lambda language: [render_one(item, language) for item in items])

    def extend(self, make) -> None:
        """Add the lines ``make(language)`` gives, in each language."""
        for language in self.texts:
            self.texts[language].extend(make(language))
        self._report("running")

    def say(self, text: str) -> None:
        self.extend(lambda language: [said_in(text, language)])

    def finish(self, state: str) -> str:
        self._report(state)
        return state


def _lock_free(settings) -> bool:
    with (settings.data_dir / "service.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True


def _stop(settings) -> tuple[str, dict] | None:
    """Ask the service to stop and wait until it and its lock are gone; the item that says why not, else None."""
    from . import cli
    asked = False
    deadline = time.monotonic() + STOP_SECONDS
    while True:
        alive = cli.service_alive(settings)
        if alive and not asked:  # also one that began to answer while this waited for its lock
            try:
                with httpx.Client(trust_env=False, timeout=5) as client:
                    response = client.post(settings.url + "/shutdown",
                                           headers={"Authorization": f"Bearer {settings.token}"})
                    response.raise_for_status()
                    stopping = response.json().get("stopping") is True
            except (httpx.HTTPError, ValueError, AttributeError) as exc:
                return STOP_FAILED, {"reason": str(exc)[:200]}
            if not stopping:
                return STOP_DECLINED, {}
            asked = True
        if not alive and _lock_free(settings):
            return None
        if time.monotonic() >= deadline:
            return STOP_TIMEOUT, {"seconds": STOP_SECONDS}
        time.sleep(0.2)


def _start(settings) -> str | None:
    """Start the service as the workspace does (background inference deferred); the reason when it did not."""
    from . import cli
    try:
        cli.ensure_service(settings, background_inference=False)
    except (ValueError, OSError) as exc:  # ServiceDidNotStart is a ValueError
        return str(exc)
    return None


def _copy(path: Path, data: bytes) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".setup-before-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _left(result: dict) -> list:
    """What a change that is put back leaves behind, from what it did."""
    notes = []
    for template, values in result["done"]:
        if template in setup_profiles.CREATED:
            notes.append((template, values))
        elif template == setup_profiles.BACKUP_WRITTEN:
            notes.append((setup_profiles.KEPT_BACKUP, {"path": values["path"]}))
        elif template == setup_profiles.ENGINE_STARTED:
            notes.append((setup_profiles.ENGINE_LEFT, {}))
        elif template == setup_profiles.RESTORED:
            notes.append((setup_profiles.RESTORE_DONE, values))
    return notes


def _apply(settings, request, expected, out) -> tuple[str, object]:
    """setup_profiles.run with the service stopped: ("changed", result), ("refused", item), ("failed", item),
    ("not_put_back", item) or ("interrupted", None)."""
    with (settings.data_dir / "service.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "refused", (SERVICE_BACK, {})
        try:
            return "changed", setup_profiles.run(settings, request, yes=True, say=out.say,
                                                 before_change=same_plan(expected),
                                                 before_create=same_creation(expected))
        except SetupRefused as exc:
            return "refused", (exc.template, exc.values)
        except SetupFailed as exc:
            return ("not_put_back" if exc.template == setup_profiles.PUT_BACK_FAILED else "failed"), (
                exc.template, exc.values)
        except KeyboardInterrupt:
            return "interrupted", None  # setup put config.json back before it let the signal go on
        except Exception as exc:  # noqa: BLE001 - run puts config.json back itself once it has changed it
            return "refused", (UNEXPECTED, {"error": str(exc) or type(exc).__name__})


def _data_dir_arguments(data_dir: Path) -> list[str]:
    from .cli import _data_dir_arguments as arguments
    return arguments(data_dir)


def _restart(data_dir: Path, out, state: str) -> str:
    """The service on the settings config.json holds now, which are the old ones."""
    reason = _start(Settings.load(data_dir))
    if reason:
        command = shlex.join(["cairnmind", *_data_dir_arguments(data_dir), "serve"])
        out.add((SERVICE_DOWN, {"reason": reason, "command": command}))
        return out.finish("service_down")
    out.add((AGAIN_OLD, {}))
    return out.finish(state)


def _signal_number() -> int:
    return _signal[0] or signal.SIGINT.value


def _new_settings_start(data_dir: Path, path: Path, snapshot: bytes, result: dict, out) -> tuple[str, bool]:
    """The service on the new settings. When it does not start, or the helper is interrupted meanwhile, config.json is
    put back and (unless interrupted) the service is started on the old settings. (final state, copy to keep)."""
    out.add((STARTING, {}))
    new, interrupted = None, False
    try:
        new = Settings.load(data_dir)
        reason = _start(new)
    except KeyboardInterrupt:  # ensure_service has ended the process it spawned; below waits until it is gone
        reason, interrupted = f"signal {_signal_number()}", True
    except Exception as exc:  # noqa: BLE001 - settings that cannot be loaded are a start that failed
        reason = str(exc) or type(exc).__name__
    if reason is None:
        shown = [item for key in ("done", "rebuild") for item in result[key]]
        out.texts["en"].extend([*(render_one(item, "en") for item in shown), "", *topology.lines(result["topology"], "en")])
        out.texts["pl"].extend([*(render_one(item, "pl") for item in shown), "", *topology.lines(result["topology"], "pl")])
        out.add((CHANGED, {}))
        return out.finish("changed"), False
    notes = _left(result)
    if (new is not None and any(item[0] == setup_profiles.WRITER_NEW for item in result["writer"])
            and store_writer.inspect(new).get("role") == "writer"):
        notes.append((NEW_STORE_CLAIMED, {"url": database_mode.address(new)}))  # the failed start opened a new store
    cannot = None
    if new is not None:  # a start that failed late or timed out may have left its process: it ends before the put-back
        still = _stop(new)  # config.json is not put back under a service that runs on the new settings
        cannot = (STILL_ON_NEW, {}) if still else None
    if cannot is None:
        try:
            setup_profiles.put_back(path, snapshot, result["applied_state"], notes)
        except OSError as failure:
            cannot = str(failure)
    if cannot is not None:
        out.add((setup_profiles.PUT_BACK_FAILED, {"step": (STEP_START, {}), "error": reason, "reason": cannot,
                                                  "notes": notes}), (COPY_KEPT, {"copy": data_dir / setup_mark.BEFORE}))
        return out.finish("service_down"), True
    out.add((START_FAILED_PUT_BACK, {"reason": reason, "notes": notes}))
    (data_dir / setup_mark.BEFORE).unlink(missing_ok=True)  # config.json is the old one again: no copy may block the start
    if interrupted:
        out.add((INTERRUPTED, {"signal": _signal_number()}))
        return out.finish("interrupted"), False
    return _restart(data_dir, out, "failed"), False


def _change(data_dir: Path, request, expected: dict, out) -> str:
    settings = Settings.load(data_dir)
    item = refusal(settings)
    if item:
        out.add(item)
        return out.finish("refused")
    out.add((STOPPING, {}))
    item = _stop(settings)
    if item:
        out.add(item)
        return _restart(data_dir, out, "refused")  # whatever is left of the service is brought back
    out.add((STOPPED, {}))
    item = _runs(settings)  # a run that began in the window between the check and the stop
    if item:
        out.add(item)
        return _restart(data_dir, out, "refused")
    path, copy = data_dir / "config.json", data_dir / setup_mark.BEFORE
    try:
        snapshot = path.read_bytes()
        _copy(copy, snapshot)
    except OSError as exc:
        out.add((COPY_FAILED, {"reason": str(exc)}))
        return _restart(data_dir, out, "refused")
    keep = False
    try:
        kind, payload = _apply(settings, request, expected, out)  # anything that escapes keeps the copy (below)
        if kind == "changed":
            state, keep = _new_settings_start(data_dir, path, snapshot, payload, out)
            return state
        if kind == "interrupted":
            out.add((INTERRUPTED, {"signal": _signal_number()}))
            return out.finish("interrupted")
        out.add(payload)
        if kind == "not_put_back":  # nothing is started on a configuration nobody has checked
            keep = True
            out.add((COPY_KEPT, {"copy": copy}))
            return out.finish("service_down")
        copy.unlink(missing_ok=True)  # refused or failed: setup put config.json back, so no copy may block the start
        return _restart(data_dir, out, kind)
    except BaseException:
        keep = True  # an end nobody planned for: config.json is unchecked, and the copy blocks every start until it is
        raise
    finally:
        if not keep:
            copy.unlink(missing_ok=True)


def carry_out(data_dir: Path, request, expected: dict, language: str, report) -> str:
    """The whole change, with the service stopped for it; ``report(state, lines)`` says how far it is. Returns the
    final state: changed, failed, refused, service_down or interrupted."""
    data_dir = Path(data_dir)
    out = _Report(report, language)
    try:
        with setup_mark.held(data_dir):
            return _change(data_dir, request, expected, out)
    except setup_mark.ChangeBusy:
        out.add((setup_mark.BUSY, {}))
        return out.finish("refused")
    except KeyboardInterrupt:  # one that came where nothing could be put back: the copy, if there, is named
        copy = data_dir / setup_mark.BEFORE
        if copy.exists():
            out.add((INTERRUPTED_KEPT, {"signal": _signal_number()}), (COPY_KEPT, {"copy": copy}))
        else:
            out.add((INTERRUPTED, {"signal": _signal_number()}))
        return out.finish("interrupted")


# --- The entry of the helper ---------------------------------------------------------------------------------------

def _text(value, name: str, *, allow_none: bool = True) -> None:
    if value is None and allow_none:
        return
    if not isinstance(value, str) or len(value) > _MAX_TEXT or "\0" in value:
        raise ValueError(f"{name} is not a text of at most {_MAX_TEXT} characters")


def _parsed(raw: bytes) -> tuple[setup_profiles.Request, dict, str]:
    """The request, the agreed plan and the language of a payload of ``begin``; ValueError names what is wrong."""
    try:
        payload = json.loads(raw)
    except ValueError:
        raise ValueError("not JSON") from None
    if not isinstance(payload, dict) or set(payload) != {"request", "expected", "language"}:
        raise ValueError("unexpected fields")
    if payload["language"] not in ("en", "pl"):
        raise ValueError("language")
    fields, expected = payload["request"], payload["expected"]
    allowed = {field.name for field in dataclasses.fields(setup_profiles.Request)}
    if not isinstance(fields, dict) or not set(fields) <= allowed or fields.get("profile") not in setup_profiles.PROFILES:
        raise ValueError("request")
    for name, value in fields.items():
        if name in ("allow_remote", "empty"):
            if not isinstance(value, bool):
                raise ValueError(name)
        else:
            _text(value, name, allow_none=name != "profile")
    if (not isinstance(expected, dict) or set(expected) != {"plan", "writer"}
            or any(not isinstance(expected[key], list) or len(expected[key]) > _MAX_ITEMS for key in expected)):
        raise ValueError("expected")
    for key in expected:
        for text in expected[key]:
            _text(text, "expected", allow_none=False)
    request = setup_profiles.Request(**fields)
    problem = setup_profiles.option_problem(request)  # the rules of the command line, which run does not apply itself
    if problem:
        raise ValueError(render_one((problem.template, problem.values)))
    return request, expected, payload["language"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cairnmind.setup_change")
    parser.add_argument("--data-dir", required=True)
    data_dir = Path(parser.parse_args(argv).data_dir)
    if not (data_dir / "config.json").is_file():
        print(f"no Cairnmind configuration in {data_dir}", file=sys.stderr)
        return 2

    def report(state: str, lines: list[str], other: list[str]) -> None:
        _write_progress(data_dir, state, language[0], lines, other)

    language = ["en"]

    def interrupted(number, frame):
        _signal[0] = number
        raise KeyboardInterrupt

    for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(number, interrupted)
    raw = sys.stdin.buffer.read(_MAX_INPUT + 1)
    try:
        if len(raw) > _MAX_INPUT:
            raise ValueError("too long")
        request, expected, language[0] = _parsed(raw)
    except ValueError as exc:
        out = _Report(report, language[0])
        out.add((REQUEST_INVALID, {"reason": str(exc)}))
        return EXIT[out.finish("refused")]
    return EXIT[carry_out(data_dir, request, expected, language[0], report)]


if __name__ == "__main__":
    sys.exit(main())
