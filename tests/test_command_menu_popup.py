"""The command menu in the real popup of an 80x24 terminal scrolls, and the chosen row stays whole (#169).

The 0.10.0 changelog said every command stays on screen there with no scrollbar. The popup is 90% x 85% of
80x24, that is 72x20, and 70x18 inside its border; the earlier test ran the app at a full 80x24.
Textual only, no tmux: it runs the same on macOS and Linux.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import DataTable

from cairnmind import workspace
from cairnmind.config import Settings
from cairnmind.terminal_commands import COMMANDS, CommandPaletteApp
from cairnmind.terminal_i18n import tr


def test_the_popup_size_this_test_assumes_is_the_one_the_workspace_opens():
    # tmux: -w 90% -h 85% of a 80x24 terminal is 72x20; the border takes one cell on each side.
    assert Path(workspace.__file__).read_text().count('"-w", "90%", "-h", "85%"') == 3
    assert (int(80 * .9) - 2, int(24 * .85) - 2) == (70, 18)


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_the_menu_scrolls_in_the_popup_and_the_chosen_command_shows_its_whole_description(
        tmp_path, monkeypatch, language):
    monkeypatch.setattr("cairnmind.terminal_i18n.language_for", lambda settings: language)
    app = CommandPaletteApp(SimpleNamespace(settings=Settings(data_dir=tmp_path / "data")))
    async with app.run_test(size=(70, 18)) as pilot:
        for _ in range(4):
            await pilot.pause()
        table = app.query_one(DataTable)
        assert table.max_scroll_y > 0 and table.max_scroll_x == 0  # a scrollbar, as the popup is too short
        last = len(COMMANDS) - 1
        await pilot.press(*["down"] * last)
        for _ in range(3):
            await pilot.pause()
        assert table.cursor_row == last and table.scroll_y > 0  # "Notices" is reached by scrolling
        shown = " ".join(str(table.get_row(str(last))[1]).split())
        assert shown == tr(COMMANDS[last][1], language)
