"""More, then Old task groups: a person reads an old task group and adopts it into a plan (#305).

Real Textual with a fake broker that records every request; no engine. The key travels in the broker client
(test_person_key_workspace); here the requests are what the panel decides to send.
"""
from __future__ import annotations

from copy import deepcopy

import pytest
from textual import _time as textual_time, events
from textual.widgets import Button, Input, Static

from cairnmind.memory import confirmation
from cairnmind.terminal import person_proof, terminal_legacy_groups
from cairnmind.memory.confirmation import GROUP_CHANGED
from cairnmind.terminal.terminal_client import BrokerError
from cairnmind.terminal.terminal_i18n import CATALOG, tr
from cairnmind.terminal.terminal_i18n_messages import BROKER_MESSAGES
from cairnmind.terminal.terminal_legacy_groups import MESSAGES, TOO_LONG
from cairnmind.terminal.terminal_panels import ConfirmModal, SidebarApp
from cairnmind.terminal.terminal_ui import ChoiceModal, FormModal, drop_unseen_key
from consent_steps import form_ready
from test_terminal_panels import settle
from test_terminal_ux080 import UIBroker, screen_text, setup_ui
from test_workspace_stage_scope import wait_until

VIEW = {"groups": [{"group": "old-group", "confirmed": 1, "tasks": [
    {"id": "t1", "version": 3, "content": "Ship the importer \x1b[31mnow", "status": "confirmed",
     "state": "todo", "confirmed_by": "before_rule"},
    {"id": "t2", "version": 1, "content": "Write the docs", "status": "proposed", "state": "in_progress",
     "confirmed_by": None}]}]}
ADOPTED = {"plan": {"id": "p1", "title": "old-group"}, "tasks": [{"id": "t1"}, {"id": "t2"}], "replayed": False,
           "audit_source_id": "s1"}


class GroupsBroker(UIBroker):
    def __init__(self, project, view, empty=False):
        super().__init__(project, empty=empty)
        self.view, self.sent, self.status, self.refuse = view, [], 200, None

    def request(self, method, path, *, params=None, json=None, timeout=20):
        if path == "/api/inspector/legacy-groups":
            return deepcopy(self.view)
        if path == "/api/inspector/legacy-groups/adopt":
            self.sent.append((method, path, deepcopy(json)))
            if self.refuse:
                raise BrokerError(self.status, self.refuse)
            return deepcopy(ADOPTED)
        return super().request(method, path, params=params, json=json, timeout=timeout)


def ui(tmp_path, monkeypatch, view=VIEW, language="en", empty=False):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch, language, empty=empty)
    project = broker.projects[0] if broker.projects else {"id": "none", "name": "None", "root": str(tmp_path)}
    broker = GroupsBroker(project, view, empty)
    return SidebarApp(settings, broker=broker, workspace=workspace), broker, workspace


def notice_of(app):
    return str(app.query_one("#panel-notice", Static).render())


async def open_form(app, pilot):
    app.legacy_groups()
    await wait_until(pilot, lambda: isinstance(app.screen, ChoiceModal))
    await pilot.press("enter")
    await wait_until(pilot, lambda: isinstance(app.screen, FormModal))


async def consent(app, pilot):
    """Continue from the form and wait for the consent dialog's draw and quiet moment (#328): before it, an
    approval is not input the dialog counts."""
    await pilot.press("ctrl+s")
    await wait_until(pilot, lambda: isinstance(app.screen, ConfirmModal))
    modal = app.screen
    assert modal.consent
    await wait_until(pilot, lambda: modal.drawn_at + modal.QUIET <= textual_time.get_time())


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_a_person_reads_the_group_and_adopts_it(tmp_path, monkeypatch, language):
    app, broker, workspace = ui(tmp_path, monkeypatch, language=language)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.legacy_groups()
        await wait_until(pilot, lambda: isinstance(app.screen, ChoiceModal))
        [(label, _)] = app.screen.choices
        assert label == ("old-group · tasks: 2 · confirmed: 1" if language == "en"
                         else "old-group · zadania: 2 · potwierdzone: 1")
        await pilot.press("enter")
        await wait_until(pilot, lambda: isinstance(app.screen, FormModal))
        await consent(app, pilot)
        shown = screen_text(app)
        assert shown.index("Ship the importer") < shown.index("Write the docs") and "\x1b" not in shown
        assert ("confirmed before the rule" if language == "en" else "potwierdzone przed regułą") in shown
        assert tr(terminal_legacy_groups.ADOPTING, language)[:40] in shown
        assert tr("Plan title: {title}", language, title="old-group") in shown
        assert broker.sent == []
        await pilot.click("#confirm-yes")
        await wait_until(pilot, lambda: broker.sent and not isinstance(app.screen, ConfirmModal))
        await settle(app, pilot)
        notice = notice_of(app)
    assert broker.sent == [("POST", "/api/inspector/legacy-groups/adopt", {
        "project": "project-keyboard", "group": "old-group", "title": "old-group",
        "task_versions": {"t1": 3, "t2": 1}, "worktree": workspace.read().get("worktree")})]
    assert ("is adopted as plan" if language == "en" else "została włączona jako plan") in notice


