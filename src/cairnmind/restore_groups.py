"""Restore writes the rows of a validated backup in groups, a few hundred per request (#219).

Row by row, a restore of a store of 26,000 graph vertices and 32,000 edges took 297,454 requests: every
vertex and edge was looked up before it was written. Here the same rows go in the same transaction:

- Documents and vector rows: one ``INSERT ... CONTENT :rows`` per group of one type (``insert_documents``),
  in file order, a group ending at ``GRAPH_ROWS_PER_REQUEST`` rows or ``DOCUMENT_BYTES_PER_REQUEST`` of text.
- Graph vertices, then edges: per project, the reads of ``Store.prefetch_graph`` and the grouped writes of
  ``Store.write_graph`` (#167), with every check of ``upsert_node`` / ``upsert_edge``, a group ending at the
  same row and text limits. The restore's transaction is reset, so the journal needs none of these rows.

A group holds rows read in pieces from the file, never the file (#196). A group that fails, or that the
database stores only in part, fails the transaction, which leaves the store as it was.
"""
from __future__ import annotations

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

    def __init__(self, tx):
        self.tx, self.collection, self.rows, self.size = tx, None, [], 0

    def add(self, collection, row):
        size = len(json_text(row))
        if self.rows and (collection != self.collection or len(self.rows) >= GRAPH_ROWS_PER_REQUEST
                          or self.size + size > DOCUMENT_BYTES_PER_REQUEST):
            self.flush()
        self.collection = collection
        self.rows.append(row)
        self.size += size

    def flush(self):
        if self.rows:
            insert_documents(self.tx, self.collection, self.rows)
        self.rows, self.size = [], 0


class GraphGroups:
    """Vertices (``kind="node"``) or edges of the graph, written per project by ``Store.write_graph``."""

    def __init__(self, store, tx, kind):
        self.store, self.tx, self.kind = store, tx, kind
        self.groups = {}  # project id -> {row id: row}
        self.sizes = {}  # project id -> characters of JSON text in its group

    def add(self, row):
        project, identifier = row.get("project_id"), row.get("id")
        if not isinstance(project, str) or not isinstance(identifier, str):
            # Refused by the upsert's own check, with its message, after the rows before it.
            self.flush()
            (self.store.upsert_node if self.kind == "node" else self.store.upsert_edge)(row, self.tx)
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
        self.sizes.pop(project, None)
        nodes, edges = (rows, []) if self.kind == "node" else ([], rows)
        self.store.prefetch_graph(self.tx, nodes, edges)
        self.store.write_graph(self.tx, project, nodes, edges)
        self.tx.known = None  # what the prefetch read is of this group only

    def flush(self):
        for project in list(self.groups):
            self._write(project)


def write_restored(store, tx, source):
    """Insert the rows of a validated backup into a store that restore's transaction has just emptied."""
    from .store import DOCUMENT_TYPES
    documents = DocumentGroups(tx)
    # Documents first (projects and sources before the graph that checks them), then vertices,
    # then edges and vectors; a streamed file is read once for each of the three.
    for _, collection, original in source.rows(DOCUMENT_TYPES):
        row = dict(original)
        if collection == "jobs" and row.get("state") == "running":
            row.update(state="pending", worker=None, lease_until=None)
        if collection == "journal_state":
            # The backup's head may name the writer of the time it was made, or none.
            row["writer"] = store.installation.id
        documents.add(collection, row)
    documents.flush()
    nodes = GraphGroups(store, tx, "node")
    for _, _, node in source.rows(("nodes",)):
        nodes.add(node)
    nodes.flush()
    edges = GraphGroups(store, tx, "edge")
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
