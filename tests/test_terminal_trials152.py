"""Findings of the 0.10.0 pre-release trial in the workspace panels (#152)."""
from __future__ import annotations

import os
import signal
import time

import pytest
from textual.widgets import Static

from cairnmind.terminal.terminal_graph import GraphApp
from cairnmind.terminal.terminal_panels import SidebarApp
from cairnmind.terminal.terminal_i18n import tr
from test_terminal_activity import make as make_activity
from test_terminal_graph import panel as panel  # noqa: F401  (fixture)
from test_terminal_panels import settle
from test_terminal_ux080 import screen_text, setup_ui
from test_trial011_workspace import wait_until
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
        # A key hint is not an event of the project: not in Notices, not copied to the activity column.
        assert not app.notice_history.entries
    for text in (app.LEAVE_KEYS, app.LEAVE_HINT):
        assert tr(text, "pl") != text


@pytest.mark.asyncio
async def test_ctrl_q_never_ends_the_graph(panel):  # noqa: F811
    settings, workspace, broker = panel
    app = GraphApp(settings, broker=broker, workspace=workspace, motion=False)
    async with app.run_test(size=(100, 35)) as pilot:
        await wait_until(pilot, lambda: bool(app.model.nodes) and not app._dirty and not app._loading)
        await pilot.press("ctrl+q")
        await wait_until(pilot, lambda: notice_text(app) == app.LEAVE_KEYS)
        assert app.is_running
        assert notice_text(app) == app.LEAVE_KEYS


