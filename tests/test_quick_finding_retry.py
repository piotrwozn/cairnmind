"""Ctrl+B n after an unclear answer: retries, reconnection and honest messages (#14)."""
from __future__ import annotations

import httpx
import pytest
from textual.widgets import TextArea

from cairnmind.terminal_client import BrokerClient, BrokerError
from cairnmind.workspace_finding import ERROR_LIMIT, FindingApp
from cairnmind.workspace_state import WorkspaceState
from test_quick_finding import INTERRUPTED, KEY, FakeBroker, selected_workspace, stack as stack, text_of


class LostReplyBroker:
    """Delivers the first request, then loses its answer, as a timeout after the commit would."""

    def __init__(self, broker):
        self.broker, self.calls = broker, []

    def connect(self):
        self.broker.connect()

    def close(self):
        self.broker.close()

    def request(self, method, path, **kwargs):
        self.calls.append(kwargs["json"])
        result = self.broker.request(method, path, **kwargs)
        if len(self.calls) == 1:
            raise BrokerError(503, INTERRUPTED)
        return result


@pytest.mark.asyncio
async def test_saving_again_after_a_lost_answer_returns_the_first_write(stack):
    settings, store, project, root, broker, _ = stack
    state = WorkspaceState(settings)
    state.select_project(project)
    lossy = LostReplyBroker(broker)
    app = FindingApp(settings, broker=lossy, workspace=state)
    async with app.run_test(size=(120, 30)) as pilot:
        app.query_one("#finding-content", TextArea).insert("Keep one writer.")
        await pilot.press("ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert text_of(app, "#finding-error").startswith("No clear answer from the memory service.")
        await pilot.press("ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()
    assert [call["key"] for call in lossy.calls] == [app.key, app.key]
    [record] = [row for row in store.list_records(project["id"]) if row["kind"] != "checkpoint"]
    assert app.return_value == {"id": record["id"], "kind": "decision", "status": "proposed",
                                "already_saved": True}


@pytest.mark.asyncio
async def test_an_edit_after_a_lost_answer_is_refused_and_kept_in_the_form(stack):
    settings, store, project, root, broker, _ = stack
    state = WorkspaceState(settings)
    state.select_project(project)
    lossy = LostReplyBroker(broker)
    app = FindingApp(settings, broker=lossy, workspace=state)
    async with app.run_test(size=(160, 30)) as pilot:
        app.query_one("#finding-content", TextArea).insert("Keep one writr.")
        await pilot.press("ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()
        area = app.query_one("#finding-content", TextArea)
        area.text = "Keep one writer."
        await pilot.press("ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.is_running and area.text == "Keep one writer."
        assert text_of(app, "#finding-error") == (
            "Could not save. Your entries were kept. This form already saved a different text, kind or "
            "status; the first one is in memory. Open a new form to save the changed one.")
    [record] = [row for row in store.list_records(project["id"]) if row["kind"] != "checkpoint"]
    assert record["content"] == "Keep one writr."


def test_the_service_compares_a_retry_with_what_the_first_request_asked(stack):
    settings, store, project, root, _, service = stack
    headers = {"Authorization": f"Bearer {settings.token}"}
    body = {"project": project["id"], "content": "One writer.", "kind": "decision", "key": KEY}
    first = service.post("/findings", json=body, headers=headers).json()["record"]
    for change in ({"content": "One writer!"}, {"kind": "fact"}, {"status": "confirmed"}):
        refused = service.post("/findings", json={**body, **change}, headers=headers)
        assert refused.status_code == 400 and refused.json()["detail"].startswith("This form already saved")
    # A review later confirms the record; the same retry still matches what the first request asked.
    reviewed = service.post(f"/inspector/records/{first['id']}/review", headers=headers,
                            json={"project": project["id"], "action": "confirm", "expected_version": 1})
    assert reviewed.status_code == 200 and reviewed.json()["record"]["status"] == "confirmed"
    again = service.post("/findings", json=body, headers=headers)
    assert again.status_code == 200 and again.json()["already_saved"] is True
    assert service.post("/findings", json={**body, "status": "confirmed"}, headers=headers).status_code == 400


@pytest.mark.asyncio
async def test_after_an_unclear_answer_the_next_save_finds_a_restarted_broker(tmp_path, monkeypatch):
    settings, state = selected_workspace(tmp_path)
    ports, seen = iter([8766, 8767]), []
    monkeypatch.setattr("cairnmind.cockpit.cockpit_status",
                        lambda settings: {"available": True, "url": f"http://127.0.0.1:{next(ports)}"})

    def handler(request):
        seen.append((request.url.port, request.url.path))
        if request.url.path == "/_control/ticket":
            return httpx.Response(200, json={"ticket": "t"})
        if request.url.path == "/auth/exchange":
            return httpx.Response(200, json={"csrf": "c"})
        if request.url.port == 8766:
            raise httpx.ConnectError("the old broker is gone", request=request)
        return httpx.Response(200, json={"record": {"id": "record-2", "kind": "decision",
                                                    "status": "proposed"}})
    broker = BrokerClient(settings, transport=httpx.MockTransport(handler))
    app = FindingApp(settings, broker=broker, workspace=state)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.press(*"note", "ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert text_of(app, "#finding-error").startswith("No clear answer from the memory service.")
        await pilot.press("ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()
    assert app.return_value["id"] == "record-2"
    restarted = [path for port, path in seen if port == 8767]
    assert restarted == ["/_control/ticket", "/auth/exchange", "/api/findings"]


class UnreadableWorkspace:
    def read(self):
        raise ValueError("Unsupported or damaged workspace state.")


@pytest.mark.asyncio
async def test_failures_before_any_request_never_say_the_finding_may_be_saved(tmp_path):
    settings, state = selected_workspace(tmp_path)
    app = FindingApp(settings, broker=FakeBroker(), workspace=UnreadableWorkspace())
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert text_of(app, "#finding-error") == ("Could not read the workspace state. "
                                                  "Unsupported or damaged workspace state.")
    down = FakeBroker()
    down.connect = lambda: (_ for _ in ()).throw(BrokerError(503, "Lokalny broker jest niedostępny lub "
                                                              "wymaga ponownego uruchomienia."))
    local = FakeBroker(ValueError("Broker request requires a relative API path"))
    for broker in (down, local):
        app = FindingApp(settings, broker=broker, workspace=state)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.press(*"note", "ctrl+s")
            await app.workers.wait_for_complete()
            await pilot.pause()
            error = text_of(app, "#finding-error")
            assert error.startswith("Could not save. Your entries were kept.")
            assert "may have been" not in error


class SurprisingBroker(FakeBroker):
    def request(self, method, path, **kwargs):
        self.calls.append(kwargs["json"])
        if len(self.calls) == 1:
            raise TypeError("unexpected \x1b[2J" + "x" * 1000)
        return {"record": {"id": "record-3", "kind": "decision", "status": "proposed"}}


@pytest.mark.asyncio
async def test_an_unexpected_exception_never_leaves_saving_on_screen(tmp_path):
    settings, state = selected_workspace(tmp_path)
    broker = SurprisingBroker()
    app = FindingApp(settings, broker=broker, workspace=state)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.press(*"note", "ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()
        error = text_of(app, "#finding-error")
        assert error.startswith("Could not save. Your entries were kept. unexpected \\x1b[2J")
        assert "\x1b" not in error
        assert len(error) <= len("Could not save. Your entries were kept. ") + ERROR_LIMIT + 1
        assert app.is_running and not app.saving and broker.closed == 1
        await pilot.press("ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()
    assert app.return_value["id"] == "record-3" and len(broker.calls) == 2
