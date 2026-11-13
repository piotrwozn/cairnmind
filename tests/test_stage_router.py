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


def test_cockpit_stage_reports_an_unknown_assistant_instead_of_another(cockpit):
    settings, app, client, runtime, project, requests = cockpit
    CockpitState(settings).update({"project_stages": {project["id"]: {"review": "removed-slot"}}})
    headers = login(app, client)
    value = client.get("/api/stage", params={"project": project["id"]}, headers=headers).json()
    assert (value["action"], value["problem"], value["slot_id"], value["client"]) == (
        "none", "unknown_assistant", "removed-slot", None)
    assert runtime.starts == []
