"""A graph larger than one transaction is written in several, and is still the graph one would have written (#377)."""
from __future__ import annotations

import pytest
from test_index_batching import code_tree, dump

from cairnmind import graph_transactions, ingest
from cairnmind.code_graph import code_graph_pending
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
