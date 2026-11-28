"""The Proposed view of the memory inspector: proposed findings nobody has touched for a while (#189).

The list is read with ``GET /api/inspector/proposed``, which answers what ``cairnmind proposed --json`` prints,
oldest first. The view writes nothing. ``Enter`` opens the record as the Records view does, and its **Confirm**
and **Dispute** are the inspector's review, a person's act with the version check and an audit note (#177).
Everything that comes from stored data is printed as literal text, one line per value.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.cells import cell_len
from textual import work
from textual.widgets import DataTable, Input

from cairnmind.memory.stale_proposed import DEFAULT_DAYS, MAX_DAYS
from cairnmind.pal.terminal_safe import plain
from cairnmind.terminal.terminal_i18n_messages import display_word
from cairnmind.terminal.terminal_ui import Field, FormModal

# How the newest evidence time is known (sql.evidence_time), as the cell says it, before translation.
BASIS_WORDS = {"stated": "stated time", "observed": "at the latest", "mixed": "mixed times", "unknown": "time unknown"}
DAYS_ERROR = "Enter a whole number from 0 to 3650."  # stale_proposed.MAX_DAYS, a fixed sentence of the catalog
SUMMARY = "Proposed findings not updated for {days} days or more: {count} · oldest first · read only."
SUMMARY_ONE_DAY = "Proposed findings not updated for 1 day or more: {count} · oldest first · read only."
TEXT_WIDTH, ID_WIDTH = 30, 8  # the narrowest text column worth reading; an id cropped to its first cells
GIVE_WAY = (2, 4, 0, 3)  # Id, Newest evidence, Days untouched, Sources: the columns that narrow for the text


def _line(value, limit=160):
    from cairnmind.memory.brief import _line as line
    return line(value, limit)


def days_error(value):
    """The form's check of the days field: a whole number in range, or the sentence that says what to type."""
    if not (value.isascii() and value.isdigit()) or not 0 <= int(value) <= MAX_DAYS:  # "٣" and "²" are digits too
        return DAYS_ERROR
    return None


def evidence_cell(row, translate):
    day = _line(str(row.get("evidence_latest") or "")[:10], 10)
    basis = row.get("evidence_basis") if row.get("evidence_basis") in BASIS_WORDS else "unknown"
    return f"{day} ({translate(BASIS_WORDS[basis])})" if day else translate("time unknown")


def list_summary(result, days, translate):
    total = result.get("total", 0) if type(result.get("total")) is int else 0
    text = translate(SUMMARY_ONE_DAY if days == 1 else SUMMARY, days=days, count=total)
    if result.get("truncated"):
        text += " " + translate("Only the {shown} oldest are shown.", shown=len(result.get("items") or []))
    if total:
        text += " " + translate("Open one to confirm or dispute it.")
    return text


class ProposedView:
    """The Proposed view's part of the inspector panel; the panel calls it from its list, filter and language code."""
    proposed_days = DEFAULT_DAYS
    _proposed_result: tuple[dict[str, Any], int] | None = None

    if TYPE_CHECKING:
        workspace_data: dict[str, Any]
        view: str
        rows: dict[str, Any]
        entity: Any
        history_owner: Any
        next_cursor: Any
        source_next: Any
        _scope_version: int

        def t(self, text: Any, **values: Any) -> str: ...
        def query_one(self, selector: Any, expect_type: Any = ...) -> Any: ...
        def notice(self, value: Any, kind: str = "info") -> None: ...
        def view_hint(self, text: str) -> None: ...
        def render_rows(self, rows: list[Any]) -> None: ...
        def use_columns(self, kind: str) -> None: ...
        def fit_columns(self, width: int | None = None) -> None: ...
        def params(self) -> dict[str, Any]: ...
        async def request(self, method: str, path: str, *, params: Any = None, json: Any = None) -> Any: ...
        async def push_screen_wait(self, screen: Any) -> Any: ...
        async def load_page(self, cursor: Any = None) -> None: ...

    async def load_proposed(self):
        if not self.workspace_data.get("project_id"):
            self.rows, self._proposed_result = {}, None
            self.render_rows([])
            self.notice(self.t("Open a project first."))
            return
        scope, view, days = self._scope_version, self.view, self.proposed_days
        result = await self.request("GET", "/api/inspector/proposed", params={**self.params(), "days": days})
        if scope != self._scope_version or view != self.view or days != self.proposed_days:
            return
        self.history_owner, self.entity = None, None
        self.next_cursor = self.source_next = None
        result = result if isinstance(result, dict) else {}
        items = [row for row in result.get("items") or [] if isinstance(row, dict) and isinstance(row.get("id"), str)]
        self.rows = {row["id"]: row for row in items}
        self.render_rows(items)
        self._proposed_result = (result, days)
        self.view_hint(list_summary(result, days, self.t))  # read again on every journal event, like a count

    def refresh_proposed_language(self):
        if self.view == "proposed" and self._proposed_result and self.entity is None:
            self.view_hint(list_summary(*self._proposed_result, self.t))

    def render_proposed_rows(self, rows):
        self.use_columns("proposed")
        table = self.query_one("#inspector-rows", DataTable)
        table.clear()
        query = self.query_one("#inspector-filter", Input).value.casefold()
        for row in rows:
            text = _line(str(row.get("content") or "").strip().split("\n", 1)[0], 200)
            identifier, kind = _line(row["id"], 64), _line(row.get("kind") or "", 30)
            if query and query not in " ".join((text, identifier, kind)).casefold():
                continue
            age = row.get("age_days")
            table.add_row(plain(str(age) if type(age) is int else "?"), plain(display_word(kind, self.t)),
                          plain(identifier), plain(str(row.get("sources", "?"))),
                          plain(evidence_cell(row, self.t)), plain(text), key=row["id"])
        self.fit_columns()

    def fit_proposed_columns(self, table, free):
        """Every column but the text keeps its longest cell (at least its heading); the text takes what is left.

        The text is what a person reads to judge a finding, so it keeps at least TEXT_WIDTH cells while the
        others can give way, in GIVE_WAY order: the id first (Enter opens the whole record), then the evidence
        cell down to its date, then the headings of the two number columns down to their numbers.
        """
        def cells(index):
            return [cell_len(str(cell)) for cell in table.get_column_at(index)]
        headings = ("Days untouched", "Kind", "Id", "Sources", "Newest evidence")
        widths = [max([cell_len(self.t(heading)), *cells(index)]) for index, heading in enumerate(headings)]
        widths[2] = min(widths[2], 36)
        floors = {2: ID_WIDTH, 4: 10, 0: max([3, *cells(0)]), 3: max([3, *cells(3)])}
        for index in GIVE_WAY:
            short = TEXT_WIDTH - (free - sum(widths))
            if short <= 0:
                break
            widths[index] = max(min(floors[index], widths[index]), widths[index] - short)
        return [*widths, max(10, free - sum(widths))]

    @work(group="inspector-filters", exclusive=True, exit_on_error=False)
    async def edit_proposed_days(self):
        value = await self.push_screen_wait(FormModal(self.t("List filters"), [
            Field("days", self.t("Not updated for at least this many days"), str(self.proposed_days), True,
                  validator=days_error)]))
        if value is not None and self.view == "proposed":
            self.proposed_days = int(value["days"])
            await self.load_page()
