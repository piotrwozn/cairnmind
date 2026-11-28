"""Standalone Textual terminal workspace for native Windows (no tmux required).

Hosts Projects, Memory Inspector, Activity, 3D Graph, and Settings in a single
Textual application backed by the same local broker (`BrokerClient`) and the
same consent/person-act rules as the POSIX tmux workspace.

While running on Windows, `run_standalone()` opens an owner-only Named Pipe
(`person_pipe.PersonPipeServer`) holding a fresh in-memory 64-hex key and
registers it in `person_proof` for this process only. Every person's act
(confirm, dispute, release, resolve conflict, confirm plan, impact verdict,
quick finding, import replace, forget project memory) goes through the normal
consent dialog and broker -> service -> Named Pipe verification path.
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import suppress

from textual import on, work
from textual.app import App, ComposeResult, ScreenStackError
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import QueryError
from textual.dom import NoScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Select, Static, TabbedContent, TabPane

from cairnmind.cli_cmds import topology
from cairnmind.pal import person_pipe
from cairnmind.pal.terminal_safe import plain, safe_text
from cairnmind.terminal import person_proof, terminal_setup_change, terminal_theme
from cairnmind.terminal.terminal_client import BrokerClient, BrokerError
from cairnmind.terminal.terminal_i18n import tr
from cairnmind.terminal.terminal_i18n_messages import broker_error_text
from cairnmind.terminal.terminal_person_act import agreed, review_text
from cairnmind.terminal.terminal_ui import ChoiceModal, Field, FormModal, MessageModal
from cairnmind.terminal.workspace_state import WorkspaceState

_INSPECTOR_VIEWS = [
    ("Records", "records"),
    ("Conflicts", "conflicts"),
    ("Plans", "plans"),
    ("Tasks", "tasks"),
    ("Checks", "checks"),
    ("Queue", "jobs"),
    ("Impact", "impact"),
    ("Proposed", "proposed"),
    ("Sources", "sources"),
]


class StandaloneWorkspaceApp(App[int]):
    """Unified Textual workspace for native Windows without tmux."""

    TITLE = "Cairnmind"
    SUB_TITLE = "Workspace"
    CSS = terminal_theme.BASE_CSS + terminal_theme.css("""
    #notice-bar { height: 1; padding: 0 1; background: $cm-panel; color: $cm-muted; }
    #workspace-tabs { height: 1fr; }
    .pane-box { height: 1fr; padding: 0 1; }
    .action-bar { height: 3; padding: 0 0 1 0; }
    .action-bar Button { margin-right: 1; }
    DataTable { height: 1fr; min-height: 6; }
    #inspector-detail-scroll { height: 1fr; min-height: 5; border-top: solid $cm-border; padding: 1 0 0 0; }
    #inspector-filter-bar { height: 3; }
    #inspector-view { width: 24; margin-right: 1; }
    #inspector-filter { width: 1fr; }
    #activity-body, #graph-body, #settings-body { height: 1fr; padding: 1; }
    """)
    BINDINGS = [
        ("1", "tab('tab-projects')", "Projects"),
        ("2", "tab('tab-inspector')", "Memory"),
        ("3", "tab('tab-activity')", "Activity"),
        ("4", "tab('tab-graph')", "Graph"),
        ("5", "tab('tab-settings')", "Settings"),
        ("f", "quick_finding", "Finding"),
        ("r", "refresh_all", "Refresh"),
        ("q", "quit_workspace", "Quit"),
    ]

    def __init__(self, settings, workspace_id: str = "default", *, broker=None, workspace=None,
                 language: str | None = None):
        super().__init__()
        self.settings = settings
        self.workspace_id = workspace_id
        self.broker = broker if broker is not None else BrokerClient(settings)
        self.workspace = workspace if workspace is not None else WorkspaceState(settings, workspace_id)
        self.language = language or (self.workspace.read().get("language") or "en")
        self.projects: list[dict] = []
        self.selected_project: dict | None = None
        self.inspector_view: str = "records"
        self.inspector_rows: list[dict] = []
        self.selected_row: dict | None = None

    def t(self, text: str, **values) -> str:
        return tr(text, self.language, **values)

    def notice(self, message) -> None:
        text = safe_text(str(message), limit=220)
        with suppress(QueryError, ScreenStackError, NoScreen, AttributeError, RuntimeError, ValueError):
            self.query_one("#notice-bar", Static).update(plain(text))

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(plain(self.t("Loading workspace…")), id="notice-bar", markup=False)
        with TabbedContent(initial="tab-projects", id="workspace-tabs"):
            with TabPane(self.t("Projects"), id="tab-projects"), Vertical(classes="pane-box"):
                with Horizontal(classes="action-bar"):
                    yield Button(self.t("Add project"), id="btn-add-project", variant="primary")
                    yield Button(self.t("Quick finding"), id="btn-finding")
                    yield Button(self.t("Refresh"), id="btn-refresh")
                    yield Button(self.t("Delete project memory"), id="btn-forget-project")
                yield DataTable(id="projects-table", cursor_type="row", zebra_stripes=True)
            with TabPane(self.t("Memory"), id="tab-inspector"), Vertical(classes="pane-box"):
                with Horizontal(id="inspector-filter-bar"):
                    yield Select([(self.t(lbl), val) for lbl, val in _INSPECTOR_VIEWS],
                                 value="records", allow_blank=False, id="inspector-view")
                    yield Input(placeholder=self.t("Filter loaded page…"), id="inspector-filter")
                yield DataTable(id="inspector-table", cursor_type="row", zebra_stripes=True)
                with Horizontal(classes="action-bar"):
                    yield Button(self.t("Confirm"), id="btn-confirm", variant="primary")
                    yield Button(self.t("Dispute"), id="btn-dispute")
                    yield Button(self.t("Release"), id="btn-release")
                    yield Button(self.t("Resolve"), id="btn-resolve")
                with VerticalScroll(id="inspector-detail-scroll"):
                    yield Static("", id="inspector-detail", markup=False)
            with TabPane(self.t("Activity"), id="tab-activity"), VerticalScroll(id="activity-body"):
                yield Static(plain(self.t("Select a project to view activity.")),
                             id="activity-text", markup=False)
            with TabPane(self.t("Graph"), id="tab-graph"), VerticalScroll(id="graph-body"):
                yield Static(plain(self.t("Select a project to inspect the memory graph.")),
                             id="graph-text", markup=False)
            with TabPane(self.t("Settings"), id="tab-settings"), Vertical(id="settings-body"):
                yield Static("", id="settings-summary", markup=False)
                with Horizontal(classes="action-bar"):
                    yield Button(self.t("Change where storage and models run"),
                                 id="btn-change-setup", variant="primary")
                    yield Button("EN / PL", id="btn-toggle-lang")
        yield Footer()

    def on_mount(self) -> None:
        pt = self.query_one("#projects-table", DataTable)
        pt.add_columns(self.t("Project"), self.t("Folder"), self.t("ID"))
        it = self.query_one("#inspector-table", DataTable)
        it.add_columns(self.t("ID"), self.t("Kind"), self.t("Status"), self.t("Summary"))
        self._render_settings()
        self.load_all()
        terminal_setup_change.show_last_outcome(self)

    def _render_settings(self) -> None:
        lines = [
            f"Workspace: Standalone Textual ({'Windows Named Pipe' if sys.platform == 'win32' else 'local'})",
            f"Data directory: {self.settings.data_dir}",
            f"Language: {self.language}",
            "",
        ]
        try:
            topo = topology.topology(self.settings)
            lines.extend(topo.get("lines", []))
        except Exception as exc:
            lines.append(f"Topology unavailable: {exc}")
        with suppress(QueryError, ScreenStackError, NoScreen, AttributeError, RuntimeError, ValueError):
            self.query_one("#settings-summary", Static).update(plain("\n".join(lines)))

    def action_tab(self, tab_id: str) -> None:
        with suppress(QueryError, ScreenStackError, NoScreen, AttributeError, RuntimeError, ValueError):
            self.query_one("#workspace-tabs", TabbedContent).active = tab_id

    def action_refresh_all(self) -> None:
        self.load_all()

    def action_quit_workspace(self) -> None:
        self.exit(0)

    def action_quick_finding(self) -> None:
        self.open_finding_form()

    def refresh_sidebar(self) -> None:
        """Called by terminal_setup_change after a storage/model switch."""
        self._render_settings()
        self.load_all()

    async def _req(self, method: str, path: str, *, params=None, json=None, timeout: int = 20):  # noqa: ASYNC109 - broker request timeout forwarded to thread
        return await asyncio.to_thread(
            self.broker.request, method, path, params=params, json=json, timeout=timeout
        )

    @work(group="standalone-load", exclusive=True, exit_on_error=False)
    async def load_all(self) -> None:
        try:
            state = await self._req("GET", "/api/state")
            self.projects = list(state.get("projects") or [])
            pt = self.query_one("#projects-table", DataTable)
            pt.clear()
            for proj in self.projects:
                pt.add_row(
                    plain(safe_text(proj.get("name", ""))),
                    plain(safe_text(proj.get("root", ""))),
                    plain(safe_text(proj.get("id", ""))),
                    key=proj.get("id"),
                )
            if self.projects:
                current_id = self.selected_project["id"] if self.selected_project else None
                self.selected_project = next(
                    (p for p in self.projects if p.get("id") == current_id), self.projects[0]
                )
                await self._load_project_views()
                self.notice(f"{len(self.projects)} project(s) loaded · {self.selected_project.get('name', '')}")
            else:
                self.selected_project = None
                self.notice(self.t("No projects registered yet. Use Add project to register a folder."))
        except (BrokerError, OSError, ValueError) as exc:
            self.notice(broker_error_text(exc, self.t))

    async def _load_project_views(self) -> None:
        if not self.selected_project:
            return
        pid = self.selected_project["id"]
        await self._load_inspector(pid)
        await self._load_activity(pid)
        await self._load_graph(pid)

    async def _load_inspector(self, pid: str) -> None:
        endpoint = f"/api/inspector/{self.inspector_view}"
        try:
            data = await self._req("GET", endpoint, params={"project": pid, "limit": 100})
        except (BrokerError, OSError, ValueError) as exc:
            self.notice(broker_error_text(exc, self.t))
            return
        items = data.get("items") or data.get("records") or data.get(self.inspector_view) or []
        if not isinstance(items, list):
            items = []
        self.inspector_rows = [row for row in items if isinstance(row, dict)]
        self._populate_inspector_table()

    def _populate_inspector_table(self) -> None:
        query = ""
        with suppress(QueryError, ScreenStackError, NoScreen, AttributeError, RuntimeError, ValueError):
            query = self.query_one("#inspector-filter", Input).value.strip().lower()
        table = self.query_one("#inspector-table", DataTable)
        table.clear()
        self.selected_row = None
        shown: list[tuple[int, dict, str]] = []
        for idx, row in enumerate(self.inspector_rows):
            summary = str(row.get("content") or row.get("title") or row.get("summary")
                          or row.get("uri") or row.get("kind") or "")
            if query and query not in summary.lower() and query not in str(row.get("id", "")).lower():
                continue
            shown.append((idx, row, summary))
        for idx, row, summary in shown:
            table.add_row(
                plain(safe_text(str(row.get("id", idx)), limit=24)),
                plain(safe_text(str(row.get("kind", self.inspector_view)), limit=18)),
                plain(safe_text(str(row.get("status") or row.get("state") or ""), limit=18)),
                plain(safe_text(summary.replace("\n", " "), limit=90)),
                key=str(idx),
            )
        if shown:
            selected = shown[0][1]
            self.selected_row = selected
            self._show_row_detail(selected)
        else:
            with suppress(QueryError, ScreenStackError, NoScreen, AttributeError, RuntimeError, ValueError):
                self.query_one("#inspector-detail", Static).update("")

    def _show_row_detail(self, row: dict) -> None:
        lines = [f"{k}: {safe_text(str(v), limit=400)}" for k, v in row.items() if v is not None]
        with suppress(QueryError, ScreenStackError, NoScreen, AttributeError, RuntimeError, ValueError):
            self.query_one("#inspector-detail", Static).update(plain("\n".join(lines)))

    async def _load_activity(self, pid: str) -> None:
        project_name = self.selected_project.get("name", pid) if self.selected_project else pid
        lines = [f"Project: {project_name}", ""]
        try:
            plans = await self._req("GET", "/api/inspector/plans", params={"project": pid, "limit": 10})
            plan_items = plans.get("items") or []
            lines.append(f"Plans ({len(plan_items)}):")
            for plan in plan_items[:5]:
                lines.append(f"  - [{plan.get('status', '')}] {safe_text(str(plan.get('title', plan.get('id', ''))))}")
        except (BrokerError, OSError, ValueError):
            lines.append("Plans: unavailable")
        try:
            tasks = await self._req("GET", "/api/inspector/tasks", params={"project": pid, "limit": 20})
            task_items = tasks.get("items") or []
            lines.append("")
            lines.append(f"Tasks ({len(task_items)}):")
            for task in task_items[:12]:
                state = (task.get("metadata") or {}).get("state") or task.get("status") or ""
                lines.append(f"  - [{state}] {safe_text(str(task.get('content', task.get('id', ''))), limit=100)}")
        except (BrokerError, OSError, ValueError):
            lines.append("Tasks: unavailable")
        with suppress(QueryError, ScreenStackError, NoScreen, AttributeError, RuntimeError, ValueError):
            self.query_one("#activity-text", Static).update(plain("\n".join(lines)))

    async def _load_graph(self, pid: str) -> None:
        try:
            graph = await self._req("GET", "/api/inspector/graph", params={"project": pid, "limit": 120})
            nodes = graph.get("nodes") or []
            edges = graph.get("edges") or []
            project_name = self.selected_project.get("name", pid) if self.selected_project else pid
            lines = [
                f"Memory graph · {project_name}",
                f"Nodes: {len(nodes)} · Edges: {len(edges)}",
                "",
            ]
            for node in nodes[:30]:
                kind = node.get("kind") or node.get("type") or "node"
                lbl = safe_text(str(node.get("label") or node.get("id") or ""), limit=80)
                lines.append(f"  [{kind}] {lbl}")
            with suppress(QueryError, ScreenStackError, NoScreen, AttributeError, RuntimeError, ValueError):
                self.query_one("#graph-text", Static).update(plain("\n".join(lines)))
        except (BrokerError, OSError, ValueError) as exc:
            with suppress(QueryError, ScreenStackError, NoScreen, AttributeError, RuntimeError, ValueError):
                self.query_one("#graph-text", Static).update(plain(broker_error_text(exc, self.t)))

    @on(DataTable.RowSelected, "#projects-table")
    def on_project_selected(self, event: DataTable.RowSelected) -> None:
        key = str(event.row_key.value)
        found = next((p for p in self.projects if p.get("id") == key), None)
        if found:
            self.selected_project = found
            self.run_worker(self._load_project_views(), group="standalone-views", exclusive=True)
            self.notice(f"Selected project: {found.get('name', key)}")

    @on(DataTable.RowHighlighted, "#inspector-table")
    def on_inspector_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key and event.row_key.value is not None:
            idx = int(str(event.row_key.value))
            if 0 <= idx < len(self.inspector_rows):
                self.selected_row = self.inspector_rows[idx]
                self._show_row_detail(self.selected_row)

    @on(Select.Changed, "#inspector-view")
    def on_view_changed(self, event: Select.Changed) -> None:
        if isinstance(event.value, str):
            self.inspector_view = event.value
            if self.selected_project:
                self.run_worker(self._load_inspector(self.selected_project["id"]),
                                group="standalone-inspector", exclusive=True)

    @on(Input.Changed, "#inspector-filter")
    def on_filter_changed(self, _event: Input.Changed) -> None:
        self._populate_inspector_table()

    @work(group="standalone-finding", exclusive=True, exit_on_error=False)
    async def open_finding_form(self) -> None:
        if not self.selected_project:
            self.notice(self.t("Select a project first."))
            return
        fields = [
            Field("kind", self.t("Kind (decision, fact, constraint, note)"), value="fact", required=True),
            Field("content", self.t("Finding"), required=True, multiline=True),
        ]
        values = await self.push_screen_wait(FormModal(self.t("Quick finding"), fields, submit=self.t("Save")))
        if not values:
            return
        prompt = f"[{values['kind'].strip()}] {safe_text(values['content'].strip(), limit=400)}"
        if not await agreed(self, self.t("Save finding?"), prompt, self.t("Save")):
            return
        try:
            await self._req("POST", "/api/findings", json={
                "project": self.selected_project["id"],
                "kind": values["kind"].strip(),
                "content": values["content"].strip(),
            })
            self.notice(self.t("Finding saved."))
            await self._load_inspector(self.selected_project["id"])
        except (BrokerError, OSError, ValueError) as exc:
            self.notice(broker_error_text(exc, self.t))

    @work(group="standalone-add-project", exclusive=True, exit_on_error=False)
    async def add_project(self) -> None:
        fields = [
            Field("name", self.t("Project name"), required=True),
            Field("root", self.t("Project folder"), required=True),
        ]
        values = await self.push_screen_wait(FormModal(self.t("Add project"), fields, submit=self.t("Add")))
        if not values:
            return
        try:
            await self._req("POST", "/api/projects", json={
                "name": values["name"].strip(),
                "root": values["root"].strip(),
            })
            self.notice(self.t("Project registered."))
            self.load_all()
        except (BrokerError, OSError, ValueError) as exc:
            self.notice(broker_error_text(exc, self.t))

    @work(group="standalone-review", exclusive=True, exit_on_error=False)
    async def review_selected(self, decision: str) -> None:
        if not self.selected_project or not self.selected_row:
            self.notice(self.t("Select a record first."))
            return
        row = self.selected_row
        rid = row.get("id")
        if not rid:
            return
        try:
            if decision in {"confirmed", "disputed"}:
                action = "confirm" if decision == "confirmed" else "dispute"
                prompt = review_text(row, action, None, "", self.t)
                title = self.t("Confirm record?") if decision == "confirmed" else self.t("Dispute record?")
                if not await agreed(self, title, prompt, self.t("Confirm" if decision == "confirmed" else "Dispute")):
                    return
                await self._req("POST", f"/api/inspector/records/{rid}/review", json={
                    "project": self.selected_project["id"],
                    "decision": decision,
                    "expected_version": row.get("version"),
                })
            elif decision == "release":
                if not await agreed(self, self.t("Release confirmation?"),
                                    safe_text(str(row.get("content", rid)), limit=400),
                                    self.t("Release")):
                    return
                await self._req("POST", f"/api/inspector/records/{rid}/release", json={
                    "project": self.selected_project["id"],
                    "expected_version": row.get("version"),
                })
            elif decision == "resolve":
                if not await agreed(self, self.t("Resolve conflict?"),
                                    safe_text(str(row.get("content", rid)), limit=400),
                                    self.t("Resolve")):
                    return
                await self._req("POST", "/api/inspector/conflicts/resolve", json={
                    "project": self.selected_project["id"],
                    "conflict_id": rid,
                })
            self.notice(self.t("Saved."))
            await self._load_inspector(self.selected_project["id"])
        except (BrokerError, OSError, ValueError) as exc:
            self.notice(broker_error_text(exc, self.t))

    @work(group="standalone-forget", exclusive=True, exit_on_error=False)
    async def forget_project_memory(self) -> None:
        if not self.selected_project:
            self.notice(self.t("Select a project first."))
            return
        proj = self.selected_project
        prompt = f"Delete all stored memory for project {proj.get('name', proj['id'])} ({proj.get('root', '')})?"
        if not await agreed(self, self.t("Delete project memory?"), prompt, self.t("Delete")):
            return
        try:
            await self._req("POST", "/api/inspector/projects/forget", json={"project": proj["id"]})
            self.notice(self.t("Project memory deleted."))
            self.load_all()
        except (BrokerError, OSError, ValueError) as exc:
            self.notice(broker_error_text(exc, self.t))

    @work(group="standalone-setup", exclusive=True, exit_on_error=False)
    async def run_setup_change(self) -> None:
        try:
            await terminal_setup_change.change_setup(self)
        except (OSError, ValueError, RuntimeError) as exc:
            await self.push_screen_wait(MessageModal(self.t("Setup"), str(exc)))

    @on(Button.Pressed)
    def on_button(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-add-project":
            self.add_project()
        elif bid == "btn-finding":
            self.open_finding_form()
        elif bid == "btn-refresh":
            self.load_all()
        elif bid == "btn-forget-project":
            self.forget_project_memory()
        elif bid == "btn-confirm":
            self.review_selected("confirmed")
        elif bid == "btn-dispute":
            self.review_selected("disputed")
        elif bid == "btn-release":
            self.review_selected("release")
        elif bid == "btn-resolve":
            self.review_selected("resolve")
        elif bid == "btn-change-setup":
            self.run_setup_change()
        elif bid == "btn-toggle-lang":
            self.language = "pl" if self.language == "en" else "en"
            self._render_settings()
            self.notice(f"Language: {self.language}")


def run_standalone(settings, *, workspace_id: str = "default") -> int:
    """Launch the standalone Textual workspace on native Windows with Named Pipe person proof."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("Cairnmind needs an interactive terminal. Use `cairnmind workspace status` to diagnose.")
    pipe_server = None
    broker = None
    try:
        if sys.platform == "win32":
            key = person_proof.new_key()
            pipe_server = person_pipe.PersonPipeServer(settings.data_dir, key)
            try:
                pipe_server.start()
            except OSError as exc:
                raise ValueError(
                    "Another Cairnmind workspace is already open for this data directory, or the "
                    f"Windows Named Pipe could not be created ({exc})."
                ) from None
            person_proof.register_active_pipe(pipe_server.pipe_name)
        broker = BrokerClient(settings, ensure=True)
        app = StandaloneWorkspaceApp(settings, workspace_id, broker=broker)
        code = app.run()
        return int(code) if isinstance(code, int) else 0
    finally:
        if pipe_server is not None:
            person_proof.unregister_active_pipe(pipe_server.pipe_name)
            pipe_server.close()
        if broker is not None:
            broker.close()


# Re-export ChoiceModal for callers inspecting standalone modal types.
__all__ = ["ChoiceModal", "StandaloneWorkspaceApp", "run_standalone"]
