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
@pytest.mark.parametrize("client,slot", [("copilot", "slot-github"), ("api", "slot-api")])
async def test_an_agent_stage_is_start_work_for_exactly_the_mapped_assistant(tmp_path, monkeypatch, client, slot):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    project = broker.projects[0]
    asked = answer_stage(broker, decision("start_slot", project, stage="review", slot_id=slot, client=client,
                                          profile_id=client + "-profile", label="Agent", model="tool-model",
                                          credential="configured_unchecked"))
    monkeypatch.setattr(workspace_stage, "run_stage_check", lambda *a: pytest.fail("an agent stage ran a check"))
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.action_next_stage()
        await settle(app, pilot)
        notice = str(app.query_one("#panel-notice", Static).render())
    assert asked == [{"project": project["id"], "worktree": project["root"]}]
    # The same workspace request as Start work for that one assistant (see the keyboard test of
    # test_terminal_ux080): the supervisor posts it once to /api/client-runs, whose launcher
    # for api and copilot is start_agent.
    assert workspace.selections[-1] == (project["id"], project["root"], "work")
    assert workspace.read()["requested_slot_ids"] == [slot] and workspace.read()["focus"] == "client1"
    assert workspace_stage.AGENT_STARTED in notice
    assert not any(path == "/api/client-runs" for _, path, _, _ in broker.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["credential_missing", "credential_unavailable", "runtime_missing"])
async def test_an_agent_that_cannot_start_is_shown_and_nothing_starts(tmp_path, monkeypatch, problem):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    project = broker.projects[0]
    answer_stage(broker, decision("none", project, problem=problem, slot_id="slot-github", client="copilot",
                                  label="GitHub Copilot"))
    monkeypatch.setattr(workspace_stage, "run_stage_check", lambda *a: pytest.fail("ran a check"))
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.action_next_stage()
        await wait_for(app, pilot, MessageModal)
        await pilot.pause()
        text = screen_text(app)
    assert "nothing was started" in " ".join(text.split())
    assert "Map stages" not in text  # The map is right; the assistant's connection needs the person.
    assert all(intent != "work" for _, _, intent in workspace.selections)
    assert workspace.read().get("requested_slot_ids", []) == []


