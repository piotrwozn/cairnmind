"""After an import and before indexing, the readers of the code graph say it is not built yet (#286).

The state is the one the import and `memory status` report (code_graph.code_graph_pending). The three readers are
`ask` (with models, and without them), `impact`, and the definition search that `ask` feeds the model with.
"""
from __future__ import annotations

import subprocess

from test_knowledge import FakeOllama
from test_snapshot_graph_rows import origin, target_of  # noqa: F401, F811  (origin is a fixture)

from cairnmind import knowledge_mode
from cairnmind.agent import KnowledgeAgent
from cairnmind.code_graph import CODE_GRAPH_NOT_BUILT
from cairnmind.config import Settings
from cairnmind.impact import build_impact
from cairnmind.ingest import index_project
from cairnmind.memory import MemoryManager
from cairnmind.terminal_i18n import tr
from cairnmind.terminal_impact import list_summary

QUESTION = "Where is f1 defined?"


def imported(tmp_path, origin):  # noqa: F811
    """The target machine right after an import: memory is there, the code graph is not."""
    published = origin["manager"].snapshot(origin["pid"])
    store, root = target_of(tmp_path, origin)
    assert MemoryManager(store).import_snapshot(published["manifest_path"], root)["status"] == "imported"
    return store, root


def commit(root):
    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@example.invalid"), ("config", "user.name", "T"),
                 ("add", "."), ("commit", "-qm", "initial")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (root / "mod1.py").write_text("def f1():\n    return 2\n")


async def test_ask_with_models_says_the_graph_is_not_built_until_indexing_has_built_it(tmp_path, origin):  # noqa: F811
    store, root = imported(tmp_path, origin)
    agent = KnowledgeAgent(store, Settings(data_dir=tmp_path / "data"), FakeOllama())
    before = await agent.ask(origin["pid"], QUESTION)
    assert CODE_GRAPH_NOT_BUILT in before["freshness"]["warnings"]
    assert before["status"] != "complete" and before["unknowns"][0] == CODE_GRAPH_NOT_BUILT
    index_project(store, origin["pid"])
    after = await agent.ask(origin["pid"], QUESTION)
    assert CODE_GRAPH_NOT_BUILT not in after["freshness"]["warnings"] + after["unknowns"]


async def test_ask_without_models_lists_it_among_its_unknowns(tmp_path, origin):  # noqa: F811
    store, root = imported(tmp_path, origin)
    agent = KnowledgeAgent(store, Settings(data_dir=tmp_path / "data"), FakeOllama())
    found = await knowledge_mode.text_matches(agent, origin["pid"], QUESTION)
    # The service passes the agent's notes as it does here.
    before = knowledge_mode.unavailable_answer(found, (), agent.code_graph_notes(origin["pid"]))
    assert CODE_GRAPH_NOT_BUILT in before["unknowns"]
    index_project(store, origin["pid"])
    after = knowledge_mode.unavailable_answer(found, (), agent.code_graph_notes(origin["pid"]))
    assert CODE_GRAPH_NOT_BUILT not in after["unknowns"]


def test_impact_says_it_in_its_text_its_json_and_the_terminal_summary_in_both_languages(tmp_path, origin):  # noqa: F811
    store, root = imported(tmp_path, origin)
    commit(root)
    before = build_impact(store, origin["pid"], "HEAD")
    assert before["warnings"] == [CODE_GRAPH_NOT_BUILT] and CODE_GRAPH_NOT_BUILT in before["text"].splitlines()
    english = list_summary(before, "HEAD", False, lambda text, **values: tr(text, "en", **values))
    polish = list_summary(before, "HEAD", False, lambda text, **values: tr(text, "pl", **values))
    assert CODE_GRAPH_NOT_BUILT in english and "nie jest jeszcze zbudowany" in polish
    index_project(store, origin["pid"])
    after = build_impact(store, origin["pid"], "HEAD")
    assert "warnings" not in after and CODE_GRAPH_NOT_BUILT not in after["text"]
    assert CODE_GRAPH_NOT_BUILT not in list_summary(after, "HEAD", False, lambda text, **values: text)


def test_the_definition_search_reads_nothing_before_indexing_and_the_note_covers_it(tmp_path, origin):  # noqa: F811
    store, root = imported(tmp_path, origin)
    agent = KnowledgeAgent(store, Settings(data_dir=tmp_path / "data"), FakeOllama())
    question = "What does `f1` return?"
    assert agent._definitions(origin["pid"], question, [], {}) == []
    assert agent.code_graph_notes(origin["pid"]) == [CODE_GRAPH_NOT_BUILT]
    index_project(store, origin["pid"])
    assert agent.code_graph_notes(origin["pid"]) == []
