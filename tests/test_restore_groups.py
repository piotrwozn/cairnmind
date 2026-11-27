"""Restore writes the rows of a backup in groups, a few hundred per request (#219).

The restored store is the one the row-by-row restore of 0.11.0 made, every refusal still comes before
anything changes, and a restore that fails in the middle of a group leaves the store as it was.
"""
import gzip
import hashlib
import json

import pytest

from cairnmind.storage import arcade
from cairnmind.storage.journal import JournalTransaction
from cairnmind.storage.restore_groups import DOCUMENT_BYTES_PER_REQUEST, GRAPH_BYTES_HELD, GraphGroups
from cairnmind.storage.restore_staged import RestoreFailed
from cairnmind.storage.sql import clean_row, insert_document, json_text, quote_name
from cairnmind.store import DOCUMENT_TYPES, Store

from test_backup_restore_stream import FIXTURE_0_10_0, _backup_payload, _variants, comparable

NODES = 600
CHUNKS = 300
VECTORS = 300


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "memory")


def _row_by_row(self, source, claims=None):
    """Store._restore_from as 0.11.0 wrote it: one request for each row (the reference result)."""
    for registry in source.vector_indexes:
        name, dimension = registry["type_name"], registry["dimension"]
        self.command(f"CREATE DOCUMENT TYPE {quote_name(name)} IF NOT EXISTS")
        self.command(f"CREATE PROPERTY {quote_name(name)}.id IF NOT EXISTS STRING")
        self.command(f"CREATE INDEX IF NOT EXISTS ON {quote_name(name)}(id) UNIQUE")
        self.command(f"CREATE PROPERTY {quote_name(name)}.vector IF NOT EXISTS ARRAY_OF_FLOATS")
        self.command(f"CREATE INDEX IF NOT EXISTS ON {quote_name(name)}(vector) LSM_VECTOR METADATA "
                     f"{{dimensions:{dimension},similarity:'COSINE',idPropertyName:'id'}}")
    with self.transaction() as tx:
        from cairnmind.storage.journal import Journal
        Journal.reset(tx, reason="restore")
        for row in tx.query("SELECT FROM vector_indexes"):
            if row.get("type_name", "").startswith("vector_"):
                tx.command(f"DELETE FROM {quote_name(row['type_name'])}")
        tx.command("DELETE FROM edges")
        tx.command("DELETE FROM nodes")
        for collection in reversed(DOCUMENT_TYPES):
            tx.command(f"DELETE FROM {quote_name(collection)}")
        for _, collection, original in source.rows(DOCUMENT_TYPES):
            row = dict(original)
            if collection == "jobs" and row.get("state") == "running":
                row.update(state="pending", worker=None, lease_until=None)
            if collection == "journal_state":
                row["writer"] = self.installation.id
            insert_document(tx, collection, row)
        for _, _, node in source.rows(("nodes",)):
            self.upsert_node(node, tx)
        for kind, name, row in source.rows(("edges",), vectors=True):
            if kind == "row":
                self.upsert_edge(row, tx)
            elif row is not None:
                insert_document(tx, name, row)


