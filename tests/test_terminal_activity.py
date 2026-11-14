"""The activity panel shows notices, plans, tasks and running sessions, read-only (#144)."""
from __future__ import annotations

import asyncio
import threading
from copy import deepcopy
from datetime import datetime

import pytest
from rich.cells import cell_len
from rich.style import Style

from cairnmind import terminal_activity, terminal_theme
from cairnmind.cockpit_state import _private_directory
from cairnmind.config import Settings
from cairnmind.terminal_activity import (ActivityApp, ActivityList, ActivityPopup, activity_app, finished_plans,
                                         fit, group_tasks, plan_progress, running_rows)
from cairnmind.terminal_i18n import CATALOG, tr
from test_terminal_notice_feed import install, publish
from cairnmind.terminal_notices import NoticeHistory
from cairnmind.terminal_ui import BasePanel

PROJECT = {"id": "project-a", "name": "cairnmind", "root": "/work/cairnmind-144"}
OTHER = {"id": "project-b", "name": "other", "root": "/work/other"}


def task(identifier, state=None, updated="2026-11-10T10:00:00", plan=None, content=None, status="proposed"):
    metadata = {**({"state": state} if state else {}), **({"plan_ref": plan} if plan else {})}
    return {"id": identifier, "kind": "task", "status": status, "content": content or f"Task {identifier}",
            "updated_at": updated, "metadata": metadata, "evidence_state": "anchored"}


TASKS = [
    task("t1", "in_progress", "2026-11-13T10:00:00", "plan-a", "Render the notices section"),
    task("t2", "needs_review", "2026-11-13T09:00:00", "plan-a", "Wire journal events into the activity panel"),
    task("t3", "needs_review", "2026-11-12T09:00:00", "plan-a", "Add Polish strings"),
    task("t4", "needs_test", "2026-11-12T08:00:00", "plan-a", "Popup at 80x24"),
    task("t5", "todo", "2026-11-11T08:00:00", "plan-a", "Narrow-terminal threshold"),
    task("t6", None, "2026-11-11T07:00:00", None, "Help text line"),
    task("t7", "todo", "2026-11-10T07:00:00", None, "Preview screenshot check"),
    task("t8", "todo", "2026-11-09T07:00:00", None, "Fourth to do"),
    task("t9", "done", "2026-11-13T11:00:00", "plan-a", "Mirror notices to a file"),
    task("t10", "done", "2026-11-12T11:00:00", "plan-old", "Kind field on Notice"),
    task("t11", "done", "2026-11-10T11:00:00", "plan-old", "Theme module in THEMED"),
    task("t12", "done", "2026-11-09T11:00:00", "plan-mixed", "Half done"),
    task("t13", "todo", "2026-11-09T10:00:00", "plan-mixed", "Other half"),
    task("t14", "in_progress", "2026-11-14T10:00:00", "plan-a", "Old version", status="superseded"),
]
PLANS = [{"id": "plan-a", "title": "Activity panel on the right", "updated_at": "2026-11-13T10:00:00"},
         {"id": "plan-old", "title": "Notice history", "updated_at": "2026-11-12T10:00:00"},
         {"id": "plan-mixed", "title": "Mixed plan", "updated_at": "2026-11-09T10:00:00"}]
STAGE = {"reason": "needs_review", "stage": "review", "slot_id": "s-claude", "client": "claude",
         "plan": {"id": "plan-a", "title": "Activity panel on the right", "version": 2},
         "task": {"id": "t2", "state": "needs_review", "content": "Wire journal events into the activity panel"}}
SLOTS = [{"id": "s-claude", "profile_id": "claude", "client": "claude", "run_id": "run-1", "lifecycle": "running"},
         {"id": "s-codex", "profile_id": "codex"}]


class Workspace:
    def __init__(self, project=PROJECT, slots=None):
        self.value = {"revision": 1, "workspace_id": "default", "project_id": project["id"] if project else None,
                      "project_name": project["name"] if project else None,
                      "worktree": project["root"] if project else None, "selected_entity": None,
                      "focus": "sidebar", "slots": deepcopy(slots or []), "run_ids": []}
        self.updates = []

    def read(self):
        return deepcopy(self.value)

    def update(self, changes, expected_revision=None):
        self.updates.append(deepcopy(changes))
        self.value.update(deepcopy(changes))
        self.value["revision"] += 1
        return self.read()

    def switch(self, project):
        self.value.update({"project_id": project["id"], "project_name": project["name"], "worktree": project["root"]})
        self.value["revision"] += 1


