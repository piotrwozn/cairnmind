"""Removing a project from the registry, keeping its memory (#327).

The project row stays, marked with `removed_at`, `removed_name`, `removed_root` and `removed_roots`; its `name` and
`root` are taken off it, so the name and the folder can be registered again. A backup refuses a row that names a
project the registry does not know, so the row must stay while any of its data does. Nothing else is deleted, and
nothing in the project's folders is touched. The English sentences are the source; MESSAGES is their Polish for the
terminal catalog.
"""
from __future__ import annotations

import shlex
import subprocess
import time
from pathlib import Path

from .first_start import UNKNOWN_PROJECT_NEXT
from .sql import clean_row, insert_document, one_row, update_document, utc_now
from .terminal_safe import safe_text
from uuid import uuid4

ENDED = frozenset({"exited", "failed", "interrupted"})
INSTALLED_FILES = ("AGENTS.md", "CLAUDE.md", ".mcp.json", ".codex/config.toml", ".codex/hooks.json",
                   ".claude/settings.local.json")
REMOVED_FIELDS = ("removed_at", "removed_name", "removed_root", "removed_roots")

IN_SESSION = ("A coding session cannot remove a project from the registry; only a person can, outside the session. "
              "Nothing was changed.")
RUNS_LIVE = ("{project} has a session running in the workspace ({runs}); end it first (in the workspace, or with "
             "cairnmind workspace stop --clients), then run this again. Nothing was changed.")
RUNS_UNKNOWN = ("Whether a session of {project} runs in the workspace could not be read ({reason}), so nothing was "
                "changed.")
JOBS_IN_WORK = ("Jobs of {project} are in work now ({kinds}); wait until they finish (cairnmind jobs list {project} "
                "--state running shows them), then run this again. Nothing was changed.")
MAIN_ROOT = ("{root} is the main folder of {project}; cairnmind project remove {project} removes the whole project. "
             "Nothing was changed.")
NOT_A_ROOT = "{root} is not a folder of {project}; its folders are: {roots}. Nothing was changed."
ROOT_SCHEDULED = ("The scheduled snapshot of {project} writes into {root}; cairnmind memory auto {project} --disable "
                  "stops it first. Nothing was changed.")
DRY_RUN = "Nothing was changed; this is what cairnmind project remove {project} --yes would do."
WOULD_STOP = ("It would stop: {project} leaves cairnmind project list; its folders are no longer indexed ({roots}); "
              "no new session or command can name it; its conversations are no longer archived.")
REMOVED = ("Removed {project} from the registry: it is no longer listed or indexed, and no new session or command "
           "can name it. Nothing was deleted.")
QUEUED = "Queued jobs of {project} ({count}) wait and run once it is back."
SCHEDULE_PAUSED = "Its scheduled snapshot pauses until it is back."
KEPT_STORE = ("Kept in the store: {records} records, {plans} plans, {sources} sources, {sessions} sessions and "
              "{check_runs} check runs, with their revisions, acknowledgements, code graph and search index; backups "
              "are not changed.")
KEPT_ANSWERS = "Kept: the answer log {path}."
KEPT_FOLDER = ("Nothing in the folder is changed; these stay as they are: {paths}. An assistant started there is told "
               "that {project} was removed when it calls Cairnmind, and the events its hooks record wait in the data "
               "directory until the project is back.")
KEPT_FOLDER_EMPTY = "Nothing in the folder is changed."
NESTED = ("{root} lies inside the folder {parent} of {other}; once this project is removed, {other} indexes the "
          ".memory folders in {root} as ordinary files, and an assistant or hook started in {root} reaches {other}, "
          "not the removed project.")
BRING_BACK = "cairnmind project add {project} {root} brings back the same project with its memory."
EXTRA_ROOTS = ("Its other folders are not added back by that; cairnmind project root {project} FOLDER adds each "
               "again: {roots}.")
DELETE_STEP = ("Deleting its memory is a separate step: first cairnmind backup DIR/, then cairnmind forget {id} "
               "--delete-project.")
