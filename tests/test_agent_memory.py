"""Own API/Copilot memory protocol on the real ArcadeDB engine."""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from uuid import uuid4

import httpx
import pytest
from jsonschema import Draft202012Validator

from devbrain.agent_journal import AgentJournal
from devbrain.agent_memory import AgentMemoryClient, MEMORY_TOOLS, archive_agent_target, capture_agent_target
from devbrain.memory import MemoryManager, inspect_snapshot
from devbrain.service import create_app
from devbrain.sessions import SessionManager
from devbrain.store import Store


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "api-fixture@example.invalid")
    git(root, "config", "user.name", "API Fixture")
    (root / "app.py").write_text("VALUE = 1\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    store = Store(tmp_path / "database")
    project = store.add_project("api-memory", root)
    return store, project, root, SessionManager(store, store.settings)


def readback(packet):
    return {key: ({"id": packet[field]["id"], "version": packet[field]["version"]}
                  if packet.get(field) else None)
            for key, field in (("plan", "active_plan"), ("task", "active_task"))}


def ack(manager, result):
    packet = result["packet"]
    return manager.ack(result["session_id"], packet["id"], packet["hash"], readback(packet))


def ack_validator():
    return Draft202012Validator(next(tool["parameters"] for tool in MEMORY_TOOLS if tool["name"] == "ack_resume"))


def test_ack_tool_exposes_exact_readback_for_absent_and_present_plan_and_task():
    validator = ack_validator()
    base = {"packet_id": "packet-id", "packet_hash": "a" * 64}
    for packet in ({"active_plan": None, "active_task": None},
                   {"active_plan": {"id": "plan-id", "version": 2}, "active_task": None},
                   {"active_plan": {"id": "plan-id", "version": 3},
                    "active_task": {"id": "task-id", "version": 4}}):
        validator.validate({**base, "readback": readback(packet)})
    for invalid in ({}, {"plan": None}, {"active_plan": None, "active_task": None},
                    {"plan": None, "task": None, "extra": None},
                    {"plan": {"id": "plan-id"}, "task": None},
                    {"plan": {"id": "", "version": 1}, "task": None},
                    {"plan": {"id": "plan-id", "version": 0}, "task": None},
                    {"plan": None, "task": {"id": "task-id", "version": True}},
                    {"plan": None, "task": {"id": "task-id", "version": "1"}}):
        assert not validator.is_valid({**base, "readback": invalid}), invalid


@pytest.mark.parametrize("client", ["api", "copilot"])
@pytest.mark.asyncio
async def test_actual_model_read_and_exact_ack_are_required_then_stale_on_state_change(workspace, client):
    store, project, root, manager = workspace
    app = create_app(store.settings, start_worker=False)
    sid = uuid4().hex
    memory = AgentMemoryClient(store.settings, project["id"], root, sid, client=client,
                               transport=httpx.ASGITransport(app=app))
    prepared = await memory.prepare()
    assert prepared["status"] == "ready"
    assert not store.query("SELECT FROM resume_acks")
    packet = prepared["packet"]
    with pytest.raises(RuntimeError, match="read resume_context"):
        await memory.execute("ack_resume", {"packet_id": packet["id"], "packet_hash": packet["hash"],
                                             "readback": readback(packet)})
    current = await memory.execute("resume_context", {})
    packet = current["packet"]
    with pytest.raises(RuntimeError, match="409"):
        await memory.execute("ack_resume", {"packet_id": packet["id"], "packet_hash": "0" * 64,
                                             "readback": readback(packet)})
    with pytest.raises(RuntimeError, match="409"):
        await memory.execute("checkpoint", {"state": {"goal": "Must not be written"}})
    saved = await memory.execute("ack_resume", {"packet_id": packet["id"], "packet_hash": packet["hash"],
                                                "readback": readback(packet)})
    assert saved["session_id"] == sid
    assert (await memory.status())["acknowledged"] is True
    assert manager._session(sid)["native_session_id"] is None
    await memory.execute("checkpoint", {"state": {"goal": "Implement API memory", "next_step": "Review"}})
    assert (await memory.execute("session_status", {}))["stale"] is True
    with pytest.raises(RuntimeError, match="409"):
        await memory.execute("checkpoint", {"state": {"goal": "Old ACK cannot authorize this"}})
    native = manager.prepare(project["id"], "codex", timeout=0)
    assert native["status"] == "ready"
    assert any(row["kind"] == "checkpoint" for row in native["packet"]["records"])
    assert not manager.status(native["session_id"])["acknowledged"]


def test_finite_archive_preserves_public_events_and_never_exports_provider_continuations(workspace):
    store, project, _, manager = workspace
    prepared = manager.prepare(project["id"], "api", timeout=0)
    owner = manager._session(prepared["session_id"])
    journal = AgentJournal(store.settings, owner["id"])
    journal.append("message", {"role": "user", "content": "Remember the deployment decision."})
    journal.append("message", {"role": "assistant", "content": "Use a staged rollout.",
                               "continuation": {"encrypted_content": "PRIVATE_PROVIDER_BLOB"},
                               "thinking": "PRIVATE_THINKING", "tool_calls": [
                                   {"id": "one", "name": "remember", "arguments": {
                                       "record": {"signature": "LEGITIMATE_TOOL_FIELD"}}}]})
    target = capture_agent_target(store.settings, owner)
    journal.append("message", {"role": "user", "content": "A later event must not move the captured horizon."})
    result = archive_agent_target(store, store.settings, owner, target, time.monotonic() + 30)
    assert result["status"] == "complete" and result["sequence"] == 2
    sources = [store.get_source(sid) for sid in result["source_ids"]]
    assert len(sources) == 2
    rendered = json.dumps(sources)
    assert "Use a staged rollout." in rendered
    assert "LEGITIMATE_TOOL_FIELD" in rendered
    assert "PRIVATE_PROVIDER_BLOB" not in rendered and "PRIVATE_THINKING" not in rendered
    assert not any("later event" in source["content"] for source in sources)
    assert all(source["kind"] == "agent_transcript" for source in sources)
    assert all(hashlib.sha256(source["content"].encode()).hexdigest() == source["sha256"] for source in sources)
    assert store.query("SELECT FROM chunks WHERE project_id=:project", {"project": project["id"]})
    assert not store.query("SELECT FROM import_cursors")
    assert not store.query("SELECT FROM resume_acks")


@pytest.mark.parametrize("client", ["api", "copilot"])
def test_archive_replay_is_idempotent_and_native_resume_verifies_largest_captured_horizon(workspace, client):
    store, project, _, manager = workspace
    start = manager.prepare(project["id"], client, timeout=0)
    journal = AgentJournal(store.settings, start["session_id"])
    journal.append("message", {"role": "user", "content": "One important instruction."})
    first = manager.archive_agent_journal(start["session_id"])
    second = manager.archive_agent_journal(start["session_id"])
    assert first["status"] == second["status"] == "complete"
    assert first["source_ids"] == second["source_ids"]
    journal.append("action_started", {"action_id": "one", "command": "pytest", "cwd": "."})
    journal.append("action_completed", {"action_id": "one", "returncode": 0, "stdout": "13 tests passed"})
    native = manager.prepare(project["id"], "claude", timeout=0)
    assert native["status"] == "ready"
    assert native["packet"]["no_history"] is False
    watermark = native["packet"]["archive_watermarks"][0]
    assert watermark["kind"] == "agent_journal" and watermark["sequence"] == 3
    assert watermark["source_count"] == 3 and watermark["status"] == "complete"
    assert not manager.status(native["session_id"])["acknowledged"]
    ack(manager, native)
    manager.require_ack(native["session_id"])


def test_proved_public_archive_survives_local_log_loss_but_not_source_loss(workspace):
    store, project, _, manager = workspace
    start = manager.prepare(project["id"], "api", timeout=0)
    journal = AgentJournal(store.settings, start["session_id"])
    journal.append("message", {"role": "user", "content": "A portable fact with its source."})
    archive = manager.archive_agent_journal(start["session_id"])
    journal.path.unlink()
    native = manager.prepare(project["id"], "codex", timeout=0)
    assert native["status"] == "ready"
    store.command("DELETE FROM sources WHERE id=:id", {"id": archive["source_ids"][0]})
    damaged = manager.prepare(project["id"], "claude", timeout=0)
    assert damaged["status"] == "partial"
    assert damaged["unknowns"]


def test_corrupt_or_incomplete_live_journal_cannot_disappear_behind_old_proof(workspace):
    store, project, _, manager = workspace
    start = manager.prepare(project["id"], "api", timeout=0)
    journal = AgentJournal(store.settings, start["session_id"])
    journal.append("message", {"role": "user", "content": "Already proved."})
    manager.archive_agent_journal(start["session_id"])
    with journal.path.open("ab") as handle:
        handle.write(b'{"incomplete":')
    native = manager.prepare(project["id"], "claude", timeout=0)
    assert native["status"] == "partial"
    assert any("journal cannot be captured" in error for error in native["unknowns"])
    with pytest.raises(ValueError, match="ready"):
        ack(manager, native)


def test_source_replacement_cannot_reuse_an_earlier_archive_proof(workspace):
    store, project, _, manager = workspace
    start = manager.prepare(project["id"], "api", timeout=0)
    journal = AgentJournal(store.settings, start["session_id"])
    journal.append("message", {"role": "user", "content": "The real instruction."})
    archived = manager.archive_agent_journal(start["session_id"])
    source = store.get_source(archived["source_ids"][0])
    replacement = source["content"].replace("The real instruction.", "A forged instruction.")
    store.command("UPDATE sources SET content=:text,sha256=:sha WHERE id=:id", {
        "id": source["id"], "text": replacement, "sha": hashlib.sha256(replacement.encode()).hexdigest()})
    journal.path.unlink()
    resumed = manager.prepare(project["id"], "claude", timeout=0)
    assert resumed["status"] == "partial"
    assert resumed["unknowns"]


def test_rewritten_journal_cannot_merge_away_previously_archived_history(workspace):
    store, project, _, manager = workspace
    start = manager.prepare(project["id"], "api", timeout=0)
    journal = AgentJournal(store.settings, start["session_id"])
    journal.append("message", {"role": "user", "content": "The original conversation."})
    first = manager.archive_agent_journal(start["session_id"])
    journal.path.unlink()
    journal.append("message", {"role": "user", "content": "A replacement conversation."})
    journal.append("message", {"role": "assistant", "content": "Longer does not prove continuation."})
    resumed = manager.prepare(project["id"], "codex", timeout=0)
    assert resumed["status"] == "partial"
    assert len(resumed["packet"]["archive_watermarks"]) == 2
    assert store.get_source(first["source_ids"][0])["content"].find("The original conversation.") >= 0


@pytest.mark.parametrize("damage", ["missing", "empty", "rewritten"])
def test_same_own_session_cannot_continue_lost_sequence_but_new_session_can_use_history(workspace, damage):
    store, project, _, manager = workspace
    start = manager.prepare(project["id"], "api", timeout=0)
    ack(manager, start)
    journal = AgentJournal(store.settings, start["session_id"])
    journal.append("message", {"role": "user", "content": "Previously archived project knowledge."})
    manager.archive_agent_journal(start["session_id"])
    journal.path.unlink()
    if damage == "empty":
        journal.path.write_bytes(b"")
        journal.path.chmod(0o600)
    elif damage == "rewritten":
        journal.append("message", {"role": "user", "content": "New sequence one is a different conversation."})
    assert manager.status(start["session_id"])["acknowledged"] is False
    with pytest.raises(ValueError, match="private journal"):
        manager.require_ack(start["session_id"])
    with pytest.raises(ValueError, match="private journal"):
        ack(manager, start)
    continued = manager.resume_context(start["session_id"])
    assert continued["status"] == "partial"
    assert any("start a new agent session" in reason for reason in continued["unknowns"])
    if damage == "rewritten":
        assert manager.archive_agent_journal(start["session_id"])["status"] == "partial"
    else:
        fresh = manager.prepare(project["id"], "api", timeout=0)
        assert fresh["status"] == "ready"
        assert fresh["packet"]["no_history"] is False
        own = AgentJournal(store.settings, fresh["session_id"])
        own.append("message", {"role": "user", "content": "Continue from retained project memory."})
        assert manager.archive_agent_journal(fresh["session_id"])["status"] == "complete"


@pytest.mark.parametrize("append_after_loss", [False, True])
def test_missing_previously_proved_source_is_reported_without_recreating_its_identity(workspace, append_after_loss):
    store, project, _, manager = workspace
    start = manager.prepare(project["id"], "api", timeout=0)
    journal = AgentJournal(store.settings, start["session_id"])
    journal.append("message", {"role": "user", "content": "Original evidence one."})
    journal.append("message", {"role": "assistant", "content": "Original evidence two."})
    archived = manager.archive_agent_journal(start["session_id"])
    store.command("DELETE FROM sources WHERE id=:id", {"id": archived["source_ids"][0]})
    if append_after_loss:
        journal.append("message", {"role": "user", "content": "A later append must not hide evidence loss."})
    replayed = manager.archive_agent_journal(start["session_id"])
    assert replayed["status"] == "partial"
    assert any("Previously proved" in reason for reason in replayed["unknowns"])
    remaining = store.query("SELECT id FROM sources WHERE project_id=:project AND kind='agent_transcript'",
                            {"project": project["id"]})
    assert [row["id"] for row in remaining] == [archived["source_ids"][1]]
    native = manager.prepare(project["id"], "claude", timeout=0)
    assert native["status"] == "partial"


@pytest.mark.asyncio
async def test_scope_override_and_foreign_sources_are_rejected(workspace, tmp_path):
    store, project, root, manager = workspace
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = store.add_project("other", other_root)
    source = store.snapshot(other["id"], "manual://foreign", "Not visible in this project", "manual")
    sid = uuid4().hex
    app = create_app(store.settings, start_worker=False)
    memory = AgentMemoryClient(store.settings, project["id"], root, sid,
                               transport=httpx.ASGITransport(app=app))
    await memory.prepare()
    for args in ({"project": other["id"]}, {"session_id": "foreign"}, {"worktree": str(other_root)}):
        with pytest.raises(ValueError, match="cannot override"):
            await memory.execute("checkpoint", args)
    with pytest.raises(RuntimeError, match="rejected"):
        await memory.execute("read_source", {"source_id": source["id"]})
    for unsafe in ("../inbox", "..", "%2e%2e%2finbox", "foreign/source", "x?project=other"):
        with pytest.raises(ValueError, match="valid source ID"):
            await memory.execute("read_source", {"source_id": unsafe})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=store.settings.url) as client:
        assert (await client.post("/sessions/agent/archive", json={"session_id": sid})).status_code == 401
        assert (await client.post("/sessions/agent/archive", json={"session_id": sid, "path": "/etc/passwd"},
            headers={"Authorization": f"Bearer {store.settings.token}"})).status_code == 422
    target = {"kind": "agent_journal", "path": "/etc/passwd", "agent_session_id": sid, "client": "api"}
    assert archive_agent_target(store, store.settings, manager._session(sid), target,
                                time.monotonic() + 30)["status"] == "partial"


