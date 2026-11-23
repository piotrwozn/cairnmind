"""The definitions of an answer and the files it checks (issue #408).

The definitions listed every current source of the project again and read the symbol nodes once per kind of match; a
file that both searches returned was checked on disk twice. Now one read serves every kind, and the first check of a
file serves the later ones of the same answer.
"""
from __future__ import annotations

from test_definitions import CONFIG, _project
from test_knowledge import FakeOllama, workspace  # noqa: F401  (workspace is a fixture)
from test_round_trips import requests_made  # noqa: F401  (a fixture)

from cairnmind.agent import KnowledgeAgent


def test_the_definitions_of_an_answer_take_a_few_requests_of_which_none_lists_the_sources(workspace, requests_made):  # noqa: F811
    store, project, settings, _root = _project(workspace)
    agent = KnowledgeAgent(store, settings, FakeOllama())
    question = "On which port does the cockpit listen by default?"
    hits = [{"source_id": "none", "text": 'getattr(settings, "cockpit_port", 1)'}]
    requests_made.clear()
    found = agent._definitions(project["id"], question, hits, {})
    assert found and any("5102" in item["text"] for item in found)
    node_reads = [sql for sql in requests_made if "FROM nodes" in sql]
    assert len(node_reads) == 1, node_reads  # one read for every kind of match
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
