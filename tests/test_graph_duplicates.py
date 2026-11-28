"""A vertex stored twice under one id is noticed after a graph write, removed with its edges put back, and said (#456)."""
from __future__ import annotations

import json
import re

import pytest
from test_graph_transactions import counted_requests, graph, synthetic
from test_index_batching import code_tree, dump

from cairnmind import graph_duplicates, ingest
from cairnmind.code_graph import code_graph_pending
from cairnmind.sql import clean_row
from cairnmind.store import Store
from cairnmind.terminal_i18n import tr


def store_twice(store, identifier):
    """What the unique index of `nodes.id` is there to refuse, behind the product: the index is dropped, the vertex is
    stored again, and its edge to the file is moved to the second copy (the one the product removes). Returns the rids."""
    store.client.command("DROP INDEX `nodes[id]`")
    original = clean_row(store.query("SELECT FROM nodes WHERE id=:id", {"id": identifier})[0])
    store.client.command("CREATE VERTEX nodes CONTENT :row", {"row": original})
    first, second = sorted((row["rid"] for row in store.query("SELECT @rid AS rid FROM nodes WHERE id=:id", {"id": identifier})),
                           key=lambda rid: tuple(int(part) for part in rid.lstrip("#").split(":")))
    for edge in map(clean_row, store.query("SELECT FROM edges WHERE kind='snapshot_of' AND from_id=:id", {"id": identifier})):
        store.client.command("DELETE FROM edges WHERE id=:id", {"id": edge["id"]})
        store.client.command(f"CREATE EDGE edges FROM {second} TO (SELECT FROM nodes WHERE id=:to) CONTENT :edge",
                             {"to": edge["to_id"], "edge": edge})
    return first, second


