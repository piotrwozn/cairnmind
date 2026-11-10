"""Terminal-only workspace sidebar and standalone panel entrypoints."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Static, Switch

from .config import Settings
from .terminal_safe import plain
from .terminal_i18n import native_failure_notice
from .terminal_ui import (BasePanel, ChoiceModal, Field, FieldError, FormModal,
                          LocalizedModal, MessageModal, summary_text)


class ConfirmModal(MessageModal):
    def __init__(self, title, value, *, approve="Confirm"):
        super().__init__(title, value)
        self.approve = approve

    def compose(self):
        from textual.containers import VerticalScroll
        with Vertical():
            yield Static(plain(self.t(self.heading)), classes="heading")
            with VerticalScroll():
                yield Static(plain(summary_text(self.value, getattr(self.app, "language", "en"))), id="message-body")
                if not isinstance(self.value, str):
                    yield Button(self.t("Technical details"), id="message-details")
            with Horizontal(classes="actions"):
                yield Button(self.t(self.approve), id="confirm-yes", variant="primary")
                yield Button(self.t("Cancel"), id="confirm-no")

    @on(Button.Pressed)
    def confirm_button(self, event):
        if event.button.id in {"confirm-yes", "confirm-no"}:
            event.stop()
            self.dismiss(event.button.id == "confirm-yes")

    def action_close(self):
        self.dismiss(False)


class CredentialModal(LocalizedModal, ModalScreen[dict | None]):
    """Write-only credential entry; a stored secret is never loaded into this form."""
    CSS = """
    CredentialModal { align: center middle; background: $background 70%; }
    CredentialModal > VerticalScroll { width: 90%; max-width: 100; height: auto; max-height: 95%;
        background: #111a18; border: round #52684b; padding: 1 2; }
    CredentialModal .actions { height: auto; min-height: 3; }
    CredentialModal #credential-error { color: #e7b594; height: auto; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, label):
        super().__init__()
        self.profile_label = label

    def compose(self):
        with VerticalScroll():
            yield Static(plain(self.t("API key") + " — " + self.profile_label), classes="heading")
            yield Static(self.t("Klucz pozostaje poza pamięcią projektu. Domyślnie działa tylko podczas bieżącego działania silnika."), markup=False)
            yield Label(self.t("Nowy klucz (zapisany klucz nie jest wyświetlany)"))
            yield Input(password=True, id="credential-key")
            yield Label(self.t("Zapamiętaj lokalnie"))
            yield Switch(False, id="credential-persist")
            yield Static(self.t("Opcja zapisu tworzy prywatny plik 0600 poza projektem. Plik nie jest szyfrowany i nie trafia do snapshotu."), markup=False)
            yield Static("", id="credential-error", markup=False)
            with Horizontal(classes="actions"):
                yield Button(self.t("Ustaw klucz"), id="credential-save", variant="primary")
                yield Button(self.t("Anuluj"), id="credential-cancel")

    @on(Button.Pressed)
    def credential_button(self, event):
        event.stop()
        if event.button.id == "credential-cancel":
            self.action_cancel()
        elif event.button.id == "credential-save":
            key_input = self.query_one("#credential-key", Input)
            value = key_input.value.strip()
            if not value:
                self.query_one("#credential-error", Static).update(self.t("Wpisz nowy klucz."))
                return
            persist = self.query_one("#credential-persist", Switch).value
            key_input.value = ""
            self.dismiss({"key": value, "persist": persist})

    def action_cancel(self):
        self.query_one("#credential-key", Input).value = ""
        self.dismiss(None)


def profile_fields(old, *, client, provider=None):
    """Editable forms, with no credential values or opaque JSON blobs."""
    old = old or {}
    fields = [Field("label", "Nazwa profilu", old.get("label", ""), True)]
    if client in {"codex", "claude"}:
        return fields + [Field("client", "Klient: codex / claude", client, True),
                         Field("model", "Model (puste = domyślny klienta)", old.get("model") or "")]
    fields.append(Field("model", "Model" if client == "api" else "Model (puste = domyślny Copilota)", old.get("model") or "", client == "api"))
    if client == "api":
        defaults = {"openai": "https://api.openai.com/v1", "anthropic": "https://api.anthropic.com/v1",
                    "openai_compatible": "http://127.0.0.1:8000/v1", "ollama": "http://127.0.0.1:11434"}
        fields.append(Field("base_url", "Adres API", old.get("base_url") or defaults[provider], True))
    fields.append(Field("credential_ref", "Odwołanie do klucza: env:NAZWA lub profile:ID (puste = bez klucza)", old.get("credential_ref") or ""))
    limits = old.get("limits") or {}
    if client == "api":
        fields += [Field("max_model_calls", "Maksymalna liczba zapytań modelu w turze", str(limits.get("max_model_calls", 20)), True),
                   Field("max_output_tokens", "Maksymalna liczba tokenów odpowiedzi", str(limits.get("max_output_tokens", 4096)), True)]
        if provider == "ollama":
            fields.append(Field("context_window_tokens", "Okno kontekstu Ollamy (tokeny; zużywa RAM/VRAM)", str(limits.get("context_window_tokens", 32768)), True))
    else:
        fields += [Field("max_tool_calls", "Maksymalna liczba narzędzi w turze", str(limits.get("max_tool_calls", 20)), True),
                   Field("max_turn_seconds", "Maksymalny czas tury (sekundy)", str(limits.get("max_turn_seconds", 900)), True)]
    fields.append(Field("command_timeout_seconds", "Limit czasu polecenia (sekundy)", str(limits.get("command_timeout_seconds", 120)), True))
    if client == "api" and provider != "ollama":
        pricing = old.get("pricing") or {}
        fields += [Field("input_per_million", "Cena wejścia USD za milion tokenów (opcjonalnie)", str(pricing.get("input_per_million", ""))),
                   Field("output_per_million", "Cena wyjścia USD za milion tokenów (opcjonalnie)", str(pricing.get("output_per_million", "")))]
    return fields


def profile_from_form(value, *, identifier, client, provider=None):
    row = {"id": identifier, "label": value["label"], "client": client}
    if client in {"codex", "claude"}:
        row.update(client=value["client"], model=value.get("model") or None)
        return row
    if value.get("model"):
        row["model"] = value["model"]
    if value.get("credential_ref"):
        row["credential_ref"] = value["credential_ref"]
    keys = ("max_model_calls", "max_output_tokens", "command_timeout_seconds") if client == "api" else ("max_tool_calls", "max_turn_seconds", "command_timeout_seconds")
    if client == "api" and provider == "ollama":
        keys += ("context_window_tokens",)
    try:
        row["limits"] = {key: int(value[key]) for key in keys}
        if client == "api":
            row.update(provider=provider, base_url=value["base_url"])
            if value.get("input_per_million") or value.get("output_per_million"):
                if not value.get("input_per_million") or not value.get("output_per_million"):
                    raise ValueError("Podaj obie stawki lub pozostaw obie puste.")
                row["pricing"] = {key: float(value[key]) for key in ("input_per_million", "output_per_million")}
    except (ValueError, KeyError) as exc:
        raise ValueError("Podaj poprawne limity całkowite i obie stawki liczbowe albo pozostaw ceny puste.") from exc
    return row


class DiscoveryModal(LocalizedModal, ModalScreen[dict | None]):
    CSS = """
    DiscoveryModal { align: center middle; background: $background 70%; }
    DiscoveryModal > Vertical { width: 95%; height: 95%; background: #101816; border: round #52684b; padding: 1; }
    DiscoveryModal DataTable { height: 1fr; }
    DiscoveryModal .actions { height: 3; }
    #discovery-notice { height: auto; max-height: 4; }
    """
    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, broker):
        super().__init__()
        self.broker = broker
        self.rows = []

    def compose(self):
        with Vertical():
            yield Static(self.t("Projekty z wybranych katalogów"), classes="heading")
            yield Static(self.t("Skanowanie…"), id="discovery-notice", markup=False)
            yield DataTable(id="discovery-rows", cursor_type="row")
            with Horizontal(classes="actions"):
                yield Button(self.t("Wybierz"), id="discovery-select", variant="primary")
                yield Button(self.t("Anuluj skan"), id="discovery-cancel")
                yield Button(self.t("Zamknij"), id="discovery-close")

    def on_mount(self):
        self.query_one("#discovery-rows", DataTable).add_columns(self.t("Projekt"), self.t("Katalog / pamięć"))
        self.set_interval(.7, self.refresh_results)
        self.refresh_results()

    @work(group="discovery-results", exclusive=True, exit_on_error=False)
    async def refresh_results(self):
        try:
            value = await asyncio.to_thread(self.broker.request, "GET", "/api/discovery")
            rows = value.get("candidates", value.get("projects", []))
            self.rows = rows
            table = self.query_one("#discovery-rows", DataTable)
            coordinate = table.cursor_coordinate
            table.clear()
            for index, row in enumerate(rows):
                table.add_row(plain(row.get("name", self.t("Projekt"))), plain(str(row.get("root", row.get("path", ""))) + (" · snapshot" if row.get("memory_snapshot") else "")), key=str(index))
            if rows:
                table.move_cursor(row=min(coordinate.row, len(rows)-1))
            self.query_one("#discovery-notice", Static).update(plain(f"{value.get('state', 'unknown')} · {len(rows)} " + self.t("Projects") +
                ("\n" + self.t("Operation needs attention.") if value.get("errors") else "")))
        except (OSError, ValueError, RuntimeError) as exc:
            self.query_one("#discovery-notice", Static).update(plain(exc))

    def choose(self):
        table = self.query_one("#discovery-rows", DataTable)
        if table.row_count:
            self.dismiss(self.rows[table.cursor_coordinate.row])

    @on(DataTable.RowSelected, "#discovery-rows")
    def row_selected(self):
        self.choose()

    @on(Button.Pressed)
    def discovery_action(self, event):
        event.stop()
        if event.button.id == "discovery-select":
            self.choose()
        elif event.button.id == "discovery-close":
            self.dismiss(None)
        elif event.button.id == "discovery-cancel":
            self.cancel_scan()

    @work(group="discovery-cancel", exit_on_error=False)
    async def cancel_scan(self):
        try:
            await asyncio.to_thread(self.broker.request, "DELETE", "/api/discovery")
            self.refresh_results()
        except (OSError, ValueError, RuntimeError) as exc:
            self.query_one("#discovery-notice", Static).update(plain(exc))

    def action_close(self):
        self.dismiss(None)


