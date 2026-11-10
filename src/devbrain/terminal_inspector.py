"""Read-only projections and explicit reviewed writes for terminal workspace memory."""
from __future__ import annotations

import asyncio
import json
from datetime import date

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Footer, Header, Input, Select, Static

from .terminal_safe import plain, safe_text
from .terminal_ui import BasePanel, ChoiceModal, Field, FieldError, FormModal, summary_text

# (evidence_state, quote_survives) as returned by the inspector for one record.
EVIDENCE_LABELS = {
    ("anchored", None): "at least one supporting source still stands",
    ("evidence_replaced", True): "the source changed; the quoted passage is still there",
    ("evidence_replaced", False): "the source changed; the quoted passage is gone",
    ("evidence_replaced", None): "the source changed; there is no verified quoted passage to compare",
    ("orphaned", None): "orphaned: no supporting source has a current version",
    ("unverified", None): "imported file evidence has not been compared with local files yet",
}

_VIEWS = [("Ustalenia", "records"), ("Źródła", "sources"), ("Plany", "plans"),
          ("Zadania", "tasks"), ("Testy", "checks"), ("Kolejka", "jobs"),
          ("Nieprzypisane", "inbox"), ("Snapshot", "memory")]
_TYPES = {"record": "records", "source": "sources", "plan": "plans", "check": "checks", "job": "jobs"}


