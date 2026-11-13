"""Run next stage never restarts a live assistant and never acts for a project no longer shown."""
from __future__ import annotations

import ast
import json
import subprocess
import threading

import pytest
from textual.widgets import Static

from cairnmind import stages, workspace_stage
from cairnmind.terminal_i18n import CATALOG
from cairnmind.terminal_panels import SidebarApp
from cairnmind.terminal_ui import MessageModal
from test_terminal_panels import settle
from test_terminal_ux080 import setup_ui
from test_workspace_stage import answer_stage, decision


def notice_of(app):
    return str(app.query_one("#panel-notice", Static).render())


def live_run(project, lifecycle):
    return {"id": "run-live", "generation": 1, "project_id": project["id"], "worktree": project["root"],
            "client": "codex", "slot_id": "slot-native", "purpose": "coding", "lifecycle": lifecycle}


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle", ["running", "starting"])
async def test_a_live_mapped_assistant_is_not_restarted_and_gets_no_new_packet(tmp_path, monkeypatch, lifecycle):
    # implement and review mapped to the same slot: the review stage finds the implementing process.
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    project = broker.projects[0]
    answer_stage(broker, decision("start_slot", project, stage="review", slot_id="slot-native", client="codex"),
                 runs=[live_run(project, lifecycle)])
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.action_next_stage()
        await settle(app, pilot)
        notice = notice_of(app)
    assert "already running here. No process was restarted and it received no new resume packet." in notice
    assert all(intent != "work" for _, _, intent in workspace.selections)
    assert workspace.read().get("requested_slot_ids", []) == []