@pytest.mark.asyncio
async def test_a_failing_native_stage_never_falls_back_to_another_assistant(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    project = broker.projects[0]
    answer_stage(broker, decision("start_slot", project, slot_id="slot-native", client="codex"))

    def refused(*args, **kwargs):
        workspace.update({"launch_errors": [{"slot_id": "slot-native", "client": "codex", "code": "client_missing"}]})
        return workspace.read()
    monkeypatch.setattr("cairnmind.workspace.refresh_workspace", refused)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.action_next_stage()
        await settle(app, pilot)
        notice = str(app.query_one("#panel-notice", Static).render())
    assert "The assistant did not start: client_missing" in notice
    # The configured Copilot slot (a paid assistant) is never requested in its place.
    assert workspace.read()["requested_slot_ids"] == ["slot-native"]
    assert [intent for _, _, intent in workspace.selections].count("work") == 1


def test_the_supervisor_posts_exactly_the_requested_agent_slot_once(tmp_path, monkeypatch):
    """The step after Run next stage, shared with Start work: one launch, of the one slot."""
    from cairnmind.workspace import WorkspaceManager
    settings = Settings.load(tmp_path / "data", create=True)
    project = {"id": "project-a", "name": "Project A", "root": str(tmp_path)}
    profiles = [{"id": "codex", "client": "codex", "label": "Codex"},
                {"id": "api-cloud", "client": "api", "label": "Cloud API", "provider": "openai", "model": "gpt-x"},
                {"id": "copilot", "client": "copilot", "label": "GitHub Copilot"}]
    slots = [{"id": "codex", "profile_id": "codex"}, {"id": "cloud", "profile_id": "api-cloud"},
             {"id": "pilot", "profile_id": "copilot"}]
    posted = []

    class Broker:
        def request(self, method, path, *, params=None, json=None, timeout=20):
            if path == "/api/state":
                return {"projects": [project], "runs": [], "profiles": profiles,
                        "settings": {"project_slots": {project["id"]: slots}, "default_slots": []}}
            if (method, path) == ("POST", "/api/client-runs"):
                posted.append(json)
                return {"runs": [], "errors": []}
            raise AssertionError(path)
    manager = WorkspaceManager(settings, broker=Broker())
    for name in ("_reconcile_views", "_layout", "_update_status"):
        monkeypatch.setattr(manager, name, lambda *args: None)
    monkeypatch.setattr(manager, "exists", lambda: True)
    manager.state.select_project(project, intent="work", requested_slot_ids=["cloud"])
    manager.refresh()
    manager.refresh()
    assert [body["slots"] for body in posted] == [[{"id": "cloud", "profile_id": "api-cloud"}]]
    assert posted[0]["project"] == project["id"] and posted[0]["worktree"] == project["root"]


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
    # Every assistant the workspace can start is offered, GitHub Copilot included (#125).
    assert offered == ["slot-native", "slot-github", ":unset"]
    saves = [body for method, path, _, body in broker.calls if (method, path) == ("PUT", "/api/settings")]
    chosen = {"slot": "slot-native", "profile_id": "native-private-1", "client": "codex"}
    assert saves == [{"project_stages": {"project-keyboard": {"review": chosen}}},
                     {"project_stages": {"project-keyboard": {"review": chosen, "test": "unit"}}}]


@pytest.mark.asyncio
async def test_saving_a_stage_again_confirms_the_assistant_its_slot_now_names(tmp_path, monkeypatch):
    from cairnmind.stages import project_routing, resolve_stage
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    # A map saved before #125: the slot only. Its slot holds Copilot, which was never confirmed.
    broker.preferences["project_stages"] = {"project-keyboard": {"review": "slot-github"}}
    step = {"plan": {"id": "plan-1", "version": 1, "title": "P"}, "reason": "needs_review",
            "task": {"id": "task-1", "version": 2, "content": "Review", "metadata": {"state": "needs_review"}}}

    def route():
        preferences = {**broker.preferences, "profiles": broker.profiles}
        return resolve_stage(step, *project_routing(preferences, "project-keyboard"),
                             readiness=lambda profile: {"credential": "not_required", "runtime": "ready"})
    assert route()["problem"] == "assistant_unconfirmed"
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.edit_stages()
        await wait_for(app, pilot, ChoiceModal)
        await pilot.pause()
        listed = screen_text(app)
        await pilot.press("down", "enter")  # review
        await wait_for(app, pilot, ChoiceModal)
        await pilot.press("down", "enter")  # slot-github
        await settle(app, pilot)
    assert "not confirmed; save it to confirm" in " ".join(listed.split())
    assert broker.preferences["project_stages"]["project-keyboard"]["review"] == {
        "slot": "slot-github", "profile_id": "copilot-private-2", "client": "copilot",
        "provider": None, "model": "auto", "base_url": None}
    decision = route()
    assert (decision["action"], decision["problem"], decision["client"]) == ("start_slot", None, "copilot")


@pytest.mark.asyncio
async def test_the_editor_marks_an_agent_stage_whose_model_was_edited_in_place(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    native = {"slot": "slot-native", "profile_id": "native-private-1", "client": "codex"}
    agent = {"slot": "slot-github", "profile_id": "copilot-private-2", "client": "copilot",
             "provider": None, "model": "auto", "base_url": None}
    broker.preferences["project_stages"] = {"project-keyboard": {"implement": native, "review": agent}}

    async def listed():
        app = SidebarApp(settings, broker=broker, workspace=workspace)
        async with app.run_test(size=(120, 35)) as pilot:
            await settle(app, pilot)
            app.edit_stages()
            await wait_for(app, pilot, ChoiceModal)
            await pilot.pause()
            return [label for label, _ in app.screen.choices]
    assert not any("not confirmed" in str(label) for label in await listed())
    broker.profiles = [{**row, "model": "premium-model"} for row in broker.profiles]  # Same ids, edited in place.
    labels = [str(label) for label in await listed()]
    assert "not confirmed" in labels[1] and "not confirmed" not in labels[0]


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
