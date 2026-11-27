"""The Sessions and Connections dialogs of the sidebar (#161, #169).

These are pilot tests of the Textual app with a fake broker: no tmux and no terminal is involved, so
they run the same on macOS and Linux.
"""
from __future__ import annotations

from copy import deepcopy

import pytest

from cairnmind.terminal.terminal_panels import SidebarApp
from cairnmind.terminal.terminal_ui import ChoiceModal
from test_terminal_panels import settle
from test_terminal_ux080 import UIBroker, setup_ui
from test_workspace_stage_scope import notice_of

LIST, ACTIONS = "Sessions on this computer", "Session actions"


class RunsBroker(UIBroker):
    runs: list = []

    def request(self, method, path, **kwargs):
        value = super().request(method, path, **kwargs)
        if path == "/api/state":
            value["runs"] = deepcopy(self.runs)
        return value


def run_of(project, lifecycle):
    return {"id": "run-" + lifecycle, "generation": 1, "project_id": project["id"], "worktree": project["root"],
            "client": "codex", "slot_id": "slot-native", "profile_id": "native-private-1", "purpose": "coding",
            "lifecycle": lifecycle}


async def heading(app, pilot, expected):
    for _ in range(100):
        await pilot.pause(.02)
        if isinstance(app.screen, ChoiceModal) and app.screen.is_mounted and app.screen.heading == expected:
            return
    raise AssertionError(f"{expected!r} did not open; the screen is {app.screen!r}")


def ui(tmp_path, monkeypatch, lifecycle="exited", project_id=True):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    broker = RunsBroker(broker.project)
    broker.runs = [run_of(broker.project, lifecycle)]
    if not project_id:
        broker.runs[0]["project_id"] = None  # a sign-in run belongs to no project
    selected = []
    monkeypatch.setattr("cairnmind.terminal.workspace.select_run", lambda *args: selected.append(args[1]))
    return SidebarApp(settings, broker=broker, workspace=workspace), selected


@pytest.mark.asyncio
async def test_esc_in_session_actions_goes_back_to_the_list(tmp_path, monkeypatch):
    app, selected = ui(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.sessions()
        await heading(app, pilot, LIST)
        await pilot.press("enter")
        await heading(app, pilot, ACTIONS)
        await pilot.press("escape")
        await heading(app, pilot, LIST)  # the hint says "Esc: back": the list, not the project screen
        assert [value for _, value in app.screen.choices] == ["run-exited"]
        await pilot.press("escape")
        await settle(app, pilot)
        assert not isinstance(app.screen, ChoiceModal) and selected == []


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle,says_ended", [("exited", True), ("running", False)])
async def test_show_on_an_ended_session_says_that_it_ended(tmp_path, monkeypatch, lifecycle, says_ended):
    app, selected = ui(tmp_path, monkeypatch, lifecycle)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.sessions()
        await heading(app, pilot, LIST)
        await pilot.press("enter")
        await heading(app, pilot, ACTIONS)
        assert app.screen.choices[0][1] == "show"
        await pilot.press("enter")
        await settle(app, pilot)
        shown = notice_of(app)
    assert selected == ["run-" + lifecycle]
    assert ("This session has ended." in shown) is says_ended


@pytest.mark.asyncio
async def test_an_ended_run_without_a_project_is_not_told_to_resume(tmp_path, monkeypatch):
    app, _ = ui(tmp_path, monkeypatch, project_id=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.sessions()
        await heading(app, pilot, LIST)
        await pilot.press("enter")
        await heading(app, pilot, ACTIONS)
        assert [value for _, value in app.screen.choices] == ["show"]
        await pilot.press("enter")
        await settle(app, pilot)
        shown = notice_of(app)
    assert "This session has ended." in shown and "Resume" not in shown


@pytest.mark.asyncio
async def test_the_connections_list_names_a_connection_like_stage_choices_do(tmp_path, monkeypatch):
    # "Codex · codex" and "Claude · claude" repeated the client in lowercase (#169).
    app, _ = ui(tmp_path, monkeypatch)
    app.broker.profiles = [{"id": "a", "label": "Codex", "client": "codex"},
                            {"id": "b", "label": "Claude", "client": "claude"},
                            {"id": "c", "label": "Local", "client": "api", "provider": "ollama", "model": "qwen3.5:9b"}]
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.login()
        await heading(app, pilot, "Connections")
        assert [title for title, _ in app.screen.choices] == [
            "Codex", "Claude (Claude Code)", "Local (Ollama · qwen3.5:9b)", "Add connection"]
