"""More, then Delete project memory: a person deletes a project's whole memory from a consent dialog (#347).

Real Textual with a fake broker that records every POST; no engine and no tmux, so every test here runs the same
on macOS. The key travels in the broker client (test_person_key_workspace); here the requests are what the panel
decides to send.
"""
from __future__ import annotations

from copy import deepcopy

import pytest
from textual import _time as textual_time
from textual.widgets import Button, Static

from cairnmind import person_proof, project_removal, terminal_project_forget as view
from cairnmind.terminal_client import BrokerError
from cairnmind.terminal_i18n import CATALOG, tr
from cairnmind.terminal_panels import ConfirmModal, SidebarApp
from cairnmind.terminal_ui import ChoiceModal
from test_terminal_panels import settle
from test_terminal_ux080 import UIBroker, screen_text, setup_ui
from test_workspace_stage_scope import wait_until

OPEN = "project-keyboard"
REMOVED_AT = "2026-11-22T10:30:00+00:00"
DELETES = {"records": 12, "plans": 2, "sources": 340, "sessions": 5, "check_runs": 1, "chunks": 901, "nodes": 77,
           "embeddings": 650}
LOG = "/data/answer-log/project-keyboard/answers.jsonl"


class ForgetBroker(UIBroker):
    def __init__(self, project, empty=False):
        super().__init__(project, empty=empty)
        self.sent, self.refuse_read, self.refuse_send, self.reply = [], None, None, {"deleted": OPEN}
        self.listed = [{"id": "old1", "name": "Old", "root": "/old", "removed_at": REMOVED_AT},
                       {"id": "other", "name": "Other", "root": "/other", "removed_at": None},
                       {"id": OPEN, "name": project["name"], "root": project["root"], "removed_at": None}]

    def request(self, method, path, *, params=None, json=None, timeout=20):
        if path == "/api/inspector/projects/deletable":
            return {"items": deepcopy(self.listed)}
        if path == "/api/inspector/projects/forget" and method == "GET":
            if self.refuse_read:
                raise BrokerError(400, self.refuse_read)
            item = next(each for each in self.listed if each["id"] == params["project"])
            return {"project": deepcopy(item), "deletes": dict(DELETES), "answer_log": LOG, "backups": "/data/backups"}
        if path == "/api/inspector/projects/forget":
            self.sent.append((method, path, deepcopy(json)))
            if self.refuse_send:
                raise BrokerError(403, self.refuse_send)
            self.projects = [each for each in self.projects if each["id"] != json["project"]]
            return deepcopy(self.reply)
        return super().request(method, path, params=params, json=json, timeout=timeout)


def ui(tmp_path, monkeypatch, language="en", empty=False):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch, language, empty=empty)
    project = broker.projects[0] if broker.projects else {"id": "none", "name": "None", "root": str(tmp_path)}
    broker = ForgetBroker(project, empty)
    return SidebarApp(settings, broker=broker, workspace=workspace), broker, workspace


def notice_of(app):
    return " ".join(str(app.query_one("#panel-notice", Static).render()).split())


def body_of(app):
    return " ".join(str(app.screen.query_one("#message-body", Static).render()).split())


async def choose(app, pilot, downs=0):
    app.forget_project()
    await wait_until(pilot, lambda: isinstance(app.screen, ChoiceModal))
    for _ in range(downs):
        await pilot.press("down")
    await pilot.press("enter")


async def consent(app, pilot):
    """Wait for the consent dialog's draw and quiet moment: before it, an approval is not input the dialog counts."""
    await wait_until(pilot, lambda: isinstance(app.screen, ConfirmModal))
    modal = app.screen
    assert modal.consent
    await wait_until(pilot, lambda: modal.drawn_at + modal.QUIET <= textual_time.get_time())


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_the_list_shows_the_open_project_first_then_registered_then_removed_ones(tmp_path, monkeypatch, language):
    app, broker, workspace = ui(tmp_path, monkeypatch, language)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.forget_project()
        await wait_until(pilot, lambda: isinstance(app.screen, ChoiceModal))
        labels = [label for label, _ in app.screen.choices]
        await pilot.press("escape")
    root = broker.listed[-1]["root"]
    removed = "(removed 2026-11-22 10:30 UTC)" if language == "en" else "(usunięty 2026-11-22 10:30 UTC)"
    assert labels == [f"Keyboard project: {root}", "Other: /other", f"Old: /old {removed}"]
    assert broker.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_the_dialog_names_the_project_the_counts_the_answer_log_and_what_stays(tmp_path, monkeypatch, language):
    app, broker, workspace = ui(tmp_path, monkeypatch, language)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        await choose(app, pilot)
        await consent(app, pilot)
        opened = body_of(app)
        heading = str(app.screen.query_one(".heading", Static).render())
        await pilot.press("escape")
        await wait_until(pilot, lambda: not isinstance(app.screen, ConfirmModal))
        await choose(app, pilot, downs=1)  # the next one is not the open project
        await consent(app, pilot)
        other = body_of(app)
        await pilot.press("escape")
        await wait_until(pilot, lambda: not isinstance(app.screen, ConfirmModal))
    assert heading == tr(view.QUESTION, language)
    t = lambda text, **values: " ".join(tr(text, language, **values).split())  # noqa: E731
    assert t(view.PROJECT_LINE, name="Keyboard project", id=OPEN) in opened
    assert t(view.FOLDER_LINE, root=broker.listed[-1]["root"]) in opened
    assert t(view.DELETES, **DELETES) in opened and t(view.ANSWER_LOG, path=LOG) in opened
    assert t(view.KEPT, backups="/data/backups") in opened and t(view.UNDO) in opened and t(view.OPEN_NOTE) in opened
    assert t(view.PROJECT_LINE, name="Other", id="other") in other and t(view.OPEN_NOTE) not in other
    assert broker.sent == []  # reading and cancelling send nothing