@pytest.mark.asyncio
async def test_a_leave_hint_does_not_hide_a_notice_from_the_mark(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app.notice("Assistant started.")
        await pilot.press("ctrl+c")
        await settle(app, pilot)
        assert [entry.text for entry in app.notice_history.entries] == ["Assistant started."]
        assert app.notice_history.replaced_unseen() == 1  # the hint took its place on the line


AGENT_LEAVE = ("Ctrl+Q does not end the agent. Ctrl+B, then d: leave the workspace; it keeps running. "
               "Ctrl+C interrupts a turn.")


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_ctrl_q_never_ends_the_api_agent(tmp_path, monkeypatch, language):
    from test_api_worker import make_app, settle as settle_agent
    from cairnmind.terminal.terminal_agent_i18n import tr as agent_tr
    monkeypatch.setattr("cairnmind.agents.api_worker.language_for", lambda settings: language)
    app = make_app(tmp_path)
    async with app.run_test(size=(95, 42)) as pilot:
        await settle_agent(app, pilot)
        await pilot.press("ctrl+q")
        await settle_agent(app, pilot)
        assert app.is_running and app.return_value is None
        assert [n.message for n in app._notifications] == [agent_tr(AGENT_LEAVE, language)]
    assert agent_tr(AGENT_LEAVE, "pl") != AGENT_LEAVE


@pytest.mark.asyncio
@pytest.mark.parametrize("started_for_it", [True, False])
async def test_a_view_started_again_by_a_menu_request_runs_it(tmp_path, monkeypatch, started_for_it):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    # WorkspaceManager.request_command writes the request first, then starts the dead view with --run-request.
    workspace.value["command_request"] = {"id": "before-start", "action": "search", "target_role": "sidebar"}
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    app.run_start_request = started_for_it
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        await pilot.pause(.4)  # a few polls: the request runs once, or never for an ordinary start
        assert (app.focused is not None and app.focused.id == "project-search") is started_for_it
        assert app._last_command_id == "before-start" and not app.run_start_request


def test_the_run_request_flag_reaches_the_view(tmp_path, monkeypatch):
    import sys
    from cairnmind.terminal import terminal_panels
    from cairnmind.config import Settings
    Settings.load(tmp_path / "data", create=True)
    seen = []
    monkeypatch.setattr(terminal_panels.SidebarApp, "run", lambda self: seen.append(self.run_start_request))
    for extra in (["--run-request"], []):
        monkeypatch.setattr(sys, "argv", ["panel", "--kind", "sidebar", "--data-dir", str(tmp_path / "data"), *extra])
        terminal_panels.main()
    assert seen == [True, False]


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


@pytest.mark.asyncio
@pytest.mark.parametrize("language,typed,expected", [("en", "Ask", "ask"), ("en", "brief", "brief"),
                                                      ("pl", "Zapytaj", "ask"), ("en", "task", "inspector")])
async def test_menu_lists_commands_whose_name_matches_first(tmp_path, language, typed, expected):
    from types import SimpleNamespace
    from cairnmind.terminal.cockpit_state import CockpitState
    from cairnmind.config import Settings
    from cairnmind.terminal.terminal_commands import COMMANDS, CommandPaletteApp
    settings = Settings.load(tmp_path / "data", create=True)
    CockpitState(settings).update({"language": language})
    app = CommandPaletteApp(SimpleNamespace(settings=settings))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press(*typed)
        await pilot.pause()
        names = [row[0] for row in app.rows]
        await pilot.press("enter")
    assert app.return_value[3] == expected, names
    if typed == "task":  # only descriptions match: the menu keeps its order
        assert names == [row[0] for row in COMMANDS if "task" in (row[0] + " " + row[1]).casefold()]


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_menu_shows_a_description_whole_or_marks_the_cut_at_80_columns(tmp_path, language):
    from types import SimpleNamespace
    from cairnmind.terminal.cockpit_state import CockpitState
    from cairnmind.config import Settings
    from cairnmind.terminal.terminal_commands import COMMANDS, CommandPaletteApp
    settings = Settings.load(tmp_path / "data", create=True)
    CockpitState(settings).update({"language": language})
    app = CommandPaletteApp(SimpleNamespace(settings=settings))
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.pause()
        await pilot.pause()
        screen = screen_text(app)
    whole = " ".join(screen.replace("▏", " ").split())  # a wrapped description continues on the next line
    for name, description, *_ in COMMANDS:
        name, description = tr(name, language), tr(description, language)
        line = next(row for row in screen.splitlines() if (" " + name + " ") in row)
        if " ".join(description.split()) in whole:
            continue
        shown = line.rstrip().rsplit("  ", 1)[-1].strip()
        assert shown.endswith("…"), line
        assert description.startswith(shown[:-1].rstrip()), line


@pytest.mark.asyncio
@pytest.mark.parametrize("container", ["cm-stack", "narrow"])
async def test_focus_on_a_stacked_primary_button_is_as_clear_as_on_the_others(container):
    from textual.app import App
    from textual.containers import Horizontal, Vertical
    from textual.widgets import Button
    from cairnmind.terminal import terminal_theme

    class Stack(App):
        CSS = terminal_theme.BASE_CSS

        def compose(self):
            with Vertical(classes="cm-dialog"):
                with Horizontal(classes="cm-stack" if container == "cm-stack" else "actions"):
                    yield Button("Add folder", id="primary", variant="primary")
                    yield Button("Open", id="other")

    app = Stack()
    async with app.run_test(size=(30 if container == "narrow" else 80, 24)) as pilot:
        terminal_theme.size_classes(app)
        await pilot.pause()

        def look(identifier):
            styles = app.query_one("#" + identifier).styles
            return styles.background, styles.color, styles.border_left

        app.query_one("#other").focus()
        await pilot.pause()
        primary_idle, other_focused = look("primary"), look("other")
        app.query_one("#primary").focus()
        await pilot.pause()
        primary_focused, other_idle = look("primary"), look("other")
    assert other_focused[0] != other_idle[0]  # the reference: focus changes the fill
    assert primary_focused[0] != primary_idle[0] and primary_focused[1] != primary_idle[1]
    from textual.color import Color
    assert primary_focused[0] != Color.parse(terminal_theme.TOKENS["select"])  # not the selected-row fill


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
@pytest.mark.parametrize("width", [80, 120])
async def test_graph_key_footer_fits_and_hidden_keys_still_work(panel, monkeypatch, language, width):  # noqa: F811
    from textual.widgets._footer import FooterKey
    from cairnmind.terminal.terminal_commands import CommandPaletteApp
    from cairnmind.terminal.terminal_graph import TerminalGraph
    settings, workspace, broker = panel
    monkeypatch.setattr("cairnmind.terminal.terminal_ui.language_for", lambda settings: language)
    app = GraphApp(settings, broker=broker, workspace=workspace, motion=False)
    async with app.run_test(size=(width, 35)) as pilot:
        # Wait for the footer laid out in this language and the graph holding the focus, not for a fixed
        # moment: on a slow machine the footer can still have no keys, or keys with no region, after .3 s.
        def ready():
            keys = list(app.query(FooterKey))
            return bool(keys) and all(key.region.width for key in keys) and app.query_one(TerminalGraph).has_focus \
                and {tr("Zoom in", language), tr("Next node", language)} <= {key.description for key in keys}
        deadline = time.monotonic() + 30
        while not ready() and time.monotonic() < deadline:
            await pilot.pause(.05)
        await pilot.pause()
        keys = list(app.query(FooterKey))
        assert keys and all(key.region.right <= width for key in keys), [(k.key, k.region) for k in keys]
        shown = {key.description for key in keys}
        assert tr("Zoom in", language) in shown and tr("Next node", language) in shown
        bound = {binding.key for _, binding, *_ in app.active_bindings.values()}
        assert {"a", "r", "backspace", "space", "enter", "g"} <= bound  # the keys still work
        assert app.query_one(TerminalGraph).has_focus
    # The keys no longer in the footer are named in the keyboard help.
    from types import SimpleNamespace
    from cairnmind.terminal.cockpit_state import CockpitState
    CockpitState(settings).update({"language": language})
    help_app = CommandPaletteApp(SimpleNamespace(settings=settings), help_only=True)
    async with help_app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        text = " ".join(screen_text(help_app).split())
    assert {"en": "Graph: a switches between all memory and the project; r resets the camera; Backspace goes up.",
            "pl": "Graf: a przełącza między całą pamięcią a projektem; r resetuje kamerę; Backspace przechodzi wyżej.",
            }[language] in text


@pytest.mark.asyncio
async def test_confirmation_and_consent_both_start_on_cancel(tmp_path, monkeypatch):
    from textual.widgets import Button
    from cairnmind.terminal.terminal_panels import ConfirmModal
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    results = []
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app.push_screen(ConfirmModal("Remove from layout?", "This hides the assignment. Existing processes are not stopped.",
                                     approve="Remove"), results.append)
        await pilot.pause()
        await pilot.pause()
        assert isinstance(app.focused, Button) and app.focused.id == "confirm-no"
        await pilot.press("enter")
        await pilot.pause()
        assert results == [False]
        app.push_screen(ConfirmModal("Run a paid model test?", "A real request.", approve="Run", consent=True),
                        results.append)
        await wait_until(pilot, lambda: app.screen.drawn_at < float("inf"))  # an Enter typed before the draw is no answer
        # A consent request starts on Cancel too (#268): a stray Enter refuses, approving needs a deliberate key.
        assert isinstance(app.focused, Button) and app.focused.id == "confirm-no"
        assert "Tab: switch" in str(app.screen.query_one(".cm-hint").render())
        await pilot.press("enter")
        await pilot.pause()
        assert results == [False, False] and not isinstance(app.screen, ConfirmModal)
        app.push_screen(ConfirmModal("Run a paid model test?", "A real request.", approve="Run", consent=True),
                        results.append)
        await wait_until(pilot, lambda: app.screen.drawn_at < float("inf"))
        await pilot.pause(ConfirmModal.QUIET + .1)  # past the quiet moment of a consent dialog (#292)
        await pilot.press("shift+tab", "enter")
        await pilot.pause()
        assert results == [False, False, True]


HELD = 2.0  # seconds the handling of early input is held past the first refresh (#318)


async def shown(pilot, modal):
    """The dialog's buttons are laid out on screen (the first refresh is near)."""
    await wait_until(pilot, lambda: modal.is_mounted and modal.query_one("#confirm-yes").region.area > 0, 10)


def key_of(app, name, at=None):
    from textual import events
    event = events.Key(name, None).set_sender(app)
    if at is not None:
        event.time = at
    return event


@pytest.mark.asyncio
@pytest.mark.parametrize("size,text", [((120, 40), "A real request."), ((60, 24), "word " * 400), ((80, 24), "word " * 400)])
@pytest.mark.parametrize("keys", [("tab", "enter", "tab", "enter"), ("shift+tab", "enter")])
async def test_keys_typed_before_the_first_refresh_never_approve_a_consent_dialog(tmp_path, monkeypatch, size, text, keys):
    from cairnmind.terminal.terminal_panels import ConfirmModal
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    results = []
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        modal = ConfirmModal("Run a command?", text, approve="Run", consent=True)
        app.push_screen(modal, results.append)
        typed = [key_of(app, key) for key in keys]  # produced now, before the dialog is drawn
        await shown(pilot, modal)
        await pilot.pause(HELD)  # their handling is held seconds past the first refresh and the quiet moment
        for key in typed:
            app.post_message(key)
        await pilot.pause()
        assert results == []  # neither approved nor refused: they were typed before the question was shown
        body = modal.query_one(".cm-dialog-body")
        fits = body.virtual_size.height <= body.scrollable_content_region.height
        assert fits == (text == "A real request.")  # the long text really scrolls, so the focus-on-text path is tested
        assert app.focused is (modal.query_one("#confirm-no") if fits else body)  # no early focus move acted
        await wait_until(pilot, lambda: key_of(app, "x").time >= modal.drawn_at + modal.QUIET)
        for _ in range(3):  # a deliberate Tab and Enter, typed later, approve as before
            if app.focused is not None and app.focused.id == "confirm-yes":
                break
            await pilot.press("tab")
        await pilot.press("enter")
        await pilot.pause()
        assert results == [True]


@pytest.mark.asyncio
async def test_keys_made_in_the_quiet_moment_move_nothing_however_late_they_are_handled(tmp_path, monkeypatch):
    from cairnmind.terminal.terminal_panels import ConfirmModal
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    results = []
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        modal = ConfirmModal("Run a command?", "A real request.", approve="Run", consent=True)
        app.push_screen(modal, results.append)
        await shown(pilot, modal)
        await wait_until(pilot, lambda: modal.drawn_at < float("inf"))
        typed = [key_of(app, "shift+tab", modal.drawn_at + .1), key_of(app, "enter", modal.drawn_at + .2)]
        await pilot.pause(HELD)
        for key in typed:
            app.post_message(key)
        await wait_until(pilot, lambda: bool(results))
        assert results == [False]  # the focus move was dropped, so the Enter landed on Cancel and refused at once


@pytest.mark.asyncio
async def test_a_click_made_before_the_dialog_was_drawn_never_approves(tmp_path, monkeypatch):
    from textual import events
    from cairnmind.terminal.terminal_panels import ConfirmModal
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    results = []
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        modal = ConfirmModal("Run a command?", "A real request.", approve="Run", consent=True)
        app.push_screen(modal, results.append)
        early = key_of(app, "x").time
        await shown(pilot, modal)
        await pilot.pause(HELD)
        yes = modal.query_one("#confirm-yes")
        x, y = yes.region.x + yes.region.width // 2, yes.region.y + yes.region.height // 2
        down = events.MouseDown(None, x, y, 0, 0, 1, False, False, False, screen_x=x, screen_y=y)
        up = events.MouseUp(None, x, y, 0, 0, 1, False, False, False, screen_x=x, screen_y=y)
        down.time = up.time = early
        app.post_message(down)
        app.post_message(up)
        await pilot.pause()
        assert results == [] and app.focused.id == "confirm-no"
        await pilot.click("#confirm-yes")
        await pilot.pause()
        assert results == [True]


async def early_key_below(app, pilot, base, modal, results):
    """A shift+tab typed in a widget of the screen below before the dialog came, handled seconds after it is drawn."""
    early = key_of(app, "shift+tab")
    await shown(pilot, modal)
    await pilot.pause(HELD)
    base._forward_event(early)  # exactly what the App does to a key typed in that widget
    await pilot.pause()
    assert app.focused.id == "confirm-no"
    await wait_until(pilot, lambda: key_of(app, "x").time >= modal.drawn_at + modal.QUIET)
    await pilot.press("enter")
    await pilot.pause()
    assert results == [False]


@pytest.mark.asyncio
async def test_a_key_queued_below_a_consent_dialog_never_moves_its_focus(tmp_path, monkeypatch):
    from cairnmind.terminal.terminal_panels import ConfirmModal
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    results = []
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        base = app.query_one("#project-rows")
        modal = ConfirmModal("Run a command?", "A real request.", approve="Run", consent=True)
        app.push_screen(modal, results.append)
        await early_key_below(app, pilot, base, modal, results)


@pytest.mark.asyncio
async def test_a_key_typed_in_the_agent_prompt_before_an_approval_never_moves_its_focus(tmp_path):
    from cairnmind.terminal.terminal_panels import ConfirmModal
    from test_api_worker import make_app, settle as settle_agent
    app = make_app(tmp_path)
    results = []
    async with app.run_test(size=(95, 42)) as pilot:
        await settle_agent(app, pilot)
        base = app.query_one("#agent-prompt")
        modal = ConfirmModal("Approve one command", "echo hi", approve="Run once", consent=True)
        app.push_screen(modal, results.append)
        await early_key_below(app, pilot, base, modal, results)


@pytest.mark.asyncio
async def test_escape_refuses_a_consent_dialog_at_once_even_before_it_is_drawn(tmp_path, monkeypatch):
    from cairnmind.terminal.terminal_panels import ConfirmModal
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    results = []
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app.push_screen(ConfirmModal("Run a command?", "A real request.", approve="Run", consent=True), results.append)
        app.post_message(key_of(app, "escape"))
        await wait_until(pilot, lambda: results)
        assert results == [False]
    assert ConfirmModal("Remove?", "x").admits(key_of(app, "enter"))  # a plain confirmation takes every key


@pytest.mark.asyncio
async def test_input_reaching_a_consent_dialog_before_it_is_mounted_is_refused_without_a_crash(tmp_path, monkeypatch):
    from textual import events
    from cairnmind.terminal.terminal_panels import ConfirmModal
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    results = []
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        base = app.query_one("#project-rows")
        modal = ConfirmModal("Run a command?", "A real request.", approve="Run", consent=True)
        app.push_screen(modal, results.append)  # on top at once, mounted later
        assert not modal.is_mounted
        app.post_message(events.MouseDown(None, 5, 5, 0, 0, 1, False, False, False, screen_x=5, screen_y=5))
        base._forward_event(key_of(app, "shift+tab"))
        await shown(pilot, modal)
        await pilot.pause()
        assert app.is_running and results == [] and app.focused.id == "confirm-no"


@pytest.mark.asyncio
async def test_a_click_on_cancel_refuses_at_once_but_a_click_on_approve_waits(tmp_path, monkeypatch):
    from textual import events
    from cairnmind.terminal.terminal_panels import ConfirmModal
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    results = []
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        modal = ConfirmModal("Run a command?", "A real request.", approve="Run", consent=True)
        app.push_screen(modal, results.append)
        await shown(pilot, modal)
        await wait_until(pilot, lambda: modal.drawn_at < float("inf"))

        def press(button, kind):
            x, y = button.region.x + button.region.width // 2, button.region.y + button.region.height // 2
            event = kind(None, x, y, 0, 0, 1, False, False, False, screen_x=x, screen_y=y)
            event.time = modal.drawn_at + .1  # inside the quiet moment
            modal._forward_event(event)

        press(modal.query_one("#confirm-yes"), events.MouseDown)
        await pilot.pause()
        assert results == [] and app.focused.id == "confirm-no"
        press(modal.query_one("#confirm-no"), events.MouseDown)
        press(modal.query_one("#confirm-no"), events.Click)
        await pilot.pause()
        assert results == [False]


@pytest.mark.asyncio
async def test_the_dialogs_own_key_guard_holds_in_an_app_without_the_app_hook(tmp_path):
    from textual.app import App
    from cairnmind.terminal.terminal_panels import ConfirmModal
    results = []

    class Host(App):
        pass

    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        modal = ConfirmModal("Run a command?", "A real request.", approve="Run", consent=True)
        app.push_screen(modal, results.append)
        early = [key_of(app, "shift+tab"), key_of(app, "enter")]
        await shown(pilot, modal)
        await pilot.pause(HELD)
        for key in early:
            app.post_message(key)
        await pilot.pause()
        assert results == [] and app.focused.id == "confirm-no"


@pytest.mark.asyncio
async def test_a_stall_before_the_first_refresh_does_not_let_queued_keys_approve(tmp_path, monkeypatch):
    from cairnmind.terminal.terminal_panels import ConfirmModal
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    results = []
    original = ConfirmModal.spare_hint_row
    monkeypatch.setattr(ConfirmModal, "spare_hint_row", lambda self: (time.sleep(.5), original(self))[1])
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app.push_screen(ConfirmModal("Run a command?", "A real request.", approve="Run", consent=True), results.append)
        for key in ("shift+tab", "enter"):  # made now, before the stall: the loop stalls longer than the quiet moment
            app.post_message(key_of(app, key))
        await pilot.pause(1)
        assert results == [] and app.focused.id == "confirm-no"


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_a_resize_keeps_the_focus_on_cancel_of_a_consent_dialog(tmp_path, monkeypatch, language):
    from cairnmind.terminal.terminal_panels import ConfirmModal
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch, language)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app.push_screen(ConfirmModal("Run a command?", "\n".join(f"line {i}" for i in range(20)), approve="Run", consent=True))
        # Drawn first: on a slow runner the first draw came after the resize, and a text that scrolls then takes the
        # focus by design, so the test met another case than the one it names (#336).
        await wait_until(pilot, lambda: getattr(app.screen, "drawn_at", float("inf")) < float("inf"), 10)
        assert app.focused.id == "confirm-no"
        await pilot.resize_terminal(120, 14)
        await pilot.pause(.5)
        assert app.focused is not None and app.focused.id == "confirm-no"