NEXT_YES = "Next: cairnmind project remove {project} --yes removes it."
NEXT_LIST = "Next: cairnmind project list --removed lists the removed projects."
ROOT_DRY_RUN = ("Nothing was changed; this is what cairnmind project remove {project} --root {root} --yes would do.")
ROOT_REMOVED = "Removed the folder {root} from {project}; nothing was deleted."
ROOT_EFFECT = ("At the next indexing the files from it stop being current: their snapshots stay as the evidence "
               "records cite, and their search passages, vectors and code graph go, as for deleted files.")
ROOT_KEPT = "Everything else of {project} stays, and nothing in {root} is changed."
NEXT_ROOT_YES = "Next: cairnmind project remove {project} --root {root} --yes removes the folder."
ROOT_NEXT = "Next: cairnmind project root {project} {root} adds it again."
PROJECT_REMOVED = ("The project {name} was removed from the registry on {when}; its memory is kept. cairnmind project "
                   "add {name} {root} brings it back with its memory.")
SEVERAL_REMOVED = ("Several removed projects have the name {name} and the folder {root} (ids {ids}); cairnmind "
                   "project list --removed lists them. Nothing was changed.")
ROOT_OF_ANOTHER = ("{root} is now a folder of the project {other}; cairnmind project remove {other} --root {root} "
                   "frees it. Nothing was changed.")
RESTORED = ("Brought back {project} in {root} with its memory, as it was when removed on {when}; files indexed: "
            "{files}.")
RESTORED_ROOTS = ("Its other folders were not added back; cairnmind project root {project} FOLDER adds each again: "
                  "{roots}.")
NOT_BROUGHT_BACK = ("The project removed on {when} as {name} in {root} keeps its memory and was not brought back: "
                    "only cairnmind project add with that same name and folder brings it back.")
NO_WAY_BACK = ("cairnmind project add cannot bring {name} back: its folder {root} lies inside the data directory, which "
               "Cairnmind never indexes. Its memory stays in the store; deleting it takes cairnmind backup DIR/ and "
               "then cairnmind forget {id} --delete-project.")
REMOVED_STUCK = ("The project {name} was removed from the registry on {when}; its memory is kept, but cairnmind project "
                 "add cannot bring it back: its folder {root} lies inside the data directory, which Cairnmind never "
                 "indexes. Deleting its memory takes cairnmind backup DIR/ and then cairnmind forget {id} "
                 "--delete-project.")
IMPORT_STUCK = ("This memory package belongs to the project {name}, which was removed from the registry on {when} and "
                "cannot be brought back, because its folder {root} lies inside the data directory. Nothing was "
                "imported.")
FORGET_BY_ID = ("The project {name} was removed from the registry, so forget takes its id: first cairnmind backup "
                "DIR/, then cairnmind forget {id} --delete-project. Nothing was deleted.")
RESTORED_WORKSPACE = ("Brought back {project} in {root} with its memory, as it was when removed on {when}; it is not a "
                      "new project.")
NEW_REFUSED = ("The project {name} in {root} was removed on {when} and keeps its memory; a new project with that name "
               "and folder cannot be told apart from it. Give the new project another name, or cairnmind project add "
               "{name} {root} brings the old one back. Nothing was changed.")
REMOVED_HEADING = "Removed projects, whose memory is kept:"
REMOVED_ROW = "  - {name}: {root} (removed {when}; id {id})"
REMOVED_NONE = "No removed project keeps memory in this store."
REMOVED_NEXT = ("Next: cairnmind project add NAME FOLDER, with the name and folder shown, brings one back with its "
                "memory; deleting its memory takes cairnmind backup DIR/ and then cairnmind forget ID "
                "--delete-project.")
REMOVED_HINT = "Removed projects whose memory is kept: {count}; cairnmind project list --removed lists them."
IMPORT_REMOVED = ("This memory package belongs to the project {name}, which was removed from the registry on {when}. "
                  "cairnmind project add {name} {root} brings it back; then import again. Nothing was imported.")

