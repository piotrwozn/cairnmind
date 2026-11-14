"""The keys of the workspace are shown on the border of the active view (#161).

The two status rows replace tmux's own row, so status-right (where the keys were) is never drawn, and the
rows are pinned and full at 80 columns. The border of the active view has room: all keys from a width that
holds them, one key below that, none in a pane too narrow for even that.
"""
import os
import re
import time

import pytest

from cairnmind.cockpit_state import CockpitState
from cairnmind.terminal_i18n import tr
from cairnmind.workspace import HINT_KEYS, HINT_SHORT, border_hint, tmux_chrome
from test_workspace import manager as manager  # noqa: F401

STYLE = re.compile(r"#\[[^]]*\]")


@pytest.mark.parametrize("language", ["en", "pl"])
def test_the_border_names_the_keys_in_the_panel_language(language):
    border = dict(tmux_chrome(language))["pane-border-format"]
    assert border_hint(language) in border and tr(HINT_KEYS, language) in border
    assert tr(HINT_SHORT, language) in border
    other = "pl" if language == "en" else "en"
    assert tr(HINT_KEYS, other) not in border
    assert border.index("#[align=right]") < border.index("#[align=left]") < border.index("#{@cairnmind_role}")


def expanded(workspace, terminal, pane):
    """The border tmux draws for this client and pane, without its styles."""
    value = workspace.tmux("display-message", "-p", "-c", os.ttyname(terminal.slave), "-t", pane,
                           "#{E:pane-border-format}").stdout.rstrip("\n")
    return STYLE.sub("", value)


@pytest.mark.parametrize("language", ["en", "pl"])
@pytest.mark.parametrize(("size", "expected"), [((80, 24), HINT_SHORT), ((180, 55), HINT_KEYS)])
def test_real_tmux_draws_the_keys_on_the_active_border_only(manager, language, size, expected):  # noqa: F811
    from cairnmind.terminal_preview import PreviewTerminal
    workspace, _, project = manager
    CockpitState(workspace.settings).update({"view": "focus", "language": language})
    workspace.create(start_supervisor=False)
    workspace.state.select_project(project)
    workspace.refresh()
    terminal = PreviewTerminal(workspace, size, readonly=False)
    try:
        deadline = time.monotonic() + 5
        while len(workspace.clients()) != 1 and time.monotonic() < deadline:
            time.sleep(.02)
        panes = workspace.panes()
        workspace.focus("client1")
        workspace._update_status(workspace.state.read())
        shown = expanded(workspace, terminal, panes["client1"])
        assert shown.startswith(tr(expected, language) + " ") and "client1" in shown
        assert tr(HINT_SHORT, language) not in expanded(workspace, terminal, panes["sidebar"])
        width = int(workspace.tmux("display-message", "-p", "-t", panes["client1"], "#{pane_width}").stdout)
        assert len(shown) <= width  # the hint and the badge fit the border together
        wanted = tr(expected, language).encode()
        deadline = time.monotonic() + 3
        while wanted not in bytes(terminal.output) and time.monotonic() < deadline:
            time.sleep(.02)
        assert wanted in bytes(terminal.output)  # drawn on the terminal, not only stored in an option
        assert b"client1" in bytes(terminal.output)  # the view's own label is drawn beside it
    finally:
        try:
            workspace.tmux("detach-client", "-t", os.ttyname(terminal.slave), check=False)
            terminal.process.wait(timeout=5)
        finally:
            terminal.close()


def test_a_read_only_terminal_is_told_only_how_to_leave(manager):  # noqa: F811
    from cairnmind.terminal_preview import PreviewTerminal
    workspace, _, project = manager
    CockpitState(workspace.settings).update({"view": "focus"})
    workspace.create(start_supervisor=False)
    workspace.state.select_project(project)
    workspace.refresh()
    writer = PreviewTerminal(workspace, (180, 55), readonly=False)
    observer = PreviewTerminal(workspace, (180, 55), readonly=True)
    try:
        deadline = time.monotonic() + 5
        while len(workspace.clients()) != 2 and time.monotonic() < deadline:
            time.sleep(.02)
        pane = workspace.panes()["sidebar"]
        workspace.focus("sidebar")
        assert expanded(workspace, observer, pane).startswith("Read only · C-b d: detach ")
        assert expanded(workspace, writer, pane).startswith(HINT_KEYS + " ")
    finally:
        for terminal in (observer, writer):
            try:
                workspace.tmux("detach-client", "-t", os.ttyname(terminal.slave), check=False)
                terminal.process.wait(timeout=5)
            finally:
                terminal.close()


def test_the_border_follows_a_language_switch(manager):  # noqa: F811
    workspace, _, project = manager
    workspace.create(start_supervisor=False)
    current = lambda: workspace.tmux("show-options", "-gv", "pane-border-format").stdout  # noqa: E731
    assert tr(HINT_KEYS, "en") in current()
    CockpitState(workspace.settings).update({"language": "pl"})
    workspace._update_status(workspace.state.read())
    assert tr(HINT_KEYS, "pl") in current() and tr(HINT_KEYS, "en") not in current()


@pytest.mark.parametrize(("message", "code"), [
    ("resume_required: plan, task, decision, scope or code changed since the acknowledged packet (a checkpoint "
     "or decision this session saved counts too); call resume_context and ack_resume its new packet", "state_changed"),
    ("resume_required: fetch and acknowledge the current ready resume packet", "packet_changed"),
    ("resume_required: current packet has no actual model ACK", "ack_mismatch"),
    ("resume_required: current project scope cannot be restored (gone)", "scope_unavailable"),
    ("resume_required: another session continued this one", "continuity"),
])
def test_a_stale_ack_is_given_a_fixed_reason(message, code):
    from cairnmind.sessions import STALE_REASONS, stale_reason
    assert stale_reason(message) == code and code in STALE_REASONS


def test_every_stale_reason_has_words_in_both_languages():
    from cairnmind.sessions import STALE_REASONS
    from cairnmind.workspace import STALE_WORDS, stale_badge
    assert set(STALE_WORDS) == set(STALE_REASONS)
    for reason in STALE_REASONS:
        assert stale_badge(reason, "en").startswith("stale: ") and stale_badge(reason, "pl") != stale_badge(reason, "en")
    assert stale_badge(None) == stale_badge("made up") == "stale"


@pytest.mark.parametrize("language", ["en", "pl"])
def test_the_pane_border_says_why_memory_is_stale(manager, language):  # noqa: F811
    # #161: after a turn whose third-party tool result changed memory, the border said "stale" and no more.
    workspace, broker, project = manager
    CockpitState(workspace.settings).update({"language": language})
    workspace.create(start_supervisor=False)
    workspace.state.select_project(project)
    broker.value["runs"] = [{"id": "native-fixture", "generation": 7, "client": "codex", "profile_id": "codex",
        "project_id": project["id"], "worktree": project["root"], "slot_id": "first", "lifecycle": "running",
        "purpose": "coding", "memory": {"status": "ready", "acknowledged": False, "stale": True,
                                        "stale_reason": "state_changed"}}]
    workspace.refresh()
    pane = workspace.panes()["client1"]
    badge = workspace.tmux("show-options", "-pv", "-t", pane, "@cairnmind_memory").stdout.strip()
    assert badge == "stale: " + tr("memory changed after ACK", language)
    # A reason the workspace does not know is never shown: only the plain word.
    broker.value["runs"][0]["memory"]["stale_reason"] = "<script>"
    workspace._last_fetch = 0
    workspace.refresh()
    assert workspace.tmux("show-options", "-pv", "-t", pane, "@cairnmind_memory").stdout.strip() == "stale"
