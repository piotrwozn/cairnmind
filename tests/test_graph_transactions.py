"""A graph larger than one transaction is written in several, and is still the graph one would have written (#377)."""
from __future__ import annotations

import httpx
import pytest
from conftest import counted_waits, own_time
from test_index_batching import code_tree, dump

from cairnmind.config import Settings
from cairnmind.scip import ingest
from cairnmind.scip.code_graph import CODE_GRAPH_NOT_BUILT, code_graph_pending
from cairnmind.storage import graph_transactions
from cairnmind.storage.arcade import ArcadeClient, ArcadeError
from cairnmind.storage.journal import JournalTransaction
from cairnmind.store import Store
from cairnmind.terminal.inspector import Inspector


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
    real, hidden = JournalTransaction.query, [2]

    def query(self, sql, params=None):
        found = real(self, sql, params)
        if "FROM nodes WHERE id IN" in sql and hidden[0]:  # the engine, compacting its index, finds nothing
            hidden[0] -= 1
            waits.missed = True
            return []
        return found
    monkeypatch.setattr(JournalTransaction, "query", query)
    waits = counted_waits(monkeypatch, graph_transactions)
    graph_transactions.await_vertices(whole, ids)
    assert len(waits.slept) == 2
    monkeypatch.setattr(graph_transactions, "VERTEX_WAIT", 0)
    with pytest.raises(RuntimeError, match="did not find 1 graph vertices"):
        graph_transactions.await_vertices(whole, [*ids, "no-such-vertex"])


def test_the_wait_for_the_vertices_runs_inside_indexing_before_the_edges_and_the_graph_is_still_the_same(tmp_path, monkeypatch):
    whole, first, split, second = pair(tmp_path)
    ingest.index_project(whole, first)
    real_query, real_wait, state = JournalTransaction.query, ingest.await_vertices, {"inside": False, "hidden": 2}

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
            waits.missed = True
            return []
        return found
    monkeypatch.setattr(graph_transactions, "ROWS_PER_TRANSACTION", 100)
    monkeypatch.setattr(ingest, "await_vertices", wait)
    monkeypatch.setattr(JournalTransaction, "query", query)
    waits = counted_waits(monkeypatch, graph_transactions)
    ingest.index_project(split, second)
    assert state["hidden"] == 0 and len(waits.slept) == 2  # indexing waited until the index found its vertices
    assert graph(split, second) == graph(whole, first)


def synthetic(store, tmp_path, shift=0, per_file=150):
    """The graph step's input for 3 files of ``per_file`` symbols; ``shift`` moves every line, so every symbol and call is new."""
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
                   for i in range(per_file)]
        calls = [{"name": f"f{(i * 7 + 1) % per_file}", "line": 2 * i + 1 + shift} for i in range(0, per_file, 4)]
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


def test_a_full_write_over_a_graph_whose_vertices_all_went_removes_them_in_few_requests(tmp_path, monkeypatch):
    # The old vertices went 250 to a request, each request scanning the project's index (#402): 1,800 of them took
    # 7 requests of their own. In groups of a transaction's rows they take one.
    store = Store(tmp_path / "memory")
    project, indexed = synthetic(store, tmp_path, per_file=600)
    ingest._build_graph(store, project, indexed)
    monkeypatch.setattr(ingest, "parser_identity", lambda: "another-parser")
    _, moved = synthetic(store, tmp_path, shift=10000, per_file=600)
    requests = counted_requests(monkeypatch)
    second = ingest._build_graph(store, project, moved)
    assert second["graph_written"]["nodes"] > 1800
    # 60 requests now, 74 with the vertices going 250 to a request; the rest is the writes of the new rows.
    assert len(requests) <= 66, len(requests)
    assert store.query("SELECT count(*) AS n FROM nodes WHERE project_id=:p AND kind='symbol'", {"p": project})[0]["n"] == 1800