MESSAGES = {
    IN_SESSION: ("Sesja programistyczna nie może usunąć projektu z rejestru; może to zrobić tylko człowiek, poza "
                 "sesją. Nic nie zostało zmienione."),
    RUNS_LIVE: ("{project} ma sesję działającą w środowisku terminalowym ({runs}); najpierw ją zakończ (w środowisku "
                "albo poleceniem cairnmind workspace stop --clients), a potem uruchom to ponownie. Nic nie zostało "
                "zmienione."),
    RUNS_UNKNOWN: ("Nie udało się odczytać, czy w środowisku terminalowym działa sesja {project} ({reason}), więc nic "
                   "nie zostało zmienione."),
    JOBS_IN_WORK: ("Zadania w kolejce dla {project} są teraz w toku ({kinds}); poczekaj, aż się zakończą (pokazuje je "
                   "cairnmind jobs list {project} --state running), a potem uruchom to ponownie. Nic nie zostało "
                   "zmienione."),
    MAIN_ROOT: ("{root} jest głównym folderem {project}; cairnmind project remove {project} usuwa cały projekt. Nic "
                "nie zostało zmienione."),
    NOT_A_ROOT: "{root} nie jest folderem {project}; jego foldery to: {roots}. Nic nie zostało zmienione.",
    ROOT_SCHEDULED: ("Zaplanowana migawka {project} zapisuje do {root}; najpierw zatrzymaj ją poleceniem cairnmind "
                     "memory auto {project} --disable. Nic nie zostało zmienione."),
    DRY_RUN: "Nic nie zostało zmienione; oto co zrobiłoby cairnmind project remove {project} --yes.",
    WOULD_STOP: ("Przestałoby działać: {project} znika z cairnmind project list; jego foldery nie są już "
                 "indeksowane ({roots}); żadna nowa sesja ani polecenie nie może go wskazać; jego rozmowy nie są już "
                 "archiwizowane."),
    REMOVED: ("Usunięto {project} z rejestru: nie jest już wymieniany ani indeksowany i żadna nowa sesja ani "
              "polecenie nie może go wskazać. Niczego nie skasowano."),
    QUEUED: "Zadania w kolejce dla {project} ({count}) czekają i ruszą, gdy projekt wróci.",
    SCHEDULE_PAUSED: "Jego zaplanowana migawka wstrzymuje się do czasu jego powrotu.",
    KEPT_STORE: ("Zostaje w magazynie: rekordy ({records}), plany ({plans}), źródła ({sources}), sesje ({sessions}) i "
                 "przebiegi kontroli ({check_runs}), wraz z ich wersjami, potwierdzeniami, grafem kodu i indeksem "
                 "wyszukiwania; kopie zapasowe nie są zmieniane."),
    KEPT_ANSWERS: "Zostaje: dziennik odpowiedzi {path}.",
    KEPT_FOLDER: ("Nic w folderze nie jest zmieniane; te pliki zostają bez zmian: {paths}. Asystent uruchomiony tam "
                  "usłyszy przy wywołaniu Cairnmind, że {project} został usunięty, a zdarzenia zapisywane przez jego "
                  "hooki czekają w katalogu danych, aż projekt wróci."),
    KEPT_FOLDER_EMPTY: "Nic w folderze nie jest zmieniane.",
    NESTED: ("{root} leży w folderze {parent} projektu {other}; po usunięciu tego projektu {other} indeksuje foldery "
             ".memory w {root} jak zwykłe pliki, a asystent lub hook uruchomiony w {root} trafia do {other}, nie do "
             "usuniętego projektu."),
    BRING_BACK: "cairnmind project add {project} {root} przywraca ten sam projekt wraz z jego pamięcią.",
    EXTRA_ROOTS: ("Jego pozostałe foldery nie wracają przy tym; cairnmind project root {project} FOLDER dodaje każdy "
                  "z nich ponownie: {roots}."),
    DELETE_STEP: ("Skasowanie jego pamięci to osobny krok: najpierw cairnmind backup KATALOG/, potem cairnmind forget "
                  "{id} --delete-project."),
    NEXT_YES: "Dalej: cairnmind project remove {project} --yes usuwa go.",
    NEXT_LIST: "Dalej: cairnmind project list --removed wymienia usunięte projekty.",
    ROOT_DRY_RUN: ("Nic nie zostało zmienione; oto co zrobiłoby cairnmind project remove {project} --root {root} "
                   "--yes."),
    ROOT_REMOVED: "Usunięto folder {root} z {project}; niczego nie skasowano.",
    ROOT_EFFECT: ("Przy następnym indeksowaniu pliki z niego przestają być aktualne: ich migawki zostają jako dowody "
                  "cytowane przez rekordy, a ich fragmenty wyszukiwania, wektory i graf kodu znikają, jak dla "
                  "usuniętych plików."),
    ROOT_KEPT: "Wszystko inne w {project} zostaje, a w {root} nic nie jest zmieniane.",
    NEXT_ROOT_YES: "Dalej: cairnmind project remove {project} --root {root} --yes usuwa ten folder.",
    ROOT_NEXT: "Dalej: cairnmind project root {project} {root} dodaje go ponownie.",
    PROJECT_REMOVED: ("Projekt {name} został usunięty z rejestru {when}; jego pamięć jest zachowana. cairnmind "
                      "project add {name} {root} przywraca go wraz z pamięcią."),
    SEVERAL_REMOVED: ("Kilka usuniętych projektów ma nazwę {name} i folder {root} (identyfikatory {ids}); cairnmind "
                      "project list --removed je wymienia. Nic nie zostało zmienione."),
    ROOT_OF_ANOTHER: ("{root} jest teraz folderem projektu {other}; cairnmind project remove {other} --root {root} go "
                      "zwalnia. Nic nie zostało zmienione."),
    RESTORED: ("Przywrócono {project} w {root} wraz z pamięcią, w stanie z chwili usunięcia ({when}); zindeksowane "
               "pliki: {files}."),
    RESTORED_ROOTS: ("Jego pozostałe foldery nie zostały dodane z powrotem; cairnmind project root {project} FOLDER "
                     "dodaje każdy z nich ponownie: {roots}."),
    NOT_BROUGHT_BACK: ("Projekt usunięty {when} jako {name} w {root} zachowuje pamięć i nie został przywrócony: "
                       "przywraca go tylko cairnmind project add z tą samą nazwą i tym samym folderem."),
    NO_WAY_BACK: ("cairnmind project add nie przywróci {name}: jego folder {root} leży w katalogu danych, którego "
                  "Cairnmind nigdy nie indeksuje. Pamięć zostaje w magazynie; jej skasowanie wymaga cairnmind backup "
                  "KATALOG/, a potem cairnmind forget {id} --delete-project."),
    REMOVED_STUCK: ("Projekt {name} został usunięty z rejestru {when}; jego pamięć jest zachowana, ale cairnmind "
                    "project add go nie przywróci: jego folder {root} leży w katalogu danych, którego Cairnmind nigdy "
                    "nie indeksuje. Skasowanie jego pamięci wymaga cairnmind backup KATALOG/, a potem cairnmind forget "
                    "{id} --delete-project."),
    IMPORT_STUCK: ("Ten pakiet pamięci należy do projektu {name}, który został usunięty z rejestru {when} i nie da się "
                   "go przywrócić, bo jego folder {root} leży w katalogu danych. Niczego nie zaimportowano."),
    FORGET_BY_ID: ("Projekt {name} został usunięty z rejestru, więc forget przyjmuje jego identyfikator: najpierw "
                   "cairnmind backup KATALOG/, potem cairnmind forget {id} --delete-project. Niczego nie skasowano."),
    RESTORED_WORKSPACE: ("Przywrócono {project} w {root} wraz z pamięcią, w stanie z chwili usunięcia ({when}); to nie "
                         "jest nowy projekt."),
    NEW_REFUSED: ("Projekt {name} w {root} został usunięty {when} i zachowuje pamięć; nowy projekt o tej nazwie i tym "
                  "folderze nie dałby się od niego odróżnić. Nadaj nowemu projektowi inną nazwę albo użyj cairnmind "
                  "project add {name} {root}, który przywraca stary. Nic nie zostało zmienione."),
    REMOVED_HEADING: "Usunięte projekty, których pamięć jest zachowana:",
    REMOVED_ROW: "  - {name}: {root} (usunięty {when}; id {id})",
    REMOVED_NONE: "Żaden usunięty projekt nie zachowuje pamięci w tym magazynie.",
    REMOVED_NEXT: ("Dalej: cairnmind project add NAZWA FOLDER, z pokazaną nazwą i folderem, przywraca projekt wraz z "
                   "jego pamięcią; skasowanie jego pamięci wymaga cairnmind backup KATALOG/, a potem cairnmind forget "
                   "ID --delete-project."),
    REMOVED_HINT: ("Usunięte projekty z zachowaną pamięcią: {count}; cairnmind project list --removed je "
                   "wymienia."),
    IMPORT_REMOVED: ("Ten pakiet pamięci należy do projektu {name}, który został usunięty z rejestru {when}. "
                     "cairnmind project add {name} {root} go przywraca; potem zaimportuj ponownie. Niczego nie "
                     "zaimportowano."),
}


