"""Advisory ingestion locks, chunk writes, batched source transactions, and pending source indexing."""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import PurePosixPath

from cairnmind.agents.agent_memory import is_mcp_source
from cairnmind.memory.impact_verdicts import is_verdict_source
from cairnmind.memory.quote_healing import is_healing_audit_source
from cairnmind.pal.file_locks import lock_exclusive, unlock
from cairnmind.scip.ingest_walk import MANAGED_FILES, _chunks, _identifier, _metadata
from cairnmind.storage.arcade import ArcadeError
from cairnmind.storage.embed_jobs import queue_embed_jobs
from cairnmind.storage.journal import MAX_TRANSACTION_EVENTS
from cairnmind.storage.sql import json_text as _json_text
from cairnmind.storage.storage_query import (
    any_of,
    asc,
    db_find,
    db_find_one,
    db_group_counts,
    db_insert_document,
    db_update_where,
    eq,
    is_in,
    is_not_null,
    is_null,
    ne,
    not_in,
)

DEFAULT_FILES_PER_TRANSACTION = 8
DEFAULT_EVENTS_PER_TRANSACTION = 200  # far below the number at which the journal stops naming single rows
_KEPT_NODE_KINDS = ("source", "decision", "task", "fact", "constraint", "question", "preference", "checkpoint")
_KEPT_EDGE_KINDS = ("supported_by", "depends_on", "supersedes")


class _TooManyEvents(Exception):
    """A group wrote more journal events than EVENTS_PER_TRANSACTION; raised to roll it back."""