class Broker:
    def __init__(self, tasks=TASKS, plans=PLANS, stage=STAGE):
        self.data = {"project-a": (tasks, plans, stage), "project-b": ([task("b1", "todo", content="Other task")], [], None)}
        self.calls, self.events_opened = [], 0
        self.down = False
        self.hold = None            # a threading.Event the next request for project-a waits on
        self.queue = None

    def ensure(self):
        raise AssertionError("the activity panel never starts the local service")

    async def events(self, **kwargs):
        self.events_opened += 1
        self.queue = asyncio.Queue()
        while True:
            yield await self.queue.get()

    def request(self, method, path, *, params=None, json=None, timeout=20):
        self.calls.append((method, path, deepcopy(params), deepcopy(json)))
        if self.down:
            raise ConnectionError("broker down")
        project = (params or {}).get("project")
        if self.hold is not None and project == "project-a":
            self.hold.wait(5)
        tasks, plans, stage = self.data[project]
        if path == "/api/inspector/tasks":
            return {"items": deepcopy(tasks), "total": len(tasks), "next_cursor": None}
        if path == "/api/inspector/plans":
            return {"items": deepcopy(plans), "next_cursor": None}
        if path == "/api/stage":
            if stage is None:
                raise ValueError("Broker 409: register the worktree")
            return deepcopy(stage)
        raise AssertionError((method, path))


def make(tmp_path, monkeypatch, *, language="en", project=PROJECT, slots=SLOTS, broker=None, popup=False):
    monkeypatch.setattr("cairnmind.terminal_ui.language_for", lambda settings: language)
    settings = Settings(data_dir=tmp_path / "data")
    _private_directory(settings.data_dir / "workspaces")
    _private_directory(settings.data_dir / "workspaces" / "default")
    workspace = Workspace(project, slots)
    broker = broker or Broker()
    install(monkeypatch)
    return activity_app(settings, broker=broker, workspace=workspace, popup=popup), broker, workspace, settings


def rows(app, identifier="activity-main"):
    widget = app.query_one("#" + identifier, ActivityList)
    return [str(widget.get_option_at_index(index).prompt) for index in range(widget.option_count)]


def text_of(app):
    return "\n".join(rows(app) + rows(app, "activity-notices"))


def flat(app):
    """The text with wrapped sentences joined again."""
    return " ".join(text_of(app).split())


async def until(pilot, condition, timeout=4.0):
    for _ in range(int(timeout / .02)):
        await pilot.pause(.02)
        if condition():
            return
    raise AssertionError("condition not reached")


def loaded(app):
    return lambda: any(word in " ".join(row.split()) for row in rows(app) for word in (
        "steps done", "kroków", "No active plan.", "no steps", "No tasks", "unavailable"))


# ----- pure functions ------------------------------------------------------------------------

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


# ----- rendering ------------------------------------------------------------------------------

async def test_realistic_data_shows_the_four_sections(tmp_path, monkeypatch):
    app, broker, workspace, settings = make(tmp_path, monkeypatch)
    history = NoticeHistory()
    history.add("Broker not responding; retrying", {"project_id": "project-a", "worktree": PROJECT["root"]}, "warn")
    history.add("Indexed 12 files", {"project_id": "project-b", "project_name": "other", "worktree": "/work/other"})
    publish(settings, "sidebar", history)
    async with app.run_test(size=(32, 43)) as pilot:
        await until(pilot, loaded(app))
        await until(pilot, lambda: len(rows(app, "activity-notices")) == 3)
        main = rows(app)
        for expected in ("Running now", "  claude    Running", "  Stage: review", "Plan",
                         "  Activity panel on the right", "  1 of 6 steps done", "  Plans finished recently", "Tasks",
                         "  In progress 1", "  Needs review 2", "  Needs test 1", "  To do 5", "   + 2 more",
                         "  Done recently 4", "   + 1 more"):
            assert expected in main, expected
        assert "   · Notice history · 2/2" in main  # the date only where it does not cut the title
        assert all(cell_len(row) <= 29 for row in main) and not any("codex" in row for row in main)
        notices = rows(app, "activity-notices")
        assert notices[0] == "Notices"
        # Newest first; in the narrow pane time and kind are above the text.
        assert "warning" in notices[2] and "\n    Broker not responding" in notices[2]
        assert " info\n    [other] Indexed 12 files" in notices[1]
        assert "Activity" in str(app.query_one("#activity-title").render())
        assert str(app.query_one("#activity-folder").render()) == "cairnmind-144"
        assert str(app.query_one("#activity-keys").render()) == "Enter: open · r: refresh"
        assert not any(call[1] == "/api/inspector/records" for call in broker.calls)