class Refused(ValueError):
    """A refusal: the command line prints it once and exits 2; nothing was changed."""


def _q(text) -> str:
    return shlex.quote(safe_text(str(text)))


def when(text: str | None) -> str:
    return (text or "")[:16].replace("T", " ") + " UTC"


def removed_rows(reader) -> list[dict]:
    return [clean_row(row) for row in reader.query(
        "SELECT FROM projects WHERE removed_at IS NOT NULL ORDER BY removed_at")]


def removed_ids(reader) -> set[str]:
    return {row["id"] for row in reader.query("SELECT id FROM projects WHERE removed_at IS NOT NULL")}


def stuck(row: dict, data_dir) -> bool:
    """A folder inside the data directory is refused by `project add` (#295), so such a project cannot come back."""
    from .path_spelling import lies_in
    return bool(row.get("removed_root")) and lies_in(row["removed_root"], data_dir)


def removed_text(row: dict, data_dir) -> str:
    return (REMOVED_STUCK if stuck(row, data_dir) else PROJECT_REMOVED).format(
        name=_q(row.get("removed_name")), root=_q(row.get("removed_root")), when=when(row.get("removed_at")),
        id=row.get("id"))


def import_refusal(row: dict, data_dir) -> str:
    return (IMPORT_STUCK if stuck(row, data_dir) else IMPORT_REMOVED).format(
        name=_q(row.get("removed_name")), root=_q(row.get("removed_root")), when=when(row.get("removed_at")))


