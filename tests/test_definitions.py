"""Definitions of the names a question and its hits use, added to the initial evidence (issue #104)."""
from __future__ import annotations

import json
from pathlib import Path

from test_knowledge import FakeOllama, document, final, workspace  # noqa: F401  (workspace is a fixture)

from cairnmind.agent import EXCERPT_CHARS, INITIAL_HITS, KnowledgeAgent
from cairnmind.definitions import MAX_DEFINITIONS, candidate_names, definition_lines, rank_definitions
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
    assert len(counted) <= 4 + 12 * len(found)