async def test_only_done_is_green_and_memory_text_keeps_the_plain_colour(tmp_path, monkeypatch):
    app, *_ = make(tmp_path, monkeypatch)
    async with app.run_test(size=(32, 43)) as pilot:
        await until(pilot, loaded(app))
        widget = app.query_one("#activity-main", ActivityList)
        green = Style(color=terminal_theme.TOKENS["ok"])
        for index in range(widget.option_count):
            prompt = widget.get_option_at_index(index).prompt
            for span in prompt.spans:
                part = prompt.plain[span.start:span.end]
                if span.style == green:
                    assert part == "Done recently", part
                if "Render the notices" in part:
                    assert span.style == terminal_theme.style("stored")


async def test_hostile_task_titles_are_literal_text(tmp_path, monkeypatch):
    hostile = [task("h1", "todo", content="[bold red]x[/]"), task("h2", "todo", content="\x1b[31mred\x1b[0m"),
               task("h3", "todo", content="‮evil")]
    plans = [{"id": "p", "title": "[link=http://x]plan[/link]"}]
    stage = {"reason": "ready", "plan": {"id": "p", "title": "[link=http://x]plan[/link]"}}
    app, *_ = make(tmp_path, monkeypatch, broker=Broker(hostile, plans, stage))
    async with app.run_test(size=(60, 43)) as pilot:
        await until(pilot, loaded(app))
        text = text_of(app)
        assert "· [bold red]x[/]" in text and "[link=http://x]plan[/link]" in text
        assert "\x1b" not in text and "\\x1b[31mred" in text and "‮" not in text and "\\u202eevil" in text
        widget = app.query_one("#activity-main", ActivityList)
        prompts = [widget.get_option_at_index(i).prompt for i in range(widget.option_count)]
        hostile_row = next(p for p in prompts if "[bold red]" in p.plain)
        assert all(span.style in {terminal_theme.style("label"), terminal_theme.style("stored")} for span in hostile_row.spans)


async def test_empty_project_has_one_sentence_per_section(tmp_path, monkeypatch):
    app, *_ = make(tmp_path, monkeypatch, slots=[], broker=Broker([], [], None))
    async with app.run_test(size=(32, 43)) as pilot:
        await until(pilot, loaded(app))
        text = flat(app)
        for sentence in ("No assistant is running.", "No active plan.", "No tasks in this project yet.",
                         "No notices since the workspace opened."):
            assert sentence in text
        assert all(line.startswith("  ") for row in rows(app) if "\n" in row for line in row.split("\n"))


async def test_no_project_replaces_the_sections_with_one_sentence(tmp_path, monkeypatch):
    app, broker, *_ = make(tmp_path, monkeypatch, project=None)
    async with app.run_test(size=(32, 43)) as pilot:
        await until(pilot, lambda: [" ".join(row.split()) for row in rows(app)] == [
            "Choose a project in the sidebar to see its activity."])
        assert broker.calls == []


async def test_broker_down_shows_unavailable_but_sessions_and_notices_render(tmp_path, monkeypatch):
    broker = Broker()
    broker.down = True
    app, _, _, settings = make(tmp_path, monkeypatch, broker=broker)
    history = NoticeHistory()
    history.add("Local note", {"project_id": "project-a", "worktree": PROJECT["root"]})
    publish(settings, "graph", history)
    async with app.run_test(size=(32, 43)) as pilot:
        await until(pilot, lambda: "Memory service unavailable; retrying." in flat(app))
        assert flat(app).count("Memory service unavailable; retrying.") == 1  # once, not under every section
        assert "  claude    Running" in rows(app)
        await until(pilot, lambda: any("Local note" in row for row in rows(app, "activity-notices")))


