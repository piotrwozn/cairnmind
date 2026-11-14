"""The activity panel's pure functions, strings and keys (#144); rendering is in test_terminal_activity."""
from __future__ import annotations

from rich.cells import cell_len

from cairnmind import terminal_activity
from cairnmind.terminal_activity import (ActivityApp, ActivityPopup, finished_plans, fit, group_tasks, plan_progress,
                                         running_rows)
from cairnmind.terminal_i18n import CATALOG, tr
from cairnmind.terminal_ui import BasePanel
from test_terminal_activity import PLANS, TASKS, task


def test_tasks_are_grouped_by_state_newest_first_with_caps():
    groups = {row["state"]: row for row in group_tasks(TASKS)}
    assert list(groups) == ["in_progress", "needs_review", "needs_test", "todo", "done"]  # no Blocked when empty
    assert [t["id"] for t in groups["in_progress"]["items"]] == ["t1"]  # the superseded t14 is dropped
    assert [t["id"] for t in groups["needs_review"]["items"]] == ["t2", "t3"]
    assert groups["todo"]["total"] == 5 and [t["id"] for t in groups["todo"]["items"]] == ["t5", "t6", "t7"]
    assert groups["todo"]["hidden"] == 2  # a missing state counts as To do
    assert [t["id"] for t in groups["done"]["items"]] == ["t9", "t10", "t11"] and groups["done"]["hidden"] == 1
    blocked = group_tasks([task(f"b{i}", "blocked") for i in range(3)], small=True)
    assert [(g["state"], g["total"], len(g["items"]), g["hidden"]) for g in blocked][3] == ("blocked", 3, 2, 1)
    assert all(len(g["items"]) <= 2 for g in group_tasks(TASKS, small=True))


def test_plan_progress_counts_only_the_active_plan():
    assert plan_progress(TASKS, "plan-a") == (1, 6)
    assert plan_progress(TASKS, "plan-old") == (2, 2)
    assert plan_progress(TASKS, None) == (0, 0)


def test_finished_plans_are_fully_done_other_plans_newest_first_at_most_three():
    assert [(row["id"], row["title"], row["date"], row["count"]) for row in finished_plans(TASKS, PLANS, "plan-a")] == [
        ("plan-old", "Notice history", "11-12", 2)]
    assert finished_plans(TASKS, PLANS, "plan-old") == []
    many = [task(f"x{i}", "done", f"2026-11-0{i}T00:00:00", f"plan-{i}") for i in range(1, 6)]
    assert [row["id"] for row in finished_plans(many, [], None)] == ["plan-5", "plan-4", "plan-3"]


def test_session_words_come_from_a_fixed_set():
    slots = [{"client": "claude", "run_id": "r", "lifecycle": "running"}, {"client": "codex", "run_id": "r",
             "lifecycle": "[bold]hacked"}, {"client": "x", "lifecycle": "running"}, "junk",
             {"client": "\x1b[31mred", "run_id": "r", "lifecycle": "exited"}]
    assert [(word, live) for _, word, live in running_rows(slots)] == [
        ("Running", True), ("None", False), ("Ended", False)]  # a slot never started is left out
    assert "\x1b" not in running_rows(slots)[-1][0]


def test_fit_cuts_by_cell_width_with_an_ellipsis():
    assert fit("short", 10) == "short"
    assert fit("abcdefghijkl", 6) == "abcde…"
    wide = fit("日本語のタスク名です", 7)
    assert cell_len(wide) <= 7 and wide.endswith("…")
    assert fit("anything", 0) == ""


def test_every_new_string_has_a_polish_entry():
    for text in ("Activity", "Running now", "In progress", "Needs review", "Needs test", "Blocked", "To do",
                 "Done recently", "Plans finished recently", "{done} of {total} steps done", "Stage: {stage}",
                 "Showing {shown} of {total} tasks.", terminal_activity.KEYS, terminal_activity.KEYS_SHORT,
                 "{client} running", "warning", "error", "+ {count} more", "a / Esc: close", "Loading…",
                 "Choose a project in the sidebar to see its activity.", "No assistant is running.",
                 "No active plan.", "This plan has no steps yet.", "No tasks in this project yet.",
                 "No notices since the workspace opened.", "Memory service unavailable; retrying."):
        assert text in CATALOG and tr(text, "pl") != text, text
    assert tr("info", "pl") == "info"
    help_text = next(key for key in CATALOG if key.startswith("Ctrl+B, then Space"))
    assert "Ctrl+B, then a: activity panel" in help_text and "Ctrl+B, potem a: panel aktywności" in CATALOG[help_text]


def test_panel_keys_do_not_collide_with_the_base_panel():
    base = {binding[0] if isinstance(binding, tuple) else binding.key for binding in BasePanel.BINDINGS}
    own = {binding[0] for binding in ActivityApp.BINDINGS}
    assert not own & base
    assert {binding[0] for binding in ActivityPopup.BINDINGS} == {"r", "escape", "q", "a"}