@pytest.mark.asyncio
async def test_native_plan_api_task_progress_and_copilot_handoff_keep_exact_versions(workspace):
    store, project, root, manager = workspace
    native = manager.prepare(project["id"], "codex", timeout=0)
    ack(manager, native)
    app = create_app(store.settings, start_worker=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=store.settings.url,
            headers={"Authorization": f"Bearer {store.settings.token}"}) as client:
        response = await client.post("/plans/save", json={
            "project": project["id"], "session_id": native["session_id"],
            "packet_id": native["packet"]["id"], "worktree": str(root), "activate": True,
            "plan": {"title": "Cross-client feature", "goal": "Implement then review"},
            "steps": [{"key": "implement", "content": "Implement parser"}],
            "idempotency_key": "native-plan"})
        assert response.status_code == 200, response.text
        saved = response.json()
    api = AgentMemoryClient(store.settings, project["id"], root, uuid4().hex, transport=transport)
    await api.prepare()
    current = await api.execute("resume_context", {})
    packet = current["packet"]
    assert packet["active_plan"]["id"] == saved["plan"]["id"]
    assert packet["active_task"]["id"] == saved["tasks"][0]["id"]
    ack_validator().validate({"packet_id": packet["id"], "packet_hash": packet["hash"],
                              "readback": readback(packet)})
    await api.execute("ack_resume", {"packet_id": packet["id"], "packet_hash": packet["hash"],
                                      "readback": readback(packet)})
    checkpoint = await api.execute("checkpoint", {"state": {
        "goal": "Implementation complete", "next_step": "A separate client must review"}})
    packet = (await api.execute("resume_context", {}))["packet"]
    await api.execute("ack_resume", {"packet_id": packet["id"], "packet_hash": packet["hash"],
                                      "readback": readback(packet)})
    updated = await api.execute("remember", {"record": {
        "id": packet["active_task"]["id"], "expected_version": packet["active_task"]["version"],
        "expected_plan_version": packet["active_plan"]["version"],
        "metadata": {"state": "needs_review"}}, "sources": checkpoint["source_ids"]})
    copilot = manager.prepare(project["id"], "copilot", timeout=0)
    assert copilot["packet"]["active_task"]["state"] == "needs_review"
    assert copilot["packet"]["active_task"]["version"] == updated["version"]
    assert copilot["packet"]["active_plan"]["version"] == saved["plan"]["version"] + 1
    assert not manager.status(copilot["session_id"])["acknowledged"]
    packet = copilot["packet"]
    arguments = {"packet_id": packet["id"], "packet_hash": packet["hash"], "readback": readback(packet)}
    ack_validator().validate(arguments)
    wrong = readback(packet)
    wrong["task"]["version"] -= 1
    with pytest.raises(ValueError, match="readback must match"):
        manager.ack(copilot["session_id"], packet["id"], packet["hash"], wrong)
    assert manager.ack(copilot["session_id"], **arguments)["status"] == "acknowledged"


