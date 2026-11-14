"""Read-only projections and explicit reviewed writes for terminal workspace memory."""
from __future__ import annotations

import asyncio
import json

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Footer, Header, Input, Select, Static

from . import terminal_theme
from .terminal_detail import EVIDENCE_LABELS, conflict_parts, conflict_text, joined, record_parts  # noqa: F401
from .terminal_impact import ImpactView
from .terminal_notices import about
from .terminal_safe import plain, safe_text
from .terminal_ui import BasePanel, ChoiceModal, Field, FieldError, FormModal, summary_text

_VIEWS = [("Ustalenia", "records"), ("Źródła", "sources"), ("Plany", "plans"),
          ("Zadania", "tasks"), ("Testy", "checks"), ("Kolejka", "jobs"),
          ("Conflicts", "conflicts"), ("Impact", "impact"), ("Nieprzypisane", "inbox"), ("Snapshot", "memory")]
_TYPES = {"record": "records", "source": "sources", "plan": "plans", "check": "checks", "job": "jobs"}


class InspectorApp(ImpactView, BasePanel):
    panel_role = "inspector"
    TITLE = "Cairnmind · Memory"
    CSS = BasePanel.CSS + terminal_theme.css("""
    #inspector-main { height: 1fr; }
    #inspector-heading { height: auto; max-height: 2; padding: 0 1; }
    #inspector-view, #inspector-filter { height: 1; }
    #inspector-filter { background: $cm-bg; }
    #inspector-rows { height: 1fr; min-height: 3; }
    #inspector-details { height: 1fr; min-height: 3; padding: 1 1 0 1; }
    #inspector-details.-empty { align: center middle; }
    #inspector-details.-empty #entity-body { color: $cm-muted; text-align: center; }
    #entity-body { height: auto; }
    #entity-actions, #review-actions, #memory-actions, #conflict-actions, #impact-actions, #memory-extra {
        height: 1; padding: 0 1; }
    #entity-actions { margin-top: 1; }
    #entity-actions Button, #review-actions Button, #memory-actions Button, #memory-extra Button,
    #conflict-actions Button, #impact-actions Button { min-width: 8; }
    #entity-open { background: $cm-select; color: $cm-strong; text-style: bold; }
    #entity-open:focus { background: $cm-accent; color: $cm-bg; }
    #entity-open:disabled { background: $cm-panel; color: $cm-dim; text-style: none; }
    #impact-bar { height: 1; padding: 0 1; }
    #impact-since-label { color: $cm-muted; width: auto; padding: 0 1 0 0; }
    #impact-since { width: 24; }
    #impact-pending { width: auto; margin-left: 2; }
    .-short #inspector-details { padding: 0 1; }
    .-short #entity-actions { margin-top: 0; }
    """)
    BINDINGS = [("r", "refresh", "Refresh"), ("escape", "back", "List"),
                ("h", "history", "History"), ("m", "memory", "Memory")]

    def __init__(self, settings, workspace_id="default", *, broker=None, workspace=None):
        super().__init__(settings, workspace_id, broker=broker, workspace=workspace)
        self.view = "records"
        self.rows = {}
        self.next_cursor = None
        self.entity = None
        self.entity_kind = None
        self.detail_data = {}
        self.plan_version = None
        self.history_owner = None
        self.source_next = None
        self.source_view = "normalized"
        self.source_content = ""
        self._entity_request = 0
        self._selected_entity_signature = None
        self.pending_import = None
        self.preview_scope = None
        self._journal_timer = None
        self.memory_status = {}
        self._memory_scope = None
        self._detail_text = ""
        self._detail_parts = None
        self._technical_details = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="inspector-main"):
            yield Static(self.t("Wybierz projekt w panelu projektów."), id="inspector-heading", markup=False)
            yield Select([(self.t(label), value) for label, value in _VIEWS], value="records", allow_blank=False, id="inspector-view")
            yield Input(placeholder=self.t("Filtruj załadowaną stronę…"), id="inspector-filter")
            yield from self.compose_impact_bar()
            yield DataTable(id="inspector-rows", cursor_type="row", zebra_stripes=True)
            with VerticalScroll(id="inspector-details"):
                yield Static("", id="entity-body", markup=False)
            with Horizontal(id="entity-actions"):
                yield Button(self.t("Otwórz"), id="entity-open")
                yield Button(self.t("Więcej"), id="entity-more")
                yield Button(self.t("Lista"), id="entity-back")
                yield Button(self.t("Filtry"), id="entity-filters")
                yield Button(self.t("Details"), id="entity-technical")
            with Horizontal(id="review-actions"):
                yield Button(self.t("Potwierdź"), id="entity-confirm")
                yield Button(self.t("Zakwestionuj"), id="entity-dispute")
                yield Button(self.t("Historia"), id="entity-history")
                yield Button(self.t("Źródła"), id="entity-sources")
            with Horizontal(id="conflict-actions"):
                yield Button(self.t("Resolve"), id="conflict-resolve")
            with Horizontal(id="impact-actions"):
                yield Button(self.t("Verdict"), id="impact-verdict")
            with Horizontal(id="memory-actions"):
                yield Button(self.t("Podgląd"), id="memory-preview")
                yield Button(self.t("Zapisz"), id="memory-save", disabled=True)
                yield Button(self.t("Automat"), id="memory-auto")
                yield Button(self.t("Import"), id="memory-import")
            with Horizontal(id="memory-extra"):
                yield Button(self.t("Wczytaj"), id="memory-apply", disabled=True)
                yield Button(self.t("Stan"), id="memory-refresh")
            yield Static("", id="panel-notice", markup=False)
        yield Footer()

    def on_mount(self):
        self.query_one("#inspector-rows", DataTable).add_columns(self.t("Typ / stan"), self.t("Treść / źródło"), self.t("Wersja / próba"))
        self.fit_columns()
        self.query_one("#inspector-details").display = False
        self.query_one("#review-actions").display = False
        self.query_one("#conflict-actions").display = False
        self.query_one("#impact-actions").display = False
        self.query_one("#impact-bar").display = False
        self.query_one("#memory-actions").display = False
        self.query_one("#memory-extra").display = False
        self.set_interval(.3, self._poll_workspace)
        self.start_panel()

    async def ready_panel(self):
        await self.load_page()
        self.watch_journal()

    def refresh_language(self):
        from .terminal_i18n import localize_bindings
        localize_bindings(self, self.language)
        self.query_one("#inspector-view", Select).set_options([(self.t(label), value) for label, value in _VIEWS])
        self.query_one("#inspector-view", Select).value = self.view
        self.query_one("#inspector-filter", Input).placeholder = self.t("Filter loaded page…")
        labels = {"entity-open": "Open", "entity-more": "More", "entity-back": "List", "entity-filters": "Filters",
                  "entity-technical": "Details", "entity-confirm": "Confirm", "entity-dispute": "Dispute",
                  "entity-history": "History", "entity-sources": "Sources", "memory-preview": "Preview",
                  "memory-save": "Save", "memory-auto": "Automatic", "memory-import": self.t("Import"),
                  "memory-apply": "Apply", "memory-refresh": "Status", "conflict-resolve": "Resolve"}
        for identifier, label in labels.items():
            self.query_one("#" + identifier, Button).label = self.t(label)
        self.refresh_impact_language()

    async def workspace_changed(self, value):
        scope = (value.get("project_id"), value.get("worktree"))
        context_changed = scope != self._memory_scope
        if context_changed:
            self._memory_scope = scope
            self.preview_scope = None
            self.pending_import = None
            self.query_one("#memory-save", Button).disabled = True
            self.query_one("#memory-apply", Button).disabled = True
        project = value.get("project_id")
        self.query_one("#inspector-heading", Static).update(terminal_theme.assemble([
            ("title", value.get("project_name") or project or self.t("Pamięć globalna")),
            ("muted", " · " + (value.get("worktree") or self.t("Unassigned")))]))
        selection = value.get("selected_entity")
        signature = json.dumps(selection, sort_keys=True, default=str)
        if selection and signature != self._selected_entity_signature:
            self._selected_entity_signature = signature
            entity_type = _TYPES.get(selection.get("entity_type"), selection.get("entity_type"))
            if entity_type == "inbox_group":
                self.set_view("inbox")
                await self.load_page()
            elif entity_type == "memory":
                self.set_view("memory")
                await self.load_memory()
            elif entity_type == "revision":
                await self.open_revision(selection)
            elif entity_type in {"records", "sources", "plans", "checks", "jobs", "inbox"}:
                self.set_view(entity_type)
                await self.open_entity(selection.get("id"), entity_type)
        elif not selection and (context_changed or self._selected_entity_signature not in {None, "null"}):
            self._selected_entity_signature = None
            self.entity = None
            if self.view == "impact":
                self.refresh_page()  # Git may take minutes: never inside the workspace poll
            else:
                await self.load_page()

    def set_view(self, view):
        self.view = view
        self.query_one("#inspector-view", Select).value = view
        is_memory = view == "memory"
        for selector in ("#memory-actions", "#memory-extra"):
            self.query_one(selector).display = is_memory
        self.query_one("#inspector-filter").display = not is_memory
        self.query_one("#impact-bar").display = view == "impact"
        self.query_one("#entity-actions").display = not is_memory

    @on(Select.Changed, "#inspector-view")
    def view_changed(self, event):
        if event.value is Select.NULL or event.value == self.view:
            return
        self.view = str(event.value)
        self._entity_request += 1
        self.set_view(self.view)
        self.entity = None
        self.entity_kind = None
        self.history_owner = None
        self.refresh_page()

    @work(group="inspector-read", exclusive=True, exit_on_error=False)
    async def refresh_page(self):
        try:
            if self.view == "memory":
                await self.load_memory()
            else:
                await self.load_page()
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc, "error")

    def params(self):
        return {"project": self.workspace_data.get("project_id"), "worktree": self.workspace_data.get("worktree")}

    async def load_page(self, cursor=None):
        if self.view == "memory":
            await self.load_memory()
            return
        if self.view == "impact":
            await self.load_impact()
            return
        project = self.workspace_data.get("project_id")
        if not project and self.view != "inbox":
            self.notice(self.t("Wybierz projekt. Nieprzypisane wpisy i import są dostępne globalnie."))
            self.render_rows([])
            return
        scope, view = self._scope_version, self.view
        collection = "records" if view == "tasks" else view
        params = {} if collection == "conflicts" else {"cursor": cursor, "limit": 100}
        if collection != "inbox":
            params.update(self.params())
            if view == "tasks":
                params["kind"] = "task"
            params.update(getattr(self, "filters", {}))
        result = await self.request("GET", "/api/inspector/" + collection, params=params)
        if scope != self._scope_version or view != self.view:
            return
        self.history_owner = None
        self.entity = None
        self.next_cursor = result.get("next_cursor")
        self.source_next = None
        self.rows = {**(self.rows if cursor else {}), **{r["id"]: r for r in result.get("items", [])}}
        self.render_rows(list(self.rows.values()))
        self.notice(self.t("{count} items · read only", count=result.get("total", len(self.rows))) + (" · " + self.t("More pages available") if self.next_cursor else ""))

    def render_rows(self, rows):
        table = self.query_one("#inspector-rows", DataTable)
        if self.view == "impact" and not self.history_owner:
            self.render_impact_rows(rows)
        else:
            self.use_columns("standard")
            table.clear()
            query = self.query_one("#inspector-filter", Input).value.casefold()
            for row in rows:
                content = row.get("content", row.get("title", row.get("uri", row.get("profile_id", row.get("id", "")))))
                if query and query not in str(content).casefold():
                    continue
                metadata = row.get("metadata") or {}
                state = row.get("state", metadata.get("state", row.get("status", row.get("outcome", ""))))
                table.add_row(self.state_cell(row.get("kind", self.view), state), plain(str(content).replace("\n", " "), limit=300),
                              plain(row.get("version", row.get("attempts", ""))), key=str(row["id"]))
            self.fit_columns()
        table.display = True
        self.query_one("#inspector-details").display = False
        self.query_one("#review-actions").display = False
        self.query_one("#conflict-actions").display = False
        self.query_one("#impact-actions").display = False
        self.query_one("#entity-more", Button).disabled = not bool(self.next_cursor)

    @on(Input.Changed, "#inspector-filter")
    def filter_changed(self):
        if not self.entity:
            self.render_rows(list(self.rows.values()))

    def selected_id(self):
        table = self.query_one("#inspector-rows", DataTable)
        if table.row_count:
            return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        return None

    @on(DataTable.RowSelected, "#inspector-rows")
    def row_selected(self, event):
        self.open_selected(str(event.row_key.value))

    @work(group="inspector-detail", exclusive=True, exit_on_error=False)
    async def open_selected(self, identifier=None, kind=None):
        try:
            identifier = identifier or self.selected_id()
            if identifier:
                if self.history_owner:
                    row = self.rows.get(identifier)
                    self.show_detail(row or {}, kind="revision")
                else:
                    await self.open_entity(identifier, kind or self.view)
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc, "error")

    def show_detail(self, value, *, kind=None, text=None, parts=None, empty=False):
        """Show one entry. `parts` (see terminal_detail) style the text they spell; `text` is shown as it is."""
        self.detail_data = value
        self.query_one("#inspector-rows").display = False
        details = self.query_one("#inspector-details")
        details.display = True
        details.set_class(empty, "-empty")
        self._technical_details = False
        if text is None and parts is None:
            parts = record_parts(value, self.t)
        self._detail_parts = parts if text is None else None
        self._detail_text = (joined(parts) if self._detail_parts else text if text is not None
                             else summary_text(value, self.language))
        self.show_body()
        self.query_one("#review-actions").display = kind in {"records", "plans", "sources", "inbox"}
        self.query_one("#conflict-actions").display = kind == "conflicts"
        self.query_one("#impact-actions").display = kind == "impact"
        for selector in ("#entity-confirm", "#entity-dispute"):
            self.query_one(selector, Button).disabled = kind != "records"
        self.query_one("#entity-history", Button).disabled = kind not in {"records", "plans"}
        self.query_one("#entity-sources", Button).label = self.t("Widok") if kind in {"sources", "inbox"} else self.t("Źródła")
        self.query_one("#entity-more", Button).disabled = not bool(self.source_next)

    def readable_detail(self, value, *, kind=None):
        parts = record_parts(value, self.t)
        return joined(parts) if parts else summary_text(value, self.language)

    def show_body(self):
        # The technical view is the stored structure as it is: never styled.
        if self._technical_details:
            body = plain(json.dumps(self.detail_data, ensure_ascii=False, indent=2))
        else:
            body = terminal_theme.assemble(self._detail_parts) if self._detail_parts else plain(self._detail_text)
        self.query_one("#entity-body", Static).update(body)

    def toggle_technical(self):
        self._technical_details = not self._technical_details
        self.query_one("#inspector-details").remove_class("-empty")
        self.show_body()

    async def open_entity(self, identifier, kind=None, *, source_view=None, offset=0):
        if not isinstance(identifier, str) or not identifier:
            return
        kind = kind or self.view
        if kind == "impact":
            self.open_impact(identifier)  # an entry has no detail route either: the list carries it whole
            return
        if kind == "conflicts":
            # A pair has no detail route: the list already carries both findings in full.
            pair = self.rows.get(identifier)
            if pair:
                self._entity_request += 1
                self.entity_kind, self.entity, self.history_owner, self.source_next = "conflicts", pair, None, None
                self.show_detail(pair, kind="conflicts", parts=conflict_parts(pair, self.t))
                self.notice(self.t("Resolve records your decision with an audit note. No source is removed."))
            return
        collection = "records" if kind == "tasks" else kind
        scope = self._scope_version
        self._entity_request += 1
        ticket = self._entity_request
        params = {} if collection == "inbox" else self.params()
        if collection in {"sources", "inbox"}:
            if source_view:
                self.source_view = source_view
            if collection == "inbox" and self.source_view == "normalized":
                self.source_view = "raw"
            params.update(view=self.source_view, offset=offset, limit=65536)
        result = await self.request("GET", f"/api/inspector/{collection}/{identifier}", params=params)
        if (scope, ticket) != (self._scope_version, self._entity_request):
            return
        candidate = result.get("record", result.get("item", result.get("source", result)))
        plan_version = None
        if collection == "records" and candidate.get("metadata", {}).get("plan_ref"):
            plan = await self.request("GET", "/api/inspector/plans/" + candidate["metadata"]["plan_ref"], params=self.params())
            if (scope, ticket) != (self._scope_version, self._entity_request):
                return
            plan_version = plan.get("item", plan).get("version")
        self.entity_kind, self.entity = collection, candidate
        self.history_owner = None
        self.source_next = result.get("next_offset")
        self.plan_version = plan_version
        if collection in {"sources", "inbox"}:
            self.source_content = (self.source_content if offset else "") + result.get("text", "")
            source = result.get("source", {})
            header = str(source.get("uri") or self.t("Sources"))
            self.show_detail(result, kind=collection,
                             parts=[("title", header), ("stored", "\n\n" + self.source_content)])
            self.notice(self.t("Źródło literalne · v=tekst/surowy/metadane · Więcej pobiera kolejny fragment"))
        else:
            self.show_detail(result, kind=collection)
            self.notice(self.t("Historia / Źródła otwierają zachowane dowody. Ocena zmienia tylko pewność ustalenia."))

    @work(group="inspector-review", exclusive=True, exit_on_error=False)
    async def review(self, action):
        if self.entity_kind != "records" or not self.entity:
            self.notice(self.t("Najpierw otwórz ustalenie."))
            return
        record = dict(self.entity)
        scope = self._scope_version
        project_id = self.workspace_data.get("project_id")
        plan_version = self.plan_version
        if record.get("status") == "superseded":
            self.notice("Wersja historyczna nie podlega ocenie.")
            return
        form = await self.push_screen_wait(FormModal(self.t("Potwierdź ustalenie") if action == "confirm" else self.t("Zakwestionuj ustalenie"),
            [Field("reason", self.t("Uzasadnienie"), required=action == "dispute", multiline=True)],
            note=f"v{record['version']}" + (f" · plan v{self.plan_version}" if self.plan_version else "") + " · " + self.t("Review changes confidence; it does not complete a task."), submit=self.t("Potwierdź ocenę")))
        if form is None:
            return
        if scope != self._scope_version:
            self.notice_for(about(project_id), self.t("Projekt zmienił się podczas oceny. Niczego nie zapisano."), "warn")
            return
        payload = {"project": project_id, "action": action,
                   "expected_version": record["version"], "reason": form["reason"]}
        if plan_version is not None:
            payload["expected_plan_version"] = plan_version
        try:
            await self.request("POST", f"/api/inspector/records/{record['id']}/review", json=payload)
            if scope == self._scope_version:
                await self.open_entity(record["id"], "records")
                self.notice("Ocena zapisana z audytem.")
        except (OSError, ValueError, RuntimeError) as exc:
            if scope == self._scope_version:
                await self.open_entity(record["id"], "records")
                self.notice(self.t("Review was not retried. The current version was reloaded.") + " " + str(exc), "error")

    @work(group="inspector-review", exclusive=True, exit_on_error=False)
    async def resolve_conflict(self):
        if self.entity_kind != "conflicts" or not self.entity:
            self.notice(self.t("Open a conflict first."))
            return
        pair = dict(self.entity)
        first, second = pair["candidate"], pair["existing"]
        scope = self._scope_version
        project_id = self.workspace_data.get("project_id")
        # A fact and a decision cannot replace each other, so that pair is not offered "Keep".
        keep = [(self.t("Keep A: it replaces B and becomes confirmed"), "keep-first"),
                (self.t("Keep B: it replaces A and becomes confirmed"), "keep-second")] if pair.get("can_supersede", True) else []
        choice = await self.push_screen_wait(ChoiceModal(self.t("Resolve conflict"), [
            *keep, (self.t("Both are valid in different scopes"), "different_scopes"),
            (self.t("False alarm: they do not contradict"), "dismiss")]))
        if choice is None:
            return
        action = "supersede" if choice.startswith("keep-") else choice
        form = await self.push_screen_wait(FormModal(self.t("Resolve conflict"),
            [Field("reason", self.t("Describe the two scopes") if action == "different_scopes" else self.t("Uzasadnienie"),
                   required=action == "different_scopes", multiline=True)],
            note=f"A v{first['version']} · B v{second['version']} · "
                 + self.t("The decision is saved with an audit note; every source is kept."),
            submit=self.t("Save decision")))
        if form is None:
            return
        if scope != self._scope_version:
            self.notice_for(about(project_id), self.t("Projekt zmienił się podczas oceny. Niczego nie zapisano."), "warn")
            return
        payload = {"project": project_id, "record_id": first["id"], "other_id": second["id"], "action": action,
                   "expected_version": first["version"], "other_expected_version": second["version"],
                   "reason": form["reason"]}
        if action == "supersede":
            payload["keep"] = first["id"] if choice == "keep-first" else second["id"]
        try:
            await self.request("POST", "/api/inspector/conflicts/resolve", json=payload)
            if scope == self._scope_version:
                await self.load_page()
                self.notice(self.t("Decision saved with an audit note."))
        except (OSError, ValueError, RuntimeError) as exc:
            if scope == self._scope_version:
                await self.load_page()
                self.notice(self.t("The decision was not saved. The current list was reloaded.") + " " + str(exc), "error")

    @work(group="inspector-history", exclusive=True, exit_on_error=False)
    async def action_history(self, cursor=None):
        if not self.entity or self.entity_kind not in {"records", "plans"}:
            self.notice(self.t("Otwórz rekord lub plan, aby odczytać historię."))
            return
        owner = self.entity["id"]
        kind = self.entity_kind
        result = await self.request("GET", f"/api/inspector/{kind}/{owner}/revisions", params={**self.params(), "cursor": cursor, "limit": 100})
        self.rows = {**(self.rows if cursor else {}), **{r["id"]: r for r in result.get("items", [])}}
        self.next_cursor = result.get("next_cursor")
        self.render_rows(list(self.rows.values()))
        self.history_owner = (kind, owner)
        self.notice(self.t("Historia jest tylko do odczytu. Enter otwiera dokładny snapshot wersji."))

    async def open_revision(self, selection):
        owner_type = selection.get("owner_type", "record")
        owner = selection.get("owner_id")
        if not owner:
            self.notice(self.t("Węzeł wersji nie zawiera identyfikatora właściciela."))
            return
        path = "records" if owner_type == "record" else "plans"
        cursor = None
        scope = self._scope_version
        while True:
            result = await self.request("GET", f"/api/inspector/{path}/{owner}/revisions", params={**self.params(), "cursor": cursor, "limit": 100})
            if scope != self._scope_version:
                return
            row = next((r for r in result.get("items", []) if r.get("id") == selection.get("id") or r.get("version") == selection.get("revision")), None)
            if row:
                self.entity = None
                self.show_detail(row, kind="revision")
                return
            cursor = result.get("next_cursor")
            if not cursor:
                self.notice("Nie znaleziono wskazanej wersji.")
                return

    @work(group="inspector-source-choice", exclusive=True, exit_on_error=False)
    async def choose_source(self):
        if self.entity and self.entity_kind in {"sources", "inbox"}:
            choices = [(self.t("Tekst"), "normalized"), ("Surowy zapis", "raw"), (self.t("Metadane"), "metadata")]
            if self.entity_kind == "inbox":
                choices = choices[1:]
            value = await self.push_screen_wait(ChoiceModal(self.t("Widok źródła"), choices))
            if value:
                await self.open_entity(self.entity["id"], self.entity_kind, source_view=value)
        elif self.entity:
            sources = self.entity.get("source_ids", [])
            if not sources:
                self.notice(self.t("Brak bezpośrednich źródeł w tym wpisie."))
                return
            value = await self.push_screen_wait(ChoiceModal(self.t("Źródła ustalenia"), [(s, s) for s in sources]))
            if value:
                await self.open_entity(value, "sources")

    @work(group="inspector-filters", exclusive=True, exit_on_error=False)
    async def edit_filters(self):
        value = await self.push_screen_wait(FormModal(self.t("Filtry listy"), [Field("kind", self.t("Typ (np. fact, task, extract)")),
            Field("status", self.t("Pewność (proposed / confirmed / disputed / superseded)")), Field("state", "Stan zadania lub kolejki")]))
        if value is not None:
            self.filters = {k: v for k, v in value.items() if v}
            await self.load_page()

    async def load_memory(self):
        self.set_view("memory")
        if not self.workspace_data.get("project_id"):
            self.memory_status = {}
            self.show_detail({}, text=self.t("Brak wybranego projektu. Import pozostaje dostępny z jawnym katalogiem docelowym."), empty=True)
            return
        scope = self._scope_version
        result = await self.request("GET", "/api/memory/status", params=self.params())
        if scope != self._scope_version:
            return
        self.memory_status = result
        self.show_detail(result, text=self.t("Snapshots contain complete conversations and sources. Review before sharing.") + "\n\n" +
                         str(result.get("path") or "") + "\n" + summary_text(result, self.language))
        self.query_one("#memory-save", Button).disabled = self.preview_scope != (self.workspace_data.get("project_id"), self.workspace_data.get("worktree"))

    @work(group="inspector-memory", exclusive=True, exit_on_error=False)
    async def memory_operation(self, action):
        try:
            params = self.params()
            scope = self._scope_version
            if action in {"preview", "save"}:
                if not params["project"]:
                    raise ValueError(self.t("Wybierz projekt do zapisania."))
                if action == "save" and self.preview_scope != (params["project"], params["worktree"]):
                    raise ValueError(self.t("Najpierw przeczytaj podgląd bieżącego projektu."))
                result = await self.request("POST", "/api/memory/" + ("snapshot" if action == "save" else "preview"), json=params)
                if scope != self._scope_version:
                    return
                self.preview_scope = (params["project"], params["worktree"])
                self.query_one("#memory-save", Button).disabled = False
                self.show_detail(result)
                self.notice(self.t("Podgląd gotowy. Zapis i publikacja w Git pozostają jawną operacją.") if action == "preview" else self.t("Snapshot zapisany."))
            elif action == "auto":
                if not params["project"]:
                    raise ValueError(self.t("Wybierz projekt do ustawienia automatu."))
                auto = self.memory_status.get("auto") or {}
                async def save_auto(values):
                    if scope != self._scope_version:
                        raise FieldError("enabled", self.t("The project changed. Close this form and open it again."))
                    enabled = values["enabled"].casefold()
                    if enabled not in {"tak", "nie", "yes", "no", "true", "false"}:
                        raise FieldError("enabled", self.t("Wpisz tak albo nie."))
                    try:
                        interval = int(values["interval"])
                        if not 1 <= interval <= 10080:
                            raise ValueError
                    except ValueError:
                        raise FieldError("interval", self.t("Enter a whole number from 1 to 10080.")) from None
                    await self.request("PATCH", "/api/memory/settings", json={**params,
                        "enabled": enabled in {"tak", "yes", "true"}, "interval_minutes": interval})
                enabled_value = ("tak" if auto.get("enabled") else "nie") if self.language == "pl" else ("yes" if auto.get("enabled") else "no")
                value = await self.push_screen_wait(FormModal(self.t("Automatyczny snapshot"), [
                    Field("enabled", self.t("Włączony: tak / nie"), enabled_value, True),
                    Field("interval", self.t("Interwał w minutach"), str(auto.get("interval_minutes", 15)), True)],
                    note=self.t("Pełny snapshot obejmuje rozmowy i źródła. Automat jest lokalny; nie wykonuje commitów ani wysyłania."),
                    on_submit=save_auto))
                if value:
                    await self.load_memory()
            elif action == "import":
                checked = {}
                async def check_import(values):
                    if scope != self._scope_version:
                        raise FieldError("root", self.t("The project changed. Close this form and open it again."))
                    payload = {"path": values["path"], "root": values["root"]}
                    if values["name"]:
                        payload["name"] = values["name"]
                    checked["result"] = await self.request("POST", "/api/memory/import", json={**payload, "dry_run": True})
                    checked["payload"] = payload
                value = await self.push_screen_wait(FormModal(self.t("Sprawdź import pamięci"), [
                    Field("path", self.t("Katalog snapshotu lub manifest.json"), required=True),
                    Field("root", self.t("Jawny katalog docelowy"), self.workspace_data.get("worktree") or "", True, folder=True),
                    Field("name", self.t("Lokalna nazwa (opcjonalnie)"))],
                    note=self.t("Import nie uruchamia modeli ani klientów. Zachowuje historię; testy z innego komputera pozostają historyczne."),
                    submit=self.t("Sprawdź import"), on_submit=check_import))
                if value and scope == self._scope_version:
                    payload, result = checked["payload"], checked["result"]
                    self.pending_import = payload
                    self.query_one("#memory-apply", Button).disabled = False
                    self.show_detail(result, text=self.t("Import checked. Review the details, then choose Apply.") + "\n" + summary_text(result, self.language))
            elif action == "apply":
                if not self.pending_import:
                    raise ValueError(self.t("Najpierw sprawdź import z konkretną ścieżką i katalogiem."))
                payload = dict(self.pending_import)
                result = await self.request("POST", "/api/memory/import", json={**payload, "dry_run": False})
                self.pending_import = None
                self.query_one("#memory-apply", Button).disabled = True
                project = result.get("project")
                if project:
                    from .workspace import refresh_workspace
                    await asyncio.to_thread(self.workspace.select_project, project, payload["root"], intent="inspect")
                    await asyncio.to_thread(refresh_workspace, self.settings, self.workspace_id)
                self.show_detail(result)
                self.notice(self.t("Pamięć wczytana; żaden klient nie został automatycznie uruchomiony."))
            else:
                await self.load_memory()
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc, "error")

    @work(group="inspector-journal", exclusive=True, exit_on_error=False)
    async def watch_journal(self):
        try:
            async for event in self.broker.events():
                data = event.get("data") or {}
                if data.get("project_id") in {None, self.workspace_data.get("project_id")}:
                    if data.get("entity_type") in {"record", "plan", "source", "reset"}:
                        self.preview_scope = None
                        self.pending_import = None
                        self.query_one("#memory-save", Button).disabled = True
                        self.query_one("#memory-apply", Button).disabled = True
                    if self._journal_timer:
                        self._journal_timer.stop()
                    self._journal_timer = self.set_timer(.2, self.refresh_if_list)
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(self.t("Memory updates") + ": " + safe_text(exc), "error")

    def refresh_if_list(self):
        # The impact list runs Git over the worktree and changes with files the journal does not report: r reads it.
        if not self.entity and not self.history_owner and self.view not in {"memory", "impact"}:
            self.refresh_page()

    @on(Button.Pressed)
    def button_pressed(self, event):
        button = event.button.id
        if button == "entity-open":
            self.open_selected()
        elif button == "entity-more":
            self.more()
        elif button == "entity-back":
            self.action_back()
        elif button == "entity-filters":
            self.edit_filters()
        elif button == "entity-technical":
            self.toggle_technical()
        elif button == "entity-confirm":
            self.review("confirm")
        elif button == "entity-dispute":
            self.review("dispute")
        elif button == "conflict-resolve":
            self.resolve_conflict()
        elif button == "impact-verdict":
            self.record_impact_verdict()
        elif button == "entity-history":
            self.action_history()
        elif button == "entity-sources":
            self.choose_source()
        elif button and button.startswith("memory-"):
            self.memory_operation(button.removeprefix("memory-"))

    @work(group="inspector-more", exclusive=True, exit_on_error=False)
    async def more(self):
        try:
            if self.entity_kind in {"sources", "inbox"} and self.entity and self.source_next is not None:
                await self.open_entity(self.entity["id"], self.entity_kind, offset=self.source_next)
            elif self.history_owner and self.next_cursor:
                self.action_history(self.next_cursor)
            elif self.next_cursor:
                await self.load_page(self.next_cursor)
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc, "error")

    @work(group="inspector-read", exclusive=True, exit_on_error=False)
    async def reload_conflict(self, identifier):
        """A pair has no detail route, so refreshing it means reading the list again."""
        try:
            await self.load_page()
            if identifier in self.rows:
                await self.open_entity(identifier, "conflicts")
            else:
                self.notice(self.t("This conflict is no longer pending."))
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc, "error")

    def action_refresh(self):
        if self.entity and self.entity_kind == "conflicts":
            self.reload_conflict(self.entity["id"])
        elif self.entity and self.entity_kind == "impact":
            self.reload_impact(self.entity["id"])
        elif self.entity:
            self.open_selected(self.entity["id"], self.entity_kind)
        else:
            self.refresh_page()

    def action_back(self):
        self.entity = None
        self.entity_kind = None
        self.history_owner = None
        self.refresh_page()

    def action_memory(self):
        self.entity = None
        self.entity_kind = None
        self.history_owner = None
        self.set_view("records")
        self.refresh_page()

    def action_snapshot(self):
        self.set_view("memory")
        self.refresh_page()
