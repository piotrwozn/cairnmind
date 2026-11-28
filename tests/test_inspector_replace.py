"""A person replaces a record with a new text in the inspector (#222): the form, the dialog, the one request.

Headless Textual with a fake broker: no tmux, engine, Docker or model, so the same on macOS; fast tier.
"""
from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path

import pytest
from conftest import plain_words
from consent_steps import consent_drawn, form_ready
from test_terminal_panels import FakeBroker, settle, setup
from test_terminal_ux080 import screen_text
from textual.widgets import Button, Select, Static, TextArea

from cairnmind.agents import agent_memory
from cairnmind.memory import record_replace
from cairnmind.terminal import terminal_replace
from cairnmind.terminal.terminal_client import BrokerError, BrokerUnreachable
from cairnmind.terminal.terminal_detail import joined, record_parts
from cairnmind.terminal.terminal_i18n import CATALOG, tr
from cairnmind.terminal.terminal_i18n_messages import BROKER_MESSAGES
from cairnmind.terminal.terminal_inspector import InspectorApp
from cairnmind.terminal.terminal_ui import FormModal

REPLACE = "/api/inspector/records/record-a/replace"
NEW = "Validate tokens with the shared helper."
NEW_RECORD = {"id": "record-b", "kind": "decision", "key": "token-rule", "content": NEW, "status": "confirmed",
              "version": 1, "source_ids": ["note-b", "audit-b"], "supersedes": "record-a", "metadata": {}}


class ReplaceBroker(FakeBroker):
    def __init__(self, project):
        super().__init__(project)
        self.record.update(kind="decision", key="token-rule", metadata={})
        self.refuse = None

    def request(self, method, path, *, params=None, json=None, timeout=20):
        if path == REPLACE:
            self.calls.append((method, path, params or {}, deepcopy(json)))
            if self.refuse:
                raise self.refuse
            self.record.update(status="superseded", version=self.record["version"] + 1)
            return {"record": deepcopy(NEW_RECORD), "replaced": deepcopy(self.record)}
        if path == "/api/inspector/records/record-b":
            self.calls.append((method, path, params or {}, None))
            return {"record": deepcopy(NEW_RECORD)}
        return super().request(method, path, params=params, json=json, timeout=timeout)


def replace_app(tmp_path, monkeypatch, language="pl"):
    settings, _, workspace = setup(tmp_path, monkeypatch)
    monkeypatch.setattr("cairnmind.terminal.terminal_ui.language_for", lambda settings: language)
    broker = ReplaceBroker({"id": "project-a", "name": "Project A", "root": str(tmp_path / "project")})
    return InspectorApp(settings, broker=broker, workspace=workspace), broker, workspace


def posts(broker):
    return [(path, body) for method, path, _, body in broker.calls if method == "POST"]


async def open_form(app, pilot):
    await settle(app, pilot)
    app.open_selected("record-a")
    await settle(app, pilot)
    await pilot.click("#entity-replace")
    await pilot.pause()
    await pilot.pause()


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["pl", "en"])
async def test_the_inspector_replaces_a_record(tmp_path, monkeypatch, language):
    app, broker, workspace = replace_app(tmp_path, monkeypatch, language)
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        app.open_selected("record-a")
        await settle(app, pilot)
        assert not app.query_one("#entity-replace", Button).disabled
        await pilot.click("#entity-replace")
        await pilot.pause()
        await pilot.pause()
        assert tr(terminal_replace.NOTE, language) in str(app.screen.query_one(".cm-note", Static).render())
        field = app.screen.query_one("#field-content", TextArea)
        assert field.text == "Validate token safely."  # starts as the current text
        field.text = NEW
        await form_ready(app, pilot)
        await pilot.click("#form-submit")
        await consent_drawn(app, pilot)
        assert posts(broker) == []  # the form sent nothing (#332)
        shown = plain_words(screen_text(app))
        for part in ("Validate token safely.", NEW, tr(terminal_replace.EFFECT, language)[:40]):
            assert plain_words(part) in shown, part
        await pilot.click("#confirm-yes")
        await settle(app, pilot)
        assert posts(broker) == [(REPLACE, {"project": "project-a", "content": NEW, "expected_version": 3,
                                            "reason": "", "worktree": workspace.value["worktree"]})]
        assert app.entity["id"] == "record-b" and app.entity["status"] == "confirmed"
        assert tr(terminal_replace.DONE, language) in str(app.query_one("#panel-notice", Static).render())


