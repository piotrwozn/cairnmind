"""One palette and one set of global rules for every terminal surface (panels, dialogs, tmux).

Every colour is a named token here; other UI modules write `$cm-<token>` in their CSS and pass it
through `css()`, or use `style()` / `styled()` / `assemble()` for Rich text. tmux uses the nearest
xterm-256 cell of the same token (`XTERM`, `TMUX`), so a state has the same colour in a panel and
in the status line.

Styling never upgrades trust: `styled()` and `assemble()` only colour text the caller built, every
string still goes through `terminal_safe.plain()`, the role `stored` is always the plain text
colour, and no evidence state is ever green.

What was checked on Textual 8.2.8 (so nobody repeats it):
- App CSS beats every widget DEFAULT_CSS rule, whatever its selector. `Button { border: none;
  height: 1 }` gives a one-row button for all variants; `compact=True` is not needed. `line-pad: 0`
  is refused by the CSS parser, so a button's label always has one more cell on each side than
  its `padding` says.
  Input and SelectCurrent become one row the same way. The focus tint of Input and Select is
  switched off with `background-tint`, and the pressed tint of Button with `tint`.
- `Screen.CSS` of a modal is scoped to the class that declares it, so a subclass loses the frame.
  Dialog rules are therefore class selectors in `BASE_CSS` (`.cm-dialog`) and every App whose
  screens open dialogs starts its CSS with `BASE_CSS`.
- A class set on the App (`-short`) matches inside modal screens: `.-short .cm-dialog` works.
- A container with `height: auto; max-height` does not shrink a scrolling child, so the action
  row would be pushed out. `fit_body()` gives the scrolling body an explicit `max-height`.
- The notice line has no left bar: a bar glyph on every wrapped row would sit inside the sentence
  when the screen is read as text (tests and screen readers join the rows).
- An App calls `install()` in `__init__` (the theme is applied when the app starts) and
  `size_classes()` from `on_mount` and `on_resize`. Textual runs the `on_mount` / `on_resize`
  of every class in the MRO, so a subclass handler does not hide the one in `BasePanel`.
"""
from __future__ import annotations

import re

from rich.style import Style
from rich.text import Text

from .terminal_safe import plain

TOKENS = {
    "bg": "#101418", "panel": "#1a2028", "raised": "#262d37", "border": "#3d4754",
    "border-hi": "#5f6b7a", "text": "#d7dde4", "strong": "#f1f5f9", "muted": "#8b97a6",
    "dim": "#5c6673", "accent": "#87afd7", "select": "#1f4468", "ok": "#8fd08a",
    "warn": "#f0cf87", "danger": "#f47067",
}
# Nearest xterm-256 cell of each token: what a 256-colour terminal and tmux show.
XTERM = {
    "bg": 233, "panel": 234, "raised": 236, "border": 238, "border-hi": 242, "text": 253,
    "strong": 255, "muted": 246, "dim": 241, "accent": 110, "select": 24, "ok": 114,
    "warn": 222, "danger": 203,
}
TMUX = {"bg": 234, "fg": 250, "label": 246, "sep": 240, "accent": 110, "ok": 114, "warn": 222,
        "danger": 203, "line": 238, "select": 24}

SHORT_HEIGHT = 30  # below this many rows dialogs drop their padding and blank rows
NARROW_WIDTH = 40  # below this many columns a dialog's buttons stack, so none is pushed out of sight
_TOKEN = re.compile(r"\$cm-([a-z]+(?:-[a-z]+)*)")


def css(text: str) -> str:
    """Replace `$cm-<token>` with its hex value; an unknown token is a programming error."""
    return _TOKEN.sub(lambda match: TOKENS[match.group(1)], text)


