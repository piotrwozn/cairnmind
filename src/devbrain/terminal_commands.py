"""A searchable, prefix-only command surface shared by every workspace pane."""
from __future__ import annotations

from textual import on
from textual.app import App, ComposeResult
from textual.widgets import Button, DataTable, Footer, Input, Static

from .cockpit_state import CockpitState
from .terminal_safe import plain


COMMANDS = (
    ("Projects", "Choose or open a project", "command", "search"),
    ("Workspace", "Return to your assistant", "focus", "client1"),
    ("Second assistant", "Show the second visible assistant", "focus", "client2"),
    ("Memory", "Facts, tasks, plans and sources", "focus", "inspector"),
    ("Ask memory", "Question answered from project memory with sources", "command", "ask"),
    ("Graph", "Explore relationships in memory", "focus", "graph"),
    ("Search projects", "Find a registered project", "command", "search"),
    ("Add folder", "Register a project folder", "command", "add_project"),
    ("Start work", "Review your assistant before starting", "command", "start_work"),
    ("Connections", "Accounts, assistants and models", "command", "connections"),
    ("Sessions", "Return to or manage an existing session", "command", "sessions"),
    ("More", "Snapshots, import and advanced tools", "command", "more"),
    ("Settings", "Language, layout and reduced motion", "command", "settings"),
    ("Focus view", "One view at a time", "view", "focus"),
    ("Cockpit view", "Multiple panes on a large terminal", "view", "cockpit"),
    ("Keyboard help", "Navigation and native client shortcuts", "help", ""),
)


