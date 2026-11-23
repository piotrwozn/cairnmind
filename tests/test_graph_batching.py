"""The code graph written in groups of rows stores the graph that writing row by row stores (issue #167)."""
from __future__ import annotations

import threading

import pytest
from test_index_batching import code_tree, counted, dump, graph_row_by_row, rewrite

from cairnmind import graph_transactions, ingest
from cairnmind.arcade import ArcadeError
from cairnmind.code_graph import code_graph_pending
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


def misdirected(store, pid):
    """Edges whose stored vertices are not the ones their content names. dump() names an edge by from_id and to_id
    alone, so an edge written backwards would still compare equal there."""
    return store.query("SELECT id, out.id AS o, in.id AS i, from_id, to_id FROM edges WHERE project_id=:p "
                       "AND (out.id <> from_id OR in.id <> to_id OR out IS NULL OR in IS NULL)", {"p": pid})


def assert_same(grouped, first, single, second, step, transactions=False):
    """``transactions``: the grouped graph was written in several transactions. Each reports one graph reset, and the
    project's fingerprint is cleared in the first and set in the last, so those two events are counted by transaction."""
    one, other = dump(grouped, first), dump(single, second)
    for part in one:
        if part == "events" and transactions:
            def without(events):
                return [item for item in events if item[0] not in (("graph", "reset"), ("project", "upsert"))]
            assert without(one[part]) == without(other[part]) and ("graph", "reset") in dict(one[part]), (step, part)
            continue
        assert one[part] == other[part], (step, part)
    assert one["edges"], step
    assert misdirected(grouped, first) == [], step
    assert misdirected(single, second) == [], step


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


ROW_BY_ROW_REMOVAL = ("SELECT id FROM edges WHERE from_id=:id", "DELETE FROM nodes WHERE id=:id", "SELECT FROM `nodes` WHERE id=:id")
GROUPED_REMOVAL = ("SELECT from_id,to_id FROM edges WHERE", "DELETE FROM nodes WHERE project_id=:project AND id IN :ids")


