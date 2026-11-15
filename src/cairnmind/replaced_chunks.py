"""Delete the chunks that earlier releases kept for sources that are no longer current (#186).

Since #186 the chunks of a source are deleted in the transaction that replaces or deactivates it
(``Store.drop_chunks``). A store written before keeps them, flagged ``is_current=false``: nothing searches them,
yet they hold a second copy of every old file version and stay in the full-text index. This removes them.

Only chunks go, with any vector left on them. Every source row stays, with its full text and hash, so evidence
that cites an old version is still readable and checkable; no record, revision, check run, node or edge is touched.

Bounded and resumable: each transaction deletes at most ``batch`` chunks and holds the writer lock only for that
long, so other writers go on in between. Nothing is remembered between runs: a run that is stopped, or limited
with ``max_batches``, leaves only rows that the next run finds and deletes the same way.
"""
from __future__ import annotations

from .sql import IDENTIFIER, quote_name

SOURCES_PER_PAGE = 200
CHUNKS_PER_TRANSACTION = 200  # below the journal's limit, under which every deleted row is still named


def _count(tx, sql: str, params: dict) -> int:
    rows = tx.query(sql, params)
    return int(rows[0].get("n") or 0) if rows else 0


def _drop_vectors(store, source_ids: list[str]) -> tuple[int, int]:
    """Vectors of sources that are not current; (embeddings, vector rows) deleted. Usually none are left."""
    # Both copies of a vector are written together; with no embedding row there is no vector row to look for.
    if not store.query("SELECT id FROM embeddings WHERE source_id IN :ids LIMIT 1", {"ids": source_ids}):
        return 0, 0
    with store.transaction() as tx:
        still = [row["id"] for row in tx.query("SELECT id FROM sources WHERE id IN :ids AND is_current=false",
                                               {"ids": source_ids})]
        if not still:
            return 0, 0
        embeddings = _count(tx, "SELECT count(*) AS n FROM embeddings WHERE source_id IN :ids", {"ids": still})
        vectors = 0
        for row in tx.query("SELECT type_name FROM vector_indexes"):
            name = row.get("type_name")
            if name and IDENTIFIER.fullmatch(name) and name.startswith("vector_"):
                vectors += _count(tx, f"SELECT count(*) AS n FROM {quote_name(name)} WHERE source_id IN :ids",
                                  {"ids": still})
        store._delete_old_vectors(tx, still)
    return embeddings, vectors


def drop_replaced_chunks(store, *, batch: int = CHUNKS_PER_TRANSACTION, max_batches: int | None = None) -> dict:
    """Delete the chunks (and any vectors) of sources that are not current; return how many rows went.

    ``complete`` is true when no such chunk was left when the run ended; false when ``max_batches`` stopped it."""
    if type(batch) is not int or not 1 <= batch <= 1000:
        raise ValueError("The batch must be a whole number from 1 to 1000")
    if max_batches is not None and (type(max_batches) is not int or max_batches < 1):
        raise ValueError("The number of batches must be a whole number of at least 1")
    result = {"removed": {"chunks": 0, "embeddings": 0, "vector_rows": 0}, "sources": 0, "transactions": 0,
              "complete": False}
    after = ""
    while True:
        page = [row["id"] for row in store.query(
            "SELECT id FROM sources WHERE is_current=false AND id > :last ORDER BY id LIMIT :limit",
            {"last": after, "limit": SOURCES_PER_PAGE})]
        if not page:
            result["complete"] = True
            return result
        after, pending, touched = page[-1], list(page), set()
        while pending:
            if max_batches is not None and result["transactions"] >= max_batches:
                return result
            found = store.query("SELECT id,source_id FROM chunks WHERE source_id IN :ids LIMIT :limit",
                                {"ids": pending, "limit": batch})
            if not found:
                break
            with store.transaction() as tx:
                # Checked again under the writer lock: a source made current meanwhile keeps its chunks.
                still = {row["id"] for row in tx.query(
                    "SELECT id FROM sources WHERE id IN :ids AND is_current=false",
                    {"ids": sorted({row["source_id"] for row in found})})}
                doomed = [row["id"] for row in found if row["source_id"] in still]
                if doomed:
                    tx.command("DELETE FROM chunks WHERE id IN :ids", {"ids": doomed})
            result["transactions"] += 1
            result["removed"]["chunks"] += len(doomed)
            touched.update(row["source_id"] for row in found if row["source_id"] in still)
            kept = {row["source_id"] for row in found} - still
            pending = [identifier for identifier in pending if identifier not in kept]
        embeddings, vectors = _drop_vectors(store, page)
        result["removed"]["embeddings"] += embeddings
        result["removed"]["vector_rows"] += vectors
        result["sources"] += len(touched)