async def test_follows_a_project_switch_and_drops_a_late_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal_activity, "THROTTLE", .1)
    broker = Broker()
    broker.hold = threading.Event()
    app, _, workspace, _ = make(tmp_path, monkeypatch, broker=broker)
    async with app.run_test(size=(32, 43)) as pilot:
        await until(pilot, lambda: any(call[2]["project"] == "project-a" for call in broker.calls))
        workspace.switch(OTHER)
        await until(pilot, lambda: any("Other task" in row for row in rows(app)))
        broker.hold.set()  # the answer for project-a arrives after the switch
        await pilot.pause(.5)
        assert any("Other task" in row for row in rows(app))
        assert not any("Render the notices" in row for row in rows(app))
        assert "other" in str(app.query_one("#activity-title").render())


async def test_journal_events_refresh_without_a_key_once_per_burst(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal_activity, "THROTTLE", .1)
    broker = Broker()
    app, _, _, settings = make(tmp_path, monkeypatch, broker=broker)
    async with app.run_test(size=(32, 43)) as pilot:
        await until(pilot, loaded(app))
        await until(pilot, lambda: broker.queue is not None)
        await pilot.pause(.3)
        before = len(broker.calls)
        broker.queue.put_nowait({"data": {"entity_type": "record", "project_id": "project-b"}})
        await pilot.pause(.8)
        assert len(broker.calls) == before  # another project
        broker.data["project-a"] = ([*TASKS, task("new", "in_progress", "2026-11-14T00:00:00", content="Fresh")],
                                    PLANS, STAGE)
        for _ in range(3):
            broker.queue.put_nowait({"data": {"entity_type": "record", "project_id": "project-a"}})
        await until(pilot, lambda: any("Fresh" in row for row in rows(app)), timeout=1.5)
        await pilot.pause(.6)
        # One request set, and only what a record can change: tasks and the stage, not the plans.
        assert [call[1] for call in broker.calls[before:]] == ["/api/inspector/tasks", "/api/stage"]
        history = NoticeHistory()
        history.add("A new notice", {"project_id": "project-a", "worktree": PROJECT["root"]}, "error")
        publish(settings, "inspector", history)
        await until(pilot, lambda: any(" error\n    A new notice" in row for row in rows(app, "activity-notices")),
                    timeout=1.6)


async def test_triggers_are_throttled_and_merged(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal_activity, "THROTTLE", .8)
    broker = Broker()
    app, *_ = make(tmp_path, monkeypatch, broker=broker)
    async with app.run_test(size=(32, 43)) as pilot:
        await until(pilot, loaded(app))
        start = len(broker.calls)
        app.request_refresh()
        await pilot.pause(.1)
        app.request_refresh()
        await pilot.pause(.1)
        assert len(broker.calls) - start <= 3
        await until(pilot, lambda: len(broker.calls) - start == 3, timeout=1.5)
        await pilot.pause(.9)
        assert len(broker.calls) - start == 3


async def test_read_only_requests_and_enter_writes_only_the_ui_selection(tmp_path, monkeypatch):
    app, broker, workspace, _ = make(tmp_path, monkeypatch)
    async with app.run_test(size=(32, 43)) as pilot:
        await until(pilot, loaded(app))
        assert {method for method, *_ in broker.calls} == {"GET"}
        assert {path for _, path, *_ in broker.calls} <= set(terminal_activity.ROUTES)
        assert all(body is None for *_, body in broker.calls) and broker.events_opened == 1
        assert workspace.updates == []
        widget = app.query_one("#activity-main", ActivityList)
        widget.focus()
        target = next(i for i in range(widget.option_count) if "Render the notices" in str(widget.get_option_at_index(i).prompt))
        widget.highlighted = target
        await pilot.press("enter")
        await until(pilot, lambda: workspace.updates)
        assert workspace.updates == [{"selected_entity": {"project_id": "project-a", "entity_type": "record", "id": "t1"},
                                      "focus": "inspector"}]
        plan = next(i for i, row in enumerate(rows(app)) if row == "  Activity panel on the right")
        widget.highlighted = plan
        await pilot.press("enter")
        await until(pilot, lambda: len(workspace.updates) == 2)
        assert workspace.updates[1]["selected_entity"]["entity_type"] == "plan"
        session = rows(app).index("  claude    Running")
        assert widget.get_option_at_index(session).disabled  # a session row opens nothing, so it is no stop


