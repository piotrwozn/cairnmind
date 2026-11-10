"""Real stdio MCP -> HTTP -> ArcadeDB resume/ACK protocol, without model calls.

ACKs here are sent by the protocol test client. They do not demonstrate that a
production coding model read or understood a packet.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import socket
import subprocess
import sys
import threading
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import httpx
import pytest
import uvicorn

from cairnmind.config import Settings
from cairnmind.ingest import import_history
from cairnmind.integrations import hook
from cairnmind.plans import PlanManager
from cairnmind.service import create_app
from cairnmind.sessions import SessionManager
from cairnmind.store import Store


class NoGeneration:
    async def models(self):
        return {"models": []}

    async def embed(self, *args, **kwargs):
        raise AssertionError("The resume protocol must not invoke a language model")

    async def chat(self, *args, **kwargs):
        raise AssertionError("The resume protocol must not invoke a language model")


@contextmanager
def local_service(settings):
    """Keep the selected port reserved until uvicorn owns the same socket."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        settings.port = listener.getsockname()[1]
        settings.save()
        server = uvicorn.Server(uvicorn.Config(
            create_app(settings, start_worker=False, ollama=NoGeneration()),
            log_config=None, access_log=False, log_level="warning"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while thread.is_alive() and not server.started and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started, "The isolated local HTTP service did not start"
            yield
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive(), "The isolated local HTTP service did not stop"


def readback(packet):
    return {key: ({"id": packet[field]["id"], "version": packet[field]["version"]}
                  if packet.get(field) else None)
            for key, field in (("plan", "active_plan"), ("task", "active_task"))}


def ack_arguments(packet):
    return {"packet_id": packet["id"], "packet_hash": packet["hash"], "readback": readback(packet)}


def tool_result(result):
    assert not result.isError, result
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads("".join(item.text for item in result.content if item.type == "text"))


def rejected(result):
    assert result.isError, result
    assert "resume_required" in "".join(item.text for item in result.content if item.type == "text")


@pytest.mark.asyncio
async def test_actual_stdio_resume_refresh_keeps_horizon_and_requires_new_explicit_ack(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    code = root / "app.py"
    code.write_text("VALUE = 1\n")
    for args in (("init",), ("config", "user.email", "fixture@example.invalid"),
                 ("config", "user.name", "Fixture"), ("add", "."), ("commit", "-m", "fixture")):
        subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)
    settings = Settings.load(tmp_path / "memory", create=True)
    store = Store(settings.data_dir)
    project = store.add_project("mcp-resume", root)
    PlanManager(store).save(project["id"], {"title": "Protocol fixture", "goal": "Verify resume"},
                            [{"key": "verify", "content": "Verify the exact packet protocol"}],
                            "initial-plan", worktree=str(root), activate=True)
    previous = tmp_path / "previous.jsonl"
    previous.write_text(json.dumps({"type": "user", "cwd": str(root), "sessionId": "previous-native",
                                   "message": {"role": "user", "content": "Preserve the current plan"}}) + "\n")
    import_history(store, previous, project["id"], "claude")
    manager = SessionManager(store, settings)
    prepared = manager.prepare(project["id"], "codex", timeout=0)
    assert prepared["status"] == "ready", prepared
    session_id, initial = prepared["session_id"], prepared["packet"]
    assert initial["active_plan"] and initial["active_task"]
    assert len(initial["archive_watermarks"]) == 1
    assert initial["archive_watermarks"][0]["path"] == str(previous)

    for key, value in {"DEVBRAIN_SESSION_ID": session_id, "DEVBRAIN_PACKET_ID": initial["id"],
                       "DEVBRAIN_PROJECT": project["id"], "DEVBRAIN_CLIENT": "codex"}.items():
        monkeypatch.setenv(key, value)
    with local_service(settings):
        # The native rollout is created after the launcher captured its horizon.
        # The scanner is stopped deliberately, so this live file has no cursor.
        current = tmp_path / "current.jsonl"
        current.write_text(json.dumps({"type": "event_msg", "payload": {
            "type": "user_message", "message": "Continue", "cwd": str(root)}}) + "\n")
        # This checks packet delivery, not the advisory hook's production 0.7s
        # budget. A shared real database can exceed that budget under test load.
        real_client = httpx.Client

        def protocol_client(**kwargs):
            if "base_url" not in kwargs:
                kwargs["timeout"] = 10
            return real_client(**kwargs)

        with monkeypatch.context() as startup_patch:
            startup_patch.setattr("cairnmind.integrations.httpx.Client", protocol_client)
            startup = hook(settings, {"hook_event_name": "SessionStart", "session_id": "current-native",
                                     "cwd": str(root), "transcript_path": str(current)}, "codex")
        context = startup["hookSpecificOutput"]["additionalContext"]
        assert context.startswith("{"), context
        delivered = json.loads(context)
        assert delivered == initial
        assert not store.query("SELECT FROM resume_acks")
        assert manager.status(session_id)["epoch"] == prepared["epoch"]

        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "cairnmind", "--data-dir", str(settings.data_dir), "mcp"],
            env=dict(os.environ))
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                initialized = await client.initialize()
                assert initialized.serverInfo.name == "devbrain"
                context = tool_result(await client.call_tool("resume_context", {}))
                assert context["packet"] == initial
                assert not store.query("SELECT FROM resume_acks")
                rejected(await client.call_tool("checkpoint", {
                    "project": project["id"], "state": {"goal": "Before ACK"}}))
                wrong = ack_arguments(initial)
                wrong["readback"]["task"]["version"] += 1
                rejected(await client.call_tool("ack_resume", wrong))
                assert not store.query("SELECT FROM resume_acks")

                acknowledgment = tool_result(await client.call_tool("ack_resume", ack_arguments(initial)))
                assert acknowledgment["packet_id"] == initial["id"]
                manager.require_ack(session_id, initial["id"])
                assert len(store.query("SELECT FROM resume_acks")) == 1

                with current.open("a") as handle:
                    handle.write(json.dumps({"type": "event_msg", "payload": {
                        "type": "agent_message", "message": "The current turn is still growing"}}) + "\n")
                code.write_text("VALUE = 2\n")
                rejected(await client.call_tool("checkpoint", {
                    "project": project["id"], "state": {"goal": "Old ACK after code change"}}))
                rejected(await client.call_tool("ack_resume", ack_arguments(initial)))
                with pytest.raises(ValueError, match="resume_required"):
                    manager.require_ack(session_id, initial["id"])

                refreshed = tool_result(await client.call_tool("resume_context", {}))
                assert refreshed["status"] == "ready", refreshed
                packet = refreshed["packet"]
                assert refreshed["session_id"] == session_id
                assert refreshed["epoch"] > prepared["epoch"]
                assert packet["id"] != initial["id"]
                # A refresh can retain an additional durable proof, while the
                # captured range, generation and actual archive evidence stay fixed.
                assert [{key: value for key, value in row.items() if key != "proof"}
                        for row in packet["archive_watermarks"]] == initial["archive_watermarks"]
                assert packet["archive"]["status"] == "complete"
                assert not store.query("SELECT FROM import_cursors WHERE path=:path", {"path": str(current)})
                session = store.query("SELECT FROM sessions WHERE id=:id", {"id": session_id})[0]
                assert session["native_session_id"] == "current-native"
                assert any(target["path"] == str(current) for target in session["transcript_targets"])
                assert [target["path"] for target in session["resume_targets"]] == [str(previous)]
                assert session.get("ack_id") is None
                assert len(store.query("SELECT FROM resume_acks")) == 1
                rejected(await client.call_tool("checkpoint", {
                    "project": project["id"], "state": {"goal": "Refreshed but not acknowledged"}}))
                # The live bridge must adopt the newly returned packet identity,
                # rather than remain pinned to its startup environment variable.
                rejected(await client.call_tool("ack_resume", ack_arguments(initial)))
                acknowledged = tool_result(await client.call_tool("ack_resume", ack_arguments(packet)))
                assert acknowledged["packet_id"] == packet["id"]
                manager.require_ack(session_id, packet["id"])
                assert len(store.query("SELECT FROM resume_acks")) == 2
                saved = tool_result(await client.call_tool("checkpoint", {
                    "project": project["id"], "state": {"goal": "Protocol test client acknowledged"}}))
                assert saved["source_ids"]