@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
@pytest.mark.parametrize("size,text", [((60, 24), "word " * 400), ((56, 14), "word " * 400), ((100, 14), "A real request."),
                                       ((100, 15), "A real request.")])
async def test_consent_hint_fits_a_narrow_pane_and_focus_never_ends_on_a_text_that_fits(tmp_path, monkeypatch, language, size, text):
    from rich.cells import cell_len
    from cairnmind.terminal.terminal_panels import ConfirmModal
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch, language)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        app.push_screen(ConfirmModal("Run a paid model test?", text, approve="Run", consent=True))
        await wait_until(pilot, lambda: getattr(app.screen, "drawn_at", float("inf")) < float("inf"), 10)
        await pilot.pause()
        hint, body = app.screen.query_one(".cm-hint"), app.screen.query_one(".cm-dialog-body")
        if hint.display:
            assert cell_len(str(hint.render())) <= hint.content_region.width
        if body.virtual_size.height <= body.scrollable_content_region.height:
            assert app.focused.id == "confirm-no"


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_a_menu_command_during_an_open_dialog_says_so_and_changes_nothing(tmp_path, monkeypatch, language):
    from cairnmind.terminal.terminal_panels import ConfirmModal
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch, language)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    results = []
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        page, selections = app.page, list(workspace.selections)
        modal = ConfirmModal("Remove from layout?", "Existing processes are not stopped.", approve="Remove")
        app.push_screen(modal, results.append)
        await pilot.pause()
        workspace.update({"command_request": {"id": "during-dialog", "action": "next_stage", "target_role": "sidebar"}})
        await app._poll_workspace()
        await settle(app, pilot)
        assert app.screen is modal and not results and app.page == page
        assert [n.message for n in app._notifications] == [tr(app.DIALOG_OPEN, language)]
        assert workspace.selections == selections
    assert tr(app.DIALOG_OPEN, "pl") != app.DIALOG_OPEN


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["button", "keys"])
async def test_after_an_agent_reply_typing_goes_to_the_prompt(tmp_path, how):
    from textual.widgets import TextArea
    from test_api_worker import make_app, settle as settle_agent
    app = make_app(tmp_path)
    async with app.run_test(size=(95, 42)) as pilot:
        await settle_agent(app, pilot)
        prompt = app.query_one("#agent-prompt", TextArea)
        assert app.focused is prompt
        await pilot.press(*"first")
        if how == "button":
            app.query_one("#agent-send").focus()
            await pilot.pause()
            await pilot.press("enter")
        else:
            await pilot.press("ctrl+enter")
        for _ in range(3):
            await settle_agent(app, pilot)
        assert app.engine.turns == ["first"]
        assert app.focused is prompt, app.focused
        await pilot.press(*"second")
        assert prompt.text == "second"


