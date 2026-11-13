"""Plan stages with the built-in API agent and GitHub Copilot (#125), at the level of the #10 acceptance test.

The router picks the stage; the cockpit's own launcher (RuntimeSupervisor.start_project, what
POST /api/client-runs calls) prepares the agent's session against the real memory service; the
production engine then reads and acknowledges the packet only because the model asked for it.
The provider is scripted and Copilot is a fake SDK turn: no network, no real key, no tmux.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from cairnmind.agent_memory import AgentMemoryClient
from cairnmind.api_agent import AgentEngine
from cairnmind.api_credentials import CredentialStore
from cairnmind.api_profiles import normalize_profile
from cairnmind.api_runtime import RuntimeSupervisor, stage_readiness
from cairnmind.cockpit_state import CockpitState
from cairnmind.plans import PlanManager
from cairnmind.service import create_app
from cairnmind.sessions import SessionManager
from cairnmind.stages import project_routing, resolve_stage
from cairnmind.store import Store
from test_api_agent import ScriptedProvider, call, reply
from test_brief import repository
from test_stage_router import move

PROFILES = [{"id": "codex", "client": "codex", "label": "Codex"},
            {"id": "claude", "client": "claude", "label": "Claude"},
            {"id": "api-cloud", "client": "api", "label": "Cloud API", "provider": "openai", "model": "gpt-x",
             "credential_ref": "env:FIXTURE_STAGE_KEY", "limits": {"max_model_calls": 7, "max_output_tokens": 512}},
            {"id": "copilot", "client": "copilot", "label": "GitHub Copilot",
             "limits": {"max_tool_calls": 9, "max_turn_seconds": 300}}]
SLOTS = [{"id": "codex", "profile_id": "codex"}, {"id": "claude", "profile_id": "claude"},
         {"id": "cloud", "profile_id": "api-cloud"}, {"id": "pilot", "profile_id": "copilot"}]


class ServiceTransport(httpx.BaseTransport):
    """The launcher's synchronous client, answered by the real memory service app."""
    def __init__(self, app):
        self.app = app

    def handle_request(self, request):
        async def forward():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                         base_url="http://memory.invalid") as client:
                answer = await client.request(request.method, request.url.raw_path.decode(),
                                              content=request.read(), headers=dict(request.headers))
                return httpx.Response(answer.status_code, content=answer.content,
                                      headers={"content-type": answer.headers.get("content-type", "application/json")})
        return asyncio.run(forward())


