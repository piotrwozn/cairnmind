"""Embedding jobs queued and repaired in groups, not one transaction per chunk (#363)."""
from __future__ import annotations

from .sql import json_text, update_document, utc_now

# Keys per IN list, as memory_egress.held_chunk_ids reads chunks.
READ_BATCH = 500
# Jobs written per transaction: far below journal.MAX_TRANSACTION_EVENTS (500), so every journal event
# still names its job, as ingest.EVENTS_PER_TRANSACTION keeps indexing's.
WRITE_BATCH = 200


def _key(project_id: str, dedupe_key: str) -> str:
    return json_text(["embed", project_id, dedupe_key])  # the job_key Store.enqueue writes


def _jobs(reader, keys: list[str]) -> dict[str, dict]:
    """The embed jobs of these keys, read ``READ_BATCH`` keys per request: job_key -> {id, job_key, state}."""
    found = {}
    for start in range(0, len(keys), READ_BATCH):
        for row in reader.query("SELECT id,job_key,state FROM jobs WHERE job_key IN :keys",
                                {"keys": keys[start:start + READ_BATCH]}):
            found[row["job_key"]] = row
    return found


def _ensure(store, wanted: dict[str, tuple[str, str, str]]) -> dict[str, dict]:
    """Make sure a job exists for every key; return job_key -> {id, state} for all of them.

    ``wanted`` maps a job_key to (project_id, chunk_id, dedupe_key). The keys are read first, outside a
    transaction; only the missing ones are written, ``WRITE_BATCH`` per transaction, and inside it they are
    read again (another thread of this process may have queued one meanwhile; the unique index on job_key
    refuses a second row in any case). An unknown project raises ValueError, as ``Store.enqueue`` does.
    """
    keys = list(wanted)
    found = _jobs(store, keys)
    missing = [key for key in keys if key not in found]
    for start in range(0, len(missing), WRITE_BATCH):
        part = missing[start:start + WRITE_BATCH]
        with store.transaction() as tx:
            again = _jobs(tx, part)
            found.update(again)
            new = [key for key in part if key not in again]
            for project_id in {wanted[key][0] for key in new}:
                store._require_project(tx, project_id)
            for key in new:
                project_id, chunk_id, dedupe = wanted[key]
                found[key] = {"id": store.enqueue_new(tx, "embed", project_id, {"chunk_id": chunk_id}, dedupe),
                              "state": "pending"}
    return found


def queue_embed_jobs(store, project_id: str, chunk_ids: list[str]) -> None:
    """For each chunk what ``store.enqueue("embed", project_id, {"chunk_id": c}, f"embed:{c}")`` did."""
    _ensure(store, {_key(project_id, f"embed:{c}"): (project_id, c, f"embed:{c}") for c in chunk_ids})


def repair_embed_jobs(store, rows: list[dict], namespace: str) -> None:
    """For chunk rows ({id, project_id}) without a vector in ``namespace``, make sure each has a job that will run.

    The job of the key ingest uses exists; where it failed (it stays failed until retry-jobs) the chunk gets a
    job per namespace; a job that is completed while its chunk still has no vector is pending again.
    Nothing is written when every job exists and none is completed.
    """
    first = {_key(r["project_id"], f"embed:{r['id']}"): (r["project_id"], r["id"], f"embed:{r['id']}") for r in rows}
    jobs = _ensure(store, first)
    chosen, second = {}, {}
    for key, (project_id, chunk, _) in first.items():
        if jobs[key]["state"] == "failed":
            dedupe = f"embedding-namespace:{namespace}:{chunk}"
            second[_key(project_id, dedupe)] = (project_id, chunk, dedupe)
        else:
            chosen[chunk] = jobs[key]
    if second:
        more = _ensure(store, second)
        chosen.update({chunk: more[key] for key, (_, chunk, _) in second.items()})
    done = [(chunk, job["id"]) for chunk, job in chosen.items() if job["state"] == "completed"]
    for start in range(0, len(done), WRITE_BATCH):
        part = done[start:start + WRITE_BATCH]
        with store.transaction() as tx:
            have = {r["chunk_id"] for r in tx.query(
                "SELECT chunk_id FROM embeddings WHERE namespace=:namespace AND chunk_id IN :chunks",
                {"namespace": namespace, "chunks": [chunk for chunk, _ in part]})}
            ids = [job for chunk, job in part if chunk not in have]
            if ids:
                update_document(tx, "jobs", {"state": "pending", "error": None, "lease_until": None,
                                             "updated_at": utc_now()}, "id IN :ids AND state='completed'", {"ids": ids})
