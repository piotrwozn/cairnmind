"""Shared literal-text controls for terminal-only workspace panels."""
from __future__ import annotations

import asyncio
import json
import os
import inspect
from pathlib import Path
from contextlib import suppress
from dataclasses import dataclass

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Static, TextArea
from textual.worker import WorkerState

from .terminal_safe import plain, safe_text
from .terminal_i18n import language_for, localize_bindings, tr


class LocalizedModal:
    def t(self, text, **values):
        return tr(text, getattr(self.app, "language", "en"), **values)

    def on_mount(self):
        localize_bindings(self, getattr(self.app, "language", "en"))


class FieldError(ValueError):
    def __init__(self, field, message):
        super().__init__(message)
        self.field = field


def summary_text(value, language="en"):
    """Readable, bounded status; full structured payload is an explicit detail view."""
    if isinstance(value, str):
        return tr(value, language)
    if not isinstance(value, dict):
        return tr("Operation completed.", language)
    lines = []
    project = value.get("project")
    if isinstance(project, dict):
        for key, label in (("name", "Project"), ("root", "Working folder")):
            if isinstance(project.get(key), str):
                lines.append(f"{tr(label, language)}: {safe_text(project[key])[:500]}")
    counts = value.get("counts")
    if isinstance(counts, dict):
        entries = [f"{tr(label, language)}: {counts[key]}"
                   for key, label in (("records", "Records"), ("sources", "Sources"), ("plans", "Plans"), ("tasks", "Tasks"))
                   if type(counts.get(key)) is int]
        if entries:
            lines.append(" · ".join(entries))
    for key in ("message", "summary", "status", "state", "code", "reason"):
        item = value.get(key)
        if isinstance(item, (str, int, float, bool)):
            lines.append(safe_text(item)[:500])
    if "authenticated" in value:
        lines.insert(0, tr("Connected" if value["authenticated"] else "Not connected", language))
    if value.get("verification") == "historical":
        lines.append(tr("Imported checks are historical.", language))
    for key in ("warnings", "errors", "conflicts"):
        if value.get(key):
            lines.append(f"{tr('Operation needs attention.', language)} ({len(value[key]) if isinstance(value[key], list) else 1})")
    return "\n".join(lines[:6]) or tr("Operation completed.", language)


async def wide_dialog(app, screen, role, open_screen):
    """Temporarily zoom a real workspace pane; preserve explicit user changes."""
    if getattr(app, "is_headless", False) or not os.environ.get("TMUX_PANE"):
        return await open_screen(screen)
    from .workspace import refresh_workspace
    before = await asyncio.to_thread(app.workspace.read)
    changed = (before.get("focus"), before.get("zoom", False)) != (role, True)
    applied = False
    try:
        if changed:
            await asyncio.to_thread(app.workspace.update, {"focus": role, "zoom": True})
            applied = True
            await asyncio.to_thread(refresh_workspace, app.settings, app.workspace_id)
            await asyncio.sleep(.08)
        return await open_screen(screen)
    finally:
        if applied:
            current = await asyncio.to_thread(app.workspace.read)
            if (current.get("focus"), current.get("zoom")) == (role, True):
                await asyncio.to_thread(app.workspace.update, {"focus": before.get("focus", role), "zoom": before.get("zoom", False)})
                await asyncio.to_thread(refresh_workspace, app.settings, app.workspace_id)


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    value: str = ""
    required: bool = False
    multiline: bool = False
    validator: object = None
    folder: bool = False


