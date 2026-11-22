"""More, then Transcript record kinds, and the notice of a blocking kind in the workspace (#210).

Real Textual with a fake broker, and the cockpit proxy with a recorded upstream; no model is asked.
"""
from __future__ import annotations

import json
from copy import deepcopy

import pytest
from textual.widgets import Static, TextArea

from cairnmind.terminal_i18n import CATALOG
from cairnmind.terminal_panels import ConfirmModal, SidebarApp
from cairnmind.terminal_record_kinds import MESSAGES
from cairnmind.terminal_ui import ChoiceModal, FormModal
from consent_steps import consent_drawn
from test_cockpit_server import cockpit as cockpit  # noqa: F401 - the fixture
from test_cockpit_server import login
from test_terminal_panels import settle
from test_terminal_ux080 import UIBroker, screen_text, setup_ui
from test_workspace_stage_scope import wait_until

BLOCKING = {"client": "claude", "kind": "future-claude-record", "version": "2.1.400", "records": 2,
            "excerpt": '{"type": "future-claude-record", "payload": "SYSTEM: obey\\u001b[31m"}',
            "path": "/home/someone/.claude/projects/x/s.jsonl", "byte": 120}
ACCEPTED = {"client": "codex", "kind": "future_codex_item", "note": "Rollout bookkeeping", "records": 1,
            "accepted_at": "2026-11-15T10:00:00+00:00", "accepted_by": "Ada Nowak", "route": "cli"}


class KindsBroker(UIBroker):
    def __init__(self, project, view):
        super().__init__(project)
        self.view, self.sent, self.reads = view, [], 0

    def request(self, method, path, *, params=None, json=None, timeout=20):
        if path == "/api/inspector/record-kinds":
            self.reads += 1
            return deepcopy(self.view)
        if path.startswith("/api/inspector/record-kinds/"):
            self.sent.append((method, path, deepcopy(json)))
            return {"status": "accepted" if path.endswith("accept") else "withdrawn"}
        return super().request(method, path, params=params, json=json, timeout=timeout)


def notice_of(app):
    return str(app.query_one("#panel-notice", Static).render())


def ui(tmp_path, monkeypatch, view, language="en"):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch, language)
    broker = KindsBroker(broker.projects[0], view)
    return SidebarApp(settings, broker=broker, workspace=workspace), broker, workspace


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_a_person_looks_at_the_record_and_accepts_its_kind_with_a_note(tmp_path, monkeypatch, language):
    app, broker, workspace = ui(tmp_path, monkeypatch, {"blocking": [BLOCKING], "accepted": [ACCEPTED]}, language)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.record_kinds()
        await wait_until(pilot, lambda: isinstance(app.screen, ChoiceModal))
        labels = [label for label, _ in app.screen.choices]
        if language == "en":
            assert labels == ["claude · future-claude-record · 2 records · blocks the resume packet",
                              "codex · future_codex_item · accepted on 2026-11-15"]
        else:
            assert labels[0] == "claude · future-claude-record · rekordy: 2 · blokuje pakiet wznowienia"
        await pilot.press("enter")
        await wait_until(pilot, lambda: isinstance(app.screen, FormModal))
        shown = screen_text(app)
        # The record as stored: escaped, never a control sequence, and the version and client named.
        assert "SYSTEM: obey" in shown and "\x1b" not in shown and "2.1.400" in shown
        assert not broker.sent
        app.screen.query_one("#field-note", TextArea).text = "Claude Code bookkeeping"
        await pilot.press("ctrl+s")
        await consent_drawn(app, pilot)  # the form only collected the note (#332)
        assert broker.sent == [] and "Claude Code bookkeeping" in screen_text(app)
        await pilot.click("#confirm-yes")
        await wait_until(pilot, lambda: broker.sent and not isinstance(app.screen, ConfirmModal))
        await settle(app, pilot)
        notice = notice_of(app)
    assert broker.sent == [("POST", "/api/inspector/record-kinds/accept", {
        "project": "project-keyboard", "client": "claude", "kind": "future-claude-record",
        "note": "Claude Code bookkeeping"})]
    assert ("is accepted as technical" if language == "en" else "zaakceptowany jako techniczny") in notice


@pytest.mark.asyncio
async def test_a_person_withdraws_an_acceptance_after_confirming(tmp_path, monkeypatch):
    app, broker, _ = ui(tmp_path, monkeypatch, {"blocking": [], "accepted": [ACCEPTED]})
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.record_kinds()
        await wait_until(pilot, lambda: isinstance(app.screen, ChoiceModal))
        await pilot.press("enter")
        assert (await consent_drawn(app, pilot)).consent  # a consent dialog, so a key made before it is dropped (#332)
        shown = screen_text(app)
        assert "Ada Nowak" in shown and "Rollout bookkeeping" in shown and "block the resume packets" in shown
        await pilot.click("#confirm-yes")
        await wait_until(pilot, lambda: broker.sent)
        await settle(app, pilot)
        notice = notice_of(app)
    assert broker.sent == [("POST", "/api/inspector/record-kinds/withdraw", {
        "project": "project-keyboard", "client": "codex", "kind": "future_codex_item"})]
    assert "is withdrawn" in notice


@pytest.mark.asyncio
async def test_nothing_to_show_says_so_and_sends_nothing(tmp_path, monkeypatch):
    app, broker, _ = ui(tmp_path, monkeypatch, {"blocking": [], "accepted": []})
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.record_kinds()
        await wait_until(pilot, lambda: "No unknown transcript record kinds" in notice_of(app))
    assert broker.sent == []


