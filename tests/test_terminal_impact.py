"""The Impact view of the workspace inspector: the list, the detail and a person's verdict (issue #8)."""
from __future__ import annotations

from copy import deepcopy

import pytest
from textual.widgets import Button, Checkbox, DataTable, Input, Static

from cairnmind.terminal_client import BrokerError
from cairnmind.terminal_i18n import tr
from cairnmind.terminal_inspector import InspectorApp
from cairnmind.terminal_ui import ChoiceModal, FormModal
from test_terminal_panels import settle
from test_terminal_ux080 import setup_ui

RULE = {"id": "rec-rule", "kind": "decision", "status": "proposed",
        "title": "Sessions last 30 minutes.\x1b[31m\nchecked: forged",
        "links": [{"via": "relates_to", "path": "src/auth.py\x1b]52;c;secret\x07\nchecked: forged", "change": "modified",
                   "link_status": "proposed", "symbol": "refresh_token", "symbol_change": "modified"}],
        "fingerprint": "a" * 64, "verdict": None}
TASK = {"id": "rec-task", "kind": "task", "status": "proposed", "task_state": "todo", "title": "Rotate refresh tokens.",
        "links": [{"via": "relates_to", "path": "src/auth.py", "change": "modified", "link_status": "observed"},
                  {"via": "supported_by", "path": "docs/login.md", "change": "modified", "snapshot": "2026-11-01",
                   "snapshot_is_current": False}],
        "evidence_state": "evidence_replaced", "quote_survives": False, "fingerprint": "b" * 64,
        "verdict": {"verdict": "still_valid", "at": "2026-11-13T10:00:00+00:00",
                    "note": "Renewal is unchanged.\nchecked: not related (forged)", "source_id": "source-v"}}
REPORT = {"project": {"id": "project-keyboard", "name": "Keyboard project"}, "worktree": "/repo", "since": "HEAD",
          "commit": "c" * 40, "changed_files": [{"path": "src/auth.py", "change": "modified", "symbols": {}}],
          "records": [RULE, TASK], "text": "", "documentation": {"documents": [{"path": "docs/login.md"}]}}
VERDICT = "/api/inspector/impact/verdict"


class Impact:
    """A broker answering the impact list and the verdict route; every call is recorded by the fake broker."""

    def __init__(self, broker, workspace, *, fail=None):
        self.broker, self.workspace, self.fail = broker, workspace, fail
        self.reads, self.posted, self.original = [], [], broker.request
        broker.request = self.request

    def request(self, method, path, *, params=None, json=None, timeout=20):
        if path == "/api/inspector/impact":
            self.broker.calls.append((method, path, deepcopy(params), deepcopy(json)))
            self.reads.append({key: value for key, value in params.items() if value is not None})
            rows = [row for row in REPORT["records"] if not (params.get("pending") and row["verdict"])]
            return deepcopy({**REPORT, "since": params["since"], "records": rows})
        if path == VERDICT:
            self.broker.calls.append((method, path, deepcopy(params), deepcopy(json)))
            self.posted.append(deepcopy(json))
            if self.fail:
                raise self.fail
            return {"record_id": json["record_id"], "already_saved": False}
        return self.original(method, path, params=params, json=json, timeout=timeout)

    def writes(self):
        return [(method, path) for method, path, *_ in self.broker.calls if method != "GET"]


def text(widget):
    return str(widget.render())


async def wait(pilot, until):
    for _ in range(150):
        await pilot.pause(.02)
        if until():
            return


async def open_impact(app, pilot, identifier="rec-rule"):
    await settle(app, pilot)
    app.set_view("impact")
    await app.load_page()
    await app.open_entity(identifier, "impact")


def script(app, monkeypatch, answers, offered):
    answers = iter(answers)

    async def scripted(screen):
        offered.append(screen)
        return next(answers)

    monkeypatch.setattr(app, "push_screen_wait", scripted)