class SidebarApp(BasePanel):
    panel_role = "sidebar"
    TITLE = "Cairnmind"
    CSS = BasePanel.CSS + """
    #sidebar-main { height: 1fr; padding: 0 1; overflow-y: auto; }
    #sidebar-heading { height: auto; color: #a6d8ff; text-style: bold; }
    #project-search { height: 3; }
    #project-rows { height: 1fr; min-height: 4; }
    #sidebar-scope { height: auto; max-height: 5; color: #edf2f7; }
    #project-page { height: 1fr; }
    #project-page Button { width: 100%; margin: 0; }
    #sidebar-empty, #project-state, #sidebar-help { height: auto; color: #b8cbe0; }
    .sidebar-actions { height: 3; }
    .sidebar-actions Button { width: 1fr; min-width: 8; margin-right: 0; padding: 0; }
    .narrow .sidebar-actions { layout: vertical; height: 6; }
    .narrow .sidebar-actions Button { width: 100%; height: 3; }
    """
    BINDINGS = [("w", "work", "Start work"), ("p", "inspect", "Open"),
                ("r", "refresh", "Refresh"), ("m", "memory", "Memory"),
                ("escape", "projects", "Projects"), ("slash", "search", "Search")]

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

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="sidebar-main"):
            yield Static(self.t("Projects"), id="sidebar-heading", markup=False)
            yield Input(placeholder=self.t("Search projects…"), id="project-search")
            yield DataTable(id="project-rows", cursor_type="row", show_header=False)
            yield Static("", id="sidebar-empty", markup=False)
            with VerticalScroll(id="project-page"):
                yield Button(self.t("Return to projects"), id="project-back")
                yield Static("", id="sidebar-scope", markup=False)
                yield Static(self.t("Browsing memory does not start an assistant."), id="project-state", markup=False)
                yield Button(self.t("Choose assistant"), id="assistant-select")
                yield Button(self.t("Start work"), id="project-work", variant="primary", disabled=True)
                yield Button(self.t("Memory"), id="memory-open")
                yield Button(self.t("Add assistant"), id="assistant-add")
            with Horizontal(classes="sidebar-actions", id="project-list-actions"):
                yield Button(self.t("Add folder"), id="project-add", variant="primary")
                yield Button(self.t("Open"), id="project-inspect", disabled=True)
            with Horizontal(classes="sidebar-actions"):
                yield Button(self.t("Connections"), id="auth-open")
                yield Button(self.t("More"), id="more-open")
            yield Static("", id="panel-notice", markup=False)
            yield Static(self.t("Ctrl+B then Space: menu · Tab: next · Esc: back"), id="sidebar-help", markup=False)
        yield Footer()

    def on_mount(self):
        self.query_one("#project-rows", DataTable).add_column(self.t("Projekt"))
        self.show_page("projects")
        self.set_interval(.3, self._poll_workspace)
        self.set_interval(3, self.refresh_sidebar)
        self.start_panel()

    def on_resize(self, event: Resize):
        self.screen.set_class(event.size.width < 40, "narrow")

    async def ready_panel(self):
        await self.load_state()

    async def workspace_changed(self, value):
        self.render_project()
        if value.get("launch_errors") or value.get("last_error"):
            self.notice(self.launch_notice(value))
        else:
            self._update_launch_feedback(value)

    def notice(self, value):
        # A form result or another action owns its own message. Later lifecycle
        # polls must not overwrite it with feedback from an earlier Start.
        self._launch_feedback = None
        self._launch_feedback_state = None
        super().notice(value)

    def selected_native_status(self, value=None):
        """Read the exact selected slot from fresh workspace data, not cached GETs."""
        value = self.workspace_data if value is None else value
        configured = next((s for s in self.configured_slots(value.get("project_id"))
                           if s["id"] == self.selected_slot_id), None)
        profile = next((p for p in self.data.get("profiles", [])
                        if configured and p["id"] == configured["profile_id"]), None)
        slot = next((s for s in value.get("slots", []) if s["id"] == self.selected_slot_id), None)
        if (not profile or not slot or slot.get("client") != profile.get("client")
                or not isinstance(slot.get("run_id"), str) or not slot["run_id"]
                or type(slot.get("generation")) is not int or slot["generation"] < 1):
            return None
        return {"id": slot["run_id"], "generation": slot["generation"], "client": slot["client"],
                "lifecycle": slot.get("lifecycle"), "exit_code": slot.get("exit_code")}

    def _update_launch_feedback(self, value=None, *, begin=False):
        value = self.workspace_data if value is None else value
        run = self.selected_native_status(value)
        identity = ((value.get("project_id"), value.get("worktree"), self.selected_slot_id,
                     run["id"], run["generation"]) if run else None)
        if begin:
            self._launch_feedback = identity
            self._launch_feedback_state = None
            if identity is None:
                self.notice("Start requested. Review the workspace.")
                return
        if self._launch_feedback is None:
            return
        if identity != self._launch_feedback:
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
            message = ("Starting the selected assistant…" if run["lifecycle"] == "starting" else
                       "Assistant started." if run["lifecycle"] == "running" else
                       "Session ended or changed. Review the project before choosing Start work.")
        super().notice(message)

    def launch_notice(self, value):
        if any(row.get("code") == "background_inference_deferred" for row in value.get("launch_errors", [])):
            return "Assistant started. Memory processing may still be paused. Use More → Queue → Process memory."
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
        remembered = next((row["slot_id"] for row in self.workspace_data.get("assistant_selections", [])
                           if (row["project_id"], row["worktree"]) == scope), None)
        valid = {s["id"] for s in slots}
        if remembered in valid:
            self.selected_slot_id = remembered
        elif remembered is not None or self.selected_slot_id not in valid:
            live = next((s for s in slots if self.reusable_run(s, profiles[s["profile_id"]])), None)
            self.selected_slot_id = live["id"] if live else (slots[0]["id"] if slots else None)
        selected = next((s for s in slots if s["id"] == self.selected_slot_id), None)
        profile = next((p for p in self.data.get("profiles", []) if selected and p["id"] == selected["profile_id"]), None)
        self.query_one("#sidebar-scope", Static).update(plain((self.workspace_data.get("project_name") or self.t("Project")) + "\n" + (self.workspace_data.get("worktree") or "")))
        self.query_one("#assistant-select", Button).label = plain(profile["label"] if profile else self.t("Choose assistant"))
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
            labels = getattr(self, "_devbrain_binding_labels", {})
            key = ("w", index, binding.action)
            original = labels.get(key, (binding.description, binding.tooltip, ""))
            labels[key] = (label, original[1], original[2])
        self.refresh_bindings()
        failure = native_failure_notice(fresh or {}, self.language)
        self.query_one("#project-state", Static).update(self.t("Resume returns to the running process. Model changes apply to its next launch.") if live else
            failure or self.t("Browsing memory does not start an assistant."))

    def reusable_run(self, slot=None, profile=None):
        """Mirror NativeRuntime.start's live-slot identity, not profile/model edits."""
        if slot is None:
            slot = next((s for s in self.configured_slots() if s["id"] == self.selected_slot_id), None)
        if profile is None:
            profile = next((p for p in self.data.get("profiles", []) if slot and p["id"] == slot["profile_id"]), None)
        if not slot or not profile:
            return None
        from .workspace import reusable_native_run
        selected = self.workspace_data
        return reusable_native_run(self.data.get("runs", []), selected.get("project_id"),
                                   selected.get("worktree"), profile["client"], slot["id"])

    def assistant_scope(self):
        return (self.workspace_data.get("project_id"), self.workspace_data.get("worktree"), self._scope_version)

    async def remember_assistant(self, slot_id, scope):
        """Persist a view preference only; never turn a delayed choice into work."""
        current = await asyncio.to_thread(self.workspace.read)
        if (scope != self.assistant_scope() or (current.get("project_id"), current.get("worktree")) != scope[:2]
                or not all(scope[:2])):
            raise ValueError(self.t("Select a visible project first."))
        profiles = {p["id"] for p in self.data.get("profiles", [])}
        if not any(s["id"] == slot_id and s["profile_id"] in profiles for s in self.configured_slots(scope[0])):
            raise ValueError(self.t("Choose an assistant before starting work."))
        rows = [row for row in current.get("assistant_selections", [])
                if (row["project_id"], row["worktree"]) != scope[:2]]
        rows.append({"project_id": scope[0], "worktree": scope[1], "slot_id": slot_id})
        await asyncio.to_thread(self.workspace.update, {"assistant_selections": rows[-1000:]},
                                expected_revision=current["revision"])
        await self._poll_workspace()

    @work(group="assistant-select", exclusive=True, exit_on_error=False)
    async def choose_assistant(self):
        scope = self.assistant_scope()
        profiles = {p["id"]: p for p in self.data.get("profiles", [])}
        choices = [(profiles[s["profile_id"]]["label"], s["id"]) for s in self.configured_slots() if s["profile_id"] in profiles]
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
        self.projects = {p["id"]: p for p in value.get("projects", [])}
        self.render_projects()
        self.render_project()

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
        for project in rows:
            table.add_row(plain(project["name"]), key=project["id"])
        if selected:
            for index, project in enumerate(rows):
                if project["id"] == selected:
                    table.move_cursor(row=index)
                    break
        self.query_one("#project-inspect", Button).disabled = not rows
        self.query_one("#sidebar-empty", Static).update("" if rows else self.t("No matching projects. Clear the search or add a folder." if query else "No projects yet. Add a folder to begin."))

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
        project = self.projects.get(identifier)
        if not project:
            raise ValueError(self.t("Select a visible project first."))
        return project

    async def reconcile(self):
        from .workspace import refresh_workspace
        await asyncio.to_thread(refresh_workspace, self.settings, self.workspace_id)
        await self._poll_workspace()

    @work(group="sidebar-select", exclusive=True, exit_on_error=False)
    async def select_project(self, intent, *, focus_start=False, resume_identity=None):
        try:
            returning = resume_identity if intent == "work" else None
            if returning:
                # A Resume click is inspect/attach only, including when the
                # process exits between rendering the button and this request.
                await self.load_state()
                await self._poll_workspace()
                current_run = self.reusable_run()
                if not current_run or (current_run["id"], current_run["generation"]) != (returning["id"], returning["generation"]):
                    self.notice("Session ended or changed. Review the project before choosing Start work.")
                    return
                if any(returning.get(key) is not None and returning[key] != current_run.get(key)
                       for key in ("pid", "process_start")):
                    self.notice("Session ended or changed. Review the project before choosing Start work.")
                    return
            project = self.chosen_project()
            roots = project.get("roots") or [project["root"]]
            selected = self.workspace_data.get("worktree") if self.workspace_data.get("project_id") == project["id"] else project["root"]
            if len(roots) > 1 and (self.page != "project" or selected not in roots):
                selected = await self.push_screen_wait(ChoiceModal(self.t("Katalog pracy"), [(r, r) for r in roots]))
                if selected is None:
                    return
            requested = []
            if intent == "work" and not returning:
                slots = self.configured_slots(project["id"])
                slot_id = self.selected_slot_id if self.selected_slot_id in {s["id"] for s in slots} else (slots[0]["id"] if slots else None)
                if not slot_id:
                    raise ValueError(self.t("Choose an assistant before starting work."))
                requested = [slot_id]
            await asyncio.to_thread(self.workspace.select_project, project, selected,
                                   intent="inspect" if returning else intent, requested_slot_ids=requested)
            if intent == "work":
                changes = {"focus": "client1"}
                if returning:
                    changes["visible_slot_ids"] = [returning["slot_id"]]
                await asyncio.to_thread(self.workspace.update, changes)
            await self.reconcile()
            if returning:
                await self.load_state()
            self.show_page("project")
            if focus_start:
                self.query_one("#project-work", Button).focus()
            current = await asyncio.to_thread(self.workspace.read)
            latest_run = self.reusable_run() if returning else None
            reused = returning and latest_run and all(returning.get(key) == latest_run.get(key)
                for key in ("id", "generation")) and all(returning.get(key) is None or returning[key] == latest_run.get(key)
                for key in ("pid", "process_start")) and (current.get("project_id"), current.get("worktree")) == (
                returning["project_id"], returning["worktree"]) and any(
                    (s.get("run_id"), s.get("generation"), s.get("id"), s.get("client")) ==
                    (returning["id"], returning["generation"], returning["slot_id"], returning["client"])
                    and s.get("lifecycle") in {"starting", "running", "stopping"} for s in current.get("slots", []))
            if current.get("launch_errors") or current.get("last_error"):
                self.notice(self.launch_notice(current))
            elif intent == "work" and not returning:
                self._update_launch_feedback(current, begin=True)
            else:
                self.notice("Returned to the running session. No process was restarted." if reused else
                    "Session ended or changed. Review the project before choosing Start work." if returning else
                    "Project opened. Choose Resume session to return." if self.reusable_run() else
                    "Project opened. Choose Start work when ready.")
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

    def action_inspect(self):
        self.select_project("inspect")

    def action_refresh(self):
        self.refresh_sidebar()

    @work(group="sidebar-scan", exclusive=True, exit_on_error=False)
    async def scan_projects(self):
        try:
            roots = self.data.get("settings", {}).get("scan_roots", [])
            value = await self.push_screen_wait(FormModal(self.t("Szukaj projektów"), [Field("roots", self.t("Jawne katalogi, po jednym w wierszu"), "\n".join(roots), True, True)], note=self.t("Skanowane są tylko wybrane katalogi. Nie uruchamia to klientów."), submit=self.t("Szukaj")))
            if value is None:
                return
            await self.request("POST", "/api/discovery", json={"roots": [r.strip() for r in value["roots"].splitlines() if r.strip()]})
            candidate = await self.push_screen_wait(DiscoveryModal(self.broker))
            if candidate:
                root = candidate.get("root", candidate.get("path"))
                existing = next((p for p in self.projects.values() if p["root"] == root), None)
                if existing:
                    await asyncio.to_thread(self.workspace.select_project, existing, root, intent="inspect")
                    await self.reconcile()
                else:
                    await self.register_project(candidate.get("name") or Path(root).name, root)
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)

    @work(group="sidebar-register", exclusive=True, exit_on_error=False)
    async def add_project(self):
        async def save(value):
            from .cockpit_state import normalize_path
            root = await asyncio.to_thread(normalize_path, value["root"])
            await self.register_project(value["name"] or root.name, str(root))
        await self.push_screen_wait(FormModal("Add folder", [
            Field("root", "Folder path (Windows or WSL)", required=True, folder=True),
            Field("name", "Name (optional; defaults to folder name)")],
            submit="Add folder", on_submit=save))

    async def register_project(self, name, root):
        inspected = await self.request("POST", "/api/projects/inspect", json={"root": root})
        root = inspected.get("root", root)
        snapshot = inspected.get("memory_snapshot")
        if snapshot:
            choice = await self.push_screen_wait(ChoiceModal(self.t("Wykryto przenośną pamięć"), [
                (self.t("Sprawdź i wczytaj — zachowaj tożsamość i historię"), "import"),
                (self.t("Nowy projekt — osobna tożsamość, bez wczytywania historii"), "new")]))
            if choice == "import":
                await self.import_memory_form(snapshot, root, name)
                return
            if choice != "new":
                return
        result = await self.request("POST", "/api/projects", json={"name": name, "root": root})
        project = result.get("project", result)
        await asyncio.to_thread(self.workspace.select_project, project, root, intent="inspect")
        await self.load_state()
        await self.reconcile()
        self.show_page("project")
        self.notice("Project opened. Choose Start work when ready.")

    async def import_memory_form(self, path="", root="", name=""):
        checked = {}
        async def validate(values):
            payload = {"path": values["path"], "root": values["root"]}
            if values["name"]:
                payload["name"] = values["name"]
            checked["preview"] = await self.request("POST", "/api/memory/import", json={**payload, "dry_run": True})
            checked["payload"] = payload
        value = await self.push_screen_wait(FormModal(self.t("Sprawdź import pamięci"), [
            Field("path", self.t("Snapshot lub manifest.json"), path, True),
            Field("root", self.t("Jawny katalog docelowy"), root, True, folder=True),
            Field("name", self.t("Nazwa lokalna (opcjonalnie)"), name)],
            note=self.t("Wczytanie zachowuje tożsamość projektu. Testy z drugiego komputera pozostają historyczne."),
            submit=self.t("Sprawdź import"), on_submit=validate))
        if value is None:
            return
        payload, preview = checked["payload"], checked["preview"]
        approve = await self.push_screen_wait(ConfirmModal(self.t("Wczytać tę pamięć projektu?"), preview, approve=self.t("Wczytaj pamięć")))
        if not approve:
            return
        result = await self.request("POST", "/api/memory/import", json={**payload, "dry_run": False})
        project = result.get("project")
        if project:
            await asyncio.to_thread(self.workspace.select_project, project, payload["root"], intent="inspect")
        await self.load_state()
        await self.reconcile()
        self.notice(self.t("Pamięć wczytana do podglądu; bez uruchamiania klientów."))

    @work(group="sidebar-import", exclusive=True, exit_on_error=False)
    async def import_memory(self):
        try:
            await self.import_memory_form(root=self.workspace_data.get("worktree") or "")
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)

    @work(group="sidebar-memory", exclusive=True, exit_on_error=False)
    async def action_memory(self):
        try:
            await asyncio.to_thread(self.workspace.update, {"selected_entity": None, "focus": "inspector",
                "command_request": {"id": uuid4().hex, "action": "memory", "target_role": "inspector"}})
            await self.reconcile()
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)

    @work(group="sidebar-ask", exclusive=True, exit_on_error=False)
    async def action_ask(self):
        """Ask the chosen project's memory; read-only, nothing is saved."""
        from .terminal_commands import ask_summary
        try:
            project = self.chosen_project()
            values = await self.push_screen_wait(FormModal(
                "Ask memory", [Field("question", "Question", required=True, multiline=True)],
                note="Answers come only from this project's memory, with cited sources. Nothing is saved.",
                submit="Ask"))
            if not values:
                return
            payload = {"project": project["id"], "question": values["question"]}
            if self.workspace_data.get("project_id") == project["id"] and self.workspace_data.get("worktree"):
                payload["worktree"] = self.workspace_data["worktree"]
            self.notice("Asking memory…")
            from .terminal_client import BrokerClient
            if isinstance(self.broker, BrokerClient):
                # A slow answer must not hold the shared client's lock and stall refreshes.
                def ask_once():
                    with BrokerClient(self.settings, ensure=False) as client:
                        return client.request("POST", "/api/ask", json=payload,
                                              timeout=self.settings.ask_timeout + 150)
                result = await asyncio.to_thread(ask_once)
            else:
                result = await asyncio.to_thread(self.broker.request, "POST", "/api/ask", json=payload,
                                                 timeout=self.settings.ask_timeout + 150)
            self.notice("")
            await self.push_screen_wait(MessageModal("Memory answer", ask_summary(result, self.t)))
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)

    @work(group="sidebar-profiles", exclusive=True, exit_on_error=False)
    async def edit_profiles(self, identifier=None, *, advanced=False):
        try:
            profiles = self.data.get("profiles", [])
            if identifier is None:
                identifier = await self.push_screen_wait(ChoiceModal("Connections",
                    [(p["label"], p["id"]) for p in profiles] + [("Add connection", "+")]))
            if identifier is None:
                return
            old = next((p for p in profiles if p["id"] == identifier), None)
            client, provider = (old["client"], old.get("provider")) if old else (None, None)
            if old is None:
                kind = await self.push_screen_wait(ChoiceModal("Connection type", [
                    ("Codex — ChatGPT account", "codex"), ("Claude Code — Claude account", "claude"),
                    ("GitHub Copilot — GitHub account", "copilot"), ("Ollama — local models", "ollama"),
                    ("OpenAI — API key", "openai"), ("Anthropic — API key", "anthropic"),
                    ("OpenAI-compatible API", "openai_compatible")]))
                if kind is None:
                    return
                client = kind if kind in {"codex", "claude", "copilot"} else "api"
                provider = kind if client == "api" else None
            identifier = old["id"] if old else "profile-" + uuid4().hex[:12]
            fields = profile_fields(old, client=client, provider=provider)
            if not advanced:
                fields = [f for f in fields if f.name in {"label", "model"} or
                          (f.name == "base_url" and provider == "openai_compatible")]
            if not old:
                for index, field in enumerate(fields):
                    if field.name == "label":
                        fields[index] = Field("label", "Connection name",
                            {"api": provider, "copilot": "GitHub Copilot", "codex": "Codex", "claude": "Claude"}[client], True)
                    elif field.name == "model" and client == "copilot":
                        fields[index] = Field("model", self.t("Model"), "auto")

            async def save(values):
                from .api_profiles import normalize_profile
                defaults = {f.name: f.value for f in profile_fields(old, client=client, provider=provider)}
                values = {**defaults, **values}
                for field in fields:
                    if field.name in {"max_model_calls", "max_output_tokens", "context_window_tokens", "max_tool_calls", "max_turn_seconds", "command_timeout_seconds"}:
                        try:
                            valid = int(values[field.name]) > 0
                        except ValueError:
                            valid = False
                        if not valid:
                            raise FieldError(field.name, self.t("Enter a positive whole number."))
                try:
                    updated = normalize_profile(profile_from_form(values, identifier=identifier, client=client, provider=provider))
                except ValueError as exc:
                    raise FieldError("model" if "model" in str(exc).lower() else fields[0].name, str(exc)) from exc
                current = await self.request("GET", "/api/state")
                rows = [updated if p["id"] == identifier else p for p in current.get("profiles", [])]
                if not any(p["id"] == identifier for p in rows):
                    rows.append(updated)
                await self.request("PUT", "/api/profiles", json={"profiles": rows})
            value = await self.push_screen_wait(FormModal("Advanced limits and credentials" if advanced else "Edit connection",
                fields, note="Changes apply to the next launch. Saving does not start a model.", on_submit=save))
            if value:
                await self.load_state()
                self.notice("Connection saved. You can connect it or choose it for your project.")
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)

    async def save_slots(self, slots, project_id):
        settings = self.data.get("settings", {})
        payload = {"project_slots": {**settings.get("project_slots", {}), project_id: slots}} if project_id else {"default_slots": slots}
        await self.request("PUT", "/api/settings", json=payload)
        await self.load_state()
        await self.reconcile()

    @work(group="assistant-add", exclusive=True, exit_on_error=False)
    async def add_assistant(self):
        try:
            scope = self.assistant_scope()
            project_id = self.workspace_data.get("project_id")
            if not project_id:
                raise ValueError(self.t("Select a visible project first."))
            profiles = self.data.get("profiles", [])
            choice = await self.push_screen_wait(ChoiceModal("Add assistant",
                [(p["label"], p["id"]) for p in profiles] + [("Add connection", "+")]))
            if scope != self.assistant_scope():
                return
            if choice == "+":
                self.edit_profiles("+")
                return
            if not choice:
                return
            slots = list(self.configured_slots(project_id))
            selected = next((s for s in slots if s["profile_id"] == choice), None)
            if selected is None:
                selected = {"id": "slot-" + uuid4().hex[:12], "profile_id": choice}
                slots.append(selected)
                await self.save_slots(slots, project_id)
            await self.remember_assistant(selected["id"], scope)
            self.notice("An assistant was added. Existing assignments were preserved.")
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)

    @work(group="sidebar-slots", exclusive=True, exit_on_error=False)
    async def edit_slots(self):
        try:
            scope = self.assistant_scope()
            project_id = self.workspace_data.get("project_id")
            profiles = {p["id"]: p for p in self.data.get("profiles", [])}
            slots = list(self.configured_slots(project_id))
            chosen = await self.push_screen_wait(ChoiceModal("Manage assistants",
                [(profiles.get(s["profile_id"], {}).get("label", self.t("Unavailable")), s["id"]) for s in slots]
                + [("Add assistant", "+")]))
            if scope != self.assistant_scope():
                return
            if chosen == "+":
                self.add_assistant()
                return
            if not chosen:
                return
            action = await self.push_screen_wait(ChoiceModal("Assistant", [
                ("Choose assistant", "choose"), ("Replace connection", "replace"),
                ("Move earlier", "up"), ("Remove from layout", "remove")]))
            if scope != self.assistant_scope():
                return
            index = next(i for i, slot in enumerate(slots) if slot["id"] == chosen)
            if action == "choose":
                await self.remember_assistant(chosen, scope)
                return
            if action == "replace":
                profile_id = await self.push_screen_wait(ChoiceModal("Connections", [(p["label"], p["id"]) for p in profiles.values()]))
                if not profile_id:
                    return
                slots[index] = {**slots[index], "profile_id": profile_id}
            elif action == "up" and index:
                slots[index - 1], slots[index] = slots[index], slots[index - 1]
            elif action == "remove":
                if len(slots) < 2:
                    self.notice("Keep at least one assistant in the layout.")
                    return
                if not await self.push_screen_wait(ConfirmModal("Remove from layout?",
                    "This hides the assignment. Existing processes are not stopped.", approve="Remove")):
                    return
                slots.pop(index)
            else:
                return
            await self.save_slots(slots, project_id)
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)

    @work(group="sidebar-auth", exclusive=True, exit_on_error=False)
    async def login(self):
        try:
            await self.load_state()
            choice = await self.push_screen_wait(ChoiceModal("Connections",
                [(p["label"] + " · " + (p.get("model") or p.get("provider") or p["client"]), p["id"])
                 for p in self.data.get("profiles", [])] + [("Add connection", "+")]))
            if choice == "+":
                self.edit_profiles("+")
                return
            if choice:
                profile = next(p for p in self.data["profiles"] if p["id"] == choice)
                await self.profile_connection(profile)
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)

    @work(group="sidebar-more", exclusive=True, exit_on_error=False)
    async def action_more(self):
        action = await self.push_screen_wait(ChoiceModal("More", [
            ("Sessions", "sessions"), ("Manage assistants", "assistants"), ("Find folders", "scan"),
            ("Queue", "queue"),
            ("Snapshots and export", "snapshot"), ("Import memory", "import"),
            ("Settings", "settings"), ("Technical details", "details")]))
        actions = {"sessions": self.sessions, "assistants": self.edit_slots, "scan": self.scan_projects,
                   "queue": self.action_queue,
                   "snapshot": self.action_snapshot, "import": self.import_memory, "settings": self.action_settings}
        if action == "details":
            await self.push_screen_wait(MessageModal("Status", self.workspace_data))
        elif action in actions:
            actions[action]()

    @work(group="sidebar-process-memory", exclusive=True, exit_on_error=False)
    async def action_queue(self):
        try:
            selected = await asyncio.to_thread(self.workspace.read)
            if not selected.get("project_id"):
                raise ValueError(self.t("Select a visible project first."))
            scope = (selected["project_id"], selected.get("worktree"))
            approved = await self.push_screen_wait(ConfirmModal("Queue",
                self.t("Use local models to process pending memory for this project only. No assistant is started.")
                + "\n\n" + str(selected.get("project_name") or scope[0]), approve="Process memory"))
            if not approved:
                return
            current = await asyncio.to_thread(self.workspace.read)
            if (current.get("project_id"), current.get("worktree")) != scope:
                raise ValueError(self.t("Project changed. Open Queue again."))
            result = await self.request("POST", "/api/background-inference/enable",
                                        json={"project": scope[0], "worktree": scope[1]})
            if result.get("accepted") is not True or result.get("project_id") != scope[0]:
                raise ValueError(self.t("Memory processing could not be enabled."))
            current = await asyncio.to_thread(self.workspace.read)
            if (current.get("project_id"), current.get("worktree")) == scope:
                await asyncio.to_thread(self.workspace.update, {"launch_errors": [
                    row for row in current.get("launch_errors", []) if row.get("code") != "background_inference_deferred"]},
                    expected_revision=current["revision"])
                self.notice("Memory processing enabled for this project." if result.get("worker_running") else
                            "Background memory processing is disabled in this service.")
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)

    @work(group="sidebar-settings", exclusive=True, exit_on_error=False)
    async def action_settings(self):
        from .cockpit_state import CockpitState
        store = CockpitState(self.settings)
        values = await asyncio.to_thread(store.load)
        choice = await self.push_screen_wait(ChoiceModal("Settings", [
            ("Language", "language"), ("Layout", "view"), ("Reduce motion", "reduced_motion")]))
        if not choice:
            return
        options = [("English", "en"), ("Polski", "pl")] if choice == "language" else (
            [("Focus", "focus"), ("Cockpit", "cockpit")] if choice == "view" else [("On", "on"), ("Off", "off")])
        selected = await self.push_screen_wait(ChoiceModal("Settings", options))
        if selected is None:
            return
        values = {choice: selected == "on" if choice == "reduced_motion" else selected}
        await asyncio.to_thread(store.update, values)
        self.language = values.get("language", self.language)
        await self.reconcile()
        self.refresh_language()
        self.notice("Preferences saved.")

    def refresh_language(self):
        from .terminal_i18n import localize_bindings
        localize_bindings(self, self.language)
        labels = {"project-back": "Return to projects", "assistant-add": "Add assistant", "project-work": "Start work",
                  "project-add": "Add folder", "project-inspect": "Open", "auth-open": "Connections", "more-open": "More",
                  "memory-open": "Memory"}
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

    @work(group="sidebar-snapshot", exclusive=True, exit_on_error=False)
    async def action_snapshot(self):
        value = {"project_id": self.workspace_data.get("project_id"), "entity_type": "memory", "id": "memory"}
        await asyncio.to_thread(self.workspace.update, {"selected_entity": value, "focus": "inspector"})
        await self.reconcile()

    async def profile_connection(self, profile):
        identifier = profile["id"]
        choices = [("Check connection", "status"), ("Edit connection", "edit"), ("Advanced limits and credentials", "advanced")]
        if profile["client"] in {"codex", "claude"}:
            choices.insert(0, ("Connect account", "native_login"))
        else:
            choices += [("Choose model", "models"), ("Test model and tools", "probe")]
        if profile["client"] == "copilot":
            choices.insert(0, ("Connect GitHub account", "login"))
            choices += [("Prepare Copilot runtime", "prepare")]
        elif profile["client"] == "api":
            choices += [("Set API key", "credential"), ("Remove saved key", "delete")]
        action = await self.push_screen_wait(ChoiceModal(profile["label"], choices))
        if action is None:
            return
        base = "/api/profiles/" + identifier
        if action in {"edit", "advanced"}:
            self.edit_profiles(identifier, advanced=action == "advanced")
        elif action == "native_login":
            from .workspace import show_auth
            await asyncio.to_thread(show_auth, self.settings, profile["client"], self.workspace_id)
            self.notice("Account login opened. Credentials stay outside project memory.")
        elif action == "credential":
            value = await self.push_screen_wait(CredentialModal(profile["label"]))
            if value is not None:
                await self.request("POST", base + "/credential", json=value)
                value.clear()
                await self.load_state()
                self.notice(self.t("Klucz ustawiony. Jego treść nie jest ponownie wyświetlana."))
        elif action == "delete":
            if await self.push_screen_wait(ConfirmModal(self.t("Usunąć klucz?"), self.t("Dotyczy tylko lokalnego klucza tego profilu."), approve=self.t("Usuń klucz"))):
                await self.request("DELETE", base + "/credential")
                self.notice(self.t("Klucz usunięty."))
        elif action == "probe":
            if await self.push_screen_wait(ConfirmModal(self.t("Wykonać test modelu?"), self.t("To rzeczywiste zapytanie do wybranego dostawcy. Może zużyć płatne tokeny lub limit Copilota. Test nie edytuje projektu."), approve=self.t("Uruchom test"))):
                self.notice(self.t("Trwa jawny test modelu i obsługi narzędzi…"))
                result = await asyncio.to_thread(self.broker.request, "POST", base + "/probe", json={}, timeout=180)
                await self.push_screen_wait(MessageModal(self.t("Wynik testu połączenia"), result))
        elif action == "prepare":
            if await self.push_screen_wait(ConfirmModal(self.t("Przygotować Copilota?"), self.t("Pobierze przypięty oficjalny runtime i sprawdzi integralność. Nie uruchamia płatnej inferencji."), approve=self.t("Przygotuj"))):
                self.notice(self.t("Trwa przygotowanie oficjalnego runtime Copilota…"))
                result = await asyncio.to_thread(self.broker.request, "POST", "/api/copilot/prepare", json={}, timeout=300)
                await self.push_screen_wait(MessageModal(self.t("Przygotowanie Copilota"), result))
        elif action == "login":
            local = await self.request("GET", base + "/status")
            runtime = local.get("runtime", {})
            if runtime.get("available") is False:
                consent = await self.push_screen_wait(ConfirmModal("Prepare Copilot?",
                    "Downloads and verifies the pinned official runtime. No model request is made.", approve="Prepare"))
                if not consent:
                    return
                prepared = await asyncio.to_thread(self.broker.request, "POST", "/api/copilot/prepare", json={}, timeout=300)
                if prepared.get("available") is False:
                    await self.push_screen_wait(MessageModal("Copilot setup", prepared))
                    return
            result = await self.request("POST", "/api/copilot/login", json={})
            run = result.get("run")
            if run:
                from .workspace import select_run
                await asyncio.to_thread(select_run, self.settings, run["id"], self.workspace_id)
                await self.reconcile()
                self.notice(self.t("Otworzono operacyjny terminal logowania Copilota. Dane logowania nie trafiają do pamięci."))
            else:
                await self.push_screen_wait(MessageModal(self.t("Logowanie Copilota"), result))
        elif action == "models":
            result = await self.request("GET", base + "/models")
            models = [model for model in result.get("models", []) if model.get("enabled", True)]
            if not models:
                await self.push_screen_wait(MessageModal(self.t("Modele"), result))
                return
            choice = await self.push_screen_wait(ChoiceModal(self.t("Wybierz model — zapis dotyczy następnego uruchomienia"), [(m["id"], m["id"]) for m in models]))
            if choice:
                updated = {**profile, "model": choice}
                rows = [updated if p["id"] == identifier else p for p in self.data.get("profiles", [])]
                await self.request("PUT", "/api/profiles", json={"profiles": rows})
                await self.load_state()
                self.notice(self.t("Model zapisany; nie uruchomiono generowania odpowiedzi."))
        elif action == "status":
            result = await self.request("GET", "/api/auth", params={"client": profile["client"]}) if profile["client"] in {"codex", "claude"} else await self.request("GET", base + "/status")
            if profile["client"] == "copilot":
                result = {**result, "message": self.t("Local setup only. Use Connect GitHub account to sign in; Choose model checks available models.")}
            await self.push_screen_wait(MessageModal("Status", result))

    @work(group="sidebar-sessions", exclusive=True, exit_on_error=False)
    async def sessions(self):
        try:
            await self.load_state()
            runs = self.data.get("runs", [])
            choices = [(f"{r.get('client')} · {r.get('lifecycle', r.get('state'))} · {r.get('worktree') or self.t('Account login')} · " +
                        self.t("Memory acknowledged" if r.get("memory", {}).get("acknowledged") else "Memory acknowledgement required"), r["id"]) for r in runs]
            if not choices:
                self.notice(self.t("Brak uruchomionych sesji."))
                return
            identifier = await self.push_screen_wait(ChoiceModal(self.t("Sesje na tym komputerze"), choices))
            if not identifier:
                return
            run = next(r for r in runs if r["id"] == identifier)
            action = await self.push_screen_wait(ChoiceModal(self.t("Operacja na sesji"), [(self.t("Pokaż / dołącz bez nowego procesu"), "show"), (self.t("Przejmij sterowanie"), "takeover"), (self.t("Przerwij bieżącą operację"), "interrupt"), (self.t("Zatrzymaj proces"), "stop")] + ([(self.t("Wznów zapisaną sesję"), "resume"), (self.t("Nowa sesja z pamięcią projektu"), "new")] if run.get("project_id") else [])))
            from . import workspace as manager
            if action == "show":
                await asyncio.to_thread(manager.select_run, self.settings, identifier, self.workspace_id)
            elif action == "takeover":
                await asyncio.to_thread(manager.take_control, self.settings, identifier, self.workspace_id)
            elif action == "stop":
                await asyncio.to_thread(manager.stop_run, self.settings, identifier, run.get("generation"), self.workspace_id)
            elif action == "interrupt":
                await self.request("POST", f"/api/client-runs/{identifier}/interrupt", json={"generation": run.get("generation")})
            elif action == "resume":
                await asyncio.to_thread(manager.resume_run, self.settings, identifier, self.workspace_id)
            elif action == "new":
                profile = run.get("profile_id") or run["client"]
                result = await self.request("POST", "/api/client-runs", json={"project": run["project_id"], "worktree": run["worktree"], "slots": [{"id": run["slot_id"], "profile_id": profile}], "resume": False, "idempotency_key": uuid4().hex})
                if result.get("errors"):
                    raise ValueError(json.dumps(result["errors"], ensure_ascii=False))
                if result.get("runs"):
                    await asyncio.to_thread(manager.select_run, self.settings, result["runs"][0]["id"], self.workspace_id)
            await self.reconcile()
            await self.load_state()
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(str(exc) + self.t(" · Nowa sesja jest osobną jawną operacją."))

    @on(Button.Pressed)
    def sidebar_action(self, event):
        actions = {"project-work": self.action_work, "project-inspect": self.action_inspect,
                   "project-scan": self.scan_projects, "project-add": self.add_project,
                   "profiles-edit": self.edit_profiles, "slots-edit": self.edit_slots,
                   "sessions-open": self.sessions, "auth-open": self.login,
                   "memory-open": self.action_memory, "memory-import": self.import_memory,
                   "project-back": self.action_projects, "assistant-select": self.choose_assistant,
                   "assistant-add": self.add_assistant, "more-open": self.action_more}
        action = actions.get(event.button.id)
        if action:
            action()


def main():
    parser = argparse.ArgumentParser(description="Prywatny panel terminalowego środowiska Cairnmind")
    parser.add_argument("--kind", choices=["sidebar", "inspector", "graph"], required=True)
    parser.add_argument("--workspace-id", default="default")
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    settings = Settings.load(args.data_dir, create=False)
    if args.kind == "sidebar":
        app = SidebarApp(settings, args.workspace_id)
    elif args.kind == "inspector":
        from .terminal_inspector import InspectorApp
        app = InspectorApp(settings, args.workspace_id)
    else:
        from .terminal_graph import GraphApp
        app = GraphApp(settings, args.workspace_id)
    app.run()


if __name__ == "__main__":
    main()
