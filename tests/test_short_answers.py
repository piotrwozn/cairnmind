"""The MCP tools answer with what changed, not with what was sent or read before (#325).

No engine, no sockets, no processes: a fake bridge and httpx.MockTransport, so it runs the same on macOS.
"""
import json

import httpx
import pytest

from cairnmind import mcp_server
from cairnmind.config import Settings
from cairnmind.mcp_server import ServiceClient, _next_task_view, _saved_plan_view, _saved_record_view, create_server

from conftest import plain_words


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for name in ("CAIRNMIND_SESSION_ID", "CAIRNMIND_PACKET_ID", "DEVBRAIN_SESSION_ID", "DEVBRAIN_PACKET_ID"):
        monkeypatch.delenv(name, raising=False)
    directory = tmp_path / "memory"
    directory.mkdir()
    (directory / "config.json").write_text(json.dumps({"port": 1, "arcade_url": "http://127.0.0.1:1"}))
    return Settings.load(directory, create=True)


class FakeService:
    def __init__(self, answers=None, external=None):
        self.calls, self.answers, self.external = [], answers or {}, external

    async def post(self, endpoint, payload):
        self.calls.append((endpoint, payload))
        return self.answers.get(endpoint, {"ok": True})

    async def get(self, endpoint, params):
        return {"checks": []}


def body(result):
    """The JSON a tool call answered with, whatever shape this version of the SDK wraps it in."""
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


STORED = {"id": "rec-1", "project_id": "p", "kind": "decision", "key": "storage", "content": "SECRET-CONTENT",
          "status": "proposed", "source_ids": ["src-1", "src-2"], "version": 3, "supersedes": "rec-0",
          "metadata": {"origin_client": "external", "origin_session_id": "s1", "origin_history": "unarchived",
                       "origin_evidence": "degraded", "topic": "storage", "possible_conflicts": ["rec-9"],
                       "supersede_warning": "rec-0 was replaced", "evidence_basis": "SECRET-METADATA"},
          "created_at": "t", "updated_at": "t", "active_key": "k"}


@pytest.mark.asyncio
async def test_resume_context_asks_short_unless_the_whole_packet_is_requested(settings):
    bridge = FakeService()
    server = create_server(settings, bridge)
    await server.call_tool("resume_context", {})
    await server.call_tool("resume_context", {"full_packet": True})
    await server.call_tool("resume_context", {"full_packet": False})
    assert bridge.calls == [("/sessions/context", {"short": True}), ("/sessions/context", {"short": False}),
                            ("/sessions/context", {"short": True})]


REAL_CLIENT = httpx.AsyncClient


def client_over(monkeypatch, settings, answer):
    real = REAL_CLIENT
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=answer if request.url.path == "/sessions/context" else {"ok": True})

    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(mcp_server, "other_version", lambda settings: None)  # its own health probe is a real socket
    return ServiceClient(settings, session_id="s", packet_id="packet-one"), seen


SHORT = {"session_id": "s", "epoch": 2, "status": "ready", "unchanged": True, "acknowledged": True,
         "packet_id": "packet-two", "packet_hash": "h2", "newer_automatic_proposals": 0, "note": "n"}


@pytest.mark.asyncio
async def test_the_bridge_keeps_the_acknowledged_packet_on_a_short_answer(settings, monkeypatch):
    client, seen = client_over(monkeypatch, settings, SHORT)
    await client.post("/sessions/context", {"short": True})
    assert client.packet_id == "packet-two"
    with pytest.raises(RuntimeError, match="exact packet"):
        await client.post("/sessions/ack", {"packet_id": "packet-one", "packet_hash": "h"})
    await client.post("/sessions/ack", {"packet_id": "packet-two", "packet_hash": "h2", "readback": {}})
    assert [path for path, _ in seen] == ["/sessions/context", "/sessions/ack"]
    for broken in ({**SHORT, "packet_id": 7}, {**SHORT, "packet_hash": None}):
        client, _ = client_over(monkeypatch, settings, broken)
        with pytest.raises(RuntimeError, match=r"unavailable \(ValueError\)"):
            await client.post("/sessions/context", {"short": True})
        assert client.packet_id == "packet-one"
    client, _ = client_over(monkeypatch, settings, {**SHORT, "session_id": "other"})
    with pytest.raises(RuntimeError, match="Session mismatch"):
        await client.post("/sessions/context", {"short": True})