def test_a_vertex_stored_twice_behind_the_product_is_removed_with_its_edge_put_back_said_and_the_graph_is_a_full_rebuild(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    code_tree(root)
    stored, twin, rebuilt = Store(tmp_path / "stored"), Store(tmp_path / "twin"), Store(tmp_path / "rebuilt")
    project, twin_project = stored.add_project("p", root)["id"], twin.add_project("p", root)["id"]
    ingest.index_project(stored, project)
    ingest.index_project(twin, twin_project)
    source = stored.query("SELECT id FROM nodes WHERE kind='source' AND label LIKE '%module_03.py'")[0]["id"]
    first, second = store_twice(stored, source)
    assert graph_duplicates.surplus(stored, project) == graph_duplicates.surplus(stored) == 1
    with (root / "module_05.py").open("a") as handle:  # a change to one file: an incremental pass
        handle.write("\n\ndef added(value):\n    return value\n")
    result = ingest.index_project(stored, project)
    ingest.index_project(twin, twin_project)  # the same pass on a store that never held a duplicate
    assert result["graph_duplicates_removed"] == 1
    assert graph_duplicates.REPAIRED.format(count=1, project="p") in result["errors"]
    assert [row["rid"] for row in stored.query("SELECT @rid AS rid FROM nodes WHERE id=:id", {"id": source})] == [first]
    assert stored.query("SELECT @out AS o FROM edges WHERE kind='snapshot_of' AND from_id=:id", {"id": source})[0]["o"] == first
    assert stored.query("SELECT count(*) AS n FROM edges WHERE out IS NULL OR in IS NULL")[0]["n"] == 0
    assert graph_duplicates.surplus(stored) == 0 and not code_graph_pending(
        stored.query("SELECT graph_fingerprint FROM projects WHERE id=:p", {"p": project})[0])
    # Everything stored is the same; the journal says one more reset of the graph, the repair's own.
    assert {k: v for k, v in dump(stored, project).items() if k != "events"} == {
        k: v for k, v in dump(twin, twin_project).items() if k != "events"}
    # A full rebuild of the same files: the same code graph (the evidence vertices of replaced sources stay, as ever).
    other = rebuilt.add_project("p", root)["id"]
    ingest.index_project(rebuilt, other)
    code = lambda rows: [row for row in rows if not row.startswith('["source"')]  # noqa: E731
    assert code(dump(stored, project)["nodes"]) == code(dump(rebuilt, other)["nodes"])
    # And the engine itself agrees that no id is held twice: the unique index can be made again.
    stored.client.command("CREATE INDEX ON nodes(id) UNIQUE NULL_STRATEGY SKIP")

def test_the_copy_that_stays_gets_what_the_pass_computed_where_the_two_copies_differ(tmp_path):
    store = Store(tmp_path / "memory")
    project, indexed = synthetic(store, tmp_path)
    ingest._build_graph(store, project, indexed)
    identifier = store.query("SELECT id FROM nodes WHERE project_id=:p AND kind!='source' LIMIT 1", {"p": project})[0]["id"]
    first, second = store_twice(store, identifier)
    current = clean_row(store.query("SELECT FROM nodes WHERE @rid=:rid", {"rid": second})[0])
    store.client.command("UPDATE nodes SET label='stale' WHERE @rid=:rid", {"rid": first})
    assert graph_duplicates.repair(store, project, [{**current, "label": "computed"}]) == (1, 0)
    rows = store.query("SELECT @rid AS rid,label FROM nodes WHERE id=:id", {"id": identifier})
    assert [(row["rid"], row["label"]) for row in rows] == [(first, "computed")]


def hiding_the_lookups_by_id(monkeypatch):
    """The engine's index of ids that does not find a row that is stored: every request that looks a vertex or an edge up by its
    id answers nothing, and says that it was asked. What the repair writes then is written without them or it is a third copy."""
    from cairnmind.journal import JournalTransaction
    asked, real = [], JournalTransaction.query
    lookup = re.compile(r"(?<![\w.])id\s*(=|IN)\s*:", re.IGNORECASE)

    def query(self, sql, params=None):
        if re.search(r"FROM (nodes|edges)\b", sql) and lookup.search(sql.split(" WHERE ", 1)[-1]):
            asked.append(sql)
            return []
        return real(self, sql, params)

    monkeypatch.setattr(JournalTransaction, "query", query)
    for name in ("command", "script"):
        real_write = getattr(JournalTransaction, name)

        def write(self, *args, _real=real_write, **kwargs):
            statements = args[0] if isinstance(args[0], list) else [args[0]]
            asked.extend(sql for sql in statements if re.search(r"\(SELECT FROM nodes WHERE id", sql))  # an edge's ends by id
            return _real(self, *args, **kwargs)

        monkeypatch.setattr(JournalTransaction, name, write)
    return asked


def test_the_repair_writes_by_the_record_id_it_holds_and_cannot_make_a_third_copy_where_the_lookup_misses(tmp_path, monkeypatch):
    store = Store(tmp_path / "memory")
    project, indexed = synthetic(store, tmp_path)
    ingest._build_graph(store, project, indexed)
    identifier = store.query("SELECT id FROM nodes WHERE project_id=:p AND kind!='source' AND label LIKE '%f7' LIMIT 1", {"p": project})[0]["id"]
    first, second = store_twice(store, identifier)
    before = store.query("SELECT count(*) AS n FROM edges WHERE project_id=:p", {"p": project})[0]["n"]
    current = clean_row(store.query("SELECT FROM nodes WHERE @rid=:rid", {"rid": second})[0])
    asked = hiding_the_lookups_by_id(monkeypatch)
    assert graph_duplicates.repair(store, project, [{**current, "label": "computed"}]) == (1, 0)
    monkeypatch.undo()
    assert not [sql for sql in asked if "CREATE" in sql or "UPDATE" in sql or "DELETE" in sql]  # no write rests on a lookup by id
    rows = store.query("SELECT @rid AS rid,label FROM nodes WHERE id=:id", {"id": identifier})
    assert [(row["rid"], row["label"]) for row in rows] == [(first, "computed")]  # one row: the repair made no third
    assert graph_duplicates.surplus(store, project) == 0
    assert store.query("SELECT count(*) AS n FROM edges WHERE project_id=:p", {"p": project})[0]["n"] == before  # every edge is back, once


def store_edge_twice(store, project):
    """The same behind the product for an edge: its index is dropped and a second edge of that id is created between the same ends."""
    store.client.command("DROP INDEX `edges[id]`")
    original = clean_row(store.query("SELECT FROM edges WHERE project_id=:p AND kind='calls' LIMIT 1", {"p": project})[0])
    store.client.command("CREATE EDGE edges FROM (SELECT FROM nodes WHERE id=:f) TO (SELECT FROM nodes WHERE id=:t) CONTENT :edge",
                         {"f": original["from_id"], "t": original["to_id"], "edge": original})
    return original["id"]


def test_an_edge_stored_twice_under_one_id_is_noticed_removed_and_said_by_the_pass(tmp_path, monkeypatch):
    store = Store(tmp_path / "memory")
    project, indexed = synthetic(store, tmp_path)
    ingest._build_graph(store, project, indexed)
    before = graph(store, project)
    identifier = store_edge_twice(store, project)
    assert graph_duplicates.surplus(store, project, "edges") == 1 and graph_duplicates.surplus(store, project) == 0
    monkeypatch.setattr(ingest, "parser_identity", lambda: "another-parser")  # a full write of the same graph
    result = ingest._build_graph(store, project, indexed)
    assert result["graph_duplicate_edges_removed"] == 1 and "graph_duplicates_removed" not in result
    assert store.query("SELECT count(*) AS n FROM edges WHERE id=:id", {"id": identifier})[0]["n"] == 1
    assert graph_duplicates.surplus(store, project, "edges") == 0
    assert graph(store, project) == before
    assert store.query("SELECT count(*) AS n FROM edges WHERE out IS NULL OR in IS NULL")[0]["n"] == 0


def test_the_index_command_repairs_an_idle_project_whose_files_did_not_change(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    code_tree(root)
    stored, twin = Store(tmp_path / "stored"), Store(tmp_path / "twin")
    project, twin_project = stored.add_project("p", root)["id"], twin.add_project("p", root)["id"]
    ingest.index_project(stored, project)
    ingest.index_project(twin, twin_project)
    source = stored.query("SELECT id FROM nodes WHERE kind='source' AND label LIKE '%module_03.py'")[0]["id"]
    store_twice(stored, source)
    edge = store_edge_twice(stored, project)
    assert graph_duplicates.by_project(stored) == [{"project": "p", "vertices": 1, "edges": 1}]
    result = ingest.index_project(stored, project)  # what `cairnmind index p` runs; no file changed since the last pass
    ingest.index_project(twin, twin_project)
    assert result["graph_duplicates_removed"] == 1 and result["graph_duplicate_edges_removed"] == 1
    assert graph_duplicates.REPAIRED.format(count=1, project="p") in result["errors"]
    assert graph_duplicates.EDGES_REPAIRED.format(count=1, project="p") in result["errors"]
    assert graph_duplicates.by_project(stored) == [] and stored.query("SELECT count(*) AS n FROM edges WHERE id=:id", {"id": edge})[0]["n"] == 1
    assert {k: v for k, v in dump(stored, project).items() if k != "events"} == {
        k: v for k, v in dump(twin, twin_project).items() if k != "events"}
    again = ingest.index_project(stored, project)  # and the next one finds nothing to do
    assert again["graph_unchanged"] is True and "graph_duplicates_removed" not in again


def test_a_duplicate_that_the_pass_did_not_make_of_a_vertex_whose_id_goes_away_is_removed_by_the_pass(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    code_tree(root)
    store = Store(tmp_path / "memory")
    project = store.add_project("p", root)["id"]
    ingest.index_project(store, project)
    symbol = store.query("SELECT id FROM nodes WHERE project_id=:p AND label LIKE '%function_5_4' AND kind!='source' LIMIT 1", {"p": project})[0]["id"]
    store_twice(store, symbol)
    text = (root / "module_05.py").read_text()
    (root / "module_05.py").write_text("\n" + text)  # every line moves: the symbol's id, which holds its line, is another
    result = ingest.index_project(store, project)
    assert not any("did not delete" in str(error) for error in result["errors"])
    assert store.query("SELECT count(*) AS n FROM nodes WHERE id=:id", {"id": symbol})[0]["n"] == 0
    assert graph_duplicates.surplus(store, project) == 0


def test_a_repair_that_does_not_end_whole_fails_by_name_and_leaves_the_graph_marked_not_built(tmp_path, monkeypatch):
    store = Store(tmp_path / "memory")
    project, indexed = synthetic(store, tmp_path)
    ingest._build_graph(store, project, indexed)
    monkeypatch.setattr(ingest, "parser_identity", lambda: "another-parser")  # a full write
    monkeypatch.setattr(graph_duplicates, "surplus", lambda reader, project_id=None, table="nodes": 1)  # a duplicate that stays
    with pytest.raises(RuntimeError, match="still stored twice"):
        ingest._build_graph(store, project, indexed)
    assert code_graph_pending(store.query("SELECT graph_fingerprint FROM projects WHERE id=:p", {"p": project})[0])


def test_the_check_costs_two_requests_where_rows_were_written_and_none_where_nothing_was(tmp_path, monkeypatch):
    def requests_of_a_pass(name, writes, check):
        store = Store(tmp_path / name / "memory")
        project, indexed = synthetic(store, tmp_path / name)
        ingest._build_graph(store, project, indexed)
        with monkeypatch.context() as patch:
            if writes:  # another parser: a full write, every vertex is written again
                patch.setattr(ingest, "parser_identity", lambda: "another-parser")
            patch.setattr(graph_duplicates, "repair", check)
            requests = counted_requests(patch)
            result = ingest._build_graph(store, project, indexed)
        return len(requests), result

    calls = []
    real = graph_duplicates.repair
    counting = lambda store, project, written: (calls.append(project), real(store, project, written))[1]  # noqa: E731
    wrote, result = requests_of_a_pass("wrote", True, counting)
    assert result["graph_written"]["nodes"] > 0 and len(calls) == 1
    assert wrote - requests_of_a_pass("wrote-too", True, lambda store, project, written: (0, 0))[0] == 2  # the counts of vertices and edges
    calls.clear()
    nothing, result = requests_of_a_pass("same", False, counting)
    assert result["graph_unchanged"] is True and not calls  # nothing written: the check is not asked
    assert nothing == requests_of_a_pass("same-too", False, lambda store, project, written: (0, 0))[0]

def test_a_store_that_holds_a_vertex_twice_is_counted_by_doctor_and_said_in_both_languages(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from cairnmind import api_runtime, cli, doctor_attention, integrations, store_writer
    from test_first_start_round2 import healthy
    store = Store(tmp_path / "memory")
    project, indexed = synthetic(store, tmp_path)
    ingest._build_graph(store, project, indexed)
    store_twice(store, f"source:{next(iter(indexed.values()))['source']['id']}")
    sentence = graph_duplicates.IN_PROJECT.format(project="synthetic", vertices=1, edges=0)
    assert "`cairnmind index synthetic`" in sentence  # the command that repairs it, with the project's name as typed
    monkeypatch.setattr(cli, "service_alive", lambda s: False)
    monkeypatch.setattr(integrations, "doctor", lambda s: dict(healthy()))
    monkeypatch.setattr(store_writer, "inspect", lambda s: {"role": "writer"})
    monkeypatch.setattr(api_runtime, "coding_profiles_status", lambda s: [])
    monkeypatch.setattr(cli, "store_for_read", lambda s: store)
    for terminal in (True, False):
        monkeypatch.setattr(cli, "_stdout_is_terminal", lambda: terminal)
        shown = CliRunner().invoke(cli.app, ["--data-dir", str(store.data_dir), "doctor"])
        assert shown.exit_code == 0, shown.output  # a note, never a fault: `cairnmind index` repairs it
        if terminal:
            lines = shown.output.splitlines()
            assert lines[lines.index(doctor_attention.NOTES) + 1] == f"  - {sentence}"
        else:
            report = json.loads(shown.output)
            assert (report["vertices_stored_twice"], report["edges_stored_twice"]) == (1, 0)
            assert report["graphs_stored_twice"] == [{"project": "synthetic", "vertices": 1, "edges": 0}]
    monkeypatch.setattr(cli, "store_for_read", lambda s: (_ for _ in ()).throw(ValueError("no store")))
    report = json.loads(CliRunner().invoke(cli.app, ["--data-dir", str(store.data_dir), "doctor", "--json"]).output)
    assert report["vertices_stored_twice"] is None and report["edges_stored_twice"] is None and report["graphs_stored_twice"] == []
    assert tr(graph_duplicates.REPAIRED, "pl", count=1, project="p").startswith("Wierzchołki grafu projektu p, które baza zapisała dwa razy pod jednym id: 1.")
    assert tr(graph_duplicates.EDGES_REPAIRED, "pl", count=2, project="p").startswith("Krawędzie grafu projektu p, które baza zapisała dwa razy pod jednym id: 2.")
    assert tr(graph_duplicates.IN_PROJECT, "pl", project="p", vertices=3, edges=0).startswith("Projekt p ma wierzchołki grafu zapisane dwa razy pod jednym id (3) lub krawędzie (0).")


def test_a_graph_without_a_duplicate_is_not_changed_by_the_check(tmp_path):
    store = Store(tmp_path / "memory")
    project, indexed = synthetic(store, tmp_path)
    ingest._build_graph(store, project, indexed)
    before = graph(store, project)
    assert graph_duplicates.repair(store, project) == 0 and graph(store, project) == before
