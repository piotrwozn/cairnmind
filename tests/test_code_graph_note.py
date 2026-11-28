"""After an import and before indexing, the readers of the code graph say it is not built yet (#286).

The state is the one the import and `memory status` report (code_graph.code_graph_pending).
Only `impact` reads the graph without indexing first: sk and the definition search run
behind /ask, which builds it.
"""
from __future__ import annotations

import subprocess
from dataclasses import replace

from fastapi.testclient import TestClient
from test_knowledge import FakeOllama
from test_snapshot_graph_rows import origin, target_of  # noqa: F401, F811  (origin is a fixture)

from cairnmind.config import Settings
from cairnmind.memory.impact import build_impact
from cairnmind.memory.memory import MemoryManager
from cairnmind.scip.code_graph import CODE_GRAPH_NOT_BUILT, code_graph_pending
from cairnmind.scip.ingest import index_project
from cairnmind.service import create_app
from cairnmind.terminal.terminal_i18n import tr
from cairnmind.terminal.terminal_impact import list_summary

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


def test_ask_builds_the_graph_before_it_reads_so_it_has_no_note_of_its_own(tmp_path, origin):  # noqa: F811
    """Every ask goes through the service's /ask, which indexes first; the definition search is reached only from
    there. So neither needs the note: the graph is built before they read it."""
    published = origin["manager"].snapshot(origin["pid"])
    store, root = target_of(tmp_path, origin)
    settings = replace(Settings.load(tmp_path / "target-database", create=True), knowledge_agent="disabled")
    settings.save()
    assert MemoryManager(store).import_snapshot(published["manifest_path"], root)["status"] == "imported"
    pid = store.resolve_project("big")["id"]
    assert code_graph_pending(store.resolve_project(pid))
    with TestClient(create_app(settings, start_worker=False, ollama=FakeOllama())) as client:
        reply = client.post("/ask", json={"project": "big", "question": QUESTION},
                            headers={"Authorization": f"Bearer {settings.token}"})
    assert reply.status_code == 200
    assert not code_graph_pending(store.resolve_project(pid))

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