@contextmanager
def _ingestion_lock(
    store,
    category: str,
    identity: str,
    *,
    deadline: float | None = None,
    waited: list | None = None,
    lock_exclusive_fn=None,
    time_mod=None,
):
    """Serialize whole ingest operations across workers and CLI processes.

    Native database transactions protect writes, but cannot prevent an older
    filesystem scan from publishing its source snapshot after a newer scan.
    These advisory locks cover that read/parse/write operation as one unit.
    """
    acquire = lock_exclusive if lock_exclusive_fn is None else lock_exclusive_fn
    clock = time if time_mod is None else time_mod
    folder = store.data_dir / "locks"
    folder.mkdir(exist_ok=True, mode=0o700)
    filename = folder / f"{category}-{_identifier(identity)}.lock"
    descriptor = os.open(filename, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if deadline is None:
            try:
                acquire(descriptor, blocking=False)
            except BlockingIOError:
                if waited is not None:
                    waited.append(True)  # whoever held it may have changed what was read before
                acquire(descriptor, blocking=True)
        else:
            while True:
                if clock.monotonic() >= deadline:
                    raise TimeoutError("Transcript reparse deadline exceeded")
                try:
                    acquire(descriptor, blocking=False)
                    break
                except BlockingIOError:
                    clock.sleep(min(0.01, max(0, deadline - clock.monotonic())))
        yield
    finally:
        unlock(descriptor)
        os.close(descriptor)


def _write_chunks(
    store,
    project_id: str,
    source_id: str,
    text: str,
    metadata: dict | None = None,
    *,
    ingestion_lock_fn=None,
    chunks_fn=None,
) -> int:
    lock_fn = _ingestion_lock if ingestion_lock_fn is None else ingestion_lock_fn
    split_chunks = _chunks if chunks_fn is None else chunks_fn
    with lock_fn(store, "chunks", source_id), store.transaction() as tx:
        sources = db_find(
            tx,
            "sources",
            {"id": source_id},
            columns=("project_id", "is_current", "uri", "kind", "metadata"),
        )
        if not sources or sources[0]["project_id"] != project_id:
            raise ValueError("Chunk source does not belong to the requested project")
        if is_verdict_source(sources[0]) or is_mcp_source(sources[0]) or is_healing_audit_source(sources[0]):
            return 0  # a person's verdict or healing audit note is never searchable text
        if sources[0].get("is_current") is False:
            return 0  # replaced meanwhile: its chunks were deleted with it and are not written again (#186)
        existing = db_find(tx, "chunks", {"source_id": source_id}, columns=("id", "line_start", "line_end"))
        uri = sources[0]["uri"]
        if existing and PurePosixPath(uri.replace("\\", "/")).name in MANAGED_FILES and (
                {(row["line_start"], row["line_end"]) for row in existing}
                != {(first, last) for _, first, last in split_chunks(text, uri)}):
            # A block indexed before #313 leaves the index here, the next time the project is indexed.
            store.drop_chunks(tx, [source_id])
            store._delete_old_vectors(tx, [source_id])
            existing = []
        ids = []
        if not existing:
            for number, (content, first, last) in enumerate(split_chunks(text, uri)):
                chunk_id = _identifier(project_id, source_id, number, first, last, content)
                db_insert_document(
                    tx,
                    "chunks",
                    {
                        "id": chunk_id,
                        "project_id": project_id,
                        "source_id": source_id,
                        "text": content,
                        "line_start": first,
                        "line_end": last,
                        "metadata": metadata or {},
                        "is_current": sources[0]["is_current"],
                    },
                )
                ids.append(chunk_id)
        else:
            ids = [row["id"] for row in existing]
    # Replaying this is intentional: a crash after chunk commit but before
    # enqueue must not leave permanently unindexed evidence.
    queue_embed_jobs(store, project_id, ids)
    return 0 if existing else len(ids)


def _deactivate_together(store, tx, source_ids: list[str]) -> None:
    """What ``snapshot`` and ``_deactivate_source`` do to one replaced source, for several in one statement each."""
    db_update_where(
        tx,
        "sources",
        set_fields={"is_current": False, "current_key": None},
        where=[is_in("id", source_ids)],
    )
    store.drop_chunks(tx, source_ids)
    db_update_where(
        tx,
        "nodes",
        set_fields={"is_current": False},
        where=[is_in("source_id", source_ids), not_in("kind", _KEPT_NODE_KINDS)],
    )
    db_update_where(
        tx,
        "edges",
        set_fields={"is_current": False},
        where=[is_in("source_id", source_ids), not_in("kind", _KEPT_EDGE_KINDS)],
    )
    store._delete_old_vectors(tx, source_ids)


def _within_limit(tx, limit: int = DEFAULT_EVENTS_PER_TRANSACTION) -> None:
    """What was counted before the write can be out of date by then: checked again, before the commit."""
    if hasattr(tx, "pending") and len(tx.pending()) > limit:
        raise _TooManyEvents


def _vector_tables(store) -> int:
    return sum(
        1
        for row in db_find(store, "vector_indexes", columns=("type_name",))
        if str(row.get("type_name") or "").startswith("vector_")
    )


def _hanging_events(store, source_ids: list[str], vector_tables: int) -> dict[str, int]:
    """The journal events that deactivating each of these sources writes, counted before anything is written.

    One for the source, one per chunk it deletes, one per node and edge it deactivates, one per vector it
    deletes. On real code the graph rows of the old source outweigh what the new one writes.
    """
    weights = dict.fromkeys(source_ids, 1)
    if not source_ids:
        return weights
    live = any_of(eq("is_current", True), is_null("is_current"))
    for table, extra_where, times in (
            ("chunks", (), 1),
            ("nodes", (live, not_in("kind", _KEPT_NODE_KINDS)), 1),
            ("edges", (live, not_in("kind", _KEPT_EDGE_KINDS)), 1),
            ("embeddings", (), 1 + vector_tables)):
        for row in db_group_counts(
            store,
            table,
            ("source_id",),
            [is_in("source_id", source_ids), *extra_where],
            count_alias="n",
        ):
            if row.get("source_id") in weights:
                weights[row["source_id"]] += int(row.get("n") or 0) * times
    return weights


def _groups(items: list, cost, *, events_per_tx: int = DEFAULT_EVENTS_PER_TRANSACTION) -> list[list]:
    """Consecutive groups whose costs add up to at most EVENTS_PER_TRANSACTION; a costlier item stands alone."""
    groups, total = [], 0
    for item in items:
        if not groups or total + cost(item) > events_per_tx:
            groups.append([])
            total = 0
        groups[-1].append(item)
        total += cost(item)
    return groups


def _deactivate_sources(
    store,
    source_ids: list[str],
    *,
    files_per_tx: int = DEFAULT_FILES_PER_TRANSACTION,
    events_per_tx: int = DEFAULT_EVENTS_PER_TRANSACTION,
    hanging_events_fn=None,
    deactivate_source_fn=None,
    within_limit_fn=None,
) -> None:
    """Deactivate the sources of deleted files, up to FILES_PER_TRANSACTION in one transaction.

    The rows and the journal events are those of ``_deactivate_source`` for each. What hangs on each source is
    counted first, so that no group holds more than EVENTS_PER_TRANSACTION events; a source over it alone is
    deactivated as before. A group that wrote more all the same is rolled back and written source by source.
    """
    hanging_fn = _hanging_events if hanging_events_fn is None else hanging_events_fn
    deactivate_one = _deactivate_source if deactivate_source_fn is None else deactivate_source_fn
    vectors = _vector_tables(store) if source_ids else 0
    for offset in range(0, len(source_ids), files_per_tx):
        part = source_ids[offset:offset + files_per_tx]
        weights = hanging_fn(store, part, vectors)
        for group in _groups(part, weights.__getitem__, events_per_tx=events_per_tx):
            if len(group) == 1:
                deactivate_one(store, group[0])
                continue
            try:
                with store.transaction() as tx:
                    _deactivate_together(store, tx, group)
                    if within_limit_fn is None:
                        _within_limit(tx, events_per_tx)
                    else:
                        within_limit_fn(tx)
            except _TooManyEvents:
                for source_id in group:
                    deactivate_one(store, source_id)


def _write_files(
    store,
    project_id: str,
    batch: list[dict],
    *,
    events_per_tx: int = DEFAULT_EVENTS_PER_TRANSACTION,
    write_files_once_fn=None,
    write_files_fn=None,
) -> list:
    """Write the files of a group, source, chunks and jobs, in one transaction; return what was written.

    A file without a current source is written as ``snapshot_new`` writes it. A file with ``replaces``
    replaces that source as ``snapshot`` does: the old source and what hangs on it are deactivated and
    their vectors deleted. The rows and the journal events are those of ``snapshot``, ``_write_chunks``
    and ``enqueue``. A file is stored whole or not at all, so nothing is left for a later run to repair.

    Returns one source per file for a leading part of the batch, possibly all of it. The caller sizes groups
    from what it counted before; a group that wrote more journal events than EVENTS_PER_TRANSACTION all
    the same is rolled back and its first half tried alone, and the caller sends the rest again. A file
    alone may hold up to the journal's own limit, under which every event is still named, as it was when
    the file took ``snapshot``. None for a file means nothing was written for it and the caller takes the
    ordinary path: its URI had a current source other than the one it was to replace, or the file alone
    writes more events than that.
    """
    once = _write_files_once if write_files_once_fn is None else write_files_once_fn
    try:
        return once(
            store,
            project_id,
            batch,
            events_per_tx if len(batch) > 1 else MAX_TRANSACTION_EVENTS,
        )
    except _TooManyEvents:
        if len(batch) == 1:
            return [None]
        if write_files_fn is not None:
            return write_files_fn(store, project_id, batch[:len(batch) // 2])
        return _write_files(
            store,
            project_id,
            batch[:len(batch) // 2],
            events_per_tx=events_per_tx,
            write_files_once_fn=once,
        )


def _write_files_once(
    store,
    project_id: str,
    batch: list[dict],
    limit: int = DEFAULT_EVENTS_PER_TRANSACTION,
    *,
    within_limit_fn=None,
) -> list:
    check_limit = _within_limit if within_limit_fn is None else within_limit_fn
    sources = []
    with store.transaction() as tx:
        store._require_project(tx, project_id)
        keys = {item["uri"]: _json_text([project_id, item["uri"]]) for item in batch if item.get("replaces")}
        current = {
            row["current_key"]: row["id"]
            for row in db_find(
                tx,
                "sources",
                [is_in("current_key", list(keys.values())), eq("is_current", True)],
                columns=("id", "current_key"),
            )
        } if keys else {}
        # Replaced only when the source read before the walk is still the current one.
        replacing = {item["uri"]: item["replaces"] for item in batch
                     if item.get("replaces") and current.get(keys[item["uri"]]) == item["replaces"]}
        if replacing:
            _deactivate_together(store, tx, list(replacing.values()))
        for item in batch:
            if item.get("replaces"):
                source = (store.new_source(tx, project_id, item["uri"], item["content"], item["kind"], item["metadata"])
                          if item["uri"] in replacing else None)
            else:
                source = store.snapshot_new(tx, project_id, item["uri"], item["content"], item["kind"], item["metadata"])
            sources.append(source)
            if source is None or is_verdict_source(source) or is_mcp_source(source) or is_healing_audit_source(source):
                continue
            for number, (content, first, last) in enumerate(item["chunks"]):
                chunk_id = _identifier(project_id, source["id"], number, first, last, content)
                db_insert_document(
                    tx,
                    "chunks",
                    {
                        "id": chunk_id,
                        "project_id": project_id,
                        "source_id": source["id"],
                        "text": content,
                        "line_start": first,
                        "line_end": last,
                        "metadata": item["chunk_metadata"],
                        "is_current": True,
                    },
                )
                store.enqueue_new(tx, "embed", project_id, {"chunk_id": chunk_id}, f"embed:{chunk_id}")
            if item["extract"]:
                store.enqueue_new(tx, "extract", project_id, {"source_id": source["id"]}, f"extract:{source['id']}")
        check_limit(tx, limit)
    return sources


def _source_before(store, project_id: str, uri: str):
    return db_find_one(store, "sources", {"project_id": project_id, "uri": uri, "is_current": True})


def _deactivate_source(store, source_id: str) -> None:
    with store.transaction() as tx:
        db_update_where(
            tx,
            "sources",
            set_fields={"is_current": False, "current_key": None},
            where={"id": source_id},
        )
        store.drop_chunks(tx, [source_id])
        db_update_where(
            tx,
            "nodes",
            set_fields={"is_current": False},
            where=[eq("source_id", source_id), not_in("kind", _KEPT_NODE_KINDS)],
        )
        db_update_where(
            tx,
            "edges",
            set_fields={"is_current": False},
            where=[eq("source_id", source_id), not_in("kind", _KEPT_EDGE_KINDS)],
        )
        store._delete_old_vectors(tx, [source_id])


def index_pending_sources(store, limit: int = 100, *, write_chunks_fn=None) -> dict:
    """Index archived memory/evidence sources created outside file importers.

    CLI notes, verification evidence and checkpoints are already literal source
    snapshots; their content must participate in semantic retrieval too. This
    does not read their provenance paths or extract more facts from summaries.
    Unknown custom source kinds are eligible only when they explicitly provide
    a nonempty normalized text view and are not files/transcripts/generated
    answers. Unassigned inbox data never enters a project's search index.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValueError("Pending source batch limit must be between 1 and 1000")
    write_chunks = _write_chunks if write_chunks_fn is None else write_chunks_fn
    known_kinds = ("manual", "checkpoint", "verification", "test_result", "plan")
    excluded_kinds = ("code", "file", "transcript", "transcript_unknown", "agent_answer", "generated_answer")
    removed_projects = sorted({
        row["id"]
        for row in db_find(store, "projects", [is_not_null("removed_at")], columns=("id",))
        if row.get("id") is not None
    })
    source_where = [
        eq("is_current", True),
        is_not_null("project_id"),
        not_in("kind", excluded_kinds),
        any_of(is_in("kind", known_kinds), is_null("metadata.path")),
        # Third-party MCP events (agent_memory.is_mcp_source) are never chunked: not even loaded here.
        any_of(
            ne("kind", "agent_transcript"),
            is_null("metadata.origin_evidence"),
            ne("metadata.origin_evidence", "degraded"),
        ),
    ]
    if removed_projects:
        source_where.append(not_in("project_id", removed_projects))
    candidates = db_find(store, "sources", source_where, order_by=(asc("created_at"), asc("id")))  # #327
    chunked: set[str] = set()
    candidate_ids = [row["id"] for row in candidates if row.get("id") is not None]
    for offset in range(0, len(candidate_ids), 400):
        for chunk_row in db_find(
            store,
            "chunks",
            [is_in("source_id", candidate_ids[offset:offset + 400])],
            columns=("source_id",),
            distinct=True,
        ):
            if chunk_row.get("source_id") is not None:
                chunked.add(chunk_row["source_id"])
    rows = [row for row in candidates if row.get("id") not in chunked]
    counts = {"sources": 0, "chunks": 0, "errors": []}
    for row in rows:
        if is_verdict_source(row) or is_mcp_source(row) or is_healing_audit_source(row):
            continue  # an audit note on an impact list or quote healing: kept and exported, never chunked
        metadata = _metadata(row["metadata"])
        normalized = metadata.get("normalized_text")
        text = normalized if isinstance(normalized, str) and normalized.strip() else row["content"]
        if row["kind"] not in known_kinds and (
            not isinstance(normalized, str) or not normalized.strip() or metadata.get("derived")
        ):
            continue
        if not text.strip():
            continue
        try:
            added = write_chunks(store, row["project_id"], row["id"], text, {
                "source_kind": row["kind"], "role": metadata.get("role", "unknown"),
                "derived": row["kind"] == "checkpoint" or bool(metadata.get("derived")),
                "index_origin": "pending_sources",
            })
            counts["sources"] += 1
            counts["chunks"] += added
        except (OSError, ValueError, ArcadeError) as exc:
            counts["errors"].append(f"{row['id']}: {exc}")
        if counts["sources"] >= limit:
            break
    return counts
