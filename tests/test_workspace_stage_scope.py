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
        await settle(app, pilot)
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
        for _ in range(100):
            await pilot.pause(.02)
            if started.is_set():
                break
        app.select_project("inspect")  # Start work's exclusive group: the stage worker is cancelled.
        await pilot.pause(.3)
        app.action_next_stage()  # The first check is still running in its thread.
        await pilot.pause(.2)
        assert "A check of an earlier stage is still running." in notice_of(app)
        release.set()
        for _ in range(25):
            await pilot.pause(.02)
        assert not isinstance(app.screen, MessageModal)
    assert len(calls) == 1


def test_every_text_stages_can_show_has_a_polish_entry():
    tree = ast.parse(open(stages.__file__, encoding="utf-8").read())
    raised = [node.args[0].value for node in ast.walk(tree)
              if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "ValueError"
              and node.args and isinstance(node.args[0], ast.Constant)]
    assert raised
    shown = [*raised, *stages._PROBLEMS.values(), *stages._REASONS.values(), *stages.STAGES, stages.FROM_WORKSPACE,
             workspace_stage.PROJECT_CHANGED, workspace_stage.CHECK_MOVED, workspace_stage.ALREADY_RUNNING]
    assert [text for text in shown if text not in CATALOG] == []