BASE_CSS = css("""
Screen { background: $cm-bg; color: $cm-text; }
* { scrollbar-size-vertical: 1; scrollbar-size-horizontal: 1; scrollbar-background: $cm-bg;
    scrollbar-background-hover: $cm-bg; scrollbar-background-active: $cm-bg;
    scrollbar-color: $cm-border; scrollbar-color-hover: $cm-border-hi;
    scrollbar-color-active: $cm-accent; scrollbar-corner-color: $cm-bg; }
Header { height: 1; background: $cm-panel; color: $cm-strong; }
HeaderIcon { display: none; }
HeaderTitle { content-align: left middle; padding: 0 1; text-style: bold; }
Footer { background: $cm-panel; }
FooterKey { background: $cm-panel; color: $cm-muted; }
FooterKey .footer-key--key { color: $cm-accent; background: $cm-panel; text-style: bold; }
FooterKey .footer-key--description { color: $cm-muted; background: $cm-panel; }
FooterKey:hover { background: $cm-raised; }
Button { height: 1; min-width: 6; width: auto; border: none; padding: 0 1; margin: 0 1 0 0;
    background: $cm-raised; color: $cm-text; text-style: none; tint: transparent 0%; }
Button:hover { background: $cm-border; border: none; }
Button.-primary { background: $cm-select; color: $cm-strong; text-style: bold; border: none; }
Button.-primary:hover { background: $cm-select; color: $cm-strong; border: none; }
Button:focus, Button.-primary:focus { background: $cm-accent; color: $cm-bg; text-style: bold; }
Button.-active { tint: transparent 0%; border: none; }
Button:disabled { opacity: 1; text-opacity: 1; background: $cm-panel; color: $cm-dim; text-style: none; }
Input { height: 1; border: none; border-left: thick $cm-border; padding: 0 1; background: $cm-raised;
    color: $cm-text; background-tint: transparent 0%; }
Input:focus { border: none; border-left: thick $cm-accent; background-tint: transparent 0%; }
Input > .input--placeholder { color: $cm-dim; }
Select { height: 1; }
Select > SelectCurrent { height: 1; border: none; border-left: thick $cm-border; padding: 0 1;
    background: $cm-raised; color: $cm-text; }
Select:focus > SelectCurrent { border: none; border-left: thick $cm-accent; text-style: bold;
    background-tint: transparent 0%; }
Select > SelectOverlay { border: round $cm-border-hi; background: $cm-panel; }
TextArea { border: round $cm-border; background: $cm-bg; padding: 0 1; }
TextArea:focus { border: round $cm-accent; background-tint: transparent 0%; }
Checkbox, Switch { height: 1; border: none; background: transparent; padding: 0 1; }
Checkbox:focus { border: none; background-tint: transparent 0%; }
Switch { border-left: thick $cm-border; }
Switch:focus { border: none; border-left: thick $cm-accent; background-tint: transparent 0%; }
Switch .switch--slider, Switch:hover > .switch--slider { color: $cm-border-hi; background: $cm-bg; }
Switch.-on .switch--slider, Switch.-on:hover > .switch--slider { color: $cm-accent; }
Checkbox:focus > .toggle--label { text-style: bold underline; }
DataTable { background: $cm-bg; color: $cm-text; border-left: vkey $cm-bg; }
DataTable:focus { border-left: vkey $cm-accent; background-tint: transparent 0%; }
DataTable > .datatable--header, DataTable:focus > .datatable--header { background: $cm-bg; color: $cm-muted;
    text-style: bold; background-tint: transparent 0%; }
DataTable > .datatable--odd-row, DataTable > .datatable--even-row { background: $cm-bg; }
DataTable > .datatable--cursor { background: $cm-raised; color: $cm-text; text-style: none; }
DataTable:focus > .datatable--cursor { background: $cm-select; color: $cm-strong; text-style: bold; }
DataTable > .datatable--hover { background: $cm-bg; }
DataTable > .datatable--header-hover { background: $cm-bg; }
OptionList { background: $cm-panel; border: none; }
OptionList > .option-list--option-highlighted { background: $cm-select; color: $cm-strong; text-style: bold; }
.heading, .form-title { color: $cm-accent; text-style: bold; height: auto; }
.muted, .form-label { color: $cm-muted; height: auto; }
.actions, .form-actions { height: 1; min-height: 1; margin-top: 1; }
#panel-notice { height: auto; color: $cm-text; padding: 0 1; max-height: 5; }
#panel-notice.-warn { color: $cm-warn; }
#panel-notice.-error { color: $cm-danger; text-style: bold; }
#notice-mark { height: auto; color: $cm-muted; padding: 0 1; display: none; }
ModalScreen { align: center middle; background: $cm-bg 70%; }
.cm-dialog { width: 92%; max-width: 100; height: auto; max-height: 92%; background: $cm-panel;
    border: round $cm-border-hi; padding: 1 2; }
.cm-dialog.-wide { max-width: 120; }
.cm-dialog.-menu { max-width: 72; }
.cm-dialog > .heading, .cm-dialog > .form-title { margin-bottom: 1; }
.cm-dialog.-consent { border: heavy $cm-warn; }
.cm-dialog.-consent > .heading { color: $cm-warn; }
.cm-dialog-body { height: auto; background: $cm-panel; }
.cm-dialog DataTable { background: $cm-panel; border-left: vkey $cm-panel; }
.cm-dialog DataTable:focus { border-left: vkey $cm-accent; }
.cm-dialog DataTable > .datatable--header, .cm-dialog DataTable > .datatable--odd-row,
.cm-dialog DataTable > .datatable--even-row, .cm-dialog DataTable > .datatable--hover,
.cm-dialog DataTable > .datatable--header-hover { background: $cm-panel; }
.cm-dialog Button:disabled { background: $cm-bg; }
.cm-dialog-body .form-label { margin-top: 1; }
.cm-dialog-body .form-label:first-child { margin-top: 0; }
#credential-remember { height: 1; margin: 1 0; }
#form-fields TextArea { height: 5; }
#folder-notice, #discovery-notice, #group-notice { height: auto; max-height: 3; color: $cm-muted; }
.cm-hint { color: $cm-muted; height: 1; margin-top: 1; }
.cm-note { height: auto; color: $cm-text; border-left: thick $cm-warn; padding: 0 1; margin: 0 0 1 0; }
.cm-error, #form-error, #credential-error { height: auto; color: $cm-danger; text-style: bold; }
.-short .cm-dialog { padding: 0 1; max-height: 100%; }
.-short .cm-dialog > .heading, .-short .cm-dialog > .form-title, .-short .actions, .-short .form-actions,
.-short .cm-hint, .-short .cm-dialog-body .form-label { margin-top: 0; margin-bottom: 0; }
.-short #credential-remember { margin: 0; }
.-narrow .cm-dialog { width: 100%; padding-left: 1; padding-right: 1; }
.-narrow .actions, .-narrow .form-actions { layout: vertical; height: auto; }
.-narrow .actions Button, .-narrow .form-actions Button { width: 100%; margin: 0; }
.-narrow .cm-hint { height: auto; }
.-short #form-fields TextArea { height: 3; }
""")


