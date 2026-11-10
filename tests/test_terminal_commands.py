from types import SimpleNamespace

import pytest

from devbrain.config import Settings
from devbrain.terminal_commands import COMMANDS, CommandPaletteApp, dispatch_command
from devbrain.terminal_commands import JoinWorkspaceApp


@pytest.mark.asyncio
async def test_palette_search_and_enter_use_keyboard_without_side_effect(tmp_path):
    manager = SimpleNamespace(settings=Settings(data_dir=tmp_path / "data"))
    app = CommandPaletteApp(manager)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press(*"connections")
        await pilot.pause()
        assert len(app.rows) == 1 and app.rows[0][3] == "connections"
        await pilot.press("enter")
    assert app.return_value[3] == "connections"
    assert not manager.settings.data_dir.exists()


@pytest.mark.asyncio
async def test_palette_zero_results_and_escape_never_select_previous_command(tmp_path):
    manager = SimpleNamespace(settings=Settings(data_dir=tmp_path / "data"))
    app = CommandPaletteApp(manager)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press(*"no-such-command-xyz", "enter")
        await pilot.pause()
        assert app.rows == [] and app.is_running
        await pilot.press("escape")
    assert app.return_value is None


def test_dispatch_rejects_unlisted_or_arbitrary_commands():
    calls = []
    manager = SimpleNamespace(request_command=lambda value: calls.append(value))
    command = next(row for row in COMMANDS if row[3] == "connections")
    dispatch_command(manager, command)
    assert calls == ["connections"]
    with pytest.raises(ValueError):
        dispatch_command(manager, ("Unknown", "", "command", "shell"))
    dispatch_command(manager, None)
    assert calls == ["connections"]


@pytest.mark.asyncio
async def test_join_defaults_to_observer_and_requires_explicit_takeover(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    app = JoinWorkspaceApp(settings)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("enter")
    assert app.return_value == "observe"
    app = JoinWorkspaceApp(settings)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("tab", "enter")
    assert app.return_value == "takeover"
    assert not settings.data_dir.exists()


def test_palette_offers_ask_memory_and_routes_it_by_name():
    calls = []
    manager = SimpleNamespace(request_command=lambda value: calls.append(value))
    command = next(row for row in COMMANDS if row[3] == "ask")
    assert command[0] == "Ask memory" and command[2] == "command"
    dispatch_command(manager, command)
    assert calls == ["ask"]


def test_ask_summary_shows_answer_quotes_missing_evidence_and_coverage():
    from devbrain.terminal_commands import ask_summary
    text = ask_summary({
        "status": "partial", "answer": "Sessions last one hour.",
        "sources": [{"uri": "file://src/auth.py", "line_start": 3, "line_end": 4,
                     "quote": "SESSION_TTL =\n  3600"}],
        "unknowns": ["No decision explains the refresh window."],
        "coverage": {"answer_terms": 3, "terms_in_quotes": 1, "ratio": 0.333},
    }, lambda value: value)
    assert "Status: partial" in text and "Sessions last one hour." in text
    assert '- file://src/auth.py (3-4): "SESSION_TTL = 3600"' in text
    assert "Missing evidence:\n- No decision explains the refresh window." in text
    assert "Answer words found in quotes: 1/3" in text
    assert ask_summary(None, lambda value: value) == "The memory service returned no answer."
