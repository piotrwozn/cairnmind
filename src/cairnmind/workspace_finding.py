"""Quick finding form: Ctrl+B n opens it as a popup, Ctrl+S saves, Escape closes.

The typed text stays inside this process until it is sent as JSON to the local broker; it is never
placed on a command line, in a tmux command or in a shell. Nothing is read from any terminal pane.
"""
from __future__ import annotations

import asyncio
from uuid import uuid4

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Label, Select, Static, TextArea

from . import terminal_theme
from .findings import FINDING_KINDS, FINDING_STATUSES, finding_error
from .terminal_client import BrokerError
from .terminal_safe import plain, safe_text

ERROR_LIMIT = 300
# Messages the broker client and the broker answer this form with are Polish in the code. They are
# translated here, for this form only, so that other screens showing them keep their behaviour.
BROKER_MESSAGES = {
    "Lokalny broker jest niedostępny lub wymaga ponownego uruchomienia.":
        "The local broker is unavailable or needs a restart.",
    "Niepoprawny adres lokalnego brokera.": "The local broker address is invalid.",
    "Niepoprawna odpowiedź lokalnego brokera.": "The local broker gave an invalid answer.",
    "Żądanie zostało odrzucone.": "The request was refused.",
    "Nie można uwierzytelnić lokalnej sesji.": "The local session could not be authenticated.",
    "Niepoprawny bilet lokalnej sesji.": "The local session ticket is invalid.",
    "Niepoprawna lokalna sesja.": "The local session is invalid.",
    "Połączenie z lokalnym brokerem zostało przerwane.":
        "The connection to the local broker was interrupted.",
    "Sesja kokpitu wygasła. Uruchom cairnmind.": "The broker session expired. Run cairnmind.",
    "Niepoprawny Host.": "Invalid Host.", "Niepoprawny Origin.": "Invalid Origin.",
    "Niepoprawny token CSRF.": "Invalid CSRF token.",
    "Żądanie przekracza limit.": "The request is too large.",
    "Niepoprawny JSON.": "Invalid JSON.", "Oczekiwano obiektu JSON.": "A JSON object was expected.",
    "Niepoprawne pola żądania.": "Invalid request fields.",
    "Usługa pamięci jest niedostępna. Uruchom ponownie cairnmind.":
        "The memory service is unavailable. Restart cairnmind.",
    "Usługa pamięci zwróciła niepoprawną odpowiedź.": "The memory service gave an invalid answer.",
}


def outcome_unknown(exc, sent) -> bool:
    """Only a request that was sent can have been committed: a 5xx or a transport error, which the
    client reports as a 503, may come after the service wrote. A 4xx was refused before writing, and
    any other exception from the client is local and happens before or instead of sending."""
    status = getattr(exc, "status_code", None)
    return bool(sent) and isinstance(exc, BrokerError) and isinstance(status, int) and status >= 500


def detail_text(exc, translate, language) -> str:
    detail = getattr(exc, "detail", None)
    detail = " ".join(str(detail if detail is not None else exc).split())
    if detail in BROKER_MESSAGES:
        return detail if language == "pl" else BROKER_MESSAGES[detail]
    if detail.startswith("Unknown project: "):
        return translate("This project is no longer registered.")
    return translate(detail)


def error_text(exc, translate, language="en", *, sent=False, lead=None) -> str:
    """One bounded line of literal text: a broker detail may quote anything, including control bytes."""
    lead = lead or ("No clear answer from the memory service. The finding may have been saved; saving again "
                    "from this form is safe and will not store it twice." if outcome_unknown(exc, sent)
                    else "Could not save. Your entries were kept.")
    return translate(lead) + " " + safe_text(detail_text(exc, translate, language), limit=ERROR_LIMIT)