@pytest.mark.asyncio
async def test_the_dialog_works_at_80_by_24(tmp_path, monkeypatch):
    many = deepcopy(VIEW)
    many["groups"][0]["tasks"] += [{"id": f"x{n}", "version": 1, "content": f"Step number {n}", "status": "proposed",
                                    "state": "todo", "confirmed_by": None} for n in range(30)]
    app, broker, _ = ui(tmp_path, monkeypatch, many)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        app.legacy_groups()
        await wait_until(pilot, lambda: isinstance(app.screen, ChoiceModal))
        await pilot.press("enter")
        await form_ready(app, pilot)
        for identifier in ("#form-submit", "#form-cancel"):
            region = app.screen.query_one(identifier, Button).region
            assert region.width > 0 and region.x >= 0 and region.right <= 80 and region.bottom <= 24, identifier
        await consent(app, pilot)
        for identifier in ("#confirm-yes", "#confirm-no"):
            region = app.screen.query_one(identifier, Button).region
            assert region.width > 0 and region.x >= 0 and region.right <= 80 and region.bottom <= 24, identifier
        body = app.screen.query_one(".cm-dialog-body")
        assert body.max_scroll_y > 0
        body.scroll_end(animate=False)
        await pilot.pause()
        assert "Step number 29" in screen_text(app)
        await pilot.click("#confirm-no")
        await wait_until(pilot, lambda: not isinstance(app.screen, ConfirmModal))
    assert broker.sent == []


@pytest.mark.asyncio
async def test_the_consent_dialog_drops_a_key_made_before_it_was_drawn(tmp_path, monkeypatch):
    app, broker, _ = ui(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        await open_form(app, pilot)
        await consent(app, pilot)
        modal = app.screen
        early = events.Key("enter", None)
        early.time = modal.drawn_at - 1  # made before the draw, handled late
        assert not modal.admits(early)
        drop_unseen_key(app, early)  # BasePanel.on_key; Textual also runs SidebarApp's own on_key for every key
        assert early._no_default_action and early._stop_propagation
        await pilot.press("escape")
        await wait_until(pilot, lambda: not isinstance(app.screen, ConfirmModal))
        await settle(app, pilot)
    assert broker.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
@pytest.mark.parametrize("status, refusal", [(403, person_proof.PROOF_MISSING), (400, GROUP_CHANGED)])
async def test_a_refused_adoption_says_why_and_sends_nothing_else(tmp_path, monkeypatch, language, status, refusal):
    app, broker, _ = ui(tmp_path, monkeypatch, language=language)
    broker.status, broker.refuse = status, refusal
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        await open_form(app, pilot)
        await consent(app, pilot)
        await pilot.click("#confirm-yes")
        await wait_until(pilot, lambda: broker.sent and tr(refusal, language) in " ".join(notice_of(app).split()))
        await settle(app, pilot)
        assert not isinstance(app.screen, (FormModal, ConfirmModal))
        assert "is adopted as plan" not in notice_of(app) and "została włączona" not in notice_of(app)
    assert len(broker.sent) == 1


@pytest.mark.asyncio
async def test_an_empty_or_overlong_title_is_not_sent(tmp_path, monkeypatch):
    app, broker, _ = ui(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        await open_form(app, pilot)
        field = app.screen.query_one("#field-title", Input)
        for value, error in (("", "Required field"), ("x" * 501, TOO_LONG)):
            field.value = value
            await pilot.press("ctrl+s")
            await wait_until(pilot, lambda: error in str(app.screen.query_one("#form-error", Static).render()))
    assert broker.sent == []


@pytest.mark.asyncio
async def test_no_group_and_no_project_are_said(tmp_path, monkeypatch):
    app, broker, _ = ui(tmp_path, monkeypatch, {"groups": []})
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.legacy_groups()
        await wait_until(pilot, lambda: "No old task groups in this project." in notice_of(app))
        assert not isinstance(app.screen, ChoiceModal)
    (tmp_path / "none").mkdir()
    app, broker, _ = ui(tmp_path / "none", monkeypatch, empty=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.legacy_groups()
        await wait_until(pilot, lambda: "Select a visible project first." in notice_of(app))
    assert broker.sent == []


def test_every_adoption_sentence_has_polish():
    import inspect

    from cairnmind.memory import plans
    for key in MESSAGES:
        assert CATALOG[key] == MESSAGES[key] != key
    assert BROKER_MESSAGES[GROUP_CHANGED] and GROUP_CHANGED is confirmation.GROUP_CHANGED
    source = inspect.getsource(plans)
    for key in ("Legacy adoption requires all dependency tasks in the same group",
                "Plan dependencies must not contain a cycle"):
        assert key in source and tr(key, "pl") != key and BROKER_MESSAGES[key] == CATALOG[key]