@pytest.mark.asyncio
async def test_the_view_lists_entries_with_their_links_and_verdict_as_literal_text(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    impact = Impact(broker, workspace)
    app = InspectorApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app.query_one("#inspector-view").value = "impact"
        await wait(pilot, lambda: impact.reads)
        await settle(app, pilot)
        scope = {"project": "project-keyboard", "worktree": workspace.value["worktree"]}
        assert impact.reads == [{**scope, "since": "HEAD"}]
        assert app.query_one("#impact-bar").display and app.query_one("#impact-since", Input).value == "HEAD"
        table = app.query_one("#inspector-rows", DataTable)
        cells = {key: [str(cell) for cell in table.get_row(key)] for key in ("rec-rule", "rec-task")}
        # A title or a path is one literal line: neither a control sequence nor a line break gets through.
        assert cells["rec-rule"][1:] == ["Sessions last 30 minutes.\\x1b[31m checked: forged",
                                         "src/auth.py\\x1b]52;c;secret\\x07 checked: forged:refresh_token",
                                         "not checked"]
        assert not app.query_one("#impact-actions").display
        assert cells["rec-task"] == ["task / proposed", "Rotate refresh tokens.", "src/auth.py, docs/login.md",
                                     "still valid"]
        notice = text(app.query_one("#panel-notice", Static))
        assert "2 records worth re-checking since HEAD (cccccccccccc)" in notice and "documentation" in notice

        await app.open_entity("rec-task", "impact")
        body = text(app.query_one("#entity-body", Static)).splitlines()
        assert body[:3] == ["[task] proposed, todo", "  | Rotate refresh tokens.", "Record: rec-task"]
        assert "  - names the file src/auth.py (modified)" in body
        assert "  - rests on a snapshot from 2026-11-01 of docs/login.md (modified)" in body
        assert "Evidence: the source changed; the quoted passage is gone" in body
        checked = body.index("checked: still valid (2026-11-13)")
        # A note cannot forge a line of the view: it stays behind the bar.
        assert body[checked + 1:checked + 3] == ["  | Renewal is unchanged.", "  | checked: not related (forged)"]
        assert body[-1] == "Compared with HEAD (cccccccccccc)."
        assert app.query_one("#impact-actions").display and not app.query_one("#review-actions").display

        await app.open_entity("rec-rule", "impact")
        shown = text(app.query_one("#entity-body", Static))
        assert "\x1b" not in shown and "\x07" not in shown and "\\x1b]52;c;secret\\x07" in shown
        assert ("  - names refresh_token in src/auth.py\\x1b]52;c;secret\\x07 checked: forged (modified); its "
                "definition changed (link proposed)") in shown.splitlines()
        assert not any(line.startswith("checked: forged") for line in shown.splitlines())

        since = app.query_one("#impact-since", Input)
        since.value = "  main "
        since.focus()
        await pilot.press("enter")
        await wait(pilot, lambda: len(impact.reads) == 2)
        await settle(app, pilot)
        assert impact.reads[-1] == {**scope, "since": "main"} and since.value == "main" and app.entity is None
        app.query_one("#impact-pending", Checkbox).value = True
        await wait(pilot, lambda: len(impact.reads) == 3)
        await settle(app, pilot)
        assert impact.reads[-1] == {**scope, "since": "main", "pending": True} and list(app.rows) == ["rec-rule"]
        app.refresh_if_list()  # what a memory update does: Git is not run again for the impact list
        await settle(app, pilot)
        assert len(impact.reads) == 3
        app.set_view("records")
        assert not app.query_one("#impact-bar").display
        await app.open_entity("record-a", "records")
        assert app.query_one("#review-actions").display and not app.query_one("#impact-actions").display
    assert impact.writes() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("choice, note", [("still_valid", ""), ("needs_update", "Mention renewal."),
                                          ("not_related", "Only the name matches.")])
async def test_a_verdict_is_posted_with_the_fingerprint_shown_and_the_list_is_reloaded(tmp_path, monkeypatch,
                                                                                       choice, note):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    impact = Impact(broker, workspace)
    app = InspectorApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await open_impact(app, pilot)
        # Another commit was entered and its list is still being read: the verdict belongs to the list shown.
        app.query_one("#impact-since", Input).value = "main"
        app.impact_since = "main"
        offered = []
        script(app, monkeypatch, [choice, {"note": note}], offered)
        app.query_one("#impact-verdict", Button).press()
        await wait(pilot, lambda: impact.posted and len(impact.reads) == 2)
        await settle(app, pilot)
        notice = text(app.query_one("#panel-notice", Static))
    expected = {"project": "project-keyboard", "worktree": workspace.value["worktree"], "since": "HEAD",
                "record_id": "rec-rule", "fingerprint": "a" * 64, "verdict": choice}
    assert impact.posted == [{**expected, **({"note": note} if note else {})}]
    assert impact.writes() == [("POST", VERDICT)] and len(impact.reads) == 2
    chooser, form = offered
    assert isinstance(chooser, ChoiceModal) and [value for _, value in chooser.choices] == [
        "still_valid", "needs_update", "not_related"]
    assert isinstance(form, FormModal) and [(f.name, f.required) for f in form.fields] == [
        ("note", choice == "needs_update")]
    assert "Verdict saved" in notice and "No record, status or task was changed." in notice


@pytest.mark.asyncio
async def test_needs_update_without_a_note_and_a_project_change_send_nothing(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    impact = Impact(broker, workspace)
    app = InspectorApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await open_impact(app, pilot)
        script(app, monkeypatch, ["needs_update", {"note": "   "}], [])
        app.record_impact_verdict()
        await settle(app, pilot)
        assert "Needs update requires a note on what to update. Nothing was saved." in text(
            app.query_one("#panel-notice", Static))
        answers = iter(["still_valid", {"note": ""}])

        async def moved(screen):
            app._scope_version += 1  # what a change of project or worktree does while the form is open
            return next(answers)

        monkeypatch.setattr(app, "push_screen_wait", moved)
        app.record_impact_verdict()
        await settle(app, pilot)
        assert "Niczego nie zapisano" in text(app.query_one("#panel-notice", Static)) or \
            "Nothing was saved" in text(app.query_one("#panel-notice", Static))
    assert impact.posted == [] and impact.writes() == []


@pytest.mark.asyncio
async def test_a_stale_entry_reloads_the_list_and_nothing_is_retried(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    impact = Impact(broker, workspace, fail=BrokerError(409, "The code or the record changed since this list "
                                                             "was shown; read the current list and decide again"))
    app = InspectorApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await open_impact(app, pilot)
        script(app, monkeypatch, ["still_valid", {"note": ""}], [])
        app.query_one("#impact-verdict", Button).press()
        await wait(pilot, lambda: len(impact.reads) == 2)
        await settle(app, pilot)
        assert text(app.query_one("#panel-notice", Static)) == (
            "The code or the record changed, the list was reloaded. Nothing was saved.")
        assert app.entity is None and app.query_one("#inspector-rows").display
    assert len(impact.posted) == 1 and len(impact.reads) == 2


@pytest.mark.asyncio
async def test_another_refusal_is_shown_as_literal_text_and_keeps_the_entry(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    impact = Impact(broker, workspace, fail=BrokerError(400, "Saying that \x1b[2Ja record needs an update"))
    app = InspectorApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await open_impact(app, pilot)
        script(app, monkeypatch, ["not_related", {"note": ""}], [])
        app.query_one("#impact-verdict", Button).press()
        await wait(pilot, lambda: impact.posted)
        await settle(app, pilot)
        notice = text(app.query_one("#panel-notice", Static))
        assert notice == "The verdict was not saved. Saying that \\x1b[2Ja record needs an update"
        assert app.entity_kind == "impact" and app.entity["id"] == "rec-rule"
    assert len(impact.posted) == 1 and len(impact.reads) == 1


def test_every_label_of_the_view_has_a_polish_translation():
    import ast
    import re
    from pathlib import Path
    import cairnmind.terminal_impact as view
    source = Path(view.__file__).read_text()
    # Every literal handed to the translator, with adjacent literals joined as Python joins them.
    found = re.findall(r'(?:self\.t|translate)\(\s*((?:"(?:[^"\\]|\\.)*"\s*)+)', source)
    labels = {ast.literal_eval("(" + literal + ")") for literal in found}
    labels |= set(view.VERDICTS.values()) | set(view.SYMBOL_CHANGES.values()) | {"Impact"}
    assert len(labels) > 30
    for label in labels:
        if tr(label, "en") == label:  # an earlier Polish message is translated the other way
            assert tr(label, "pl") != label, label
