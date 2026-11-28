"""Steps shared by the tests of a person's acts (#332): reaching a consent dialog past its draw and quiet moment.

Not collected (no test_ prefix). Never call settle() while a dialog is open: the act's worker is parked on it.
"""
from __future__ import annotations

from textual import _time as textual_time, events
from textual.css.query import NoMatches
from textual.widgets import Button

from cairnmind.terminal_panels import ConfirmModal
from cairnmind.terminal_ui import FormModal


async def consent_drawn(app, pilot, seconds=6.0):
    """Wait, bounded, until the top screen is a consent dialog past its draw and quiet moment (#318) whose approval
    button is laid out on the screen (a click before that lands at (0, 0), #352); return it."""
    for _ in range(int(seconds / .02)):
        await pilot.pause(.02)
        screen = app.screen
        if (isinstance(screen, ConfirmModal) and screen.consent
                and screen.drawn_at + screen.QUIET <= textual_time.get_time()):
            try:
                button = screen.query_one("#confirm-yes", Button)
            except NoMatches:
                continue
            if button.region.area and button.region in screen.size.region:
                return screen
    raise AssertionError("no consent dialog; the top screen is " + type(app.screen).__name__ + form_state(app.screen))


def form_state(screen):
    """What a form on top shows, so a timeout names its cause (#352): the error line, `saving`, the submit button."""
    if not isinstance(screen, FormModal):
        return ""
    try:
        button = screen.query_one("#form-submit", Button)
        error = str(screen.query_one("#form-error").render())
    except NoMatches:
        return f" (not composed yet, saving={screen.saving})"
    return f" (error line {error!r}, saving={screen.saving}, submit region={button.region}, disabled={button.disabled})"


async def form_ready(app, pilot, seconds=6.0):
    """Wait, bounded, until the top screen is a form whose submit button is laid out on the screen and enabled.
    A click before that lands at (0, 0) and nothing says so (#352); return the form."""
    for _ in range(int(seconds / .02)):
        await pilot.pause(.02)
        screen = app.screen
        if isinstance(screen, FormModal) and not screen.saving:
            try:
                button = screen.query_one("#form-submit", Button)
            except NoMatches:
                continue
            if button.region.area and not button.disabled and button.region in screen.size.region:
                return screen
    raise AssertionError("no laid-out form; the top screen is " + type(app.screen).__name__ + form_state(app.screen))


async def early_enter_sends_nothing(app, pilot, modal):
    """The worst case: the approval button has the focus and an Enter made before the dialog was drawn is
    handled now. It must neither approve nor cancel."""
    modal.set_focus(modal.query_one("#confirm-yes", Button))
    early = events.Key("enter", None)
    early.time = modal.drawn_at - 1
    assert not modal.admits(early)
    app.post_message(early)
    await pilot.pause()
    await pilot.pause()
    assert app.screen is modal
