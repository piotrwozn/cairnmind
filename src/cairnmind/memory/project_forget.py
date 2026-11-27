"""Deleting a project's whole memory (#347): a person's act, made in the workspace only.

The service route needs the open workspace's key (person_proof) and `delete` refuses without the proof; the command
line refuses (CLI_REFUSED) and opens no store. What remains of a deletion is one entry of the `deleted_projects` row
of `devbrain_meta`, written in the deletion's own transaction; backups do not carry it, as they do not carry the
writer row. Backups and the project's folders are never touched. The English sentences are the source; MESSAGES is
their Polish for the terminal catalog.
"""
from __future__ import annotations

import socket
from pathlib import Path

from cairnmind.cli_cmds.first_start import UNKNOWN_PROJECT_NEXT
from cairnmind.storage.sql import insert_document, utc_now
from cairnmind.storage.storage_query import db_find_one, db_update_where
from cairnmind.pal.terminal_safe import safe_text

HISTORY = "deleted_projects"
HISTORY_LIMIT = 100
COUNTS = ("records", "plans", "sources", "sessions", "check_runs", "chunks", "nodes", "embeddings")
KEPT_COUNTS = ("records", "plans", "sources", "sessions", "check_runs")  # what the audit entry keeps
CLI_REFUSED = (
    "The command line no longer deletes a project's memory. Nothing was deleted. The person deletes it in the "
    "workspace (`cairnmind`): More, then Delete project memory, which names the project and what is deleted and "
    "asks before deleting. `cairnmind project remove PROJECT --yes` takes a project out of the registry and keeps "
    "its memory.")
MESSAGES = {CLI_REFUSED: (
    "Wiersz poleceń nie kasuje już pamięci projektu. Niczego nie skasowano. Osoba kasuje ją w środowisku "
    "terminalowym (`cairnmind`): Więcej, potem Skasuj pamięć projektu, które podaje projekt i to, co zostanie "
    "skasowane, i pyta przed skasowaniem. `cairnmind project remove PROJEKT --yes` usuwa projekt z rejestru i "
    "zachowuje jego pamięć.")}


def _row(reader, project_id: str) -> dict:
    """The project by its id (a removed one too): the workspace sends ids, never a name to be guessed."""
    row = db_find_one(reader, "projects", {"id": project_id})
    if not row:
        raise ValueError(f"Unknown project: {project_id}. {UNKNOWN_PROJECT_NEXT}")
    return row


def named(row: dict) -> dict:
    return {"id": row["id"], "name": row.get("name") or row.get("removed_name"),
            "root": row.get("root") or row.get("removed_root"), "removed_at": row.get("removed_at")}


def deletable(store) -> dict:
    """Every project that has memory in the store: the registered ones, then the removed ones."""
    from cairnmind.memory.project_removal import removed_rows
    return {"items": [named(row) for row in store.list_projects()] + [named(row) for row in removed_rows(store)]}


def plan(settings, store, project_id: str) -> dict:
    """What deleting would delete, after the checks of the deletion itself; changes nothing."""
    from cairnmind.agents import answer_log
    from cairnmind.memory import project_removal
    from cairnmind.storage.store_writer import require_writer
    require_writer(store)
    project = named(_row(store, project_id))
    project_removal.require_idle(settings, store, project)
    counts = store.status(project_id)
    log = answer_log.log_path(settings, project_id)
    return {"project": project, "deletes": {key: counts[key] for key in COUNTS},
            "answer_log": str(log) if log.exists() else None, "backups": str(Path(settings.data_dir) / "backups")}


def delete(settings, store, project_id: str) -> dict:
    """Delete the project's memory in one transaction that reads the project and its jobs again, and keep the trace."""
    from cairnmind.agents import answer_log
    from cairnmind.memory import project_removal
    from cairnmind.storage import store_writer
    from cairnmind.memory.confirmation import ConfirmationRefused
    from cairnmind.scip.ingest import forget_index_signature
    from cairnmind.terminal.person_proof import FORGET_REFUSED, present
    if not present():
        raise ConfirmationRefused(FORGET_REFUSED)  # before anything is read
    shown = plan(settings, store, project_id)
    with store.transaction() as tx:
        row = named(_row(tx, project_id))
        project_removal._jobs_in_work(tx, {**row, "name": row["name"] or row["id"]})
        store.forget_project(project_id)  # the same transaction
        # No name and no folder: the person asked for this project to be gone, so the trace does not keep them.
        _record(tx, {"project_id": project_id,
                     "removed_at": shown["project"]["removed_at"], "deleted_at": utc_now(),
                     "host": store_writer.label(socket.gethostname()),
                     "counts": {key: shown["deletes"][key] for key in KEPT_COUNTS}, "route": "workspace"})
    forget_index_signature(store, project_id)
    result = {"deleted": project_id, "project": shown["project"], "deleted_counts": shown["deletes"],
              "answer_log_deleted": False, "backups_unchanged": True}
    try:
        result["answer_log_deleted"] = answer_log.forget(settings, project_id)
    except OSError as exc:  # said, never silent: the store is already deleted
        result["answer_log_error"] = safe_text(str(exc), limit=300)
    return result


def _record(tx, entry: dict) -> None:
    row = db_find_one(tx, "devbrain_meta", {"name": HISTORY})
    history = [*((row or {}).get("history") or []), entry][-HISTORY_LIMIT:]
    if row:
        db_update_where(tx, "devbrain_meta", set_fields={"history": history}, where={"name": HISTORY})
    else:
        insert_document(tx, "devbrain_meta", {"name": HISTORY, "history": history})


def history(reader) -> list[dict]:
    row = db_find_one(reader, "devbrain_meta", {"name": HISTORY})
    return [entry for entry in (row or {}).get("history") or [] if isinstance(entry, dict)]