def test_portable_snapshot_keeps_api_sources_done_and_no_session_authority(workspace, tmp_path):
    store, project, root, manager = workspace
    start = manager.prepare(project["id"], "copilot", timeout=0)
    journal = AgentJournal(store.settings, start["session_id"])
    journal.append("message", {"role": "user", "content": "The parser feature is finished and reviewed."})
    archived = manager.archive_agent_journal(start["session_id"])
    task = store.remember(project["id"], {"kind": "task", "key": "parser", "content": "Implement parser",
        "metadata": {"state": "done"}}, archived["source_ids"])
    snapshot = MemoryManager(store).snapshot(project["id"], worktree=str(root))
    assert inspect_snapshot(snapshot["manifest_path"])["format_version"] == 1
    destination = tmp_path / "new-repo"
    subprocess.run(["git", "clone", "-q", str(root), str(destination)], check=True, capture_output=True)
    new = Store(tmp_path / "new-database")
    MemoryManager(new).import_snapshot(snapshot["manifest_path"], destination)
    transferred = new.get_source(archived["source_ids"][0])
    assert transferred["content"] == store.get_source(archived["source_ids"][0])["content"]
    assert new.list_records(project["id"])[0]["id"] == task["id"]
    assert new.list_records(project["id"])[0]["metadata"]["state"] == "done"
    assert not new.query("SELECT FROM sessions") and not new.query("SELECT FROM resume_acks")
    resumed = SessionManager(new, new.settings).prepare(project["id"], "api", timeout=0)
    assert resumed["status"] == "ready"
    assert resumed["packet"]["no_history"] is False
    assert not SessionManager(new, new.settings).status(resumed["session_id"])["acknowledged"]
