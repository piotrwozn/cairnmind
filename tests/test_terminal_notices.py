"""The sidebar keeps the notices it showed, so a replaced one can be read again (#130)."""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta

import pytest
from textual.widgets import Static

from cairnmind import terminal_notices
from cairnmind.terminal_commands import COMMANDS
from cairnmind.terminal_i18n import CATALOG, tr
from cairnmind.terminal_notices import NoticeHistory
from cairnmind.terminal_panels import SidebarApp
from cairnmind.terminal_ui import ChoiceModal, MessageModal
from test_terminal_panels import settle
from test_terminal_ux080 import setup_ui
from test_workspace_state import menu_request
from test_workspace_stage_scope import notice_of, wait_until

CHECK = "Check finished: unit · Check did not pass."
STAGE = "Stage started. The assistant must acknowledge its resume packet."


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    ticks = itertools.count()
    base = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(terminal_notices, "_now", lambda: base + timedelta(seconds=next(ticks)))


def mark_of(app):
    mark = app.query_one("#notice-mark", Static)
    return str(mark.render()) if mark.display else ""


def body_of(app):
    return str(app.screen.query_one("#message-body", Static).render())


async def open_history(app, pilot):
    app.action_notices()
    await wait_until(pilot, lambda: isinstance(app.screen, MessageModal))
    return body_of(app)


async def close(app, pilot):
    await pilot.press("escape")
    await wait_until(pilot, lambda: not isinstance(app.screen, MessageModal))


def menu_ready(app):
    """The More menu is on screen with its rows filled in and the list focused.

    The menu becomes the current screen before its mount step adds the rows, so moving the
    cursor or reading a row earlier works on an empty list (#145). Keys a person presses wait
    in the screen's queue behind that step and are not lost.
    """
    screen = app.screen
    return (isinstance(screen, ChoiceModal) and screen.is_mounted
            and screen.focused is screen.query_one("#choices"))


async def open_more(app, pilot):
    app.action_more()
    await wait_until(pilot, lambda: menu_ready(app))
    return app.screen.query_one("#choices")


