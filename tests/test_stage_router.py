"""The stage router suggests: reading the next stage changes no task and starts nothing."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient
import httpx
import pytest
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.cockpit import create_cockpit_app
from cairnmind.cockpit_state import CockpitState
from cairnmind.config import Settings
from cairnmind.plans import PlanManager
from cairnmind.store import Store
from test_cockpit_server import ORIGIN, Runtime, login

STAGES = {"implement": "codex", "review": "claude", "test": "unit"}


@pytest.fixture
def planned(tmp_path, monkeypatch):
    for name in ("CAIRNMIND_SESSION_ID", "DEVBRAIN_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings.load(tmp_path / "data", create=True)
    store = Store(settings.data_dir)
    root = tmp_path / "repo"
    root.mkdir()
    project = store.add_project("demo", root)
    plans = PlanManager(store)
    saved = plans.save(project["id"], {"title": "Two \x1b[31msteps"},
                       [{"key": "build", "content": "Build it"},
                        {"key": "review", "content": "Review it", "depends_on": ["build"]}], "initial", activate=True)
    CockpitState(settings).update({"project_stages": {project["id"]: STAGES}})
    monkeypatch.setattr(cli, "store_for", lambda _: store)
    return settings, store, project, plans, saved


def move(store, plans, project, saved, state):
    current = plans.get(project["id"], saved["plan"]["id"])
    task = next(t for t in current["tasks"] if t["metadata"]["step_key"] == "build")
    return store.remember(project["id"], {"id": task["id"], "expected_version": task["version"],
        "expected_plan_version": current["plan"]["version"], "metadata": {"state": state}}, task["source_ids"])


def test_cli_stage_next_prints_the_existing_command_and_changes_nothing(planned):
    settings, store, project, plans, saved = planned
    runner = CliRunner()
    before = plans.get(project["id"], saved["plan"]["id"])
    shown = runner.invoke(cli.app, ["--data-dir", str(settings.data_dir), "stage", "next", "demo"])
    assert shown.exit_code == 0, shown.output or repr(shown.exception)
    assert "Stage: implement" in shown.output and "Assistant: Codex (codex)" in shown.output
    assert "Two \\x1b[31msteps" in shown.output and "\x1b" not in shown.output
    root = str(store.resolve_project("demo")["root"])
    assert f"Next: cairnmind run {project['id']} codex --worktree {root}" in shown.output
    assert plans.get(project["id"], saved["plan"]["id"]) == before

    moved = move(store, plans, project, saved, "needs_test")
    decision = json.loads(runner.invoke(cli.app, ["--data-dir", str(settings.data_dir), "stage", "next", "demo",
                                                  "--json"]).output)
    assert (decision["stage"], decision["action"], decision["check_profile"]) == ("test", "run_check", "unit")
    assert decision["command"] == ["cairnmind", "check", "run", project["id"], "unit", "--task", moved["id"],
                                   "--worktree", root]
    assert decision["task"]["version"] == moved["version"]
    assert store.query("SELECT FROM check_runs WHERE project_id=:p", {"p": project["id"]}) == []

    CockpitState(settings).update({"project_stages": {project["id"]: {"implement": "codex"}}})
    unmapped = runner.invoke(cli.app, ["--data-dir", str(settings.data_dir), "stage", "next", "demo"])
    assert unmapped.exit_code == 0 and "No assistant or check is mapped to this stage." in unmapped.output
    assert "Next:" not in unmapped.output


def test_cli_prints_a_launch_command_only_when_it_does_exactly_what_the_map_says(planned):
    settings, store, project, plans, saved = planned
    runner = CliRunner()

    def ask(*extra):
        shown = runner.invoke(cli.app, ["--data-dir", str(settings.data_dir), "stage", "next", "demo", *extra])
        assert shown.exit_code == 0, shown.output or repr(shown.exception)
        return shown.output

    state = CockpitState(settings)
    plain = [{"id": "codex", "client": "codex", "label": "Codex"}, {"id": "claude", "client": "claude", "label": "Claude"}]
    state.update({"profiles": [{**plain[0], "model": "gpt-6.1"}, plain[1]]})
    output = ask()
    assert "Assistant: Codex (codex)" in output and "Next:" not in output
    assert "Start this stage from the workspace with Run next stage." in output
    value = json.loads(ask("--json"))
    assert (value["command"], value["command_reason"]) == (None, "profile_model")

    state.update({"profiles": plain, "project_slots": {project["id"]: [
        {"id": "codex", "profile_id": "codex"}, {"id": "codex-two", "profile_id": "codex"}]}})
    assert json.loads(ask("--json"))["command_reason"] == "shared_client"

    state.update({"project_slots": {}})
    other = plans.save(project["id"], {"title": "Not active"}, [{"key": "x", "content": "Other"}], "other")
    value = json.loads(ask("--plan-id", other["plan"]["id"], "--json"))
    assert value["plan"]["id"] == other["plan"]["id"] and value["action"] == "start_slot"
    assert (value["command"], value["command_reason"]) == (None, "inactive_plan")
    assert json.loads(ask("--plan-id", saved["plan"]["id"], "--json"))["command"][:2] == ["cairnmind", "run"]


@pytest.fixture
def cockpit(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data", create=True)
    root = tmp_path / "repo"
    root.mkdir()
    project = {"id": "project-one", "name": "Example", "root": str(root)}
    runtime, requests = Runtime(), []
    task = {"id": "task-1", "version": 4, "content": "Review \x1b]0;x\x07 it", "metadata": {"state": "needs_review"}}

    def upstream(request):
        requests.append((request.method, request.url.path, request.content))
        if request.url.path == "/projects":
            return httpx.Response(200, json=[project])
        if request.url.path == "/project-roots":
            return httpx.Response(200, json={"roots": [str(root)]})
        if request.url.path == "/plans/next":
            return httpx.Response(200, json={"plan": {"id": "plan-1", "version": 2, "title": "API"}, "task": task,
                                             "reason": "needs_review", "blocked": []})
        return httpx.Response(404, json={"detail": "unexpected"})

    CockpitState(settings).update({"project_stages": {project["id"]: STAGES}})
    app = create_cockpit_app(settings, runtime=runtime, proxy_transport=httpx.MockTransport(upstream), public_url=ORIGIN)
    with TestClient(app, base_url=ORIGIN) as client:
        yield settings, app, client, runtime, project, requests


def test_cockpit_stage_is_read_only_and_never_starts_an_assistant(cockpit):
    settings, app, client, runtime, project, requests = cockpit
    headers = login(app, client)
    answer = client.get("/api/stage", params={"project": project["id"]}, headers=headers)
    assert answer.status_code == 200, answer.text
    value = answer.json()
    assert (value["stage"], value["action"], value["slot_id"], value["client"]) == ("review", "start_slot", "claude", "claude")
    assert value["task"]["version"] == 4 and value["worktree"] == project["root"]
    assert ("POST", "/plans/next") in [row[:2] for row in requests]
    sent = next(json.loads(row[2]) for row in requests if row[1] == "/plans/next")
    assert sent == {"project": project["id"], "worktree": project["root"]}
    assert runtime.starts == [] and not any(row[1].startswith("/sessions") for row in requests)
    count = len(requests)
    for params in ({"project": project["id"], "session_id": "forged"}, {"worktree": project["root"]}):
        assert client.get("/api/stage", params=params, headers=headers).status_code == 422
    assert len(requests) == count
    assert client.get("/api/stage", params={"project": project["id"], "worktree": "/elsewhere"},
                      headers=headers).status_code == 409
    client.cookies.clear()
    assert client.get("/api/stage", params={"project": project["id"]},
                      headers={"Origin": ORIGIN}).status_code in {401, 403}


def test_two_step_plan_hands_implement_to_codex_and_review_to_claude_each_with_its_own_ack(tmp_path):
    """The acceptance scenario with a fake runner: the router picks, the launcher's packet is ACKed."""
    from cairnmind.sessions import SessionManager
    from cairnmind.stages import project_routing, resolve_stage
    from test_brief import repository
    root = repository(tmp_path / "repo")
    store = Store(tmp_path / "memory")
    settings = Settings.load(store.data_dir)
    project = store.add_project("demo", root)
    plans, sessions = PlanManager(store), SessionManager(store, settings)
    saved = plans.save(project["id"], {"title": "Two steps"},
                       [{"key": "build", "content": "Build it"},
                        {"key": "polish", "content": "Polish it", "depends_on": ["build"]}], "initial", activate=True)
    CockpitState(settings).update({"project_stages": {project["id"]: STAGES}})
    preferences = CockpitState(settings).load()

    def route():
        return resolve_stage(plans.next_task(project["id"], None, str(root)),
                             *project_routing(preferences, project["id"]))

    def launch_and_ack(decision):
        # What the launcher does for the chosen client: prepare a packet; the assistant ACKs it.
        prepared = sessions.prepare(project["id"], decision["client"], worktree=str(root), timeout=0)
        assert prepared["status"] == "ready"
        packet = prepared["packet"]
        readback = {key: ({"id": packet[field]["id"], "version": packet[field]["version"]} if packet.get(field) else None)
                    for key, field in (("plan", "active_plan"), ("task", "active_task"))}
        assert readback["task"] == {"id": decision["task"]["id"], "version": decision["task"]["version"]}
        sessions.ack(prepared["session_id"], packet["id"], packet["hash"], readback)
        sessions.require_ack(prepared["session_id"])
        return prepared["session_id"], readback

    first = route()
    assert (first["stage"], first["client"], first["task"]["state"]) == ("implement", "codex", "todo")
    codex, codex_readback = launch_and_ack(first)
    assert route() == first  # Routing again changes nothing.
    # The implementing assistant (not the router) hands the step on to review.
    move(store, plans, project, saved, "needs_review")
    second = route()
    assert (second["stage"], second["client"], second["task"]["id"]) == ("review", "claude", first["task"]["id"])
    with pytest.raises(ValueError, match="resume_required"):
        sessions.require_ack(codex)  # The task changed: the earlier ACK no longer covers it.
    claude, claude_readback = launch_and_ack(second)
    # Both ACK the same plan. Its version is not the same: recording the task's new state is a
    # plan change (plans.py bumps the version with every task edit), so each session ACKs the
    # version current at its own launch. Nothing but that one state edit happened in between.
    assert codex_readback["plan"] == {"id": saved["plan"]["id"], "version": saved["plan"]["version"]}
    assert claude_readback["plan"] == {"id": saved["plan"]["id"], "version": saved["plan"]["version"] + 1}
    assert claude_readback["task"]["version"] == codex_readback["task"]["version"] + 1
    steps = [(t["content"], t["metadata"].get("depends_on")) for t in plans.get(project["id"], saved["plan"]["id"])["tasks"]]
    assert steps == [(t["content"], t["metadata"].get("depends_on")) for t in saved["tasks"]]


def test_cockpit_stage_reports_an_unknown_assistant_instead_of_another(cockpit):
    settings, app, client, runtime, project, requests = cockpit
    CockpitState(settings).update({"project_stages": {project["id"]: {"review": "removed-slot"}}})
    headers = login(app, client)
    value = client.get("/api/stage", params={"project": project["id"]}, headers=headers).json()
    assert (value["action"], value["problem"], value["slot_id"], value["client"]) == (
        "none", "unknown_assistant", "removed-slot", None)
    assert runtime.starts == []
