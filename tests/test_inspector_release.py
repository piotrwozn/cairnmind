"""A person releases a record in the inspector (#306): the form, the request it sends, and where it is offered."""
from __future__ import annotations

import inspect

import pytest
from consent_steps import consent_drawn, form_ready
from test_terminal_panels import FakeBroker, settle, setup
from textual.widgets import Button, Select, Static, TextArea

from cairnmind.memory import memory_published_release
from cairnmind.memory.memory_published_secrets import MAX_RELEASE_REASON
from cairnmind.terminal import terminal_release
from cairnmind.terminal.person_proof import PROOF_MISSING
from cairnmind.terminal.terminal_client import BrokerError
from cairnmind.terminal.terminal_i18n import CATALOG, tr
from cairnmind.terminal.terminal_i18n_messages import BROKER_MESSAGES
from cairnmind.terminal.terminal_inspector import InspectorApp

RELEASE = "/api/inspector/records/record-a/release"
BROKER_KEYS = ("Nothing in this record needs a release", "The record is not published",
               "The reason itself holds a secret or personal data; write it without one")


class ReleaseBroker(FakeBroker):
    def __init__(self, project):
        super().__init__(project)
        self.refuse = None

    def request(self, method, path, *, params=None, json=None, timeout=20):
        if path == RELEASE:
            self.calls.append((method, path, params or {}, json))
            if self.refuse:
                raise self.refuse
            return {"record_id": "record-a", "released": ["fact/record-a.md: aws access key in text"],
                    "reason": json["reason"]}
        return super().request(method, path, params=params, json=json, timeout=timeout)


def release_app(tmp_path, monkeypatch, language="pl"):
    settings, _, workspace = setup(tmp_path, monkeypatch)
    monkeypatch.setattr("cairnmind.terminal.terminal_ui.language_for", lambda settings: language)
    broker = ReleaseBroker({"id": "project-a", "name": "Project A", "root": str(tmp_path / "project")})
    return InspectorApp(settings, broker=broker, workspace=workspace), broker, workspace


def posts(broker):
    return [(path, body) for method, path, _, body in broker.calls if method == "POST"]


async def open_release_form(app, pilot):
    await settle(app, pilot)
    app.open_selected("record-a")
    await settle(app, pilot)
    await pilot.click("#entity-release")
    await pilot.pause()


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["pl", "en"])
async def test_the_inspector_releases_a_record(tmp_path, monkeypatch, language):
    app, broker, workspace = release_app(tmp_path, monkeypatch, language)
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        app.open_selected("record-a")
        await settle(app, pilot)
        assert not app.query_one("#entity-release", Button).disabled
        await pilot.click("#entity-release")
        await pilot.pause()
        note = str(app.screen.query_one(".cm-note", Static).render())
        assert tr("Lets this record's current hits of the secrets scan be published. Your reason is published with "
                  "the record. A registered secret is never released.", language) in note
        app.screen.query_one("#field-reason", TextArea).text = "The documented example key."
        await form_ready(app, pilot)
        await pilot.click("#form-submit")
        await consent_drawn(app, pilot)
        assert posts(broker) == []  # the form sent nothing (#332)
        await pilot.click("#confirm-yes")
        await settle(app, pilot)
        assert posts(broker) == [(RELEASE, {"project": "project-a", "reason": "The documented example key.",
                                            "worktree": workspace.value["worktree"]})]
        shown = str(app.query_one("#panel-notice", Static).render())
        assert tr("Release saved with your reason. Hits of the secrets scan let through in this record: {count}.",
                  language, count=1) in shown


@pytest.mark.asyncio
async def test_release_is_offered_only_on_a_record(tmp_path, monkeypatch):
    app, broker, _ = release_app(tmp_path, monkeypatch, "en")
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        app.query_one("#inspector-view", Select).value = "plans"
        await settle(app, pilot)
        app.open_selected("plan-a", "plans")
        await settle(app, pilot)
        assert app.query_one("#entity-release", Button).disabled
        app.entity, app.entity_kind = None, "records"
        app.review("release")
        await settle(app, pilot)
        assert "Open a record first." in str(app.query_one("#panel-notice", Static).render())
        assert posts(broker) == []