class CommandPaletteApp(App):
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { background: #101720; color: #edf2f7; padding: 1 2; }
    #commands { height: 1fr; }
    #command-search { margin: 1 0; }
    #command-help { height: 1fr; overflow-y: auto; }
    Footer { background: #1b2938; }
    """
    BINDINGS = [("escape", "cancel", "Back"), ("ctrl+n,down", "next", "Next"),
                ("ctrl+p,up", "previous", "Previous")]

    def __init__(self, manager, *, help_only=False):
        super().__init__()
        self.manager = manager
        self.help_only = help_only
        self.rows = []
        from .terminal_i18n import language_for, tr
        self.language = language_for(manager.settings)
        self.translate = lambda value: tr(value, self.language)

    def compose(self) -> ComposeResult:
        yield Static(plain(self.translate("Keyboard help" if self.help_only else "Commands")))
        if self.help_only:
            yield Static(plain(self.translate(
                "Ctrl+B, then Space: search all commands\n"
                "Ctrl+B, then p / 1 / 2 / i / g: projects / assistant / second assistant / memory / graph\n"
                "Ctrl+B, then Tab: next view\nCtrl+B, then z: expand or restore panes\n"
                "Ctrl+B, then [: scroll and copy terminal history (q to leave)\n"
                "Ctrl+B, then d: detach; run cairnmind to return\n"
                "Ctrl+B, then Ctrl+B: send Ctrl+B to the native assistant\n\n"
                "Cairnmind controls: Tab / Shift+Tab move focus; Enter chooses; Esc goes back.\n"
                "Projects: Enter opens; Start work starts only the chosen assistant.\n"
                "Graph: [ / ] select nodes; arrows rotate; Space pauses; Enter opens a node.\n"
                "Native assistants keep their own shortcuts, paste, Ctrl+C and approval dialogs.\n"
                "Memory readiness, session activity and terminal control are separate states."
            )), id="command-help")
        else:
            yield Input(placeholder=self.translate("Type to find a command…"), id="command-search")
            yield DataTable(id="commands", cursor_type="row", show_header=False)
        yield Footer()

    def on_mount(self):
        if not self.help_only:
            self.query_one(DataTable).add_columns("Command", "Description")
            self.populate("")
            self.query_one(Input).focus()

    def populate(self, query):
        table = self.query_one(DataTable)
        table.clear()
        self.rows = [row for row in COMMANDS if query.casefold() in
                     (self.translate(row[0]) + " " + self.translate(row[1])).casefold()]
        for index, row in enumerate(self.rows):
            table.add_row(plain(self.translate(row[0])), plain(self.translate(row[1])), key=str(index))

    @on(Input.Changed, "#command-search")
    def search(self, event):
        self.populate(event.value)

    @on(Input.Submitted, "#command-search")
    @on(DataTable.RowSelected, "#commands")
    def choose(self):
        table = self.query_one(DataTable)
        if table.row_count:
            self.exit(self.rows[table.cursor_coordinate.row])

    def action_next(self):
        if not self.help_only:
            table = self.query_one(DataTable)
            table.move_cursor(row=min(table.row_count - 1, table.cursor_row + 1))

    def action_previous(self):
        if not self.help_only:
            table = self.query_one(DataTable)
            table.move_cursor(row=max(0, table.cursor_row - 1))

    def action_cancel(self):
        self.exit(None)


def ask_summary(result, translate) -> str:
    """Readable view of an /ask result: answer, cited quotes, unknowns and coverage."""
    if not isinstance(result, dict):
        return translate("The memory service returned no answer.")
    lines = [translate("Status") + ": " + str(result.get("status", "unknown")), "",
             str(result.get("answer") or translate("No verified answer.")).strip()]
    sources = [source for source in result.get("sources", []) if isinstance(source, dict)][:8]
    if sources:
        lines += ["", translate("Sources") + ":"]
        for source in sources:
            where = str(source.get("uri", source.get("id", "")))
            if source.get("line_start"):
                where += f" ({source['line_start']}-{source.get('line_end', source['line_start'])})"
            quote = " ".join(str(source.get("quote", "")).split())
            lines.append(f"- {where}" + (f': "{quote[:240]}"' if quote else ""))
    unknowns = [str(item) for item in result.get("unknowns", [])][:8]
    if unknowns:
        lines += ["", translate("Missing evidence") + ":"] + [f"- {item}" for item in unknowns]
    coverage = result.get("coverage")
    if isinstance(coverage, dict) and coverage.get("answer_terms"):
        lines += ["", translate("Answer words found in quotes")
                  + f": {coverage.get('terms_in_quotes')}/{coverage.get('answer_terms')}"]
    return "\n".join(lines)


def dispatch_command(manager, row):
    if row is None:
        return
    if row not in COMMANDS:
        raise ValueError("Unknown workspace command")
    _, _, kind, value = row
    if kind == "focus":
        manager.focus(value)
    elif kind == "command":
        manager.request_command(value)
    elif kind == "view":
        CockpitState(manager.settings).update({"view": value})
        manager._last_layout = None
        manager._layout()
    elif kind == "help":
        CommandPaletteApp(manager, help_only=True).run()


class JoinWorkspaceApp(App):
    """Explicit keyboard choice when another terminal already has control."""
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { align: center middle; background: #101720; color: #edf2f7; }
    #join { width: 90%; max-width: 78; height: auto; padding: 1 2; border: round #a6d8ff; }
    #join Button { width: 100%; margin-top: 1; }
    """
    BINDINGS = [("escape", "cancel", "Back")]

    def __init__(self, settings):
        super().__init__()
        from .terminal_i18n import language_for, tr
        self.language = language_for(settings)
        self.t = lambda value: tr(value, self.language)

    def compose(self):
        from textual.containers import Vertical
        with Vertical(id="join"):
            yield Static(plain(self.t("This workspace is open in another terminal.")))
            yield Static(plain(self.t("Choose read-only observation or explicitly take control. Existing assistants keep running.")))
            yield Button(self.t("Observe (read only)"), id="join-observe", variant="primary")
            yield Button(self.t("Take control"), id="join-takeover")
            yield Button(self.t("Return to shell"), id="join-cancel")

    def on_mount(self):
        self.query_one("#join-observe").focus()

    @on(Button.Pressed)
    def choose(self, event):
        self.exit({"join-observe": "observe", "join-takeover": "takeover", "join-cancel": None}[event.button.id])

    def action_cancel(self):
        self.exit(None)
