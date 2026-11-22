"""More, then Delete project memory (#347): the person deletes a project's whole memory.

The command line deletes nothing any more (project_forget.CLI_REFUSED). The request goes out only from the consent
dialog, which names the project and what is deleted with the counts the service read; the open workspace's key
travels with it in terminal_client. A refusal of the checks (a session running, a job in work, a reader) comes
before the dialog, so the dialog is never shown for a deletion that would be refused.
"""
from __future__ import annotations

import asyncio

from .terminal_person_act import agreed
from .terminal_safe import safe_text

TITLE = "Delete project memory"
NONE = "No project keeps memory in this store."
REMOVED_ROW = "{name}: {root} (removed {when})"
QUESTION = "Delete this project's memory?"
APPROVE = "Delete"
PROJECT_LINE = "Project: {name} (id {id})"
FOLDER_LINE = "Folder: {root}"
REMOVED_LINE = "Removed from the registry on {when}."
DELETES = ("This deletes from the store: {records} records, {plans} plans, {sources} sources, {sessions} sessions and "
           "{check_runs} check runs, with their revisions, acknowledgements, code graph ({nodes} nodes) and search "
           "index ({chunks} passages, {embeddings} vectors).")
ANSWER_LOG = "This deletes the answer log {path}."
KEPT = ("Not changed: the backups in {backups}, and nothing in the project's folders (its .memory snapshots, its "
        "published memory and the files cairnmind install wrote stay there).")
UNDO = ("It cannot be undone: only restoring a backup brings it back. To keep a copy, run cairnmind backup DIR/ in a "
        "terminal first.")
OPEN_NOTE = "It is the open project: the workspace returns to the project list."
DONE = ("Deleted the memory of {name}: {records} records, {plans} plans, {sources} sources, {sessions} sessions and "
        "{check_runs} check runs. Backups and the project's folders were not changed.")
LOG_LEFT = "The answer log {path} could not be deleted ({reason}); delete it by hand."
NOT_DELETED = "The project's memory was not deleted."
UNKNOWN = ("No answer to the request came, so it is not known whether the project's memory was deleted. Open More, "
           "then Delete project memory: the project is listed only while its memory is there.")
REGISTRY = ("The project also leaves the registry and the project list; adding its folder again makes a new, empty "
            "project.")
MESSAGES = {
    UNKNOWN: ("Nie przyszła odpowiedź na żądanie, więc nie wiadomo, czy pamięć projektu została skasowana. Otwórz "
              "Więcej, potem Skasuj pamięć projektu: projekt jest na liście tylko wtedy, gdy jego pamięć jeszcze "
              "istnieje."),
    REGISTRY: ("Projekt znika też z rejestru i z listy projektów; ponowne dodanie jego folderu tworzy nowy, pusty "
               "projekt."),
    TITLE: "Skasuj pamięć projektu",
    NONE: "Żaden projekt nie ma pamięci w tym magazynie.",
    REMOVED_ROW: "{name}: {root} (usunięty {when})",
    QUESTION: "Skasować pamięć tego projektu?",
    APPROVE: "Skasuj",
    PROJECT_LINE: "Projekt: {name} (id {id})",
    FOLDER_LINE: "Folder projektu: {root}",
    REMOVED_LINE: "Usunięty z rejestru {when}.",
    DELETES: ("To kasuje z magazynu: rekordy ({records}), plany ({plans}), źródła ({sources}), sesje ({sessions}) i "
              "przebiegi kontroli ({check_runs}), wraz z ich wersjami, potwierdzeniami, grafem kodu (węzły: {nodes}) "
              "i indeksem wyszukiwania (fragmenty: {chunks}, wektory: {embeddings})."),
    ANSWER_LOG: "To kasuje dziennik odpowiedzi {path}.",
    KEPT: ("Bez zmian: kopie zapasowe w {backups} i nic w folderach projektu (zostają tam jego migawki .memory, jego "
           "opublikowana pamięć i pliki zapisane przez cairnmind install)."),
    UNDO: ("Tego nie da się cofnąć: przywraca to tylko odtworzenie kopii zapasowej. Aby zachować kopię, najpierw "
           "uruchom w terminalu cairnmind backup KATALOG/."),
    OPEN_NOTE: "To otwarty projekt: środowisko wraca do listy projektów.",
    DONE: ("Skasowano pamięć {name}: rekordy ({records}), plany ({plans}), źródła ({sources}), sesje ({sessions}) i "
           "przebiegi kontroli ({check_runs}). Kopie zapasowe i foldery projektu nie zostały zmienione."),
    LOG_LEFT: "Nie udało się skasować dziennika odpowiedzi {path} ({reason}); skasuj go ręcznie.",
    NOT_DELETED: "Pamięć projektu nie została skasowana.",
}