@pytest.mark.asyncio
async def test_down_from_the_project_search_goes_into_the_list(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app.open_command("search")  # Projects from the menu, and Ctrl+B p
        await pilot.pause()
        assert app.focused.id == "project-search"
        await pilot.press("down")
        await pilot.pause()
        assert app.focused.id == "project-rows"


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
    from cairnmind.terminal.terminal_preview import PreviewTerminal
    workspace, _, _ = manager
    workspace.create(start_supervisor=False)
    for role, detaches in (("client1", False), ("sidebar", True), ("graph", True)):
        workspace.focus(role)
        terminal = PreviewTerminal(workspace, (120, 40), readonly=False)
        # Named now: macOS revokes the terminal, and this descriptor of it, when the client that controlled it ends.
        tty = os.ttyname(terminal.slave)
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
            workspace.tmux("detach-client", "-t", tty, check=False)
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
    # A menu request is written before the view starts again, and the new process is told to run it.
    started = workspace.tmux("display-message", "-p", "-t", workspace.panes()[role], "#{pane_start_command}").stdout
    assert ("--run-request" in started) is (ask == "projects")
    if ask == "projects":
        assert workspace.state.read()["command_request"]["action"] == "search"
    assert workspace.tmux("display-message", "-p", "-t", workspace.panes()["client1"], "#{pane_pid}").stdout.strip() == client