async def test_tab_moves_between_sections(tmp_path, monkeypatch):
    app, *_ = make(tmp_path, monkeypatch)
    async with app.run_test(size=(32, 43)) as pilot:
        await until(pilot, loaded(app))
        widget = app.query_one("#activity-main", ActivityList)
        widget.focus()
        seen = []
        for _ in range(4):
            await pilot.press("tab")
            seen.append(app._payloads[app.focused.id].get(app.focused.highlighted))
        # Only rows that open something: the stage task, the plan, the first task; then round again.
        assert seen == [("record", "t2"), ("plan", "plan-a"), ("record", "t1"), ("record", "t2")]
        await pilot.press("shift+tab")
        assert app._payloads[app.focused.id].get(app.focused.highlighted) == ("record", "t1")


async def test_polish_labels(tmp_path, monkeypatch):
    app, *_ = make(tmp_path, monkeypatch, language="pl")
    async with app.run_test(size=(32, 43)) as pilot:
        await until(pilot, loaded(app))
        text = text_of(app)
        for word in ("Teraz działa", "W toku 1", "Do przeglądu 2", "Etap: przegląd", "Ostatnio zakończone plany",
                     "1 z 6 kroków ukończone", "Ostatnio zakończone", "Komunikaty", "Zadania"):
            assert word in text, word
        assert "Aktywność" in str(app.query_one("#activity-title").render())


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


@pytest.mark.parametrize("key", ["escape", "q", "a"])
async def test_the_popup_closes_with_one_key(tmp_path, monkeypatch, key):
    app, *_ = make(tmp_path, monkeypatch, popup=True)
    async with app.run_test(size=(120, 34)) as pilot:
        await until(pilot, loaded(app))
        assert app.has_class("-columns") and "a / Esc: close" in str(app.query_one("#activity-hint").render())
        await pilot.press(key)
        await until(pilot, lambda: not app.is_running)


async def test_the_popup_closes_after_opening_an_item(tmp_path, monkeypatch):
    app, _, workspace, _ = make(tmp_path, monkeypatch, popup=True)
    async with app.run_test(size=(72, 20)) as pilot:
        await until(pilot, loaded(app))
        assert not app.has_class("-columns")
        widget = app.query_one("#activity-main", ActivityList)
        widget.focus()
        widget.highlighted = next(i for i, row in enumerate(rows(app)) if "Render the notices" in row)
        await pilot.press("enter")
        await until(pilot, lambda: not app.is_running)
    assert workspace.updates[-1]["focus"] == "inspector"


def test_today_notice_shows_the_time(tmp_path, monkeypatch):
    app, *_ = make(tmp_path, monkeypatch)
    app.notices = [{"role": "sidebar", "when": datetime.now().replace(hour=14, minute=2), "text": "Hi", "kind": "warn",
                    "project_id": "project-a", "project_name": "cairnmind", "worktree": PROJECT["root"],
                    "count": 2, "seq": 1}]
    app._scope = ("project-a", PROJECT["root"])
    assert [str(text) for text, _ in app.notice_rows(60)][1] == "  14:02 warning Hi (×2)"
    assert [str(text) for text, _ in app.notice_rows(30)][1] == "  14:02 warning (×2)\n    Hi"


# ----- review fixes ---------------------------------------------------------------------------

async def test_a_short_popup_shows_the_tasks_without_scrolling(tmp_path, monkeypatch):
    app, _, _, settings = make(tmp_path, monkeypatch, popup=True)
    history = NoticeHistory()
    for index in range(6):
        history.add(f"Notice {index}", {"project_id": "project-a", "worktree": PROJECT["root"]})
    publish(settings, "sidebar", history)
    async with app.run_test(size=(70, 18)) as pilot:  # the popup at 80x24
        await until(pilot, loaded(app))
        await until(pilot, lambda: len(rows(app, "activity-notices")) > 3)
        await pilot.pause(.2)
        main = app.query_one("#activity-main", ActivityList)
        assert rows(app).index("Tasks") + 1 < main.size.height
        summary = rows(app)[rows(app).index("Tasks") + 1]
        assert summary.startswith("  In progress 1 · Needs review 2 · Needs test 1")


