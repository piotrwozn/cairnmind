"""Steps shared by the tests of a person's acts (#332): reaching a consent dialog past its draw and quiet moment.

Not collected (no test_ prefix). Never call settle() while a dialog is open: the act's worker is parked on it.
"""
from __future__ import annotations

from textual import _time as textual_time, events
from textual.widgets import Button

from cairnmind.terminal_panels import ConfirmModal


async def consent_drawn(app, pilot, seconds=6.0):
    """Wait, bounded, until the top screen is a consent dialog past its draw and quiet moment (#318); return it."""
    for _ in range(int(seconds / .02)):
        await pilot.pause(.02)
        screen = app.screen
        if (isinstance(screen, ConfirmModal) and screen.consent
                and screen.drawn_at + screen.QUIET <= textual_time.get_time()):
            return screen
    raise AssertionError("no consent dialog; the top screen is " + type(app.screen).__name__)


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