def find_removed(reader, value: str, path: Path | None = None) -> dict | None:
    """The removed project a name, an id or (with `path`) a folder inside one of its folders names."""
    if path is None:
        rows = reader.query("SELECT FROM projects WHERE removed_at IS NOT NULL AND (id=:value OR removed_name=:value) "
                            "ORDER BY removed_at DESC LIMIT 1", {"value": value})
        return clean_row(rows[0]) if rows else None
    best, found = -1, None
    for row in removed_rows(reader):
        for folder in (row.get("removed_root"), *(row.get("removed_roots") or [])):
            if folder and (path == Path(folder) or Path(folder) in path.parents) and len(folder) > best:
                best, found = len(folder), row
    return found


def restore_in(tx, name: str, path: str, allow: bool = True) -> dict | None:
    """Inside Store.add_project: the removed project that had exactly this name and main folder comes back; a caller
    that asked for a separate identity (allow False) is refused instead of getting the old one."""
    rows = tx.query("SELECT FROM projects WHERE removed_at IS NOT NULL AND removed_name=:name AND removed_root=:root "
                    "LIMIT 2", {"name": name, "root": path})
    if not rows:
        return None
    if not allow:
        raise ValueError(NEW_REFUSED.format(name=_q(name), root=_q(path), when=when(rows[0].get("removed_at"))))
    if len(rows) > 1:
        raise ValueError(SEVERAL_REMOVED.format(name=_q(name), root=_q(path), ids=", ".join(r["id"] for r in rows)))
    pid = rows[0]["id"]
    taken = tx.query("SELECT project_id FROM project_roots WHERE path=:path", {"path": path})
    if taken:
        other = one_row(tx, "SELECT FROM projects WHERE id=:id", {"id": taken[0]["project_id"]}, False) or {}
        raise ValueError(ROOT_OF_ANOTHER.format(root=_q(path), other=_q(other.get("name") or taken[0]["project_id"])))
    update_document(tx, "projects", {"name": name, "root": path}, "id=:id", {"id": pid})
    tx.command(f"UPDATE projects REMOVE {', '.join(REMOVED_FIELDS)} WHERE id=:id", {"id": pid})
    insert_document(tx, "project_roots", {"id": str(uuid4()), "project_id": pid, "path": path})
    return one_row(tx, "SELECT FROM projects WHERE id=:id", {"id": pid})


