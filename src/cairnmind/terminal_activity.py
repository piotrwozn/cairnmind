"""The activity panel (#144): what is happening in the selected project, read-only, in one column.

Four sections: Running now (the sessions in the workspace state and the stage of the next step),
Plan (the active plan's progress and plans finished recently), Tasks (grouped by state) and
Notices (the private notice copies of the other panels, terminal_notice_feed).

It only reads: three GET routes of the broker (`/api/inspector/tasks`, `/api/inspector/plans`,
`/api/stage`), the journal event stream, the workspace state file and the notice files. Enter on a
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
from .terminal_activity_data import (STAGES, finished_plans, first_line, fit, group_tasks, live_tasks, plan_progress,
                                     running_rows, items_of)
from .terminal_theme import assemble, state_style, styled
from .terminal_ui import BasePanel

_LIFECYCLE_ROLE = {"Running": "value", "Starting": "warn", "Stopping": "warn", "Failed": "danger", "Interrupted": "danger"}
KIND_WORDS = {"info": "info", "warn": "warning", "error": "error"}
_KIND_ROLE = {"info": "label", "warn": "warn", "error": "danger"}
JOURNAL = {"record", "plan", "scope", "check"}
COLUMNS = 90                 # from this width (the popup) Notices get a column of their own
NARROW = 40                  # below this width a notice takes two rows: time and kind, then the text
THROTTLE, DEBOUNCE, FALLBACK = 2.0, .5, 15.0
ROUTES = ("/api/inspector/tasks", "/api/inspector/plans", "/api/stage")
TITLE, ROW = ("title",), ("row",)   # row payloads: a section title; an item that opens nothing


class ActivityList(OptionList):
    """One section list. Tab / Shift+Tab jump between sections (and to the other list)."""
    BINDINGS = [("tab", "app.section(1)", "Next section"), ("shift+tab", "app.section(-1)", "Previous section")]


class ActivityApp(BasePanel):
    panel_role = "activity"
    TITLE = "Cairnmind"
    CSS = BasePanel.CSS + terminal_theme.css("""
    #activity { height: 1fr; padding: 0 1; }
    #activity-head { height: 1; }
    #activity-title { width: 1fr; height: 1; }
    #activity-hint { width: auto; height: 1; color: $cm-muted; display: none; }
    .-popup #activity-hint { display: block; }
    #activity-folder { height: 1; color: $cm-muted; }
    .-popup #activity-folder { display: none; }
    #activity-body { height: 1fr; layout: vertical; margin-top: 1; }
    #activity-main { height: auto; }
    #activity-notices { height: auto; margin-top: 1; }
    .-columns #activity-body { layout: horizontal; }
    .-columns #activity-main { width: 1fr; height: 1fr; margin-right: 3; }
    .-columns #activity-notices { width: 1fr; height: 1fr; max-height: 100%; margin-top: 0; }
    ActivityList { border: none; padding: 0; background: $cm-bg; }
    ActivityList:focus { border: none; background-tint: transparent 0%; }
    ActivityList > .option-list--option { padding: 0; }
    ActivityList > .option-list--option-disabled { color: $cm-text; }
    ActivityList > .option-list--option-highlighted { background: $cm-bg; color: $cm-text; text-style: none; }
    ActivityList:focus > .option-list--option-highlighted { background: $cm-raised; color: $cm-strong; text-style: none; }
    ActivityList > .option-list--option-hover { background: $cm-bg; }
    """)
    BINDINGS = [("r", "refresh", "Refresh")]

    def __init__(self, settings, workspace_id="default", *, broker=None, workspace=None, popup=False):
        super().__init__(settings, workspace_id, broker=broker, workspace=workspace)
        self.popup = popup
        self.status = "loading"            # loading | ok | unavailable | none (no project)
        self.tasks, self.plans, self.stage = [], [], None
        self.notices = []
        self._scope = None
        self._feed_signature = None
        self._in_flight, self._pending = None, False   # the scope of the request set in flight
        self._last_fetch = 0.0
        self._refresh_timer = self._journal_timer = None
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

    def on_mount(self):
        self.set_class(self.popup, "-popup")
        self.set_class(self.size.width >= COLUMNS, "-columns")
        self.set_interval(.5, self._poll_workspace)
        self.set_interval(1, self.watch_notices)
        self.set_interval(FALLBACK, self.request_refresh)
        self.start_activity()

    def on_resize(self, event):
        self.set_class(event.size.width >= COLUMNS, "-columns")
        self.call_after_refresh(self.render_all)

    @work(group="activity-start", exclusive=True, exit_on_error=False)
    async def start_activity(self):
        # No broker.ensure(): the sidebar starts the local service; this panel only reads it.
        await self._poll_workspace()
        self.watch_notices()
        self.render_all()
        self.watch_journal()

    # ----- data --------------------------------------------------------------------------------
    async def workspace_changed(self, value):
        scope = (value.get("project_id"), value.get("worktree"))
        if scope != self._scope:
            self._scope = scope
            self.tasks, self.plans, self.stage = [], [], None
            self.status = "loading" if scope[0] else "none"
            self._last_fetch = 0.0  # a project switch is not throttled
            self.render_all()
            self.request_refresh()
        else:
            self.render_main()  # the sessions come from the state file

    def request_refresh(self):
        """At most one request set in flight and one every THROTTLE seconds; later triggers are merged.

        An answer still on its way for the project shown before does not hold up the new one.
        """
        if not (self._scope or (None,))[0]:
            return
        if self._in_flight == self._scope:
            self._pending = True
            return
        wait = self._last_fetch + THROTTLE - time.monotonic()
        if wait > 0:
            if self._refresh_timer is None:
                self._refresh_timer = self.set_timer(wait, self._timer_refresh)
            return
        self._in_flight = self._scope
        self._last_fetch = time.monotonic()
        self.fetch(self._scope, self._scope_version)

    def _timer_refresh(self):
        self._refresh_timer = None
        self.request_refresh()

    def action_refresh(self):
        self.request_refresh()

    @work(group="activity-fetch", exit_on_error=False)
    async def fetch(self, scope, version):
        project, worktree = scope
        try:
            params = {"project": project, "worktree": worktree}
            tasks, plans, stage = await asyncio.gather(
                self.request("GET", ROUTES[0], params={**params, "limit": 500}),
                self.request("GET", ROUTES[1], params={**params, "limit": 500}),
                self.request("GET", ROUTES[2], params=params), return_exceptions=True)
            if version != self._scope_version or scope != self._scope:
                return  # an answer for the project shown before
            if isinstance(tasks, BaseException):
                self.status = "unavailable"
            else:
                self.status = "ok"
                self.tasks = items_of(tasks)
                self.plans = items_of(plans) if not isinstance(plans, BaseException) else self.plans
                # 404 / 409 (no registered worktree, no active plan) is "No active plan.", not a failure.
                self.stage = stage if isinstance(stage, dict) else None
            self.render_main()
        finally:
            if self._in_flight == scope:
                self._in_flight = None
            if self._pending and self._in_flight is None:
                self._pending = False
                self.request_refresh()

    @work(group="activity-journal", exclusive=True, exit_on_error=False)
    async def watch_journal(self):
        try:
            async for event in self.broker.events():
                data = event.get("data") if isinstance(event, dict) else None
                data = data if isinstance(data, dict) else {}
                project = (self._scope or (None,))[0]
                if project and data.get("project_id") == project and data.get("entity_type") in JOURNAL:
                    if self._journal_timer is not None:
                        self._journal_timer.stop()
                    self._journal_timer = self.set_timer(DEBOUNCE, self._journal_refresh)
        except Exception:  # the stream is gone: the 15 s poll goes on; try the stream again later
            self.set_timer(FALLBACK, self.watch_journal)

    def _journal_refresh(self):
        self._journal_timer = None
        self.request_refresh()

    def watch_notices(self):
        with suppress(Exception):
            signature = terminal_notice_feed.signature(self.settings, self.workspace_id)
            if signature != self._feed_signature:
                self._feed_signature = signature
                self.notices = terminal_notice_feed.read_all(self.settings, self.workspace_id)
                self.render_notices()

    # ----- rows --------------------------------------------------------------------------------
    def _sentence(self, text, width):
        """The only text that wraps: every line keeps the indent of the section."""
        lines = textwrap.wrap(self.t(text), max(8, width - 2)) or [""]
        return (styled("\n".join("  " + line for line in lines), "muted"), None)

    def _title(self, text):
        return (styled(self.t(text), "accent"), TITLE)

    def main_rows(self, width):
        if self.status == "none":
            return [self._sentence("Choose a project in the sidebar to see its activity.", width)]
        small = self.size.height < terminal_theme.SHORT_HEIGHT  # a short popup shows two rows per group
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
            line = self.t("Stage: {stage}", stage=self.t(STAGES[stage["reason"]]))
            if slot.get("run_id") and slot.get("lifecycle") == "running":
                line += " · " + self.t("{client} running", client=first_line(slot.get("client") or ""))
            rows.append((styled(fit("  " + line, width), "label"), ROW))
            task = stage.get("task") if isinstance(stage.get("task"), dict) else {}
            if first_line(task.get("content")):
                rows.append((styled(fit("  " + first_line(task.get("content")), width), "stored"),
                             ("record", task.get("id"))))
        rows.append((styled("", "value"), None))
        rows.append(self._title("Plan"))
        if self.status in {"loading", "unavailable"}:
            rows.append(self._sentence("Loading…" if self.status == "loading" else "Memory service unavailable; retrying.", width))
        else:
            plan = stage.get("plan") if isinstance(stage.get("plan"), dict) else None
            if not plan or not plan.get("id"):
                rows.append(self._sentence("No active plan.", width))
            else:
                done, total = plan_progress(self.tasks, plan["id"])
                rows.append((styled(fit("  " + first_line(plan.get("title") or plan["id"]), width), "stored"),
                             ("plan", plan["id"])))
                rows.append(self._sentence("This plan has no steps yet.", width) if not total else
                            (styled("  " + self.t("{done} of {total} steps done", done=done, total=total), "label"), None))
            finished = finished_plans(self.tasks, self.plans, plan.get("id") if plan else None)
            if finished:
                rows.append((styled("  " + self.t("Finished recently"), "label"), None))
                for row in finished:
                    tail = f" {row['date']} {row['count']}/{row['count']}"
                    title = fit(first_line(row["title"]), max(1, width - 3 - cell_len(tail)))
                    pad = " " * max(0, width - 3 - cell_len(title) - cell_len(tail))
                    rows.append((assemble([("value", "   "), ("stored", title + pad), ("label", tail)]), ("plan", row["id"])))
        rows.append((styled("", "value"), None))
        rows.append(self._title("Tasks"))
        if self.status in {"loading", "unavailable"}:
            rows.append(self._sentence("Loading…" if self.status == "loading" else "Memory service unavailable; retrying.", width))
        elif not live_tasks(self.tasks):
            rows.append(self._sentence("No tasks in this project yet.", width))
        else:
            for group in group_tasks(self.tasks, small=small):
                rows.append((assemble([("value", "  "), (state_style(group["state"]), self.t(group["label"])),
                                       ("label", f" {group['total']}")]), None))
                for task in group["items"]:
                    rows.append((assemble([("label", "   · "), ("stored", fit(first_line(task.get("content")) or task.get("id", ""),
                                                                              width - 5))]), ("record", task.get("id"))))
                if group["hidden"]:
                    rows.append((styled("   " + self.t("+ {count} more", count=group["hidden"]), "muted"), None))
        return rows

    def notice_rows(self, width):
        rows = [self._title("Notices")]
        if not self.notices:
            return rows + [self._sentence("No notices since the workspace opened.", width)]
        words = {kind: self.t(word) for kind, word in KIND_WORDS.items()}
        pad = max(cell_len(word) for word in words.values())
        project, folder = self._scope or (None, None)
        today = datetime.now().date()
        for entry in self.notices:
            stamp = entry["when"].strftime("%H:%M" if entry["when"].date() == today else "%m-%d")
            label = ""
            if entry["project_id"] != project or (entry["worktree"] and folder and entry["worktree"] != folder):
                label = "[" + first_line(entry["project_name"] or entry["project_id"] or self.t("No project")) + "] "
            text = label + first_line(entry["text"])
            count = f" (×{entry['count']})" if entry["count"] > 1 else ""
            word, role = words[entry["kind"]], _KIND_ROLE[entry["kind"]]
            if width < NARROW:  # time and kind above the text, so the text keeps the whole width
                parts = [("label", f"  {stamp} "), (role, word), ("label", count), ("value", "\n    "),
                         ("stored", fit(text, width - 4))]
            else:
                head = f"  {stamp} "
                room = width - cell_len(head) - pad - 1 - cell_len(count)
                parts = [("label", head), (role, word + " " * (pad - cell_len(word) + 1)), ("stored", fit(text, room)),
                         ("label", count)]
            rows.append((assemble(parts), ROW))
        return rows

    # ----- rendering ---------------------------------------------------------------------------
    def render_all(self):
        self.render_header()
        self.render_main()
        self.render_notices()

    def render_header(self):
        with suppress(Exception):
            project, folder = self._scope or (None, None)
            width = max(10, self.query_one("#activity").content_region.width)
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
            self.query_one("#activity-folder", Static).update(
                styled(fit(first_line(PurePath(folder).name) if folder else "", width), "muted"))

    def _fill(self, identifier, build):
        with suppress(Exception):
            widget = self.query_one("#" + identifier, ActivityList)
            width = widget.scrollable_content_region.width - 1  # a column for the scrollbar
            if width < 8:
                return
            rows = build(width)
            signature = (width, self.language, tuple(text.plain for text, _ in rows),
                         tuple(payload for _, payload in rows))
            if signature == self._signatures.get(identifier):
                return
            self._signatures[identifier] = signature
            highlighted = widget.highlighted
            widget.clear_options()
            # Titles, blank rows and sentences are not stops of the cursor; every item row is.
            widget.add_options([Option(text, disabled=payload in {None, TITLE}) for text, payload in rows])
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
        """One column: Notices follow the other sections, and both lists scroll."""
        with suppress(Exception):
            main, notes = self.query_one("#activity-main", ActivityList), self.query_one("#activity-notices", ActivityList)
            if self.has_class("-columns"):
                main.styles.max_height = notes.styles.max_height = None
                return
            body = self.query_one("#activity-body").content_region.height
            def lines(widget):
                return sum(str(widget.get_option_at_index(i).prompt).count("\n") + 1 for i in range(widget.option_count))
            # The other sections first; Notices get what is left, but never less than a quarter of the column.
            shown = max(3, min(lines(notes), max(body - lines(main) - 1, int(body * .25))))
            if (notes.styles.max_height, main.styles.max_height) != (shown, max(3, body - shown - 1)):
                notes.styles.max_height = shown
                main.styles.max_height = max(3, body - shown - 1)

    def refresh_language(self):
        self._signatures.clear()
        self.render_all()

    # ----- keys --------------------------------------------------------------------------------
    def action_section(self, step):
        """Move the cursor to the first item of the next (or previous) section, across both lists."""
        stops, here = [], (-1, -1)
        for order, identifier in enumerate(("activity-main", "activity-notices")):
            widget = self.query_one("#" + identifier, ActivityList)
            payloads = self._payloads.get(identifier, {})
            titles = sorted(index for index, payload in payloads.items() if payload == TITLE)
            for start, end in zip(titles, [*titles[1:], widget.option_count]):
                target = next((index for index in range(start + 1, end) if payloads.get(index) not in {None, TITLE}), None)
                if target is not None:
                    stops.append(((order, target), widget))
            if widget.has_focus and widget.highlighted is not None:
                here = (order, widget.highlighted)
        if not stops:
            return
        if step > 0:
            key, widget = next((stop for stop in stops if stop[0] > here), stops[0])
        else:
            current = max((index for index, stop in enumerate(stops) if stop[0] <= here), default=0)
            key, widget = stops[current - 1]
        widget.focus()
        widget.highlighted = key[1]

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