def test_removed_graph_nodes_are_deleted_a_group_per_request(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    code_tree(root)
    store = Store(tmp_path / "memory")
    pid = store.add_project("p", root)["id"]
    ingest.index_project(store, pid)
    before = len(store.query("SELECT id FROM nodes WHERE project_id=:p", {"p": pid}))
    (root / "module_big.py").unlink()
    (root / "module_04.py").unlink()
    with monkeypatch.context() as patch:
        patch.setattr(graph_transactions, "ROWS_PER_TRANSACTION", 40)  # several groups from a small tree
        seen = counted(patch)
        ingest.index_project(store, pid)
    removed = before - len(store.query("SELECT id FROM nodes WHERE project_id=:p", {"p": pid}))
    assert removed > 2 * 40
    assert not [text for text in seen if text.startswith(ROW_BY_ROW_REMOVAL)]
    groups = -(-removed // 40)
    assert 0 < len([text for text in seen if text.startswith(GROUPED_REMOVAL)]) <= 2 * groups


def removal_row_by_row(self, tx, project_id, identifiers):
    """Removed vertices as before: one look for an edge, then one DELETE (which the journal reads first), per vertex."""
    for identifier in identifiers:
        if not tx.query("SELECT id FROM edges WHERE from_id=:id OR to_id=:id LIMIT 1", {"id": identifier}):
            tx.command("DELETE FROM nodes WHERE id=:id", {"id": identifier})


def all_row_by_row(patch):
    patch.setattr(Store, "write_graph", graph_row_by_row)
    patch.setattr(Store, "remove_graph_nodes", removal_row_by_row)


def mention(store, pid, root):
    """A note that links to code which goes away below: a symbol, a file that is deleted and a file that is renamed."""
    note = store.snapshot(pid, "note://about-code", "about the code", "note")
    store.upsert_node({"id": f"note:{pid}", "project_id": pid, "kind": "note", "label": "note", "source_id": note["id"]})
    linked = []
    for kind, label in (("symbol", "function_5_3"), ("file", str(root / "module_06.py")), ("file", str(root / "module_07.py"))):
        target = store.query("SELECT id FROM nodes WHERE project_id=:p AND kind=:k AND label=:l", {"p": pid, "k": kind, "l": label})
        assert len(target) == 1, label
        store.upsert_edge({"id": f"mentions:{pid}:{label}", "project_id": pid, "from_id": f"note:{pid}",
                           "to_id": target[0]["id"], "kind": "mentions", "source_id": note["id"]})
        linked.append(target[0]["id"])
    return linked


def remove_code(root):
    module = root / "module_05.py"
    module.write_text("".join(f"\n\ndef function_5_{number}(value):\n    return value\n" for number in (0, 1, 2)))
    (root / "module_06.py").unlink()
    (root / "module_07.py").rename(root / "renamed_07.py")
    big = root / "module_big.py"
    big.write_text("".join(f"\n\ndef big_{number}(value):\n    return value\n" for number in range(0, 90, 3)))


def test_grouped_removal_stores_the_row_by_row_graph_and_keeps_linked_vertices(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    code_tree(root)
    grouped, single = Store(tmp_path / "grouped"), Store(tmp_path / "single")
    first, second = grouped.add_project("p", root)["id"], single.add_project("p", root)["id"]
    ingest.index_project(grouped, first)
    with monkeypatch.context() as patch:
        all_row_by_row(patch)
        ingest.index_project(single, second)
    kept = {first: mention(grouped, first, root), second: mention(single, second, root)}
    rows = {pid: store.query("SELECT FROM nodes WHERE id IN :ids", {"ids": kept[pid]}) for pid, store in
            ((first, grouped), (second, single))}
    remove_code(root)
    for step, forget in (("removed symbols, a removed and a renamed file", False), ("written again in full", True)):
        if forget:
            rewrite(root, step)
        for store in (grouped, single):
            if forget:
                ingest.forget_index_signature(store)
        count = lambda pid, store: len(store.query("SELECT id FROM nodes WHERE project_id=:p", {"p": pid}))  # noqa: E731
        before = count(first, grouped)
        with monkeypatch.context() as patch:
            patch.setattr(graph_transactions, "ROWS_PER_TRANSACTION", 7)  # many groups, the kept vertices among them
            seen = counted(patch)
            ingest.index_project(grouped, first)
        with monkeypatch.context() as patch:
            all_row_by_row(patch)
            ingest.index_project(single, second)
        assert_same(grouped, first, single, second, step, transactions=True)
        assert not [text for text in seen if text.startswith(ROW_BY_ROW_REMOVAL)], step
        if not forget:
            assert before - count(first, grouped) > 3 * 7, step
            assert [text for text in seen if text.startswith(GROUPED_REMOVAL[1])], step
        # The vertices the note links to stay, row for row as they were (apart from is_current, which the files'
        # own writes clear when their source is replaced, in both stores alike), and its edges still reach them.
        for pid, store in ((first, grouped), (second, single)):
            again = store.query("SELECT FROM nodes WHERE id IN :ids", {"ids": kept[pid]})
            assert sorted(map(clean, again), key=str) == sorted(map(clean, rows[pid]), key=str), step
            reach = store.query("SELECT in.id AS i FROM edges WHERE kind='mentions' AND project_id=:p", {"p": pid})
            assert sorted(row["i"] for row in reach) == sorted(kept[pid]), step


def clean(row):
    return {key: value for key, value in row.items() if not key.startswith("@") and key != "is_current"}


def failed_removal(tmp_path, monkeypatch, rows):
    """Index a changed tree with ``rows`` rows to a transaction (None: as in the product), the removal failing in the
    middle: its second DELETE of vertices, or the only one when the removal is one group, is run and then fails."""
    root = tmp_path / "repo"
    root.mkdir()
    code_tree(root)
    grouped, first, single, second, *_ = both(tmp_path, root, monkeypatch)
    remove_code(root)
    real, deletes, build, before = JournalTransaction.command, [], ingest._build_graph, {}

    def building(store, *args, **kwargs):
        before["graph"] = graph(store, first)
        before["traversed"] = store.query("SELECT out.id AS o, in.id AS i FROM edges WHERE project_id=:p", {"p": first})
        return build(store, *args, **kwargs)

    def failing(self, sql, params=None):
        result = real(self, sql, params)
        if sql.startswith(GROUPED_REMOVAL[1]):
            deletes.append(len(params["ids"]))
            if len(deletes) == (1 if rows is None else 2):  # the group is deleted, then the connection drops
                raise RuntimeError("the connection dropped")
        return result
    with monkeypatch.context() as patch:
        if rows is not None:
            patch.setattr(graph_transactions, "ROWS_PER_TRANSACTION", rows)
        patch.setattr(JournalTransaction, "command", failing)
        patch.setattr(ingest, "_build_graph", building)
        with pytest.raises(RuntimeError, match="connection dropped"):
            ingest.index_project(grouped, first)
    assert len(deletes) == (1 if rows is None else 2) and all(deletes)
    assert before["graph"]["nodes"]
    return grouped, first, single, second, before


def test_a_failure_in_the_middle_of_the_removal_leaves_the_previous_graph_intact(tmp_path, monkeypatch):
    # The write is one transaction, as for every graph under graph_transactions.ROWS_PER_TRANSACTION rows: all or nothing.
    grouped, first, single, second, before = failed_removal(tmp_path, monkeypatch, None)
    assert graph(grouped, first) == before["graph"]
    again = grouped.query("SELECT out.id AS o, in.id AS i FROM edges WHERE project_id=:p", {"p": first})
    assert sorted((row["o"], row["i"]) for row in again) == sorted((row["o"], row["i"]) for row in before["traversed"])

    ingest.index_project(grouped, first)  # the next run completes it
    with monkeypatch.context() as patch:
        all_row_by_row(patch)
        ingest.index_project(single, second)
    assert_same(grouped, first, single, second, "after the failed run")


def test_a_removal_that_fails_in_a_write_over_several_transactions_leaves_the_graph_not_built_and_the_next_run_completes_it(
        tmp_path, monkeypatch):
    # Over several transactions (#377) what an earlier one committed stays: the graph is reported as not built.
    grouped, first, single, second, _ = failed_removal(tmp_path, monkeypatch, 20)
    assert code_graph_pending(grouped.query("SELECT graph_fingerprint FROM projects WHERE id=:p", {"p": first})[0])
    again = grouped.query("SELECT out.id AS o, in.id AS i FROM edges WHERE project_id=:p", {"p": first})
    assert all(row["o"] and row["i"] for row in again)

    with monkeypatch.context() as patch:
        patch.setattr(graph_transactions, "ROWS_PER_TRANSACTION", 20)
        ingest.index_project(grouped, first)  # the next run completes it
    with monkeypatch.context() as patch:
        all_row_by_row(patch)
        ingest.index_project(single, second)
    assert_same(grouped, first, single, second, "after the failed run", transactions=True)


def test_a_removal_group_the_database_did_not_delete_in_full_fails_its_transaction(tmp_path, monkeypatch):
    store = Store(tmp_path / "memory")
    pid = store.add_project("p", tmp_path)["id"]
    nodes = [{"id": f"n{index}", "project_id": pid, "kind": "symbol", "label": f"n{index}"} for index in range(3)]
    with store.transaction() as tx:
        Journal.touch(tx, pid, "graph", op="reset")
        store.write_graph(tx, pid, nodes, [])
    real = JournalTransaction.command
    monkeypatch.setattr(JournalTransaction, "command", lambda self, sql, params=None: real(
        self, sql, {**params, "ids": params["ids"][:-1]}) if sql.startswith(GROUPED_REMOVAL[1]) else real(self, sql, params))
    with pytest.raises(RuntimeError, match="every graph vertex"):
        with store.transaction() as tx:
            Journal.touch(tx, pid, "graph", op="reset")
            store.remove_graph_nodes(tx, pid, ["n0", "n1", "n2"])
    assert len(store.query("SELECT id FROM nodes WHERE project_id=:p", {"p": pid})) == 3


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