def _jobs_in_work(reader, project: dict) -> None:
    jobs = reader.query("SELECT kind FROM jobs WHERE project_id=:project AND state='running' AND lease_until>=:now",
                        {"project": project["id"], "now": time.time()})
    if jobs:
        raise Refused(JOBS_IN_WORK.format(project=_q(project["name"]),
                                          kinds=", ".join(sorted({str(j.get("kind")) for j in jobs}))))


def require_idle(settings, store, project: dict) -> None:
    """A run of the workspace that is not known to have ended, or a job in work, refuses; so does a registry that
    cannot be read. A client outside the workspace leaves no sign that it runs and cannot be seen here."""
    from .client_runtime import NativeRuntime
    name = _q(project["name"])
    runtime = NativeRuntime(settings)
    try:
        # Read only, even for a dry run: the registry is never rewritten here (list_runs would mark rows).
        rows = runtime._read() if runtime.registry.exists() else []
        mine = [run for run in rows if run.get("project_id") == project["id"]
                and str(run.get("lifecycle") or run.get("state")) not in ENDED]  # unknown counts as live
        if mine and runtime.binary is None:
            # Without tmux a live session cannot be ruled out: refused, not assumed ended.
            raise ValueError(runtime._binary_error or "tmux cannot be found")
        # Only a running or stopping row is judged by its pane, as list_runs does: a row still starting (its pane may
        # not exist yet, the registry is read without the spawn's lock) or of an unknown lifecycle counts as live.
        live = [run for run in mine if str(run.get("lifecycle") or run.get("state")) not in ("running", "stopping")
                or ((pane := runtime._pane(run)) is not None and not pane["dead"])]
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        raise Refused(RUNS_UNKNOWN.format(project=name, reason=safe_text(str(exc))[:200])) from None
    if live:
        shown = ", ".join(f"{safe_text(run.get('client'))} {safe_text(run.get('lifecycle') or run.get('state'))}"
                          for run in live)
        raise Refused(RUNS_LIVE.format(project=name, runs=shown))
    _jobs_in_work(store, project)


def _folder_files(root: str) -> list[str]:
    from .memory_paths import RESERVED_DIRECTORIES
    found = []
    for relative in INSTALLED_FILES:
        path = Path(root) / relative
        if path.is_symlink() or not path.is_file():
            continue
        try:
            with path.open("rb") as handle:
                text = handle.read(1 << 20).lower()
        except OSError:
            found.append(str(path))  # not readable: named rather than assumed to be ours
            continue
        if b"cairnmind" in text or b"devbrain" in text:
            found.append(str(path))
    found.extend(str(Path(root) / d) for d in RESERVED_DIRECTORIES if (Path(root) / d).exists())
    return found