@pytest.mark.asyncio
async def test_acceptance_two_notices_in_one_moment_both_stay_readable(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.notice(CHECK)
        app.notice(STAGE)
        for number in range(150):  # many later notices, still below the bound
            app.notice(f"Later notice {number}")
        assert notice_of(app) == "Later notice 149"
        text = await open_history(app, pilot)
        lines = text.splitlines()
        assert lines[0].endswith("Later notice 149")
        assert [line for line in lines if CHECK in line] and [line for line in lines if STAGE in line]
        assert text.index(STAGE) < text.index(CHECK)  # newest first
        check_line = next(line for line in lines if CHECK in line)
        assert check_line[:8].count(":") == 2  # the time
        assert "[" not in check_line  # the project now shown is not repeated on every line
        await close(app, pilot)
        assert not isinstance(app.screen, MessageModal)


@pytest.mark.asyncio
async def test_bounded_by_count_and_by_size_oldest_fall_out():
    history = NoticeHistory(max_entries=5, max_chars=10_000)
    for number in range(8):
        history.add(f"notice {number}", {})
    assert [entry.text for entry in history.entries] == [f"notice {number}" for number in range(3, 8)]
    sized = NoticeHistory(max_entries=100, max_chars=100)
    for number in range(10):
        sized.add(f"{number}" * 30, {})
    assert sum(len(entry.text) for entry in sized.entries) <= 100
    assert sized.entries[-1].text == "9" * 30 and len(sized.entries) == 3
    huge = NoticeHistory()
    huge.add("x" * 100_000, {})
    assert len(huge.entries) == 1 and len(huge.entries[0].text) <= terminal_notices.MAX_ENTRY_CHARS + 1


@pytest.mark.asyncio
async def test_the_default_bound_is_two_hundred(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        for number in range(260):
            app.notice(f"n{number}")
        assert len(app.notice_history.entries) == 200
        assert app.notice_history.entries[0].text == "n60"


@pytest.mark.asyncio
async def test_a_cleared_line_is_not_an_entry_and_a_repeat_is_one_entry_with_a_count(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.notice("")
        before = len(app.notice_history.entries)
        for _ in range(5):
            app.notice("Running check: unit")
        first = app.notice_history.entries[-1].when
        app.notice("Running check: unit")
        app.notice("")
        assert len(app.notice_history.entries) == before + 1
        entry = app.notice_history.entries[-1]
        assert entry.count == 6 and entry.when > first
        text = await open_history(app, pilot)
        assert "Running check: unit (×6)" in text


@pytest.mark.asyncio
async def test_the_mark_appears_when_a_notice_was_replaced_and_clears_on_opening(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.notice(CHECK)
        assert mark_of(app) == ""  # nothing replaced yet: the line shows the only notice
        app.notice(STAGE)
        assert mark_of(app) == "[Notices: 1]"
        assert notice_of(app) == STAGE  # the notice text itself carries no suffix
        app.notice("Third")
        assert mark_of(app) == "[Notices: 2]"
        await open_history(app, pilot)
        assert mark_of(app) == ""
        await close(app, pilot)
        app.notice("Fourth")
        assert mark_of(app) == ""  # "Third" was read in the list; "Fourth" is on the line
        app.notice("Fifth")
        assert mark_of(app) == "[Notices: 1]"  # "Fourth" was replaced after the last look
        app.notice("")
        assert mark_of(app) == "[Notices: 2]"  # clearing the line replaces "Fifth" too


@pytest.mark.asyncio
async def test_escapes_newlines_and_markup_are_shown_literally(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.notice("[bold]path[/bold]\x1b]52;c;secret\x07\rREPLACE‮\nsecond line")
        app.notice("later")
        text = await open_history(app, pilot)
    assert "\x1b" not in text and "\x07" not in text and "\r" not in text and "‮" not in text
    assert "[bold]path[/bold]\\x1b]52;c;secret\\x07\\x0dREPLACE\\u202e" in text
    assert "\n    second line" in text  # a continued line stays under its notice


@pytest.mark.asyncio
async def test_opened_from_the_more_menu_as_its_last_entry(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.notice(CHECK)
        app.notice(STAGE)
        table = await open_more(app, pilot)
        assert app.screen.choices[-1] == ("Notices", "notices")
        last = len(app.screen.choices) - 1
        table.move_cursor(row=last)
        assert table.cursor_row == last
        await pilot.press("enter")
        await wait_until(pilot, lambda: isinstance(app.screen, MessageModal))
        assert CHECK in body_of(app)
        await close(app, pilot)
        assert not isinstance(app.screen, ChoiceModal)  # back where the person was


@pytest.mark.asyncio
async def test_opened_from_the_command_menu_through_the_real_dispatch(tmp_path, monkeypatch):
    assert COMMANDS[-1][3] == "notices" and COMMANDS[-1][2] == "command"
    request = menu_request(tmp_path / "state", COMMANDS[-1])
    assert request["action"] == "notices" and request["target_role"] == "sidebar"
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.notice(CHECK)
        app.notice(STAGE)
        assert app.open_command(request["action"]) is True
        await wait_until(pilot, lambda: isinstance(app.screen, MessageModal))
        assert CHECK in body_of(app)
        await close(app, pilot)


@pytest.mark.asyncio
async def test_a_notice_of_another_project_is_labelled_and_the_current_one_is_not(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.notice(CHECK)
        workspace.update({"project_id": "project-other", "project_name": "Other project", "worktree": str(other)})
        await wait_until(pilot, lambda: app.workspace_data.get("project_id") == "project-other")
        app.notice("Opened here")
        text = await open_history(app, pilot)
        lines = text.splitlines()
        assert "[Keyboard project" in next(line for line in lines if CHECK in line)
        assert "[" not in next(line for line in lines if "Opened here" in line)
        await close(app, pilot)
        workspace.update({"project_id": "project-keyboard", "project_name": "Keyboard project",
                          "worktree": str(tmp_path / "Keyboard project")})
        await wait_until(pilot, lambda: app.workspace_data.get("project_id") == "project-keyboard")
        text = await open_history(app, pilot)
        lines = text.splitlines()
        assert "[Other project" in next(line for line in lines if "Opened here" in line)
        assert "[" not in next(line for line in lines if CHECK in line)


@pytest.mark.asyncio
async def test_a_notice_with_no_project_is_labelled_when_a_project_is_shown(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch, empty=True)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.notice("Nothing open")
        assert "[" not in await open_history(app, pilot)
        await close(app, pilot)
        app.notice_history.entries[-1].project_id = None
        workspace.update({"project_id": "project-x", "project_name": "X", "worktree": str(tmp_path)})
        await wait_until(pilot, lambda: app.workspace_data.get("project_id") == "project-x")
        assert "[No project] Nothing open" in await open_history(app, pilot)


@pytest.mark.asyncio
async def test_the_history_is_polish_and_empty_history_says_so(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch, language="pl")
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        assert await open_history(app, pilot) == "Brak komunikatów."
        await close(app, pilot)
        app.notice("Pierwszy")
        app.notice("Drugi")
        assert mark_of(app) == "[Komunikaty: 1]"
        table = await open_more(app, pilot)
        assert "Komunikaty" in str(table.get_row_at(len(app.screen.choices) - 1))
        await pilot.press("escape")


def test_every_new_string_has_a_polish_entry():
    for text in ("Notices", "No notices yet.", "No project", COMMANDS[-1][1]):
        assert text in CATALOG and tr(text, "pl") != text


def test_nothing_is_written_to_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    history = NoticeHistory()
    history.add("a path /home/x/secret", {"project_id": "p", "project_name": "P", "worktree": "/w"})
    history.lines({}, lambda text: text)
    assert list(tmp_path.iterdir()) == []
    source = open(terminal_notices.__file__, encoding="utf-8").read()
    assert "open(" not in source and "write" not in source and "logging" not in source