def _large(store, tmp_path):
    """The 0.10.0 fixture with a second project and a graph, chunks and vectors of several groups each."""
    store.restore(FIXTURE_0_10_0)
    first = next(row for row in store.list_projects() if row["name"] == "backup-fixture")["id"]
    second = store.add_project("second", tmp_path / "second")["id"]
    for pid, count in ((first, NODES), (second, 30)):
        source = store.snapshot(pid, f"manual://graph-{pid}", f"graph of {pid} \U0001F600")
        nodes = [{"id": f"{pid}:n{i}", "project_id": pid, "kind": "function", "label": f"f{i} \"ż\"",
                  "source_id": source["id"], "metadata": {"line": i, "weight": i / 7, "tags": ["a", i]}}
                 for i in range(count)]
        nodes[0].pop("source_id")  # a vertex without a source, current by default
        edges = [{"id": f"{pid}:e{i}", "project_id": pid, "from_id": f"{pid}:n{i}", "to_id": f"{pid}:n{(i * 7 + 1) % count}",
                  "kind": "calls", "source_id": source["id"]} for i in range(count)]
        edges[1].update(status="inferred", is_current=False)
        with store.transaction() as tx:
            tx.touch(pid, "graph", op="reset")
            store.prefetch_graph(tx, nodes, edges)
            store.write_graph(tx, pid, nodes, edges)
    store.client.command("INSERT INTO chunks CONTENT :rows", {"rows": [
        {"id": f"bulk-{n}", "project_id": first, "source_id": "bulk", "text": f"{n} " + "tekst " * 40,
         "is_current": n % 2 == 0, "metadata": {"start": n, "score": 0.1 * n}} for n in range(CHUNKS)]})
    vector = store.query("SELECT type_name FROM vector_indexes")[0]["type_name"]
    store.client.command(f"INSERT INTO {quote_name(vector)} CONTENT :rows", {"rows": [
        {"id": f"vector-{n}", "chunk_id": f"bulk-{n}", "dimension": 3, "is_current": True, "model": "fixture-embedding",
         "namespace": "fixture-embedding@digest", "project_id": first, "source_id": "bulk",
         "vector": [1.0, n / VECTORS, 0.25]} for n in range(VECTORS)]})
    return _with_vectors(store, store.backup(tmp_path / "large.cairnmind.json.gz"))


def _with_vectors(store, path):
    """The file as a version before 0.13.0 wrote it, with the embeddings, the registry and the vectors (#187): this
    version writes none, and restore still loads them from an older file."""
    envelope = json.loads(gzip.decompress(path.read_bytes()))
    payload = envelope["payload"]
    payload["embeddings"] = [clean_row(row) for row in store.query("SELECT FROM embeddings")]
    payload["vector_indexes"] = [clean_row(row) for row in store.query("SELECT FROM vector_indexes")]
    payload["vector_data"] = {row["type_name"]: [clean_row(vector) for vector in store.query(
        f"SELECT FROM {quote_name(row['type_name'])}")] for row in payload["vector_indexes"]}
    envelope["sha256"] = hashlib.sha256(json_text(payload).encode()).hexdigest()
    path.write_bytes(gzip.compress(json_text(envelope).encode()))
    return path


def _counting(monkeypatch):
    seen = []
    request = arcade.ArcadeClient._request

    def counted(self, method, path, **kwargs):
        seen.append((path, (kwargs.get("json") or {}).get("command", "")))
        return request(self, method, path, **kwargs)

    monkeypatch.setattr(arcade.ArcadeClient, "_request", counted)
    return seen


def test_restore_writes_rows_in_groups(store, tmp_path, monkeypatch):
    path = _large(store, tmp_path)
    target = Store(tmp_path / "target")
    seen = _counting(monkeypatch)
    target.restore(path)
    rows = 2 * (NODES + 30) + CHUNKS + VECTORS
    # Row by row this was 6,407 requests: about five per edge, four per vertex and one per document.
    # Now about a hundred, most of them the safety copy and the statements around the inserts; each edge group
    # also commits and opens a transaction, so that the wait for the index has none open (#377).
    assert len(seen) < 250,f"{len(seen)} requests for {rows} rows"
    graph = [command for _, command in seen if command.startswith(("CREATE VERTEX", "CREATE EDGE"))]
    assert len(graph) <= 10, graph  # 630 vertices and 630 edges of two projects, and the fixture's few
    assert comparable(target.export()) == comparable(store.export())


@pytest.mark.parametrize("written_by", ["0.10.0", "this version"])
def test_the_grouped_restore_equals_the_row_by_row_one(store, tmp_path, monkeypatch, written_by):
    path = FIXTURE_0_10_0 if written_by == "0.10.0" else _large(store, tmp_path)
    grouped = Store(tmp_path / "grouped")
    grouped.restore(path)
    reference = Store(tmp_path / "reference")
    monkeypatch.setattr(Store, "_restore_from", _row_by_row)
    reference.restore(path)
    assert comparable(grouped.export()) == comparable(reference.export())
    if written_by == "this version":
        assert comparable(grouped.export()) == comparable(store.export())


