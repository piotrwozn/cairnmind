"""A person replaces a record with a new text in the inspector (#222): the form, the dialog, the one request.

Headless Textual with a fake broker: no tmux, engine, Docker or model, so the same on macOS; fast tier.
"""
from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path

import pytest
from textual.widgets import Button, Select, Static, TextArea

from cairnmind import agent_memory, record_replace, terminal_replace
from cairnmind.terminal_client import BrokerError
from cairnmind.terminal_detail import joined, record_parts
from cairnmind.terminal_i18n import CATALOG, tr
from cairnmind.terminal_i18n_messages import BROKER_MESSAGES
from cairnmind.terminal_inspector import InspectorApp
from cairnmind.terminal_ui import FormModal
from conftest import plain_words
from consent_steps import consent_drawn
from test_terminal_panels import FakeBroker, setup, settle
from test_terminal_ux080 import screen_text

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
    monkeypatch.setattr("cairnmind.terminal_ui.language_for", lambda settings: language)
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
    package = Path(agent_memory.__file__).parent
    for name in ("mcp_server.py", "mcp_schema.py", "agent_memory.py", "agent_tools.py"):
        assert "/replace" not in (package / name).read_text(encoding="utf-8"), name
    names = {tool["name"] for tool in agent_memory.MEMORY_TOOLS}
    assert not [name for name in names if "replace" in name or "supersede" in name]
    # The only POST routes of the built-in agent's memory client: a new one has to be added here on purpose.
    source = (package / "agent_memory.py").read_text(encoding="utf-8")
    assert set(re.findall(r'_request\("POST", ([^,]+),', source)) == {
        '"/sessions/prepare"', '"/sessions/agent/archive"', '"/sessions/context"', '"/sessions/ack"', '"/ask"',
        '"/plans/next"', "endpoints[name]"}
    assert 'endpoints = {"remember": "/remember", "checkpoint": "/checkpoint", "save_plan": "/plans/save"}' in source
