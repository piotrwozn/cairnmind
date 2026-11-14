"""Language findings of the 0.10.0 trial (#153): English source text, Polish translation, nothing mixed."""
import pytest

from cairnmind.terminal_panels import SidebarApp
from cairnmind.terminal_ui import ChoiceModal
from test_terminal_panels import setup, settle


@pytest.mark.asyncio
@pytest.mark.parametrize(("choice", "title"), [
    ("language", "Language"), ("view", "Layout"), ("reduced_motion", "Reduce motion")])
async def test_the_value_dialog_is_titled_after_the_setting_it_changes(tmp_path, monkeypatch, choice, title):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    seen = []

    async def choose(screen):
        seen.append(screen)
        return choice if len(seen) == 1 else None
    monkeypatch.setattr(app, "push_screen_wait", choose)
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        app.action_settings()
        await settle(app, pilot)
    assert [screen.heading for screen in seen if isinstance(screen, ChoiceModal)] == ["Settings", title]