def test_a_vertex_delete_that_removes_fewer_rows_than_asked_fails_the_write_and_leaves_the_graph_not_built(tmp_path, monkeypatch):
    store = Store(tmp_path / "memory")
    project, indexed = synthetic(store, tmp_path)
    ingest._build_graph(store, project, indexed)
    monkeypatch.setattr(ingest, "parser_identity", lambda: "another-parser")
    monkeypatch.setattr(graph_transactions, "ROWS_PER_TRANSACTION", 100)  # the fingerprint is cleared in a transaction that commits
    _, moved = synthetic(store, tmp_path, shift=1000)
    real = JournalTransaction.command
    monkeypatch.setattr(JournalTransaction, "command", lambda self, sql, params=None: real(
        self, sql, {**params, "ids": params["ids"][:-1]}) if sql.startswith("DELETE FROM nodes") else real(self, sql, params))
    with pytest.raises(RuntimeError, match="every graph vertex"):
        ingest._build_graph(store, project, moved)
    row = store.query("SELECT graph_fingerprint FROM projects WHERE id=:p", {"p": project})[0]
    assert code_graph_pending(row)


def hiding_stored_vertices(monkeypatch, after=0, times=None):
    """The lookup of a vertex by its id says 'not there' for a vertex that is (``times`` lookups, from the one after ``after``):
    the write creates it again and the engine itself refuses that, with the error it gives for the id of a vertex deleted a
    moment ago (#433). The engine's own lag was shown by a script on a renamed directory of 160 files and more; on a test
    engine it cannot be forced, so its answer is hidden the way the tests of ``await_vertices`` hide it."""
    real, state = Store._checked_node, {"calls": 0, "hidden": 0}

    def checked(self, tx, node):
        out, existing = real(self, tx, node)
        state["calls"] += 1
        if existing and state["calls"] > after and (times is None or state["hidden"] < times):
            state["hidden"] += 1
            return out, None
        return out, existing
    monkeypatch.setattr(Store, "_checked_node", checked)
    return state


# Answers of ArcadeDB 26.11.1 to a commit that met a duplicated key, captured with a probe on 2026-11-24: the managed
# engine (development mode) and the pinned image in production mode (#455). The body of production mode has no `detail`,
# and its `exceptionArgs` hides the key; the index is named by `exceptionArgs` in both.
DUPLICATED_KEY = {
    "managed": {"error": "Found duplicate key in index", "exception": "com.arcadedb.exception.DuplicatedKeyException",
                "exceptionArgs": "{index}|[v1]|#28:0",
                "detail": "Duplicated key [v1] found on index '{index}' already assigned to record #28:0"},
    "production": {"error": "Found duplicate key in index", "exception": "com.arcadedb.exception.DuplicatedKeyException",
                   "exceptionArgs": "{index}|[concealed]|#28:0"},
}


def engine_refusal(tmp_path, mode, index, status=409):
    """The ArcadeError the client raises for a captured answer (``ArcadeClient._request`` over a stub transport)."""
    body = {key: value.replace("{index}", index) for key, value in DUPLICATED_KEY[mode].items()}
    client = ArcadeClient(Settings.load(tmp_path, create=True), transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body)))
    with pytest.raises(ArcadeError) as caught:
        client.command("CREATE VERTEX nodes SET id='v1'")
    return caught.value


@pytest.mark.parametrize("mode", ["managed", "production"])
def test_the_refusal_of_the_index_of_vertex_ids_is_recognised_in_both_wordings_of_the_engine(tmp_path, mode):
    assert graph_transactions.refused_by_id_index(engine_refusal(tmp_path, mode, "nodes[id]"))


@pytest.mark.parametrize("mode", ["managed", "production"])
@pytest.mark.parametrize("index", ["records[id]", "records[active_key]", "projects[name]", "projects[root]", "sources[current_key]",
                                   "journal_events[epoch,seq]", "nodes_0_61688107467487"])
def test_a_duplicated_key_of_another_index_is_not_taken_for_the_index_of_vertex_ids(tmp_path, mode, index):
    assert not graph_transactions.refused_by_id_index(engine_refusal(tmp_path, mode, index))


def test_only_a_duplicated_key_answered_with_409_counts(tmp_path):
    assert not graph_transactions.refused_by_id_index(engine_refusal(tmp_path, "production", "nodes[id]", status=503))
    assert not graph_transactions.refused_by_id_index(ArcadeError("ArcadeDB HTTP 409: Found duplicate key in index", status_code=409))


