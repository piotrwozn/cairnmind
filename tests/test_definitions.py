"""Definitions of the names a question and its hits use, added to the initial evidence (issue #104)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_knowledge import FakeOllama, document, final, workspace  # noqa: F401  (workspace is a fixture)

from cairnmind.agent import EXCERPT_CHARS, INITIAL_HITS, KnowledgeAgent
from cairnmind.definitions import (
    MAX_DEFINITION_ATTEMPTS, MAX_DEFINITIONS, MAX_ROWS, candidate_names, definition_lines, rank_definitions,
)
from cairnmind.ingest import index_project

CONFIG = '''"""Settings."""
from dataclasses import dataclass

TIMEOUT_SECONDS = 60


@dataclass
class Settings:
    data_dir: str = "data"
    host: str = "127.0.0.1"
    port: int = 8765
    cockpit_port: int = 8766
    workers: int = 4

    def url(self):
        return f"http://{self.host}:{self.port}"
'''
COCKPIT = '''import socket


def serve(settings, port=None):
    listener = socket.socket()
    # the cockpit listens where it is told, or on the configured cockpit port
    listener.bind(("127.0.0.1", port if port is not None else getattr(settings, "cockpit_port", 8766)))
    return listener
'''
TEST = '''TIMEOUT_SECONDS = 5
cockpit_port = 9999


def test_serve():
    assert serve(None, cockpit_port)
'''


def _project(workspace):  # noqa: F811
    store, project, settings = workspace
    root = Path(project["root"])
    (root / "pkg").mkdir()
    (root / "tests").mkdir()
    (root / "pkg" / "config.py").write_text(CONFIG)
    (root / "pkg" / "cockpit.py").write_text(COCKPIT)
    (root / "tests" / "test_cockpit.py").write_text(TEST)
    index_project(store, project["id"])
    return store, project, settings, root


def test_candidate_names_take_code_names_and_joined_question_terms():
    code, plain = candidate_names("What is the database schema version?",
                                  ['if version != SCHEMA_VERSION:\n    getattr(settings, "cockpit_port", 1)',
                                   "plain words only, self.retry_budget"])
    assert {"schema_version", "SCHEMA_VERSION", "database_schema", "version"} <= set(plain)
    assert {"cockpit_port", "retry_budget", "self.retry_budget"} <= set(code)
    assert not {"settings", "plain", "self", "SCHEMA_VERSION"} & set(code)


def test_ranking_drops_unrelated_names_and_tests_that_a_primary_file_defines():
    rows = [{"symbol": {"name": "cockpit_port", "kind": "constant"}, "path": "tests/test_cockpit.py"},
            {"symbol": {"name": "Settings.cockpit_port", "kind": "field", "owner": "Settings", "member": "cockpit_port"},
             "path": "pkg/config.py"},
            {"symbol": {"name": "Settings.port", "kind": "field", "owner": "Settings", "member": "port"},
             "path": "pkg/config.py"},
            {"symbol": {"name": "serve", "kind": "function"}, "path": "pkg/cockpit.py"},
            {"symbol": {"name": "TIMEOUT_SECONDS", "kind": "constant"}, "path": "tests/test_cockpit.py"}]
    ranked = rank_definitions("On which port does the cockpit listen by default?", rows)
    # cockpit and port: both words; port alone. serve shares only its file name and TIMEOUT nothing:
    # both are dropped, and so is the test's cockpit_port, which the settings file defines.
    assert [(row["symbol"]["name"], row["path"]) for row in ranked] == [
        ("Settings.cockpit_port", "pkg/config.py"), ("Settings.port", "pkg/config.py")]
    only_test = rank_definitions("What is the cockpit port?", rows[:1])
    assert [row["path"] for row in only_test] == ["tests/test_cockpit.py"]  # no primary file defines it


def test_a_field_is_shown_with_the_fields_around_it_and_its_class_header():
    lines = CONFIG.split("\n")
    first, last = definition_lines(lines, 12, 12, EXCERPT_CHARS)
    shown = lines[first - 1:last]
    assert shown[0] == "class Settings:" and "    cockpit_port: int = 8766" in shown
    assert "    port: int = 8765" in shown and "    workers: int = 4" in shown
    assert "TIMEOUT_SECONDS = 60" not in shown  # the header ends the scope upwards
    first, last = definition_lines(lines, 12, 12, 60)
    assert "    cockpit_port: int = 8766" in lines[first - 1:last] and len("\n".join(lines[first - 1:last])) <= 60
    # A definition longer than the excerpt is shown from its first line.
    assert definition_lines(lines, 8, 17, 40) == (8, 8)


async def test_the_definition_of_a_used_setting_is_shown_and_citable(workspace):  # noqa: F811
    store, project, settings, root = _project(workspace)
    # Notes that match the question better than the settings file, so that the file is not
    # among the hits that are always kept and its definition is shown on its own.
    for index in range(INITIAL_HITS - MAX_DEFINITIONS):
        document(store, project, f"Cockpit port note {index}: the cockpit listens on the default cockpit port.",
                 uri=f"notes://cockpit-{index}")

    def answer(messages, tools):
        shown = json.loads(messages[1]["content"])["initial_evidence"]
        definition = next(item for item in shown if item.get("kind") == "definition"
                          and item["definition"] == "Settings.cockpit_port")
        return final(definition["source_id"], "    cockpit_port: int = 8766", "8766.")

    model = FakeOllama([answer])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "On which port does the cockpit listen by default?")
    assert result["status"] == "complete" and result["answer"] == "8766."
    assert result["sources"][0]["uri"] == str(root / "pkg" / "config.py") and result["sources"][0]["line_start"] == 12
    shown = json.loads(model.calls[0]["messages"][1]["content"])["initial_evidence"]
    definitions = [item for item in shown if item.get("kind") == "definition"]
    assert 1 <= len(definitions) <= MAX_DEFINITIONS and len(shown) <= INITIAL_HITS
    entry = next(item for item in definitions if item["definition"] == "Settings.cockpit_port")
    lines = (root / "pkg" / "config.py").read_text().split("\n")
    assert entry["path"] == "pkg/config.py" and entry["definition_kind"] == "field"
    assert entry["text"] == "\n".join(lines[entry["line_start"] - 1:entry["line_end"]])
    # The test file assigns cockpit_port too, but a primary file defines it.
    assert all(not item["path"].startswith("tests/") for item in definitions)
    catalog = json.loads(model.calls[0]["messages"][1]["content"])["citation_catalog"]
    assert entry["source_id"] in {item["source_id"] for item in catalog}


async def test_a_question_whose_names_resolve_to_nothing_is_asked_as_before(workspace):  # noqa: F811
    store, project, settings, root = _project(workspace)
    (root / "notes.md").write_text("Lunch is at noon in the small kitchen.\n")
    index_project(store, project["id"])
    model = FakeOllama([{"content": json.dumps({"answer": "unknown", "citations": [], "unknowns": ["none"]})}])
    await KnowledgeAgent(store, settings, model).ask(project["id"], "Where is lunch?")
    shown = json.loads(model.calls[0]["messages"][1]["content"])["initial_evidence"]
    assert shown and all(item.get("kind") != "definition" for item in shown)


@pytest.mark.parametrize("question, first", [
    ("What is SCHEMA_VERSION?", "SCHEMA_VERSION"),
    ("What is the default ask_timeout?", "Settings.ask_timeout"),
    ("What is the default Settings.port?", "Settings.port"),
])
def test_a_definition_the_question_names_as_code_ranks_first(question, first):
    rows = [{"symbol": {"name": name, "kind": kind, **({"owner": "Settings", "member": name.split(".")[1]}
                                                       if "." in name else {})}, "path": path}
            for name, kind, path in [("VERSION", "constant", "pkg/memory.py"),
                                     ("SCHEMA_VERSION", "constant", "pkg/store.py"),
                                     ("Settings.timeout", "field", "pkg/config.py"),
                                     ("Settings.ask_timeout", "field", "pkg/config.py"),
                                     ("Settings.port", "field", "pkg/config.py"),
                                     ("default_port", "function", "pkg/net.py")]]
    ranked = rank_definitions(question, rows)
    assert ranked and ranked[0]["symbol"]["name"] == first


def test_definitions_come_from_the_asked_worktree_only_and_in_a_stable_order(workspace, tmp_path):  # noqa: F811
    store, project, settings, root = _project(workspace)
    # Another registered root that sorts first and defines the same name more often than the
    # bound on candidate rows: a bound applied before the worktree filter would leave only these.
    second = tmp_path / "a-second-worktree"
    (second / "pkg").mkdir(parents=True)
    (second / "pkg" / "config.py").write_text(CONFIG.replace("8766", "9766"))
    for index in range(MAX_ROWS + 10):
        (second / "pkg" / f"copy_{index:03d}.py").write_text(f"class Settings:\n    cockpit_port: int = {index}\n")
    store.add_project_root(project["id"], second)
    index_project(store, project["id"])
    agent = KnowledgeAgent(store, settings, FakeOllama())
    hits = [{"source_id": "none", "text": 'getattr(settings, "cockpit_port", 1)'}]
    question = "On which port does the cockpit listen by default?"
    first = agent._definitions(project["id"], question, hits, {}, worktree=str(second))
    again = agent._definitions(project["id"], question, hits, {}, worktree=str(second))
    assert first and [item["text"] for item in first] == [item["text"] for item in again]
    assert all(item["uri"].startswith(str(second)) for item in first)
    assert any("9766" in item["text"] for item in first)
    main = agent._definitions(project["id"], question, hits, {})
    assert main and all(item["uri"].startswith(str(root)) for item in main)
    assert any("8766" in item["text"] for item in main)


def test_files_changed_since_indexing_cost_a_bounded_number_of_checks(workspace):  # noqa: F811
    store, project, settings, root = _project(workspace)
    for index in range(MAX_DEFINITION_ATTEMPTS + 4):
        (root / "pkg" / f"limits_{index}.py").write_text(f"RETRY_LIMIT = {index}\n")
    index_project(store, project["id"])
    for index in range(MAX_DEFINITION_ATTEMPTS + 4):  # every definition is stale now
        (root / "pkg" / f"limits_{index}.py").write_text(f"# moved\nRETRY_LIMIT = {index}\n")
    agent = KnowledgeAgent(store, settings, FakeOllama())
    checks, real = [], agent._source
    agent._source = lambda *args, **kwargs: checks.append(args[0]) or real(*args, **kwargs)
    counted, real_query = [], store.query
    store.query = lambda *args, **kwargs: counted.append(args[0]) or real_query(*args, **kwargs)
    try:
        found = agent._definitions(project["id"], "What is the retry limit?", [], {})
    finally:
        store.query, agent._source = real_query, real
    assert found == [] and len(checks) == MAX_DEFINITION_ATTEMPTS
    print(f"stale: checks={len(checks)} queries={len(counted)}")


def test_a_hit_and_a_definition_of_one_file_both_reach_the_repair_catalog(workspace):  # noqa: F811
    store, project, settings, root = _project(workspace)
    agent = KnowledgeAgent(store, settings, FakeOllama())
    source = store.query("SELECT id FROM sources WHERE uri=:uri AND is_current=true",
                         {"uri": str(root / "pkg" / "config.py")})[0]["id"]
    lines = CONFIG.split("\n")
    evidence = {}
    hit = agent._hits([{"id": "chunk", "source_id": source, "text": "\n".join(lines[:4]), "line_start": 1,
                        "line_end": 4, "uri": str(root / "pkg" / "config.py")}], project["id"], evidence)
    definitions = agent._definitions(project["id"], "On which port does the cockpit listen by default?",
                                     [*hit, {"source_id": "none", "text": "cockpit_port"}], evidence)
    assert any(item["source_id"] == source for item in definitions)
    catalog = agent._citation_catalog(project["id"], evidence, "not JSON")
    passages = [item["quote_excerpt"] for item in catalog if item["source_id"] == source]
    assert hit[0]["text"] in passages
    assert any("cockpit_port: int = 8766" in passage for passage in passages) and len(passages) == 2


def test_resolving_definitions_costs_a_bounded_number_of_queries(workspace):  # noqa: F811
    store, project, settings, root = _project(workspace)
    agent = KnowledgeAgent(store, settings, FakeOllama())
    hits = [{"source_id": "none", "text": 'getattr(settings, "cockpit_port", 8766)\nTIMEOUT_SECONDS'}]
    counted, real_query, real_command = [], store.query, store.command
    store.query = lambda *args, **kwargs: counted.append(args[0]) or real_query(*args, **kwargs)
    store.command = lambda *args, **kwargs: counted.append(args[0]) or real_command(*args, **kwargs)
    try:
        found = agent._definitions(project["id"], "On which port does the cockpit listen by default?", hits, {})
    finally:
        store.query, store.command = real_query, real_command
    assert found and len(found) <= MAX_DEFINITIONS
    print(f"definitions={len(found)} queries={len(counted)}")
    # Project, roots, the worktree's sources (two), the nodes; then one freshness check per definition.
    assert len(counted) <= 6 + 12 * len(found)
