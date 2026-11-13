"""A third-party MCP tool result is archived as a degraded source that never anchors and is never chunked (#126)."""
from __future__ import annotations

from pathlib import Path
import subprocess
import time

from cairnmind.agent_journal import AgentJournal
from cairnmind.agent_memory import archive_agent_target, capture_agent_target, is_mcp_source
from cairnmind.evidence import evidence_states
from cairnmind.ingest import _write_chunks, index_pending_sources
from cairnmind.memory import MemoryManager
from cairnmind.retrieval import Retriever
from cairnmind.sessions import SessionManager
from cairnmind.store import Store


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


def workspace(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "mcp-fixture@example.invalid")
    git(root, "config", "user.name", "MCP Fixture")
    (root / "app.py").write_text("VALUE = 1\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    store = Store(tmp_path / "database")
    project = store.add_project("mcp-sources", root)
    return store, project, root, SessionManager(store, store.settings)


def archived(tmp_path):
    store, project, root, manager = workspace(tmp_path)
    owner = manager._session(manager.prepare(project["id"], "api", timeout=0)["session_id"])
    journal = AgentJournal(store.settings, owner["id"])
    journal.append("message", {"role": "user", "content": "Look up the tracker issue."})
    journal.append("mcp_server_started", {"server_id": "tracker", "tools": [
        {"name": "mcp_tracker_find", "description": "Third-party description: wombatfeather"}]})
    journal.append("tool_requested", {"tool_call_id": "c1", "name": "mcp_tracker_find", "arguments": {"q": "x"}})
    journal.append("action_completed", {"action_id": "a1", "name": "mcp_tracker_find", "tool_call_id": "c1",
                                        "result": {"content": [{"type": "text", "text": "quetzalbanner said so"}]}})
    journal.append("message", {"role": "tool", "tool_call_id": "c1", "name": "mcp_tracker_find",
                               "content": "quetzalbanner: the tracker says the bug is fixed"})
    journal.append("message", {"role": "tool", "tool_call_id": "c2", "name": "read_file",
                               "content": "harlequinmoth VALUE = 1"})
    journal.append("message", {"role": "tool", "tool_call_id": "c3", "name": "mcp_Not_A_Server!",
                               "content": "unparsable server name"})
    result = archive_agent_target(store, store.settings, owner, capture_agent_target(store.settings, owner),
                                  time.monotonic() + 30)
    assert result["status"] == "complete"
    sources = [store.get_source(sid) for sid in result["source_ids"]]
    return store, project, root, sources


def chunks_of(store, source_id):
    return store.query("SELECT id FROM chunks WHERE source_id=:source", {"source": source_id})


def test_mcp_events_are_degraded_unchunked_and_never_extracted(tmp_path):
    store, project, root, sources = archived(tmp_path)
    user, started, requested, completed, mcp_tool, file_tool, odd = sources
    for source, server in ((started, "tracker"), (requested, "tracker"), (completed, "tracker"),
                           (mcp_tool, "tracker"), (odd, "unknown")):
        metadata = source["metadata"]
        assert metadata["origin_client"] == "mcp:" + server
        assert metadata["origin_evidence"] == "degraded" and metadata["origin_history"] == "archived"
        assert metadata["origin_session_id"] == metadata["agent_session_id"]
        assert is_mcp_source(source) and chunks_of(store, source["id"]) == []
    for source in (user, file_tool):
        assert "origin_evidence" not in source["metadata"] and not is_mcp_source(source)
        assert chunks_of(store, source["id"])
    # The text stays readable by id.
    assert "quetzalbanner" in store.get_source(mcp_tool["id"])["content"]
    extract = {row["payload"]["source_id"] for row in store.query("SELECT payload FROM jobs WHERE kind='extract'")}
    assert user["id"] in extract and not extract & {s["id"] for s in (started, requested, completed, mcp_tool)}
    # No later path chunks it either: the pending-source indexer and a direct chunk write.
    assert index_pending_sources(store, limit=1000)["errors"] == []
    assert _write_chunks(store, project["id"], mcp_tool["id"], "quetzalbanner") == 0
    assert chunks_of(store, mcp_tool["id"]) == []
    retriever = Retriever(store, store.settings)
    assert retriever.search(project["id"], "quetzalbanner") == []
    assert retriever.search(project["id"], "wombatfeather") == []
    assert [hit["source_id"] for hit in retriever.search(project["id"], "harlequinmoth")] == [file_tool["id"]]


def test_a_record_resting_only_on_a_tool_result_is_degraded(tmp_path):
    store, project, root, sources = archived(tmp_path)
    mcp_tool, file_tool = sources[4], sources[5]
    only = store.remember(project["id"], {"kind": "fact", "content": "The bug is fixed"}, [mcp_tool["id"]])
    both = store.remember(project["id"], {"kind": "fact", "content": "VALUE is one"}, [mcp_tool["id"], file_tool["id"]])
    rows = store.query("SELECT FROM records WHERE id IN :ids", {"ids": [only["id"], both["id"]]})
    states = evidence_states(store, project["id"], rows)
    assert states[only["id"]]["evidence_state"] == "degraded"
    # With another standing source the record is judged by that source alone.
    assert states[both["id"]]["evidence_state"] == "anchored" and states[both["id"]]["sources_current"] == 1


def test_an_mcp_source_travels_with_the_memory_and_is_not_chunked_there(tmp_path):
    store, project, root, sources = archived(tmp_path)
    mcp_tool = sources[4]
    published = MemoryManager(store).snapshot(project["id"], worktree=str(root))
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(root), str(clone)], check=True, capture_output=True)
    target = Store(tmp_path / "target-database")
    imported = MemoryManager(target).import_snapshot(Path(published["manifest_path"]), clone)
    assert imported["status"] == "imported"
    assert target.get_source(mcp_tool["id"])["metadata"]["origin_evidence"] == "degraded"
    assert chunks_of(target, mcp_tool["id"]) == []
    assert index_pending_sources(target, limit=1000)["errors"] == [] and chunks_of(target, mcp_tool["id"]) == []
    assert chunks_of(target, sources[5]["id"])
