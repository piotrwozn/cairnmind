"""The Proposed view of the workspace inspector (#189): a read-only list, and the ordinary review from it."""
from __future__ import annotations

from copy import deepcopy

import pytest
from consent_steps import consent_drawn, form_ready
from test_terminal_panels import settle, setup
from textual.widgets import DataTable, Input, Select, Static, TextArea

from cairnmind.memory.stale_proposed import MAX_DAYS
from cairnmind.terminal.terminal_i18n import tr
from cairnmind.terminal.terminal_inspector import _VIEWS, InspectorApp
from cairnmind.terminal.terminal_proposed import (
    BASIS_WORDS,
    DAYS_ERROR,
    SUMMARY_ONE_DAY,
    TEXT_WIDTH,
    days_error,
    list_summary,
)
from cairnmind.terminal.terminal_ui import FormModal

ITEMS = [
    {"id": "record-a", "kind": "decision", "version": 3, "updated_at": "2026-08-01T10:00:00+00:00", "age_days": 90,
     "sources": 2, "evidence_latest": "2026-06-03T10:00:00+00:00", "evidence_basis": "stated",
     "content": "Sessions last 30 minutes.\x1b[31m\nchecked: forged", "content_truncated": False},
    {"id": "record-b", "kind": "fact", "version": 1, "updated_at": "2026-09-15T10:00:00+00:00", "age_days": 45,
     "sources": 1, "evidence_latest": "2026-09-14T08:00:00+00:00", "evidence_basis": "observed",
     "content": "Redis holds the cache.", "content_truncated": False},
]
PROPOSED = "/api/inspector/proposed"


class Proposed:
    """Answers the list as the service does: a record leaves it once it is no longer proposed."""

    def __init__(self, broker):
        self.broker, self.original, self.reads = broker, broker.request, []
        broker.record.update(kind="decision", status="proposed", metadata={})
        broker.request = self.request

    def request(self, method, path, *, params=None, json=None, timeout=20):
        if path == PROPOSED:
            self.broker.calls.append((method, path, deepcopy(params), deepcopy(json)))
            self.reads.append(dict(params))
            items = [item for item in ITEMS if item["id"] != "record-a" or self.broker.record["status"] == "proposed"]
            return deepcopy({"items": items, "total": len(items), "days": params["days"], "truncated": False})
        return self.original(method, path, params=params, json=json, timeout=timeout)


def writes(broker):
    return [(method, path, body) for method, path, _, body in broker.calls if method != "GET"]


def cells(table, key):
    return [str(cell) for cell in table.get_row(key)]


async def open_view(app, pilot):
    await settle(app, pilot)
    app.query_one("#inspector-view", Select).value = "proposed"
    await settle(app, pilot)


def test_the_view_is_the_last_entry_and_the_others_keep_their_order():
    assert _VIEWS == [("Records", "records"), ("Sources", "sources"), ("Plans", "plans"), ("Tasks", "tasks"),
                      ("Checks", "checks"), ("Queue", "jobs"), ("Conflicts", "conflicts"), ("Impact", "impact"),
                      ("Unassigned", "inbox"), ("Snapshot", "memory"), ("Proposed", "proposed")]


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_the_list_shows_age_kind_id_sources_and_evidence_time_as_literal_text(tmp_path, monkeypatch, language):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    monkeypatch.setattr("cairnmind.terminal.terminal_ui.language_for", lambda settings: language)
    proposed = Proposed(broker)
    app = InspectorApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(140, 40)) as pilot:
        await open_view(app, pilot)
        assert proposed.reads == [{"project": "project-a", "worktree": workspace.value["worktree"], "days": 30}]
        table = app.query_one("#inspector-rows", DataTable)
        headings = [str(column.label) for column in table.ordered_columns]
        assert headings == [tr(word, language) for word in
                            ("Days untouched", "Kind", "Id", "Sources", "Newest evidence", "Text")]
        assert list(table.rows) and [key.value for key in table.rows] == ["record-a", "record-b"]
        # A stored text is one literal line: neither a control sequence nor a line break gets through.
        assert cells(table, "record-a") == ["90", tr("decision", language), "record-a", "2",
                                            "2026-06-03 (" + tr("stated time", language) + ")",
                                            "Sessions last 30 minutes.\\x1b[31m"]
        assert cells(table, "record-b")[4] == "2026-09-14 (" + tr("at the latest", language) + ")"
        notice = str(app.query_one("#panel-notice", Static).render())
        assert notice == " ".join([
            tr("Proposed findings not updated for {days} days or more: {count} · oldest first · read only.",
               language, days=30, count=2), tr("Open one to confirm or dispute it.", language)])
        assert writes(broker) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [80, 95, 120])
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_a_narrow_pane_keeps_the_text_on_screen_and_crops_the_id_first(tmp_path, monkeypatch, language, width):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    monkeypatch.setattr("cairnmind.terminal.terminal_ui.language_for", lambda settings: language)
    record = {**ITEMS[1], "id": "3f2b6a1e-9c4d-4e8f-a1b2-c3d4e5f60718"}  # record ids are 36-character UUIDs
    monkeypatch.setitem(globals(), "ITEMS", [record])
    Proposed(broker)
    app = InspectorApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(width, 40)) as pilot:
        await open_view(app, pilot)
        table = app.query_one("#inspector-rows", DataTable)
        columns = table.ordered_columns
        assert sum(column.get_render_width(table) for column in columns) <= table.size.width
        assert columns[5].width >= TEXT_WIDTH  # the text a person judges the finding by
        assert columns[2].width < 36 and columns[1].width >= len(tr("fact", language))  # the id gave way, not the kind
        assert cells(table, record["id"])[2] == record["id"]  # cropped on screen only; Enter opens the whole record