class InspectorApp(BasePanel):
    panel_role = "inspector"
    TITLE = "Cairnmind · Memory"
    CSS = BasePanel.CSS + """
    #inspector-main { height: 1fr; }
    #inspector-heading { height: auto; max-height: 2; padding: 0 1; }
    #inspector-view { height: 3; }
    #inspector-filter { height: 3; }
    #inspector-rows { height: 1fr; min-height: 3; }
    #inspector-details { height: 1fr; min-height: 3; padding: 0 1; }
    #entity-body { height: auto; }
    #entity-actions, #review-actions, #memory-actions { height: 3; }
    #memory-extra { height: 3; }
    #entity-actions Button, #review-actions Button, #memory-actions Button, #memory-extra Button { min-width: 8; }
    """
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
        self._technical_details = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="inspector-main"):
            yield Static(self.t("Wybierz projekt w panelu projektów."), id="inspector-heading", markup=False)
            yield Select([(self.t(label), value) for label, value in _VIEWS], value="records", allow_blank=False, id="inspector-view")
            yield Input(placeholder=self.t("Filtruj załadowaną stronę…"), id="inspector-filter")
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
        self.query_one("#inspector-details").display = False
        self.query_one("#review-actions").display = False
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
                  "memory-apply": "Apply", "memory-refresh": "Status"}
        for identifier, label in labels.items():
            self.query_one("#" + identifier, Button).label = self.t(label)

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
        self.query_one("#inspector-heading", Static).update(plain(
            (value.get("project_name") or project or self.t("Pamięć globalna")) + " · " + (value.get("worktree") or self.t("Unassigned"))))
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
            await self.load_page()

    def set_view(self, view):
        self.view = view
        self.query_one("#inspector-view", Select).value = view
        is_memory = view == "memory"
        for selector in ("#memory-actions", "#memory-extra"):
            self.query_one(selector).display = is_memory
        self.query_one("#inspector-filter").display = not is_memory
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
            self.notice(exc)

    def params(self):
        return {"project": self.workspace_data.get("project_id"), "worktree": self.workspace_data.get("worktree")}

    async def load_page(self, cursor=None):
        if self.view == "memory":
            await self.load_memory()
            return
        project = self.workspace_data.get("project_id")
        if not project and self.view != "inbox":
            self.notice(self.t("Wybierz projekt. Nieprzypisane wpisy i import są dostępne globalnie."))
            self.render_rows([])
            return
        scope, view = self._scope_version, self.view
        collection = "records" if view == "tasks" else view
        params = {"cursor": cursor, "limit": 100}
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
        table.clear()
        query = self.query_one("#inspector-filter", Input).value.casefold()
        for row in rows:
            content = row.get("content", row.get("title", row.get("uri", row.get("profile_id", row.get("id", "")))))
            if query and query not in str(content).casefold():
                continue
            metadata = row.get("metadata") or {}
            state = row.get("state", metadata.get("state", row.get("status", row.get("outcome", ""))))
            table.add_row(plain(f"{row.get('kind', self.view)} / {state}"), plain(str(content).replace("\n", " "), limit=300),
                          plain(row.get("version", row.get("attempts", ""))), key=str(row["id"]))
        table.display = True
        self.query_one("#inspector-details").display = False
        self.query_one("#review-actions").display = False
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
            self.notice(exc)

    def show_detail(self, value, *, kind=None, text=None):
        self.detail_data = value
        self.query_one("#inspector-rows").display = False
        self.query_one("#inspector-details").display = True
        self._technical_details = False
        self._detail_text = text if text is not None else self.readable_detail(value, kind=kind)
        self.query_one("#entity-body", Static).update(plain(self._detail_text))
        self.query_one("#review-actions").display = kind in {"records", "plans", "sources", "inbox"}
        for selector in ("#entity-confirm", "#entity-dispute"):
            self.query_one(selector, Button).disabled = kind != "records"
        self.query_one("#entity-history", Button).disabled = kind not in {"records", "plans"}
        self.query_one("#entity-sources", Button).label = self.t("Widok") if kind in {"sources", "inbox"} else self.t("Źródła")
        self.query_one("#entity-more", Button).disabled = not bool(self.source_next)

    def readable_detail(self, value, *, kind=None):
        if not isinstance(value, dict):
            return summary_text(value, self.language)

        item = value.get("record", value.get("item", value.get("source", value)))
        if not isinstance(item, dict):
            return summary_text(value, self.language)

        lines = []
        for name in ("title", "content", "uri", "message"):
            if isinstance(item.get(name), str):
                lines.append(item[name])

        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}

        state = item.get("state", metadata.get("state", item.get("status")))
        if state:
            lines.append(self.t("Status") + ": " + str(state))
        if item.get("version"):
            lines.append(self.t("Version") + ": " + str(item["version"]))
        if item.get("source_ids"):
            lines.append(self.t("Sources") + ": " + str(len(item["source_ids"])))

        if isinstance(value.get("record"), dict):
            # Any record detail, including a task opened from the task list.
            def evidence_date(value):
                if not isinstance(value, str):
                    return None
                try:
                    return date.fromisoformat(value[:10]).isoformat() if len(value) >= 10 else None
                except ValueError:
                    return None

            earliest = evidence_date(metadata.get("evidence_earliest"))
            latest = evidence_date(metadata.get("evidence_latest"))
            basis = metadata.get("evidence_basis")

            if basis == "stated":
                evidence_time = latest or earliest
                if evidence_time:
                    lines.append(self.t("Stated") + ": " + evidence_time)
            elif basis == "observed":
                evidence_time = latest or earliest
                if evidence_time:
                    lines.append(self.t("Observed no later than") + ": " + evidence_time)
            elif basis == "mixed":
                if earliest and latest:
                    evidence_range = earliest if earliest == latest else f"{earliest} – {latest}"
                    lines.append(self.t("Mixed evidence") + ": " + evidence_range)

            if metadata.get("supersede_warning") == "anachronistic_supersede":
                replaced_date = evidence_date(metadata.get("supersede_replaced_evidence_latest"))
                warning = self.t("The superseded finding had newer evidence")
                if replaced_date:
                    warning += f" ({replaced_date})"
                lines.append(self.t("Warning") + ": " + warning)

        evidence = value.get("evidence") if isinstance(value.get("evidence"), dict) else {}
        label = EVIDENCE_LABELS.get((evidence.get("evidence_state"), evidence.get("quote_survives")))
        if label:
            lines.append(self.t("Evidence") + ": " + self.t(label))
        if not lines:
            return summary_text(value, self.language)
        return "\n\n".join(lines)

    def toggle_technical(self):
        self._technical_details = not self._technical_details
        self.query_one("#entity-body", Static).update(plain(json.dumps(self.detail_data, ensure_ascii=False, indent=2)
            if self._technical_details else self._detail_text))

    async def open_entity(self, identifier, kind=None, *, source_view=None, offset=0):
        if not isinstance(identifier, str) or not identifier:
            return
        kind = kind or self.view
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
            self.show_detail(result, kind=collection, text=header + "\n\n" + self.source_content)
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
            self.notice(self.t("Projekt zmienił się podczas oceny. Niczego nie zapisano."))
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
                self.notice(self.t("Review was not retried. The current version was reloaded.") + " " + str(exc))

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
            self.show_detail({}, text=self.t("Brak wybranego projektu. Import pozostaje dostępny z jawnym katalogiem docelowym."))
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
            self.notice(exc)

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
            self.notice(self.t("Memory updates") + ": " + safe_text(exc))

    def refresh_if_list(self):
        if not self.entity and not self.history_owner and self.view != "memory":
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
            self.notice(exc)

    def action_refresh(self):
        if self.entity:
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