def install(app) -> None:
    """Register and select the Textual theme, so widgets no rule names also use the palette."""
    from textual.theme import Theme
    t = TOKENS
    app.register_theme(Theme(
        name="cairnmind", dark=True, primary=t["select"], secondary=t["border-hi"], accent=t["accent"],
        foreground=t["text"], background=t["bg"], surface=t["panel"], panel=t["raised"],
        success=t["ok"], warning=t["warn"], error=t["danger"],
        variables={
            "block-cursor-background": t["select"], "block-cursor-foreground": t["strong"],
            "block-cursor-text-style": "bold", "block-cursor-blurred-background": t["raised"],
            "block-cursor-blurred-foreground": t["text"], "block-cursor-blurred-text-style": "none",
            "scrollbar": t["border"], "scrollbar-hover": t["border-hi"], "scrollbar-active": t["accent"],
            "scrollbar-background": t["bg"], "scrollbar-background-hover": t["bg"],
            "scrollbar-background-active": t["bg"], "scrollbar-corner-color": t["bg"],
            "footer-background": t["panel"], "footer-foreground": t["muted"],
            "footer-key-foreground": t["accent"], "footer-key-background": t["panel"],
            "footer-description-foreground": t["muted"], "footer-description-background": t["panel"],
            "footer-item-background": t["panel"],
            "input-cursor-background": t["text"], "input-cursor-foreground": t["bg"],
            "input-selection-background": t["select"], "input-selection-foreground": t["strong"],
            "border": t["accent"], "border-blurred": t["border"],
            "button-foreground": t["text"], "button-color-foreground": t["strong"],
            "button-focus-text-style": "bold",
        }))
    app.theme = "cairnmind"


def is_short(app) -> bool:
    return app.size.height < SHORT_HEIGHT


def size_classes(app) -> None:
    """`-short` on the App while the terminal has few rows, `-narrow` while it has few columns.

    A short dialog gives up padding and blank rows; a narrow one stacks its buttons.
    """
    app.set_class(is_short(app), "-short")
    app.set_class(app.size.width < NARROW_WIDTH, "-narrow")


