"""Terminal-only workspace sidebar and standalone panel entrypoints."""
from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Static, Switch

from . import terminal_theme
from .config import Settings
from .terminal_safe import plain
from .terminal_i18n import native_failure_notice
from .terminal_notices import about as notice_about
from .terminal_i18n_messages import broker_error_text
from .terminal_ui import (BasePanel, ChoiceModal, Field, FieldError, FormModal,
                          LocalizedModal, MessageModal, remeasure_buttons, summary_text)


class ConfirmModal(MessageModal):
    SCROLL_HINT, CLOSE_HINT = "PgUp/PgDn: scroll · Esc: cancel", "Esc: cancel"

    def __init__(self, title, value, *, approve="Confirm", consent=False):
        super().__init__(title, value)
        self.approve = approve
        self.consent = consent

    def compose(self):
        # The heavy yellow frame is kept for consent (a command or third-party tool call, a paid
        # request, a download, loading memory), so it still stands out; a routine confirmation has the
        # ordinary frame. The request is one Static in one colour: nothing in it is dimmed,
        # highlighted, cut or capped.
        with Vertical(classes="cm-dialog -consent" if self.consent else "cm-dialog"):
            yield Static(plain(self.t(self.heading)), classes="heading")
            with VerticalScroll(classes="cm-dialog-body -message"):
                yield Static(plain(summary_text(self.value, getattr(self.app, "language", "en"))), id="message-body")
                if not isinstance(self.value, str):
                    yield Button(self.t("Technical details"), id="message-details")
            with Horizontal(classes="actions"):
                yield Button(self.t(self.approve), id="confirm-yes", variant="primary")
                yield Button(self.t("Cancel"), id="confirm-no")
            yield Static(self.t("PgUp/PgDn: scroll · Esc: cancel"), classes="cm-hint", markup=False)

    def fit_dialog(self):
        super().fit_dialog()
        self.call_after_refresh(self.spare_hint_row)

    def spare_hint_row(self):
        """On a short terminal the key-hint row goes to the request when that one row is what keeps
        the whole request and its note on screen at once. Nothing of the request is ever cut."""
        try:
            body, hint = self.query_one(".cm-dialog-body"), self.query_one(".cm-hint")
        except Exception:
            return
        rows = self.app.size.height - (self.BODY_CHROME - 5)
        spare = (terminal_theme.is_short(self.app) and self.app.size.width >= terminal_theme.NARROW_WIDTH
                 and body.virtual_size.height == rows + 1)
        hint.display = not spare
        if spare:
            body.styles.max_height = rows + 1

    def on_mount(self):
        if not self.consent:
            self.call_after_refresh(self.start_on_cancel)

    def start_on_cancel(self):
        """A routine confirmation whose text fits starts on Cancel, so the focus shows and Enter cancels.
        A consent request, or a text that scrolls, starts on the text, where no key decides anything."""
        with suppress(Exception):
            body = self.query_one(".cm-dialog-body")
            if body.virtual_size.height <= body.scrollable_content_region.height and self.focused is body:
                self.query_one("#confirm-no", Button).focus()

    @on(Button.Pressed)
    def confirm_button(self, event):
        if event.button.id in {"confirm-yes", "confirm-no"}:
            event.stop()
            self.dismiss(event.button.id == "confirm-yes")

    def action_close(self):
        self.dismiss(False)


