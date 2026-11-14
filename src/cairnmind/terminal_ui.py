"""Shared literal-text controls for terminal-only workspace panels."""
from __future__ import annotations

import asyncio
import json
import os
import inspect
import threading
from pathlib import Path
from contextlib import suppress
from dataclasses import dataclass

from rich.cells import cell_len
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Static, TextArea
from textual.worker import WorkerState

from . import terminal_theme
from .terminal_safe import plain, safe_text
from .terminal_i18n import language_for, localize_bindings, tr
from .terminal_i18n_messages import broker_error_text


def localize_select_menus(node, language):
    """The open list of a Select has a Footer key of its own ("Dismiss menu"); translate it too."""
    from textual.widgets._select import SelectOverlay
    with suppress(Exception):
        for overlay in node.query(SelectOverlay):
            localize_bindings(overlay, language)


def remeasure_buttons(node) -> None:
    """After a language change: a button as wide as its label is measured again. Textual 8 keeps the
    width of the old label, so a longer new one is cut ("Otwó" for "Otwórz") until the next resize."""
    for button in node.query(Button):
        button.clear_cached_dimensions()
        button.refresh(layout=True)


def relabel_select(select, options, value) -> None:
    """New option labels for a Select, keeping its value and announcing no change.

    set_options alone picks the first option, and Textual 8 redraws the shown label only when the
    value changes, so the old label would stay on screen.
    """
    from textual.widgets import Select
    from textual.widgets._select import SelectCurrent
    with select.prevent(Select.Changed):
        select.set_options(options)
        select.value = value
    with suppress(Exception):
        select.query_one(SelectCurrent).update(dict((v, label) for label, v in options)[value])


