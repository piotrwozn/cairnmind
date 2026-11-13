"""The Impact view of the memory inspector: what ``cairnmind impact`` lists, and a person's verdict on one entry.

The list is read with ``GET /api/inspector/impact``, which answers what ``cairnmind impact --json`` prints. The
only write is ``POST /api/inspector/impact/verdict``, sent through the same local broker session as a review
(the panel's ``broker``), with the fingerprint of the entry that was shown. Nothing is retried automatically.
Everything that comes from stored data or from Git is printed as literal text, one line per value.
"""
from __future__ import annotations

import asyncio
import re

from textual import work
from textual.containers import Horizontal
from textual.widgets import Button, Checkbox, DataTable, Input, Label

from .terminal_safe import plain
from .terminal_ui import ChoiceModal, Field, FormModal

IMPACT_TIMEOUT = 120  # Git and the parser run for each list; a large worktree needs longer than a page read.
DEFAULT_SINCE = "HEAD"
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
    return ["  | " + _line(line, 300) for line in str(text).splitlines() if line.strip()]


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


def impact_text(row, report, translate):
    """The detail of one entry: the record, every link with its reason, the verdict, and what was compared."""
    from .terminal_inspector import EVIDENCE_LABELS
    state = f", {_line(row['task_state'], 30)}" if row.get("task_state") else ""
    lines = [f"[{_line(row.get('kind', ''), 30)}] {_line(row.get('status', ''), 30)}{state}",
             *_bar(row.get("title", "")), translate("Record") + ": " + _line(row.get("id", ""), 200), "",
             translate("Linked to what changed") + ":"]
    lines += ["  - " + link_text(link, translate) for link in row.get("links") or [] if isinstance(link, dict)]
    label = EVIDENCE_LABELS.get((row.get("evidence_state"), row.get("quote_survives")))
    if label:
        lines.append(translate("Evidence") + ": " + translate(label))
    lines += ["", checked_text(row, translate)]
    verdict = row.get("verdict")
    if isinstance(verdict, dict) and verdict.get("note"):
        lines += _bar(verdict["note"])
    lines += ["", translate("A link means \"worth checking\", not that the record is wrong."),
              translate("Compared with {since} ({commit}).", since=_line(report.get("since", ""), 80),
                        commit=_line(str(report.get("commit") or "")[:12], 12))]
    return "\n".join(lines)


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