def test_every_refusal_comes_before_anything_changes_with_the_same_message(store, tmp_path, monkeypatch):
    payload, project_id, source_id = _backup_payload(store, tmp_path)
    expected = {"truncated": "Invalid or corrupt", "checksum": "checksum mismatch", "dangling-edge": "graph endpoint",
                "corrupt-source": "corrupt source snapshot", "several-faults": "invalid project reference",
                "dangling-evidence": "evidence reference", "foreign-format": "not a supported",
                "dangling-project": "invalid project reference", "dangling-plan": "invalid managed plan steps"}
    cases = _variants(payload, project_id, source_id)
    before, copies = comparable(store.export()), set((store.data_dir / "backups").glob("before-restore-*"))
    seen = _counting(monkeypatch)
    for name, message in expected.items():
        path = tmp_path / f"{name}.cairnmind.json.gz"
        path.write_bytes(gzip.compress(cases[name][0]))
        with pytest.raises(ValueError, match=message):
            store.restore(path)
    # Refused while the file was read: not one database request, not even the safety copy.
    assert seen == [] and set((store.data_dir / "backups").glob("before-restore-*")) == copies
    assert comparable(store.export()) == before


def _failing_at(monkeypatch, number, test):
    """Make the ``number``-th database command of the restore transaction that passes ``test`` fail."""
    command, script, left = JournalTransaction.command, JournalTransaction.script, [number]

    def check(sql):
        if test(sql):
            left[0] -= 1
            if left[0] == 0:
                raise OSError("the engine went away")

    def failing_command(self, sql, params=None):
        check(sql)
        return command(self, sql, params)

    def failing_script(self, statements, params, project_id):
        check(";".join(statements))
        return script(self, statements, params, project_id)

    monkeypatch.setattr(JournalTransaction, "command", failing_command)
    monkeypatch.setattr(JournalTransaction, "script", failing_script)
    return left


@pytest.mark.parametrize("statement", ["INSERT INTO `chunks` CONTENT :rows", "CREATE VERTEX nodes CONTENT :rows",
                                       "CREATE EDGE edges", "INSERT INTO `vector_"])
def test_a_restore_that_fails_in_the_middle_of_a_group_changes_nothing(store, tmp_path, monkeypatch, statement):
    path = _large(store, tmp_path)
    store.add_project("after the backup", tmp_path / "after")
    before = comparable(store.export())
    # The second group: the first has been sent and is part of the open transaction.
    left = _failing_at(monkeypatch, 2, lambda sql: statement in sql)
    with pytest.raises(RestoreFailed, match="went away.*put back as it was before"):
        store.restore(path)
    assert left[0] <= 0  # the undo writes the same kinds of group again: it is the one failure that counted
    monkeypatch.undo()
    assert comparable(store.export()) == before


def test_a_group_the_database_did_not_store_in_full_fails_the_restore(store, tmp_path, monkeypatch):
    path = _large(store, tmp_path)
    before = comparable(store.export())
    command, short_once = JournalTransaction.command, []

    def short(self, sql, params=None):
        result = command(self, sql, params)
        if sql.startswith("INSERT INTO `chunks` CONTENT :rows") and not short_once:
            short_once.append(sql)  # once: the undo writes its chunks in full, as an engine that recovered would
            return result[:-1]
        return result

    monkeypatch.setattr(JournalTransaction, "command", short)
    with pytest.raises(RestoreFailed, match="did not store every row"):
        store.restore(path)
    monkeypatch.undo()
    assert comparable(store.export()) == before


