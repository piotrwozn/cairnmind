"""Whether this machine can index a project's folder (#371), and what Cairnmind says when it cannot.

A folder that is missing, cannot be read or is not on this machine is never walked: nothing of the project is
deactivated, replaced or dropped from the code graph for it. Only the folder itself is tested; a folder that
opens and is empty is a folder whose files were deleted.
"""
from __future__ import annotations

import os
from pathlib import Path

from .terminal_safe import safe_text

STATES = ("available", "missing", "unreadable")

FOLDER_UNAVAILABLE = ("The folder {root} of project {project} is unavailable on this machine: {reason}. Nothing of the "
                      "project is changed for it: its files are not counted as deleted and its code graph is kept.")
REASON_MISSING = "it does not exist here"
REASON_UNREADABLE = "it cannot be read here ({error})"
GRAPH_KEPT = ("The code graph of project {project} is kept as it was, not rebuilt: files of it that another parser "
              "version analysed and that cannot be read here: {count}. Indexing where they are rebuilds it.")
MESSAGES = {
    FOLDER_UNAVAILABLE: ("Folder {root} projektu {project} jest niedostępny na tym komputerze: {reason}. Nic w projekcie "
                         "nie zmienia się z jego powodu: jego pliki nie są liczone jako usunięte, a graf kodu zostaje "
                         "zachowany."),
    REASON_MISSING: "nie istnieje tutaj",
    REASON_UNREADABLE: "nie da się go tu odczytać ({error})",
    GRAPH_KEPT: ("Graf kodu projektu {project} zostaje taki, jaki był, bez przebudowy: liczba jego plików, które "
                 "przeanalizowała inna wersja parsera i których nie da się tu odczytać: {count}. Przebuduje go "
                 "indeksowanie tam, gdzie one są."),
}


def _reason(folder: dict) -> tuple[str, dict]:
    if folder["state"] == "missing":
        return REASON_MISSING, {}
    return REASON_UNREADABLE, {"error": safe_text(folder.get("error") or "unknown error")}


def folder_states(store, project: dict) -> list[dict]:
    """One item per registered folder, the project's main one first: root, state, host, error, row_id."""
    rows = {str(Path(row["path"]).resolve()): row for row in store.query(
        "SELECT id,path FROM project_roots WHERE project_id=:project", {"project": project["id"]})}
    # The order and resolution of ingest._roots, written out here so that module can import this one.
    roots = list(dict.fromkeys([Path(project["root"]).resolve(), *[Path(path) for path in rows]]))
    found = []
    for root in roots:
        state, error = "available", None
        try:
            with os.scandir(root):
                pass
        except (FileNotFoundError, NotADirectoryError):
            state = "missing"
        except OSError as exc:
            state, error = "unreadable", exc.strerror or type(exc).__name__
        row = rows.get(str(root))
        found.append({"root": str(root), "state": state, "host": None, "error": error,
                      "row_id": row["id"] if row else None})
    return found


def message(project: dict, folder: dict) -> str:
    """The English sentence for one folder that is not available."""
    reason, values = _reason(folder)
    return FOLDER_UNAVAILABLE.format(root=safe_text(folder["root"]), project=safe_text(project["name"]),
                                     reason=reason.format(**values))


def entry(project: dict, folder: dict) -> dict:
    return {"project": project["name"], "root": folder["root"], "state": folder["state"], "host": folder.get("host"),
            "error": folder.get("error"), "message": message(project, folder)}


def unavailable(store, project_id: str | None = None) -> list[dict]:
    """The entries of every folder that is not available, for one project or for all, by project name then root."""
    projects = [store.resolve_project(project_id)] if project_id else store.list_projects()
    found = [entry(project, folder) for project in projects for folder in folder_states(store, project)
             if folder["state"] != "available"]
    return sorted(found, key=lambda item: (item["project"], item["root"]))


def folder_notice(entries: list[dict], language: str = "en") -> str:
    """The same sentences in the person's language, for the workspace."""
    from .terminal_i18n import tr
    parts = []
    for item in entries:
        reason, values = _reason(item)
        parts.append(tr(FOLDER_UNAVAILABLE, language, root=safe_text(item["root"]), project=safe_text(item["project"]),
                        reason=tr(reason, language, **values)))
    return " ".join(parts)
