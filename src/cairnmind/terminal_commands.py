"""A searchable, prefix-only command surface shared by every workspace pane."""
from __future__ import annotations

import textwrap

from rich.cells import cell_len
from textual import on
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Button, DataTable, Footer, Input, Static

from . import terminal_theme
from .cockpit_state import CockpitState
from .terminal_i18n_messages import ASK_STATUS, fixed_message
from .terminal_safe import plain


COMMANDS = (
    ("Projects", "Choose or open a project", "command", "search"),
    ("Workspace", "Return to your assistant", "focus", "client1"),
    ("Second assistant", "Show the second visible assistant", "focus", "client2"),
    ("Memory", "Facts, tasks, plans and sources", "focus", "inspector"),
    ("Ask memory", "Question answered from project memory with sources", "command", "ask"),
    ("Brief", "Where work stopped: checkpoint, next task, checks and commits", "command", "brief"),
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
    # New entries go last: the position of every existing command stays the same.
    ("Run next stage", "Show the project, then start what is mapped to the next plan step", "command", "next_stage"),
    ("Notices", "Read the notices that were replaced on the notice line", "command", "notices"),
)


class CommandPaletteApp(App):
    ENABLE_COMMAND_PALETTE = False
    CSS = terminal_theme.BASE_CSS + terminal_theme.css("""
    Screen > .heading { padding: 0 3; }
    #commands { height: 1fr; margin: 0 1; }
    #command-search { margin: 0 1 1 1; }
    #command-empty { height: 1fr; margin: 0 1; content-align: center middle; text-align: center;
        color: $cm-muted; display: none; }
    #command-help-scroll { height: 1fr; margin: 1 1 0 1; padding: 0 2 0 1; border-left: vkey $cm-bg; }
    #command-help-scroll:focus { border-left: vkey $cm-accent; background-tint: transparent 0%; }
    #command-help { height: auto; }
    .-short #command-search, .-short #command-help-scroll { margin: 0 1; }
    """)
    TITLE_CELLS = 22  # the command column; a longer translated name widens it up to a third of the menu
    BINDINGS = [("escape", "cancel", "Back"), ("ctrl+n,down", "next", "Next"),
                ("ctrl+p,up", "previous", "Previous")]

    def __init__(self, manager, *, help_only=False):
        super().__init__()
        terminal_theme.install(self)
        self.manager = manager
        self.help_only = help_only
        self.rows = []
        self._description_cells = 0  # the width of the description column, once it is known
        self._height = 0  # the lines the list has on screen, once known
        self._expanded = None  # the one row shown in full when the wrapped list would not fit
        from .terminal_i18n import language_for, localize_bindings, tr
        self.language = language_for(manager.settings)
        self.translate = lambda value: tr(value, self.language)
        localize_bindings(self, self.language)  # the Footer's key labels in the menu's language

    def compose(self) -> ComposeResult:
        yield Static(plain(self.translate("Keyboard help" if self.help_only else "Commands")), classes="heading")
        if self.help_only:
            # A scrolling area with the focus, so text that does not fit shows a scrollbar and scrolls.
            with VerticalScroll(id="command-help-scroll"):
                yield from self.compose_help()
        else:
            yield Input(placeholder=self.translate("Type to find a command…"), id="command-search")
            yield DataTable(id="commands", cursor_type="row", show_header=False)
            yield Static(plain(self.translate("No matching commands.")), id="command-empty")
        yield Footer()

    def compose_help(self):
        yield Static(help_text(self.translate(
                "Ctrl+B, then Space: search all commands\n"
                "Ctrl+B, then p / 1 / 2 / i / g: projects / assistant / second assistant / memory / graph\n"
                "Ctrl+B, then Tab: next view\nCtrl+B, then z: expand or restore panes\n"
                "Ctrl+B, then n: save a finding to memory (Ctrl+S saves, Esc closes)\n"
                "Ctrl+B, then a: activity panel (notices, plans, tasks)\n"
                "Ctrl+B, then [: scroll and copy terminal history (q to leave)\n"
                "Ctrl+B, then d (or Ctrl+Q in a Cairnmind view): detach; run cairnmind to return\n"
                "Ctrl+B, then Ctrl+B: send Ctrl+B to the native assistant\n\n"
                "Cairnmind controls: Tab / Shift+Tab move focus; Enter chooses; Esc goes back.\n"
                "Projects: Enter opens; Start work starts only the chosen assistant.\n"
                "Graph: [ / ] select nodes; arrows rotate; Space pauses; Enter opens a node.\n"
                "Graph: a switches between all memory and the project; r resets the camera; Backspace goes up.\n"
                "Native assistants keep their own shortcuts, paste, Ctrl+C and approval dialogs.\n"
                "Memory readiness, session activity and terminal control are separate states."
            )), id="command-help")

    def on_mount(self):
        terminal_theme.size_classes(self)
        if self.help_only:
            self.query_one("#command-help-scroll").focus()
        else:
            self.query_one(DataTable).add_columns("Command", "Description")
            self.populate("")
            self.query_one(Input).focus()
            self.call_after_refresh(self.fit_columns)

    def on_resize(self, event):
        terminal_theme.size_classes(self, event.size)
        if not self.help_only:
            self.call_after_refresh(self.fit_columns)

    def fit_columns(self):
        """Two fixed columns that fill the menu, so no row scrolls sideways; a cell keeps its full text.

        The room is taken from the table's own size, not from the region its scrollbars leave, so a
        scrollbar that comes or goes with taller rows cannot leave the columns one cell too wide."""
        table = self.query_one(DataTable)
        height = table.size.height
        reserve = int(table.styles.scrollbar_size_vertical or 0) if len(COMMANDS) > height else 0
        room = table.size.width - reserve - 4 * table.cell_padding
        if room < 20:
            return
        longest = max(cell_len(self.translate(row[0])) for row in COMMANDS)
        title = min(max(self.TITLE_CELLS, longest), max(self.TITLE_CELLS, room // 3))
        terminal_theme.column_widths(table, (title, room - title))
        if (room - title, height) != (self._description_cells, self._height):
            self._description_cells, self._height = room - title, height
            row = table.cursor_row
            self.populate(self.query_one(Input).value, cursor=row)
            table.move_cursor(row=max(0, min(row, table.row_count - 1)))

    def populate(self, query, cursor=0):
        """A description longer than its column wraps onto more lines of its row instead of being cut.
        When the wrapped list would not fit the menu's height, only the chosen command shows its whole
        description and the others end in "…", so every command stays on screen."""
        table = self.query_one(DataTable)
        table.clear()
        query = query.casefold()
        found = [row for row in COMMANDS if query in (self.translate(row[0]) + " " + self.translate(row[1])).casefold()]

        def rank(row):  # names that start with the text, then names that hold it, then descriptions
            name = self.translate(row[0]).casefold()
            return (not name.startswith(query), query not in name)
        self.rows = sorted(found, key=rank)  # stable: the menu's own order within each group
        cells = self._description_cells
        descriptions = [self.translate(row[1]) for row in self.rows]
        wrapped = [textwrap.wrap(text, cells) if cells else [] for text in descriptions]
        self._expanded = None
        if cells and sum(max(1, len(lines)) for lines in wrapped) > self._height:
            self._expanded = max(0, min(cursor, len(self.rows) - 1))
        for index, (row, description, lines) in enumerate(zip(self.rows, descriptions, wrapped)):
            if self._expanded is not None and index != self._expanded:
                lines = [textwrap.shorten(description, cells, placeholder="…")]
            table.add_row(terminal_theme.cropped(self.translate(row[0]), "value"),
                          terminal_theme.styled("\n".join(lines) or description, "muted"), key=str(index),
                          height=max(1, len(lines)))
        # The list stays in the screen; with nothing to list, the sentence takes its place.
        table.display = bool(self.rows)
        self.query_one("#command-empty").display = not self.rows

    @on(DataTable.RowHighlighted, "#commands")
    def expand_chosen(self):
        # The table's own cursor, not the event's row: a highlight left over from a redraw changes nothing.
        table = self.query_one(DataTable)
        if self._expanded is not None and table.row_count and table.cursor_row != self._expanded:
            row = table.cursor_row
            self.populate(self.query_one(Input).value, cursor=row)
            table.move_cursor(row=row)

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


def help_text(value):
    """The help as it is written, with the keys of each line set apart: the part up to the first
    `: ` of a line is the accent, the rest plain. The text itself is not changed."""
    parts = []
    for index, line in enumerate(str(value).split("\n")):
        keys, colon, rest = line.partition(": ")
        if index:
            parts.append(("value", "\n"))
        parts += [("accent", keys + colon), ("value", rest)] if colon else [("value", line)]
    return terminal_theme.assemble(parts)


def ask_summary(result, translate) -> str:
    """Readable view of an /ask result: answer, cited quotes, unknowns and coverage."""
    if not isinstance(result, dict):
        return translate("The memory service returned no answer.")
    status = str(result.get("status", "unknown"))
    answer = str(result.get("answer") or translate("No verified answer.")).strip()
    if status != "complete":
        # Not a verified answer: the agent wrote one of its fixed sentences (or kept the model's text,
        # which fixed_message leaves exactly as it is).
        answer = fixed_message(answer, translate)
    lines = [translate("Status") + ": " + translate(ASK_STATUS.get(status, status)), "", answer]
    sources = [source for source in result.get("sources", []) if isinstance(source, dict)][:8]
    if sources:
        lines += ["", translate("Sources") + ":"]
        for source in sources:
            where = str(source.get("uri", source.get("id", "")))
            if source.get("line_start"):
                where += f" ({source['line_start']}-{source.get('line_end', source['line_start'])})"
            quote = " ".join(str(source.get("quote", "")).split())
            lines.append(f"- {where}" + (f': "{quote[:240]}"' if quote else ""))
    unknowns = [fixed_message(item, translate) for item in result.get("unknowns", [])][:8]
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
    CSS = terminal_theme.BASE_CSS + terminal_theme.css("""
    Screen { align: center middle; }
    #join { max-width: 78; }
    #join-explanation { color: $cm-muted; margin-bottom: 1; }
    #join-takeover, #join-takeover:focus { color: $cm-warn; }
    """)
    BINDINGS = [("escape", "cancel", "Back")]

    def __init__(self, settings):
        super().__init__()
        terminal_theme.install(self)
        from .terminal_i18n import language_for, tr
        self.language = language_for(settings)
        self.t = lambda value: tr(value, self.language)

    def compose(self):
        from textual.containers import Vertical
        with Vertical(id="join", classes="cm-dialog cm-stack"):
            yield Static(terminal_theme.styled(self.t("This workspace is open in another terminal."), "title"),
                         classes="heading")
            yield Static(plain(self.t("Choose read-only observation or explicitly take control. Existing assistants keep running.")),
                         id="join-explanation")
            yield Button(self.t("Observe (read only)"), id="join-observe", variant="primary")
            yield Button(self.t("Take control"), id="join-takeover")
            yield Button(self.t("Return to shell"), id="join-cancel")

    def on_mount(self):
        terminal_theme.size_classes(self)
        self.query_one("#join-observe").focus()

    def on_resize(self, event):
        terminal_theme.size_classes(self, event.size)

    @on(Button.Pressed)
    def choose(self, event):
        self.exit({"join-observe": "observe", "join-takeover": "takeover", "join-cancel": None}[event.button.id])

    def action_cancel(self):
        self.exit(None)
