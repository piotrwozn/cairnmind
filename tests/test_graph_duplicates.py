"""A vertex stored twice under one id is noticed after a graph write, removed with its edges put back, and said (#456)."""
from __future__ import annotations

import json

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
    edge = clean_row(store.query("SELECT FROM edges WHERE kind='snapshot_of' AND from_id=:id", {"id": identifier})[0])
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

def test_a_repair_that_does_not_end_whole_fails_by_name_and_leaves_the_graph_marked_not_built(tmp_path, monkeypatch):
    store = Store(tmp_path / "memory")
    project, indexed = synthetic(store, tmp_path)
    ingest._build_graph(store, project, indexed)
    monkeypatch.setattr(ingest, "parser_identity", lambda: "another-parser")  # a full write
    monkeypatch.setattr(graph_duplicates, "surplus", lambda reader, project_id=None: 1)  # a duplicate that stays
    with pytest.raises(RuntimeError, match="still stored twice"):
        ingest._build_graph(store, project, indexed)
    assert code_graph_pending(store.query("SELECT graph_fingerprint FROM projects WHERE id=:p", {"p": project})[0])


def test_the_check_costs_one_request_where_vertices_were_written_and_none_where_nothing_was(tmp_path, monkeypatch):
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
    counting = lambda store, project: (calls.append(project), real(store, project))[1]  # noqa: E731
    wrote, result = requests_of_a_pass("wrote", True, counting)
    assert result["graph_written"]["nodes"] > 0 and len(calls) == 1
    assert wrote - requests_of_a_pass("wrote-too", True, lambda store, project: 0)[0] == 1  # the one count
    calls.clear()
    nothing, result = requests_of_a_pass("same", False, counting)
    assert result["graph_unchanged"] is True and not calls  # nothing written: the check is not asked
    assert nothing == requests_of_a_pass("same-too", False, lambda store, project: 0)[0]

def test_a_store_that_holds_a_vertex_twice_is_counted_by_doctor_and_said_in_both_languages(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from cairnmind import api_runtime, cli, doctor_attention, integrations, store_writer
    from test_first_start_round2 import healthy
    store = Store(tmp_path / "memory")
    project, indexed = synthetic(store, tmp_path)
    ingest._build_graph(store, project, indexed)
    store_twice(store, f"source:{next(iter(indexed.values()))['source']['id']}")
    sentence = graph_duplicates.IN_STORE.format(count=1)
    monkeypatch.setattr(cli, "service_alive", lambda s: False)
    monkeypatch.setattr(integrations, "doctor", lambda s: dict(healthy()))
    monkeypatch.setattr(store_writer, "inspect", lambda s: {"role": "writer"})
    monkeypatch.setattr(api_runtime, "coding_profiles_status", lambda s: [])
    monkeypatch.setattr(cli, "store_for_read", lambda s: store)
    for terminal in (True, False):
        monkeypatch.setattr(cli, "_stdout_is_terminal", lambda: terminal)
        shown = CliRunner().invoke(cli.app, ["--data-dir", str(store.data_dir), "doctor"])
        assert shown.exit_code == 0, shown.output  # a note, never a fault: the next pass that writes the graph repairs it
        if terminal:
            lines = shown.output.splitlines()
            assert lines[lines.index(doctor_attention.NOTES) + 1] == f"  - {sentence}"
        else:
            assert json.loads(shown.output)["vertices_stored_twice"] == 1
    monkeypatch.setattr(cli, "store_for_read", lambda s: (_ for _ in ()).throw(ValueError("no store")))
    assert json.loads(CliRunner().invoke(cli.app, ["--data-dir", str(store.data_dir), "doctor", "--json"]).output)[
        "vertices_stored_twice"] is None
    assert tr(graph_duplicates.REPAIRED, "pl", count=2, project="p").startswith("Baza zapisała 2 wierzchołków grafu projektu p")
    assert tr(graph_duplicates.IN_STORE, "pl", count=3).startswith("W magazynie jest 3 wierzchołków")


def test_a_graph_without_a_duplicate_is_not_changed_by_the_check(tmp_path):
    store = Store(tmp_path / "memory")
    project, indexed = synthetic(store, tmp_path)
    ingest._build_graph(store, project, indexed)
    before = graph(store, project)
    assert graph_duplicates.repair(store, project) == 0 and graph(store, project) == before