class FormModal(LocalizedModal, ModalScreen[dict | None]):
    CSS = """
    FormModal { align: center middle; background: $background 70%; }
    FormModal > VerticalScroll { width: 88%; max-width: 100; height: auto; max-height: 95%;
        border: round #52684b; background: #111a18; padding: 1 2; }
    FormModal .form-title { text-style: bold; color: #c3df9b; height: auto; margin-bottom: 1; }
    FormModal .form-label { height: auto; margin-top: 1; }
    FormModal TextArea { height: 7; }
    FormModal .form-actions { height: 3; margin-top: 1; }
    FormModal .form-actions Button { margin-right: 1; }
    FormModal #form-error { height: auto; color: #e7b594; }
    """
    BINDINGS = [("escape", "cancel", "Cancel"), ("ctrl+s", "submit", "Save")]

    def __init__(self, title: str, fields: list[Field], *, note: str = "", submit="Save", on_submit=None):
        super().__init__()
        self.title_text, self.fields, self.note, self.submit_label = title, fields, note, submit
        self.on_submit = on_submit
        self.saving = False

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(plain(self.t(self.title_text)), classes="form-title")
            if self.note:
                yield Static(plain(self.t(self.note)))
            for field in self.fields:
                yield Label(plain(self.t(field.label)), classes="form-label")
                if field.multiline:
                    yield TextArea(field.value, id="field-" + field.name)
                else:
                    yield Input(field.value, id="field-" + field.name)
                if field.folder:
                    yield Button(self.t("Browse folders"), id="browse-" + field.name)
            yield Static("", id="form-error", markup=False)
            with Horizontal(classes="form-actions"):
                yield Button(self.t(self.submit_label), id="form-submit", variant="primary")
                yield Button(self.t("Cancel"), id="form-cancel")

    def on_mount(self):
        if self.fields:
            self.query_one("#field-" + self.fields[0].name).focus()

    @on(Button.Pressed)
    def form_button(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "form-cancel":
            self.action_cancel()
        elif event.button.id == "form-submit":
            self.action_submit()
        elif event.button.id and event.button.id.startswith("browse-"):
            self.choose_folder(event.button.id.removeprefix("browse-"))

    @work(group="form-folder", exclusive=True, exit_on_error=False)
    async def choose_folder(self, field_name):
        widget = self.query_one("#field-" + field_name, Input)
        path = await self.app.push_screen_wait(FolderPicker(widget.value or None))
        if path:
            widget.value = path
            widget.focus()

    @work(group="form-submit", exit_on_error=False)
    async def action_submit(self):
        if self.saving:
            return
        self.saving = True
        button = self.query_one("#form-submit", Button)
        button.disabled = True
        self.query_one("#form-cancel", Button).disabled = True
        self.query_one("#form-error", Static).update("")
        try:
            values = {}
            for field in self.fields:
                widget = self.query_one("#field-" + field.name)
                value = widget.text if isinstance(widget, TextArea) else widget.value
                if field.required and not value.strip():
                    raise FieldError(field.name, self.t("Required field") + ": " + self.t(field.label))
                if field.validator is not None:
                    error = field.validator(value.strip())
                    if error:
                        raise FieldError(field.name, self.t(error))
                values[field.name] = value.strip()
            if self.on_submit is not None:
                result = self.on_submit(values)
                if inspect.isawaitable(result):
                    await result
            self.dismiss(values)
        except (OSError, ValueError, RuntimeError) as exc:
            self.query_one("#form-error", Static).update(plain(self.t(str(exc))))
            if isinstance(exc, FieldError):
                self.query_one("#field-" + exc.field).focus()
        finally:
            self.saving = False
            if self.is_mounted:
                button.disabled = False
                self.query_one("#form-cancel", Button).disabled = False

    def action_cancel(self) -> None:
        # A submitted mutation must settle before closing; cancellation must
        # not imply that a remote write has been rolled back.
        if not self.saving:
            self.dismiss(None)


class MessageModal(LocalizedModal, ModalScreen[None]):
    CSS = """
    MessageModal { align: center middle; background: $background 70%; }
    MessageModal > Vertical { width: 90%; max-width: 120; height: 90%; border: round #52684b;
      background: #111a18; padding: 1 2; }
    MessageModal VerticalScroll { height: 1fr; }
    MessageModal Button { margin-top: 1; }
    """
    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, title: str, value):
        super().__init__()
        self.heading = title
        self.value = value
        self.details = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(plain(self.t(self.heading)), classes="heading")
            with VerticalScroll():
                yield Static(plain(summary_text(self.value, getattr(self.app, "language", "en"))), id="message-body")
            if not isinstance(self.value, str):
                yield Button(self.t("Technical details"), id="message-details")
            yield Button(self.t("Close"), id="message-close")

    @on(Button.Pressed, "#message-details")
    def toggle_details(self, event):
        event.stop()
        self.details = not self.details
        self.query_one("#message-body", Static).update(plain(json.dumps(self.value, ensure_ascii=False, indent=2)
            if self.details else summary_text(self.value, getattr(self.app, "language", "en"))))
        event.button.label = self.t("Hide technical details" if self.details else "Technical details")

    @on(Button.Pressed, "#message-close")
    def close_button(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class BasePanel(App):
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { background: #101720; color: #edf2f7; }
    Header { height: 1; background: #1b2938; color: #e3edf7; }
    Footer { background: #1b2938; }
    Button { min-width: 10; border: tall #526579; margin-right: 1; }
    Button.-primary { background: #2865a0; color: #ffffff; }
    Button:focus { border: tall #a6d8ff; text-style: bold; }
    Input, TextArea { border: tall #526579; background: #152131; }
    Input:focus, TextArea:focus { border: tall #a6d8ff; }
    Select { border: none; background: #152131; }
    SelectCurrent { border: tall #526579; background: #152131; }
    DataTable { background: #101720; }
    DataTable:focus { border-left: solid #a6d8ff; }
    .heading { color: #a6d8ff; text-style: bold; height: auto; }
    .actions { height: auto; min-height: 3; }
    #panel-notice { height: auto; max-height: 5; color: #dcb78c; padding: 0 1; }
    #notice-mark { display: none; height: auto; color: #8fa3b8; padding: 0 1; }
    """

    def __init__(self, settings, workspace_id="default", *, broker=None, workspace=None):
        super().__init__()
        from .terminal_client import BrokerClient
        from .workspace_state import WorkspaceState
        self.settings = settings
        self.workspace_id = workspace_id
        self.broker = broker if broker is not None else BrokerClient(settings, ensure=False)
        self.workspace = workspace if workspace is not None else WorkspaceState(settings, workspace_id)
        self.workspace_data = {}
        from .terminal_notices import NoticeHistory
        self.notice_history = NoticeHistory()  # memory only, for the life of this panel (#130)
        self._notice_about, self._mark_text = None, ""
        self._scope_version = 0
        self._workspace_revision = None
        self.language = language_for(settings)
        localize_bindings(self, self.language)
        self._last_command_id = self.workspace.read().get("command_request", {}).get("id") if isinstance(self.workspace.read().get("command_request"), dict) else None

    def t(self, text, **values):
        return tr(text, self.language, **values)

    def open_command(self, action):
        """Only known UI actions can be dispatched; never evaluate command text."""
        actions = {"search": "action_search", "add_project": "add_project", "start_work": "action_prepare_work",
                   "connections": "login", "sessions": "sessions", "memory": "action_memory", "ask": "action_ask",
                   "brief": "action_brief", "next_stage": "action_prepare_next_stage",
                   "more": "action_more", "settings": "action_settings", "notices": "action_notices"}
        method = getattr(self, actions.get(action, ""), None)
        if callable(method):
            method()
            return True
        return False

    async def push_screen_wait(self, screen):
        return await wide_dialog(self, screen, getattr(self, "panel_role", "inspector"), super().push_screen_wait)

    async def request(self, method, path, *, params=None, json=None):
        return await asyncio.to_thread(self.broker.request, method, path, params=params, json=json)

    def notice(self, value) -> None:
        shown = None
        with suppress(Exception):
            shown = plain(self.t(value))
            self.query_one("#panel-notice", Static).update(shown)
        if shown is not None:
            with suppress(Exception):
                self.notice_history.add(shown.plain, self._notice_about or self.workspace_data)
                self.show_notice_mark()

    def notice_for(self, about, value) -> None:
        """A notice about a project of its own (see terminal_notices.about), not the one shown now.

        It goes through `notice`, so the line and its timing are those of every other notice.
        """
        self._notice_about = about
        try:
            self.notice(value)
        finally:
            self._notice_about = None

    def show_notice_mark(self) -> None:
        """A small mark in its own line, so the notice text itself is never changed."""
        with suppress(Exception):
            text = self.notice_history.mark(self.t)
            if text == self._mark_text:
                return  # nothing to repaint: most notices leave the mark as it was
            mark = self.query_one("#notice-mark", Static)
            mark.update(plain(text))
            mark.display = bool(text)
            self._mark_text = text

    async def workspace_changed(self, value: dict) -> None:
        """Subclass refreshes its projection when the local view changes."""

    async def _poll_workspace(self) -> None:
        try:
            language = await asyncio.to_thread(language_for, self.settings)
            if not self.is_running:
                return  # Shutdown began during the thread hop; widgets may already be gone.
            if language != self.language:
                self.language = language
                localize_bindings(self, language)
                for screen in self.screen_stack:
                    if isinstance(screen, LocalizedModal):
                        localize_bindings(screen, language)
                refresh = getattr(self, "refresh_language", None)
                if callable(refresh):
                    refresh()
            value = await asyncio.to_thread(self.workspace.read)
            if not self.is_running:
                return
            revision = value.get("revision")
            if revision != self._workspace_revision:
                old = self.workspace_data
                self._workspace_revision = revision
                self.workspace_data = value
                if (old.get("project_id"), old.get("worktree")) != (value.get("project_id"), value.get("worktree")):
                    self._scope_version += 1
                await self.workspace_changed(value)
            command = value.get("command_request")
            if isinstance(command, dict) and command.get("id") != self._last_command_id:
                self._last_command_id = command.get("id")
                if command.get("target_role") == getattr(self, "panel_role", None):
                    self.open_command(command.get("action"))
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(safe_text(exc))

    @work(group="panel-startup", exclusive=True, exit_on_error=False)
    async def start_panel(self):
        try:
            await asyncio.to_thread(self.broker.ensure)
            await self._poll_workspace()
            await self.ready_panel()
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(safe_text(exc))

    async def ready_panel(self):
        pass

    def on_unmount(self):
        with suppress(Exception):
            self.workers.cancel_all()

    def on_worker_state_changed(self, event):
        if event.state == WorkerState.ERROR:
            self.notice(event.worker.error)

    def header_footer(self):
        yield Header()
        yield Footer()


class ChoiceModal(LocalizedModal, ModalScreen[str | None]):
    CSS = """
    ChoiceModal { align: center middle; background: $background 70%; }
    ChoiceModal > Vertical { width: 90%; max-width: 110; height: 85%; padding: 1 2;
      background: #111a18; border: round #52684b; }
    ChoiceModal DataTable { height: 1fr; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, title, choices):
        super().__init__()
        self.heading, self.choices = title, choices

    def compose(self):
        from textual.widgets import DataTable
        with Vertical():
            yield Static(plain(self.t(self.heading)), classes="heading")
            yield DataTable(id="choices", cursor_type="row")
            yield Button(self.t("Cancel"), id="choice-cancel")

    def on_mount(self):
        from textual.widgets import DataTable
        table = self.query_one("#choices", DataTable)
        table.add_column(self.t("Open"))
        for index, (title, _) in enumerate(self.choices):
            table.add_row(plain(self.t(title)), key=str(index))
        table.focus()

    @on(Button.Pressed, "#choice-cancel")
    def cancel_button(self):
        self.dismiss(None)

    def action_cancel(self):
        self.dismiss(None)

    def on_data_table_row_selected(self, event):
        if event.data_table.id == "choices":
            self.dismiss(self.choices[int(event.row_key.value)][1])


class FolderPicker(LocalizedModal, ModalScreen[str | None]):
    """Keyboard-first directory navigation. No shell, recursion, or writes."""
    CSS = """
    FolderPicker { align: center middle; background: $background 70%; }
    FolderPicker > Vertical { width: 90%; height: 90%; border: round #a6d8ff; background: #101720; padding: 1; }
    FolderPicker DataTable { height: 1fr; }
    FolderPicker .actions { height: 3; }
    #folder-notice { height: auto; max-height: 3; }
    """
    BINDINGS = [("escape", "cancel", "Cancel"), ("alt+up", "parent", "Parent folder"), ("ctrl+s", "choose", "Use this folder")]

    def __init__(self, path=None):
        super().__init__()
        self.path = Path(path or Path.home())
        self.folder_children = []
        self._generation = 0

    def compose(self):
        with Vertical():
            yield Static(self.t("Browse folders"), classes="heading")
            yield Label(self.t("Folder path (Windows or WSL)"))
            yield Input(str(self.path), id="folder-path")
            yield DataTable(id="folder-rows", cursor_type="row")
            yield Static(self.t("Enter a folder path, then press Enter to browse."), id="folder-notice")
            with Horizontal(classes="actions"):
                yield Button(self.t("Use this folder"), id="folder-choose", variant="primary")
                yield Button(self.t("Parent folder"), id="folder-parent")
                yield Button(self.t("Cancel"), id="folder-cancel")

    def on_mount(self):
        self.query_one("#folder-rows", DataTable).add_column(self.t("Folder"))
        self.browse(str(self.path))

    @work(group="folder-read", exclusive=True, exit_on_error=False)
    async def browse(self, path):
        from .cockpit_state import normalize_path
        self._generation += 1
        generation = self._generation
        try:
            def read():
                target = normalize_path(path)
                if not target.is_dir():
                    raise ValueError(self.t("This folder does not exist or cannot be read."))
                # A single directory, bounded before sorting; no tree scan.
                children = []
                for count, entry in enumerate(target.iterdir()):
                    if entry.is_dir():
                        children.append(entry)
                    if len(children) >= 1000 or count >= 9999:
                        break
                return target, sorted(children, key=lambda p: p.name.casefold())
            target, children = await asyncio.to_thread(read)
            if generation != self._generation:
                return
            self.path, self.folder_children = target, children
            self.query_one("#folder-path", Input).value = str(target)
            table = self.query_one("#folder-rows", DataTable)
            table.clear()
            if target.parent != target:
                table.add_row("..", key="parent")
            for index, child in enumerate(children):
                table.add_row(plain(child.name), key=str(index))
            self.query_one("#folder-notice", Static).update(plain(str(target) if children else self.t("No subfolders. You can use this folder.")))
            table.focus()
        except (OSError, ValueError) as exc:
            self.query_one("#folder-notice", Static).update(plain(self.t(str(exc))))
            self.query_one("#folder-path", Input).focus()

    @on(Input.Submitted, "#folder-path")
    def path_submitted(self, event):
        self.browse(event.value)

    @on(DataTable.RowSelected, "#folder-rows")
    def folder_selected(self, event):
        key = str(event.row_key.value)
        self.browse(str(self.path.parent if key == "parent" else self.folder_children[int(key)]))

    @on(Button.Pressed)
    def folder_button(self, event):
        event.stop()
        {"folder-choose": self.action_choose, "folder-parent": self.action_parent,
         "folder-cancel": self.action_cancel}[event.button.id]()

    def action_parent(self):
        self.browse(str(self.path.parent))

    def action_choose(self):
        # Only a successfully browsed directory can be selected.
        if self.query_one("#folder-path", Input).value == str(self.path) and self.path.is_dir():
            self.dismiss(str(self.path))
        else:
            self.browse(self.query_one("#folder-path", Input).value)

    def action_cancel(self):
        self.dismiss(None)
