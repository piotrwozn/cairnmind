"""The one palette of the terminal UI: tokens, styled text that cannot upgrade trust, tmux chrome."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from cairnmind import terminal_theme as theme
from cairnmind.terminal_i18n import CATALOG, tr

SOURCE = Path(theme.__file__).parent
# Modules whose colours have moved to the theme. The list grows as panels are converted.
THEMED = ["terminal_ui.py", "terminal_panels.py", "workspace.py", "api_worker.py", "terminal_inspector.py",
          "terminal_impact.py", "terminal_detail.py", "terminal_commands.py", "workspace_finding.py",
          "terminal_activity.py", "terminal_activity_data.py", "terminal_notice_feed.py"]
# terminal_graph.py is not listed: its node colours are the graph's own data colours, not chrome.
GREENS = {theme.TOKENS["ok"], f"colour{theme.XTERM['ok']}"}


def test_css_replaces_every_token_and_refuses_an_unknown_one():
    assert "$cm-" not in theme.BASE_CSS
    assert theme.css("a { color: $cm-border-hi; background: $cm-bg 70%; }") == (
        "a { color: " + theme.TOKENS["border-hi"] + "; background: " + theme.TOKENS["bg"] + " 70%; }")
    with pytest.raises(KeyError):
        theme.css("a { color: $cm-nosuch; }")


def test_every_token_has_a_256_colour_cell_and_states_stay_apart_there():
    assert set(theme.TOKENS) == set(theme.XTERM)
    cells = theme.XTERM
    # Structure and state must survive a 256-colour terminal: these may never share a cell.
    for group in (("bg", "panel", "raised", "select", "accent"), ("ok", "warn", "danger"), ("text", "muted", "dim")):
        assert len({cells[name] for name in group}) == len(group), group
    assert {theme.TMUX[name] for name in ("ok", "warn", "danger", "accent")} == {
        cells[name] for name in ("ok", "warn", "danger", "accent")}


def test_a_256_colour_terminal_gets_the_cells_the_table_names():
    from rich.color import Color, ColorSystem
    # Rich picks the cell, so the table must be what it picks: a surface that came out as an ANSI
    # blue or black would change the look on every 256-colour terminal.
    for name, value in theme.TOKENS.items():
        assert Color.parse(value).downgrade(ColorSystem.EIGHT_BIT).number == theme.XTERM[name], name


def test_styled_text_keeps_the_whole_string_and_escapes_it():
    hostile = "ok\x1b[31m‮red"
    text = theme.styled(hostile, "warn")
    assert text.spans == [] and "\x1b" not in text.plain and "‮" not in text.plain
    assert text.plain == "ok\\x1b[31m\\u202ered"
    parts = [("label", "Status: "), ("stored", "do \x1b[2J this"), ("value", "\n"), ("muted", "/tmp/x")]
    joined = theme.assemble(parts)
    assert joined.plain == "Status: do \\x1b[2J this\n/tmp/x"
    assert "\x1b" not in joined.plain


def test_stored_text_is_never_dressed_as_a_label_or_a_state():
    assert theme.style("stored") == theme.style("value")
    assert not theme.style("stored").bold
    span = theme.assemble([("stored", "confirmed")]).spans[0]
    assert span.style == theme.style("value")


def test_no_evidence_state_is_green_and_unknown_is_a_warning():
    anchored = theme.evidence_style("anchored")
    assert anchored == theme.style("value") and not anchored.bold
    for state in ("anchored", "evidence_replaced", "unverified", "degraded", "orphaned", "", "verified", "ok"):
        assert theme.evidence_style(state).color.name not in GREENS, state
    for state in ("evidence_replaced", "unverified", "degraded", "something new"):
        assert theme.evidence_style(state) == theme.style("warn")
    assert theme.evidence_style("orphaned") == theme.style("danger")
    assert theme.origin_style("[machine]") == theme.style("muted")
    assert theme.origin_style("[reported]") == theme.style("warn")
    assert theme.origin_style("[verified]") == theme.style("value")


def test_an_unknown_state_word_is_plain_never_green():
    assert theme.state_style("confirmed") == theme.style("value")
    assert theme.state_style("still valid") == theme.style("value")
    assert theme.state_style("done") == theme.style("ok")
    assert theme.state_style("ACK required") == theme.style("warn")
    assert theme.state_style("failed") == theme.style("danger")
    for word in ("Confirmed", "confirmed!", "ack", "", "trusted"):
        assert theme.state_style(word) == theme.style("value"), word


@pytest.mark.parametrize("name", THEMED)
def test_converted_modules_hold_no_colour_literal(name):
    text = (SOURCE / name).read_text(encoding="utf-8")
    assert re.findall(r"#[0-9a-fA-F]{6}\b", text) == []


def test_dialog_hints_exist_in_both_languages():
    for text in ("Enter: select · Esc: back", "Ctrl+S: save · Esc: cancel", "PgUp/PgDn: scroll · Esc: close",
                 "PgUp/PgDn: scroll · Esc: cancel", "No matching commands."):
        assert text in CATALOG and tr(text, "pl") != text


def test_tmux_pane_badge_is_green_only_for_ack():
    from cairnmind.workspace import tmux_chrome
    options = dict(tmux_chrome())
    border = options["pane-border-format"]
    ok, warn, danger = (f"colour{theme.TMUX[name]}" for name in ("ok", "warn", "danger"))
    assert border.count(ok) == 1
    assert "#{?#{==:#{@cairnmind_memory},ack},#[fg=" + ok + "]," in border
    assert "#{==:#{@cairnmind_memory},unavailable},#[fg=" + danger + "],#[fg=" + warn + "]}" in border
    # The words tmux shows are the raw option values, in the old order.
    shown = re.sub(r"#\[[^]]*\]", "", border)
    assert shown.index("#{@cairnmind_role} ") < shown.index("#{@cairnmind_control} ") < shown.rindex("#{@cairnmind_memory} ")
    assert shown.endswith("#{@cairnmind_memory} ")
    for option, value in options.items():
        assert "'" not in value and "\n" not in value, option  # each is written into one quoted config line


def test_status_rows_read_the_same_without_styles():
    from cairnmind.workspace import workspace_status_lines
    cached = {"profiles": [{"id": "codex", "client": "codex"}],
              "auth": {"codex": {"auth": "reported_signed_in", "ready_to_launch": True}}}
    slot = {"run_id": "run-7f3a91c2", "client": "codex", "profile_id": "codex", "lifecycle": "running",
            "memory": {"status": "ready", "acknowledged": True}}
    first, second = workspace_status_lines({"project_name": "payments-api", "broker_available": True},
                                           cached, slot, "controller")
    strip = lambda row: re.sub(r"#\[[^]]*\]", "", row)  # noqa: E731
    assert strip(first) == " Project:payments-api | Session:codex run-7f3… Running"
    assert strip(second) == (" Memory:ACK ready | Connection:Ready(local) | "
                             "#{?client_readonly,Control:Read only,Control:You}")
    assert "," not in re.sub(r"#\{\?client_readonly,.*", "", second)


def test_keyboard_help_keeps_its_text_when_the_keys_are_set_apart():
    from cairnmind.terminal_commands import help_text
    value = "Ctrl+B, then n: save a finding (Ctrl+S: saves)\n\nNo keys on this line.\nGraph: [ / ] select"
    shown = help_text(value)
    assert shown.plain == value
    accents = [shown.plain[span.start:span.end] for span in shown.spans if span.style == theme.style("accent")]
    assert accents == ["Ctrl+B, then n: ", "Graph: "]
