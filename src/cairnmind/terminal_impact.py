"""The Impact view of the memory inspector: what ``cairnmind impact`` lists, and a person's verdict on one entry.

The list is read with ``GET /api/inspector/impact``, which answers what ``cairnmind impact --json`` prints. The
only write is ``POST /api/inspector/impact/verdict``, sent through the same local broker session as a review
(the panel's ``broker``), with the fingerprint of the entry that was shown. Nothing is retried automatically.
Everything that comes from stored data or from Git is printed as literal text, one line per value.
"""
from __future__ import annotations

import asyncio
import re

from rich.cells import cell_len
from textual import work
from textual.containers import Horizontal
from textual.widgets import Button, Checkbox, DataTable, Input, Label

from . import terminal_theme
from .terminal_i18n_messages import display_word
from .terminal_detail import BAR, evidence_parts, join_lines, joined, labelled, state_role
from .terminal_notices import about
from .terminal_safe import plain
from .terminal_ui import ChoiceModal, Field, FormModal

IMPACT_TIMEOUT = 120  # Git and the parser run for each list; a large worktree needs longer than a page read.
DEFAULT_SINCE = "HEAD"
# The headings of the inspector's table, before translation, for the list and for the impact view.
COLUMNS = {"standard": ("Type / state", "Content / source", "Version / attempt"),
           "impact": ("Kind / status", "Record", "Linked to", "Checked")}
VERDICTS = {"still_valid": "still valid", "needs_update": "needs update", "not_related": "not related"}
SYMBOL_CHANGES = {"modified": "its definition changed", "removed": "it is no longer defined there",
                  "added": "it is newly defined there", "moved": "it is now defined in the renamed file",
                  None: "its definition could not be compared"}
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")


def _line(value, limit=160):
    """One literal line, as ``cairnmind impact`` prints a value; imported when the view is used, not with the panel."""
    from .brief import _line as line
    return line(value, limit)


def _bar(text):
    """Stored text behind a bar, without blank lines: it can never look like a line of this view."""
    return [[BAR, ("stored", _line(line, 300))] for line in str(text).splitlines() if line.strip()]


def checked_text(row, translate):
    """The ``checked:`` line of an entry, or the note that it has no verdict yet."""
    verdict = row.get("verdict")
    if not isinstance(verdict, dict) or verdict.get("verdict") not in VERDICTS:
        return translate("Not checked yet.")
    day = _line(str(verdict.get("at") or "")[:10], 10)
    return translate("checked: {verdict} ({day})", verdict=translate(VERDICTS[verdict["verdict"]]), day=day)


def link_text(link, translate):
    where = _line(link.get("path", ""), 160) + " (" + _line(link.get("change", ""), 140) + ")"
    if link.get("via") == "supported_by":
        snapshot = _line(link.get("snapshot") or "", 10)
        return (translate("rests on a snapshot from {day} of", day=snapshot) if snapshot
                else translate("rests on a snapshot of")) + " " + where
    status = link.get("link_status")
    proposed = "" if status in (None, "observed") else " " + translate("(link {status})", status=_line(status, 20))
    if "symbol" in link:
        change = SYMBOL_CHANGES.get(link.get("symbol_change"), SYMBOL_CHANGES[None])
        return (translate("names {symbol} in", symbol=_line(link["symbol"], 80)) + " " + where + "; "
                + translate(change) + proposed)
    return translate("names the file") + " " + where + proposed


