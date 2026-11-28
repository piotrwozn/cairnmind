"""Terminal-only workspace sidebar and standalone panel entrypoints."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.widgets import Button, DataTable, Footer, Header, Input, Static

from cairnmind.config import Settings
from cairnmind.mcp.mcp_failure_notice import announce as announce_mcp_failures
from cairnmind.memory.project_folders import folder_notice
from cairnmind.pal.service_token import FOREIGN_SERVICE, foreign_service
from cairnmind.pal.terminal_safe import plain
from cairnmind.terminal import terminal_theme
from cairnmind.terminal.terminal_connection_hints import NOT_STARTED, connection_hint
from cairnmind.terminal.terminal_i18n import native_failure_notice
from cairnmind.terminal.terminal_panels_actions import _SidebarActionsMixin
from cairnmind.terminal.terminal_panels_modals import (
    ConfirmModal,
    CredentialModal,
    DiscoveryModal,
    profile_fields,
    profile_from_form,
)
from cairnmind.terminal.terminal_ui import BasePanel, ChoiceModal

__all__ = [
    "ConfirmModal",
    "CredentialModal",
    "DiscoveryModal",
    "SidebarApp",
    "main",
    "profile_fields",
    "profile_from_form",
]


class SidebarApp(_SidebarActionsMixin, BasePanel):
    panel_role = "sidebar"
    TITLE = "Cairnmind"
    CSS = BasePanel.CSS + terminal_theme.css("""
    #sidebar-main { height: 1fr; padding: 0 1; overflow-y: auto; }
    #sidebar-heading { height: auto; color: $cm-accent; text-style: bold; }
    #project-search { height: 1; }
    #project-rows { height: 1fr; min-height: 4; }
    #project-state, #sidebar-help { height: auto; color: $cm-muted; }
    #sidebar-empty { height: 0; color: $cm-muted; }
    #sidebar-main.-empty #project-rows { height: 1; min-height: 1; }
    #sidebar-main.-empty #sidebar-empty { height: 1fr; content-align: center middle; text-align: center; }
    #sidebar-scope { height: auto; max-height: 5; margin-top: 1; }
    #project-state { margin-bottom: 1; }
    #project-state.-warn { color: $cm-warn; }
    #project-page { height: 1fr; }
    #project-page Button { max-width: 44; }
    #project-page #project-back { border-left: none; background: transparent; color: $cm-muted; }
    #project-page #project-back:hover { color: $cm-text; }
    #project-page #project-back:focus { background: $cm-raised; color: $cm-strong; text-style: bold; }
    #assistant-row { height: 1; max-width: 44; }
    #project-page #assistant-select { width: 1fr; background: $cm-raised; color: $cm-text;
        border-left: thick $cm-border-hi; }
    #project-page #assistant-select:focus { background: $cm-raised; color: $cm-strong; text-style: bold;
        border-left: thick $cm-accent; }
    #assistant-mark { width: 3; height: 1; background: $cm-raised; color: $cm-muted; content-align: center middle; }
    #assistant-row:focus-within #assistant-mark { color: $cm-accent; }
    #project-page #memory-open { margin-top: 1; }
    .sidebar-actions { height: 1; margin-top: 0; max-width: 45; }
    #project-list-actions { margin-top: 1; }
    .sidebar-actions Button { width: 1fr; min-width: 8; margin: 0 1 0 0; padding: 0; content-align: left middle;
        text-align: left; }
    .narrow .sidebar-actions { layout: vertical; height: 2; max-width: 100%; }
    .narrow .sidebar-actions Button { width: 100%; height: 1; margin: 0; }
    .narrow #project-list-actions { margin-top: 0; }
    #panel-notice, #notice-mark { padding: 0; }
    .narrow Footer { display: none; }
    """)
    BINDINGS = [
        ("w", "work", "Start work"),
        ("p", "inspect", "Open"),
        ("r", "refresh", "Refresh"),
        ("m", "memory", "Memory"),
        ("escape", "projects", "Projects"),
        ("slash", "search", "Search"),
    ]

    def __init__(self, settings, workspace_id="default", *, broker=None, workspace=None):
        super().__init__(settings, workspace_id, broker=broker, workspace=workspace)
        self.data = {"projects": [], "runs": [], "profiles": [], "settings": {}}
        self.projects = {}
        self._last_render_signature = None
        self.page = "projects"
        self.selected_slot_id = None
        self._selected_scope = None
        self._primary_run = None
        self._launch_feedback = None
        self._launch_feedback_state = None
        self._longest_name = 0

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="sidebar-main"):
            yield Static(self.t("Projects"), id="sidebar-heading", markup=False)
            yield Input(placeholder=self.t("Search projects…"), id="project-search")
            yield DataTable(id="project-rows", cursor_type="row", show_header=False)
            yield Static("", id="sidebar-empty", markup=False)
            # One button per row: no fill until focused (`cm-stack`), so the rows stay apart.
            with VerticalScroll(id="project-page", classes="cm-stack"):
                yield Button(self.t("Return to projects"), id="project-back")
                yield Static("", id="sidebar-scope", markup=False)
                yield Static(self.t("Browsing memory does not start an assistant."), id="project-state", markup=False)
                with Horizontal(id="assistant-row"):  # a selector: its mark is a glyph, not part of the label
                    yield Button(self.t("Choose assistant"), id="assistant-select")
                    yield Static("▼", id="assistant-mark", markup=False)
                yield Button(self.t("Start work"), id="project-work", variant="primary", disabled=True)
                yield Button(self.t("Memory"), id="memory-open")
                yield Button(self.t("Add assistant"), id="assistant-add")
                # Last, so the Tab order of the existing buttons is unchanged.
                yield Button(self.t("Run next stage"), id="stage-next")
            # Two rows of two: no fill until focused (`cm-stack`), so the rows do not merge into a slab.
            with Horizontal(classes="sidebar-actions cm-stack", id="project-list-actions"):
                yield Button(self.t("Add folder"), id="project-add", variant="primary")
                yield Button(self.t("Open"), id="project-inspect", disabled=True)
            with Horizontal(classes="sidebar-actions cm-stack"):
                yield Button(self.t("Connections"), id="auth-open")
                yield Button(self.t("More"), id="more-open")
            yield Static("", id="panel-notice", markup=False)
            yield Static("", id="notice-mark", markup=False)
            yield Static(self.t("Ctrl+B then Space: menu · Tab: next · Esc: back"), id="sidebar-help", markup=False)
        yield Footer()

    def on_mount(self):
        self.query_one("#project-rows", DataTable).add_column(self.t("Project"), key="project")
        self.query_one("#project-back", Button).styles.line_pad = 0  # the label starts at the text column
        self.show_page("projects")
        self.set_interval(.3, self._poll_workspace)
        self.set_interval(3, self.refresh_sidebar)
        from cairnmind.terminal.terminal_setup_change import (
            show_last_outcome,  # the end of a change nobody watched, once (#339)
        )
        show_last_outcome(self)
        self.start_panel()

    def on_resize(self, event: Resize):
        self.screen.set_class(event.size.width < 40, "narrow")
        self.call_after_refresh(self.fill_project_rows)

    def fill_project_rows(self):
        """The cursor row spans the list; names longer than the pane keep their full text."""
        for table in self.query("#project-rows"):
            terminal_theme.fill_column(table, "project", self._longest_name)

    async def ready_panel(self):
        await self.load_state()

    async def workspace_changed(self, value):
        self.render_project()
        if value.get("launch_errors") or value.get("last_error"):
            self.post_launch_notice(value)
        else:
            self._update_launch_feedback(value)
        from cairnmind.terminal.terminal_record_kinds import check_partial

        check_partial(self, value)  # an unknown transcript record kind that blocks the packet (#210)

    def post_launch_notice(self, value, about=None):
        """The launch problem, once while the line shows it: the state poll and Start work, Open or Run
        next stage all report it, and a change of the state that does not change the text adds nothing
        to Notices. `about` is the project a press was for (see notice_for)."""
        text = self.launch_notice(value)
        if str(self.query_one("#panel-notice", Static).render()) != self.t(text):
            self.notice_for(about, text) if about else self.notice(text)

    def notice(self, value, kind="info"):
        # A form result or another action owns its own message. Later lifecycle
        # polls must not overwrite it with feedback from an earlier Start.
        self._launch_feedback = None
        self._launch_feedback_state = None
        super().notice(value, kind)
        self.keep_focus_in_view()

    def keep_focus_in_view(self):
        """A notice of several rows shortens the scrolling project page: the focused button stays visible."""
        def show():
            focused = self.focused
            if focused is not None and focused.is_attached:
                focused.scroll_visible(animate=False)

        def after_layout():
            show()
            self.call_after_refresh(show)

        self.call_after_refresh(after_layout)

    def selected_native_status(self, value=None):
        """Read the exact selected slot from fresh workspace data, not cached GETs."""
        value = self.workspace_data if value is None else value
        configured = next(
            (s for s in self.configured_slots(value.get("project_id")) if s["id"] == self.selected_slot_id),
            None,
        )
        profile = next(
            (p for p in self.data.get("profiles", []) if configured and p["id"] == configured["profile_id"]),
            None,
        )
        slot = next((s for s in value.get("slots", []) if s["id"] == self.selected_slot_id), None)
        if (
            not profile
            or not slot
            or slot.get("client") != profile.get("client")
            or not isinstance(slot.get("run_id"), str)
            or not slot["run_id"]
            or type(slot.get("generation")) is not int
            or slot["generation"] < 1
        ):
            return None
        return {
            "id": slot["run_id"],
            "generation": slot["generation"],
            "client": slot["client"],
            "lifecycle": slot.get("lifecycle"),
            "exit_code": slot.get("exit_code"),
        }

    def _update_launch_feedback(self, value=None, *, begin=False):
        value = self.workspace_data if value is None else value
        run = self.selected_native_status(value)
        identity = (
            (value.get("project_id"), value.get("worktree"), self.selected_slot_id, run["id"], run["generation"])
            if run
            else None
        )
        if begin:
            self._launch_feedback = identity
            self._launch_feedback_state = None
            if identity is None:
                self.notice("Start requested. Review the workspace.")
                return
        if self._launch_feedback is None:
            return
        if identity != self._launch_feedback or run is None:
            self._launch_feedback = None
            self._launch_feedback_state = None
            super().notice("")
            return
        signature = (run.get("lifecycle"), self.language)
        if signature == self._launch_feedback_state:
            return
        self._launch_feedback_state = signature
        message = native_failure_notice(run, self.language)
        if not message:
            message = (
                "Starting the selected assistant…"
                if run["lifecycle"] == "starting"
                else "Assistant started."
                if run["lifecycle"] == "running"
                else "Session ended or changed. Review the project before choosing Start work."
            )
        super().notice(message)
        self.keep_focus_in_view()

    def launch_notice(self, value):
        codes = [row.get("code") for row in value.get("launch_errors", [])]
        if "background_inference_deferred" in codes:
            return "Assistant started. Memory processing may still be paused. Use More → Queue → Process memory."
        if hint := connection_hint(codes, self.t):
            return self.t(NOT_STARTED) + " " + hint  # what to do, not only that something needs attention
        return self.t("Operation needs attention.") + " " + self.t("Connections")

    def show_page(self, page):
        self.page = page
        for selector in ("#project-search", "#project-rows", "#sidebar-empty", "#project-list-actions"):
            self.query_one(selector).display = page == "projects"
        self.query_one("#project-page").display = page == "project"
        self.query_one("#sidebar-heading", Static).update(self.t("Projects" if page == "projects" else "Project"))
        self.query_one("#project-rows" if page == "projects" else "#assistant-select").focus()

    def action_projects(self):
        self.show_page("projects")

    def action_search(self):
        self.show_page("projects")
        self.query_one("#project-search", Input).focus()

    def on_key(self, event):
        # Down in the search field goes into the list it filters.
        if event.key == "down" and getattr(self.focused, "id", None) == "project-search":
            rows = self.query_one("#project-rows", DataTable)
            if rows.row_count:
                event.stop()
                rows.focus()

    def configured_slots(self, project_id=None):
        settings = self.data.get("settings", {})
        project_id = project_id or self.workspace_data.get("project_id")
        return settings.get("project_slots", {}).get(project_id, settings.get("default_slots", []))

    def render_project(self):
        project_id = self.workspace_data.get("project_id")
        scope = (project_id, self.workspace_data.get("worktree"))
        if self._selected_scope != scope:
            self._selected_scope = scope
            self.selected_slot_id = None
        profiles = {p["id"]: p for p in self.data.get("profiles", [])}
        slots = [s for s in self.configured_slots(project_id) if s["profile_id"] in profiles]
        remembered = next(
            (
                row["slot_id"]
                for row in self.workspace_data.get("assistant_selections", [])
                if (row["project_id"], row["worktree"]) == scope
            ),
            None,
        )
        valid = {s["id"] for s in slots}
        if remembered in valid:
            self.selected_slot_id = remembered
        elif remembered is not None or self.selected_slot_id not in valid:
            live = next((s for s in slots if self.reusable_run(s, profiles[s["profile_id"]])), None)
            self.selected_slot_id = live["id"] if live else (slots[0]["id"] if slots else None)
        selected = next((s for s in slots if s["id"] == self.selected_slot_id), None)
        profile = next(
            (p for p in self.data.get("profiles", []) if selected and p["id"] == selected["profile_id"]),
            None,
        )
        # Two parts this panel knows apart: the name stands out, the path is quiet. The text is unchanged.
        self.query_one("#sidebar-scope", Static).update(
            terminal_theme.assemble(
                [
                    ("title", self.workspace_data.get("project_name") or self.t("Project")),
                    ("value", "\n"),
                    ("muted", self.workspace_data.get("worktree") or ""),
                ]
            )
        )
        self.query_one("#assistant-select", Button).label = plain(
            profile["label"] if profile else self.t("Choose assistant")
        )
        self.query_one("#project-work", Button).disabled = not (project_id and profile)
        live = self.reusable_run(selected, profile)
        fresh = self.selected_native_status()
        if fresh and fresh["lifecycle"] in {"failed", "interrupted", "exited"}:
            live = None
        self._primary_run = dict(live) if live else None
        label = "Resume session" if live else "Start work"
        self.query_one("#project-work", Button).label = self.t(label)
        # Textual's footer and later locale changes must describe the same action.
        from dataclasses import replace

        for index, binding in enumerate(self._bindings.key_to_bindings.get("w", [])):
            self._bindings.key_to_bindings["w"][index] = replace(binding, description=self.t(label))
            labels = getattr(self, "_cairnmind_binding_labels", {})
            key = ("w", index, binding.action)
            original = labels.get(key, (binding.description, binding.tooltip, ""))
            labels[key] = (label, original[1], original[2])
        self.refresh_bindings()
        failure = native_failure_notice(fresh or {}, self.language)
        # A folder this machine cannot read is named in the person's language, by project (#371).
        project_row = self.projects.get(project_id) if isinstance(project_id, str) else None
        folder = folder_notice((project_row or {}).get("unavailable") or [], self.language)
        self.query_one("#project-state", Static).set_class(bool(failure or folder) and not live, "-warn")
        self.query_one("#project-state", Static).update(
            self.t("Resume returns to the running process. Model changes apply to its next launch.")
            if live
            else failure or folder or self.t("Browsing memory does not start an assistant.")
        )

    def reusable_run(self, slot=None, profile=None):
        """Mirror NativeRuntime.start's live-slot identity, not profile/model edits."""
        if slot is None:
            slot = next((s for s in self.configured_slots() if s["id"] == self.selected_slot_id), None)
        if profile is None:
            profile = next(
                (p for p in self.data.get("profiles", []) if slot and p["id"] == slot["profile_id"]),
                None,
            )
        if not slot or not profile:
            return None
        from cairnmind.terminal.workspace import reusable_native_run

        selected = self.workspace_data
        return reusable_native_run(
            self.data.get("runs", []),
            selected.get("project_id"),
            selected.get("worktree"),
            profile["client"],
            slot["id"],
        )

    def assistant_scope(self):
        return (self.workspace_data.get("project_id"), self.workspace_data.get("worktree"), self._scope_version)

    async def remember_assistant(self, slot_id, scope):
        """Persist a view preference only; never turn a delayed choice into work."""
        current = await asyncio.to_thread(self.workspace.read)
        if (
            scope != self.assistant_scope()
            or (current.get("project_id"), current.get("worktree")) != scope[:2]
            or not all(scope[:2])
        ):
            raise ValueError(self.t("Select a visible project first."))
        profiles = {p["id"] for p in self.data.get("profiles", [])}
        if not any(s["id"] == slot_id and s["profile_id"] in profiles for s in self.configured_slots(scope[0])):
            raise ValueError(self.t("Choose an assistant before starting work."))
        rows = [
            row
            for row in current.get("assistant_selections", [])
            if (row["project_id"], row["worktree"]) != scope[:2]
        ]
        rows.append({"project_id": scope[0], "worktree": scope[1], "slot_id": slot_id})
        await asyncio.to_thread(
            self.workspace.update, {"assistant_selections": rows[-1000:]}, expected_revision=current["revision"]
        )
        await self._poll_workspace()

    @work(group="assistant-select", exclusive=True, exit_on_error=False)
    async def choose_assistant(self):
        scope = self.assistant_scope()
        profiles = {p["id"]: p for p in self.data.get("profiles", [])}
        choices = [
            (profiles[s["profile_id"]]["label"], s["id"])
            for s in self.configured_slots()
            if s["profile_id"] in profiles
        ]
        choice = await self.push_screen_wait(ChoiceModal("Choose assistant", choices + [("Add assistant", "+")]))
        if scope != self.assistant_scope():
            return
        if choice == "+":
            self.add_assistant()
        elif choice:
            try:
                await self.remember_assistant(choice, scope)
            except (OSError, ValueError, RuntimeError) as exc:
                self.notice(exc)

    @work(group="sidebar-refresh", exclusive=True, exit_on_error=False)
    async def refresh_sidebar(self):
        try:
            await self.load_state()
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)

    async def load_state(self):
        value = await self.request("GET", "/api/state")
        self.data = value
        if value.get("foreign_service"):
            # The service refused this directory's token: name it, as the command line does (#308).
            text = self.t(FOREIGN_SERVICE).format(**foreign_service(self.settings))
            if str(self.query_one("#panel-notice", Static).render()) != text:
                self.notice(text, "error")
        self.projects = {p["id"]: p for p in value.get("projects", [])}
        self.render_projects()
        self.render_project()
        # A third-party MCP server of an agent run that did not start: one notice per server per run (#209).
        announce_mcp_failures(self, value.get("runs", []))

    def render_projects(self):
        table = self.query_one("#project-rows", DataTable)
        selected = self.current_project_id()
        query = self.query_one("#project-search", Input).value.casefold()
        rows = [p for p in self.projects.values() if query in (p["name"] + " " + p["root"]).casefold()]
        signature = json.dumps([(p["id"], p["name"], p["root"]) for p in rows], ensure_ascii=False)
        if signature == self._last_render_signature:
            return
        self._last_render_signature = signature
        table.clear()
        names = [plain(project["name"]) for project in rows]
        self._longest_name = max([name.cell_len for name in names], default=0)
        for project, name in zip(rows, names, strict=True):
            table.add_row(name, key=project["id"])
        self.call_after_refresh(self.fill_project_rows)
        if selected:
            for index, project in enumerate(rows):
                if project["id"] == selected:
                    table.move_cursor(row=index)
                    break
        self.query_one("#project-inspect", Button).disabled = not rows
        self.query_one("#sidebar-main").set_class(not rows, "-empty")
        self.query_one("#sidebar-empty", Static).update(
            ""
            if rows
            else self.t(
                "No matching projects. Clear the search or add a folder."
                if query
                else "No projects yet. Add a folder to begin."
            )
        )

    @on(Input.Changed, "#project-search")
    def search_changed(self):
        self._last_render_signature = None
        self.render_projects()

    def current_project_id(self):
        table = self.query_one("#project-rows", DataTable)
        if table.row_count:
            return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        return None

    def chosen_project(self):
        identifier = self.current_project_id() if self.page == "projects" else self.workspace_data.get("project_id")
        project = self.projects.get(identifier) if isinstance(identifier, str) else None
        if not project:
            raise ValueError(self.t("Select a visible project first."))
        return project

    async def reconcile(self):
        from cairnmind.terminal.workspace import refresh_workspace

        await asyncio.to_thread(refresh_workspace, self.settings, self.workspace_id)
        await self._poll_workspace()

    @work(group="sidebar-select", exclusive=True, exit_on_error=False)
    async def select_project(self, intent, *, focus_start=False, resume_identity=None, focus_id="project-work"):
        try:
            returning = resume_identity if intent == "work" else None
            if returning:
                # A Resume click is inspect/attach only, including when the
                # process exits between rendering the button and this request.
                await self.load_state()
                await self._poll_workspace()
                current_run = self.reusable_run()
                if not current_run or (current_run["id"], current_run["generation"]) != (
                    returning["id"],
                    returning["generation"],
                ):
                    self.notice("Session ended or changed. Review the project before choosing Start work.")
                    return
                if any(
                    returning.get(key) is not None and returning[key] != current_run.get(key)
                    for key in ("pid", "process_start")
                ):
                    self.notice("Session ended or changed. Review the project before choosing Start work.")
                    return
            project = self.chosen_project()
            roots = project.get("roots") or [project["root"]]
            selected = (
                self.workspace_data.get("worktree")
                if self.workspace_data.get("project_id") == project["id"]
                else project["root"]
            )
            if len(roots) > 1 and (self.page != "project" or selected not in roots):
                selected = await self.push_screen_wait(ChoiceModal(self.t("Working folder"), [(r, r) for r in roots]))
                if selected is None:
                    return
            requested = []
            if intent == "work" and not returning:
                slots = self.configured_slots(project["id"])
                slot_id = (
                    self.selected_slot_id
                    if self.selected_slot_id in {s["id"] for s in slots}
                    else (slots[0]["id"] if slots else None)
                )
                if not slot_id:
                    raise ValueError(self.t("Choose an assistant before starting work."))
                requested = [slot_id]
            await asyncio.to_thread(
                self.workspace.select_project,
                project,
                selected,
                intent="inspect" if returning else intent,
                requested_slot_ids=requested,
            )
            if intent == "work":
                changes: dict[str, Any] = {"focus": "client1"}
                if returning:
                    # Bring the resumed session into view without dropping another visible one (#155).
                    shown = list((await asyncio.to_thread(self.workspace.read)).get("visible_slot_ids") or [])
                    if returning["slot_id"] not in shown:
                        shown = [returning["slot_id"], *shown][:2]
                    changes.update(
                        visible_slot_ids=shown,
                        focus="client2" if shown.index(returning["slot_id"]) else "client1",
                    )
                await asyncio.to_thread(self.workspace.update, changes)
            await self.reconcile()
            if returning:
                await self.load_state()
            self.show_page("project")
            if focus_start:
                self.query_one("#" + focus_id, Button).focus()
            current = await asyncio.to_thread(self.workspace.read)
            latest_run = self.reusable_run() if returning else None
            reused = (
                returning
                and latest_run
                and all(returning.get(key) == latest_run.get(key) for key in ("id", "generation"))
                and all(
                    returning.get(key) is None or returning[key] == latest_run.get(key)
                    for key in ("pid", "process_start")
                )
                and (current.get("project_id"), current.get("worktree"))
                == (returning["project_id"], returning["worktree"])
                and any(
                    (s.get("run_id"), s.get("generation"), s.get("id"), s.get("client"))
                    == (returning["id"], returning["generation"], returning["slot_id"], returning["client"])
                    and s.get("lifecycle") in {"starting", "running", "stopping"}
                    for s in current.get("slots", [])
                )
            )
            if current.get("launch_errors") or current.get("last_error"):
                self.post_launch_notice(current)  # the state poll may have said it already
            elif intent == "work" and not returning:
                self._update_launch_feedback(current, begin=True)
            else:
                self.notice(
                    "Returned to the running session. No process was restarted."
                    if reused
                    else "Session ended or changed. Review the project before choosing Start work."
                    if returning
                    else "Project opened. Choose Resume session to return."
                    if self.reusable_run()
                    else "Project opened. Choose Start work when ready."
                )
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)

    @on(DataTable.RowSelected, "#project-rows")
    def project_row_selected(self):
        self.select_project("inspect")

    def action_work(self):
        identity = dict(self._primary_run) if self._primary_run else None
        self.select_project("work" if self.page == "project" else "inspect", resume_identity=identity)

    def action_prepare_work(self):
        # A queued palette request may outlive its original project selection.
        # Always show the current project and require a fresh local activation.
        self.select_project("inspect", focus_start=True)

    def action_prepare_next_stage(self):
        # The same rule for Run next stage: a menu request only shows the current project.
        self.select_project("inspect", focus_start=True, focus_id="stage-next")

    def action_inspect(self):
        self.select_project("inspect")

    def action_refresh(self):
        self.refresh_sidebar()

    def refresh_language(self):
        from cairnmind.terminal.terminal_i18n import localize_bindings

        localize_bindings(self, self.language)
        labels = {
            "project-back": "Return to projects",
            "assistant-add": "Add assistant",
            "project-work": "Start work",
            "project-add": "Add folder",
            "project-inspect": "Open",
            "auth-open": "Connections",
            "more-open": "More",
            "memory-open": "Memory",
            "stage-next": "Run next stage",
        }
        for identifier, label in labels.items():
            self.query_one("#" + identifier, Button).label = self.t(label)
        self.render_project()
        self.query_one("#project-search", Input).placeholder = self.t("Search projects…")
        self.query_one("#sidebar-help", Static).update(self.t("Ctrl+B then Space: menu · Tab: next · Esc: back"))
        self.query_one("#sidebar-heading", Static).update(self.t("Projects" if self.page == "projects" else "Project"))
        self._last_render_signature = None
        self._update_launch_feedback()
        self.render_projects()
        self.render_project()

    @on(Button.Pressed)
    def sidebar_action(self, event):
        actions = {
            "project-work": self.action_work,
            "project-inspect": self.action_inspect,
            "project-scan": self.scan_projects,
            "project-add": self.add_project,
            "profiles-edit": self.edit_profiles,
            "slots-edit": self.edit_slots,
            "sessions-open": self.sessions,
            "auth-open": self.login,
            "memory-open": self.action_memory,
            "memory-import": self.import_memory,
            "project-back": self.action_projects,
            "assistant-select": self.choose_assistant,
            "assistant-add": self.add_assistant,
            "more-open": self.action_more,
            "stage-next": self.action_next_stage,
        }
        action = actions.get(event.button.id)
        if action:
            action()


def main():
    parser = argparse.ArgumentParser(description="Private panel of the Cairnmind terminal workspace")
    parser.add_argument("--kind", choices=["sidebar", "inspector", "graph", "activity"], required=True)
    parser.add_argument("--workspace-id", default="default")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--popup", action="store_true")
    parser.add_argument("--run-request", action="store_true")  # a view started again by a menu request
    args = parser.parse_args()
    settings = Settings.load(args.data_dir, create=False)
    if args.kind == "activity":
        from cairnmind.terminal.terminal_activity import activity_app

        app = activity_app(settings, args.workspace_id, popup=args.popup)
    elif args.kind == "sidebar":
        app = SidebarApp(settings, args.workspace_id)
    elif args.kind == "inspector":
        from cairnmind.terminal.terminal_inspector import InspectorApp

        app = InspectorApp(settings, args.workspace_id)
    else:
        from cairnmind.terminal.terminal_graph import GraphApp

        app = GraphApp(settings, args.workspace_id)
    app.run_start_request = args.run_request
    app.run()


if __name__ == "__main__":
    main()
