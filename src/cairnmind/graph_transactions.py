"""The graph of a large project written in transactions of bounded size (#377).

The engine keeps the pages a transaction changed in its heap until the commit. On its default heap of 1 GB, one
transaction holding the graph of a project of about 330,000 vertices (and 410,000 edges) ran out of memory (rows with
more metadata failed from 120,000 vertices), so the graph is written in transactions of at most
``ROWS_PER_TRANSACTION`` rows. A graph that fits in one is written in one, all or nothing, as before.

Split, it is no longer all or nothing, so the project's ``graph_fingerprint`` is cleared in the first transaction and
set again in the last: until then the readers that check it (impact, ``memory status``, the inspector's graph) say
that the code graph is not built (``code_graph_pending``), and a write that stops half way leaves that mark, so the
next indexing replaces the whole graph.

The engine's unique index on ``nodes.id`` does not find vertices that were committed a moment ago while the engine
compacts the index (seen on ArcadeDB 26.11.1; seconds, and not with compaction switched off). ``await_vertices``
reads the vertices back by that index before the edges that name them are written.

This is the small local counterpart of ``Batches`` in restore_staged.py (#362): when both are on main they are to be
one helper.
"""
from __future__ import annotations

from contextlib import ExitStack
import sys
import time

ROWS_PER_TRANSACTION = 20000
VERTEX_WAIT = 60.0  # seconds the engine's index may take to find the vertices that were committed


class GraphTransactions:
    """The open transaction of a graph write; ``wrote`` counts rows into it and, at the bound, commits it."""

    def __init__(self, store, project_id):
        self.store, self.project_id, self.commits, self._stack = store, project_id, 0, None
        self.open()

    def open(self):
        from .journal import Journal
        stack = ExitStack()
        try:
            self.tx = stack.enter_context(self.store.transaction())
            Journal.touch(self.tx, self.project_id, "graph", op="reset")
        except BaseException:
            stack.close()
            raise
        self._stack, self.rows = stack, 0

    def commit(self):
        """Commit the open transaction; none is open afterwards."""
        stack, self._stack = self._stack, None
        if stack is not None:
            stack.close()
            self.commits += 1

    def wrote(self, rows):
        self.rows += rows
        if self.rows >= ROWS_PER_TRANSACTION:
            self.commit()
            self.open()

    def abandon(self):
        """Roll the open transaction back, inside the handler of the error that ends the write. Never raises."""
        stack, self._stack = self._stack, None
        if stack is not None:
            try:
                stack.__exit__(*sys.exc_info())
            except Exception:  # noqa: BLE001 - the first error is the one the person needs
                pass


def await_vertices(store, identifiers):
    """Return when a new transaction finds every vertex of ``identifiers`` by id; fail after ``VERTEX_WAIT`` seconds."""
    missing, deadline = sorted(identifiers), time.monotonic() + VERTEX_WAIT
    while missing:
        still = []
        with store.transaction() as tx:
            for offset in range(0, len(missing), 1000):
                part = missing[offset:offset + 1000]
                found = {row["id"] for row in tx.query("SELECT id FROM nodes WHERE id IN :ids", {"ids": part})}
                still.extend(identifier for identifier in part if identifier not in found)
        missing = still
        if missing:
            if time.monotonic() > deadline:
                raise RuntimeError(f"The database did not find {len(missing)} graph vertices it had committed")
            time.sleep(1)
