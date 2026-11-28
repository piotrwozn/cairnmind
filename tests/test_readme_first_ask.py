"""What the first `ask` after the README's quick start shows (#370): the saved note, and the passages of the project's
files whose words match the question's. No model is asked in the first test; the second reads the evidence the model
is handed (a fake model), before any vector exists."""
from __future__ import annotations

import json

import pytest
from test_knowledge import FakeOllama, final

from cairnmind.agents.agent import KnowledgeAgent
from cairnmind.agents.knowledge_mode import text_matches
from cairnmind.agents.retrieval import VECTORS_BUILDING
from cairnmind.scip.ingest import index_project_if_changed

NOTE = "Accounts lock after five failed logins."
QUESTION = "When do accounts lock?"


@pytest.fixture
def my_app(tmp_path):
    from cairnmind.store import Store
    root = tmp_path / "my-app"
    (root / "src").mkdir(parents=True)
    (root / "README.md").write_text("# my-app\n\nA small web service.\n")
    (root / "src" / "same_words.py").write_text("# Accounts lock after five failed logins.\nLIMIT = 5\n")
    (root / "src" / "other_words.py").write_text("# The user is locked out on the fifth wrong password.\nLIMIT = 5\n")
    store = Store(tmp_path / "data")
    project = store.add_project("my-app", root)
    index_project_if_changed(store, project["id"])
    source = store.snapshot(project["id"], "manual://one", NOTE, "manual", {"role": "user", "origin": "cli"})
    store.remember(project["id"], {"kind": "fact", "content": NOTE, "status": "proposed"}, [source["id"]])
    from cairnmind.config import Settings
    return store, project, Settings(data_dir=tmp_path / "data"), source


async def test_first_ask_brings_passages_whose_words_match_and_not_the_others(my_app):
    store, project, settings, _ = my_app
    agent = KnowledgeAgent(store, settings, FakeOllama())
    found = await text_matches(agent, project["id"], QUESTION)
    uris = [match["uri"] for match in found["matches"]]
    assert any(uri.endswith("same_words.py") for uri in uris), uris
    assert any(uri.startswith("manual://") for uri in uris), uris
    assert not any(uri.endswith("other_words.py") for uri in uris), uris  # no common word: "lock" is not "locked"


async def test_first_ask_with_models_hands_the_matching_passage_to_the_model_and_warns_of_the_vectors(my_app):
    store, project, settings, note = my_app
    seen = {}

    def answer(messages, tools):
        seen["evidence"] = json.loads(messages[-1]["content"])["initial_evidence"]
        return final(note["id"], NOTE, "After five failed logins.")

    model = FakeOllama([answer])
    agent = KnowledgeAgent(store, settings, model)
    chunk = store.query("SELECT id FROM chunks WHERE project_id=:id", {"id": project["id"]})[0]["id"]
    # One chunk embedded, the other jobs still pending: right after `project add`.
    await agent.retriever.embed_pending({"project_id": project["id"], "payload": {"chunk_id": chunk}})
    result = await agent.ask(project["id"], QUESTION)
    uris = [item.get("uri", "") for item in seen["evidence"]]
    assert any(uri.endswith("same_words.py") for uri in uris), uris
    assert any(warning.startswith(VECTORS_BUILDING)
               for warning in result["freshness"]["warnings"]), result["freshness"]
    assert [source["id"] for source in result["sources"]] == [note["id"]], result["sources"]
