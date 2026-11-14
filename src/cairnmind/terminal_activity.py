"""The activity panel (#144): what is happening in the selected project, read-only, in one column.

Four sections: Running now (the started sessions in the workspace state and the stage of the next
step), Plan (the active plan's progress and plans finished recently), Tasks (grouped by state) and
Notices (the copies the other panels keep on their tmux panes, terminal_notice_feed).

It only reads: three GET routes of the broker (`/api/inspector/tasks`, `/api/inspector/plans`,
`/api/stage`), the journal event stream, the workspace state file and the notice copies. Enter on a
task or plan opens it in the inspector by writing the UI selection, as the sidebar does; it writes
no record, review, verdict, task state or plan. Every text from memory or a model keeps the plain
colour and goes through `terminal_safe`; a state is told by a word.
"""
from __future__ import annotations

import asyncio
import textwrap
import time
from contextlib import suppress
from datetime import datetime
from pathlib import PurePath

from rich.cells import cell_len
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from . import terminal_notice_feed, terminal_theme
from .terminal_activity_data import (KIND_WORDS, STAGES, finished_plans, first_line, fit, group_tasks, items_of,
                                     live_tasks, notice_parts, plan_progress, running_rows, task_summary)
from .terminal_client import BrokerUnreachable
from .terminal_theme import assemble, state_style, styled
from .terminal_ui import BasePanel

_LIFECYCLE_ROLE = {"Running": "value", "Starting": "warn", "Stopping": "warn", "Failed": "danger", "Interrupted": "danger"}
_GROUP_ROLE = {"needs_test": "warn", "blocked": "warn"}  # waiting for a person, like Needs review
COLUMNS = 110                # from this width (the popup) Notices get a column of their own
NARROW = 40                  # below this width a notice takes two rows: time and kind, then the text
THROTTLE, DEBOUNCE, FALLBACK, TIMEOUT = 2.0, .5, 15.0, 5
ROUTES = ("/api/inspector/tasks", "/api/inspector/plans", "/api/stage")
PARTS = dict(zip(("tasks", "plans", "stage"), ROUTES))
# What a journal event can change; a reset or an event of no project may have changed anything.
EVENT_PARTS = {"record": {"tasks", "stage"}, "check": {"tasks", "stage"}, "plan": {"plans", "stage"},
               "scope": {"stage"}}
TITLE, ROW = ("title",), ("row",)   # row payloads: a section title; an item that opens nothing
KEYS, KEYS_SHORT = "Tab: next section · Enter: open · r: refresh", "Enter: open · r: refresh"


class ActivityList(OptionList):
    """One section list. Tab / Shift+Tab jump between sections (and to the other list).

    Up and down scroll a list that has nothing to open (the notices), since no row there is a stop.
    """
    BINDINGS = [("tab", "app.section(1)", "Next section"), ("shift+tab", "app.section(-1)", "Previous section")]

    def _inert(self):
        return all(self.get_option_at_index(index).disabled for index in range(self.option_count))

    def action_cursor_down(self):
        self.scroll_down(animate=False) if self._inert() else super().action_cursor_down()

    def action_cursor_up(self):
        self.scroll_up(animate=False) if self._inert() else super().action_cursor_up()


