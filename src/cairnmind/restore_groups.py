"""Restore writes the rows of a validated backup in groups, a few hundred per request (#219).

Row by row, a restore of a store of 26,000 graph vertices and 32,000 edges took 297,454 requests: every
vertex and edge was looked up before it was written. Here the same rows go in the transactions of
``restore_staged.Batches`` (#362), which commits after a bounded number of rows, never between a group's
reads and its write:

- Documents and vector rows: one ``INSERT ... CONTENT :rows`` per group of one type (``insert_documents``),
  in file order, a group ending at ``GRAPH_ROWS_PER_REQUEST`` rows or ``DOCUMENT_BYTES_PER_REQUEST`` of text.
- Graph vertices, then edges: per project, the reads of ``Store.prefetch_graph`` and the grouped writes of
  ``Store.write_graph`` (#167), with every check of ``upsert_node`` / ``upsert_edge``, a group ending at the
  same row and text limits. The restore's transaction is reset, so the journal needs none of these rows.

A group holds rows read in pieces from the file, never the file (#196). A group that fails, or that the
database stores only in part, fails the transaction, which leaves the store as it was.
"""
from __future__ import annotations

from .arcade import ArcadeError
from .graph_transactions import await_vertices
from .sql import insert_documents, json_text
from .store import GRAPH_ROWS_PER_REQUEST

# A request carries rows of at most about this much JSON text; a single larger row goes alone. It keeps
# the request, and what the client holds to send it, small next to the restore's memory limit.
DOCUMENT_BYTES_PER_REQUEST = 1 << 20
# Graph rows, and their JSON text, held at once across projects before every project's group is written.
GRAPH_ROWS_HELD = 4 * GRAPH_ROWS_PER_REQUEST
GRAPH_BYTES_HELD = 4 * DOCUMENT_BYTES_PER_REQUEST


class DocumentGroups:
    """Rows of document and vector types, written one group of one type per request, in the order added."""

    def __init__(self, batches):
        self.batches, self.collection, self.rows, self.size = batches, None, [], 0

    def add(self, collection, row):
        size = len(json_text(row))
        if self.rows and (collection != self.collection or len(self.rows) >= GRAPH_ROWS_PER_REQUEST
                          or self.size + size > DOCUMENT_BYTES_PER_REQUEST):
            self.flush()
        self.collection = collection
        self.rows.append(row)
        self.size += size

    def flush(self):
        rows, size = self.rows, self.size
        self.rows, self.size = [], 0
        if rows:
            insert_documents(self.batches.tx, self.collection, rows)
            self.batches.wrote(len(rows), size)


class GraphGroups:
    """Vertices (``kind="node"``) or edges of the graph, written per project by ``Store.write_graph``."""

    def __init__(self, store, batches, kind):
        self.store, self.batches, self.kind = store, batches, kind
        self.groups = {}  # project id -> {row id: row}
        self.sizes = {}  # project id -> characters of JSON text in its group

    def add(self, row):
        project, identifier = row.get("project_id"), row.get("id")
        if not isinstance(project, str) or not isinstance(identifier, str):
            # Refused by the upsert's own check, with its message, after the rows before it.
            self.flush()
            (self.store.upsert_node if self.kind == "node" else self.store.upsert_edge)(row, self.batches.tx)
            self.batches.wrote(1)
            return
        size = len(json_text(row))
        group = self.groups.get(project)
        # The second copy of an id updates the first, as the upsert row by row did: it goes in the next
        # group. A row that would take the group past the text limit starts the next one; alone, it goes.
        if group and (identifier in group or self.sizes[project] + size > DOCUMENT_BYTES_PER_REQUEST):
            self._write(project)
        group = self.groups.setdefault(project, {})
        group[identifier] = row
        self.sizes[project] = self.sizes.get(project, 0) + size
        if len(group) >= GRAPH_ROWS_PER_REQUEST or self.sizes[project] >= DOCUMENT_BYTES_PER_REQUEST:
            self._write(project)
        elif (sum(len(rows) for rows in self.groups.values()) >= GRAPH_ROWS_HELD
              or sum(self.sizes.values()) >= GRAPH_BYTES_HELD):
            self.flush()

    def _write(self, project):
        rows = list(self.groups.pop(project).values())
        size = self.sizes.pop(project, 0)
        nodes, edges = (rows, []) if self.kind == "node" else ([], rows)
        try:
            self._store(project, nodes, edges)
        except (ValueError, RuntimeError) as exc:
            ends = {row[key] for row in edges for key in ("from_id", "to_id") if isinstance(row.get(key), str)}
            # An engine error (out of memory, timeout, syntax) has already rolled the transaction back: it goes up as it is.
            if isinstance(exc, ArcadeError) or not ends:
                raise
            # The index of nodes.id may still miss a vertex that write_restored waited for (#377), and a transaction
            # that has missed one keeps missing it: commit (the retry finds what the failed try wrote and keeps it),
            # wait with none open, and write the group once more in a new transaction. A second failure ends it.
            self.batches.commit()
            await_vertices(self.store, ends, *self.batches.timeout)
            self.batches.open()
            self._store(project, nodes, edges)
        self.batches.wrote(len(rows), size)  # the one place a commit may fall: after a group, never inside one

    def _store(self, project, nodes, edges):
        tx = self.batches.tx
        self.store.prefetch_graph(tx, nodes, edges)
        self.store.write_graph(tx, project, nodes, edges)
        tx.known = None  # what the prefetch read is of this group only

    def flush(self):
        for project in list(self.groups):
            self._write(project)


def write_restored(store, batches, source, claims=None):
    """Insert the rows of a validated backup into a store that a restore has just emptied, in the transactions of
    `batches` (restore_staged.Batches).

    With `claims` (restore_claims.BackupClaims) each row first loses what only a person's act could give it (#214).
    The backup's journal rows are not written: every commit of the restore resets the journal and writes a head
    that names this installation as the writer."""
    from .restore_staged import JOURNAL_TYPES
    from .store import DOCUMENT_TYPES
    documents = DocumentGroups(batches)
    # Documents first (projects and sources before the graph that checks them), then vertices,
    # then edges and vectors; a streamed file is read once for each of the three.
    for _, collection, original in source.rows(DOCUMENT_TYPES):
        if collection in JOURNAL_TYPES:
            continue
        row = dict(original)
        if collection == "jobs" and row.get("state") == "running":
            row.update(state="pending", worker=None, lease_until=None)
        if claims is not None:
            row = claims.turn(collection, row)
        documents.add(collection, row)
    documents.flush()
    nodes = GraphGroups(store, batches, "node")
    ids = set()
    for _, _, node in source.rows(("nodes",)):
        nodes.add(node)
        if isinstance(node.get("id"), str):
            ids.add(node["id"])
    nodes.flush()
    if ids:
        # The edges look their vertices up by the index of nodes.id, which may not find them yet (#377). Once, with no
        # transaction open, wait for all of them: the edge transactions then begin after the index has caught up.
        batches.commit()
        await_vertices(store, ids, *batches.timeout)
        batches.open()
    edges = GraphGroups(store, batches, "edge")
    for kind, name, row in source.rows(("edges",), vectors=True):
        if kind == "row":
            edges.add(row)
            continue
        edges.flush()
        if not isinstance(name, str) or not name.startswith("vector_"):
            raise ValueError("Invalid vector type in backup")
        if row is not None:
            documents.add(name, row)
    edges.flush()
    documents.flush()
