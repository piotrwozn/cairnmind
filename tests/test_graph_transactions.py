"""A graph larger than one transaction is written in several, and is still the graph one would have written (#377)."""
from __future__ import annotations

import pytest
from test_index_batching import code_tree, dump

from cairnmind import graph_transactions, ingest
from cairnmind.arcade import ArcadeClient
from cairnmind.code_graph import CODE_GRAPH_NOT_BUILT, code_graph_pending
from cairnmind.inspector import Inspector
from cairnmind.journal import JournalTransaction
from cairnmind.store import Store


def graph(store, pid):
    one = dump(store, pid)
    return {"nodes": one["nodes"], "edges": one["edges"]}


def pair(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    code_tree(root)
    whole, split = Store(tmp_path / "whole"), Store(tmp_path / "split")
    return whole, whole.add_project("p", root)["id"], split, split.add_project("p", root)["id"]


def test_a_graph_over_the_bound_is_written_in_several_transactions_as_the_same_graph(tmp_path, monkeypatch):
    whole, first, split, second = pair(tmp_path)
    ingest.index_project(whole, first)
    commits = []
    real = graph_transactions.GraphTransactions.commit
    monkeypatch.setattr(graph_transactions.GraphTransactions, "commit", lambda self: (commits.append(self.rows), real(self))[1])
    monkeypatch.setattr(graph_transactions, "ROWS_PER_TRANSACTION", 100)
    result = ingest.index_project(split, second)
    written = result["graph_written"]["nodes"] + result["graph_written"]["edges"]
    assert len(commits) >= written // 100 - 1 > 5  # one transaction of the bound at a time, not one of everything
    assert max(commits) < 100 + 1000  # a group of 1,000 rows is the most one transaction takes beyond the bound
    assert graph(split, second) == graph(whole, first) and graph(split, second)["edges"]
    assert not code_graph_pending(split.query("SELECT graph_fingerprint FROM projects WHERE id=:p", {"p": second})[0])


def test_a_write_that_stops_half_way_leaves_the_graph_marked_not_built_and_the_next_run_completes_it(tmp_path, monkeypatch):
    whole, first, split, second = pair(tmp_path)
    ingest.index_project(whole, first)
    real, calls = Store.write_graph, []

    def failing(self, tx, project_id, nodes, edges):
        calls.append(len(nodes) + len(edges))
        if len(calls) == 8:
            raise RuntimeError("the connection dropped")
        return real(self, tx, project_id, nodes, edges)
    with monkeypatch.context() as patch:
        patch.setattr(graph_transactions, "ROWS_PER_TRANSACTION", 100)
        patch.setattr(Store, "write_graph", failing)
        with pytest.raises(RuntimeError, match="connection dropped"):
            ingest.index_project(split, second)
    # Rows of earlier transactions are there, and every reader is told that the graph is not built.
    assert split.query("SELECT count(*) AS n FROM nodes WHERE project_id=:p", {"p": second})[0]["n"] > 0
    row = split.query("SELECT graph_fingerprint FROM projects WHERE id=:p", {"p": second})[0]
    assert code_graph_pending(row)
    ingest.index_project(split, second)
    assert graph(split, second) == graph(whole, first)
    assert not code_graph_pending(split.query("SELECT graph_fingerprint FROM projects WHERE id=:p", {"p": second})[0])


def test_vertices_the_index_does_not_find_yet_are_waited_for_and_never_assumed(tmp_path, monkeypatch):
    whole, first, *_ = pair(tmp_path)
    ingest.index_project(whole, first)
    ids = [row["id"] for row in whole.query("SELECT id FROM nodes WHERE project_id=:p LIMIT 5", {"p": first})]
    real, hidden, slept = JournalTransaction.query, [2], []

    def query(self, sql, params=None):
        found = real(self, sql, params)
        if "FROM nodes WHERE id IN" in sql and hidden[0]:  # the engine, compacting its index, finds nothing
            hidden[0] -= 1
            return []
        return found
    monkeypatch.setattr(JournalTransaction, "query", query)
    monkeypatch.setattr(graph_transactions.time, "sleep", slept.append)
    graph_transactions.await_vertices(whole, ids)
    assert len(slept) == 2
    monkeypatch.setattr(graph_transactions, "VERTEX_WAIT", 0)
    with pytest.raises(RuntimeError, match="did not find 1 graph vertices"):
        graph_transactions.await_vertices(whole, [*ids, "no-such-vertex"])


def test_the_wait_for_the_vertices_runs_inside_indexing_before_the_edges_and_the_graph_is_still_the_same(tmp_path, monkeypatch):
    whole, first, split, second = pair(tmp_path)
    ingest.index_project(whole, first)
    real_query, real_wait, state = JournalTransaction.query, ingest.await_vertices, {"inside": False, "hidden": 2, "waits": 0}

    def wait(store, identifiers):
        state["inside"] = True
        try:
            real_wait(store, identifiers)
        finally:
            state["inside"] = False

    def query(self, sql, params=None):
        found = real_query(self, sql, params)
        if state["inside"] and state["hidden"] and "FROM nodes WHERE id IN" in sql:  # the index, compacting, finds nothing
            state["hidden"] -= 1
            return []
        return found
    monkeypatch.setattr(graph_transactions, "ROWS_PER_TRANSACTION", 100)
    monkeypatch.setattr(ingest, "await_vertices", wait)
    monkeypatch.setattr(JournalTransaction, "query", query)
    monkeypatch.setattr(graph_transactions.time, "sleep", lambda _seconds: state.update(waits=state["waits"] + 1))
    ingest.index_project(split, second)
    assert state["hidden"] == 0 and state["waits"] == 2  # indexing waited until the index found its vertices
    assert graph(split, second) == graph(whole, first)


def synthetic(store, tmp_path, shift=0):
    """The graph step's input for 3 files of 150 symbols; ``shift`` moves every line, so every symbol and call is new."""
    folder = tmp_path / "folder"
    folder.mkdir(exist_ok=True)
    project = store.add_project("synthetic", folder)["id"]
    if not store.query("SELECT id FROM sources WHERE project_id=:p LIMIT 1", {"p": project}):
        store.client.command("INSERT INTO sources CONTENT :rows", {"rows": [
            {"id": f"source-{n}", "project_id": project, "uri": f"file://{folder}/m{n}.py", "kind": "file", "content": "x",
             "sha256": f"{n:064x}", "is_current": True, "created_at": "2026-11-22T00:00:00+00:00"} for n in range(3)]})
    indexed = {}
    for number in range(3):
        path = folder / f"m{number}.py"
        symbols = [{"name": f"f{i}", "kind": "function", "line_start": 2 * i + 1 + shift, "line_end": 2 * i + 2 + shift}
                   for i in range(150)]
        calls = [{"name": f"f{(i * 7 + 1) % 150}", "line": 2 * i + 1 + shift} for i in range(0, 150, 4)]
        indexed[path] = {"source": {"id": f"source-{number}", "uri": f"file://{path}", "kind": "file"}, "root": folder,
                         "analysis": {"symbols": symbols, "imports": [], "calls": calls}}
    return project, indexed


def counted_requests(monkeypatch):
    requests, real = [], ArcadeClient._request
    monkeypatch.setattr(ArcadeClient, "_request", lambda self, method, path, **kw: (requests.append(path), real(self, method, path, **kw))[1])
    return requests


def test_a_second_full_write_of_the_same_graph_makes_no_more_requests_than_the_first(tmp_path, monkeypatch):
    # Deleting every edge and writing the same ids again failed on the engine from about 40,000 edges on (#385):
    # the edges that are stored as they would be written stay, and the rest of the write is the same as before.
    store = Store(tmp_path / "memory")
    project, indexed = synthetic(store, tmp_path)
    monkeypatch.setattr(graph_transactions, "ROWS_PER_TRANSACTION", 100)
    requests = counted_requests(monkeypatch)
    first = ingest._build_graph(store, project, indexed)
    first_requests = len(requests)
    requests.clear()
    monkeypatch.setattr(ingest, "parser_identity", lambda: "another-parser")  # the graph is not the fingerprinted one: full write
    second = ingest._build_graph(store, project, indexed)
    assert len(requests) <= first_requests, (len(requests), first_requests)
    assert first["graph_written"]["edges"] > 500 and second["graph_written"]["edges"] == 0
    assert store.query("SELECT count(*) AS n FROM edges WHERE project_id=:p", {"p": project})[0]["n"] == first["edges"]
    assert store.query("SELECT count(*) AS n FROM edges WHERE out IS NULL OR in IS NULL")[0]["n"] == 0


def test_a_full_write_over_a_graph_that_changed_deletes_only_the_edges_that_went_and_leaves_no_old_one(tmp_path, monkeypatch):
    store = Store(tmp_path / "memory")
    project, indexed = synthetic(store, tmp_path)
    monkeypatch.setattr(graph_transactions, "ROWS_PER_TRANSACTION", 100)
    first = ingest._build_graph(store, project, indexed)
    monkeypatch.setattr(ingest, "parser_identity", lambda: "another-parser")
    _, moved = synthetic(store, tmp_path, shift=1000)
    second = ingest._build_graph(store, project, moved)
    kept = first["edges"] - second["graph_written"]["edges"]
    assert 0 < kept < 10  # the edge of each source to its file does not name a line
    assert store.query("SELECT count(*) AS n FROM edges WHERE project_id=:p", {"p": project})[0]["n"] == second["edges"]
    assert store.query("SELECT count(*) AS n FROM edges WHERE out IS NULL OR in IS NULL")[0]["n"] == 0


def test_the_inspector_graph_says_that_the_code_graph_is_not_built_while_it_is_being_written(tmp_path):
    whole, first, *_ = pair(tmp_path)
    ingest.index_project(whole, first)
    inspector = Inspector(whole)
    assert inspector.graph(first)["warnings"] == [] and inspector.groups(first)["warnings"] == []
    with whole.transaction() as tx:  # what a split write leaves until its last transaction
        tx.command("UPDATE projects SET graph_fingerprint=null WHERE id=:p", {"p": first})
    assert inspector.graph(first)["warnings"] == [CODE_GRAPH_NOT_BUILT]
    assert inspector.groups(first)["warnings"] == [CODE_GRAPH_NOT_BUILT]
    node = inspector.graph(first, _full=True)["nodes"][-1]["id"]
    assert inspector.neighbors(first, node)["warnings"] == [CODE_GRAPH_NOT_BUILT]