@pytest.fixture
def stack(tmp_path, monkeypatch):
    for name in ("CAIRNMIND_SESSION_ID", "DEVBRAIN_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    root = repository(tmp_path / "repo")
    store = Store(tmp_path / "memory")
    settings = store.settings
    project = store.add_project("demo", root)
    plans, sessions = PlanManager(store), SessionManager(store, settings)
    saved = plans.save(project["id"], {"title": "Two steps"},
                       [{"key": "build", "content": "Build it"},
                        {"key": "polish", "content": "Polish it", "depends_on": ["build"]}], "initial", activate=True)
    service = create_app(settings, start_worker=False)
    environment = {"FIXTURE_STAGE_KEY": "fixture-not-a-real-key"}
    credentials = CredentialStore(settings, environ=environment)
    monkeypatch.setattr("cairnmind.copilot_adapter.runtime_status", lambda settings=None: {
        "available": True, "code": "ready"})
    runner = RuntimeSupervisor(settings, "/nonexistent/tmux", credentials=credentials,
                               session_transport=ServiceTransport(service))
    monkeypatch.setattr(runner, "diagnostics", lambda: {"available": True})
    spawned = []

    def spawn(rows, project, client, slot, launch, generation, purpose, *, run_id):
        # Instead of a tmux pane: the registry row the real spawn would write.
        spawned.append(launch)
        row = {"id": run_id, "generation": generation, "session_id": launch["session_id"],
               "project_id": project["id"], "worktree": launch["cwd"], "client": client,
               "profile_id": launch["profile_id"], "slot_id": slot, "purpose": purpose, "lifecycle": "running"}
        rows.append(row)
        runner._save(rows)
        return dict(row)
    monkeypatch.setattr(runner, "_spawn", spawn)
    monkeypatch.setattr(runner, "_refresh", lambda row: row)
    return {"root": root, "store": store, "settings": settings, "project": project, "plans": plans,
            "sessions": sessions, "saved": saved, "service": service, "credentials": credentials,
            "environment": environment, "runner": runner, "spawned": spawned}


def route(stack):
    preferences = CockpitState(stack["settings"]).load()
    return resolve_stage(stack["plans"].next_task(stack["project"]["id"], None, str(stack["root"])),
                         *project_routing(preferences, stack["project"]["id"]),
                         readiness=stage_readiness(stack["settings"], stack["credentials"]))


def readback_of(packet):
    return {key: ({"id": packet[field]["id"], "version": packet[field]["version"]} if packet.get(field) else None)
            for key, field in (("plan", "active_plan"), ("task", "active_task"))}


def hand_on(stack):
    """The remember call that moves the step to review, as the implementing assistant sends it."""
    current = stack["plans"].get(stack["project"]["id"], stack["saved"]["plan"]["id"])
    task = next(t for t in current["tasks"] if t["metadata"]["step_key"] == "build")
    return {"record": {"id": task["id"], "expected_version": task["version"],
                       "expected_plan_version": current["plan"]["version"], "metadata": {"state": "needs_review"}},
            "sources": task["source_ids"]}


def native_session(stack, decision):
    # What the native launcher does for the chosen client: prepare a packet; the assistant ACKs it.
    sessions = stack["sessions"]
    prepared = sessions.prepare(stack["project"]["id"], decision["client"], worktree=str(stack["root"]), timeout=0)
    assert prepared["status"] == "ready"
    packet = prepared["packet"]
    readback = readback_of(packet)
    sessions.ack(prepared["session_id"], packet["id"], packet["hash"], readback)
    sessions.require_ack(prepared["session_id"])
    if decision["stage"] == "implement":
        move(stack["store"], stack["plans"], stack["project"], stack["saved"], "needs_review")
    return prepared["session_id"], readback


def agent_session(stack, decision):
    store, sessions, plans, project = stack["store"], stack["sessions"], stack["plans"], stack["project"]
    profile = next(p for p in CockpitState(stack["settings"]).load()["profiles"] if p["id"] == decision["profile_id"])
    before = plans.get(project["id"], stack["saved"]["plan"]["id"])
    # The slot exactly as POST /api/client-runs hands it to the launcher (cockpit.py start_runs).
    slot = {**profile, "id": decision["slot_id"], "profile_id": profile["id"]}
    started = stack["runner"].start_project({**project, "root": str(stack["root"])}, worktree=str(stack["root"]),
                                            slots=[slot])
    assert started["errors"] == [] and len(started["runs"]) == 1
    run = started["runs"][0]
    sid = run["session_id"]
    assert (run["client"], run["slot_id"], run["profile_id"]) == (decision["client"], decision["slot_id"], profile["id"])
    # Starting prepared a packet and nothing else: no ACK, no task or plan change.
    assert sessions.status(sid)["acknowledged"] is False
    assert store.query("SELECT FROM resume_acks WHERE session_id=:id", {"id": sid}) == []
    assert plans.get(project["id"], stack["saved"]["plan"]["id"]) == before
    # The run is bounded by the profile's own limits, as when it is started by hand.
    configured = stack["runner"].agent_configuration(run["id"], run["generation"])["profile"]
    assert configured["limits"] == normalize_profile(profile)["limits"]
    assert {key: configured["limits"][key] for key in profile["limits"]} == profile["limits"]
    implementing = decision["stage"] == "implement"
    seen = {}

    def ack(packet):
        assert packet["status"] == "ready"
        seen["readback"] = readback_of(packet)
        return {"packet_id": packet["id"], "packet_hash": packet["hash"], "readback": seen["readback"]}

    if decision["client"] == "api":
        def acknowledge(messages, tools):
            return reply(call("ack_resume", ack(json.loads(messages[-1]["content"])["packet"])))

        def finish(messages, tools):
            last = json.loads(messages[-1]["content"])
            assert "error_code" not in last, last
            return reply(text="Done for this stage.")
        script = [reply(call("resume_context")), acknowledge]
        if implementing:
            script.append(lambda messages, tools: reply(call("remember", hand_on(stack))))
        provider = ScriptedProvider(*script, finish)
    else:
        class Copilot:
            async def run_turn(self, prompt, tools, handler, *, system="", on_delta=None, on_event=None):
                packet = (await handler("resume_context", {}))["packet"]
                acknowledged = await handler("ack_resume", ack(packet))
                assert acknowledged["status"] == "acknowledged", acknowledged
                if implementing:
                    saved = await handler("remember", hand_on(stack))
                    assert "error_code" not in saved, saved
                return {"text": "Done for this stage.", "usage": {"input_tokens": 5, "output_tokens": 2}}
        provider = Copilot()
    memory = AgentMemoryClient(stack["settings"], project["id"], stack["root"], sid, client=decision["client"],
                               transport=httpx.ASGITransport(app=stack["service"]))

    async def work():
        engine = AgentEngine(stack["settings"], run, configured, provider=provider, memory=memory)
        try:
            return await engine.turn("Work on the next plan step.")
        finally:
            await engine.close()
    result = asyncio.run(work())
    assert result["status"] == "completed", result
    acks = store.query("SELECT FROM resume_acks WHERE session_id=:id", {"id": sid})
    assert len(acks) == 1 and acks[0]["readback"] == seen["readback"]
    if not implementing:
        sessions.require_ack(sid)
    return sid, seen["readback"]


@pytest.mark.parametrize("implement,review", [("cloud", "claude"), ("claude", "cloud"),
                                              ("pilot", "claude"), ("claude", "pilot")])
def test_two_step_plan_with_an_api_or_copilot_stage_each_session_acks_the_same_plan(stack, implement, review):
    project, plans, saved = stack["project"], stack["plans"], stack["saved"]
    CockpitState(stack["settings"]).update({"profiles": PROFILES, "project_slots": {project["id"]: SLOTS},
                                            "project_stages": {project["id"]: {"implement": implement,
                                                                               "review": review}}})

    def launch(decision):
        return (agent_session if decision["client"] in {"api", "copilot"} else native_session)(stack, decision)

    first = route(stack)
    assert (first["stage"], first["action"], first["slot_id"], first["task"]["state"]) == (
        "implement", "start_slot", implement, "todo")
    assert route(stack) == first  # Routing again changes nothing.
    implementer, implement_readback = launch(first)
    second = route(stack)
    assert (second["stage"], second["action"], second["slot_id"], second["task"]["id"]) == (
        "review", "start_slot", review, first["task"]["id"])
    with pytest.raises(ValueError, match="resume_required"):
        stack["sessions"].require_ack(implementer)  # The step changed: the earlier ACK no longer covers it.
    reviewer, review_readback = launch(second)
    assert implement_readback["task"] == {"id": first["task"]["id"], "version": first["task"]["version"]}
    assert review_readback["task"] == {"id": second["task"]["id"], "version": second["task"]["version"]}
    # The same plan, each at the version current when its session started (see #10).
    assert implement_readback["plan"] == {"id": saved["plan"]["id"], "version": saved["plan"]["version"]}
    assert review_readback["plan"] == {"id": saved["plan"]["id"], "version": saved["plan"]["version"] + 1}
    assert review_readback["task"]["version"] == implement_readback["task"]["version"] + 1
    steps = [(t["content"], t["metadata"].get("depends_on")) for t in plans.get(project["id"], saved["plan"]["id"])["tasks"]]
    assert steps == [(t["content"], t["metadata"].get("depends_on")) for t in saved["tasks"]]
    # Only the mapped assistants ran: one agent launch, never a substitute.
    assert [launch["profile_id"] for launch in stack["spawned"]] == [
        "api-cloud" if "cloud" in (implement, review) else "copilot"]
    states = [t["metadata"]["state"] for t in plans.get(project["id"], saved["plan"]["id"])["tasks"]]
    assert states == ["needs_review", "todo"]  # Nobody but the implementing assistant moved a step.


def test_an_api_stage_without_a_credential_says_so_and_starts_nothing(stack):
    project = stack["project"]
    stack["environment"].clear()
    CockpitState(stack["settings"]).update({"profiles": PROFILES, "project_slots": {project["id"]: SLOTS},
                                            "project_stages": {project["id"]: {"implement": "cloud"}}})
    sessions_before = stack["store"].query("SELECT FROM sessions")
    decision = route(stack)
    assert (decision["action"], decision["problem"], decision["slot_id"]) == ("none", "credential_missing", "cloud")
    # Even when it is started by hand, the launcher refuses before a session or a process exists.
    profile = next(p for p in PROFILES if p["id"] == "api-cloud")
    started = stack["runner"].start_project({**project, "root": str(stack["root"])}, worktree=str(stack["root"]),
                                            slots=[{**normalize_profile(profile), "id": "cloud",
                                                    "profile_id": "api-cloud"}])
    assert started["runs"] == [] and [e["code"] for e in started["errors"]] == ["credential_missing"]
    assert stack["spawned"] == [] and stack["store"].query("SELECT FROM sessions") == sessions_before
