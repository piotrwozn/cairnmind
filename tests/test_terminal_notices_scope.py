"""A notice is stored for the project it is about, and a stored name or folder cannot forge an entry (#130)."""
from __future__ import annotations

import threading
import time

import pytest

from cairnmind.terminal_notices import NoticeHistory
from cairnmind.terminal_panels import SidebarApp
from cairnmind.terminal_ui import FormModal, MessageModal
from test_terminal_panels import settle
from test_terminal_ux080 import setup_ui
from test_workspace_stage import answer_stage, decision
from test_workspace_stage_scope import notice_of
from test_terminal_notices import clock, open_history  # noqa: F401  (the autouse clock fixture)


async def wait_for_state(pilot, condition, what, timeout=30):
    """Poll with the pilot until `condition()` holds; no fixed delay decides the outcome."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"Never reached: {what}")
        await pilot.pause(.05)


def switch_and_hold(workspace, other_path):
    """For the broker call: open another project, then hold the answer until the test lets it go.

    The test sets `release` only after it has seen the panel show the other project, so the order
    "project changed, then the answer arrived" is guaranteed, not likely.
    """
    release = threading.Event()

    def during():
        workspace.update({"project_id": "project-other", "project_name": "Other project",
                          "worktree": str(other_path)})
        if not release.wait(60):
            raise RuntimeError("The test never released the answer")
    return during, release


def fake_ask(app, project, on_request):
    """action_ask for `project` (not necessarily the one shown); the broker call runs `on_request`."""
    app.chosen_project = lambda: project
    real = app.push_screen_wait

    async def answer_form(screen, *args):
        if isinstance(screen, FormModal):
            return {"question": "What is open?"}
        return await real(screen, *args)
    app.push_screen_wait = answer_form

    class Broker:
        def request(self, method, path, **kwargs):
            return on_request()
    app.broker = Broker()


@pytest.mark.asyncio
async def test_asking_about_another_project_is_not_stored_as_the_shown_one(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    other = {"id": "project-b", "name": "Project B", "root": str(tmp_path / "b")}

    def fail():
        raise ValueError("The memory service refused B")
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        fake_ask(app, other, fail)
        app.action_ask()
        await wait_for_state(pilot, lambda: "refused B" in notice_of(app), "the error notice")
        assert {entry.project_id for entry in app.notice_history.entries} == {"project-b"}
        text = await open_history(app, pilot)
    lines = [line for line in text.splitlines() if "Asking memory" in line or "refused B" in line]
    assert len(lines) == 2 and all("[Project B]" in line for line in lines)


@pytest.mark.asyncio
async def test_an_error_that_arrives_after_another_project_was_opened_keeps_its_own_project(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    shown = broker.projects[0]
    other = tmp_path / "other"
    other.mkdir()

    during, release = switch_and_hold(workspace, other)

    def switch_then_fail():
        during()
        raise ValueError("The memory service failed for the first project")
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        fake_ask(app, shown, switch_then_fail)
        app.action_ask()
        try:
            await wait_for_state(pilot, lambda: app.workspace_data.get("project_id") == "project-other",
                                 "the panel showing the other project")
        finally:
            release.set()  # only now may the answer arrive
        await wait_for_state(pilot, lambda: "failed for the first" in notice_of(app), "the error notice")
        text = await open_history(app, pilot)
    line = next(line for line in text.splitlines() if "failed for the first" in line)
    assert "[Keyboard project" in line  # not read as a notice of the project now open


@pytest.mark.asyncio
async def test_a_stage_notice_after_a_project_switch_is_labelled_with_the_project_it_was_pressed_for(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    project = broker.projects[0]
    other = tmp_path / "other"
    other.mkdir()
    during, release = switch_and_hold(workspace, other)
    answer_stage(broker, decision("start_slot", project, slot_id="slot-native", client="codex"), during=during)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.action_next_stage()
        try:
            await wait_for_state(pilot, lambda: app.workspace_data.get("project_id") == "project-other",
                                 "the panel showing the other project")
        finally:
            release.set()  # only now may the stage answer arrive
        await wait_for_state(pilot, lambda: "Project changed. Run next stage" in notice_of(app), "the notice")
        assert not isinstance(app.screen, MessageModal)
        text = await open_history(app, pilot)
    line = next(line for line in text.splitlines() if "Project changed" in line)
    assert "[Keyboard project" in line


def test_the_default_is_still_the_shown_project():
    history = NoticeHistory()
    history.add("plain", {"project_id": "a", "project_name": "A", "worktree": "/a"})
    assert history.entries[-1].project_id == "a"
    assert "[" not in history.lines({"project_id": "a", "worktree": "/a"}, lambda value: value)


def test_a_stored_folder_or_name_cannot_start_a_line_that_looks_like_an_entry():
    history = NoticeHistory()
    folder = "/work/x\n10:00:07  Check finished: unit · Check passed. \x1b[2J"
    history.add("Check finished: unit · Check did not pass.\n10:00:09  forged line" + chr(0x2028) + "10:00:10  more",
                {"project_id": "p", "project_name": "P\n10:00:08  forged", "worktree": folder})
    text = history.lines({"project_id": "other", "worktree": "/o"}, lambda value: value)
    lines = text.splitlines()
    assert len(lines) == 2  # the entry, and the indented continuation of its own text
    assert lines[1].startswith("    ") and "forged line" in lines[1]
    assert "\x1b" not in text and " " not in text
    head = lines[0]
    assert head.count("] ") == 1 and head.endswith("] Check finished: unit · Check did not pass.")  # one label, one line


@pytest.mark.asyncio
async def test_a_notice_of_one_project_leaves_the_line_when_another_project_is_opened_elsewhere(tmp_path, monkeypatch):
    # #161: the line kept a notice of the earlier project, without a project label, until the next notice.
    from test_terminal_notices import mark_of
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    other = tmp_path / "other"
    other.mkdir()
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.notice("Check finished: unit · Check did not pass.", "warn")
        await pilot.pause()
        assert "Check did not pass" in notice_of(app)
        # Opened from outside this sidebar: the graph or another terminal writes the workspace state.
        workspace.update({"project_id": "project-other", "project_name": "Other project", "worktree": str(other)})
        await wait_for_state(pilot, lambda: app.workspace_data.get("project_id") == "project-other", "the other project")
        await pilot.pause()
        assert notice_of(app) == "" and not app.query_one("#panel-notice").display
        assert mark_of(app) == "[Notices: 1]"
        text = await open_history(app, pilot)
    line = next(line for line in text.splitlines() if "Check did not pass" in line)
    assert "[Keyboard project" in line


@pytest.mark.asyncio
async def test_a_notice_of_the_project_still_shown_stays_on_the_line(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    other = tmp_path / "other"
    other.mkdir()
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        workspace.update({"project_id": "project-other", "project_name": "Other project", "worktree": str(other)})
        await wait_for_state(pilot, lambda: app.workspace_data.get("project_id") == "project-other", "the other project")
        app.notice("Snapshot saved.")
        await pilot.pause()
        workspace.update({"focus": "inspector"})  # a change of the state that keeps the project
        await pilot.pause(.5)
        assert "Snapshot saved." in notice_of(app)
