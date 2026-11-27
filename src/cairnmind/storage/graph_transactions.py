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
reads the vertices back by that index before the edges that name them are written; the restore (#362) commits its
vertices, waits once with no transaction open and only then begins the transactions of its edges (a transaction that
has missed a vertex keeps missing it, whatever is committed after; a group that still misses one is committed, waited
for and written again in a new transaction, once), and shares ``BoundedTransactions`` with the indexer.
"""
from __future__ import annotations

from contextlib import ExitStack
import sys
import time

from cairnmind.storage.arcade import ArcadeError
from cairnmind.storage.storage_query import db_find, is_in

ROWS_PER_TRANSACTION = 20000
VERTEX_WAIT = 60.0  # seconds the engine's index may take to find the vertices that were committed
DUPLICATE_WAIT = 5.0  # seconds a pass waits after the index refused a vertex of a deleted id, before it writes the graph once more


class BoundedTransactions:
    """The open transaction of a write; ``wrote`` counts what went into it and, at the bound, commits it.

    The one helper of the indexer's graph write (``GraphTransactions``) and of the restore's (``restore_staged.Batches``,
    #362): each says in ``begin`` how the journal is reset, in ``full`` when to commit, and in ``timeout`` what the
    commit waits (the restore's 5,000 rows, 8 MiB or 5 s against 20,000 rows here: the client's wait is the reason)."""
    timeout = ()  # arguments of Store.transaction

    def __init__(self, store):
        self.store, self.commits, self._stack = store, 0, None
        self.open()

    def begin(self, tx):
        raise NotImplementedError

    def full(self):
        raise NotImplementedError

    def open(self):
        stack = ExitStack()
        try:
            self.tx = stack.enter_context(self.store.transaction(*self.timeout))
            self.begin(self.tx)
        except BaseException:
            stack.close()
            raise
        self._stack, self.rows, self.size, self.opened = stack, 0, 0, time.monotonic()

    def commit(self):
        """Commit the open transaction; none is open afterwards."""
        stack, self._stack = self._stack, None
        if stack is not None:
            stack.close()
            self.commits += 1

    def wrote(self, rows, size=0):
        self.rows += rows
        self.size += size
        if self.full():
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


class GraphTransactions(BoundedTransactions):
    """The graph write of one project's indexing: its journal reset is the project's graph, its bound the rows."""

    def __init__(self, store, project_id):
        self.project_id = project_id
        super().__init__(store)

    def begin(self, tx):
        from cairnmind.storage.journal import Journal
        Journal.touch(tx, self.project_id, "graph", op="reset")

    def full(self):
        return self.rows >= ROWS_PER_TRANSACTION


def refused_by_id_index(exc):
    """Whether the engine's index of ``nodes.id`` is why a write failed, after a vertex of that id was deleted shortly before (#433).

    A vertex deleted in an earlier pass and written again by the next one (a directory renamed and renamed back) meets it
    in one of two ways, and the index cannot be asked about either: the commit is refused with a duplicated key, or the
    edges that name the re-created vertex find no endpoint (``Store.write_graph`` counts what it wrote).

    The engine names the index in its ``exceptionArgs`` ("nodes[id]|[key]|RID") in both modes; its ``detail`` text, which
    production mode leaves out ("Found duplicate key in index"), is not read."""
    if isinstance(exc, ArcadeError):
        return (exc.status_code == 409 and exc.exception_name.endswith(".DuplicatedKeyException")
                and exc.exception_args.split("|")[0] == "nodes[id]")
    return isinstance(exc, RuntimeError) and "did not create every graph edge" in str(exc)


def await_vertices(store, identifiers, *timeout):
    """Return when a new transaction finds every vertex of ``identifiers`` by id; fail after ``VERTEX_WAIT`` seconds.

    Call it with no transaction open: inside one, the transaction is reused and keeps missing what it missed.
    ``timeout`` is what the commit of each read waits (arguments of Store.transaction)."""
    missing, deadline = sorted(identifiers), time.monotonic() + VERTEX_WAIT
    while missing:
        still = []
        with store.transaction(*timeout) as tx:
            for offset in range(0, len(missing), 1000):
                part = missing[offset:offset + 1000]
                found = {row["id"] for row in db_find(tx, "nodes", [is_in("id", part)], columns=("id",))}
                still.extend(identifier for identifier in part if identifier not in found)
        missing = still
        if missing:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"The database did not find {len(missing)} graph vertices it had committed")
            time.sleep(1)