class LocalizedModal:
    BODY_CHROME = 10  # rows of a .cm-dialog that are not its scrolling body (see terminal_theme.fit_body)

    def t(self, text, **values):
        return tr(text, getattr(self.app, "language", "en"), **values)

    def on_mount(self):
        localize_bindings(self, getattr(self.app, "language", "en"))
        self.fit_dialog()

    def on_resize(self):
        self.fit_dialog()

    def fit_dialog(self):
        """Size follows content: the body scrolls only when the terminal has no more rows to give."""
        with suppress(Exception):
            for body in self.query(".cm-dialog-body"):
                terminal_theme.fit_body(self, body, self.BODY_CHROME)


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
    BODY_CHROME = 11  # the error line may take a row of its own
    BINDINGS = [("escape", "cancel", "Cancel"), ("ctrl+s", "submit", "Save")]

    def __init__(self, title: str, fields: list[Field], *, note: str = "", submit="Save", on_submit=None):
        super().__init__()
        self.title_text, self.fields, self.note, self.submit_label = title, fields, note, submit
        self.on_submit = on_submit
        self.saving = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="cm-dialog"):
            yield Static(plain(self.t(self.title_text)), classes="form-title")
            with VerticalScroll(id="form-fields", classes="cm-dialog-body"):
                if self.note:
                    # Never cut or capped: a long note scrolls with the fields.
                    yield Static(plain(self.t(self.note)), classes="cm-note")
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
            # Ctrl+S runs the form's own primary action, so the hint names that action.
            yield Static(plain(self.t("Ctrl+S: {action} · Esc: cancel", action=self.t(self.submit_label))),
                         classes="cm-hint")

    def on_mount(self):
        if self.fields:
            # At once, not queued: a key typed as the form opens must reach the first field.
            self.set_focus(self.query_one("#field-" + self.fields[0].name))
            # Focus scrolls the first field into view; a long note is still read from its first line.
            fields = self.query_one("#form-fields", VerticalScroll)
            self.call_after_refresh(lambda: fields.scroll_home(animate=False))

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
            self.query_one("#form-error", Static).update(plain(self.t(broker_error_text(exc, self.t))))
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
    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, title: str, value):
        super().__init__()
        self.heading = title
        self.value = value
        self.details = False

    SCROLL_HINT, CLOSE_HINT = "PgUp/PgDn: scroll · Esc: close", "Esc: close"
    SHORT_CELLS = 60  # a summary whose lines are all shorter gets the narrow menu frame

    def short_summary(self):
        text = summary_text(self.value, getattr(self.app, "language", "en"))
        return max((cell_len(line) for line in text.split("\n")), default=0) < self.SHORT_CELLS

    def compose(self) -> ComposeResult:
        with Vertical(classes="cm-dialog " + ("-menu" if self.short_summary() else "-wide")):
            yield Static(plain(self.t(self.heading)), classes="heading")
            # The body takes focus first (PgUp/PgDn scroll it); its bar shows that it has the focus.
            with VerticalScroll(classes="cm-dialog-body -message"):
                yield Static(plain(summary_text(self.value, getattr(self.app, "language", "en"))), id="message-body")
            with Horizontal(classes="actions"):
                if not isinstance(self.value, str):
                    yield Button(self.t("Technical details"), id="message-details")
                yield Button(self.t("Close"), id="message-close")
            yield Static(self.t(self.SCROLL_HINT), classes="cm-hint", markup=False)

    def fit_dialog(self):
        super().fit_dialog()
        self.call_after_refresh(self.fit_hint)

    def fit_hint(self):
        """PgUp/PgDn are named only while the body has more than it shows."""
        with suppress(Exception):
            body, hint = self.query_one(".cm-dialog-body"), self.query_one(".cm-hint", Static)
            scrolls = body.virtual_size.height > body.scrollable_content_region.height
            hint.update(self.t(self.SCROLL_HINT if scrolls else self.CLOSE_HINT))

    @on(Button.Pressed, "#message-details")
    def toggle_details(self, event):
        event.stop()
        self.details = not self.details
        self.query_one("#message-body", Static).update(plain(json.dumps(self.value, ensure_ascii=False, indent=2)
            if self.details else summary_text(self.value, getattr(self.app, "language", "en"))))
        event.button.label = self.t("Hide technical details" if self.details else "Technical details")
        with suppress(Exception):  # the indented JSON gets the wide frame; the summary its own size
            wide = self.details or not self.short_summary()
            dialog = self.query_one(".cm-dialog")
            dialog.set_class(wide, "-wide")
            dialog.set_class(not wide, "-menu")
        self.call_after_refresh(self.fit_hint)

    @on(Button.Pressed, "#message-close")
    def close_button(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class BasePanel(App):
    ENABLE_COMMAND_PALETTE = False
    run_start_request = False  # set by `--run-request`: the request found at start is for this start
    # Colours and the shape of every control and dialog live in terminal_theme; panels add sizes only.
    CSS = terminal_theme.BASE_CSS

    def __init__(self, settings, workspace_id="default", *, broker=None, workspace=None):
        super().__init__()
        terminal_theme.install(self)
        from .terminal_client import BrokerClient
        from .workspace_state import WorkspaceState
        self.settings = settings
        self.workspace_id = workspace_id
        self.broker = broker if broker is not None else BrokerClient(settings, ensure=False)
        self.workspace = workspace if workspace is not None else WorkspaceState(settings, workspace_id)
        self.workspace_data = {}
        from .terminal_notices import NoticeHistory
        self.notice_history = NoticeHistory()  # memory only, for the life of this panel (#130)
        self._feed_lock, self._feed_rows, self._feed_sent = threading.Lock(), None, None
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

    def notice(self, value, kind="info") -> None:
        """Show one line. `kind` ("info", "warn", "error") only picks a style; the text is exactly `value`."""
        shown = None
        if isinstance(value, BaseException):
            value = broker_error_text(value, self.t)  # "Broker 503: …" with a fixed detail translated
        with suppress(Exception):
            shown = plain(self.t(value))
            line = self.query_one("#panel-notice", Static)
            line.update(shown)
            line.display = bool(shown.plain)  # an empty notice line takes no row from the list
            line.set_class(kind == "warn", "-warn")
            line.set_class(kind == "error", "-error")
        if shown is not None:
            with suppress(Exception):
                self.notice_history.add(shown.plain, self._notice_about or self.workspace_data, kind)
                self.show_notice_mark()
            self.publish_notices(later=True)

    def publish_notices(self, *, later=False) -> None:
        """Copy the newest notices to this panel's tmux pane for the activity panel (#144).

        `later` copies once per burst of notices, half a second after the first. The copy runs in a
        thread, never on the event loop, and a failed copy is ignored: a notice never fails because of it.
        """
        if getattr(self, "panel_role", None) not in {"sidebar", "inspector", "graph"}:
            return
        if not later:
            self._publish_now()
        elif getattr(self, "_feed_timer", None) is None:
            self._feed_timer = self.set_timer(.5, self._publish_now)

    def _publish_now(self) -> None:
        self._feed_timer = None
        with suppress(Exception):
            from .terminal_notice_feed import snapshot
            self._feed_rows = snapshot(self.notice_history)  # read here: the history is not shared with a thread
            self.run_worker(self._send_notices, thread=True, group="notice-feed", exit_on_error=False)

    def _send_notices(self) -> None:
        from .terminal_notice_feed import publish
        with self._feed_lock:  # one copy at a time, and always the newest snapshot
            rows = self._feed_rows
            if rows is not self._feed_sent:
                with suppress(Exception):
                    publish(self.settings, self.workspace_id, self.panel_role, rows)
                self._feed_sent = rows

    def notice_for(self, about, value, kind="info") -> None:
        """A notice about a project of its own (see terminal_notices.about), not the one shown now.

        It goes through `notice`, so the line and its timing are those of every other notice.
        """
        self._notice_about = about
        try:
            # One argument unless a kind is asked for: callers and tests may wrap `notice(value)`.
            self.notice(value) if kind == "info" else self.notice(value, kind)
        finally:
            self._notice_about = None

    def notice_elsewhere(self, about, value, kind="info") -> None:
        """A notice about a project that is not shown now: kept in Notices under that project, with the
        mark, and never put on the notice line over the shown project's own text."""
        with suppress(Exception):
            self.notice_history.add(plain(self.t(value)).plain, about, kind, on_line=False)
            self.show_notice_mark()
        self.publish_notices(later=True)

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
                localize_select_menus(self, language)
                for screen in self.screen_stack:
                    if isinstance(screen, LocalizedModal):
                        localize_bindings(screen, language)
                refresh = getattr(self, "refresh_language", None)
                if callable(refresh):
                    refresh()
                remeasure_buttons(self)
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
                    if isinstance(self.screen, ModalScreen):
                        # Run behind an open dialog, a command would change the view that dialog belongs to.
                        self.notify(self.t(self.DIALOG_OPEN), timeout=8, markup=False)
                    else:
                        self.open_command(command.get("action"))
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)

    @work(group="panel-startup", exclusive=True, exit_on_error=False)
    async def start_panel(self):
        # A copy left on the pane by an earlier run of this panel is replaced: it lives as long as the panel.
        self.publish_notices()
        try:
            await asyncio.to_thread(self.broker.ensure)
            await self._poll_workspace()
            await self.ready_panel()
            if self.run_start_request:
                # Started again by the menu request in the state (WorkspaceManager._revive): run it once.
                self.run_start_request, self._last_command_id = False, None
                await self._poll_workspace()
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)

    async def ready_panel(self):
        pass

    def on_mount(self):
        terminal_theme.size_classes(self)
        with suppress(Exception):
            line = self.query_one("#panel-notice", Static)
            if not str(line.render()):
                line.display = False
        localize_select_menus(self, self.language)

    def on_resize(self, event):
        terminal_theme.size_classes(self, event.size)

    def on_unmount(self):
        with suppress(Exception):
            self.workers.cancel_all()

    def on_worker_state_changed(self, event):
        if event.state == WorkerState.ERROR:
            self.notice(event.worker.error)

    def header_footer(self):
        yield Header()
        yield Footer()

    # Textual binds Ctrl+Q to quit and Ctrl+C to "press Ctrl+Q to quit". A workspace view must not end
    # from a key: tmux keeps its pane (remain-on-exit) and shows it dead. In the workspace tmux takes
    # Ctrl+Q first and detaches the terminal (WorkspaceManager._bindings); these run where it does not.
    LEAVE_KEYS = "Ctrl+B, then d: leave the workspace. It keeps running; run cairnmind to return."
    LEAVE_HINT = "To leave the workspace press Ctrl+Q, or Ctrl+B then d. It keeps running; run cairnmind to return."
    DIALOG_OPEN = "A dialog is open in this view. Finish it or press Esc, then choose the command again."

    async def action_quit(self) -> None:
        self.leave_hint(self.LEAVE_KEYS)

    def action_help_quit(self) -> None:
        self.leave_hint(self.LEAVE_HINT)

    def leave_hint(self, text) -> None:
        """A key hint, not an event of the project: shown on the notice line, never kept in Notices."""
        if lines := self.query("#panel-notice"):
            line = lines.first(Static)
            line.update(plain(self.t(text)))
            line.display = True
            line.remove_class("-warn", "-error")
            self.notice_history.add("", None)  # the newest notice left the line: the mark counts it again
            self.show_notice_mark()
        else:  # a view without a notice line (the activity column)
            self.notify(self.t(text), timeout=8, markup=False)