def test_a_backup_with_a_vertex_twice_restores_as_row_by_row(store, tmp_path, monkeypatch):
    """No version writes one, but the upsert of the old restore took the second copy; so do the groups."""
    store.restore(FIXTURE_0_10_0)
    payload = json.loads(gzip.decompress(store.backup(tmp_path / "base.cairnmind.json.gz").read_bytes()))["payload"]
    twice = dict(payload["nodes"][0], label="the second copy")
    payload["nodes"].insert(1, twice)
    envelope = {"format": "cairnmind-arcadedb-backup", "payload": payload,
                "sha256": hashlib.sha256(json_text(payload).encode()).hexdigest(), "version": 1}
    path = tmp_path / "twice.cairnmind.json.gz"
    path.write_bytes(gzip.compress(json_text(envelope).encode()))
    grouped = Store(tmp_path / "grouped")
    grouped.restore(path)
    reference = Store(tmp_path / "reference")
    monkeypatch.setattr(Store, "_restore_from", _row_by_row)
    reference.restore(path)
    assert comparable(grouped.export()) == comparable(reference.export())
    assert any(row["label"] == "the second copy" for row in grouped.export()["nodes"])


class _Recording:
    """A store and transaction that only record the graph groups ``GraphGroups`` writes."""

    def __init__(self):
        self.groups, self.known, self.tx = [], None, self

    def wrote(self, rows, size=0):
        pass

    def prefetch_graph(self, tx, nodes, edges):
        pass

    def write_graph(self, tx, project_id, nodes, edges):
        self.groups.append((project_id, list(nodes or edges)))


@pytest.mark.parametrize("kind", ["node", "edge"])
def test_a_graph_group_ends_at_the_text_limit_as_a_document_group_does(kind):
    recording = _Recording()
    groups = GraphGroups(recording, recording, kind)
    held = []
    wide = "x" * 40_000
    for n in range(600):
        groups.add({"id": f"p{n % 3}:r{n}", "project_id": f"p{n % 3}", "metadata": {"text": wide}})
        held.append(sum(len(json_text(row)) for rows in groups.groups.values() for row in rows.values()))
    groups.add({"id": "p0:alone", "project_id": "p0", "metadata": {"text": "y" * (DOCUMENT_BYTES_PER_REQUEST + 1)}})
    groups.flush()
    assert sum(len(rows) for _, rows in recording.groups) == 601
    for _, rows in recording.groups:
        assert len(rows) == 1 or sum(len(json_text(row)) for row in rows) <= DOCUMENT_BYTES_PER_REQUEST
    assert [rows for _, rows in recording.groups if len(rows) == 1 and rows[0]["id"] == "p0:alone"]
    # What is held across projects stays near a few groups, never the 1,000 rows (40 MB) of the row limit.
    assert max(held) < GRAPH_BYTES_HELD


def test_wide_vertices_restore_in_requests_of_bounded_size(store, tmp_path, monkeypatch):
    project = store.add_project("wide", tmp_path / "wide")["id"]
    source = store.snapshot(project, "manual://wide", "wide graph")
    nodes = [{"id": f"{project}:w{i}", "project_id": project, "kind": "function", "label": f"w{i}",
              "source_id": source["id"], "metadata": {"text": f"{i} " + "z" * 40_000}} for i in range(80)]
    with store.transaction() as tx:
        tx.touch(project, "graph", op="reset")
        store.prefetch_graph(tx, nodes, [])
        store.write_graph(tx, project, nodes, [])
    path = store.backup(tmp_path / "wide.cairnmind.json.gz")
    target = Store(tmp_path / "target")
    sizes = []
    request = arcade.ArcadeClient._request

    def measured(self, method, path, **kwargs):
        body = kwargs.get("json") or {}
        if str(body.get("command", "")).startswith("CREATE VERTEX"):
            sizes.append(len(json.dumps(body)))
        return request(self, method, path, **kwargs)

    monkeypatch.setattr(arcade.ArcadeClient, "_request", measured)
    target.restore(path)
    monkeypatch.undo()
    # 3.2 MB of vertices: several requests, none much larger than the text limit of one group.
    assert len(sizes) >= 3 and max(sizes) < DOCUMENT_BYTES_PER_REQUEST + 64 * 1024, sizes
    assert comparable(target.export()) == comparable(store.export())