def test_a_vertex_the_index_refuses_for_a_duplicated_key_is_written_after_one_wait_and_the_graph_is_the_same(tmp_path, monkeypatch):
    store = Store(tmp_path / "memory")
    project, indexed = synthetic(store, tmp_path)
    first = ingest._build_graph(store, project, indexed)
    before = graph(store, project)
    monkeypatch.setattr(ingest, "parser_identity", lambda: "another-parser")  # a full write: every vertex is written again
    refusals, slept = hiding_stored_vertices(monkeypatch, times=1), []
    own_time(monkeypatch, ingest, sleep=slept.append)
    result = ingest._build_graph(store, project, indexed)  # before #433: ArcadeDB HTTP 409, Duplicated key on nodes[id]
    assert refusals["hidden"] == 1 and slept == [graph_transactions.DUPLICATE_WAIT]
    assert result["graph_written_again"] is True  # the doubled write is in the result, not only in the time it took
    assert graph(store, project) == before and (result["nodes"], result["edges"]) == (first["nodes"], first["edges"])
    assert not code_graph_pending(store.query("SELECT graph_fingerprint FROM projects WHERE id=:p", {"p": project})[0])


def test_a_vertex_the_index_refuses_twice_fails_by_name_leaves_the_graph_not_built_and_the_next_pass_repairs_it(tmp_path, monkeypatch):
    store = Store(tmp_path / "memory")
    project, indexed = synthetic(store, tmp_path, per_file=600)
    first = ingest._build_graph(store, project, indexed)
    before = graph(store, project)
    monkeypatch.setattr(ingest, "parser_identity", lambda: "another-parser")
    monkeypatch.setattr(graph_transactions, "ROWS_PER_TRANSACTION", 100)  # the first group commits, with the fingerprint cleared
    slept = []
    own_time(monkeypatch, ingest, sleep=slept.append)
    with monkeypatch.context() as patch:
        hiding_stored_vertices(patch, after=1000)  # the second group of vertices is refused, every time
        with pytest.raises(RuntimeError, match="still refused a graph vertex.*(Duplicated key|Found duplicate key).*marked not built"):  # the engine's own text, either mode
            ingest._build_graph(store, project, indexed)
    assert slept == [graph_transactions.DUPLICATE_WAIT]  # one wait, one more write, then the name
    assert code_graph_pending(store.query("SELECT graph_fingerprint FROM projects WHERE id=:p", {"p": project})[0])
    result = ingest._build_graph(store, project, indexed)
    assert graph(store, project) == before and result["nodes"] == first["nodes"]
    assert not code_graph_pending(store.query("SELECT graph_fingerprint FROM projects WHERE id=:p", {"p": project})[0])


def test_a_write_in_one_transaction_that_the_index_refuses_twice_says_that_the_previous_graph_is_kept(tmp_path, monkeypatch):
    store = Store(tmp_path / "memory")
    project, indexed = synthetic(store, tmp_path)
    ingest._build_graph(store, project, indexed)
    before = graph(store, project)
    monkeypatch.setattr(ingest, "parser_identity", lambda: "another-parser")
    own_time(monkeypatch, ingest, sleep=lambda seconds: None)
    hiding_stored_vertices(monkeypatch)
    with pytest.raises(RuntimeError, match="previous code graph is kept") as caught:
        ingest._build_graph(store, project, indexed)
    assert "not built" not in str(caught.value)  # nothing was committed: the readers still see the previous graph as built
    assert not code_graph_pending(store.query("SELECT graph_fingerprint FROM projects WHERE id=:p", {"p": project})[0])
    assert graph(store, project) == before


def test_edges_that_find_no_endpoint_for_a_vertex_deleted_shortly_before_are_written_after_one_wait(tmp_path, monkeypatch):
    # The same index, the other way it shows (#433): the group of edges counts fewer rows than it created.
    store = Store(tmp_path / "memory")
    project, indexed = synthetic(store, tmp_path)
    ingest._build_graph(store, project, indexed)
    monkeypatch.setattr(ingest, "parser_identity", lambda: "another-parser")
    _, moved = synthetic(store, tmp_path, shift=1000)  # new edges: a group of them is written
    real, calls, slept = Store.write_graph, [], []

    def failing(self, tx, project_id, nodes, edges):
        if edges and not calls:
            calls.append(1)
            raise RuntimeError("The database did not create every graph edge of a group")
        return real(self, tx, project_id, nodes, edges)
    monkeypatch.setattr(Store, "write_graph", failing)
    own_time(monkeypatch, ingest, sleep=slept.append)
    result = ingest._build_graph(store, project, moved)
    assert slept == [graph_transactions.DUPLICATE_WAIT] and result["graph_written_again"] is True
    assert store.query("SELECT count(*) AS n FROM edges WHERE project_id=:p", {"p": project})[0]["n"] == result["edges"]
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