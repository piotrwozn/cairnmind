"""Findings of the 0.10.0 pre-release trial in the workspace panels (#152)."""
from __future__ import annotations

import os
import signal
import time

import pytest
from textual.widgets import Static

from cairnmind.terminal_graph import GraphApp
from cairnmind.terminal_panels import SidebarApp
from cairnmind.terminal_i18n import tr
from test_terminal_activity import make as make_activity
from test_terminal_graph import panel as panel  # noqa: F401  (fixture)
from test_terminal_panels import settle
from test_terminal_ux080 import screen_text, setup_ui
from test_workspace import manager as manager  # noqa: F401  (fixture)


def notice_text(app):
    return str(app.query_one("#panel-notice", Static).render())


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_ctrl_q_and_ctrl_c_never_end_a_sidebar_and_name_the_keys_that_leave(tmp_path, monkeypatch, language):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch, language)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        await pilot.press("ctrl+q")
        await settle(app, pilot)
        assert app.is_running and app.return_value is None
        assert notice_text(app) == tr(app.LEAVE_KEYS, language)
        await pilot.press("ctrl+c")
        await settle(app, pilot)
        assert app.is_running
        shown = notice_text(app)
        assert shown == tr(app.LEAVE_HINT, language)
        assert "Ctrl+Q" in shown and "Ctrl+B" in shown and "ctrl+q to quit" not in screen_text(app)
    for text in (app.LEAVE_KEYS, app.LEAVE_HINT):
        assert tr(text, "pl") != text


@pytest.mark.asyncio
async def test_ctrl_q_never_ends_the_graph(panel):  # noqa: F811
    settings, workspace, broker = panel
    app = GraphApp(settings, broker=broker, workspace=workspace, motion=False)
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+q")
        await pilot.pause(.2)
        assert app.is_running
        assert notice_text(app) == app.LEAVE_KEYS


@pytest.mark.asyncio
async def test_activity_column_stays_and_its_popup_closes(tmp_path, monkeypatch):
    app, *_ = make_activity(tmp_path, monkeypatch)
    async with app.run_test(size=(60, 30)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+q", "ctrl+c")
        await pilot.pause(.2)
        assert app.is_running
        assert [n.message for n in app._notifications] == [app.LEAVE_KEYS, app.LEAVE_HINT]
    popup, *_ = make_activity(tmp_path / "popup", monkeypatch, popup=True)
    async with popup.run_test(size=(60, 30)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+q")
        await pilot.pause(.2)
        assert not popup.is_running


def wait_for(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.02)
    return predicate()


def dead(workspace, role):
    pane = workspace.panes((role,))[role]
    return workspace.tmux("display-message", "-p", "-t", pane, "#{pane_dead}").stdout.strip() == "1"


def test_real_tmux_ctrl_q_in_a_view_detaches_only_and_reaches_an_assistant_pane(manager):  # noqa: F811
    from cairnmind.terminal_preview import PreviewTerminal
    workspace, _, _ = manager
    workspace.create(start_supervisor=False)
    for role, detaches in (("client1", False), ("sidebar", True), ("graph", True)):
        workspace.focus(role)
        terminal = PreviewTerminal(workspace, (120, 40), readonly=False)
        try:
            assert wait_for(lambda: len(workspace.clients()) == 1)
            active = workspace.tmux("display-message", "-p", "#{@cairnmind_role}").stdout.strip()
            assert active == role
            terminal.send(b"\x11")  # Ctrl+Q
            if detaches:
                assert wait_for(lambda: not workspace.clients())
                assert terminal.process.wait(timeout=5) is not None
            else:
                time.sleep(.5)
                assert len(workspace.clients()) == 1  # the key went to the assistant pane instead
            assert not any(dead(workspace, view) for view in ("sidebar", "inspector", "graph"))
        finally:
            workspace.tmux("detach-client", "-t", os.ttyname(terminal.slave), check=False)
            terminal.close()


@pytest.mark.parametrize("ask", ["projects", "graph", "inspector"])
def test_real_tmux_a_dead_view_comes_back_when_its_key_asks_for_it(manager, ask):  # noqa: F811
    workspace, _, _ = manager
    workspace.create(start_supervisor=False)
    role = "sidebar" if ask == "projects" else ask
    before = workspace.tmux("display-message", "-p", "-t", workspace.panes()[role], "#{pane_pid}").stdout.strip()
    client = workspace.tmux("display-message", "-p", "-t", workspace.panes()["client1"], "#{pane_pid}").stdout.strip()
    os.kill(int(before), signal.SIGTERM)
    assert wait_for(lambda: dead(workspace, role))
    if ask == "projects":
        workspace.request_command("search")  # Ctrl+B p, and Projects in the menu
    else:
        workspace.focus(role)  # Ctrl+B g / i, Tab, and the menu
    assert not dead(workspace, role)
    after = workspace.tmux("display-message", "-p", "-t", workspace.panes()[role], "#{pane_pid}").stdout.strip()
    assert after != before
    assert workspace.tmux("display-message", "-p", "-t", workspace.panes()["client1"], "#{pane_pid}").stdout.strip() == client
