"""Quick finding form: Ctrl+B n opens it as a popup, Ctrl+S saves, Escape closes.

The typed text stays inside this process until it is sent as JSON to the local broker; it is never
placed on a command line, in a tmux command or in a shell. Nothing is read from any terminal pane.
"""
from __future__ import annotations

import asyncio

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Label, Select, Static, TextArea

from .findings import FINDING_KINDS, FINDING_STATUSES, finding_error
from .terminal_safe import plain, safe_text

ERROR_LIMIT = 300


def error_text(exc, translate) -> str:
    """One bounded line of literal text: a broker detail may quote anything, including control bytes."""
    detail = getattr(exc, "detail", None)
    detail = str(detail if detail is not None else exc)
    return translate("Could not save. Your entries were kept.") + " " + safe_text(
        " ".join(translate(detail).split()), limit=ERROR_LIMIT)


class FindingApp(App):
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { background: #101720; color: #edf2f7; padding: 1 2; }
    #finding-title { color: #a6d8ff; text-style: bold; height: auto; }
    #finding-scope { color: #b8cbe0; height: auto; }
    #finding-content { height: 1fr; min-height: 4; border: tall #526579; background: #152131; }
    #finding-content:focus { border: tall #a6d8ff; }
    #finding-choices { height: auto; }
    #finding-choices Select { width: 1fr; }
    #finding-error { color: #e7b594; height: auto; }
    Footer { background: #1b2938; }
    """
    BINDINGS = [("ctrl+s", "save", "Save"), ("escape", "cancel", "Cancel")]

    def __init__(self, settings, workspace_id="default", *, broker=None, workspace=None):
        super().__init__()
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

    def compose(self) -> ComposeResult:
        t = self.translate
        yield Static(plain(t("Save a finding")), id="finding-title")
        yield Static("", id="finding-scope", markup=False)
        yield Label(plain(t("Finding")))
        yield TextArea("", id="finding-content")
        with Horizontal(id="finding-choices"):
            yield Select([(kind, kind) for kind in FINDING_KINDS], value=FINDING_KINDS[0],
                         allow_blank=False, id="finding-kind")
            yield Select([(status, status) for status in FINDING_STATUSES], value=FINDING_STATUSES[0],
                         allow_blank=False, id="finding-status")
        yield Static("", id="finding-error", markup=False)
        yield Footer()

    def on_mount(self):
        # The finding goes to the project the person sees here, even if the workspace moves on meanwhile.
        try:
            selected = self.workspace.read()
        except (OSError, ValueError) as exc:
            selected = {}
            self.show_error(exc)
        self.scope = {"project": selected.get("project_id"), "worktree": selected.get("worktree")}
        name = selected.get("project_name") or selected.get("project_id")
        text = (self.translate("Project") + ": " + str(name) + " · " + str(selected.get("worktree") or "")
                if self.scope["project"] else self.translate("Open a project first."))
        self.query_one("#finding-scope", Static).update(plain(text, limit=ERROR_LIMIT))
        self.query_one("#finding-content", TextArea).focus()

    def show_error(self, exc):
        self.query_one("#finding-error", Static).update(plain(error_text(exc, self.translate)))

    def values(self):
        return {"content": self.query_one("#finding-content", TextArea).text,
                "kind": self.query_one("#finding-kind", Select).value,
                "status": self.query_one("#finding-status", Select).value}

    @work(group="finding-save", exclusive=True, exit_on_error=False)
    async def action_save(self):
        if self.saving:
            return
        values = self.values()
        problem = finding_error(values["content"], values["kind"], values["status"])
        if not self.scope.get("project"):
            problem = "Open a project first."
        if problem:
            self.query_one("#finding-error", Static).update(plain(self.translate(problem)))
            self.query_one("#finding-content", TextArea).focus()
            return
        self.saving = True
        self.query_one("#finding-error", Static).update(plain(self.translate("Saving…")))
        try:
            payload = {"project": self.scope["project"], **values}
            if self.scope.get("worktree"):
                payload["worktree"] = self.scope["worktree"]
            result = await asyncio.to_thread(self.broker.request, "POST", "/api/findings", json=payload)
        except (OSError, ValueError, RuntimeError) as exc:
            # The typed text and choices stay in their widgets; only a deliberate Ctrl+S retries.
            self.show_error(exc)
            return
        finally:
            self.saving = False
        record = result.get("record") if isinstance(result, dict) else None
        self.exit({"id": record.get("id") if isinstance(record, dict) else None,
                   "kind": values["kind"], "status": values["status"]})

    def action_cancel(self):
        # A save in flight settles first: closing must not suggest that a sent write was undone.
        if not self.saving:
            self.exit(None)


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