class ImpactView:
    """The Impact view's part of the inspector panel. Its handlers are named, so Textual finds them by MRO."""
    impact_since = DEFAULT_SINCE
    impact_pending = False
    impact_report: dict = {}
    _columns_kind = "standard"

    def compose_impact_bar(self):
        with Horizontal(id="impact-bar"):
            yield Label(self.t("Compare with"), id="impact-since-label")
            yield Input(DEFAULT_SINCE, placeholder=DEFAULT_SINCE, id="impact-since", max_length=200)
            yield Checkbox(self.t("Pending only"), False, id="impact-pending")

    def refresh_impact_language(self):
        self.query_one("#impact-since-label", Label).update(self.t("Compare with"))
        self.query_one("#impact-pending", Checkbox).label = self.t("Pending only")
        self.query_one("#impact-verdict", Button).label = self.t("Verdict")

    def use_columns(self, kind):
        if kind == self._columns_kind:
            return
        table = self.query_one("#inspector-rows", DataTable)
        table.clear(columns=True)
        if kind == "impact":
            table.add_columns(self.t("Kind / status"), self.t("Record"), self.t("Linked to"), self.t("Checked"))
        else:
            table.add_columns(self.t("Typ / stan"), self.t("Treść / źródło"), self.t("Wersja / próba"))
        self._columns_kind = kind

    async def load_impact(self):
        if not self.workspace_data.get("project_id"):
            self.rows = {}
            self.render_rows([])
            self.notice(self.t("Open a project first."))
            return
        scope, since, pending = self._scope_version, self.impact_since, self.impact_pending
        params = {**self.params(), "since": since, "pending": True if pending else None}
        self.notice(self.t("Comparing the worktree with {since}…", since=_line(since, 80)))
        try:
            result = await asyncio.to_thread(self.broker.request, "GET", "/api/inspector/impact", params=params,
                                             timeout=IMPACT_TIMEOUT)
        except (OSError, ValueError, RuntimeError) as exc:
            if (scope, since, pending) == (self._scope_version, self.impact_since, self.impact_pending):
                self.rows, self.entity, self.entity_kind = {}, None, None
                self.render_rows([])
                self.notice(self.t("Could not read the impact list.") + " " + self.error_detail(exc))
            return
        if (scope, since, pending) != (self._scope_version, self.impact_since, self.impact_pending) \
                or self.view != "impact":
            return
        self.history_owner, self.entity, self.entity_kind = None, None, None
        self.next_cursor = self.source_next = None
        self.impact_report = {"since": since, "commit": result.get("commit"), "pending": pending}
        self.rows = {row["id"]: row for row in result.get("records") or []
                     if isinstance(row, dict) and isinstance(row.get("id"), str)}
        self.render_rows(list(self.rows.values()))
        self.notice(list_summary(result, since, pending, self.t))

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
            checked = (self.t(VERDICTS[verdict["verdict"]]) if isinstance(verdict, dict)
                       and verdict.get("verdict") in VERDICTS else self.t("not checked"))
            table.add_row(plain(f"{_line(row.get('kind', ''), 20)} / {_line(row.get('status', ''), 20)}"),
                          plain(title), plain(shown, limit=200), plain(checked), key=row["id"])

    def open_impact(self, identifier):
        row = self.rows.get(identifier)
        if not row:
            return
        self._entity_request += 1
        self.entity_kind, self.entity, self.history_owner, self.source_next = "impact", row, None, None
        self.show_detail(row, kind="impact", text=impact_text(row, self.impact_report, self.t))
        self.notice(self.t("Verdict saves your judgement of this entry as an audit note. It changes no record, "
                           "no status and no task."))

    def on_input_submitted(self, event):
        if event.input.id != "impact-since":
            return
        since = " ".join(event.value.split()) or DEFAULT_SINCE
        event.input.value = since
        if since != self.impact_since:
            self.impact_since = since
            self.entity = self.entity_kind = None
        self.refresh_page()

    def on_checkbox_changed(self, event):
        if event.checkbox.id == "impact-pending" and bool(event.value) != self.impact_pending:
            self.impact_pending = bool(event.value)
            self.entity = self.entity_kind = None
            self.refresh_page()

    def error_detail(self, exc):
        from .workspace_finding import ERROR_LIMIT, detail_text
        return _line(detail_text(exc, self.t, self.language), ERROR_LIMIT)

    @work(group="inspector-read", exclusive=True, exit_on_error=False)
    async def reload_impact(self, identifier):
        await self.load_impact()
        if identifier in self.rows:
            self.open_impact(identifier)

    @work(group="inspector-review", exclusive=True, exit_on_error=False)
    async def record_impact_verdict(self):
        if self.entity_kind != "impact" or not self.entity:
            self.notice(self.t("Open an entry of the list first."))
            return
        row, since, scope, params = dict(self.entity), self.impact_report.get("since"), self._scope_version, self.params()
        if not isinstance(row.get("fingerprint"), str) or not _FINGERPRINT.fullmatch(row["fingerprint"]) or not since:
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
        if scope != self._scope_version:
            self.notice(self.t("Projekt zmienił się podczas oceny. Niczego nie zapisano."))
            return
        note = str(form.get("note") or "").strip()
        if needs and not note:
            self.notice(self.t("Needs update requires a note on what to update. Nothing was saved."))
            return
        payload = {"project": params["project"], "since": since, "record_id": row["id"],
                   "fingerprint": row["fingerprint"], "verdict": choice}
        if params.get("worktree"):
            payload["worktree"] = params["worktree"]
        if note:
            payload["note"] = note
        try:
            result = await asyncio.to_thread(self.broker.request, "POST", "/api/inspector/impact/verdict",
                                             json=payload, timeout=IMPACT_TIMEOUT)
        except (OSError, ValueError, RuntimeError) as exc:
            if scope != self._scope_version:
                return
            if getattr(exc, "status_code", None) == 409:
                await self.load_impact()
                self.notice(self.t("The code or the record changed, the list was reloaded. Nothing was saved."))
            else:
                self.notice(self.t("The verdict was not saved.") + " " + self.error_detail(exc))
            return
        if scope == self._scope_version:
            await self.load_impact()
            saved = isinstance(result, dict) and result.get("already_saved")
            self.notice(self.t("This verdict was already saved." if saved else
                               "Verdict saved: {verdict}. No record, status or task was changed.",
                               verdict=self.t(VERDICTS[choice])))
