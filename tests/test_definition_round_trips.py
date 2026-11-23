"""The definitions of an answer and the files it checks (issue #408).

The definitions listed every current source of the project and sent the list with each read of the symbol nodes; a
file that both searches returned was checked on disk twice. Now the sources of the rows are read together, and the
first check of a file serves the later ones of the same answer.
"""
from __future__ import annotations

from test_definitions import CONFIG, _project
from test_knowledge import FakeOllama, workspace  # noqa: F401  (workspace is a fixture)
from test_lookup_indexes import as_0_12_0_left_it, indexed, marker
from test_round_trips import requests_made  # noqa: F401  (a fixture)
from test_store_writer import second_installation

from cairnmind.agent import KnowledgeAgent
from cairnmind.store import Store


def test_the_definitions_of_an_answer_take_a_few_requests_of_which_none_lists_the_sources(workspace, requests_made):  # noqa: F811
    store, project, settings, _root = _project(workspace)
    agent = KnowledgeAgent(store, settings, FakeOllama())
    question = "On which port does the cockpit listen by default?"
    hits = [{"source_id": "none", "text": 'getattr(settings, "cockpit_port", 1)'}]
    requests_made.clear()
    found = agent._definitions(project["id"], question, hits, {})
    assert found and any("5102" in item["text"] for item in found)
    node_reads = [sql for sql in requests_made if "FROM nodes" in sql]
    assert len(node_reads) <= 4 and not any("source_id IN" in sql for sql in node_reads), node_reads  # one per kind
    assert not any("FROM sources" in sql and "metadata.root AS root" in sql
                   for sql in requests_made), requests_made  # the list of sources
    print(f"definitions: {len(requests_made)} requests for {len(found)} definitions")
    # the scope, one read of the nodes, their sources, and about a dozen requests for each file checked on disk
    # (18 in all today, for this project)
    assert len(requests_made) <= 30, requests_made


def test_a_file_both_passes_return_is_checked_on_disk_once(workspace):  # noqa: F811
    store, project, settings, root = _project(workspace)
    agent = KnowledgeAgent(store, settings, FakeOllama())
    source = store.query("SELECT id FROM sources WHERE uri=:uri AND is_current=true",
                         {"uri": str(root / "pkg" / "config.py")})[0]["id"]
    hit = {"id": "chunk", "source_id": source, "text": CONFIG, "line_start": 1, "line_end": 20}
    checks, real = [], agent._source
    agent._source = lambda *args, **kwargs: checks.append(args[0]) or real(*args, **kwargs)
    checked, evidence = {}, {}
    first = agent._hits([hit], project["id"], evidence, None, "cockpit port", False, checked=checked)
    second = agent._hits([hit], project["id"], evidence, None, "cockpit port", False, checked=checked)
    agent._definitions(project["id"], "On which port does the cockpit listen by default?", second, evidence,
                       checked=checked)
    assert first == second and checks.count(source) == 1
    agent._hits([hit], project["id"], evidence, None, "cockpit port", False)  # without the memo: checked again
    assert checks.count(source) == 2


def test_a_file_that_failed_its_check_fails_the_same_way_the_second_time(workspace):  # noqa: F811
    store, project, settings, root = _project(workspace)
    agent = KnowledgeAgent(store, settings, FakeOllama())
    source = store.query("SELECT id FROM sources WHERE uri=:uri AND is_current=true",
                         {"uri": str(root / "pkg" / "config.py")})[0]["id"]
    (root / "pkg" / "config.py").unlink()
    hit = {"id": "chunk", "source_id": source, "text": CONFIG, "line_start": 1, "line_end": 20}
    checks, real = [], agent._source
    agent._source = lambda *args, **kwargs: checks.append(args[0]) or real(*args, **kwargs)
    checked = {}
    assert agent._hits([hit], project["id"], {}, None, "cockpit", False, checked=checked) == []
    assert agent._hits([hit], project["id"], {}, None, "cockpit", False, checked=checked) == []
    assert len(checks) == 1


def test_a_reader_before_the_writer_built_the_kind_index_gets_the_same_definitions(workspace, requests_made, tmp_path):  # noqa: F811
    store, project, settings, _root = _project(workspace)
    question = "On which port does the cockpit listen by default?"
    hits = [{"source_id": "none", "text": 'getattr(settings, "cockpit_port", 1)'}]

    def definitions(of):
        return [item["text"] for item in KnowledgeAgent(of, settings, FakeOllama())._definitions(
            project["id"], question, hits, {})]
    expected = definitions(store)
    assert expected
    as_0_12_0_left_it(store)
    reader = Store(second_installation(tmp_path, store))
    assert reader.read_only
    requests_made.clear()
    assert definitions(reader) == expected  # slower without the index, not different
    assert not [sql for sql in requests_made if sql.startswith(("CREATE INDEX", "CREATE PROPERTY", "DROP"))]
    assert ("nodes", ("kind",)) not in indexed(store) and marker(store) == [1]  # a reader builds nothing
    writer = Store(store.data_dir)  # the writer's next start builds it; the reader's next read uses it
    assert ("nodes", ("kind",)) in indexed(writer) and marker(writer) == [2]
    assert definitions(reader) == expected


def test_a_kind_with_more_rows_than_the_read_takes_gives_the_same_definitions(workspace, monkeypatch):  # noqa: F811
    store, project, settings, _root = _project(workspace)
    agent = KnowledgeAgent(store, settings, FakeOllama())
    question = "On which port does the cockpit listen by default?"
    hits = [{"source_id": "none", "text": 'getattr(settings, "cockpit_port", 1)'}]
    narrow = [item["text"] for item in agent._definitions(project["id"], question, hits, {})]
    monkeypatch.setattr("cairnmind.definitions.SCAN_ROWS", 1)  # every kind is read again, in the worktree's sources
    assert narrow and [item["text"] for item in agent._definitions(project["id"], question, hits, {})] == narrow