@pytest.mark.asyncio
async def test_replace_is_not_offered_for_a_task_a_checkpoint_or_a_historical_version(tmp_path, monkeypatch):
    app, broker, _ = replace_app(tmp_path, monkeypatch, "en")
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        app.open_selected("record-a")
        await settle(app, pilot)
        record = dict(app.entity)
        for changes, refusal in (({"kind": "task"}, record_replace.NOT_REPLACEABLE),
                                 ({"kind": "checkpoint"}, record_replace.NOT_REPLACEABLE),
                                 ({"status": "superseded"}, record_replace.HISTORICAL)):
            app.entity = {**record, **changes}
            app.review("replace")
            await settle(app, pilot)
            assert not isinstance(app.screen, FormModal)
            assert refusal in str(app.query_one("#panel-notice", Static).render())
        app.entity, app.entity_kind = None, "records"
        app.review("replace")
        await settle(app, pilot)
        assert "Open a record first." in str(app.query_one("#panel-notice", Static).render())
        app.query_one("#inspector-view", Select).value = "plans"
        await settle(app, pilot)
        app.open_selected("plan-a", "plans")
        await settle(app, pilot)
        assert app.query_one("#entity-replace", Button).disabled
        assert posts(broker) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["same", "long"])
async def test_the_form_refuses_the_same_text_and_a_too_long_text(tmp_path, monkeypatch, how):
    app, broker, _ = replace_app(tmp_path, monkeypatch, "en")
    async with app.run_test(size=(95, 42)) as pilot:
        await open_form(app, pilot)
        if how == "long":
            app.screen.query_one("#field-content", TextArea).text = "x" * 16001
        await form_ready(app, pilot)
        await pilot.click("#form-submit")
        await pilot.pause()
        await pilot.pause()
        error = str(app.screen.query_one("#form-error", Static).render())  # the form stays open
        assert (terminal_replace.TOO_LONG if how == "long" else record_replace.SAME_TEXT) in error
        assert posts(broker) == []
        await pilot.click("#form-cancel")
        await settle(app, pilot)
        assert posts(broker) == []


@pytest.mark.asyncio
async def test_a_refused_replace_reloads_the_record_and_says_why(tmp_path, monkeypatch):
    app, broker, _ = replace_app(tmp_path, monkeypatch, "pl")
    broker.refuse = BrokerError(409, record_replace.CHANGED)
    async with app.run_test(size=(95, 42)) as pilot:
        await open_form(app, pilot)
        app.screen.query_one("#field-content", TextArea).text = NEW
        await form_ready(app, pilot)
        await pilot.click("#form-submit")
        await consent_drawn(app, pilot)
        reads = [call for call in broker.calls if call[1] == "/api/inspector/records/record-a"]
        await pilot.click("#confirm-yes")
        await settle(app, pilot)
        shown = plain_words(str(app.query_one("#panel-notice", Static).render()))
        assert tr(terminal_replace.FAILED, "pl") in shown and "409" in shown
        assert plain_words(tr(record_replace.CHANGED, "pl")) in shown
        assert len(posts(broker)) == 1
        assert len([call for call in broker.calls if call[1] == "/api/inspector/records/record-a"]) > len(reads)