def impact_parts(row, report, translate):
    """The detail of one entry: the record, every link with its reason, the verdict, and what was compared."""
    verdict = row.get("verdict")
    lines = [[("muted", f"[{_line(row.get('kind', ''), 30)}] "),
              (state_role(row.get("status", "")), _line(row.get("status", ""), 30))]]
    if row.get("task_state"):
        lines[0] += [("muted", ", "), (state_role(row["task_state"]), _line(row["task_state"], 30))]
    lines += [*_bar(row.get("title", "")), labelled(translate("Record"), _line(row.get("id", ""), 200)), [],
              [("label", translate("Linked to what changed") + ":")]]
    lines += [[("value", "  - " + link_text(link, translate))] for link in row.get("links") or [] if isinstance(link, dict)]
    lines += evidence_parts(row, translate)
    # The verdict is a person's: its colour is that of the word saved, never of the note under it.
    judged = isinstance(verdict, dict) and verdict.get("verdict") in VERDICTS
    lines += [[], [(terminal_theme.state_style(VERDICTS[verdict["verdict"]]) if judged else "warn",
                    checked_text(row, translate))]]
    if isinstance(verdict, dict) and verdict.get("note"):
        lines += _bar(verdict["note"])
    lines += [[], [("muted", translate("A link means \"worth checking\", not that the record is wrong."))],
              [("dim", translate("Compared with {since} ({commit}).", since=_line(report.get("since", ""), 80),
                                 commit=_line(str(report.get("commit") or "")[:12], 12)))]]
    return join_lines(lines)


def impact_text(row, report, translate):
    return joined(impact_parts(row, report, translate))


def list_summary(result, since, pending, translate):
    """One notice for the loaded list: what it compares, how many entries, and what was left out."""
    records = result.get("records") or []
    since, commit = _line(since, 80), _line(str(result.get("commit") or "")[:12], 12)
    if not result.get("changed_files"):
        text = translate("Nothing in this worktree differs from {since} ({commit}). Enter another commit to "
                         "compare with, for example main or HEAD~3.", since=since, commit=commit)
    elif not records:
        text = translate("No entry without a verdict since {since} ({commit}).", since=since, commit=commit) \
            if pending else translate("No record is linked to what changed since {since} ({commit}).",
                                      since=since, commit=commit)
    else:
        text = translate("{count} records worth re-checking since {since} ({commit}). A link means \"worth "
                         "checking\", not that the record is wrong.", count=len(records), since=since, commit=commit)
    documents = (result.get("documentation") or {}).get("documents") or []
    if documents:
        text += " " + translate("{count} documentation pages mention a changed definition (cairnmind impact "
                                "lists them).", count=len(documents))
    return text


def _settle(task):
    """Collect the outcome of a read nobody waits for any more, so it is never reported as lost."""
    if not task.cancelled():
        task.exception()