def fit_body(screen, body, chrome_rows: int = 10) -> None:
    """Let a dialog's scrolling body use the rows its frame, title, actions and hint leave free.

    `chrome_rows` counts every row of a `.cm-dialog` that is not the body at normal size
    (2 border, 2 padding, title and its blank row, action row and its blank row, hint and its
    blank row = 10). A short terminal drops the 2 padding rows and the 3 blank rows.
    """
    app = screen.app
    height = app.size.height
    if is_short(app):
        rows = height - (chrome_rows - 5)
    else:
        rows = int(height * .92) - chrome_rows
    if app.size.width < NARROW_WIDTH:
        rows -= 4  # stacked buttons and a wrapped hint
    body.styles.max_height = max(3, rows)


def fill_column(table, key: str, longest: int) -> None:
    """Make a one-column list's cursor row span the list.

    `longest` is the widest cell. When it does not fit, the column keeps its natural width, so
    the full text stays in the row and the list scrolls sideways as before: nothing is cut.
    """
    try:
        column = table.columns[key]
        room = table.scrollable_content_region.width - 2 * table.cell_padding
    except (KeyError, AttributeError):
        return
    fill = room >= longest
    width = room if fill else longest
    if (column.width, column.auto_width) != (width, not fill):
        column.width, column.auto_width = width, not fill
        table._require_update_dimensions = True
        table.refresh(layout=True)


_ROLES = {
    "title": Style(color=TOKENS["strong"], bold=True),
    "label": Style(color=TOKENS["muted"]),
    "value": Style(color=TOKENS["text"]),
    "muted": Style(color=TOKENS["muted"]),
    "dim": Style(color=TOKENS["dim"]),
    "accent": Style(color=TOKENS["accent"], bold=True),
    "ok": Style(color=TOKENS["ok"]),
    "warn": Style(color=TOKENS["warn"]),
    "warn-strong": Style(color=TOKENS["warn"], bold=True),
    "danger": Style(color=TOKENS["danger"], bold=True),
    "quote-bar": Style(color=TOKENS["border-hi"]),
    "stored": Style(color=TOKENS["text"]),
}


def style(role: str) -> Style:
    return _ROLES[role]


def styled(value, role: str) -> Text:
    """One escaped string in one style: no spans, so `str()` and `.plain` are the full string."""
    text = plain(value)
    text.style = style(role)
    return text


def styled_with(value, chosen: Style) -> Text:
    """As `styled`, for a style returned by `state_style` or `evidence_style`."""
    text = plain(value)
    text.style = chosen
    return text


def assemble(parts) -> Text:
    """Join `(role, string)` parts; `.plain` is exactly the escaped strings joined.

    A part's role may also be a Style from `state_style` / `evidence_style`. The role `stored`
    (text kept from a person, a model or a third-party tool) is always the plain text colour.
    """
    text = Text()
    for role, value in parts:
        text.append(plain(value).plain, style=role if isinstance(role, Style) else style(role))
    return text


_STATE = {
    **dict.fromkeys(("confirmed", "done", "still valid"), "ok"),
    **dict.fromkeys(("needs_review", "needs update", "not checked", "stale", "partial", "disputed",
                     "ACK required"), "warn"),
    **dict.fromkeys(("superseded", "not related"), "muted"),
    **dict.fromkeys(("failed", "unavailable"), "danger"),
    **dict.fromkeys(("proposed", "todo"), "value"),
}


def state_style(word: str) -> Style:
    """Style of an exact raw state word; an unknown word is plain text, never green."""
    return _ROLES[_STATE.get(word, "value")]


_EVIDENCE = {"anchored": "value", "evidence_replaced": "warn", "unverified": "warn", "degraded": "warn",
             "orphaned": "danger"}


def evidence_style(state: str) -> Style:
    """Style by the record's `evidence_state` field. Anchored is neutral; unknown is a warning."""
    return _ROLES[_EVIDENCE.get(state, "warn")]


def origin_style(mark: str) -> Style:
    """`[machine]` is muted, `[reported]` a warning; anything else is plain text."""
    return _ROLES[{"[machine]": "muted", "[reported]": "warn"}.get(mark, "value")]
