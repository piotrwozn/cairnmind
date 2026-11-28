"""Whether this machine can index a project's folder (#371), and what Cairnmind says when it cannot.

A folder that is missing, cannot be read or belongs to another machine is never walked: nothing of the project is
deactivated, replaced or dropped from the code graph for it. Only the folder itself is tested; a folder that
opens and is empty is a folder whose files were deleted. A folder belongs to the machine that registered it: its
`project_roots` row records a hash of that machine and its host name, and another machine never indexes it, even
when a folder of the same path exists there. A row written before this rule has no machine and is stamped by the
first complete index that reads it.
"""
from __future__ import annotations

import hashlib
import os
import socket
from pathlib import Path

from .terminal_safe import safe_text

STATES = ("available", "missing", "unreadable", "elsewhere")

FOLDER_UNAVAILABLE = ("The folder {root} of project {project} is unavailable on this machine: {reason}. Nothing of the "
                      "project is changed for it: its files are not counted as deleted and its code graph is kept.")
REASON_MISSING = "it does not exist here"
REASON_UNREADABLE = "it cannot be read here ({error})"
REASON_ELSEWHERE = "it belongs to {host}, the machine that added it, and only that machine indexes it"
REASON_ELSEWHERE_UNNAMED = "it belongs to the machine that added it, and only that machine indexes it"
FOLDER_MOVE = ("If the project's files are on this machine now, at this path, cairnmind project root {project} {root} "
               "records that, and the next indexing reads them here; files that differ from the indexed ones then "
               "replace them.")
GRAPH_KEPT = ("The code graph of project {project} is kept as it was, not rebuilt: files of it that another parser "
              "version analysed and that cannot be read here: {count}. Indexing where they are rebuilds it.")
MESSAGES = {
    FOLDER_UNAVAILABLE: ("Folder {root} projektu {project} jest niedostępny na tym komputerze: {reason}. Nic w projekcie "
                         "nie zmienia się z jego powodu: jego pliki nie są liczone jako usunięte, a graf kodu zostaje "
                         "zachowany."),
    REASON_MISSING: "nie istnieje tutaj",
    REASON_UNREADABLE: "nie da się go tu odczytać ({error})",
    REASON_ELSEWHERE: "należy do {host}, komputera, który go dodał, i tylko tamten komputer go indeksuje",
    REASON_ELSEWHERE_UNNAMED: "należy do komputera, który go dodał, i tylko tamten komputer go indeksuje",
    FOLDER_MOVE: ("Jeśli pliki projektu są teraz na tym komputerze, pod tą ścieżką, cairnmind project root {project} "
                  "{root} to zapisuje, a następne indeksowanie czyta je tutaj; pliki różne od zaindeksowanych zastąpią "
                  "wtedy tamte."),
    GRAPH_KEPT: ("Graf kodu projektu {project} zostaje taki, jaki był, bez przebudowy: liczba jego plików, które "
                 "przeanalizowała inna wersja parsera i których nie da się tu odczytać: {count}. Przebuduje go "
                 "indeksowanie tam, gdzie one są."),
}


def this_machine() -> str | None:
    """A hash of what tells this machine from another, so the raw machine id never enters the store or a backup.
    None when nothing better than the host name identifies it (no machine id, a Mac whose platform UUID cannot be
    read, an image with an uninitialised id): a host name changes with the network and is shared by clones, so such
    a machine neither claims a folder nor refuses one, and every folder behaves as it did before 0.13.0."""
    from .store_writer import machine_identity
    identity = machine_identity()
    if identity.startswith("host:") or identity == "machine-id:uninitialized":
        return None
    return hashlib.sha256(("cairnmind-folder\0" + identity).encode()).hexdigest()


def stamp() -> dict:
    """What a `project_roots` row records of the machine that registered it; nothing when this machine is not
    identifiable (see this_machine)."""
    from .store_writer import label
    here = this_machine()
    return {"machine": here, "host": label(socket.gethostname())} if here else {}