def _counts(shown) -> dict:
    deletes = shown.get("deletes") if isinstance(shown.get("deletes"), dict) else {}
    return {key: deletes[key] if type(deletes.get(key)) is int else "?"
            for key in ("records", "plans", "sources", "sessions", "check_runs", "chunks", "nodes", "embeddings")}


def _no_answer(exc) -> bool:
    from .terminal_client import BrokerError, BrokerUnreachable
    return isinstance(exc, BrokerUnreachable) or (isinstance(exc, BrokerError) and exc.status_code in (502, 503, 504))


async def _gone(app, project_id) -> bool:
    """True only when the list was read again and no longer has the project."""
    try:
        listing = await app.request("GET", "/api/inspector/projects/deletable")
    except (OSError, ValueError, RuntimeError):
        return False
    return not any(isinstance(item, dict) and item.get("id") == project_id for item in listing.get("items") or [])


def label(item, t) -> str:
    from .project_removal import when
    name, root = safe_text(item.get("name"), limit=80), safe_text(item.get("root"), limit=120)
    if item.get("removed_at"):
        return t(REMOVED_ROW, name=name, root=root, when=when(item["removed_at"]))
    return f"{name}: {root}"


def describe(shown, is_open, t) -> str:
    """What the deletion will do, for the person to read before Delete."""
    from .project_removal import when
    project = shown["project"]
    # Escaped, never cut: this is what the person agrees to delete.
    lines = [t(PROJECT_LINE, name=safe_text(project.get("name")), id=safe_text(project.get("id"))),
             t(FOLDER_LINE, root=safe_text(project.get("root")))]
    if project.get("removed_at"):
        lines.append(t(REMOVED_LINE, when=when(project["removed_at"])))
    lines += ["", t(DELETES, **_counts(shown))]
    if shown.get("answer_log"):
        lines.append(t(ANSWER_LOG, path=safe_text(shown["answer_log"])))
    lines.append(t(REGISTRY))
    lines += [t(KEPT, backups=safe_text(shown.get("backups"))), t(UNDO)]
    if is_open:
        lines.append(t(OPEN_NOTE))
    return "\n".join(lines)


async def open_view(app):
    # Imported here: the catalog (terminal_i18n) reads MESSAGES from this module, and these import the catalog.
    from .terminal_i18n_messages import broker_error_text
    from .terminal_ui import ChoiceModal
    listing = await app.request("GET", "/api/inspector/projects/deletable")
    items = [item for item in (listing.get("items") or []) if isinstance(item, dict) and isinstance(item.get("id"), str)]
    if not items:
        app.notice(app.t(NONE))
        return
    open_id = (await asyncio.to_thread(app.workspace.read)).get("project_id")
    items.sort(key=lambda item: (item["id"] != open_id, item.get("removed_at") is not None))
    choice = await app.push_screen_wait(ChoiceModal(app.t(TITLE), [
        (label(item, app.t), str(number)) for number, item in enumerate(items)]))
    if choice is None:
        return
    item = items[int(choice)]
    try:
        shown = await app.request("GET", "/api/inspector/projects/forget", params={"project": item["id"]})
    except (OSError, ValueError, RuntimeError) as exc:
        # A session running, a job in work or a reader: the refusal is the answer, and no dialog is shown.
        app.notice(app.t(NOT_DELETED) + " " + broker_error_text(exc, app.t), "error")
        return
    if not await agreed(app, QUESTION, describe(shown, item["id"] == open_id, app.t), APPROVE):
        return
    result = {}
    try:
        result = await app.request("POST", "/api/inspector/projects/forget", json={"project": item["id"]})
    except (OSError, ValueError, RuntimeError) as exc:
        if not _no_answer(exc):
            app.notice(app.t(NOT_DELETED) + " " + broker_error_text(exc, app.t), "error")
            return
        # No answer came, and the service may have deleted all the same: the list says whether it did, never a guess.
        if not await _gone(app, item["id"]):
            app.notice(app.t(UNKNOWN), "error")
            return
    if (await asyncio.to_thread(app.workspace.read)).get("project_id") == item["id"]:
        await asyncio.to_thread(app.workspace.select_project, None)
        app.action_projects()
    deleted = result.get("deleted_counts")
    done = app.t(DONE, name=safe_text(item.get("name"), limit=80),
                 **_counts({"deletes": deleted} if isinstance(deleted, dict) else shown))
    if result.get("answer_log_error"):
        done += " " + app.t(LOG_LEFT, path=safe_text(shown.get("answer_log"), limit=400),
                            reason=safe_text(result["answer_log_error"], limit=300))
    app.notice(done)
    await app.reconcile()
