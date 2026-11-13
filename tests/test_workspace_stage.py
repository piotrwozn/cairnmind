"""Run next stage in the workspace: Start work for one assistant, or exactly `cairnmind check run`."""
from __future__ import annotations

from copy import deepcopy
import json
import subprocess
import sys

import pytest
from textual.widgets import Static
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind import workspace_stage
from cairnmind.config import Settings
from cairnmind.plans import PlanManager
from cairnmind.sessions import SessionManager
from cairnmind.store import Store
from cairnmind.terminal_commands import COMMANDS, dispatch_command
from cairnmind.terminal_panels import SidebarApp
from cairnmind.terminal_ui import ChoiceModal, FormModal, MessageModal
from cairnmind.workspace_state import WorkspaceState
from test_brief import check_profile, repository
from test_terminal_panels import settle
from test_terminal_ux080 import screen_text, setup_ui


def decision(action, project, **extra):
    value = {"stage": "implement", "action": action, "problem": None, "reason": "ready",
             "plan": {"id": "plan-1", "version": 2, "title": "API"},
             "task": {"id": "task-1", "version": 5, "state": "todo", "content": "Build \x1b[2J it"},
             "slot_id": None, "profile_id": None, "client": None, "label": None, "check_profile": None,
             "required_profiles": [], "project_id": project["id"], "worktree": project["root"]}
    value.update(extra)
    return value


def answer_stage(broker, value, *, runs=(), during=None):
    original, asked = broker.request, []

    def request(method, path, *, params=None, json=None, timeout=20):
        if path == "/api/stage":
            asked.append(deepcopy(params))
            if during is not None:
                during()
            return deepcopy(value)
        result = original(method, path, params=params, json=json, timeout=timeout)
        if path == "/api/state" and runs:
            result = {**result, "runs": deepcopy(list(runs))}
        return result
    broker.request = request
    return asked


async def wait_for(app, pilot, kind):
    for _ in range(100):
        await pilot.pause(.02)
        if isinstance(app.screen, kind):
            return
    raise AssertionError(f"{kind.__name__} did not open")


