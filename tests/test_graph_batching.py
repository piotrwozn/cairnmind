"""The code graph written in groups of rows stores the graph that writing row by row stores (issue #167)."""
from __future__ import annotations

import threading

import pytest
from test_index_batching import code_tree, counted, dump, graph_row_by_row, rewrite

from cairnmind import ingest
from cairnmind.arcade import ArcadeError
from cairnmind.journal import Journal, JournalTransaction
from cairnmind.store import GRAPH_ROWS_PER_REQUEST, Store

ROW_BY_ROW_EDGE = "CREATE EDGE edges FROM (SELECT FROM nodes WHERE id=:from_id)"


def graph(store, pid):
    one = dump(store, pid)
    return {"nodes": one["nodes"], "edges": one["edges"]}


def both(tmp_path, root, monkeypatch):
    """A store written in groups and one written row by row, from the same tree. Only the graph writes differ."""
    grouped, single = Store(tmp_path / "grouped"), Store(tmp_path / "single")
    first, second = grouped.add_project("p", root)["id"], single.add_project("p", root)["id"]
    result = ingest.index_project(grouped, first)
    with monkeypatch.context() as patch:
        patch.setattr(Store, "write_graph", graph_row_by_row)
        reference = ingest.index_project(single, second)
    return grouped, first, single, second, result, reference


def index_both(grouped, first, single, second, monkeypatch, *, forget=False):
    for store in (grouped, single):
        if forget:  # the next run replaces the whole graph instead of the rows that differ from the remembered one
            ingest.forget_index_signature(store)
    with monkeypatch.context() as patch:
        seen = counted(patch)
        result = ingest.index_project(grouped, first)
    with monkeypatch.context() as patch:
        patch.setattr(Store, "write_graph", graph_row_by_row)
        reference = ingest.index_project(single, second)
    keys = ("files", "changed", "unchanged", "deleted", "chunks", "nodes", "edges")
    assert {key: result[key] for key in keys} == {key: reference[key] for key in keys}
    return result, seen


def assert_same(grouped, first, single, second, step):
    one, other = dump(grouped, first), dump(single, second)
    for part in one:
        assert one[part] == other[part], (step, part)