async def test_notices_are_never_cut_between_their_two_rows(tmp_path, monkeypatch):
    app, _, _, settings = make(tmp_path, monkeypatch)
    history = NoticeHistory()
    for index in range(20):
        history.add(f"Notice {index}", {"project_id": "project-a", "worktree": PROJECT["root"]}, "error")
    publish(settings, "sidebar", history)
    async with app.run_test(size=(32, 43)) as pilot:
        await until(pilot, loaded(app))
        await until(pilot, lambda: len(rows(app, "activity-notices")) == 21)
        await pilot.pause(.2)
        notes = app.query_one("#activity-notices", ActivityList)
        assert (notes.styles.max_height.value - 1) % 2 == 0  # the title, then whole two-row entries


async def test_a_wide_popup_shows_more_of_each_group(tmp_path, monkeypatch):
    app, *_ = make(tmp_path, monkeypatch, popup=True)
    async with app.run_test(size=(141, 36)) as pilot:
        await until(pilot, loaded(app))
        await pilot.pause(.2)
        assert app.has_class("-columns")
        assert "   · Fourth to do" in rows(app) and "   + 2 more" not in rows(app)


async def test_no_project_labels_no_notice_and_hides_the_folder_row(tmp_path, monkeypatch):
    app, _, _, settings = make(tmp_path, monkeypatch, project=None)
    history = NoticeHistory()
    history.add("Broker not responding", {"project_id": "project-a", "project_name": "cairnmind"}, "warn")
    publish(settings, "sidebar", history)
    async with app.run_test(size=(32, 43)) as pilot:
        await until(pilot, lambda: any("Broker not responding" in row for row in rows(app, "activity-notices")))
        assert not any("[cairnmind]" in row for row in rows(app, "activity-notices"))
        assert app.query_one("#activity-folder").has_class("-empty")


async def test_the_stream_coming_back_or_a_reset_fetches_again(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal_activity, "THROTTLE", .1)
    broker = Broker()
    app, *_ = make(tmp_path, monkeypatch, broker=broker)
    async with app.run_test(size=(32, 43)) as pilot:
        await until(pilot, loaded(app))
        await until(pilot, lambda: broker.queue is not None)
        await pilot.pause(.3)
        before = len(broker.calls)
        broker.queue.put_nowait({"event": "unavailable", "data": {"reason": "broker"}})
        broker.queue.put_nowait({"data": {"entity_type": "record", "project_id": "project-b"}})
        await until(pilot, lambda: len(broker.calls) == before + 3, timeout=1.5)
        before = len(broker.calls)
        broker.queue.put_nowait({"event": "reset", "data": {"project_id": None, "entity_type": "reset"}})
        await until(pilot, lambda: len(broker.calls) == before + 3, timeout=1.5)


async def test_a_cut_task_list_says_how_many_are_shown(tmp_path, monkeypatch):
    class Many(Broker):
        def request(self, method, path, **kwargs):
            value = super().request(method, path, **kwargs)
            if path == "/api/inspector/tasks":
                value.update(total=812, next_cursor="x")
            return value
    app, *_ = make(tmp_path, monkeypatch, broker=Many())
    async with app.run_test(size=(60, 43)) as pilot:
        await until(pilot, loaded(app))
        assert f"Showing {len(TASKS)} of 812 tasks." in flat(app)


def test_blocked_and_needs_test_are_told_apart_from_to_do(tmp_path, monkeypatch):
    app, *_ = make(tmp_path, monkeypatch)
    app.status, app.tasks = "ok", [task("b", "blocked"), task("n", "needs_test"), task("t", "todo")]
    labels = {}
    for text, _ in app._task_rows(60, False, 0):
        for span in text.spans:
            labels[text.plain[span.start:span.end]] = span.style
    assert labels["Blocked"] == labels["Needs test"] == terminal_theme.style("warn")
    assert labels["To do"] == terminal_theme.style("value") and labels["Blocked"] != terminal_theme.style("ok")