def cut_labels(app, width):
    """The buttons of both rows whose label is not whole on the drawn screen (Button's line-pad takes 2 columns)."""
    lines = ["".join(segment.text for segment in strip) for strip in app.screen._compositor.render_strips()]
    cut = []
    for button in app.query("#entity-actions Button, #review-actions Button").results(Button):
        region = button.region
        if str(button.label) not in lines[region.y][max(region.x, 0):min(region.right, width)]:
            cut.append((button.id, str(button.label)))
    return cut


# The drawn screen is what counts: Textual's Button loses 2 columns to its line padding. 68 columns is the inspector pane of a
# Cockpit of 140 columns (workspace._apply_layout), 66 the narrowest one measured to be whole before.
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "language, width",
    [("en", 66), ("en", 68), ("en", 70), ("pl", 66), ("pl", 68), ("pl", 70), ("pl", 76), ("pl", 80)],
)
async def test_no_button_of_either_row_is_cut_on_the_drawn_screen(tmp_path, monkeypatch, language, width):
    app, _, _ = release_app(tmp_path, monkeypatch, language)
    async with app.run_test(size=(width, 42)) as pilot:
        await settle(app, pilot)
        assert app.query_one("#entity-replace", Button).disabled  # the list has no open record
        app.open_selected("record-a")
        await settle(app, pilot)
        assert not app.query_one("#entity-replace", Button).disabled
        assert app.query_one("#entity-replace").parent.id == "entity-actions"
        assert cut_labels(app, width) == []


@pytest.mark.asyncio
async def test_replace_is_reachable_from_the_proposed_view(tmp_path, monkeypatch):
    app, _, _ = release_app(tmp_path, monkeypatch, "en")
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        app.query_one("#inspector-view", Select).value = "proposed"
        await settle(app, pilot)
        assert app.query_one("#entity-replace", Button).disabled
        app.open_selected("record-a")  # as Enter does on a row of the Proposed list
        await settle(app, pilot)
        assert not app.query_one("#entity-replace", Button).disabled


@pytest.mark.asyncio
async def test_a_refused_release_says_why_and_is_not_sent_again(tmp_path, monkeypatch):
    app, broker, _ = release_app(tmp_path, monkeypatch, "en")
    broker.refuse = BrokerError(403, PROOF_MISSING)
    async with app.run_test(size=(95, 42)) as pilot:
        await open_release_form(app, pilot)
        app.screen.query_one("#field-reason", TextArea).text = "Example."
        await form_ready(app, pilot)
        await pilot.click("#form-submit")
        await consent_drawn(app, pilot)
        await pilot.click("#confirm-yes")
        await settle(app, pilot)
        shown = str(app.query_one("#panel-notice", Static).render())
        assert "The record was not released." in shown and "403" in shown
        assert len(posts(broker)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["", "x" * (MAX_RELEASE_REASON + 1)])
async def test_a_reason_the_form_refuses_is_not_sent(tmp_path, monkeypatch, reason):
    app, broker, _ = release_app(tmp_path, monkeypatch, "en")
    async with app.run_test(size=(95, 42)) as pilot:
        await open_release_form(app, pilot)
        app.screen.query_one("#field-reason", TextArea).text = reason
        await form_ready(app, pilot)
        await pilot.click("#form-submit")
        await pilot.pause()
        await pilot.pause()
        error = str(app.screen.query_one("#form-error", Static).render())  # the form stays open
        assert (terminal_release.TOO_LONG if reason else "Required field") in error
        assert posts(broker) == []
        await pilot.click("#form-cancel")
        await settle(app, pilot)
        assert posts(broker) == []


def test_every_release_sentence_has_polish():
    for key in terminal_release.MESSAGES:
        assert CATALOG[key] == tr(key, "pl") != key
    source = inspect.getsource(memory_published_release.release)
    for key in BROKER_KEYS:
        assert key in source
        assert BROKER_MESSAGES[key] and tr(key, "pl") != key
    assert str(MAX_RELEASE_REASON) in terminal_release.TOO_LONG
