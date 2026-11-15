"""Bound what the store and the data directory keep without end (#190): old job history and safety backups.

Completed jobs. A job row is never deleted here, because it is read long after it finished:

- its ``job_key`` is the deduplication key of every later ``enqueue``. A snapshot that cannot reuse its
  remembered files enqueues the extraction of every current document again, and ``_write_chunks`` enqueues
  the embedding of every existing chunk again; while the completed row exists, each such call returns it.
  Without the row, each would queue the work, and run the model, a second time;
- ``status``, ``job_summary``, the memory snapshot and the session packet count rows by kind and state
  (``processed`` is the number of completed extractions);
- import coverage reads the import job's ``payload`` (with the published resume packets, which no prune
  touches); the release of extractions held back for a remote model and the retry of an extraction whose
  records were all rejected read ``diagnostics`` and ``payload`` of completed extractions.

So an old completed job keeps every field and loses only its ``attempt_history``, the one field that grows
with each attempt (each event carries the worker, the error and a copy of the diagnostics). The number of
events it had is kept in ``attempt_history_pruned``, and ``compacted_at`` says when. ``updated_at`` does not
change, so the order of the job list and the age of the job stay as they were. Failed, pending and running
jobs are never touched. Only the writer installation runs it, as any write (#70).

Safety backups. ``before-schema-*`` and ``before-restore-*`` copies are made by the store before it changes
itself, and nothing deletes them on its own: ``prune_plan`` says which of them a person could delete, keeping
the newest ``keep`` of each kind, and ``delete_backups`` deletes exactly the files of such a plan. The command
line asks first and runs only for a person (``cairnmind store prune-backups``).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path

from .config import BACKUP_SUFFIXES

JOBS_PER_TRANSACTION = 200  # below the journal's limit, under which every changed row is still named
DEFAULT_JOB_HISTORY_DAYS = 30
MAX_JOB_HISTORY_DAYS = 3650
SAFETY_KINDS = {"before-schema-": "before_schema", "before-restore-": "before_restore"}
# Never compacted, or changed since: a job queued again after a compaction (a retry, a release of held work)
# gets a new updated_at, and once it is completed and old again its new history is removed too.
_NOT_COMPACTED = "(compacted_at IS NULL OR compacted_at < updated_at)"


def valid_days(days) -> int | None:
    """The age in days after which a completed job loses its history, or None when nothing is to be compacted.

    0 turns compaction off. A value that is not a whole number of days between 0 and MAX_JOB_HISTORY_DAYS
    also compacts nothing: a mistyped setting must not remove anything."""
    if type(days) is not int or not 0 < days <= MAX_JOB_HISTORY_DAYS:
        return None
    return days


def compact_old_jobs(store, older_than_days: int, *, now: datetime | None = None,
                     batch: int = JOBS_PER_TRANSACTION) -> dict:
    """Remove the attempt history of completed jobs last changed more than ``older_than_days`` ago.

    Returns ``{"compacted": jobs, "events": attempt events removed, "transactions": n, "cutoff": iso}``.
    Bounded and resumable: one page of at most ``batch`` jobs per transaction, re-checked inside it."""
    if valid_days(older_than_days) is None:
        raise ValueError(f"older_than_days must be a whole number from 1 to {MAX_JOB_HISTORY_DAYS}")
    if type(batch) is not int or not 1 <= batch <= 1000:
        raise ValueError("batch must be a whole number from 1 to 1000")
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=older_than_days)).isoformat(timespec="microseconds")
    report = {"compacted": 0, "events": 0, "transactions": 0, "cutoff": cutoff}
    after = ""
    while True:
        page = store.query(
            "SELECT id FROM jobs WHERE id > :position AND state='completed' "
            f"AND updated_at < :cutoff AND {_NOT_COMPACTED} ORDER BY id LIMIT :limit",
            {"position": after, "cutoff": cutoff, "limit": batch})
        if not page:
            return report
        after = page[-1]["id"]
        changed = _compact(store, [row["id"] for row in page], cutoff)
        report["compacted"] += changed[0]
        report["events"] += changed[1]
        report["transactions"] += 1
        if len(page) < batch:
            return report


def _compact(store, ids, cutoff) -> tuple[int, int]:
    from .sql import update_document, utc_now
    compacted = events = 0
    at = utc_now()
    with store.transaction() as tx:
        # Read again under the writer lock: a job retried or released since the page was read is pending now.
        for row in tx.query("SELECT id, attempt_history, attempt_history_pruned FROM jobs WHERE id IN :ids "
                            f"AND state='completed' AND updated_at < :cutoff AND {_NOT_COMPACTED}",
                            {"ids": ids, "cutoff": cutoff}):
            history = row.get("attempt_history") or []
            earlier = row.get("attempt_history_pruned")
            removed = len(history) if isinstance(history, list) else 1
            update_document(tx, "jobs", {"attempt_history": [], "compacted_at": at,
                                         "attempt_history_pruned": removed + (earlier if type(earlier) is int else 0)},
                            "id=:id AND state='completed'", {"id": row["id"]})
            compacted += 1
            events += removed
    return compacted, events


def safety_backups(directory) -> list[dict]:
    """The safety backups in ``directory``, newest first: name, kind, bytes and the time they were last written.

    Only regular files named ``before-schema-*`` or ``before-restore-*`` with a backup suffix; a link or a
    directory with such a name is not listed, and so never deleted."""
    directory, found = Path(directory), []
    if not directory.is_dir() or directory.is_symlink():
        return []
    for path in directory.iterdir():
        kind = next((name for prefix, name in SAFETY_KINDS.items() if path.name.startswith(prefix)), None)
        if kind is None or not path.name.endswith(BACKUP_SUFFIXES):
            continue
        try:
            info = os.lstat(path)
        except OSError:
            continue  # removed meanwhile
        if not path.is_file() or path.is_symlink():
            continue
        found.append({"name": path.name, "kind": kind, "bytes": info.st_size, "mtime": info.st_mtime,
                      "modified": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(timespec="seconds")})
    return sorted(found, key=lambda entry: (-entry["mtime"], entry["name"]))


def prune_plan(directory, keep: int) -> dict:
    """Which safety backups ``delete_backups`` would delete: all but the newest ``keep`` of each kind."""
    if type(keep) is not int or keep < 0:
        raise ValueError("keep must be a whole number of backups, 0 or more")
    kept, deleted, seen = [], [], {}
    for entry in safety_backups(directory):
        seen[entry["kind"]] = seen.get(entry["kind"], 0) + 1
        (kept if seen[entry["kind"]] <= keep else deleted).append(entry)
    return {"directory": str(Path(directory)), "keep": keep, "kept": kept, "delete": deleted,
            "delete_bytes": sum(entry["bytes"] for entry in deleted)}


def delete_backups(directory, plan: dict) -> dict:
    """Delete the files a plan names, each only if it is still the same safety backup the plan listed."""
    directory, current = Path(directory), {entry["name"]: entry for entry in safety_backups(directory)}
    deleted, skipped = [], []
    for entry in plan["delete"]:
        now = current.get(entry["name"])
        # Written again since the plan was shown (same name, other content): it is not the file the person saw.
        if now is None or now["mtime"] != entry["mtime"] or now["bytes"] != entry["bytes"]:
            skipped.append(entry["name"])
            continue
        (directory / entry["name"]).unlink(missing_ok=True)
        deleted.append(entry["name"])
    return {"deleted": deleted, "skipped": skipped,
            "deleted_bytes": sum(entry["bytes"] for entry in plan["delete"] if entry["name"] in deleted)}
