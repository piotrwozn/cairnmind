"""Modal dialogs and profile form builders for the Cairnmind terminal sidebar."""
from __future__ import annotations

import asyncio
from contextlib import suppress

from textual import _time as textual_time  # the clock of Message.time, which stamps every key and mouse event
from textual import on, work
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import QueryError
from textual.errors import NoWidget
from textual.events import Click, MouseDown
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Static, Switch

from cairnmind.pal.terminal_safe import plain
from cairnmind.terminal import terminal_theme
from cairnmind.terminal.terminal_i18n_messages import broker_error_text
from cairnmind.terminal.terminal_ui import (
    Field,
    LocalizedModal,
    MessageModal,
    summary_text,
)


class ConfirmModal(MessageModal):
    QUIET = 0.4  # seconds after the first refresh in which a consent dialog takes only Esc and Enter (#292, #318)
    SCROLL_HINT, CLOSE_HINT = "PgUp/PgDn: scroll · Esc: cancel", "Esc: cancel"

    def __init__(self, title, value, *, approve="Confirm", consent=False):
        super().__init__(title, value)
        self.approve = approve
        self.consent = consent
        self.drawn_at = float("inf") if consent else 0  # set before any input can reach a dialog that is not mounted yet
        self.SCROLL_HINT, self.CLOSE_HINT = (
            ("PgUp/PgDn: scroll · Tab: switch · Esc: cancel", "Tab: switch · Esc: cancel")
            if consent
            else (self.SCROLL_HINT, self.CLOSE_HINT)
        )

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
            yield Static(self.t(self.SCROLL_HINT), classes="cm-hint", markup=False)

    def fit_dialog(self):
        super().fit_dialog()
        self.call_after_refresh(self.spare_hint_row)

    def spare_hint_row(self):
        """On a short terminal the key-hint row goes to the request when that one row is what keeps
        the whole request and its note on screen at once. Nothing of the request is ever cut."""
        self.call_after_refresh(self.start_on_cancel)  # after this row's relayout, so the fit check sees the final body
        try:
            body, hint = self.query_one(".cm-dialog-body"), self.query_one(".cm-hint")
        except (QueryError, AttributeError, LookupError, RuntimeError):
            return
        rows = self.app.size.height - (self.BODY_CHROME - 5)
        spare = (
            terminal_theme.is_short(self.app)
            and self.app.size.width >= terminal_theme.NARROW_WIDTH
            and body.virtual_size.height == rows + 1
        )
        hint.display = not spare
        if spare:
            body.styles.max_height = rows + 1

    def on_mount(self):
        # At once, before any queued key is handled (#292). drawn_at, the first refresh (start_on_cancel), is on the clock
        # Textual stamps every input event with when it reads it (#318).
        if self.consent:
            self.set_focus(self.query_one("#confirm-no", Button))

    def start_on_cancel(self):
        """A confirmation whose text fits starts on Cancel, so the focus shows and Enter cancels, even for consent.
        A text that scrolls starts on the text, where Enter does nothing; its hint names Tab, which reaches the buttons."""
        first = self.drawn_at == float("inf")  # only the first call may leave Cancel for the text, not a resize
        if first:
            self.drawn_at = textual_time.get_time()
        with suppress(QueryError, AttributeError, LookupError, RuntimeError, TypeError):
            body, cancel = self.query_one(".cm-dialog-body"), self.query_one("#confirm-no", Button)
            fits = body.virtual_size.height <= body.scrollable_content_region.height
            if self.focused in (body, cancel) and (fits or first):
                self.set_focus(cancel if fits else body)

    def admits(self, event):
        """Whether a key or a mouse press may act on this dialog (#318). Only input produced after the dialog was
        first drawn counts: Textual stamps each event when it reads it (Message.time), so an early key keeps its
        early time however late it is handled. Esc always refuses. In the quiet moment only Esc and Enter act,
        and Enter then reaches only Cancel or the text, because no focus move or click made in it counts."""
        key = getattr(event, "key", None)
        if not self.consent or key == "escape":
            return True
        return event.time >= self.drawn_at + self.QUIET or (key == "enter" and event.time >= self.drawn_at)

    def presses_cancel(self, event):
        # Cancel only refuses, so a press on it made after the dialog was drawn needs no quiet moment (#318).
        with suppress(NoWidget, AttributeError, IndexError, LookupError, RuntimeError, TypeError):
            return (
                event.time >= self.drawn_at
                and self.get_widget_at(event.screen_x, event.screen_y)[0].id == "confirm-no"
            )
        return False

    def on_key(self, event):
        # A key typed while this dialog is on top passes here before any binding; drop_unseen_key in the App
        # also catches a key queued on the screen below, which never passes here.
        if not self.admits(event):
            event.prevent_default()
            event.stop()

    def _forward_event(self, event):
        # Textual hands every mouse event to the top screen here before any widget sees it: a press the dialog
        # does not admit neither focuses nor clicks, and the click of its release is cancelled.
        if isinstance(event, (MouseDown, Click)) and not self.admits(event) and not self.presses_cancel(event):
            self.suppress_click()
            return
        super()._forward_event(event)

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
                yield Static(
                    self.t("The key stays outside project memory. By default it lasts only for this engine session."),
                    markup=False,
                )
                yield Label(self.t("New key (saved keys are never displayed)"), classes="form-label")
                yield Input(password=True, id="credential-key")
                with Horizontal(id="credential-remember"):
                    yield Switch(False, id="credential-persist")
                    yield Label(self.t("Remember on this computer"))
                yield Static(
                    self.t("Remembering creates a private, unencrypted file outside the project. It is not included in snapshots."),
                    markup=False,
                    classes="cm-note",
                )
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
        return fields + [
            Field("client", "Client: codex / claude", client, True),
            Field("model", "Model (blank = provider default)", old.get("model") or ""),
        ]
    fields.append(
        Field("model", "Model" if client == "api" else "Model (blank = provider default)", old.get("model") or "", client == "api")
    )
    if client == "api":
        defaults = {
            "openai": "https://api.openai.com/v1",
            "anthropic": "https://api.anthropic.com/v1",
            "openai_compatible": "http://127.0.0.1:8000/v1",
            "ollama": "http://127.0.0.1:11434",
        }
        default_url = defaults[provider] if isinstance(provider, str) else ""
        fields.append(Field("base_url", "API address", old.get("base_url") or default_url, True))
    fields.append(
        Field("credential_ref", "Credential reference: env:NAME or profile:ID (blank = no key)", old.get("credential_ref") or "")
    )
    limits = old.get("limits") or {}
    if client == "api":
        fields += [
            Field("max_model_calls", "Maximum model calls per turn", str(limits.get("max_model_calls", 20)), True),
            Field("max_output_tokens", "Maximum output tokens", str(limits.get("max_output_tokens", 4096)), True),
        ]
        if provider == "ollama":
            fields.append(
                Field("context_window_tokens", "Ollama context tokens (uses RAM/VRAM)", str(limits.get("context_window_tokens", 32768)), True)
            )
    else:
        fields += [
            Field("max_tool_calls", "Maximum tool calls per turn", str(limits.get("max_tool_calls", 20)), True),
            Field("max_turn_seconds", "Maximum turn duration (seconds)", str(limits.get("max_turn_seconds", 900)), True),
        ]
    fields.append(Field("command_timeout_seconds", "Command timeout (seconds)", str(limits.get("command_timeout_seconds", 120)), True))
    if client == "api" and provider != "ollama":
        pricing = old.get("pricing") or {}
        fields += [
            Field("input_per_million", "Input USD per million tokens (optional)", str(pricing.get("input_per_million", ""))),
            Field("output_per_million", "Output USD per million tokens (optional)", str(pricing.get("output_per_million", ""))),
        ]
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
    keys = (
        ("max_model_calls", "max_output_tokens", "command_timeout_seconds")
        if client == "api"
        else ("max_tool_calls", "max_turn_seconds", "command_timeout_seconds")
    )
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
                table.add_row(
                    plain(row.get("name", self.t("Project"))),
                    plain(str(row.get("root", row.get("path", ""))) + (" · snapshot" if row.get("memory_snapshot") else "")),
                    key=str(index),
                )
            if rows:
                table.move_cursor(row=min(coordinate.row, len(rows) - 1))
            self.query_one("#discovery-notice", Static).update(
                plain(
                    f"{value.get('state', 'unknown')} · {len(rows)} "
                    + self.t("Projects")
                    + ("\n" + self.t("Operation needs attention.") if value.get("errors") else "")
                )
            )
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