@pytest.mark.asyncio
async def test_client_stage_is_start_work_for_exactly_the_mapped_assistant(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    project = broker.projects[0]
    asked = answer_stage(broker, decision("start_slot", project, slot_id="slot-native", client="codex",
                                          profile_id="native-private-1", label="Codex"))
    monkeypatch.setattr(workspace_stage, "run_stage_check", lambda *a: pytest.fail("a client stage ran a check"))
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        # The menu entry only shows the project and focuses the button, like Start work.
        assert app.open_command("next_stage") is True
        await settle(app, pilot)
        assert app.page == "project" and app.focused.id == "stage-next" and asked == []
        assert all(intent == "inspect" for _, _, intent in workspace.selections)
        await pilot.press("enter")
        await settle(app, pilot)
        notice = str(app.query_one("#panel-notice", Static).render())
    assert asked == [{"project": project["id"], "worktree": project["root"]}]
    assert workspace.selections[-1] == (project["id"], project["root"], "work")
    assert workspace.read()["requested_slot_ids"] == ["slot-native"] and workspace.read()["focus"] == "client1"
    assert "Stage started. The assistant must acknowledge its resume packet." in notice
    assert "Build \\x1b[2J it" in notice and "\x1b" not in notice
    # The sidebar itself never posts a launch; the supervisor claims the work request once.
    assert not any(path == "/api/client-runs" for _, path, _, _ in broker.calls)


@pytest.mark.asyncio
async def test_client_stage_shows_why_the_assistant_did_not_start(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    project = broker.projects[0]
    answer_stage(broker, decision("start_slot", project, slot_id="slot-native", client="codex"))

    def refused(*args, **kwargs):
        workspace.update({"launch_errors": [{"slot_id": "slot-native", "client": "codex", "code": "login_required"}]})
        return workspace.read()
    monkeypatch.setattr("cairnmind.workspace.refresh_workspace", refused)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.action_next_stage()
        await settle(app, pilot)
        notice = str(app.query_one("#panel-notice", Static).render())
    assert "The assistant did not start: login_required" in notice


@pytest.mark.asyncio
async def test_check_stage_runs_only_cairnmind_check_run_and_leaves_the_task(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    project = broker.projects[0]
    answer_stage(broker, decision("run_check", project, stage="test", check_profile="unit",
                                  required_profiles=["unit"]))
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        run = {"id": "run-1", "passed": False, "outcome": "completed", "returncode": 1, "reasons": [], "error": None}
        return subprocess.CompletedProcess(argv, 1, stdout=json.dumps(run), stderr="")
    real = workspace_stage.run_stage_check
    monkeypatch.setattr(workspace_stage, "run_stage_check", lambda s, d: real(s, d, runner=runner))
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.action_next_stage()
        await wait_for(app, pilot, MessageModal)
        await pilot.pause()
        text = screen_text(app)
        await pilot.press("escape")
        await settle(app, pilot)
    assert [argv for argv, _ in calls] == [[sys.executable, "-I", "-m", "cairnmind", "--data-dir", str(settings.data_dir),
        "check", "run", project["id"], "unit", "--task", "task-1", "--worktree", project["root"]]]
    assert "env" not in calls[0][1]  # The check runs in the sidebar's own identity, never a different one.
    assert "Check did not pass." in text and "The task state is unchanged." in text
    assert all(intent != "work" for _, _, intent in workspace.selections)


@pytest.mark.asyncio
async def test_unknown_assistant_is_shown_and_nothing_starts(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    project = broker.projects[0]
    answer_stage(broker, decision("none", project, problem="unknown_assistant", slot_id="removed"))
    monkeypatch.setattr(workspace_stage, "run_stage_check", lambda *a: pytest.fail("ran a check"))
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.action_next_stage()
        await wait_for(app, pilot, MessageModal)
        await pilot.pause()
        text = screen_text(app)
    assert "The mapped assistant is not configured for this project." in text
    assert all(intent != "work" for _, _, intent in workspace.selections)


@pytest.mark.asyncio
async def test_plan_stages_offer_only_launchable_assistants_and_save_in_preferences(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.edit_stages()
        await wait_for(app, pilot, ChoiceModal)
        await pilot.press("down", "enter")  # review
        await wait_for(app, pilot, ChoiceModal)
        await pilot.pause()
        offered = [value for _, value in app.screen.choices]
        await pilot.press("enter")
        await settle(app, pilot)
        app.edit_stages()
        await wait_for(app, pilot, ChoiceModal)
        await pilot.press("down", "down", "enter")  # test
        await wait_for(app, pilot, FormModal)
        await pilot.press(*"unit", "ctrl+s")
        await settle(app, pilot)
    # GitHub Copilot is configured as an assistant but cannot be launched for a plan step.
    assert offered == ["slot-native", ":unset"]
    saves = [body for method, path, _, body in broker.calls if (method, path) == ("PUT", "/api/settings")]
    assert saves == [{"project_stages": {"project-keyboard": {"review": "slot-native"}}},
                     {"project_stages": {"project-keyboard": {"review": "slot-native", "test": "unit"}}}]


def test_palette_routes_run_next_stage_through_the_workspace_state(tmp_path):
    calls = []
    command = next(row for row in COMMANDS if row[3] == "next_stage")
    assert command[0] == "Run next stage" and command[2] == "command"
    dispatch_command(type("Manager", (), {"request_command": lambda self, value: calls.append(value)})(), command)
    assert calls == ["next_stage"]
    state = WorkspaceState(Settings(data_dir=tmp_path / "data"))
    saved = state.update({"command_request": {"id": "c1", "action": "next_stage", "target_role": "sidebar"}})
    assert saved["command_request"]["action"] == "next_stage"


def test_the_workspace_check_command_is_the_cli_check_run_with_its_session_rule(tmp_path, monkeypatch):
    for name in ("CAIRNMIND_SESSION_ID", "DEVBRAIN_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    root = repository(tmp_path / "repo")
    check_profile(root)
    store = Store(tmp_path / "memory")
    settings = Settings.load(store.data_dir)
    project = store.add_project("demo", root)
    plans = PlanManager(store)
    saved = plans.save(project["id"], {"title": "One step"}, [{"key": "build", "content": "Build"}], "s", activate=True)
    task = saved["tasks"][0]
    task = store.remember(project["id"], {"id": task["id"], "expected_version": task["version"],
        "expected_plan_version": saved["plan"]["version"], "metadata": {"state": "needs_test"}}, task["source_ids"])
    monkeypatch.setattr(cli, "store_for", lambda _: store)
    chosen = {"action": "run_check", "check_profile": "unit", "task": {"id": task["id"]},
              "project_id": project["id"], "worktree": str(root)}
    argv = workspace_stage.check_argv(settings, chosen)
    assert argv[:4] == [sys.executable, "-I", "-m", "cairnmind"]
    runner = CliRunner()
    ran = runner.invoke(cli.app, argv[4:])
    assert ran.exit_code == 0, ran.output or repr(ran.exception)
    assert json.loads(ran.output)["passed"] is True
    after = store.query("SELECT FROM records WHERE id=:id", {"id": task["id"]})[0]
    assert after["metadata"]["state"] == "needs_test" and after["version"] == task["version"]
    # A coding session that has not acknowledged its packet is refused by the same command.
    session = SessionManager(store, settings).prepare(project["id"], "codex", worktree=str(root), timeout=0)
    monkeypatch.setenv("DEVBRAIN_SESSION_ID", session["session_id"])
    refused = runner.invoke(cli.app, argv[4:])
    assert refused.exit_code == 1 and "resume_required" in str(refused.exception)
    assert len(store.query("SELECT FROM check_runs WHERE task_id=:t", {"t": task["id"]})) == 1
