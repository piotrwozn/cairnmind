"""A notice is stored for the project it is about, and a stored name or folder cannot forge an entry (#130)."""
from __future__ import annotations

import time

import pytest

from cairnmind.terminal_notices import NoticeHistory
from cairnmind.terminal_panels import SidebarApp
from cairnmind.terminal_ui import FormModal, MessageModal
from test_terminal_panels import settle
from test_terminal_ux080 import setup_ui
from test_workspace_stage import answer_stage, decision
from test_workspace_stage_scope import notice_of, wait_until
from test_terminal_notices import clock, open_history  # noqa: F401  (the autouse clock fixture)


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
        await wait_until(pilot, lambda: "refused B" in notice_of(app))
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

    def switch_then_fail():
        workspace.update({"project_id": "project-other", "project_name": "Other project", "worktree": str(other)})
        time.sleep(.7)  # the panel polls the workspace every .3 s
        raise ValueError("The memory service failed for the first project")
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        fake_ask(app, shown, switch_then_fail)
        app.action_ask()
        await wait_until(pilot, lambda: "failed for the first" in notice_of(app))
        assert app.workspace_data["project_id"] == "project-other"
        text = await open_history(app, pilot)
    line = next(line for line in text.splitlines() if "failed for the first" in line)
    assert "[Keyboard project" in line  # not read as a notice of the project now open


@pytest.mark.asyncio
async def test_a_stage_notice_after_a_project_switch_is_labelled_with_the_project_it_was_pressed_for(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    project = broker.projects[0]
    other = tmp_path / "other"
    other.mkdir()
    def switch():
        workspace.update({"project_id": "project-other", "project_name": "Other project", "worktree": str(other)})
        time.sleep(.7)  # the panel polls every .3 s: it shows the other project before the answer comes back
    answer_stage(broker, decision("start_slot", project, slot_id="slot-native", client="codex"), during=switch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.action_next_stage()
        await wait_until(pilot, lambda: "Project changed. Run next stage" in notice_of(app))
        assert not isinstance(app.screen, MessageModal)
        await wait_until(pilot, lambda: app.workspace_data.get("project_id") == "project-other")
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
