"""Edge-case tests for agent_memory.py (archiving validation, continuity, AgentMemoryClient)."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from cairnmind import agent_memory as am
from cairnmind.config import Settings


class _FakeStore:
    def __init__(self, rows=None, session=None):
        self.rows = rows or []
        self.session = session or {}

    def query(self, sql, params=None):
        if "FROM sessions" in sql:
            return [self.session] if self.session else []
        return list(self.rows)


def test_agent_memory_source_checks_continuity_and_archive_validation(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data")

    # is_mcp_source with JSON string (valid + invalid) and dict
    assert am.is_mcp_source({
        "kind": "agent_transcript",
        "metadata": json.dumps({"origin_client": "mcp:demo", "origin_evidence": "degraded"}),
    }) is True
    assert am.is_mcp_source({"kind": "agent_transcript", "metadata": "{bad-json"}) is False

    # _journal_events rejects non-list
    monkeypatch.setattr("cairnmind.agent_journal.AgentJournal.events", lambda self: "not-a-list")
    with pytest.raises(ValueError, match="Invalid own-agent journal"):
        am._journal_events(settings, "sess-1")

    # agent_continuity_unknowns: non-agent client -> []; corrupt journal -> [reason]
    assert am.agent_continuity_unknowns(_FakeStore(), settings, {"client": "codex"}) == []
    jpath = am._journal_path(settings, "sess-1")
    jpath.parent.mkdir(parents=True, exist_ok=True)
    jpath.write_text("corrupt", encoding="utf-8")
    store_with_row = _FakeStore(rows=[{"sequence": 1, "event_hash": "abc"}])
    assert am.agent_continuity_unknowns(store_with_row, settings, {"id": "sess-1", "project_id": "p1", "client": "api"})

    # _verify_archived: deadline exceeded, bad sha256, invalid JSON, mismatched event seq
    ev_text = json.dumps({"seq": 1, "session_id": "sess-1", "hash": "h1"})
    ev_sha = hashlib.sha256(ev_text.encode()).hexdigest()
    good_row = {"id": "s1", "kind": "agent_transcript", "sha256": ev_sha, "content": ev_text,
                "metadata": {"agent_sequence": 1, "agent_event_hash": "h1"}}
    proof = {
        "status": "complete", "sequence": 1, "size": 10, "sha256": "rawsha",
        "source_count": 1, "source_ids_hash": am._hash(["s1"]),
        "public_sources_hash": am._source_proof([good_row]), "chain_hash": "h1",
    }
    target = {"agent_session_id": "sess-1", "sequence": 1, "size": 10, "sha256": "rawsha"}
    with pytest.raises(TimeoutError):
        am._verify_archived([good_row], target, proof, deadline=-1.0)

    bad_json_row = {**good_row, "content": "{not-json", "sha256": hashlib.sha256(b"{not-json").hexdigest()}
    bad_proof = {**proof, "public_sources_hash": am._source_proof([bad_json_row])}
    assert am._verify_archived([bad_json_row], target, bad_proof, deadline=1e18) is False

    wrong_seq_text = json.dumps({"seq": 9, "session_id": "sess-1", "hash": "h1"})
    wrong_seq_row = {**good_row, "content": wrong_seq_text, "sha256": hashlib.sha256(wrong_seq_text.encode()).hexdigest()}
    wrong_proof = {**proof, "public_sources_hash": am._source_proof([wrong_seq_row])}
    assert am._verify_archived([wrong_seq_row], target, wrong_proof, deadline=1e18) is False

    # archive_agent_target validation error paths
    session = {"id": "sess-1", "project_id": "p1", "worktree": "/w", "client": "api"}
    base_target = {
        "agent_session_id": "sess-1", "path": str(jpath), "client": "api",
        "sequence": 1, "size": 10, "sha256": "x", "chain_hash": "h1",
    }
    # Wrong project/worktree on owner
    res = am.archive_agent_target(_FakeStore(session={**session, "project_id": "other"}), settings, session, base_target, 1e18)
    assert res["status"] == "partial" and "another project" in res["unknowns"][0]
    # Invalid horizon
    res = am.archive_agent_target(_FakeStore(session=session), settings, session, {**base_target, "sequence": 0}, 1e18)
    assert "Invalid own-agent archive horizon" in res["unknowns"][0]
    # Duplicate sequence in durable sources
    dup_rows = [{"id": "a", "metadata": {"agent_sequence": 1}}, {"id": "b", "metadata": {"agent_sequence": 1}}]
    res = am.archive_agent_target(_FakeStore(rows=dup_rows, session=session), settings, session, base_target, 1e18)
    assert "Conflicting durable" in res["unknowns"][0]
    # Truncated journal
    monkeypatch.setattr(am, "_journal_events", lambda s, sid: [])
    res = am.archive_agent_target(_FakeStore(session=session), settings, session, base_target, 1e18)
    assert "truncated" in res["unknowns"][0]
    # Captured prefix changed
    monkeypatch.setattr(am, "_journal_events", lambda s, sid: [{"seq": 1, "hash": "other", "type": "message", "data": {}}])
    res = am.archive_agent_target(_FakeStore(session=session), settings, session, base_target, 1e18)
    assert "prefix changed" in res["unknowns"][0]


@pytest.mark.asyncio
async def test_agent_memory_client_errors_and_all_tools(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    with pytest.raises(ValueError, match="api or copilot"):
        am.AgentMemoryClient(settings, "p1", tmp_path, "sess-1", client="codex")

    responses = []

    def handler(request: httpx.Request) -> httpx.Response:
        status, body = responses.pop(0)
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    client = am.AgentMemoryClient(settings, "p1", tmp_path, "sess-1", transport=httpx.MockTransport(handler))

    # Non-JSON error body -> RuntimeError
    responses.append((500, "plain text failure"))
    with pytest.raises(RuntimeError, match="500"):
        await client._request("GET", "/x")

    # 400 confirmation refusal & record refusal (without "Nothing was saved") & 409 & 404 & 400 unknown_task
    responses.append((400, {"refusal": "confirmation", "detail": "needs human"}))
    with pytest.raises(am.MemoryRefusal) as exc:
        await client._request("POST", "/remember")
    assert exc.value.code == "confirmation_refused"

    responses.append((400, {"refusal": "record", "detail": "bad field"}))
    with pytest.raises(am.MemoryRefusal) as exc:
        await client._request("POST", "/remember")
    assert exc.value.code == "invalid_record" and am.RECORD_SHAPE_HINT in exc.value.message

    responses.append((409, {"detail": am._KEY_EXISTS + " dup"}))
    with pytest.raises(am.MemoryRefusal) as exc:
        await client._request("POST", "/remember")
    assert exc.value.code == "key_exists"

    responses.append((404, {"detail": "not found"}))
    with pytest.raises(am.MemoryRefusal) as exc:
        await client._request("GET", "/inspector/sources/abc")
    assert exc.value.code == "unknown_source"

    responses.append((400, {"detail": "Task does not exist in this project"}))
    with pytest.raises(am.MemoryRefusal) as exc:
        await client._request("GET", "/checks")
    assert exc.value.code == "unknown_task"

    # Non-dict JSON response -> RuntimeError("unavailable")
    responses.append((200, [1, 2, 3]))
    with pytest.raises(RuntimeError, match="unavailable"):
        await client._request("GET", "/x")

    # prepare / status / archive mismatches
    responses.append((200, {"session_id": "wrong"}))
    with pytest.raises(RuntimeError, match="Session mismatch"):
        await client.prepare()

    responses.append((200, {"session_id": "sess-1", "project_id": "wrong", "client": "api", "worktree": client.worktree}))
    with pytest.raises(RuntimeError, match="scope changed"):
        await client.status()

    with pytest.raises(RuntimeError, match="different agent session"):
        await client.archive(SimpleNamespace(session_id="other"))

    # execute validation and tool routes
    with pytest.raises(ValueError, match="object"):
        await client.execute("ask", "not-a-dict")
    with pytest.raises(ValueError, match="override"):
        await client.execute("ask", {"project": "other"})
    with pytest.raises(ValueError, match="Unknown"):
        await client.execute("nonexistent_tool", {})
    with pytest.raises(RuntimeError, match="resume_required"):
        await client.execute("remember", {"content": "hi"})
    with pytest.raises(am.MemoryRefusal):
        await client.execute("read_source", {"source_id": "../bad"})

    # resume_context session mismatch & scope mismatch & valid read -> save_plan / next_task / memory_records
    responses.append((200, {"session_id": "other"}))
    with pytest.raises(RuntimeError, match="Session mismatch"):
        await client.execute("resume_context", {})
    responses.append((200, {"session_id": "sess-1", "packet": {"id": "pkt-1", "project_id": "other", "worktree": client.worktree}}))
    with pytest.raises(RuntimeError, match="scope changed"):
        await client.execute("resume_context", {})

    responses.append((200, {"session_id": "sess-1", "packet": {"id": "pkt-1", "project_id": "p1", "worktree": client.worktree}}))
    await client.execute("resume_context", {})
    responses.extend([(200, {"ok": True}), (200, {"ok": True}), (200, {"ok": True}), (200, {"ok": True})])
    assert await client.execute("next_task", {}) == {"ok": True}
    assert await client.execute("check_status", {}) == {"ok": True}
    assert await client.execute("memory_records", {}) == {"ok": True}
    assert await client.execute("save_plan", {"title": "P"}) == {"ok": True}