def test_write_answers_name_what_the_service_decided_not_what_was_sent():
    sent = {"origin_client": "codex", "topic": "storage", "conflicts_resolved": ["x"]}
    view = _saved_record_view(STORED, sent)
    assert {"id": "rec-1", "version": 3, "status": "proposed", "kind": "decision", "key": "storage",
            "source_ids": ["src-1", "src-2"], "supersedes": "rec-0", "possible_conflicts": ["rec-9"],
            "supersede_warning": "rec-0 was replaced"}.items() <= view.items()
    assert {key: view[key] for key in mcp_server.ORIGIN_KEYS} == {
        key: STORED["metadata"][key] for key in mcp_server.ORIGIN_KEYS}
    assert view["metadata_not_stored_as_sent"] == ["conflicts_resolved", "origin_client"]
    assert "SECRET" not in json.dumps(view) and "content" not in view and "metadata" not in view
    assert "metadata_not_stored_as_sent" not in _saved_record_view(STORED, {"topic": "storage"})
    assert _saved_record_view({"ok": True}, sent) == {"ok": True}
    many = _saved_record_view({**STORED, "source_ids": [str(n) for n in range(20)]}, None)
    assert len(many["source_ids"]) == 8 and many["omitted_source_ids"] == 12
    assert "omitted_source_ids" not in view and "metadata_dropped" not in view
    # The service's answer to this bridge (name_dropped) names keys the version before held; the store never does.
    dropped = _saved_record_view({**STORED, "metadata_dropped": ["acceptance_passed", "test_passed"]}, None)
    assert dropped["metadata_dropped"] == ["acceptance_passed", "test_passed"]


@pytest.mark.asyncio
async def test_remember_and_checkpoint_tools_answer_short(settings):
    stored_state = {**STORED, "kind": "checkpoint", "metadata": {"summary": "SENT-STATE", "origin_client": "external"}}
    bridge = FakeService({"/remember": STORED, "/checkpoint": stored_state})
    server = create_server(settings, bridge)
    remembered = body(await server.call_tool("remember", {
        "project": "p", "record": {"kind": "decision", "content": "SENT-TEXT", "metadata": {"topic": "storage"}},
        "sources": ["src-1"]}))
    checkpointed = body(await server.call_tool("checkpoint", {
        "project": "p", "state": {"summary": "SENT-STATE", "evidence_basis": "mine"}}))
    for answer in (remembered, checkpointed):
        assert not {"content", "metadata", "active_key"} & set(answer)
        assert answer["source_ids"] == ["src-1", "src-2"] and answer["id"] == "rec-1"
        assert "SECRET" not in json.dumps(answer) and "SENT" not in json.dumps(answer)
    assert "metadata_not_stored_as_sent" not in remembered
    assert checkpointed["metadata_not_stored_as_sent"] == ["evidence_basis"]  # derived by the store, never as sent
    # Only this bridge asks the service to name removed keys; the command line and the built-in agent do not.
    assert [payload.get("name_dropped") for _, payload in bridge.calls] == [True, True]


def test_the_saved_plan_answer_has_no_title_and_names_each_task_status():
    tasks = [{"id": "t1", "version": 1, "status": "proposed", "content": "SECRET-STEP",
              "metadata": {"step_key": "build", "state": "todo"}}]
    plan = {"id": "plan-a", "version": 1, "title": "SECRET-TITLE", "step_ids": ["t1"], "source_ids": ["s"]}
    view = _saved_plan_view({"plan": plan, "tasks": tasks, "replayed": False})
    assert "title" not in view["plan"] and view["plan"]["id"] == "plan-a" and view["plan"]["step_count"] == 1
    assert view["tasks"][0]["status"] == "proposed" and view["tasks"][0]["state"] == "todo"
    assert "SECRET" not in json.dumps(view)
    assert _next_task_view({"plan": plan, "task": None, "reason": "none", "blocked": []})["plan"]["title"] == \
        "SECRET-TITLE"


@pytest.mark.asyncio
@pytest.mark.parametrize("external", [None, {"project": "p", "worktree": None}])
async def test_tool_texts_describe_the_short_answers(settings, external):
    server = create_server(settings, FakeService(external=external))
    tools = {tool.name: plain_words(tool.description or "") for tool in await server.list_tools()}
    assert "unchanged: true" in tools["resume_context"] and "full_packet: true" in tools["resume_context"]
    assert "not an acknowledgement" in tools["resume_context"]
    assert "not repeated" in tools["remember"]
    assert "not the state you sent" in tools["checkpoint"]
    assert "not the plan's text" in tools["save_plan"]
    instructions = plain_words(server.instructions)
    assert "full_packet: true" in instructions and "not the content you sent" in instructions
    assert ("history: unarchived" in instructions) is (external is not None)
