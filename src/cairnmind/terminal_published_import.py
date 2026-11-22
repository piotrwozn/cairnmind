"""Import published team memory in the memory view (#304): from a folder or a commit, over the person's own
verdicts only with their consent. The check lists every record confirmed or disputed here that the import
replaces; the import is sent with that list, and the workspace's key travels with it (terminal_client)."""
from __future__ import annotations

import os
import re
from uuid import UUID

from .terminal_notices import about

FULL_PATH = "Enter the full path of the folder."
TAKE_ENTRY = "Enter record IDs or file names such as fact/<ID>.md, at most 1,000."
FOLDER_NOTE = ("The check writes nothing. If it lists records that diverged, paste their file names into the last "
               "field to take the published side of them; your versions stay in the history.")
COMMIT_NOTE = "The check writes nothing. The import also checks every quoted passage against that commit's code."
REPLACED_HEADING = ("These records were confirmed or disputed on this computer, and the import puts the published "
                    "text or status in their place ({count}):")
COUNTS = ("New: {inserted} · moved on: {advanced} · taken from the published side: {taken} · unchanged: {unchanged} "
          "· plans: {plans}")

MESSAGES = {
    "Team memory": "Pamięć zespołu",
    "Choose a project to import its team memory.": "Wybierz projekt, aby zaimportować jego pamięć zespołu.",
    "Import team memory": "Importuj pamięć zespołu",
    "From a published memory folder": "Z katalogu opublikowanej pamięci",
    "From a commit of the project's repository": "Z commita repozytorium projektu",
    "Check team memory import": "Sprawdź import pamięci zespołu",
    "Published memory folder": "Katalog opublikowanej pamięci",
    "Take the published side of (file names or record IDs, optional)":
        "Weź stronę opublikowaną dla (nazwy plików lub identyfikatory rekordów, opcjonalnie)",
    "Commit (a branch, HEAD or a commit ID)": "Commit (gałąź, HEAD albo identyfikator commita)",
    FOLDER_NOTE: "Sprawdzenie niczego nie zapisuje. Jeśli wymieni rekordy, które się rozeszły, wklej nazwy ich plików "
                 "do ostatniego pola, aby wziąć ich stronę opublikowaną; twoje wersje zostają w historii.",
    COMMIT_NOTE: "Sprawdzenie niczego nie zapisuje. Import sprawdza też każdy cytowany fragment z kodem tego commita.",
    FULL_PATH: "Podaj pełną ścieżkę katalogu.",
    TAKE_ENTRY: "Podaj identyfikatory rekordów albo nazwy plików, np. fact/<ID>.md, najwyżej 1000.",
    "Import this team memory?": "Zaimportować tę pamięć zespołu?",
    "Commit {commit}": "Commit {commit}",
    COUNTS: "Nowe: {inserted} · przesunięte dalej: {advanced} · wzięte ze strony opublikowanej: {taken} · "
            "bez zmian: {unchanged} · plany: {plans}",
    REPLACED_HEADING: "Te rekordy zostały potwierdzone lub zakwestionowane na tym komputerze, a import wstawia w ich "
                      "miejsce opublikowaną treść lub status ({count}):",
    "No record confirmed or disputed on this computer is replaced.":
        "Żaden rekord potwierdzony ani zakwestionowany na tym komputerze nie zostanie zastąpiony.",
    "Taken from the published side; your versions stay in the history ({count}):":
        "Wzięte ze strony opublikowanej; twoje wersje zostają w historii ({count}):",
    "Nothing imported is verified here.": "Nic z zaimportowanego nie jest tu zweryfikowane.",
    "Commit {commit} holds no published memory of this project. Nothing was imported.":
        "Commit {commit} nie zawiera opublikowanej pamięci tego projektu. Niczego nie zaimportowano.",
    "Team memory imported: {written} records written, {replaced} of them in place of a verdict given on this "
    "computer. Nothing imported is verified here.":
        "Zaimportowano pamięć zespołu: zapisano rekordów: {written}, w tym {replaced} w miejsce werdyktu wydanego na "
        "tym komputerze. Nic z zaimportowanego nie jest tu zweryfikowane.",
}


def full_path(value: str) -> str:
    """The folder as the service reads it: a typed Windows drive path becomes its WSL path, like the folder picker's."""
    if re.match(r"^[A-Za-z]:[\\/]", value.strip()):
        from .cockpit_state import normalize_path
        try:
            return str(normalize_path(value))
        except ValueError:
            return value
    return os.path.expanduser(value)


def folder_error(value: str) -> str | None:
    return None if os.path.isabs(full_path(value)) else FULL_PATH


def taken_ids(value: str) -> list[str] | None:
    """Record IDs from IDs or file names (`fact/<ID>.md`) split by spaces, commas or lines; None when any entry is
    not an ID or there are more than 1,000: an entry is refused, never dropped."""
    found: list[str] = []
    for entry in filter(None, re.split(r"[\s,]+", value)):
        name = entry.rsplit("/", 1)[-1].removesuffix(".md")
        try:
            if str(UUID(name)) != name:
                return None
        except ValueError:
            return None
        if name not in found:
            found.append(name)
    return found if len(found) <= 1000 else None