async def send_replace(app, broker, pilot):
    await open_form(app, pilot)
    app.screen.query_one("#field-content", TextArea).text = NEW
    await form_ready(app, pilot)
    await pilot.click("#form-submit")
    await consent_drawn(app, pilot)
    await pilot.click("#confirm-yes")
    await settle(app, pilot)
    return plain_words(str(app.query_one("#panel-notice", Static).render()))


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["unreachable", "bad answer", "new record unreadable"])
async def test_an_unclear_or_saved_replace_never_says_the_record_was_not_replaced(tmp_path, monkeypatch, how):
    app, broker, _ = replace_app(tmp_path, monkeypatch, "pl")
    if how == "unreachable":
        broker.refuse = BrokerUnreachable(0, "The local broker is unreachable.")
    plain_request = broker.request

    def request(method, path, **kwargs):
        if how == "bad answer" and path == REPLACE:
            plain_request(method, path, **kwargs)
            return {"replaced": {}}
        if how == "new record unreadable" and path == "/api/inspector/records/record-b":
            raise BrokerError(503, "Memory storage is unavailable")
        return plain_request(method, path, **kwargs)
    broker.request = request
    async with app.run_test(size=(95, 42)) as pilot:
        shown = await send_replace(app, broker, pilot)
        wanted = {"unreachable": terminal_replace.UNCLEAR, "bad answer": terminal_replace.SAVED_UNREAD,
                  "new record unreadable": terminal_replace.SAVED_NOT_SHOWN}[how]
        assert plain_words(tr(wanted, "pl")) in shown
        assert plain_words(tr(terminal_replace.FAILED, "pl")) not in shown
        assert plain_words(tr(terminal_replace.DONE, "pl")) not in shown
        assert len(posts(broker)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [503, 502, 500, 200])
async def test_only_a_4xx_answer_is_a_refusal_every_other_failure_is_unclear(tmp_path, monkeypatch, status):
    # 503/502: the broker's own call failed or its answer was not JSON; 200: the answer could not be decoded.
    # The service may have committed in each of them.
    app, broker, _ = replace_app(tmp_path, monkeypatch, "pl")
    broker.refuse = BrokerError(status, "Bad answer")
    async with app.run_test(size=(95, 42)) as pilot:
        shown = await send_replace(app, broker, pilot)
        assert plain_words(tr(terminal_replace.UNCLEAR, "pl")) in shown
        assert plain_words(tr(terminal_replace.FAILED, "pl")) not in shown
        assert len(posts(broker)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["unclear", "saved but unread"])
async def test_the_notice_stays_when_the_reload_of_the_old_record_fails(tmp_path, monkeypatch, how):
    app, broker, _ = replace_app(tmp_path, monkeypatch, "pl")
    if how == "unclear":
        broker.refuse = BrokerError(503, "Memory storage is unavailable")
    plain_request = broker.request
    state = {"posted": False}

    def request(method, path, **kwargs):
        if path == REPLACE:
            state["posted"] = True
            answer = plain_request(method, path, **kwargs)
            return {"replaced": {}} if how == "saved but unread" else answer
        if state["posted"] and path == "/api/inspector/records/record-a":
            raise BrokerError(503, "Memory storage is unavailable")
        return plain_request(method, path, **kwargs)
    broker.request = request
    async with app.run_test(size=(95, 42)) as pilot:
        shown = await send_replace(app, broker, pilot)
        wanted = terminal_replace.UNCLEAR if how == "unclear" else terminal_replace.SAVED_UNREAD
        assert plain_words(tr(wanted, "pl")) in shown
        assert plain_words(tr(terminal_replace.NOT_RELOADED, "pl")) in shown  # no false "was reloaded"
        assert plain_words(tr(terminal_replace.RELOADED, "pl")) not in shown
        assert len(posts(broker)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["done", "unclear", "saved but unread"])