class ChoiceModal(LocalizedModal, ModalScreen[str | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]
    MENU_CELLS = 60  # a list whose longest row is shorter gets the narrow menu frame

    def __init__(self, title, choices):
        super().__init__()
        self.heading, self.choices = title, choices
        self._longest = 0

    def compose(self):
        with Vertical(classes="cm-dialog"):
            yield Static(plain(self.t(self.heading)), classes="heading")
            # No header row: a row's index is the choice's index. Rows stay one line each, uncut.
            yield DataTable(id="choices", cursor_type="row", show_header=False, classes="cm-dialog-body")
            with Horizontal(classes="actions"):
                yield Button(self.t("Cancel"), id="choice-cancel")
            yield Static(self.t("Enter: select · Esc: back"), classes="cm-hint", markup=False)

    def on_mount(self):
        table = self.query_one("#choices", DataTable)
        rows = [plain(self.t(title)) for title, _ in self.choices]
        self._longest = max([row.cell_len for row in rows], default=0)
        self.query_one(".cm-dialog").set_class(self._longest < self.MENU_CELLS, "-menu")
        table.add_column(self.t("Open"), key="choice")
        for index, row in enumerate(rows):
            table.add_row(row, key=str(index))
        table.focus()
        self.call_after_refresh(self.fill_width)

    def on_resize(self):
        self.call_after_refresh(self.fill_width)

    def fill_width(self):
        """The cursor row spans the dialog; a row longer than the dialog keeps its full text and scrolls."""
        with suppress(Exception):
            terminal_theme.fill_column(self.query_one("#choices", DataTable), "choice", self._longest)

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
    BODY_CHROME = 14  # frame, padding, title, label, path, notice (up to 3 rows), actions and blank rows
    BINDINGS = [("escape", "cancel", "Cancel"), ("alt+up", "parent", "Parent folder"), ("ctrl+s", "choose", "Use this folder")]

    def __init__(self, path=None):
        super().__init__()
        self.path = Path(path or Path.home())
        self.folder_children = []
        self._generation = 0

    def compose(self):
        with Vertical(classes="cm-dialog -wide"):
            yield Static(self.t("Browse folders"), classes="heading")
            yield Label(self.t("Folder path (Windows or WSL)"), classes="form-label")
            yield Input(str(self.path), id="folder-path")
            yield DataTable(id="folder-rows", cursor_type="row", classes="cm-dialog-body")
            yield Static(self.t("Enter a folder path, then press Enter to browse."), id="folder-notice", classes="muted")
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
            self.query_one("#folder-notice", Static).update(plain(self.t(broker_error_text(exc, self.t))))
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
