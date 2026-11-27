"""Delete the chunks that earlier releases kept for sources that are no longer current (#186).

Since #186 the chunks of a source are deleted in the transaction that replaces or deactivates it
(``Store.drop_chunks``). A store written before keeps them, flagged ``is_current=false``: nothing searches them,
yet they hold a second copy of every old file version and stay in the full-text index. This removes them.

It covers every project in the store, and sources with no project. Only chunks go, with any vector left on
them. Every source row stays, with its full text and hash, so evidence that cites an old version is still
readable and checkable; no record, revision, check run, node or edge is touched.

Bounded and resumable: each transaction deletes at most ``batch`` rows (chunks or vectors) and holds the writer
lock only for that long, so other writers go on in between. Nothing is remembered between runs: a run that is stopped, or limited
with ``max_batches``, leaves only rows that the next run finds and deletes the same way.
"""
from __future__ import annotations

from cairnmind.storage.sql import IDENTIFIER
from cairnmind.storage.storage_query import asc, db_delete_where, db_find, eq, gt, is_in

SOURCES_PER_PAGE = 200
CHUNKS_PER_TRANSACTION = 200  # below the journal's limit, under which every deleted row is still named


def _tables(store) -> list[tuple[str, str]]:
    """(table, report key) of every table that can hold rows of a source: chunks first, then each vector copy."""
    tables = [("chunks", "chunks"), ("embeddings", "embeddings")]
    if getattr(getattr(store, "backend", None), "backend_kind", "arcade") == "sqlite":
        return tables
    for row in db_find(store, "vector_indexes", columns=("type_name",)):
        name = row.get("type_name")
        if name and IDENTIFIER.fullmatch(name) and name.startswith("vector_"):
            tables.append((name, "vector_rows"))
    return tables


def drop_replaced_chunks(store, *, batch: int = CHUNKS_PER_TRANSACTION, max_batches: int | None = None) -> dict:
    """Delete the chunks (and any vectors) of sources that are not current, in every project; return what went.

    ``removed`` counts the deleted rows; ``sources`` the sources that lost at least one row in this run;
    ``transactions`` every transaction that deleted rows, vector clean-up included, each deleting at most
    ``batch`` rows. ``complete`` is true when no such row was left when the run ended; false when ``max_batches``
    stopped it with rows still to delete."""
    if type(batch) is not int or not 1 <= batch <= 1000:
        raise ValueError("The batch must be a whole number from 1 to 1000")
    if max_batches is not None and (type(max_batches) is not int or max_batches < 1):
        raise ValueError("The number of batches must be a whole number of at least 1")
    result = {"removed": {"chunks": 0, "embeddings": 0, "vector_rows": 0}, "sources": 0, "transactions": 0,
              "complete": False}
    tables = _tables(store)
    after = ""
    while True:
        page = [
            row["id"]
            for row in db_find(
                store,
                "sources",
                [eq("is_current", False), gt("id", after)],
                columns=("id",),
                order_by=(asc("id"),),
                limit=SOURCES_PER_PAGE,
            )
        ]
        if not page:
            result["complete"] = True
            return result
        after, pending, touched = page[-1], list(page), set()
        for table, key in tables:
            while pending:
                found = db_find(
                    store,
                    table,
                    is_in("source_id", pending),
                    columns=("id", "source_id"),
                    limit=batch,
                )
                if not found:
                    break
                if max_batches is not None and result["transactions"] >= max_batches:
                    # Stopped with rows found still to delete, so the run is not complete.
                    result["sources"] += len(touched)
                    return result
                with store.transaction() as tx:
                    # Checked again under the writer lock: a source made current meanwhile keeps its rows.
                    still = {
                        row["id"]
                        for row in db_find(
                            tx,
                            "sources",
                            [is_in("id", sorted({row["source_id"] for row in found})), eq("is_current", False)],
                            columns=("id",),
                        )
                    }
                    doomed = [row["id"] for row in found if row["source_id"] in still]
                    if doomed:
                        db_delete_where(tx, table, is_in("id", doomed))
                result["transactions"] += 1
                result["removed"][key] += len(doomed)
                touched.update(row["source_id"] for row in found if row["source_id"] in still)
                kept = {row["source_id"] for row in found} - still
                pending = [identifier for identifier in pending if identifier not in kept]
        result["sources"] += len(touched)