def _reason(folder: dict) -> tuple[str, dict]:
    if folder["state"] == "missing":
        return REASON_MISSING, {}
    if folder["state"] == "elsewhere":
        if not folder.get("host"):
            return REASON_ELSEWHERE_UNNAMED, {}
        return REASON_ELSEWHERE, {"host": safe_text(folder["host"])}
    return REASON_UNREADABLE, {"error": safe_text(folder.get("error") or "unknown error")}


def folder_states(store, project: dict, rows: list[dict] | None = None) -> list[dict]:
    """One item per registered folder, the project's main one first: root, state, host, error, row_id, adopt.
    `rows` are the project's `project_roots` rows when the caller has read them already."""
    from .store_writer import label
    if rows is None:
        rows = store.query("SELECT id,path,machine,host FROM project_roots WHERE project_id=:project",
                           {"project": project["id"]})
    rows = {str(Path(row["path"]).resolve()): row for row in rows}
    # The order and resolution of ingest._roots, written out here so that module can import this one.
    roots = list(dict.fromkeys([Path(project["root"]).resolve(), *[Path(path) for path in rows]]))
    here, found = this_machine(), []
    for root in roots:
        row = rows.get(str(root)) or {}
        state, error = "available", None
        if here and row.get("machine") and row["machine"] != here:
            state = "elsewhere"  # decided before the disk is touched
        else:
            try:
                with os.scandir(root):
                    pass
            except (FileNotFoundError, NotADirectoryError):
                state = "missing"
            except OSError as exc:
                state, error = "unreadable", exc.strerror or type(exc).__name__
        found.append({"root": str(root), "state": state, "host": label(row["host"]) if row.get("host") else None,
                      "error": error, "row_id": row.get("id"),
                      "adopt": state == "available" and bool(row) and not row.get("machine")})
    return found


def message(project: dict, folder: dict) -> str:
    """The English sentence for one folder that is not available."""
    reason, values = _reason(folder)
    text = FOLDER_UNAVAILABLE.format(root=safe_text(folder["root"]), project=safe_text(project["name"]),
                                     reason=reason.format(**values))
    if folder["state"] == "elsewhere":
        from .process_control import quote_arg
        text += " " + FOLDER_MOVE.format(project=quote_arg(safe_text(project["name"])),
                                         root=quote_arg(safe_text(folder["root"])))
    return text


def entry(project: dict, folder: dict) -> dict:
    return {"project": project["name"], "root": folder["root"], "state": folder["state"], "host": folder.get("host"),
            "error": folder.get("error"), "message": message(project, folder)}


def unavailable(store, project_id: str | None = None) -> list[dict]:
    """The entries of every folder that is not available, for one project or for all, by project name then root."""
    projects = [store.resolve_project(project_id)] if project_id else store.list_projects()
    found = [entry(project, folder) for project in projects for folder in folder_states(store, project)
             if folder["state"] != "available"]
    return sorted(found, key=lambda item: (item["project"], item["root"]))


def adopt(store, row_ids: list[str]) -> None:
    """Stamp rows that have no machine with this one: the first complete index that read the folder claims it."""
    if row_ids and stamp():
        with store.transaction() as tx:
            tx.command("UPDATE project_roots SET machine=:machine,host=:host WHERE id IN :ids AND machine IS NULL",
                       {**stamp(), "ids": row_ids})

def require_here(store, project: dict, root: Path) -> None:
    """Refuse an act that writes into a folder that belongs to another machine."""
    for folder in folder_states(store, project):
        if folder["state"] == "elsewhere" and Path(folder["root"]) == root.resolve():
            raise ValueError(message(project, folder))


def folder_notice(entries: list[dict], language: str = "en") -> str:
    """The same sentences in the person's language, for the workspace."""
    from .terminal_i18n import tr
    parts = []
    for item in entries:
        reason, values = _reason(item)
        text = tr(FOLDER_UNAVAILABLE, language, root=safe_text(item["root"]), project=safe_text(item["project"]),
                  reason=tr(reason, language, **values))
        if item["state"] == "elsewhere":
            from .process_control import quote_arg
            text += " " + tr(FOLDER_MOVE, language, project=quote_arg(safe_text(item["project"])),
                             root=quote_arg(safe_text(item["root"])))
        parts.append(text)
    return " ".join(parts)