@pytest.mark.parametrize("language", ["en", "pl"])
def test_the_summary_says_one_day_in_the_singular(language):
    one = list_summary({"items": [], "total": 0}, 1, lambda text, **values: tr(text, language, **values))
    assert one == tr(SUMMARY_ONE_DAY, language, count=0)
    assert ("1 day or" if language == "en" else "1 dnia:") in one and "1 days" not in one and "1 dni:" not in one


@pytest.mark.asyncio
async def test_an_entry_is_reviewed_through_the_inspector_review_and_then_leaves_the_list(tmp_path, monkeypatch):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    proposed = Proposed(broker)
    app = InspectorApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 42)) as pilot:
        await open_view(app, pilot)
        app.query_one("#inspector-rows", DataTable).focus()
        await pilot.press("enter")
        await settle(app, pilot)
        # The record itself, read from its detail route, with the review buttons of the Records view.
        assert ("GET", "/api/inspector/records/record-a") in [(m, p) for m, p, *_ in broker.calls]
        assert app.entity_kind == "records" and app.query_one("#review-actions").display
        assert writes(broker) == []
        await pilot.click("#entity-confirm")
        await pilot.pause()
        app.screen.query_one("#field-reason", TextArea).text = "Read the transcript."
        await form_ready(app, pilot)
        await pilot.click("#form-submit")
        await consent_drawn(app, pilot)
        assert writes(broker) == []
        await pilot.click("#confirm-yes")
        await settle(app, pilot)
        assert writes(broker) == [("POST", "/api/inspector/records/record-a/review",
                                   {"project": "project-a", "action": "confirm", "expected_version": 3,
                                    "reason": "Read the transcript.", "worktree": workspace.value["worktree"]})]
        await pilot.click("#entity-back")
        await settle(app, pilot)
        assert len(proposed.reads) == 2
        assert [key.value for key in app.query_one("#inspector-rows", DataTable).rows] == ["record-b"]
        assert len(writes(broker)) == 1


@pytest.mark.asyncio
async def test_filters_ask_for_the_days_and_refuse_anything_else(tmp_path, monkeypatch):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    monkeypatch.setattr("cairnmind.terminal.terminal_ui.language_for", lambda settings: "en")
    proposed = Proposed(broker)
    app = InspectorApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 42)) as pilot:
        await open_view(app, pilot)
        await pilot.click("#entity-filters")
        await pilot.pause()
        assert isinstance(app.screen, FormModal)
        field = app.screen.query_one("#field-days", Input)
        assert field.value == "30"
        field.value = "-3"
        await form_ready(app, pilot)
        await pilot.click("#form-submit")
        for _ in range(20):  # not settle: the filter's worker waits for this form until it closes
            await pilot.pause(.02)
        assert isinstance(app.screen, FormModal)
        assert str(app.screen.query_one("#form-error", Static).render()) == DAYS_ERROR
        field.value = "0"
        await form_ready(app, pilot)
        await pilot.click("#form-submit")
        await settle(app, pilot)
        assert [read["days"] for read in proposed.reads] == [30, 0] and writes(broker) == []


@pytest.mark.parametrize(("value", "error"), [("0", None), (str(MAX_DAYS), None), ("3651", DAYS_ERROR),
                                              ("", DAYS_ERROR), ("1.5", DAYS_ERROR), ("٣", DAYS_ERROR)])
def test_the_days_field_takes_a_whole_number_in_range(value, error):
    assert days_error(value) == error


def test_every_word_the_view_shows_is_translated():
    words = [*BASIS_WORDS.values(), "Proposed", "Days untouched", "Kind", "Newest evidence", DAYS_ERROR,
             "Proposed findings not updated for {days} days or more: {count} · oldest first · read only.",
             SUMMARY_ONE_DAY, "Only the {shown} oldest are shown.", "Open one to confirm or dispute it.",
             "Not updated for at least this many days"]
    assert [word for word in words if tr(word, "pl") == word] == []