class ImpactView:
    """The Impact view's part of the inspector panel. Its handlers are named, so Textual finds them by MRO.

    Rows can be opened as entries only while they are the impact answer shown now: ``impact_shown`` is the
    identity of that answer (project, worktree, commit as typed, pending filter) and ``impact_entries`` its rows.
    """
    impact_since = DEFAULT_SINCE
    impact_pending = False
    impact_shown = None
    impact_entries: dict = {}
    impact_commit = None
    _columns_kind = "standard"
    _impact_task = None
    _verdict_busy = False

    def compose_impact_bar(self):
        with Horizontal(id="impact-bar"):
            yield Label(self.t("Compare with"), id="impact-since-label")
            yield Input(DEFAULT_SINCE, placeholder=DEFAULT_SINCE, id="impact-since", max_length=200)
            yield Checkbox(self.t("Pending only"), False, id="impact-pending")

    def refresh_impact_language(self):
        self.query_one("#impact-since-label", Label).update(self.t("Compare with"))
        self.query_one("#impact-pending", Checkbox).label = self.t("Pending only")
        self.query_one("#impact-verdict", Button).label = self.t("Verdict")
        table = self.query_one("#inspector-rows", DataTable)
        terminal_theme.column_labels(table, [self.t(label) for label in COLUMNS[self._columns_kind]])
        self.fit_columns()

    def state_cell(self, kind, state):
        """`kind / state` as one cell in the panel language; the state takes the colour of its stored word."""
        return terminal_theme.assemble([("muted", display_word(kind, self.t) + " / "),
                                        (state_role(state), display_word(state, self.t))])

    def fit_columns(self, width=None):
        """Give the text column the width the others leave, so every column is on screen without scrolling.

        The first column is never narrower than its longest `kind / state`: a state is not cropped.
        """
        table = self.query_one("#inspector-rows", DataTable)
        if not table.columns:
            return
        width = width or self.size.width
        wide = width >= 120
        longest = max([0, *(cell_len(str(cell)) for cell in table.get_column_at(0))])
        free = width - 2 - 2 * table.cell_padding * len(table.columns)  # focus bar and scrollbar
        if self._columns_kind == "impact":
            first = max(22 if wide else 20, longest)
            checked = max(cell_len(self.t(word)) for word in ("Checked", "not checked", *VERDICTS.values()))
            linked = 34 if wide else 18
            record = free - first - linked - checked
            if record < 20:  # a narrow pane: the paths give way first, down to their heading
                linked = max(cell_len(self.t("Linked to")), linked - (20 - record))
                # then the record text, so the verdict column, the point of this view, stays on screen
                record = max(6, free - first - linked - checked)
            widths = [first, record, linked, checked]
        else:
            first, last = max(24, longest), cell_len(self.t("Version / attempt"))
            if width < 100 and table.row_count:  # the numbers are short: the heading may crop
                last = max(7, *(cell_len(str(cell)) for cell in table.get_column_at(2)))
            if free - first - last < 20:  # a narrow pane: the number column gives way, down to seven cells
                last = max(7, last - (20 - (free - first - last)))
            widths = [first, max(20, free - first - last), last]
        terminal_theme.column_widths(table, widths)

    def on_resize(self, event):
        self.fit_columns(event.size.width)  # the App takes the new size only after this handler

    def use_columns(self, kind):
        if kind != "impact":
            self.impact_shown, self.impact_entries = None, {}  # the table no longer shows an impact answer
        if kind == self._columns_kind:
            return
        table = self.query_one("#inspector-rows", DataTable)
        table.clear(columns=True)
        table.add_columns(*(self.t(label) for label in COLUMNS[kind]))
        self._columns_kind = kind

    def impact_key(self):
        return (self.workspace_data.get("project_id"), self.workspace_data.get("worktree"), self.impact_since,
                self.impact_pending)

    def impact_request(self, method, path, *, sent=None, **kwargs):
        """One request on a client of its own, as /api/ask and /api/brief do: the shared client's lock is held for a
        whole call, and a list that runs Git for two minutes must not hold up the other views."""
        from .terminal_client import BrokerClient
        if not isinstance(self.broker, BrokerClient):
            if sent is not None:
                sent.append(True)
            return self.broker.request(method, path, timeout=IMPACT_TIMEOUT, **kwargs)
        with BrokerClient(self.settings, ensure=False) as client:
            client.connect()
            if sent is not None:
                sent.append(True)  # found and authenticated: from here on the request may reach the service
            return client.request(method, path, timeout=IMPACT_TIMEOUT, **kwargs)

    async def read_impact(self, params):
        """At most one read runs; a newer request waits for it, then reads what is asked now. A read whose caller
        was cancelled still finishes in its thread, and nobody starts a second one beside it."""
        while self._impact_task is not None and not self._impact_task.done():
            await asyncio.wait([self._impact_task])
        self._impact_task = asyncio.ensure_future(asyncio.to_thread(
            self.impact_request, "GET", "/api/inspector/impact", params=params))
        self._impact_task.add_done_callback(_settle)
        return await asyncio.shield(self._impact_task)

    def clear_impact(self):
        self.rows, self.impact_shown, self.impact_entries = {}, None, {}
        if self.entity_kind == "impact":
            self.entity = self.entity_kind = None
        self.render_rows([])

    async def load_impact(self):
        """Read the list for what the view asks now; True only when that list is the one now shown."""
        if self.view != "impact":
            return False
        self.clear_impact()
        if not self.workspace_data.get("project_id"):
            self.notice(self.t("Open a project first."))
            return False
        key = self.impact_key()
        since, pending = key[2], key[3]
        params = {**self.params(), "since": since, "pending": True if pending else None}
        self.notice(self.t("Comparing the worktree with {since}…", since=_line(since, 80)))
        try:
            result = await self.read_impact(params)
        except (OSError, ValueError, RuntimeError) as exc:
            if self.view == "impact" and key == self.impact_key():
                self.clear_impact()
                self.notice(self.t("Could not read the impact list.") + " " + self.error_detail(exc), "error")
            return False
        if self.view != "impact" or key != self.impact_key():
            return False
        self.history_owner, self.entity, self.entity_kind = None, None, None
        self.next_cursor = self.source_next = None
        if not isinstance(result, dict) or "changed_files" not in result or "commit" not in result:
            self.clear_impact()  # e.g. the preview answers every inspector read with an empty page
            self.notice(self.t("The impact list is not available here."))
            return False
        entries = {row["id"]: row for row in result.get("records") or []
                   if isinstance(row, dict) and isinstance(row.get("id"), str)}
        self.rows = dict(entries)
        self.render_rows(list(entries.values()))
        self.impact_shown, self.impact_entries, self.impact_commit = key, entries, result.get("commit")
        self.notice(list_summary(result, since, pending, self.t))
        return True

    def render_impact_rows(self, rows):
        self.use_columns("impact")
        table = self.query_one("#inspector-rows", DataTable)
        table.clear()
        query = self.query_one("#inspector-filter", Input).value.casefold()
        for row in rows:
            title = _line(row.get("title", ""), 90)
            links = list(dict.fromkeys(_line(link.get("path", ""), 80) + (":" + _line(link["symbol"], 60)
                                       if "symbol" in link else "") for link in row.get("links") or []
                                       if isinstance(link, dict)))
            shown = ", ".join(links[:2]) + (f" (+{len(links) - 2})" if len(links) > 2 else "")
            if query and query not in (title + " " + " ".join(links)).casefold():
                continue
            verdict = row.get("verdict")
            word = (VERDICTS[verdict["verdict"]] if isinstance(verdict, dict)
                    and verdict.get("verdict") in VERDICTS else "not checked")
            table.add_row(self.state_cell(_line(row.get("kind", ""), 20), _line(row.get("status", ""), 20)),
                          plain(title), plain(shown, limit=200),
                          terminal_theme.styled_with(self.t(word), terminal_theme.state_style(word)), key=row["id"])
        self.fit_columns()

    def open_impact(self, identifier):
        # Only an entry of the answer shown now, for the project, worktree and commit the view asks for now.
        row = self.impact_entries.get(identifier)
        if self.view != "impact" or row is None or self.impact_shown != self.impact_key():
            return
        self._entity_request += 1
        self.entity_kind, self.entity, self.history_owner, self.source_next = "impact", row, None, None
        report = {"since": self.impact_shown[2], "commit": self.impact_commit}
        self.show_detail(row, kind="impact", parts=impact_parts(row, report, self.t))
        self.notice(self.t("Verdict saves your judgement of this entry as an audit note. It changes no record, "
                           "no status and no task."))

    def on_input_submitted(self, event):
        if event.input.id != "impact-since":
            return
        since = " ".join(event.value.split()) or DEFAULT_SINCE
        event.input.value = since
        self.impact_since = since
        self.refresh_page()

    def on_checkbox_changed(self, event):
        if event.checkbox.id == "impact-pending" and bool(event.value) != self.impact_pending:
            self.impact_pending = bool(event.value)
            self.refresh_page()

    def error_detail(self, exc):
        from .workspace_finding import ERROR_LIMIT, detail_text
        return _line(detail_text(exc, self.t, self.language), ERROR_LIMIT)

    @work(group="inspector-read", exclusive=True, exit_on_error=False)
    async def reload_impact(self, identifier):
        before = self.impact_shown
        if await self.load_impact() and self.impact_shown == before and identifier in self.impact_entries:
            self.open_impact(identifier)

    def record_impact_verdict(self):
        # Checked before a worker starts: a second press must neither cancel nor repeat a verdict being saved.
        if self._verdict_busy:
            self.notice(self.t("A verdict is being saved. Wait for its answer."))
            return
        if self.entity_kind != "impact" or not self.entity or self.impact_shown != self.impact_key():
            self.notice(self.t("Open an entry of the list first."))
            return
        self._verdict_busy = True
        self.send_impact_verdict(dict(self.entity), self.impact_shown)

    @work(group="impact-verdict", exit_on_error=False)
    async def send_impact_verdict(self, row, shown):
        try:
            await self.ask_and_send_verdict(row, shown)
        finally:
            self._verdict_busy = False

    async def ask_and_send_verdict(self, row, shown):
        project, worktree, since, _ = shown
        scope = self._scope_version
        if not isinstance(row.get("fingerprint"), str) or not _FINGERPRINT.fullmatch(row["fingerprint"]):
            self.notice(self.t("This entry cannot be judged as shown. Press r to read the list again."))
            return
        choice = await self.push_screen_wait(ChoiceModal(self.t("Verdict"), [
            (self.t("Still valid"), "still_valid"), (self.t("Needs update: say what to update"), "needs_update"),
            (self.t("Not related"), "not_related")]))
        if choice not in VERDICTS:
            return
        needs = choice == "needs_update"
        form = await self.push_screen_wait(FormModal(self.t("Verdict") + ": " + self.t(VERDICTS[choice]), [
            Field("note", self.t("What should be updated") if needs else self.t("Note (optional)"),
                  required=needs, multiline=True)],
            note=self.t("Saved as an audit note for this entry as listed. No record, status or task is changed."),
            submit=self.t("Save verdict")))
        if form is None:
            return
        if scope != self._scope_version or self.impact_key()[:2] != (project, worktree):
            self.notice_for(about(project, worktree), self.t("The project changed during review. Nothing was saved."),
                            "warn")
            return
        note = str(form.get("note") or "").strip()
        if needs and not note:
            self.notice(self.t("Needs update requires a note on what to update. Nothing was saved."), "warn")
            return
        payload = {"project": project, "since": since, "record_id": row["id"], "fingerprint": row["fingerprint"],
                   "verdict": choice}
        if worktree:
            payload["worktree"] = worktree
        if note:
            payload["note"] = note
        sent = []
        try:
            result = await asyncio.to_thread(self.impact_request, "POST", "/api/inspector/impact/verdict",
                                             json=payload, sent=sent)
        except (OSError, ValueError, RuntimeError) as exc:
            if scope != self._scope_version:
                return
            from .workspace_finding import outcome_unknown
            status = getattr(exc, "status_code", None)
            if status in (404, 409) or outcome_unknown(exc, sent):
                await self.load_impact()
            if status == 409:
                self.notice(self.t("The code or the record changed, the list was reloaded. Nothing was saved."), "warn")
            elif status == 404:
                self.notice(self.t("This record is no longer in the list, the list was reloaded. Nothing was saved."),
                            "warn")
            elif outcome_unknown(exc, sent):
                self.notice(self.t("No clear answer from the memory service. The verdict may have been saved; the "
                                   "list was reloaded and shows it if so. Saving the same verdict again is safe "
                                   "and stores nothing twice.") + " " + self.error_detail(exc), "warn")
            else:
                self.notice(self.t("The verdict was not saved.") + " " + self.error_detail(exc), "error")
            return
        if scope == self._scope_version:
            await self.load_impact()
            saved = isinstance(result, dict) and result.get("already_saved")
            self.notice(self.t("This verdict was already saved." if saved else
                               "Verdict saved: {verdict}. No record, status or task was changed.",
                               verdict=self.t(VERDICTS[choice])))