@pytest.mark.asyncio
async def test_an_exited_run_of_the_slot_does_not_block_a_new_stage(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    project = broker.projects[0]
    answer_stage(broker, decision("start_slot", project, slot_id="slot-native", client="codex"),
                 runs=[live_run(project, "exited")])
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.action_next_stage()
        await settle(app, pilot)
    assert workspace.read()["requested_slot_ids"] == ["slot-native"]


@pytest.mark.asyncio
async def test_a_project_switch_during_the_stage_request_drops_the_launch(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    project = broker.projects[0]
    other = tmp_path / "other"
    other.mkdir()
    switch = lambda: workspace.update({"project_id": "project-other", "worktree": str(other)})  # noqa: E731
    answer_stage(broker, decision("start_slot", project, slot_id="slot-native", client="codex"), during=switch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.action_next_stage()
        await settle(app, pilot)
        notice = notice_of(app)
    assert "Project changed. Run next stage was not carried out" in notice
    assert all(intent != "work" for _, _, intent in workspace.selections)


@pytest.mark.asyncio
async def test_a_check_result_is_not_shown_for_another_project(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    project = broker.projects[0]
    answer_stage(broker, decision("run_check", project, stage="test", check_profile="unit"))

    def runner(argv, **kwargs):
        workspace.update({"project_id": "project-other", "worktree": "/elsewhere"})
        run = {"id": "run-1", "passed": True, "outcome": "completed", "returncode": 0, "reasons": []}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(run), stderr="")
    real = workspace_stage.run_stage_check
    monkeypatch.setattr(workspace_stage, "run_stage_check", lambda s, d: real(s, d, runner=runner))
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.action_next_stage()
        for _ in range(50):  # Polling, not settle: a wrongly shown result would hold the worker open.
            await pilot.pause(.02)
            if isinstance(app.screen, MessageModal) or "not shown here" in notice_of(app):
                break
        assert not isinstance(app.screen, MessageModal)
        notice = notice_of(app)
    assert "recorded for the earlier project and is not shown here" in notice


@pytest.mark.asyncio
async def test_choosing_a_project_cancels_a_running_check_stage_and_blocks_a_second_check(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    project = broker.projects[0]
    answer_stage(broker, decision("run_check", project, stage="test", check_profile="unit"))
    started, release, calls = threading.Event(), threading.Event(), []

    def runner(argv, **kwargs):
        calls.append(argv)
        started.set()
        release.wait(10)
        run = {"id": "run-1", "passed": True, "outcome": "completed", "returncode": 0, "reasons": []}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(run), stderr="")
    real = workspace_stage.run_stage_check
    monkeypatch.setattr(workspace_stage, "run_stage_check", lambda s, d: real(s, d, runner=runner))
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.action_next_stage()
        await wait_until(pilot, started.is_set)
        app.action_inspect()  # Start work's exclusive group: the stage worker is cancelled.
        await pilot.pause(.3)
        workspace.update({"project_id": "project-other", "worktree": "/elsewhere"})  # Then another project opens.
        release.set()
        for _ in range(25):
            await pilot.pause(.02)
        assert not isinstance(app.screen, MessageModal)
        assert "Check finished" not in notice_of(app)
    assert len(calls) == 1


async def wait_until(pilot, condition):
    for _ in range(150):
        await pilot.pause(.02)
        if condition():
            return
    raise AssertionError("condition not reached")


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["failed", "oserror"])
async def test_a_click_in_the_same_project_keeps_the_check_result_visible(tmp_path, monkeypatch, ending):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    project = broker.projects[0]
    stage = decision("run_check", project, stage="test", check_profile="unit")
    answer_stage(broker, stage)
    started, release, calls = threading.Event(), threading.Event(), []

    def runner(argv, **kwargs):
        calls.append(argv)
        started.set()
        release.wait(10)
        if ending == "oserror":
            raise OSError("cannot start the check runner")
        run = {"id": "run-1", "passed": False, "outcome": "completed", "returncode": 1, "reasons": []}
        return subprocess.CompletedProcess(argv, 1, stdout=json.dumps(run), stderr="")
    real = workspace_stage.run_stage_check
    monkeypatch.setattr(workspace_stage, "run_stage_check", lambda s, d: real(s, d, runner=runner))
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    shown, notice = [], app.notice
    app.notice = lambda value: (shown.append(str(value)), notice(value))[1]
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.action_next_stage()
        await wait_until(pilot, started.is_set)
        app.select_project("inspect")  # Same project: cancels the worker, not the check.
        await pilot.pause(.3)
        assert not any("Project changed" in text for text in shown)
        assert any(workspace_stage.CHECK_CONTINUES in text for text in shown)
        app.action_next_stage()  # Another check stage is refused and names the running one.
        await wait_until(pilot, lambda: "A check is still running" in notice_of(app))
        assert "unit · Keyboard project · task-1" in notice_of(app)
        # An assistant stage is not held back by a running check.
        stage.update(action="start_slot", stage="implement", slot_id="slot-native", client="codex", check_profile=None)
        app.action_next_stage()
        await wait_until(pilot, lambda: workspace.read().get("requested_slot_ids") == ["slot-native"])
        release.set()
        expected = ("The check could not run: unit · cannot start the check runner" if ending == "oserror"
                    else "Check finished: unit · Check did not pass.")
        # Read what was shown, in order, not the panel at one instant: the assistant stage's own
        # notice may arrive just after the check result and replace it on the single notice line.
        started_notice = "Stage started. The assistant must acknowledge its resume packet."
        await wait_until(pilot, lambda: any(expected in text for text in shown)
                         and any(started_notice in text for text in shown))
        assert not isinstance(app.screen, MessageModal)
    refused = next(i for i, text in enumerate(shown) if "A check is still running" in text)
    delivered = [i for i, text in enumerate(shown) if expected in text]
    assert len(delivered) == 1 and delivered[0] > refused
    assert not any("Project changed" in text for text in shown)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_new_entries_do_not_move_existing_menu_positions(tmp_path, monkeypatch):
    # Keyboard paths (and tests) reach existing entries by position; new ones are appended.
    from cairnmind.terminal_commands import COMMANDS
    from cairnmind.terminal_ui import ChoiceModal
    assert [row[3] for row in COMMANDS] == [
        "search", "client1", "client2", "inspector", "ask", "brief", "graph", "search", "add_project", "start_work",
        "connections", "sessions", "more", "settings", "focus", "cockpit", "", "next_stage", "notices"]
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.action_more()
        await wait_until(pilot, lambda: isinstance(app.screen, ChoiceModal))
        assert [value for _, value in app.screen.choices] == [
            "sessions", "assistants", "scan", "queue", "snapshot", "import", "settings", "details", "stages", "notices"]
        await pilot.press("escape")
        buttons = [button.id for button in app.query("#project-page Button")]
    assert buttons == ["project-back", "assistant-select", "project-work", "memory-open", "assistant-add", "stage-next"]


def test_every_text_stages_can_show_has_a_polish_entry():
    tree = ast.parse(open(stages.__file__, encoding="utf-8").read())
    raised = [node.args[0].value for node in ast.walk(tree)
              if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "ValueError"
              and node.args and isinstance(node.args[0], ast.Constant)]
    assert raised
    shown = [*raised, *stages._PROBLEMS.values(), *stages._REASONS.values(), *stages.STAGES, stages.FROM_WORKSPACE,
             workspace_stage.PROJECT_CHANGED, workspace_stage.CHECK_MOVED, workspace_stage.ALREADY_RUNNING,
             workspace_stage.CHECK_CONTINUES, "A check is still running", "The check could not run", "Check finished"]
    assert [text for text in shown if text not in CATALOG] == []
