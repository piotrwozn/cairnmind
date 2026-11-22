"""The pane of an ended session says so in a sentence of ours, not tmux's "Pane is dead" (#169).

Real tmux 3.4+ and a fake client (the fixture of test_client_runtime); runs the same on Linux and macOS.
"""
from __future__ import annotations

import time

from cairnmind import terminal_i18n
from cairnmind.client_runtime import ENDED
from test_client_runtime import native as native  # noqa: F401 (fixture)


def ended_pane(runtime, project):
    row = runtime.start(project, "codex", slot_id="slot-native", profile_id="codex")
    session = "cairnmind_" + row["id"]
    deadline = time.monotonic() + 8
    # Input sent before the fake client sets its terminal mode is flushed with it.
    while "NATIVE_READY" not in runtime._tmux("capture-pane", "-p", "-t", session).stdout and time.monotonic() < deadline:
        time.sleep(.05)
    runtime._tmux("send-keys", "-t", session, "quit", "Enter")
    while time.monotonic() < deadline:
        if runtime._tmux("display-message", "-p", "-t", session, "#{pane_dead}").stdout.strip() == "1":
            break
        time.sleep(.05)
    return runtime._tmux("capture-pane", "-p", "-t", session).stdout


def test_an_ended_session_shows_our_sentence_and_how_it_ended(native):
    runtime, project, _ = native
    text = " ".join(ended_pane(runtime, project).split())
    assert "Session ended (exit status 0). Resume it from Sessions." in text
    assert "Pane is dead" not in text


def test_the_sentence_has_polish_and_keeps_tmux_formats_intact():
    polish = terminal_i18n.tr(ENDED, "pl")
    assert polish != ENDED and "Wznów" in polish
    for text in (ENDED, polish):  # the same tmux format variables in both
        assert "#{pane_dead_signal}" in text and "#{pane_dead_status}" in text