class ActivityApp(BasePanel):
    panel_role = "activity"
    TITLE = "Cairnmind"
    CSS = BasePanel.CSS + terminal_theme.css("""
    #activity { height: 1fr; padding: 0 1 0 0; }
    #activity-head, #activity-folder, #activity-keys { padding-left: 1; }
    #activity-head { height: 1; }
    #activity-title { width: 1fr; height: 1; }
    #activity-hint { width: auto; height: 1; color: $cm-muted; display: none; }
    .-popup #activity-hint { display: block; }
    #activity-folder { height: 1; color: $cm-muted; }
    .-popup #activity-folder, #activity-folder.-empty { display: none; }
    #activity-body { height: 1fr; layout: vertical; margin-top: 1; }
    #activity-main { height: auto; }
    #activity-notices { height: auto; margin-top: 1; }
    #activity-keys { height: 1; color: $cm-muted; }
    .-popup.-short #activity-keys { display: none; }
    .-columns #activity-body { layout: horizontal; }
    .-columns #activity-main { width: 1fr; max-width: 60; height: 1fr; margin-right: 3; }
    .-columns #activity-notices { width: 1fr; height: 1fr; max-height: 100%; margin-top: 0; }
    ActivityList { border: none; border-left: vkey $cm-bg; padding: 0; background: $cm-bg; scrollbar-gutter: stable; }
    ActivityList:focus { border: none; border-left: vkey $cm-accent; background-tint: transparent 0%; }
    ActivityList > .option-list--option { padding: 0; }
    ActivityList > .option-list--option-disabled { color: $cm-text; }
    ActivityList > .option-list--option-highlighted { background: $cm-bg; color: $cm-text; text-style: none; }
    ActivityList:focus > .option-list--option-highlighted { background: $cm-select; color: $cm-strong; text-style: bold; }
    ActivityList > .option-list--option-hover { background: $cm-bg; }
    """)
    BINDINGS = [("r", "refresh", "Refresh")]

    def __init__(self, settings, workspace_id="default", *, broker=None, workspace=None, popup=False):
        super().__init__(settings, workspace_id, broker=broker, workspace=workspace)
        self.popup = popup
        self.status = "loading"            # loading | ok | unavailable | none (no project)
        self.tasks, self.plans, self.stage, self.tasks_total = [], [], None, 0
        self.notices = []
        self._scope = None
        self._in_flight, self._parts = None, set()   # the scope of the request set in flight; parts asked for
        self._last_fetch = 0.0
        self._refresh_timer = self._journal_timer = None
        self._stream_down = False
        self._payloads = {"activity-main": {}, "activity-notices": {}}
        self._signatures = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="activity"):
            with Horizontal(id="activity-head"):
                yield Static("", id="activity-title", markup=False)
                yield Static("", id="activity-hint", markup=False)
            yield Static("", id="activity-folder", markup=False)
            with Container(id="activity-body"):
                yield ActivityList(id="activity-main")
                yield ActivityList(id="activity-notices")
            yield Static("", id="activity-keys", markup=False)

    def on_mount(self):
        self.set_class(self.popup, "-popup")
        self._size_classes(self.size)
        self.set_interval(.5, self._poll_workspace)
        self.set_interval(1, self.watch_notices)
        self.set_interval(FALLBACK, self.request_refresh)
        self.set_interval(3, self._retry)
        self.start_activity()

    def _size_classes(self, size):
        self.set_class(size.width >= COLUMNS, "-columns")
        self.set_class(size.height < terminal_theme.SHORT_HEIGHT, "-short")

    def on_resize(self, event):
        self._size_classes(event.size)
        self.call_after_refresh(self.render_all)

    @work(group="activity-start", exclusive=True, exit_on_error=False)
    async def start_activity(self):
        # No broker.ensure(): the sidebar starts the local service; this panel only reads it.
        await self._poll_workspace()
        self.watch_notices()
        self.render_all()
        self.watch_journal()

    async def workspace_changed(self, value):
        scope = (value.get("project_id"), value.get("worktree"))
        if scope != self._scope:
            self._scope = scope
            self.tasks, self.plans, self.stage, self.tasks_total = [], [], None, 0
            self.status = "loading" if scope[0] else "none"
            self._last_fetch = 0.0  # a project switch is not throttled
            self.render_all()
            self.request_refresh()
        else:
            self.render_main()  # the sessions come from the state file

    def request_refresh(self, parts=tuple(PARTS)):
        """At most one request set in flight and one every THROTTLE seconds; later triggers are merged.

        An answer still on its way for the project shown before does not hold up the new one.
        """
        self._parts |= set(parts)
        if not (self._scope or (None,))[0] or not self._parts or self._in_flight == self._scope:
            return  # an answer in flight for this project: the parts asked for wait for it
        wait = self._last_fetch + THROTTLE - time.monotonic()
        if wait > 0:
            if self._refresh_timer is None:
                self._refresh_timer = self.set_timer(wait, self._timer_refresh)
            return
        self._in_flight, self._last_fetch = self._scope, time.monotonic()
        parts, self._parts = self._parts, set()
        self.fetch(self._scope, self._scope_version, parts)

    def _timer_refresh(self):
        self._refresh_timer = None
        self.request_refresh(())

    def _retry(self):
        if self.status == "unavailable":
            self.request_refresh()

    def action_refresh(self):
        self.request_refresh()

    async def _get(self, part, params):
        limit = {} if part == "stage" else {"limit": 500}
        try:  # one after another: the broker client serves one request at a time anyway
            return await asyncio.to_thread(self.broker.request, "GET", PARTS[part], params={**params, **limit},
                                           timeout=TIMEOUT)
        except Exception as error:
            return error

    @work(group="activity-fetch", exit_on_error=False)
    async def fetch(self, scope, version, parts):
        project, worktree = scope
        try:
            got = {part: await self._get(part, {"project": project, "worktree": worktree})
                   for part in PARTS if part in parts}
            if version != self._scope_version or scope != self._scope:
                return  # an answer for the project shown before
            down = isinstance(got.get("tasks"), BaseException) or isinstance(got.get("stage"), (BrokerUnreachable, OSError))
            self.status = "unavailable" if down else "ok"
            if isinstance(got.get("tasks"), dict):
                self.tasks = items_of(got["tasks"])
                total = got["tasks"].get("total")
                self.tasks_total = total if type(total) is int and total >= len(self.tasks) else len(self.tasks)
            if isinstance(got.get("plans"), dict):
                self.plans = items_of(got["plans"])
            if "stage" in got and not down:
                # 404 / 409 (no registered worktree, no active plan) is "No active plan.", not a failure.
                self.stage = got["stage"] if isinstance(got["stage"], dict) else None
            self.render_main()
        finally:
            if self._in_flight == scope:
                self._in_flight = None
            if self._parts and self._in_flight is None:
                self.request_refresh(())

    @work(group="activity-journal", exclusive=True, exit_on_error=False)
    async def watch_journal(self):
        try:
            async for event in self.broker.events():
                event = event if isinstance(event, dict) else {}
                if event.get("event") == "unavailable":  # the stream reconnects by itself
                    self._stream_down = True
                    continue
                data = event.get("data") if isinstance(event.get("data"), dict) else {}
                project = (self._scope or (None,))[0]
                if self._stream_down:  # back after an outage: what was missed is fetched again
                    self._stream_down = False
                    self._journal(tuple(PARTS))
                elif event.get("event") == "reset" or data.get("entity_type") == "reset" or data.get("project_id") is None:
                    self._journal(tuple(PARTS))
                elif project and data.get("project_id") == project and data.get("entity_type") in EVENT_PARTS:
                    self._journal(EVENT_PARTS[data["entity_type"]])
        except Exception:  # the stream is gone: the 15 s poll goes on; try the stream again later
            self.set_timer(FALLBACK, self.watch_journal)

    def _journal(self, parts):
        self._parts |= set(parts)
        if self._journal_timer is not None:
            self._journal_timer.stop()
        self._journal_timer = self.set_timer(DEBOUNCE, self._journal_refresh)

    def _journal_refresh(self):
        self._journal_timer = None
        self.request_refresh(())

    @work(group="activity-notices", exclusive=True, exit_on_error=False)
    async def watch_notices(self):
        notices = await asyncio.to_thread(terminal_notice_feed.read_all, self.settings, self.workspace_id)
        if notices != self.notices:
            self.notices = notices
            self.render_notices()

    # ----- rows --------------------------------------------------------------------------------
    def _sentence(self, text, width, **values):
        """The only text that wraps: every line keeps the indent of the section."""
        lines = textwrap.wrap(self.t(text, **values), max(8, width - 2)) or [""]
        return (styled("\n".join("  " + line for line in lines), "muted"), None)

    def _title(self, text):
        return (styled(self.t(text), "accent"), TITLE)

    def _blank(self):
        return (styled("", "value"), None)

    def main_rows(self, width, extra=0):
        if self.status == "none":
            return [self._sentence("Choose a project in the sidebar to see its activity.", width)]
        small = self.size.height < terminal_theme.SHORT_HEIGHT  # a short popup keeps Tasks in the first view
        return [*self._running_rows(width, small), self._blank(), *self._plan_rows(width, small), self._blank(),
                *self._task_rows(width, small, extra)]

    def _running_rows(self, width, small):
        rows = [self._title("Running now")]
        sessions = running_rows(self.workspace_data.get("slots"))
        if not any(live for _, _, live in sessions):
            rows.append(self._sentence("No assistant is running.", width))
        else:
            for client, word, _ in sessions:
                shown = fit(client, 10)
                rows.append((assemble([("value", "  "), ("stored", shown + " " * (10 - cell_len(shown))),
                                       (_LIFECYCLE_ROLE.get(word, "muted"), self.t(word))]), ROW))
        stage = self.stage or {}
        if stage.get("reason") in STAGES:
            slot = next((row for row in self.workspace_data.get("slots") or [] if isinstance(row, dict)
                         and row.get("id") == stage.get("slot_id")), {})
            line = "  " + self.t("Stage: {stage}", stage=self.t(STAGES[stage["reason"]]))
            if slot.get("run_id") and slot.get("lifecycle") == "running":
                running = " · " + self.t("{client} running", client=first_line(slot.get("client") or ""))
                line += running if cell_len(line + running) <= width else ""  # the session row says it already
            rows.append((styled(fit(line, width), "label"), ROW))
            task = stage.get("task") if isinstance(stage.get("task"), dict) else {}
            if first_line(task.get("content")) and not small:
                rows.append((styled(fit("  " + first_line(task.get("content")), width), "stored"),
                             ("record", task.get("id"))))
        return rows

    def _plan_rows(self, width, small):
        rows = [self._title("Plan")]
        if self.status in {"loading", "unavailable"}:  # said once here; Tasks shows a dash
            return rows + [self._sentence("Loading…" if self.status == "loading" else "Memory service unavailable; retrying.", width)]
        stage = self.stage or {}
        plan = stage.get("plan") if isinstance(stage.get("plan"), dict) else None
        if not plan or not plan.get("id"):
            rows.append(self._sentence("No active plan.", width))
        else:
            done, total = plan_progress(self.tasks, plan["id"])
            rows.append((styled(fit("  " + first_line(plan.get("title") or plan["id"]), width), "stored"),
                         ("plan", plan["id"])))
            rows.append(self._sentence("This plan has no steps yet.", width) if not total else
                        (styled("  " + self.t("{done} of {total} steps done", done=done, total=total), "label"), None))
        finished = [] if small else finished_plans(self.tasks, self.plans, plan.get("id") if plan else None)
        if finished:
            rows.append((styled("  " + self.t("Plans finished recently"), "label"), None))
            for row in finished:
                date = f" · {row['date']}" if row["date"] and width >= NARROW else ""  # the narrow pane keeps the title
                tail = f"{date} · {row['count']}/{row['count']}"
                title = fit(first_line(row["title"]), max(1, width - 5 - cell_len(tail)))
                rows.append((assemble([("label", "   · "), ("stored", title), ("label", tail)]), ("plan", row["id"])))
        return rows

    def _task_rows(self, width, small, extra):
        rows = [self._title("Tasks")]
        if self.status in {"loading", "unavailable"}:
            return rows + [(styled("  –", "muted"), None)]
        if not live_tasks(self.tasks):
            return rows + [self._sentence("No tasks in this project yet.", width)]
        groups = group_tasks(self.tasks, small=small, extra=extra)
        if small:  # every state with its count in one row, before the groups
            summary = " · ".join(f"{self.t(label)} {count}" for label, count in task_summary(groups))
            rows.append((styled(fit("  " + summary, width), "label"), None))
        for group in groups:
            role = _GROUP_ROLE.get(group["state"]) or state_style(group["state"])
            rows.append((assemble([("value", "  "), (role, self.t(group["label"])), ("label", f" {group['total']}")]), None))
            for task in group["items"]:
                rows.append((assemble([("label", "   · "), ("stored", fit(first_line(task.get("content")) or task.get("id", ""),
                                                                          width - 5))]), ("record", task.get("id"))))
            if group["hidden"]:
                rows.append((styled("   " + self.t("+ {count} more", count=group["hidden"]), "muted"), None))
        if self.tasks_total > len(self.tasks):
            rows.append(self._sentence("Showing {shown} of {total} tasks.", width, shown=len(self.tasks), total=self.tasks_total))
        return rows

    def notice_rows(self, width):
        rows = [self._title("Notices")]
        if not self.notices:
            return rows + [self._sentence("No notices since the workspace opened.", width)]
        words = {kind: self.t(word) for kind, word in KIND_WORDS.items()} | {None: self.t("No project")}
        project, folder = self._scope or (None, None)
        today = datetime.now().date()
        return rows + [(assemble(notice_parts(entry, width, words, project, folder, today, two_rows=width < NARROW)), ROW)
                       for entry in self.notices]

    # ----- rendering ---------------------------------------------------------------------------
    def render_all(self):
        self.render_header()
        self.render_main()
        self.render_notices()

    def render_header(self):
        with suppress(Exception):
            project, folder = self._scope or (None, None)
            width = max(10, self.query_one("#activity").content_region.width - 1)  # the rows' left padding
            name = first_line(self.workspace_data.get("project_name") or project or "")
            hint = self.t("a / Esc: close") if self.popup else ""
            self.query_one("#activity-hint", Static).update(hint)
            room = width - (cell_len(hint) + 2 if hint else 0)
            head = self.t("Activity")
            tail = " · " + name if name else ""
            if self.popup and folder:
                tail += " · " + PurePath(folder).name
            self.query_one("#activity-title", Static).update(
                assemble([("accent", head), ("stored", fit(tail, max(1, room - cell_len(head))))]))
            shown = first_line(PurePath(folder).name) if folder else ""
            line = self.query_one("#activity-folder", Static)
            line.update(styled(fit(shown, width), "muted"))
            line.set_class(not shown, "-empty")
            keys = self.t(KEYS) if cell_len(self.t(KEYS)) <= width else self.t(KEYS_SHORT)
            self.query_one("#activity-keys", Static).update(styled(fit(keys, width), "muted"))

    def _fill(self, identifier, build):
        with suppress(Exception):
            widget = self.query_one("#" + identifier, ActivityList)
            width = widget.scrollable_content_region.width  # the scrollbar gutter is always kept
            if width < 8:
                return
            rows = build(width)
            if identifier == "activity-main" and self.has_class("-columns"):
                # A wide popup: rows of free height show more of each task group.
                spare = widget.scrollable_content_region.height - sum(text.plain.count("\n") + 1 for text, _ in rows)
                if spare > 0:
                    rows = build(width, spare)
            signature = (width, self.language, tuple(text.plain for text, _ in rows),
                         tuple(payload for _, payload in rows))
            if signature == self._signatures.get(identifier):
                return
            self._signatures[identifier] = signature
            highlighted = widget.highlighted
            widget.clear_options()
            # Only a row that opens something (a task, a plan) is a stop of the cursor.
            widget.add_options([Option(text, disabled=payload in {None, TITLE, ROW}) for text, payload in rows])
            self._payloads[identifier] = {index: payload for index, (_, payload) in enumerate(rows) if payload}
            if highlighted is not None and widget.option_count:
                widget.highlighted = min(highlighted, widget.option_count - 1)
                if widget.get_option_at_index(widget.highlighted).disabled:
                    widget.action_cursor_down()

    def render_main(self):
        self._fill("activity-main", self.main_rows)
        self.call_after_refresh(self.size_lists)

    def render_notices(self):
        self._fill("activity-notices", self.notice_rows)
        self.call_after_refresh(self.size_lists)

    def size_lists(self):
        """One column: Notices follow the other sections, both lists scroll, and a notice is never cut in two."""
        with suppress(Exception):
            main, notes = self.query_one("#activity-main", ActivityList), self.query_one("#activity-notices", ActivityList)
            if self.has_class("-columns"):
                main.styles.max_height = notes.styles.max_height = None
                return
            body = self.query_one("#activity-body").content_region.height
            sizes = [str(notes.get_option_at_index(i).prompt).count("\n") + 1 for i in range(notes.option_count)]
            used = sum(str(main.get_option_at_index(i).prompt).count("\n") + 1 for i in range(main.option_count))
            # The other sections first; Notices get what is left, at least a quarter (three rows when short).
            budget = max(3, min(sum(sizes), max(body - used - 1, int(body * .25))))
            if self.has_class("-short"):
                budget = min(budget, 4)
            shown = 0
            for index, size in enumerate(sizes):  # whole entries only: the title and the first always
                if shown + size > budget and index > 1:
                    break
                shown += size
            shown = max(shown, 2)
            if (notes.styles.max_height, main.styles.max_height) != (shown, max(3, body - shown - 1)):
                notes.styles.max_height = shown
                main.styles.max_height = max(3, body - shown - 1)

    def refresh_language(self):
        self._signatures.clear()
        self.render_all()

    def action_section(self, step):
        """Move to the first task or plan of the next (or previous) section, or into the notices."""
        stops, here = [], (-1, -1)
        for order, identifier in enumerate(("activity-main", "activity-notices")):
            widget = self.query_one("#" + identifier, ActivityList)
            payloads = self._payloads.get(identifier, {})
            titles = sorted(index for index, payload in payloads.items() if payload == TITLE)
            for start, end in zip(titles, [*titles[1:], widget.option_count]):
                target = next((index for index in range(start + 1, end) if len(payloads.get(index) or ()) == 2), None)
                if target is not None:
                    stops.append(((order, target), widget))
            if identifier == "activity-notices" and self.notices:
                stops.append(((order, 0), widget))  # the notices scroll; nothing in them opens
            if widget.has_focus:
                here = (order, widget.highlighted if widget.highlighted is not None else 0)
        if not stops:
            return
        if step > 0:
            key, widget = next((stop for stop in stops if stop[0] > here), stops[0])
        else:
            current = max((index for index, stop in enumerate(stops) if stop[0] <= here), default=0)
            key, widget = stops[current - 1]
        widget.focus()
        widget.highlighted = None if widget.get_option_at_index(key[1]).disabled else key[1]

    @on(OptionList.OptionSelected)
    async def open_item(self, event):
        event.stop()
        payload = self._payloads.get(event.option_list.id or "", {}).get(event.option_index)
        project = (self._scope or (None,))[0]
        if not payload or len(payload) != 2 or not project or not isinstance(payload[1], str) or not payload[1]:
            return
        try:
            # UI selection only, exactly as the sidebar opens an entry: no memory is written.
            await asyncio.to_thread(self.workspace.update, {"selected_entity": {
                "project_id": project, "entity_type": payload[0], "id": payload[1]}, "focus": "inspector"})
        except (OSError, ValueError, RuntimeError):
            return
        if self.popup:
            self.exit()


class ActivityPopup(ActivityApp):
    BINDINGS = [("r", "refresh", "Refresh"), ("escape", "close", "Close"), ("q", "close", "Close"),
                ("a", "close", "Close")]

    def action_close(self):
        self.exit()


def activity_app(settings, workspace_id="default", *, popup=False, **kwargs):
    return (ActivityPopup if popup else ActivityApp)(settings, workspace_id, popup=popup, **kwargs)