async def test_an_answer_after_a_project_switch_is_still_told(tmp_path, monkeypatch, how):
    app, broker, _ = replace_app(tmp_path, monkeypatch, "pl")
    if how == "unclear":
        broker.refuse = BrokerError(503, "Memory storage is unavailable")
    plain_request = broker.request

    def request(method, path, **kwargs):
        if path == REPLACE:
            app._scope_version += 1  # the project changes while the request is in flight
            answer = plain_request(method, path, **kwargs)
            return {"replaced": {}} if how == "saved but unread" else answer
        return plain_request(method, path, **kwargs)
    broker.request = request
    async with app.run_test(size=(95, 42)) as pilot:
        shown = await send_replace(app, broker, pilot)
        wanted = {"done": terminal_replace.DONE, "unclear": terminal_replace.UNCLEAR,
                  "saved but unread": terminal_replace.SAVED_UNREAD}[how]
        assert plain_words(tr(wanted, "pl")) in shown
        assert len(posts(broker)) == 1


@pytest.mark.asyncio
async def test_the_consent_dialog_itself_holds_the_current_text_and_scrolls_it(tmp_path, monkeypatch):
    old = "\n".join([f"old line {number}" for number in range(1, 40)] + ["old last line"])
    app, broker, _ = replace_app(tmp_path, monkeypatch, "en")
    broker.record["content"] = old
    async with app.run_test(size=(80, 24)) as pilot:
        await open_form(app, pilot)
        app.screen.query_one("#field-content", TextArea).text = NEW
        await form_ready(app, pilot)
        await pilot.click("#form-submit")
        modal = await consent_drawn(app, pilot)
        body = str(modal.query_one("#message-body", Static).render())  # the dialog's own text, not the screen behind
        assert old in body and NEW in body
        scroller = modal.query_one(".cm-dialog-body")
        assert scroller.max_scroll_y > 0
        assert "old line 1" in plain_words(screen_text(modal.app))  # the top of the old text is on screen
        scroller.scroll_end(animate=False)
        await pilot.pause()
        assert plain_words(terminal_replace.EFFECT[:30]) in plain_words(screen_text(app))  # the end of the body is reached
        await pilot.click("#confirm-no")
        await settle(app, pilot)
        assert posts(broker) == []


@pytest.mark.parametrize("language", ["en", "pl"])
def test_the_record_view_names_the_replacement(language):
    def translate(text, **values):
        return tr(text, language, **values)
    record = {**NEW_RECORD, "id": "record-a", "status": "superseded", "version": 4, "supersedes": None}
    shown = joined(record_parts({"record": record, "replaced_by": "record-b"}, translate))
    assert f"{translate('Replaced by')}: record-b" in shown
    assert translate("Replaced by") != "Replaced by" or language == "en"
    assert "record-b" not in joined(record_parts({"record": record}, translate))


def test_every_replace_sentence_has_polish():
    for key in terminal_replace.MESSAGES:
        assert CATALOG[key] == tr(key, "pl") != key
    for key in (record_replace.CHANGED, record_replace.HISTORICAL, record_replace.NOT_REPLACEABLE,
                record_replace.SAME_TEXT):
        assert BROKER_MESSAGES[key] and tr(key, "pl") != key
    assert str(terminal_replace.MAX_FINDING_CHARS) in terminal_replace.TOO_LONG


def test_no_mcp_tool_or_agent_tool_reaches_the_replace_route():
    package = Path(agent_memory.__file__).parents[1]
    for name in ("mcp/mcp_server.py", "mcp/mcp_schema.py", "agents/agent_memory.py", "agents/agent_tools.py"):
        assert "/replace" not in (package / name).read_text(encoding="utf-8"), name
    names = {tool["name"] for tool in agent_memory.MEMORY_TOOLS}
    assert not [name for name in names if "replace" in name or "supersede" in name]
    # The only POST routes of the built-in agent's memory client: a new one has to be added here on purpose.
    source = (package / "agents" / "agent_memory.py").read_text(encoding="utf-8")
    assert set(re.findall(r'_request\("POST", ([^,]+),', source)) == {
        '"/sessions/prepare"', '"/sessions/agent/archive"', '"/sessions/context"', '"/sessions/ack"', '"/ask"',
        '"/plans/next"', "endpoints[name]"}
    assert 'endpoints = {"remember": "/remember", "checkpoint": "/checkpoint", "save_plan": "/plans/save"}' in source