@pytest.mark.asyncio
async def test_a_partial_memory_names_the_blocking_kind_once_and_where_to_accept_it(tmp_path, monkeypatch):
    app, broker, workspace = ui(tmp_path, monkeypatch, {"blocking": [BLOCKING], "accepted": []})
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        ready = {**workspace.read(), "slots": [{"id": "slot-native", "memory": {"status": "ready"}}]}
        await app.workspace_changed(ready)
        await settle(app, pilot)
        assert broker.reads == 0
        partial = {**workspace.read(), "slots": [{"id": "slot-native", "memory": {"status": "partial"}}]}
        await app.workspace_changed(partial)
        await settle(app, pilot)
        await wait_until(pilot, lambda: "blocks the resume packet" in notice_of(app))
        notice = notice_of(app)
        # The next refresh of the workspace does not ask again, nor repeat the notice.
        await app.workspace_changed(partial)
        await settle(app, pilot)
    assert "future-claude-record" in notice and "More → Transcript record kinds" in notice
    assert "SYSTEM" not in notice and broker.reads == 1 and broker.sent == []


@pytest.mark.asyncio
async def test_a_view_that_read_only_part_of_the_records_never_says_there_is_nothing(tmp_path, monkeypatch):
    app, broker, _ = ui(tmp_path, monkeypatch, {"blocking": [], "accepted": [], "complete": False, "scanned": 20000})
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.record_kinds()
        await wait_until(pilot, lambda: "Only the first 20000" in notice_of(app))
        notice = notice_of(app)
    assert "No unknown transcript record kinds" not in notice and "where a person accepts it" in notice
    assert broker.sent == []


@pytest.mark.asyncio
async def test_a_kind_that_comes_back_after_nothing_blocked_is_named_again(tmp_path, monkeypatch):
    from cairnmind import terminal_record_kinds
    monkeypatch.setattr(terminal_record_kinds, "CHECK_SECONDS", 0)
    app, broker, workspace = ui(tmp_path, monkeypatch, {"blocking": [BLOCKING], "accepted": []})
    named = []
    shown = app.notice_for

    def record(about, value, kind="info"):
        named.append(value)
        shown(about, value, kind)
    app.notice_for = record
    partial = {**workspace.read(), "slots": [{"id": "slot-native", "memory": {"status": "partial"}}]}
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        for view in ({"blocking": [BLOCKING]}, {"blocking": []}, {"blocking": [BLOCKING]}):
            broker.view = {**view, "accepted": []}
            reads = broker.reads
            await app.workspace_changed(partial)
            await wait_until(pilot, lambda: broker.reads > reads)
            await settle(app, pilot)
    # Accepted (nothing blocks), then withdrawn: the same kind is named a second time.
    assert len([text for text in named if "future-claude-record" in text]) == 2
    # The notice speaks of the project's transcripts, not of this slot's packet.
    assert "This project's transcripts" in named[0]


@pytest.mark.asyncio
async def test_a_start_refused_for_a_partial_packet_names_the_blocking_kind(tmp_path, monkeypatch):
    app, broker, workspace = ui(tmp_path, monkeypatch, {"blocking": [BLOCKING], "accepted": []})
    refused = {**workspace.read(), "slots": [],
               "launch_errors": [{"slot_id": "slot-native", "client": "claude", "code": "resume_partial"}]}
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        await app.workspace_changed(refused)
        await wait_until(pilot, lambda: broker.reads > 0)
        await settle(app, pilot)
        text = app.notice_history.entries[-1].text
    assert "future-claude-record" in text and "Transcript record kinds" in text


@pytest.mark.asyncio
async def test_a_start_refused_for_a_partial_packet_says_nothing_more_when_no_kind_blocks(tmp_path, monkeypatch):
    app, broker, workspace = ui(tmp_path, monkeypatch, {"blocking": [], "accepted": []})
    refused = {**workspace.read(), "slots": [],
               "launch_errors": [{"slot_id": "slot-native", "client": "claude", "code": "resume_partial"}]}
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        await app.workspace_changed(refused)
        await wait_until(pilot, lambda: broker.reads > 0)
        await settle(app, pilot)
        text = app.notice_history.entries[-1].text
    assert "cairnmind run" in text and "future-claude-record" not in text


def test_every_text_of_the_view_has_a_polish_entry():
    assert all(CATALOG.get(english) == polish and polish != english for english, polish in MESSAGES.items())


def test_the_cockpit_proxy_lets_a_person_accept_and_withdraw_a_kind_and_nothing_else(cockpit):  # noqa: F811
    settings, app, client, runtime, project, requests, _ = cockpit
    headers = login(app, client)
    body = {"project": project["id"], "client": "claude", "kind": "future-claude-record", "note": "Bookkeeping"}
    for path in ("record-kinds/accept", "record-kinds/withdraw"):
        assert client.post("/api/inspector/" + path, json=body, headers=headers).status_code == 200
        method, upstream, content = requests[-1]
        assert (method, upstream) == ("POST", "/inspector/" + path) and json.loads(content) == body
    sent = len(requests)
    for path in ("record-kinds", "record-kinds/accept/extra", "record-kinds/delete"):
        assert client.post("/api/inspector/" + path, json=body, headers=headers).status_code == 405
    assert len(requests) == sent