def plan(settings, store, project: dict, root: Path | None = None) -> dict:
    """What removing the project (or one of its folders) would do; changes nothing."""
    from . import answer_log
    pid, main, name = project["id"], project["root"], _q(project["name"])
    head = {"id": pid, "name": project["name"], "root": main}
    paths = [r["path"] for r in store.query("SELECT path FROM project_roots WHERE project_id=:project ORDER BY path",
                                            {"project": pid})]
    roots = [main, *[p for p in paths if p != main]]
    if root is not None:
        folder = str(root)
        if folder == main:
            raise Refused(MAIN_ROOT.format(root=safe_text(folder), project=name))
        if folder not in paths:
            raise Refused(NOT_A_ROOT.format(root=safe_text(folder), project=name,
                                            roots=", ".join(safe_text(p) for p in roots)))
        if store.query("SELECT id FROM memory_settings WHERE id=:id AND enabled=true AND worktree=:root",
                       {"id": pid, "root": folder}):
            raise Refused(ROOT_SCHEDULED.format(project=name, root=safe_text(folder)))
        return {"project": head, "root": folder, "applied": False,
                "bring_back": f"cairnmind project root {name} {shlex.quote(safe_text(folder))}"}
    counts = store.status(pid)
    log = answer_log.log_path(settings, pid)
    names = {r["id"]: r["name"] for r in store.query("SELECT id,name FROM projects WHERE removed_at IS NULL")}
    nested = [{"root": own, "project": names.get(other["project_id"], other["project_id"]), "parent": other["path"]}
              for own in roots
              for other in store.query("SELECT project_id,path FROM project_roots WHERE project_id!=:project",
                                       {"project": pid})
              if Path(other["path"]) in Path(own).parents]
    kept = {key: counts[key] for key in ("records", "plans", "sources", "sessions", "check_runs", "chunks", "nodes",
                                         "embeddings")}
    kept.update(answer_log=str(log) if log.exists() else None, backups=str(Path(settings.data_dir) / "backups"),
                folder=[path for each in roots for path in _folder_files(each)])
    return {"project": head, "root": None, "applied": False, "removed_at": None, "roots": roots,
            "queued_jobs": counts["jobs"]["pending"],
            "transcripts_followed": len(store.query("SELECT path FROM import_cursors WHERE project_id=:project",
                                                    {"project": pid})),
            "scheduled_snapshot": bool(store.query("SELECT id FROM memory_settings WHERE id=:id AND enabled=true",
                                                   {"id": pid})),
            "kept": kept, "nested": nested, "bring_back": f"cairnmind project add {name} {_q(main)}",
            "no_way_back": stuck({"removed_root": main}, store.data_dir),
            "extra_roots": roots[1:], "delete": f"cairnmind forget {pid} --delete-project"}


def apply(store, result: dict) -> dict:
    """Make the change in one transaction that reads the row again: a project removed meanwhile, or a job that
    started meanwhile, refuses."""
    from .ingest import forget_index_signature
    pid, folder = result["project"]["id"], result["root"]
    with store.transaction() as tx:
        row = one_row(tx, "SELECT FROM projects WHERE id=:id AND removed_at IS NULL", {"id": pid}, False)
        if not row:
            raise Refused(f"Unknown project: {pid}. {UNKNOWN_PROJECT_NEXT}")
        _jobs_in_work(tx, row)
        paths = [r["path"] for r in tx.query("SELECT path FROM project_roots WHERE project_id=:project",
                                             {"project": pid})]
        if folder is not None:
            if folder not in paths:
                raise Refused(NOT_A_ROOT.format(root=safe_text(folder), project=_q(row["name"]),
                                                roots=", ".join(safe_text(p) for p in sorted(paths))))
            tx.command("DELETE FROM project_roots WHERE project_id=:project AND path=:path",
                       {"project": pid, "path": folder})
            removed_at = None
        else:
            removed_at = utc_now()
            update_document(tx, "projects", {"removed_at": removed_at, "removed_name": row["name"],
                                             "removed_root": row["root"],
                                             "removed_roots": sorted(p for p in paths if p != row["root"])},
                            "id=:id", {"id": pid})
            tx.command("UPDATE projects REMOVE name, root WHERE id=:id", {"id": pid})
            tx.command("DELETE FROM project_roots WHERE project_id=:project", {"project": pid})
    forget_index_signature(store, pid)
    return {**result, "applied": True, **({"removed_at": removed_at} if folder is None else {})}