@pytest.mark.asyncio
async def test_the_dialog_scrolls_to_its_last_line_at_80_by_24(tmp_path, monkeypatch):
    app, broker, workspace = ui(tmp_path, monkeypatch)
    broker.listed[-1]["root"] = "/very/long/folder/" + "x" * 700
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await choose(app, pilot)
        await consent(app, pilot)
        for identifier in ("#confirm-yes", "#confirm-no"):
            region = app.screen.query_one(identifier, Button).region
            assert region.width > 0 and region.x >= 0 and region.right <= 80 and region.bottom <= 24, identifier
        body = app.screen.query_one(".cm-dialog-body")
        assert body.max_scroll_y > 0, (body.virtual_size, body.size, screen_text(app))
        body.scroll_end(animate=False)
        await pilot.pause()
        assert body.scroll_y == body.max_scroll_y and "list." in screen_text(app)  # the open project's last sentence
        await pilot.click("#confirm-no")
        await wait_until(pilot, lambda: not isinstance(app.screen, ConfirmModal))
    assert broker.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_deleting_the_open_project_returns_to_the_project_list(tmp_path, monkeypatch, language):
    app, broker, workspace = ui(tmp_path, monkeypatch, language)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        await pilot.press("enter")  # the project's own page
        await settle(app, pilot)
        assert app.page == "project"
        await choose(app, pilot)
        await consent(app, pilot)
        assert broker.sent == []
        await pilot.click("#confirm-yes")
        await wait_until(pilot, lambda: broker.sent and not isinstance(app.screen, ConfirmModal))
        await settle(app, pilot)
        done = tr(view.DONE, language, name="Keyboard project", **DELETES)
        await wait_until(pilot, lambda: " ".join(done.split()) in notice_of(app))
        assert app.page == "projects"
    assert broker.sent == [("POST", "/api/inspector/projects/forget", {"project": OPEN})]
    assert workspace.read()["project_id"] is None and workspace.read()["worktree"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_a_refusal_before_the_dialog_shows_no_dialog_and_is_in_the_panels_language(tmp_path, monkeypatch, language):
    app, broker, workspace = ui(tmp_path, monkeypatch, language)
    broker.refuse_read = project_removal.RUNS_LIVE.format(project="Keyboard project", runs="claude running")
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        await choose(app, pilot)
        shown = tr(view.NOT_DELETED, language) + " Broker 400: " + tr(project_removal.RUNS_LIVE, language).format(
            project="Keyboard project", runs="claude running")
        await wait_until(pilot, lambda: " ".join(shown.split()) in notice_of(app))
        assert not isinstance(app.screen, ConfirmModal)
    assert broker.sent == [] and workspace.read()["project_id"] == OPEN


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_a_refusal_after_consent_says_nothing_was_deleted(tmp_path, monkeypatch, language):
    app, broker, workspace = ui(tmp_path, monkeypatch, language)
    broker.refuse_send = person_proof.FORGET_REFUSED
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        await choose(app, pilot)
        await consent(app, pilot)
        await pilot.click("#confirm-yes")
        shown = tr(view.NOT_DELETED, language) + " Broker 403: " + tr(person_proof.FORGET_REFUSED, language)
        await wait_until(pilot, lambda: broker.sent and " ".join(shown.split()) in notice_of(app))
        assert " ".join(tr(view.DONE, language, name="", **DELETES).split()[:3]) not in notice_of(app)
    assert len(broker.sent) == 1 and workspace.read()["project_id"] == OPEN  # still open: nothing was deleted


@pytest.mark.asyncio
async def test_an_answer_log_left_behind_is_said(tmp_path, monkeypatch):
    app, broker, workspace = ui(tmp_path, monkeypatch)
    broker.reply = {"deleted": OPEN, "answer_log_error": "[Errno 13] Permission denied"}
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        await choose(app, pilot, downs=1)
        await consent(app, pilot)
        await pilot.click("#confirm-yes")
        left = view.LOG_LEFT.format(path=LOG, reason="[Errno 13] Permission denied")
        await wait_until(pilot, lambda: broker.sent and left in notice_of(app))


@pytest.mark.asyncio
async def test_no_project_with_memory_says_so_and_asks_nothing(tmp_path, monkeypatch):
    app, broker, workspace = ui(tmp_path, monkeypatch)
    broker.listed = []
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.forget_project()
        await wait_until(pilot, lambda: view.NONE in notice_of(app))
        assert not isinstance(app.screen, (ChoiceModal, ConfirmModal))
    assert broker.sent == []


def test_every_sentence_of_the_view_has_polish():
    for key in view.MESSAGES:
        assert CATALOG[key] == view.MESSAGES[key] != key
    for template in (project_removal.RUNS_LIVE, project_removal.RUNS_UNKNOWN, project_removal.JOBS_IN_WORK):
        assert template in project_removal.BROKER_TEMPLATES and tr(template, "pl") != template