class CredentialModal(LocalizedModal, ModalScreen[dict | None]):
    """Write-only credential entry; a stored secret is never loaded into this form."""
    BODY_CHROME = 9  # frame, padding, title, error line, actions and blank rows; no hint row
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, label):
        super().__init__()
        self.profile_label = label

    def compose(self):
        with Vertical(classes="cm-dialog"):
            yield Static(plain(self.t("API key") + " — " + self.profile_label), classes="heading")
            with VerticalScroll(classes="cm-dialog-body"):
                yield Static(self.t("The key stays outside project memory. By default it lasts only for this engine session."), markup=False)
                yield Label(self.t("New key (saved keys are never displayed)"), classes="form-label")
                yield Input(password=True, id="credential-key")
                with Horizontal(id="credential-remember"):
                    yield Switch(False, id="credential-persist")
                    yield Label(self.t("Remember on this computer"))
                yield Static(self.t("Remembering creates a private, unencrypted file outside the project. It is not included in snapshots."), markup=False, classes="cm-note")
            yield Static("", id="credential-error", markup=False)
            with Horizontal(classes="actions"):
                yield Button(self.t("Set API key"), id="credential-save", variant="primary")
                yield Button(self.t("Cancel"), id="credential-cancel")

    @on(Button.Pressed)
    def credential_button(self, event):
        event.stop()
        if event.button.id == "credential-cancel":
            self.action_cancel()
        elif event.button.id == "credential-save":
            key_input = self.query_one("#credential-key", Input)
            value = key_input.value.strip()
            if not value:
                self.query_one("#credential-error", Static).update(self.t("Enter a new key."))
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
    fields = [Field("label", "Connection name", old.get("label", ""), True)]
    if client in {"codex", "claude"}:
        return fields + [Field("client", "Client: codex / claude", client, True),
                         Field("model", "Model (blank = provider default)", old.get("model") or "")]
    fields.append(Field("model", "Model" if client == "api" else "Model (blank = provider default)", old.get("model") or "", client == "api"))
    if client == "api":
        defaults = {"openai": "https://api.openai.com/v1", "anthropic": "https://api.anthropic.com/v1",
                    "openai_compatible": "http://127.0.0.1:8000/v1", "ollama": "http://127.0.0.1:11434"}
        fields.append(Field("base_url", "API address", old.get("base_url") or defaults[provider], True))
    fields.append(Field("credential_ref", "Credential reference: env:NAME or profile:ID (blank = no key)", old.get("credential_ref") or ""))
    limits = old.get("limits") or {}
    if client == "api":
        fields += [Field("max_model_calls", "Maximum model calls per turn", str(limits.get("max_model_calls", 20)), True),
                   Field("max_output_tokens", "Maximum output tokens", str(limits.get("max_output_tokens", 4096)), True)]
        if provider == "ollama":
            fields.append(Field("context_window_tokens", "Ollama context tokens (uses RAM/VRAM)", str(limits.get("context_window_tokens", 32768)), True))
    else:
        fields += [Field("max_tool_calls", "Maximum tool calls per turn", str(limits.get("max_tool_calls", 20)), True),
                   Field("max_turn_seconds", "Maximum turn duration (seconds)", str(limits.get("max_turn_seconds", 900)), True)]
    fields.append(Field("command_timeout_seconds", "Command timeout (seconds)", str(limits.get("command_timeout_seconds", 120)), True))
    if client == "api" and provider != "ollama":
        pricing = old.get("pricing") or {}
        fields += [Field("input_per_million", "Input USD per million tokens (optional)", str(pricing.get("input_per_million", ""))),
                   Field("output_per_million", "Output USD per million tokens (optional)", str(pricing.get("output_per_million", "")))]
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
                    raise ValueError("Give both rates or leave both empty.")
                row["pricing"] = {key: float(value[key]) for key in ("input_per_million", "output_per_million")}
    except (ValueError, KeyError) as exc:
        raise ValueError("Give whole-number limits and both rates as numbers, or leave the prices empty.") from exc
    return row


class DiscoveryModal(LocalizedModal, ModalScreen[dict | None]):
    BODY_CHROME = 12  # frame, padding, title, notice (up to 4 rows), actions and blank rows; no hint row
    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, broker):
        super().__init__()
        self.broker = broker
        self.rows = []

    def compose(self):
        with Vertical(classes="cm-dialog -wide"):
            yield Static(self.t("Projects in selected folders"), classes="heading")
            yield Static(self.t("Scanning…"), id="discovery-notice", markup=False)
            yield DataTable(id="discovery-rows", cursor_type="row", classes="cm-dialog-body")
            with Horizontal(classes="actions"):
                yield Button(self.t("Select"), id="discovery-select", variant="primary")
                yield Button(self.t("Cancel scan"), id="discovery-cancel")
                yield Button(self.t("Close"), id="discovery-close")

    def on_mount(self):
        self.query_one("#discovery-rows", DataTable).add_columns(self.t("Project"), self.t("Folder / memory"))
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
                table.add_row(plain(row.get("name", self.t("Project"))), plain(str(row.get("root", row.get("path", ""))) + (" · snapshot" if row.get("memory_snapshot") else "")), key=str(index))
            if rows:
                table.move_cursor(row=min(coordinate.row, len(rows)-1))
            self.query_one("#discovery-notice", Static).update(plain(f"{value.get('state', 'unknown')} · {len(rows)} " + self.t("Projects") +
                ("\n" + self.t("Operation needs attention.") if value.get("errors") else "")))
        except (OSError, ValueError, RuntimeError) as exc:
            self.query_one("#discovery-notice", Static).update(plain(broker_error_text(exc, self.t)))

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
            self.query_one("#discovery-notice", Static).update(plain(broker_error_text(exc, self.t)))

    def action_close(self):
        self.dismiss(None)