def sentences(result: dict) -> list[str]:
    name, applied = _q(result["project"]["name"]), result["applied"]
    if result["root"] is not None:
        shown, quoted = safe_text(result["root"]), shlex.quote(safe_text(result["root"]))
        return [(ROOT_REMOVED.format(project=name, root=shown) if applied
                 else ROOT_DRY_RUN.format(project=name, root=quoted)),
                ROOT_EFFECT, ROOT_KEPT.format(project=name, root=shown),
                (ROOT_NEXT if applied else NEXT_ROOT_YES).format(project=name, root=quoted)]
    kept = result["kept"]
    lines = [REMOVED.format(project=name) if applied else DRY_RUN.format(project=name)]
    if not applied:
        lines.append(WOULD_STOP.format(project=name, roots=", ".join(safe_text(p) for p in result["roots"])))
    if result["queued_jobs"]:
        lines.append(QUEUED.format(project=name, count=result["queued_jobs"]))
    if result["scheduled_snapshot"]:
        lines.append(SCHEDULE_PAUSED)
    lines.append(KEPT_STORE.format(**{key: kept[key] for key in ("records", "plans", "sources", "sessions",
                                                                  "check_runs")}))
    if kept["answer_log"]:
        lines.append(KEPT_ANSWERS.format(path=safe_text(kept["answer_log"])))
    lines.append(KEPT_FOLDER.format(project=name, paths=", ".join(safe_text(p) for p in kept["folder"]))
                 if kept["folder"] else KEPT_FOLDER_EMPTY)
    lines.extend(NESTED.format(root=safe_text(n["root"]), parent=safe_text(n["parent"]), other=_q(n["project"]))
                 for n in result["nested"])
    if result["no_way_back"]:
        lines.append(NO_WAY_BACK.format(name=name, root=_q(result["project"]["root"]), id=result["project"]["id"]))
    else:
        lines.append(BRING_BACK.format(project=name, root=_q(result["project"]["root"])))
    if result["extra_roots"] and not result["no_way_back"]:
        lines.append(EXTRA_ROOTS.format(project=name, roots=", ".join(safe_text(p) for p in result["extra_roots"])))
    lines.append(DELETE_STEP.format(id=result["project"]["id"]))
    lines.append((NEXT_LIST if applied else NEXT_YES).format(project=name))
    return lines


def removed_view(reader) -> list[dict]:
    return [{"id": row["id"], "name": row.get("removed_name"), "root": row.get("removed_root"),
             "roots": row.get("removed_roots") or [], "removed_at": row.get("removed_at"),
             "can_return": not stuck(row, reader.data_dir)}
            for row in removed_rows(reader)]


def removed_sentences(rows: list[dict]) -> list[str]:
    if not rows:
        return [REMOVED_NONE]
    return [REMOVED_HEADING,
            *(REMOVED_ROW.format(name=safe_text(r["name"]), root=safe_text(r["root"]), when=when(r["removed_at"]),
                                 id=r["id"]) for r in rows),
            *(NO_WAY_BACK.format(name=_q(r["name"]), root=_q(r["root"]), id=r["id"])
              for r in rows if not r["can_return"]),
            REMOVED_NEXT]


def restored_lines(project: dict, removed: dict, files: int) -> list[str]:
    """What `project add` says when it brought a removed project back."""
    name = _q(project["name"])
    lines = [RESTORED.format(project=name, root=safe_text(project["root"]), when=when(removed.get("removed_at")),
                             files=files)]
    if removed.get("removed_roots"):
        lines.append(RESTORED_ROOTS.format(project=name, roots=", ".join(safe_text(p)
                                                                       for p in removed["removed_roots"])))
    return lines


def workspace_notice(translate, project: dict, root: str, back: dict) -> str:
    """What the workspace says when adding a folder brought a removed project back."""
    return translate(RESTORED_WORKSPACE, project=_q(project["name"]), root=safe_text(root),
                     when=when(back.get("removed_at")))


def not_brought_back(row: dict) -> str:
    return NOT_BROUGHT_BACK.format(when=when(row.get("removed_at")), name=_q(row.get("removed_name")),
                                   root=safe_text(row.get("removed_root")))


def forget_target(store, value: str) -> dict:
    """`forget` takes a registered project, or a removed one by its id (its folder may be gone)."""
    try:
        return store.resolve_project(value)
    except ValueError:
        rows = store.query("SELECT FROM projects WHERE removed_at IS NOT NULL AND id=:id", {"id": value})
        if not rows:
            named = find_removed(store, value)
            if named:
                raise ValueError(FORGET_BY_ID.format(name=_q(value), id=named["id"])) from None
            raise
        return clean_row(rows[0])
