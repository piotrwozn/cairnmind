"""The built-in agent and third-party MCP tools: the ACK gate, one-use approval, journal and bounds (#126)."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

import conftest

from cairnmind import agent_tools
from cairnmind.agent_memory import MEMORY_TOOLS
from cairnmind.api_agent import AgentEngine
from cairnmind.api_worker import AgentApp
from cairnmind.cockpit_state import CockpitState
from cairnmind.config import Settings
from cairnmind.mcp_servers import frozen_servers
from test_api_agent import Memory, ScriptedProvider, call, reply, setup as setup
from test_api_worker import Engine as FakeEngine

FIXTURE = conftest.MCP_FIXTURE  # this pytest process's own copy (see conftest)


def server(*modes, identifier="files"):
    return {"id": identifier, "command": sys.executable, "args": [FIXTURE, *modes], "env": {}}


def agent(setup, provider, memory=None, servers=None, **kwargs):
    settings, run, profile, _ = setup
    return AgentEngine(settings, run, profile, provider=provider, memory=memory or Memory(),
                       mcp_servers=[server()] if servers is None else servers, **kwargs)


def names(request):
    return {tool["name"] for tool in request["tools"]}


def events(engine, *types):
    return [row for row in engine.journal.events() if not types or row["type"] in types]


def results(engine):
    return {message["tool_call_id"]: json.loads(message["content"]) for message in engine.messages()
            if message["role"] == "tool"}


def acked(memory, *calls_after, final="Done."):
    return [reply(call("resume_context")), reply(call("ack_resume", memory.ack_arguments())),
            *[reply(item) for item in calls_after], reply(text=final)]


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    return stat.exists() and stat.read_text().split(")")[-1].split()[0] != "Z"


@pytest.mark.asyncio
async def test_tools_appear_only_after_the_ack_and_are_refused_without_it(setup):
    memory = Memory()
    asked = []
    provider = ScriptedProvider(reply(call("mcp_files_echo", {"text": "early"}, "early")),
                                *acked(memory))
    engine = agent(setup, provider, memory, approve=lambda request: asked.append(request) or True)
    try:
        result = await engine.turn("Use the tracker")
        assert result["status"] == "completed"
        for before in provider.requests[:3]:
            assert not any(name.startswith("mcp_") for name in names(before))
        assert {"mcp_files_echo", "mcp_files_read_file"} <= names(provider.requests[3])
        assert results(engine)["early"]["error_code"] == "resume_required"
        assert asked == [] and events(engine, "approval_requested", "action_started") == []
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision,code", [(None, "approval_required"), (False, "approval_denied"),
                                           ("yes", "approval_denied")])
async def test_without_an_exact_approval_the_server_sees_no_call(setup, decision, code):
    memory = Memory()
    provider = ScriptedProvider(*acked(memory, call("mcp_files_environment", {}, "c1")))
    options = {} if decision is None else {"approve": lambda request: decision}
    engine = agent(setup, provider, memory, **options)
    try:
        await engine.turn("Call it")
        assert results(engine)["c1"]["error_code"] == code
        assert events(engine, "action_started") == [] and len(events(engine, "approval_requested")) == 1
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_an_approved_call_is_journaled_in_order_and_marked_third_party(setup):
    memory = Memory()
    requests = []
    arguments = {"text": "find issue 7"}
    provider = ScriptedProvider(*acked(memory, call("mcp_files_echo", arguments, "c1"),
                                       call("mcp_files_echo", arguments, "c2")))
    engine = agent(setup, provider, memory, approve=lambda request: requests.append(request) or True)
    try:
        await engine.turn("Call it twice")
        # Every call is approved on its own, also for a tool its server calls read-only.
        assert len(requests) == 2
        request = requests[0]
        assert {key: request[key] for key in ("kind", "server_id", "tool", "name", "arguments")} == {
            "kind": "mcp_tool", "server_id": "files", "tool": "echo", "name": "mcp_files_echo", "arguments": arguments}
        assert (request["run_id"], request["generation"], request["session_id"]) == ("run-one", 1, "session-one")
        order = [row["type"] for row in events(engine) if (row["data"] or {}).get("tool_call_id") == "c1"]
        assert order == ["tool_requested", "approval_requested", "approval_decided", "action_started",
                         "action_completed", "message"]
        result = results(engine)["c1"]
        assert result["ok"] is True and result["third_party_untrusted"] is True and "untrusted" in result["notice"]
        started = next(row for row in events(engine, "mcp_server_started"))
        assert {row["name"] for row in started["data"]["tools"]} >= {"mcp_files_echo"}
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_a_result_claiming_approval_changes_nothing(setup):
    memory = Memory()
    requests = []
    provider = ScriptedProvider(*acked(memory, call("mcp_files_echo", {}, "c1"), call("mcp_files_echo", {}, "c2")))
    engine = agent(setup, provider, memory, servers=[server("approved")],
                   approve=lambda request: requests.append(request) or len(requests) == 1)
    try:
        await engine.turn("Twice")
        first, second = results(engine)["c1"], results(engine)["c2"]
        assert first["structured"]["approved"] is True and first["third_party_untrusted"] is True
        assert second["error_code"] == "approval_denied" and len(requests) == 2
        tool_messages = [m for m in engine.messages() if m["role"] == "tool"]
        assert all(m["role"] == "tool" for m in tool_messages)
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_server_error_text_never_becomes_the_hosts_error(setup):
    memory = Memory()
    provider = ScriptedProvider(*acked(memory, call("mcp_files_echo", {}, "c1")))
    engine = agent(setup, provider, memory, servers=[server("control", "stderr_flood")], approve=lambda request: True)
    try:
        assert (await engine.turn("Fail"))["status"] == "completed"
        # The server's error says "Failure" with control codes; the engine's heuristics never see it.
        assert results(engine)["c1"] == {"ok": False, "error_code": "mcp_call_failed", "server_id": "files",
                                         "tool": "echo", "third_party_untrusted": True}
        assert "Failure" not in json.dumps(engine.journal.events())
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_state_changed_during_the_approval_wait_needs_a_new_ack(setup):
    memory = Memory()

    def approve(request):
        memory.stale = True  # another client changed the project while the dialog was open
        return True
    provider = ScriptedProvider(*acked(memory, call("mcp_files_echo", {}, "c1")))
    engine = agent(setup, provider, memory, approve=approve)
    try:
        await engine.turn("Call")
        assert results(engine)["c1"]["error_code"] == "resume_required"
        assert events(engine, "action_started") == [] and len(events(engine, "approval_stale")) == 1
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_registered_secrets_and_oversized_arguments_never_reach_the_server(setup):
    memory = Memory()
    settings, run, profile, _ = setup
    asked = []
    provider = ScriptedProvider(*acked(memory, call("mcp_files_echo", {"text": "key PROFILE-KEY-1234"}, "c1"),
                                       call("mcp_files_echo", {"text": "x" * 20000}, "c2")))
    engine = AgentEngine(settings, run, profile, key="PROFILE-KEY-1234", provider=provider, memory=memory,
                         mcp_servers=[server()], approve=lambda request: asked.append(request) or True)
    try:
        await engine.turn("Leak it")
        assert results(engine)["c1"]["error_code"] == "credential_in_tool_arguments"
        assert results(engine)["c2"]["error_code"] == "arguments_too_large"
        assert asked == [] and events(engine, "action_started") == []
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_a_server_that_does_not_start_is_reported_and_the_turn_uses_built_in_tools(setup):
    memory = Memory()
    setup[3].joinpath("app.py").write_text("VALUE = 1\n")
    seen = []
    provider = ScriptedProvider(*acked(memory, call("read_file", {"path": "app.py"}, "c1")))
    engine = agent(setup, provider, memory, servers=[server("exit"), server(identifier="good")],
                   on_event=lambda event: seen.append(event))
    try:
        result = await engine.turn("Read app.py")
        assert result["status"] == "completed"
        assert results(engine)["c1"]["content"] == "VALUE = 1\n"
        assert [row["data"] for row in events(engine, "mcp_server_failed")] == [{"server_id": "files", "code": "start_failed"}]
        assert {"type": "mcp_server_failed", "data": {"server_id": "files", "code": "start_failed"}} in seen
        offered = names(provider.requests[2])
        assert "mcp_good_echo" in offered and not any(name.startswith("mcp_files_") for name in offered)
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_a_server_tool_named_like_a_built_in_one_never_shadows_it(setup, monkeypatch):
    memory = Memory()
    setup[3].joinpath("app.py").write_text("BUILT_IN = True\n")
    provider = ScriptedProvider(*acked(memory, call("read_file", {"path": "app.py"}, "c1")))
    engine = agent(setup, provider, memory, approve=lambda request: pytest.fail("a built-in read needs no approval"))
    try:
        await engine.turn("Read")
        assert results(engine)["c1"]["content"] == "BUILT_IN = True\n"
        offered = names(provider.requests[2])
        assert "read_file" in offered and "mcp_files_read_file" in offered
        # Nothing a person alone writes is a tool, and a server adds none: its tools carry the prefix.
        assert {row["name"] for row in MEMORY_TOOLS} == {
            "resume_context", "ack_resume", "ask", "remember", "checkpoint", "save_plan", "next_task",
            "check_status", "read_source", "memory_records"}
        assert all(name.startswith("mcp_") for name in engine.mcp.catalogue)
    finally:
        await engine.close()
    monkeypatch.setattr(agent_tools, "FILE_TOOLS", [*agent_tools.FILE_TOOLS, {
        "name": "mcp_builtin", "description": "", "parameters": {"type": "object"}}])
    with pytest.raises(ValueError, match="reserved"):
        agent(setup, ScriptedProvider(), memory)


@pytest.mark.asyncio
async def test_an_interrupted_call_is_an_uncertain_action_and_the_server_stops(setup):
    memory = Memory()
    provider = ScriptedProvider(*acked(memory, call("mcp_files_environment", {}, "c0"),
                                       call("mcp_files_slow", {}, "c1"), final="never"))
    engine = agent(setup, provider, memory, approve=lambda request: True)
    task = asyncio.create_task(engine.turn("Slow"))
    for _ in range(200):
        await asyncio.sleep(.05)
        if any(row["data"].get("tool_call_id") == "c1" for row in events(engine, "action_started")):
            break
    pid = json.loads(results(engine)["c0"]["content"][0]["text"])["pid"]
    await asyncio.sleep(.2)
    await engine.interrupt()
    result = await task
    try:
        assert result["status"] == "interrupted"
        assert [row["name"] for row in result["pending_actions"]] == ["mcp_files_slow"]
        assert events(engine, "action_interrupted")[0]["data"]["name"] == "mcp_files_slow"
        assert not alive(pid) and engine.mcp.host.servers["files"].state == "off"
        # Nothing is retried: the next turn waits for the person.
        assert (await engine.turn("Continue"))["status"] == "uncertain"
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_close_stops_every_server_and_copilot_runs_get_none(setup):
    memory = Memory()
    provider = ScriptedProvider(*acked(memory, call("mcp_files_environment", {}, "c1")))
    engine = agent(setup, provider, memory, approve=lambda request: True)
    await engine.turn("Env")
    pid = json.loads(results(engine)["c1"]["content"][0]["text"])["pid"]
    assert alive(pid)
    await engine.close()
    assert not alive(pid)
    assert [row["data"] for row in events(engine, "mcp_server_stopped")] == [{"server_id": "files", "reason": "run_ended"}]
    settings, run, profile, worktree = setup
    copilot = AgentEngine(settings, {**run, "client": "copilot", "session_id": "session-two"}, profile,
                          provider=ScriptedProvider(), memory=Memory(), mcp_servers=[server()])
    assert copilot.mcp.host.definitions == []
    await copilot.close()


def worker(tmp_path, servers=None, problems=None):
    run = {"id": "run-a", "generation": 2, "project_id": "project-a", "worktree": str(tmp_path / "project"),
           "session_id": "session-a", "client": "api"}
    profile = {"id": "profile-a", "label": "Test API", "client": "api", "model": "controlled"}
    created = {}

    def factory(*args, **kwargs):
        created.update(kwargs)
        return FakeEngine(*args, **kwargs)
    app = AgentApp(Settings(data_dir=tmp_path / "data"), run, profile, key="fixture-key", engine_factory=factory,
                   mcp_servers=servers, mcp_problems=problems)
    return app, created


@pytest.mark.asyncio
async def test_the_pane_approves_one_mcp_call_of_its_run_and_keeps_the_command_rule(tmp_path, monkeypatch):
    app, created = worker(tmp_path, servers=[server()], problems=[{"server_id": "local", "code": "definition_refused"}])
    async with app.run_test(size=(95, 42)) as pilot:
        await pilot.pause()
        assert created["mcp_servers"] == [server()]
        status = str(app.query_one("#agent-status").render())
        assert "local" in status and "definition_refused" in status
        shown = []

        async def confirm(screen):
            shown.append(screen)
            return True
        monkeypatch.setattr(app, "push_screen_wait", confirm)
        request = {"kind": "mcp_tool", "action_id": "a1", "server_id": "files", "tool": "echo",
                   "name": "mcp_files_echo", "arguments": {"text": "\x1b]0;owned\x07‮"}, "run_id": "run-a",
                   "generation": 2, "session_id": "session-a"}
        assert await app.approve({**request, "generation": 3}) is False
        assert await app.approve({**request, "session_id": "other"}) is False
        assert await app.approve({**request, "arguments": "text"}) is False
        # A command request still needs its string command, whatever else it carries.
        command = {k: v for k, v in request.items() if k != "kind"}
        assert await app.approve(command) is False
        assert shown == []
        assert await app.approve(request) is True
        assert shown[0].heading == "Approve one third-party tool call" and shown[0].approve == "Call once"
        body = shown[0].value
        assert "\x1b" not in body and "‮" not in body and "\\u202e" in body and "mcp_files_echo" in body
        await app.agent_event({"type": "mcp_server_failed", "data": {"server_id": "x\x1b[2J", "code": "start\x07"}})
        lines = "\n".join(line.text for line in app.query_one("#agent-history").lines)
        assert "\x1b" not in lines and "Third-party MCP server unknown is off (unknown)" in lines


@pytest.mark.asyncio
async def test_acceptance_a_person_added_server_in_one_project_only(tmp_path):
    """The issue's scenario at the engine level with a fake provider and the fixture server."""
    settings = Settings(tmp_path / "private-data")
    state = CockpitState(settings)
    state.update({"project_mcp_servers": {"project-a": [server(identifier="tracker"), server("exit", identifier="broken")]}})
    worktree_a, worktree_b = tmp_path / "a", tmp_path / "b"
    worktree_a.mkdir()
    worktree_b.mkdir()
    (worktree_a / "notes.md").write_text("plan\n")
    servers_a, problems_a = frozen_servers(state.load(), "project-a", worktree_a)
    servers_b, _ = frozen_servers(state.load(), "project-b", worktree_b)
    assert problems_a == [] and len(servers_a) == 2 and servers_b == []
    profile = {"id": "api", "client": "api", "label": "API", "provider": "ollama", "base_url": "http://127.0.0.1:11434",
               "model": "tool-model", "limits": {}}
    memory = Memory()
    approvals, seen = [], []
    provider = ScriptedProvider(*acked(memory, call("mcp_tracker_echo", {"text": "issue 7"}, "c1"),
                                       call("read_file", {"path": "notes.md"}, "c2")))
    run_a = {"id": "run-a", "generation": 1, "project_id": "project-a", "worktree": str(worktree_a),
             "session_id": "session-a", "client": "api"}
    engine = AgentEngine(settings, run_a, profile, provider=provider, memory=memory, mcp_servers=servers_a,
                         approve=lambda request: approvals.append(request) or True, on_event=seen.append)
    try:
        assert (await engine.turn("Check issue 7 in the tracker"))["status"] == "completed"
        assert "mcp_tracker_echo" in names(provider.requests[2])
        assert [request["name"] for request in approvals] == ["mcp_tracker_echo"]
        completed = [row["data"] for row in events(engine, "action_completed")]
        assert completed[0]["name"] == "mcp_tracker_echo" and completed[0]["arguments"] == {"text": "issue 7"}
        assert completed[0]["result"]["content"][0]["text"] == 'echo: {"text": "issue 7"}'
        assert results(engine)["c2"]["content"] == "plan\n"
        assert {"type": "mcp_server_failed", "data": {"server_id": "broken", "code": "start_failed"}} in seen
    finally:
        await engine.close()
    other_memory = Memory()
    other = ScriptedProvider(*acked(other_memory))
    run_b = {**run_a, "id": "run-b", "project_id": "project-b", "worktree": str(worktree_b), "session_id": "session-b"}
    engine_b = AgentEngine(settings, run_b, profile, provider=other, memory=other_memory, mcp_servers=servers_b)
    try:
        await engine_b.turn("Anything")
        assert not any(name.startswith("mcp_") for request in other.requests for name in names(request))
    finally:
        await engine_b.close()


@pytest.mark.asyncio
async def test_a_server_that_did_not_start_stays_named_after_later_statuses(tmp_path):
    # #157: the warning was replaced at once by a raw {"state": "running"} status and then by Completed.
    app, _ = worker(tmp_path, servers=[server()])
    async with app.run_test(size=(95, 42)) as pilot:
        await pilot.pause()
        assert not app.query_one("#agent-mcp").has_class("-live")
        await app.agent_event({"type": "mcp_server_failed", "data": {"server_id": "time", "code": "start_failed"}})
        await app.agent_event({"type": "status", "data": {"state": "running", "memory": "requires_model_ack"}})
        status = str(app.query_one("#agent-status").render())
        assert "{" not in status and "Working" in status
        app.execute_turn("hello")
        await app.workers.wait_for_complete()
        await pilot.pause()
        row = app.query_one("#agent-mcp")
        assert row.has_class("-live") and "time (start_failed)" in str(row.render())
        assert "Completed" in str(app.query_one("#agent-status").render())
