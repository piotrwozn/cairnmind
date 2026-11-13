"""An external MCP client (Cursor, Cline, Zed, ...) gets a degraded session, never a verified one.

Real ArcadeDB and Git. Its own conversation is unarchived, while the transcripts of native clients
in the same worktree still block a ready packet exactly as they block a native one.
"""
import json
import subprocess

import pytest

from cairnmind.ingest import import_history
from cairnmind.sessions import EXTERNAL_WARNING, SessionManager, format_packet
from cairnmind.store import Store


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def sessions(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    for args in (("init", "-b", "main"), ("config", "user.email", "fixture@example.invalid"),
                 ("config", "user.name", "Fixture")):
        git(root, *args)
    (root / "app.py").write_text("VALUE = 1\n")
    git(root, "add", "app.py")
    git(root, "commit", "-m", "initial")
    store = Store(tmp_path / "memory")
    project = store.add_project("external-fixture", root)
    return store, project, root, SessionManager(store, store.settings)


def readback(packet):
    return {key: ({"id": packet[field]["id"], "version": packet[field]["version"]} if packet.get(field) else None)
            for key, field in (("plan", "active_plan"), ("task", "active_task"))}


def acknowledge(manager, result):
    packet = result["packet"]
    return manager.ack(result["session_id"], packet["id"], packet["hash"], readback(packet))


def history(path, root, text="A native instruction"):
    with path.open("a") as handle:
        handle.write(json.dumps({"type": "user", "cwd": str(root), "sessionId": "native",
                                 "message": {"role": "user", "content": text}}) + "\n")


def test_external_packet_says_unarchived_and_degraded_and_still_needs_an_ack(sessions):
    store, project, _, manager = sessions
    result = manager.prepare(project["id"], "external", timeout=0)
    packet = result["packet"]
    assert result["status"] == "ready", result
    assert packet["client"] == "external"
    assert (packet["history"], packet["evidence"]) == ("unarchived", "degraded")
    # The history exists but cannot be read: it is not "no prior history".
    assert packet["no_history"] is False
    assert packet["warnings"][0] == EXTERNAL_WARNING
    assert "not archived" in packet["archive"]["scope"]
    text = format_packet(packet)
    assert "history unarchived, evidence degraded" in text
    assert "verified resume" not in text and "No prior conversation history" not in text
    # The same gate as a native client: delivery is not an ACK, and an ACK alone is not verification.
    with pytest.raises(ValueError, match="resume_required"):
        manager.require_ack(result["session_id"])
    acknowledge(manager, result)
    manager.require_ack(result["session_id"], packet["id"])
    status = manager.status(result["session_id"])
    assert status["acknowledged"] is True and status["client"] == "external"
    assert (status["history"], status["evidence"]) == ("unarchived", "degraded")
    session = store.query("SELECT FROM sessions WHERE id=:id", {"id": result["session_id"]})[0]
    assert session["native_session_id"] is None and session["transcript_targets"] == []


def test_native_packets_are_unchanged(sessions):
    _, project, _, manager = sessions
    packet = manager.prepare(project["id"], "codex", timeout=0)["packet"]
    assert "history" not in packet and "evidence" not in packet and "warnings" not in packet
    assert packet["no_history"] is True and packet["archive"]["scope"] == "captured EOFs only"
    assert format_packet(packet).startswith("Cairnmind verified resume bootstrap")
    assert "status" in manager.status(packet["session_id"]) and "evidence" not in manager.status(packet["session_id"])


@pytest.mark.parametrize("client", ["cursor", "zed", "cline", "", "External"])
def test_no_product_name_or_guess_is_a_session_client(sessions, client):
    _, project, _, manager = sessions
    with pytest.raises(ValueError, match="Session client must be"):
        manager.prepare(project["id"], client, timeout=0)


def test_an_external_session_cannot_become_a_native_one(sessions):
    _, project, _, manager = sessions
    result = manager.prepare(project["id"], "external", timeout=0)
    for client in ("codex", "claude"):
        with pytest.raises(ValueError, match="cannot change project, client or worktree"):
            manager.prepare(project["id"], client, session_id=result["session_id"], timeout=0)


def test_an_unknown_native_transcript_record_blocks_the_external_session(sessions, tmp_path):
    store, project, root, manager = sessions
    transcript = tmp_path / "claude.jsonl"
    history(transcript, root)
    with transcript.open("a") as handle:
        handle.write('{"new_schema":true}\n')
    import_history(store, transcript, project["id"], "claude")
    result = manager.prepare(project["id"], "external", timeout=0)
    assert result["status"] == "partial"
    assert any("Unparsed or conflicting" in text for text in result["unknowns"])
    with pytest.raises(ValueError, match="resume_required"):
        acknowledge(manager, result)
    with pytest.raises(ValueError, match="resume_required"):
        manager.require_ack(result["session_id"])


def test_a_pending_native_hook_horizon_blocks_the_external_session(sessions, tmp_path):
    store, project, root, manager = sessions
    transcript = tmp_path / "claude-live.jsonl"
    history(transcript, root)
    spool = store.data_dir / "hook-events"
    spool.mkdir()
    (spool / "pending.json").write_text(json.dumps({
        "hook_event_name": "Stop", "session_id": "native", "cwd": str(root), "client": "claude",
        "transcript_path": str(transcript)}))
    result = manager.prepare(project["id"], "external", timeout=0)
    assert result["status"] == "partial"
    with pytest.raises(ValueError, match="resume_required"):
        acknowledge(manager, result)
    # Once the native transcript is archived, the same external session can be resumed.
    (spool / "pending.json").unlink()
    import_history(store, transcript, project["id"], "claude")
    retried = manager.resume_context(result["session_id"])
    assert retried["session_id"] == result["session_id"]
    assert retried["status"] == "ready", retried
    assert retried["packet"]["archive"]["status"] == "complete"
    assert retried["packet"]["history"] == "unarchived"
