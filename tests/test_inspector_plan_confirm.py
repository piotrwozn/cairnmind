"""A person confirms a plan in the inspector (#214): the form, the request it sends, and what stays disabled."""
from __future__ import annotations

import pytest
from consent_steps import consent_drawn, form_ready
from test_terminal_panels import FakeBroker, settle, setup
from textual.widgets import Button, Select, Static, TextArea

from cairnmind.terminal.terminal_client import BrokerError
from cairnmind.terminal.terminal_i18n import tr
from cairnmind.terminal.terminal_inspector import InspectorApp


class PlanBroker(FakeBroker):
    def __init__(self, project):
        super().__init__(project)
        self.plan_version = 5
        self.refuse = None

    def request(self, method, path, *, params=None, json=None, timeout=20):
        if path == "/api/inspector/plans/plan-a":
            self.calls.append((method, path, params or {}, None))
            return {"item": {"id": "plan-a", "version": self.plan_version, "title": "Managed plan"}}
        if path == "/api/inspector/plans/plan-a/confirm":
            self.calls.append((method, path, params or {}, json))
            if self.refuse:
                self.plan_version += 1
                raise self.refuse
            self.plan_version += 2
            return {"plan_id": "plan-a", "plan_version": self.plan_version, "tasks": []}
        return super().request(method, path, params=params, json=json, timeout=timeout)


def plan_app(tmp_path, monkeypatch, language="pl"):
    settings, _, workspace = setup(tmp_path, monkeypatch)
    monkeypatch.setattr("cairnmind.terminal.terminal_ui.language_for", lambda settings: language)
    broker = PlanBroker({"id": "project-a", "name": "Project A", "root": str(tmp_path / "project")})
    return InspectorApp(settings, broker=broker, workspace=workspace), broker, workspace


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["pl", "en"])
async def test_the_inspector_confirms_a_plan(tmp_path, monkeypatch, language):
    app, broker, workspace = plan_app(tmp_path, monkeypatch, language)
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        app.query_one("#inspector-view", Select).value = "plans"
        await settle(app, pilot)
        app.open_selected("plan-a", "plans")
        await settle(app, pilot)
        assert not app.query_one("#entity-confirm", Button).disabled
        assert app.query_one("#entity-dispute", Button).disabled  # a plan is confirmed here, not disputed
        await pilot.click("#entity-confirm")
        await pilot.pause()
        note = str(app.screen.query_one(".cm-note", Static).render())
        assert tr("v{version} · Confirms the plan's title, goal and constraints with every active step.", language,
                  version=5) in note
        app.screen.query_one("#field-reason", TextArea).text = "Read every step."
        await form_ready(app, pilot)
        await pilot.click("#form-submit")
        await consent_drawn(app, pilot)
        assert [path for method, path, _, _ in broker.calls if method == "POST"] == []  # the form sent nothing (#332)
        await pilot.click("#confirm-yes")
        await settle(app, pilot)
        writes = [(path, body) for method, path, _, body in broker.calls if method == "POST"]
        assert writes == [("/api/inspector/plans/plan-a/confirm",
                           {"project": "project-a", "expected_plan_version": 5, "reason": "Read every step.",
                            "worktree": workspace.value["worktree"]})]
        assert tr("Plan confirmed with an audit note.", language) in str(app.query_one("#panel-notice", Static).render())
        assert app.entity["version"] == 7  # the plan was read again


@pytest.mark.asyncio
async def test_a_refused_plan_confirmation_reloads_the_plan_and_says_why(tmp_path, monkeypatch):
    app, broker, _ = plan_app(tmp_path, monkeypatch, "en")
    broker.refuse = BrokerError(403, "not from the workspace")
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        app.query_one("#inspector-view", Select).value = "plans"
        await settle(app, pilot)
        app.open_selected("plan-a", "plans")
        await settle(app, pilot)
        await pilot.click("#entity-confirm")
        await pilot.pause()
        await form_ready(app, pilot)
        await pilot.click("#form-submit")
        await consent_drawn(app, pilot)
        await pilot.click("#confirm-yes")
        await settle(app, pilot)
        shown = str(app.query_one("#panel-notice", Static).render())
        assert "The plan was not confirmed. The current version was reloaded." in shown and "403" in shown
        assert app.entity["version"] == 6  # not sent again
        assert len([1 for method, path, _, _ in broker.calls if method == "POST"]) == 1


@pytest.mark.asyncio
async def test_a_record_is_still_confirmed_and_disputed_as_before(tmp_path, monkeypatch):
    app, broker, _ = plan_app(tmp_path, monkeypatch, "en")
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        app.open_selected("record-a")
        await settle(app, pilot)
        assert not app.query_one("#entity-confirm", Button).disabled
        assert not app.query_one("#entity-dispute", Button).disabled