def take_error(value: str) -> str | None:
    return TAKE_ENTRY if taken_ids(value) is None else None


def count(value) -> int:
    return len(value) if isinstance(value, list) else value if type(value) is int else 0


def preview_text(result: dict, t) -> str:
    """What the consent dialog says: every replaced and every taken path, none cut (the dialog scrolls)."""
    from .terminal_safe import safe_text
    here = result.get("replaces_confirmed_here") or {}
    replaced, taken = list(here.get("records") or []), list(result.get("taken") or [])
    lines = []
    if result.get("commit"):
        lines.append(t("Commit {commit}", commit=safe_text(str(result["commit"]))[:12]))
    lines.append(t(COUNTS, inserted=count(result.get("inserted")), advanced=count(result.get("advanced")),
                   taken=len(taken), unchanged=count(result.get("unchanged")), plans=count(result.get("plans"))))
    if replaced:
        lines.append(t(REPLACED_HEADING, count=len(replaced)))
        lines.extend("  " + safe_text(str(path)) for path in replaced)
    else:
        lines.append(t("No record confirmed or disputed on this computer is replaced."))
    if taken:
        lines.append(t("Taken from the published side; your versions stay in the history ({count}):", count=len(taken)))
        lines.extend("  " + safe_text(str(path)) for path in taken)
    brought = result.get("confirmed_records")
    if isinstance(brought, dict) and type(brought.get("total")) is int and brought["total"] > 0:
        lines.append(t("Confirmed elsewhere: {count}. They stay confirmed, are shown as imported and are not "
                       "verified here.", count=brought["total"]))
        names = [safe_text(name)[:80] for name in (brought.get("confirmed_by") or [])[:8] if isinstance(name, str)]
        if names:
            lines.append(t("Confirmed by, as the package says: {names}", names=", ".join(names)))
    lines.append(t("Nothing imported is verified here."))
    return "\n".join(lines)


async def import_published(app) -> None:
    # Imported here: the catalog (terminal_i18n) reads MESSAGES from this module, and these import the catalog.
    from .terminal_panels import ConfirmModal
    from .terminal_ui import ChoiceModal, Field, FieldError, FormModal, replaces_here
    params = app.params()
    project_id, worktree = params["project"], params["worktree"]
    if not project_id:
        app.notice(app.t("Choose a project to import its team memory."), "warn")
        return
    scope = app._scope_version
    choice = await app.push_screen_wait(ChoiceModal("Import team memory", [
        ("From a published memory folder", "folder"), ("From a commit of the project's repository", "commit")]))
    if choice is None:
        return
    route = "/api/memory/import-published" if choice == "folder" else "/api/memory/import-commit"
    checked = {}

    async def check(values):
        if scope != app._scope_version:
            raise FieldError(next(iter(values)), "The project changed. Close this form and open it again.")
        if choice == "folder":
            body = {"path": full_path(values["path"]), "project": project_id,
                    "take_published": taken_ids(values["take"])}
        else:
            body = {"revision": values["revision"], "project": project_id, "worktree": worktree}
        checked["result"] = await app.request("POST", route, json={**body, "dry_run": True})
        checked["body"] = body

    if choice == "folder":
        destination = (app.memory_status.get("published") or {}).get("destination")
        fields = [Field("path", "Published memory folder", destination if isinstance(destination, str) else "",
                        required=True, validator=folder_error, folder=True),
                  Field("take", "Take the published side of (file names or record IDs, optional)", multiline=True,
                        validator=take_error)]
    else:
        fields = [Field("revision", "Commit (a branch, HEAD or a commit ID)", "HEAD", required=True)]
    form = await app.push_screen_wait(FormModal(
        "Check team memory import", fields, note=FOLDER_NOTE if choice == "folder" else COMMIT_NOTE,
        submit="Check import", on_submit=check))
    if form is None:
        return
    result, body = checked["result"], checked["body"]
    if result.get("status") == "nothing_published":
        app.notice(app.t("Commit {commit} holds no published memory of this project. Nothing was imported.",
                         commit=str(result.get("commit"))[:12]), "warn")
        return
    agreed = await app.push_screen_wait(ConfirmModal(
        "Import this team memory?", preview_text(result, app.t), approve="Import team memory", consent=True))
    if not agreed:
        return
    if scope != app._scope_version:
        app.notice_for(about(project_id), app.t("The project changed during review. Nothing was saved."), "warn")
        return
    if choice == "commit":
        body = {**body, "revision": result["commit"]}  # the commit the check read, not the name that may have moved
    listed = list((result.get("replaces_confirmed_here") or {}).get("records") or [])
    done = await app.request("POST", route, json={**body, "dry_run": False, "replace_confirmed": replaces_here(result),
                                                   "expected_replaced": listed})
    if scope == app._scope_version:
        await app.load_memory()
    written = sum(count(done.get(key)) for key in ("inserted", "advanced", "taken"))
    app.notice_for(about(project_id), app.t(
        "Team memory imported: {written} records written, {replaced} of them in place of a verdict given on this "
        "computer. Nothing imported is verified here.", written=written,
        replaced=count((done.get("replaces_confirmed_here") or {}).get("total"))))