def test_the_grouped_graph_is_the_row_by_row_graph_for_edited_renamed_and_deleted_files(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    code_tree(root)
    grouped, first, single, second, result, _ = both(tmp_path, root, monkeypatch)
    # More rows than one request writes, so that every kind of statement is sent in several groups.
    assert result["graph_written"]["nodes"] > GRAPH_ROWS_PER_REQUEST and result["graph_written"]["edges"] > GRAPH_ROWS_PER_REQUEST
    assert_same(grouped, first, single, second, "first")

    module = root / "module_01.py"
    module.write_text(module.read_text().replace("helper_1(value)", "helper_1(value) * 2") + "\n\ndef added(v):\n    return helper_1(v)\n")
    (root / "module_02.py").rename(root / "renamed_02.py")
    (root / "module_03.py").unlink()
    for step, forget in (("edited, renamed and deleted", False), ("written again in full", True)):
        if forget:
            rewrite(root, step)
        result, seen = index_both(grouped, first, single, second, monkeypatch, forget=forget)
        assert bool(result["deleted"]) is not forget and not result["errors"] and not result["graph_unchanged"]
        assert_same(grouped, first, single, second, step)
        written = result["graph_written"]["nodes"] + result["graph_written"]["edges"]
        assert written > (2 * GRAPH_ROWS_PER_REQUEST if forget else 0), step
        # No graph row was written by a request of its own.
        assert not [text for text in seen if text.startswith((ROW_BY_ROW_EDGE, "UPDATE `nodes` SET", "CREATE VERTEX nodes CONTENT {"))]
        grouped_requests = [text for text in seen if text.startswith(("CREATE EDGE edges", "UPDATE nodes SET `", "CREATE VERTEX nodes"))]
        assert 0 < len(grouped_requests) <= written // GRAPH_ROWS_PER_REQUEST + 3, step


def test_a_failure_in_the_middle_of_a_group_leaves_the_previous_graph_intact(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    code_tree(root)
    grouped, first, single, second, *_ = both(tmp_path, root, monkeypatch)
    rewrite(root)
    ingest.forget_index_signature(grouped)
    real, groups, build, before = JournalTransaction.script, [], ingest._build_graph, {}

    def building(store, *args, **kwargs):
        # The graph as the files' own writes left it (they mark the rows of a replaced source as not current).
        before["graph"] = graph(store, first)
        before["traversed"] = store.query("SELECT out.id AS o, in.id AS i FROM edges WHERE project_id=:p", {"p": first})
        return build(store, *args, **kwargs)

    def failing(self, statements, params, project_id):
        groups.append(statements[0].split(" ")[0])
        if groups.count("CREATE") == 2:  # the second group of edges: half of it is written, then the connection drops
            real(self, statements[:len(statements) // 2], params, project_id)
            raise RuntimeError("the connection dropped")
        return real(self, statements, params, project_id)
    with monkeypatch.context() as patch:
        patch.setattr(JournalTransaction, "script", failing)
        patch.setattr(ingest, "_build_graph", building)
        with pytest.raises(RuntimeError, match="connection dropped"):
            ingest.index_project(grouped, first)
    assert "UPDATE" in groups and groups.count("CREATE") == 2
    # The graph is the previous one, row for row, and it is still traversed the same way.
    assert before["graph"]["edges"] and graph(grouped, first) == before["graph"]
    again = grouped.query("SELECT out.id AS o, in.id AS i FROM edges WHERE project_id=:p", {"p": first})
    assert sorted((row["o"], row["i"]) for row in again) == sorted((row["o"], row["i"]) for row in before["traversed"])
    assert all(row["o"] and row["i"] for row in again)

    ingest.index_project(grouped, first)  # the next run completes it
    with monkeypatch.context() as patch:
        patch.setattr(Store, "write_graph", graph_row_by_row)
        ingest.forget_index_signature(single)
        ingest.index_project(single, second)
    assert graph(grouped, first) == graph(single, second)


def test_a_group_the_database_did_not_write_in_full_fails_its_transaction(tmp_path, monkeypatch):
    store = Store(tmp_path / "memory")
    pid = store.add_project("p", tmp_path)["id"]
    nodes = [{"id": f"n{index}", "project_id": pid, "kind": "symbol", "label": f"n{index}", "metadata": {"line": index}}
             for index in range(3)]
    edges = [{"id": "e0", "project_id": pid, "from_id": "n0", "to_id": "n1", "kind": "calls"},
             {"id": "e1", "project_id": pid, "from_id": "n1", "to_id": "gone", "kind": "calls"}]
    # The checks refuse an edge without its endpoint before anything of its group is sent.
    with pytest.raises(ValueError, match="endpoints must exist"):
        with store.transaction() as tx:
            Journal.touch(tx, pid, "graph", op="reset")
            store.write_graph(tx, pid, nodes, edges)
    real = Store._checked_edge
    # Past the checks, CREATE EDGE writes nothing and says nothing when an endpoint is missing: the count catches it.
    monkeypatch.setattr(Store, "_checked_edge", lambda self, tx, edge: (
        {"status": "observed", "is_current": True, **edge}, None) if edge["to_id"] == "gone" else real(self, tx, edge))
    with pytest.raises(RuntimeError, match="every graph edge"):
        with store.transaction() as tx:
            Journal.touch(tx, pid, "graph", op="reset")
            store.write_graph(tx, pid, nodes, edges)
    assert not store.query("SELECT id FROM nodes WHERE project_id=:p", {"p": pid})
    assert not store.query("SELECT id FROM edges WHERE project_id=:p", {"p": pid})

    # The engine refuses a statement in the middle of a script, after the one before it was applied.
    source = store.snapshot(pid, "note://a", "a", "note")
    with pytest.raises(ArcadeError):
        with store.transaction() as tx:
            Journal.touch(tx, pid, "graph", op="reset")
            store.write_graph(tx, pid, nodes, edges[:1])
            tx.script(["UPDATE nodes SET label=:label WHERE id=:node",
                       "CREATE EDGE edges FROM (SELECT FROM sources WHERE id=:source) TO (SELECT FROM nodes WHERE id=:node) "
                       "CONTENT :edge", "UPDATE nodes SET label=:label WHERE id='n2'"],
                      {"label": "changed", "node": "n1", "source": source["id"], "edge": {**edges[0], "id": "e9"}}, pid)
    assert not store.query("SELECT id FROM nodes WHERE project_id=:p", {"p": pid})
    assert not store.query("SELECT id FROM edges WHERE project_id=:p", {"p": pid})
    with store.transaction() as tx:  # and the next transaction is an ordinary one
        Journal.touch(tx, pid, "graph", op="reset")
        store.write_graph(tx, pid, nodes, edges[:1])
    assert len(store.query("SELECT id FROM edges WHERE project_id=:p", {"p": pid})) == 1


def test_grouped_statements_are_only_for_the_graph_of_a_project_reported_as_reset(tmp_path):
    store = Store(tmp_path / "memory")
    pid = store.add_project("p", tmp_path)["id"]
    other = store.add_project("q", tmp_path / "q")["id"]
    node = {"id": "n", "project_id": pid, "kind": "symbol", "label": "n"}
    statement = ["UPDATE nodes SET label=:label WHERE id=:id"]
    with store.transaction() as tx:
        with pytest.raises(ValueError, match="reported as reset|reports the project's graph as reset"):
            tx.script(statement, {"label": "x", "id": "n"}, pid)
        Journal.touch(tx, pid, "graph", op="reset")
        with pytest.raises(ValueError, match="reports the project's graph as reset"):
            tx.script(statement, {"label": "x", "id": "n"}, other)
        for wrong in (["DELETE FROM sources WHERE id=:id"], ["UPDATE nodes SET label=:label WHERE id=:id; DELETE FROM sources"],
                      ["SELECT id FROM nodes", *statement], []):
            with pytest.raises(ValueError):
                tx.script(wrong, {"label": "x", "id": "n"}, pid)
        with pytest.raises(ValueError, match="belongs to its project"):
            store.write_graph(tx, pid, [{**node, "project_id": other}], [])
        with pytest.raises(ValueError, match="twice"):
            store.write_graph(tx, pid, [node, node], [])
    # Outside a graph reset the rows are written one by one, and the journal names each of them.
    cursor = Journal(store).cursor()
    with store.transaction() as tx:
        store.write_graph(tx, pid, [node], [])
    events = Journal(store).read(cursor)["events"]
    assert [(event["entity_type"], event["entity_id"]) for event in events] == [("node", "n")]


def test_two_projects_indexed_at_once_each_get_their_own_graph(tmp_path, monkeypatch):
    roots = []
    for name, files in (("a", 8), ("b", 11)):
        root = tmp_path / name
        root.mkdir()
        code_tree(root, files=files)
        roots.append(root)
    single = Store(tmp_path / "single")
    references = [single.add_project(root.name, root)["id"] for root in roots]
    with monkeypatch.context() as patch:
        patch.setattr(Store, "write_graph", graph_row_by_row)
        for pid in references:
            ingest.index_project(single, pid)
    grouped = Store(tmp_path / "grouped")
    projects = [grouped.add_project(root.name, root)["id"] for root in roots]
    for round_ in ("first", "rewritten"):
        if round_ == "rewritten":
            for root in roots:
                rewrite(root)
            ingest.forget_index_signature(grouped)
            ingest.forget_index_signature(single)
            with monkeypatch.context() as patch:
                patch.setattr(Store, "write_graph", graph_row_by_row)
                for pid in references:
                    ingest.index_project(single, pid)
        start, failures = threading.Barrier(2), []

        def run(pid):
            try:
                start.wait()
                ingest.index_project(grouped, pid)
            except BaseException as exc:  # noqa: BLE001 - reported by the assertion below
                failures.append(exc)
        threads = [threading.Thread(target=run, args=(pid,)) for pid in projects]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)
        assert not failures and not any(thread.is_alive() for thread in threads)
        for pid, reference in zip(projects, references):
            assert graph(grouped, pid) == graph(single, reference), round_
        # No edge joins vertices of another project.
        assert not grouped.query("SELECT id FROM edges WHERE out.project_id != project_id OR in.project_id != project_id")