class FindingApp(App):
    ENABLE_COMMAND_PALETTE = False
    CSS = terminal_theme.BASE_CSS + terminal_theme.css("""
    Screen { padding: 0 1; }
    #finding-scope { color: $cm-muted; height: auto; padding: 0 1; margin-bottom: 1; }
    #finding-title, #finding-label { padding: 0 1; }
    #finding-content { height: 1fr; min-height: 4; }
    #finding-choices { height: 1; }
    #finding-choices Select { width: 1fr; margin-right: 1; }
    #finding-error { color: $cm-muted; height: auto; padding: 0 1; }
    #finding-error.-error { color: $cm-danger; text-style: bold; }
    .-short #finding-scope { margin-bottom: 0; }
    """)
    BINDINGS = [("ctrl+s", "save", "Save"), ("escape", "cancel", "Cancel")]

    def __init__(self, settings, workspace_id="default", *, broker=None, workspace=None):
        super().__init__()
        terminal_theme.install(self)
        from .terminal_client import BrokerClient
        from .terminal_i18n import language_for, localize_bindings, tr
        from .workspace_state import WorkspaceState
        self.settings = settings
        self.broker = broker if broker is not None else BrokerClient(settings, ensure=False)
        self.workspace = workspace if workspace is not None else WorkspaceState(settings, workspace_id)
        self.language = language_for(settings)
        self.translate = lambda value, **values: tr(value, self.language, **values)
        localize_bindings(self, self.language)
        self.scope = {}
        self.saving = False
        # One key for every save of this form: a retry after an unclear answer returns the first write.
        self.key = str(uuid4())

    def compose(self) -> ComposeResult:
        t = self.translate
        yield Static(plain(t("Save a finding")), id="finding-title", classes="heading")
        yield Static("", id="finding-scope", markup=False)
        yield Label(plain(t("Finding")), id="finding-label", classes="form-label")
        yield TextArea("", id="finding-content")
        with Horizontal(id="finding-choices"):
            yield Select([(kind, kind) for kind in FINDING_KINDS], value=FINDING_KINDS[0],
                         allow_blank=False, id="finding-kind")
            yield Select([(status, status) for status in FINDING_STATUSES], value=FINDING_STATUSES[0],
                         allow_blank=False, id="finding-status")
        yield Static("", id="finding-error", markup=False)
        yield Footer()

    def on_resize(self, event):
        terminal_theme.size_classes(self, event.size)

    def on_mount(self):
        terminal_theme.size_classes(self)
        # The finding goes to the project the person sees here, even if the workspace moves on meanwhile.
        try:
            selected = self.workspace.read()
        except (OSError, ValueError) as exc:
            # Nothing was sent, so this must never read like an unclear save.
            selected = {}
            self.show_error(exc, lead="Could not read the workspace state.")
        self.scope = {"project": selected.get("project_id"), "worktree": selected.get("worktree")}
        name = selected.get("project_name") or selected.get("project_id")
        text = (self.translate("Project") + ": " + str(name) + " · " + str(selected.get("worktree") or "")
                if self.scope["project"] else self.translate("Open a project first."))
        self.query_one("#finding-scope", Static).update(plain(text, limit=ERROR_LIMIT))
        self.query_one("#finding-content", TextArea).focus()

    def show_error(self, exc, *, sent=False, lead=None):
        text = error_text(exc, self.translate, self.language, sent=sent, lead=lead)
        self.show_line(text, error=True)

    def show_line(self, text, *, error=False):
        """The line under the form. Only a refusal or a failure is styled as one; "Saving…" is not."""
        line = self.query_one("#finding-error", Static)
        line.update(plain(text))
        line.set_class(error, "-error")

    def values(self):
        return {"content": self.query_one("#finding-content", TextArea).text,
                "kind": self.query_one("#finding-kind", Select).value,
                "status": self.query_one("#finding-status", Select).value}

    def action_save(self):
        # Synchronous up to the guard: no await separates the check from setting it, and a running
        # save is never cancelled by a second press, so one form sends at most one request at a time.
        if self.saving:
            return
        values = self.values()
        problem = finding_error(values["content"], values["kind"], values["status"])
        if not self.scope.get("project"):
            problem = "Open a project first."
        if problem:
            self.show_line(self.translate(problem), error=True)
            self.query_one("#finding-content", TextArea).focus()
            return
        self.saving = True
        self.show_line(self.translate("Saving…"))
        self.send(values)

    @work(group="finding-save", exit_on_error=False)
    async def send(self, values):
        sent = False
        try:
            payload = {"project": self.scope["project"], "key": self.key, **values}
            if self.scope.get("worktree"):
                payload["worktree"] = self.scope["worktree"]
            # Finding and authenticating the broker first separates "never sent" from "sent, no answer".
            await asyncio.to_thread(self.broker.connect)
            sent = True
            result = await asyncio.to_thread(self.broker.request, "POST", "/api/findings", json=payload)
        except Exception as exc:
            # Every failure replaces "Saving…" with its reason. The typed text and choices stay in
            # their widgets; only a deliberate Ctrl+S retries.
            status = getattr(exc, "status_code", None)
            refused = isinstance(exc, BrokerError) and isinstance(status, int) and status < 500
            if not refused:
                # A broker that restarted elsewhere, or a half-made connection, is found and
                # authenticated again on the next Ctrl+S; the form's key keeps that retry to one write.
                try:
                    await asyncio.to_thread(self.broker.close)
                except Exception:
                    pass
            self.show_error(exc, sent=sent)
            return
        finally:
            self.saving = False
        record = result.get("record") if isinstance(result, dict) else None
        record = record if isinstance(record, dict) else {}
        # After a retry the stored record is the earlier write: confirm what it holds.
        self.exit({"id": record.get("id"), "kind": record.get("kind", payload["kind"]),
                   "status": record.get("status", payload["status"]),
                   "already_saved": bool(isinstance(result, dict) and result.get("already_saved"))})

    def action_cancel(self):
        # A save in flight settles first: closing must not suggest that a sent write was undone.
        if not self.saving:
            self.exit(None)

    async def action_quit(self) -> None:
        # Textual's own Ctrl+Q closes like Escape, never past a save in flight.
        self.action_cancel()


def confirmation(result, language="en") -> str:
    """The one-line status message, as tmux shows it; only allowlisted identifiers enter it."""
    from .terminal_i18n import tr
    from .workspace import _status_text
    kind = result["kind"] if result.get("kind") in FINDING_KINDS else "?"
    status = result["status"] if result.get("status") in FINDING_STATUSES else "?"
    message = tr("Finding saved to memory: {kind}, {status}.", language, kind=kind, status=status)
    return _status_text(message, 120)


def run_finding_form(manager, client_tty=None):
    """Run the form in its popup, then confirm on the terminal that opened it."""
    from .terminal_i18n import language_for
    result = FindingApp(manager.settings, manager.workspace_id).run()
    if result and client_tty:
        manager.tmux("display-message", "-c", client_tty, "-d", "5000",
                     confirmation(result, language_for(manager.settings)), check=False)
    return result
