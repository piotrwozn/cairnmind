"""More → MCP servers: a person adds or removes a project's servers through the settings route (#126)."""
from __future__ import annotations

from pathlib import Path
import re

import pytest
from textual.widgets import Input, Static, TextArea

from cairnmind import cli
from cairnmind.agent_memory import MEMORY_TOOLS
from cairnmind.agent_tools import FILE_TOOLS
from cairnmind.terminal_i18n import CATALOG
from cairnmind.terminal_panels import ConfirmModal, SidebarApp
from cairnmind.terminal_ui import ChoiceModal, FormModal
from cairnmind.mcp_servers import LOADER_ENV
from cairnmind.workspace_mcp import LOADER_NOTE, NOTE, REMOVED, SAVED, SAVED_NOT_RUNNABLE, parse_form
from test_terminal_panels import settle
from test_terminal_ux080 import screen_text, setup_ui
from test_workspace_stage import wait_for

SERVER = {"id": "tracker", "command": "/usr/bin/env", "args": ["tracker-mcp", "--quiet"], "env": {"LOG_LEVEL": "warn"}}


def saves(broker):
    return [body for method, path, _, body in broker.calls if (method, path) == ("PUT", "/api/settings")]


@pytest.mark.asyncio
async def test_adding_a_server_saves_it_for_the_selected_project_only(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    other = [{"id": "other", "command": "/bin/other", "args": [], "env": {}}]
    broker.preferences["project_mcp_servers"] = {"another-project": other}
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.edit_mcp_servers()
        await wait_for(app, pilot, ChoiceModal)
        assert [value for _, value in app.screen.choices] == ["+"]
        await pilot.press("enter")
        await wait_for(app, pilot, FormModal)
        form = app.screen
        form.query_one("#field-id", Input).value = "tracker"
        form.query_one("#field-command", Input).value = "/usr/bin/env"
        form.query_one("#field-args", TextArea).text = "tracker-mcp\n--quiet\n"
        form.query_one("#field-env", TextArea).text = "LOG_LEVEL=warn"
        await pilot.press("ctrl+s")
        await settle(app, pilot)
        notice = screen_text(app)
    # Only this project's list, checked against the list shown; other projects are never sent back.
    assert saves(broker) == [{"project_mcp_server_edit": {"project": "project-keyboard", "expected": [],
                                                          "servers": [SERVER]}}]
    assert SAVED.split(".")[0] in notice


@pytest.mark.asyncio
async def test_a_program_that_cannot_run_here_is_saved_with_a_warning(tmp_path, monkeypatch):
    # #157: a missing program was accepted silently and failed only, briefly, in the next agent run.
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.edit_mcp_servers()
        await wait_for(app, pilot, ChoiceModal)
        await pilot.press("enter")
        await wait_for(app, pilot, FormModal)
        app.screen.query_one("#field-id", Input).value = "time"
        app.screen.query_one("#field-command", Input).value = str(tmp_path / "no-such-mcp-server")
        await pilot.press("ctrl+s")
        await settle(app, pilot)
        line = app.query_one("#panel-notice", Static)
        shown, warned = str(line.render()), line.has_class("-warn")
    assert saves(broker)[0]["project_mcp_server_edit"]["servers"][0]["id"] == "time"
    assert "cannot be run on this computer" in shown and warned
    assert SAVED_NOT_RUNNABLE in CATALOG


@pytest.mark.asyncio
async def test_a_refused_definition_is_shown_and_nothing_is_saved(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.edit_mcp_servers()
        await wait_for(app, pilot, ChoiceModal)
        await pilot.press("enter")
        await wait_for(app, pilot, FormModal)
        form = app.screen
        form.query_one("#field-id", Input).value = "tracker"
        form.query_one("#field-command", Input).value = "/usr/bin/env"
        form.query_one("#field-env", TextArea).text = "GITHUB_TOKEN=ghp_x"
        await pilot.press("ctrl+s")
        await pilot.pause(.2)
        text = str(app.screen.query_one("#form-error", Static).render())
        assert isinstance(app.screen, FormModal)
    assert "A server gets no secrets" in text and saves(broker) == []


@pytest.mark.asyncio
async def test_stored_text_is_shown_escaped_and_removal_needs_a_confirmation(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    hostile = {"id": "tracker", "command": "/usr/bin/env", "args": ["\x1b]0;owned\x07\x1b[2J"], "env": {}}
    broker.preferences["project_mcp_servers"] = {"project-keyboard": [hostile, SERVER | {"id": "second"}]}
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.edit_mcp_servers()
        await wait_for(app, pilot, ChoiceModal)
        listed = screen_text(app)
        await pilot.press("enter")
        await wait_for(app, pilot, ConfirmModal)
        confirm = screen_text(app)
        await pilot.press("escape")
        await settle(app, pilot)
        assert saves(broker) == []
        app.edit_mcp_servers()
        await wait_for(app, pilot, ChoiceModal)
        await pilot.press("enter")
        await wait_for(app, pilot, ConfirmModal)
        app.screen.query_one("#confirm-yes").press()
        await settle(app, pilot)
    for text in (listed, confirm):
        assert "\x1b" not in text and "\x07" not in text and "\\x1b" in text
    assert saves(broker) == [{"project_mcp_server_edit": {"project": "project-keyboard",
                                                          "expected": [hostile, SERVER | {"id": "second"}],
                                                          "servers": [SERVER | {"id": "second"}]}}]
    assert REMOVED in CATALOG


def test_form_parsing_and_every_new_string_has_a_polish_entry():
    assert parse_form({"id": "a", "command": "/bin/a", "args": "x\r\n\n y", "env": "A=1=2\n"}) == {
        "id": "a", "command": "/bin/a", "args": ["x", " y"], "env": {"A": "1=2"}}
    with pytest.raises(ValueError, match="NAME=value"):
        parse_form({"id": "a", "command": "/bin/a", "env": "JUSTNAME"})
    for text in (NOTE, SAVED, REMOVED, LOADER_NOTE, "MCP servers", "Add server", "Add MCP server",
                 "Remove MCP server"):
        assert text in CATALOG, text
    # The form names every variable that is refused.
    for name in LOADER_ENV:
        assert name in LOADER_NOTE or name.startswith("LD_") and "LD_*" in LOADER_NOTE, name
    source = Path(__file__).parents[1] / "src" / "cairnmind" / "mcp_servers.py"
    for message in re.findall(r'raise ValueError\("([^"]+)"\)', source.read_text()):
        assert message in CATALOG, message


def test_no_command_or_agent_tool_can_write_the_server_list():
    names = [row["name"] for row in [*MEMORY_TOOLS, *FILE_TOOLS]]
    assert not any("mcp" in name or "setting" in name or "server" in name for name in names)
    commands = [command.name or command.callback.__name__ for command in cli.app.registered_commands]
    groups = [group.name for group in cli.app.registered_groups]
    assert not any("mcp" in str(name) and "server" in str(name) for name in commands + groups)
    assert "project_mcp_servers" not in Path(cli.__file__).read_text()