class SidebarApp(BasePanel):
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
            self.notice(self.launch_notice(value))
        else:
            self._update_launch_feedback(value)

    def notice(self, value, kind="info"):
        # A form result or another action owns its own message. Later lifecycle
        # polls must not overwrite it with feedback from an earlier Start.
        self._launch_feedback = None
        self._launch_feedback_state = None
        super().notice(value, kind)

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
        # Two parts this panel knows apart: the name stands out, the path is quiet. The text is unchanged.
        self.query_one("#sidebar-scope", Static).update(terminal_theme.assemble([
            ("title", self.workspace_data.get("project_name") or self.t("Project")), ("value", "\n"),
            ("muted", self.workspace_data.get("worktree") or "")]))
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
            labels = getattr(self, "_cairnmind_binding_labels", {})
            key = ("w", index, binding.action)
            original = labels.get(key, (binding.description, binding.tooltip, ""))
            labels[key] = (label, original[1], original[2])
        self.refresh_bindings()
        failure = native_failure_notice(fresh or {}, self.language)
        self.query_one("#project-state", Static).set_class(bool(failure) and not live, "-warn")
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
        names = [plain(project["name"]) for project in rows]
        self._longest_name = max([name.cell_len for name in names], default=0)
        for project, name in zip(rows, names):
            table.add_row(name, key=project["id"])
        self.call_after_refresh(self.fill_project_rows)
        if selected:
            for index, project in enumerate(rows):
                if project["id"] == selected:
                    table.move_cursor(row=index)
                    break
        self.query_one("#project-inspect", Button).disabled = not rows
        self.query_one("#sidebar-main").set_class(not rows, "-empty")
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
    async def select_project(self, intent, *, focus_start=False, resume_identity=None, focus_id="project-work"):
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
                selected = await self.push_screen_wait(ChoiceModal(self.t("Working folder"), [(r, r) for r in roots]))
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
                    # Bring the resumed session into view without dropping another visible one (#155).
                    shown = list((await asyncio.to_thread(self.workspace.read)).get("visible_slot_ids") or [])
                    if returning["slot_id"] not in shown:
                        shown = [returning["slot_id"], *shown][:2]
                    changes.update(visible_slot_ids=shown,
                                   focus="client2" if shown.index(returning["slot_id"]) else "client1")
                await asyncio.to_thread(self.workspace.update, changes)
            await self.reconcile()
            if returning:
                await self.load_state()
            self.show_page("project")
            if focus_start:
                self.query_one("#" + focus_id, Button).focus()
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

    def action_prepare_next_stage(self):
        # The same rule for Run next stage: a menu request only shows the current project.
        self.select_project("inspect", focus_start=True, focus_id="stage-next")

    def action_inspect(self):
        self.select_project("inspect")

    def action_refresh(self):
        self.refresh_sidebar()

    @work(group="sidebar-scan", exclusive=True, exit_on_error=False)
    async def scan_projects(self):
        try:
            roots = self.data.get("settings", {}).get("scan_roots", [])
            value = await self.push_screen_wait(FormModal(self.t("Find folders"), [Field("roots", self.t("Folders to scan, one per line"), "\n".join(roots), True, True)], note=self.t("Only selected folders are scanned. No client is started."), submit=self.t("Search")))
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
            choice = await self.push_screen_wait(ChoiceModal(self.t("Project memory found"), [
                (self.t("Review and import — keep identity and history"), "import"),
                (self.t("New project — separate identity, without existing history"), "new")]))
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
        value = await self.push_screen_wait(FormModal(self.t("Check memory import"), [
            Field("path", self.t("Snapshot or manifest.json"), path, True),
            Field("root", self.t("Destination folder"), root, True, folder=True),
            Field("name", self.t("Local name (optional)"), name)],
            note=self.t("Import preserves project identity. Checks from another computer remain historical."),
            submit=self.t("Check import"), on_submit=validate))
        if value is None:
            return
        payload, preview = checked["payload"], checked["preview"]
        approve = await self.push_screen_wait(ConfirmModal(self.t("Import this project memory?"), preview, approve=self.t("Import memory"), consent=True))
        if not approve:
            return
        result = await self.request("POST", "/api/memory/import", json={**payload, "dry_run": False})
        project = result.get("project")
        if project:
            await asyncio.to_thread(self.workspace.select_project, project, payload["root"], intent="inspect")
        await self.load_state()
        await self.reconcile()
        self.notice(self.t("Memory imported for viewing. No clients started."))

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
        subject = None  # the project asked about, which may not be the one shown when the answer or error comes
        try:
            project = self.chosen_project()
            subject = notice_about(project["id"], project.get("name"))
            values = await self.push_screen_wait(FormModal(
                "Ask memory", [Field("question", "Question", required=True, multiline=True)],
                note="Answers come only from this project's memory, with cited sources. Nothing is saved.",
                submit="Ask"))
            if not values:
                return
            payload = {"project": project["id"], "question": values["question"]}
            if self.workspace_data.get("project_id") == project["id"] and self.workspace_data.get("worktree"):
                payload["worktree"] = self.workspace_data["worktree"]
                subject = notice_about(project["id"], project.get("name"), payload["worktree"])
            self.notice_for(subject, "Asking memory…")
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
            self.notice_for(subject, exc)

    @work(group="sidebar-brief", exclusive=True, exit_on_error=False)
    async def action_brief(self):
        """Show where work stopped in the chosen project; read-only and without a model."""
        subject = None
        try:
            project = self.chosen_project()
            params = {"project": project["id"]}
            subject = notice_about(project["id"], project.get("name"))
            if self.workspace_data.get("project_id") == project["id"] and self.workspace_data.get("worktree"):
                params["worktree"] = self.workspace_data["worktree"]
                subject = notice_about(project["id"], project.get("name"), params["worktree"])
            # Check evidence hashes the worktree: allow more than the default 20 s, and use a
            # separate client so a slow brief does not hold the shared client's lock.
            from .terminal_client import BrokerClient
            if isinstance(self.broker, BrokerClient):
                def brief_once():
                    with BrokerClient(self.settings, ensure=False) as client:
                        return client.request("GET", "/api/brief", params=params, timeout=120)
                result = await asyncio.to_thread(brief_once)
            else:
                result = await asyncio.to_thread(self.broker.request, "GET", "/api/brief", params=params, timeout=120)
            text = result.get("text") if isinstance(result, dict) else None
            await self.push_screen_wait(MessageModal("Brief", text or self.t("The memory service returned no brief.")))
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice_for(subject, exc)

    # The group of Start work: choosing another project cancels a stage in flight, and back.
    @work(group="sidebar-select", exclusive=True, exit_on_error=False)
    async def action_next_stage(self):
        """Start the assistant or run the check mapped to the next plan step; changes no task."""
        from .workspace_stage import run_next_stage
        try:
            await run_next_stage(self)
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)

    @work(group="sidebar-stages", exclusive=True, exit_on_error=False)
    async def edit_stages(self):
        from .workspace_stage import edit_stages
        try:
            await edit_stages(self)
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)

    @work(group="sidebar-mcp", exclusive=True, exit_on_error=False)
    async def edit_mcp_servers(self):
        """Only a person adds or removes a project's MCP servers, here (#126)."""
        from .workspace_mcp import edit_mcp_servers
        try:
            await edit_mcp_servers(self)
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
            title = "Advanced limits and credentials" if advanced else "Edit connection" if old else "Add connection"
            value = await self.push_screen_wait(FormModal(title,
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
            ("Sessions", "sessions"), ("Manage assistants", "assistants"),
            ("Find folders", "scan"),
            ("Queue", "queue"),
            ("Snapshots and export", "snapshot"), ("Import memory", "import"),
            # New entries go last: keyboard paths to the existing ones keep their positions.
            ("Settings", "settings"), ("Technical details", "details"), ("Plan stages", "stages"),
            ("MCP servers", "mcp"),
            ("Notices", "notices")]))
        actions = {"sessions": self.sessions, "assistants": self.edit_slots, "stages": self.edit_stages,
                   "mcp": self.edit_mcp_servers,
                   "scan": self.scan_projects,
                   "queue": self.action_queue,
                   "snapshot": self.action_snapshot, "import": self.import_memory, "settings": self.action_settings,
                   "notices": self.action_notices}
        if action == "details":
            await self.push_screen_wait(MessageModal("Status", self.workspace_data))
        elif action in actions:
            actions[action]()

    @work(group="sidebar-notices", exclusive=True, exit_on_error=False)
    async def action_notices(self):
        """The notices this sidebar showed, newest first; opening clears the mark on the notice line."""
        self.notice_history.mark_seen()
        self.show_notice_mark()
        text = self.notice_history.lines(self.workspace_data, self.t)
        await self.push_screen_wait(MessageModal("Notices", text or self.t("No notices yet.")))

    @work(group="sidebar-process-memory", exclusive=True, exit_on_error=False)
    async def action_queue(self):
        subject = None
        try:
            selected = await asyncio.to_thread(self.workspace.read)
            if not selected.get("project_id"):
                raise ValueError(self.t("Select a visible project first."))
            scope = (selected["project_id"], selected.get("worktree"))
            subject = notice_about(scope[0], selected.get("project_name"), scope[1])
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
                self.notice_for(subject, "Memory processing enabled for this project." if result.get("worker_running") else
                                "Background memory processing is disabled in this service.")
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice_for(subject, exc)

    @work(group="sidebar-settings", exclusive=True, exit_on_error=False)
    async def action_settings(self):
        from .cockpit_state import CockpitState
        store = CockpitState(self.settings)
        values = await asyncio.to_thread(store.load)
        choice = await self.push_screen_wait(ChoiceModal("Settings", [
            ("Language", "language"), ("Layout", "view"), ("Reduce motion", "reduced_motion"),
            ("Activity column", "activity_column")]))
        if not choice:
            return
        options = [("English", "en"), ("Polski", "pl")] if choice == "language" else (
            [("Focus", "focus"), ("Cockpit", "cockpit")] if choice == "view" else [("On", "on"), ("Off", "off")])
        titles = {"language": "Language", "view": "Layout", "reduced_motion": "Reduce motion",
                  "activity_column": "Activity column"}
        selected = await self.push_screen_wait(ChoiceModal(titles[choice], options))
        if selected is None:
            return
        values = {choice: selected == "on" if choice in {"reduced_motion", "activity_column"} else selected}
        await asyncio.to_thread(store.update, values)
        self.language = values.get("language", self.language)
        await self.reconcile()
        self.refresh_language()
        remeasure_buttons(self)
        self.notice("Preferences saved.")

    def refresh_language(self):
        from .terminal_i18n import localize_bindings
        localize_bindings(self, self.language)
        labels = {"project-back": "Return to projects", "assistant-add": "Add assistant", "project-work": "Start work",
                  "project-add": "Add folder", "project-inspect": "Open", "auth-open": "Connections", "more-open": "More",
                  "memory-open": "Memory", "stage-next": "Run next stage"}
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
                self.notice(self.t("Key saved. Its value will not be displayed again."))
        elif action == "delete":
            if await self.push_screen_wait(ConfirmModal(self.t("Remove saved key?"), self.t("Only the local key for this connection will be removed."), approve=self.t("Remove saved key"))):
                await self.request("DELETE", base + "/credential")
                self.notice(self.t("Key removed."))
        elif action == "probe":
            if await self.push_screen_wait(ConfirmModal(self.t("Test this model?"), self.t("This sends a real request to the provider and can use paid tokens or Copilot quota. It does not edit your project."), approve=self.t("Run test"), consent=True)):
                self.notice(self.t("Testing the model and tools…"))
                result = await asyncio.to_thread(self.broker.request, "POST", base + "/probe", json={}, timeout=180)
                await self.push_screen_wait(MessageModal(self.t("Connection test result"), result))
        elif action == "prepare":
            if await self.push_screen_wait(ConfirmModal(self.t("Prepare Copilot?"), self.t("Downloads and verifies the pinned official runtime. No model request is made."), approve=self.t("Prepare"), consent=True)):
                self.notice(self.t("Preparing the official Copilot runtime…"))
                result = await asyncio.to_thread(self.broker.request, "POST", "/api/copilot/prepare", json={}, timeout=300)
                await self.push_screen_wait(MessageModal(self.t("Copilot setup"), result))
        elif action == "login":
            local = await self.request("GET", base + "/status")
            runtime = local.get("runtime", {})
            if runtime.get("available") is False:
                consent = await self.push_screen_wait(ConfirmModal("Prepare Copilot?",
                    "Downloads and verifies the pinned official runtime. No model request is made.", approve="Prepare", consent=True))
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
                self.notice(self.t("Copilot login opened. Credentials stay outside project memory."))
            else:
                await self.push_screen_wait(MessageModal(self.t("Copilot login"), result))
        elif action == "models":
            result = await self.request("GET", base + "/models")
            models = [model for model in result.get("models", []) if model.get("enabled", True)]
            if not models:
                await self.push_screen_wait(MessageModal(self.t("Models"), result))
                return
            choice = await self.push_screen_wait(ChoiceModal(self.t("Choose model — applies to the next launch"), [(m["id"], m["id"]) for m in models]))
            if choice:
                updated = {**profile, "model": choice}
                rows = [updated if p["id"] == identifier else p for p in self.data.get("profiles", [])]
                await self.request("PUT", "/api/profiles", json={"profiles": rows})
                await self.load_state()
                self.notice(self.t("Model saved. No generation started."))
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
            from .terminal_activity_data import run_is_live, session_label
            # The memory state comes before the folder, so a narrow list cuts the path, never the state.
            choices = [(session_label(r, self.t), r["id"]) for r in runs]
            if not choices:
                self.notice(self.t("No sessions yet."))
                return
            identifier = await self.push_screen_wait(ChoiceModal(self.t("Sessions on this computer"), choices))
            if not identifier:
                return
            run = next(r for r in runs if r["id"] == identifier)
            # An ended run has no process: stopping, interrupting or taking control of it is not offered.
            live = [(self.t("Take control"), "takeover"), (self.t("Interrupt current operation"), "interrupt"), (self.t("Stop process"), "stop")] if run_is_live(run) else []
            action = await self.push_screen_wait(ChoiceModal(self.t("Session actions"), [(self.t("Show / attach without a new process"), "show"), *live] + ([(self.t("Resume saved session"), "resume"), (self.t("New session with project memory"), "new")] if run.get("project_id") else [])))
            from . import workspace as manager
            if action == "show":
                await asyncio.to_thread(manager.select_run, self.settings, identifier, self.workspace_id)
            elif action == "takeover":
                await asyncio.to_thread(manager.take_control, self.settings, identifier, self.workspace_id)
            elif action == "stop":
                await asyncio.to_thread(manager.stop_run, self.settings, identifier, run.get("generation"), self.workspace_id)
                self.notice("The session was stopped.")
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
            self.notice(broker_error_text(exc, self.t) + self.t(" · Starting a new session is a separate action."))

    @on(Button.Pressed)
    def sidebar_action(self, event):
        actions = {"project-work": self.action_work, "project-inspect": self.action_inspect,
                   "project-scan": self.scan_projects, "project-add": self.add_project,
                   "profiles-edit": self.edit_profiles, "slots-edit": self.edit_slots,
                   "sessions-open": self.sessions, "auth-open": self.login,
                   "memory-open": self.action_memory, "memory-import": self.import_memory,
                   "project-back": self.action_projects, "assistant-select": self.choose_assistant,
                   "assistant-add": self.add_assistant, "more-open": self.action_more,
                   "stage-next": self.action_next_stage}
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
        from .terminal_activity import activity_app
        app = activity_app(settings, args.workspace_id, popup=args.popup)
    elif args.kind == "sidebar":
        app = SidebarApp(settings, args.workspace_id)
    elif args.kind == "inspector":
        from .terminal_inspector import InspectorApp
        app = InspectorApp(settings, args.workspace_id)
    else:
        from .terminal_graph import GraphApp
        app = GraphApp(settings, args.workspace_id)
    app.run_start_request = args.run_request
    app.run()


if __name__ == "__main__":
    main()
