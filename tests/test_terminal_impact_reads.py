"""The Impact view under slow reads, failures and changes of what it asks for (issue #8, review of the view)."""
from __future__ import annotations

import asyncio
import threading

import pytest
from textual.widgets import Button, DataTable, Static

from cairnmind.terminal_client import BrokerClient, BrokerError
from cairnmind.terminal_inspector import InspectorApp
from test_terminal_impact import REPORT, VERDICT, Impact, open_impact, script, text, wait
from test_terminal_panels import settle
from test_terminal_ux080 import setup_ui


def started(tmp_path, monkeypatch, **kwargs):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    impact = Impact(broker, workspace, **kwargs)
    return InspectorApp(settings, broker=broker, workspace=workspace), impact, workspace


@pytest.mark.asyncio
async def test_a_reload_that_did_not_load_the_same_list_does_not_reopen_the_entry(tmp_path, monkeypatch):
    app, impact, _ = started(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 40)) as pilot:
        await open_impact(app, pilot)
        gate = threading.Event()
        impact.read_gates.append(gate)
        app.action_refresh()  # r on the open entry
        await wait(pilot, lambda: impact.in_flight)
        app.impact_since = "main"  # the person asked for another list meanwhile
        gate.set()
        await settle(app, pilot)
        assert app.entity is None and not app.query_one("#impact-actions").display
        app.impact_since = "HEAD"
        await app.load_page()
        await app.open_entity("rec-rule", "impact")
        app.action_refresh()  # the same list again: the entry is shown again
        await wait(pilot, lambda: len(impact.reads) == 4)
        await settle(app, pilot)
        assert app.entity_kind == "impact" and app.entity["id"] == "rec-rule"


@pytest.mark.asyncio
async def test_a_read_that_fails_after_the_person_left_the_view_leaves_the_other_view_alone(tmp_path, monkeypatch):
    app, impact, _ = started(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app.set_view("impact")
        gate = threading.Event()
        impact.read_gates.append(gate)
        impact.read_fail = BrokerError(400, "Git does not know the commit")
        app.run_worker(app.load_impact(), group="test-read")
        await wait(pilot, lambda: impact.in_flight)
        app.set_view("records")
        await app.load_page()
        await app.open_entity("record-a", "records")
        gate.set()
        await settle(app, pilot)
        assert app.entity_kind == "records" and app.entity["id"] == "record-a"
        assert "impact" not in text(app.query_one("#panel-notice", Static))


@pytest.mark.asyncio
@pytest.mark.parametrize("error, notice, reloaded", [
    (BrokerError(503, "Połączenie z lokalnym brokerem zostało przerwane."),
     "No clear answer from the memory service. The verdict may have been saved; the list was reloaded and shows it "
     "if so. Saving the same verdict again is safe and stores nothing twice. "
     "The connection to the local broker was interrupted.", True),
    (BrokerError(404, "This record is not in the impact list of that project and commit"),
     "This record is no longer in the list, the list was reloaded. Nothing was saved.", True),
    (BrokerError(400, "The note must be text of at most 4000 characters"),
     "The verdict was not saved. The note must be text of at most 4000 characters", False),
])
async def test_an_unclear_or_stale_answer_reloads_the_list(tmp_path, monkeypatch, error, notice, reloaded):
    app, impact, _ = started(tmp_path, monkeypatch, fail=error)
    async with app.run_test(size=(120, 40)) as pilot:
        await open_impact(app, pilot)
        script(app, monkeypatch, ["still_valid", {"note": ""}], [])
        app.query_one("#impact-verdict", Button).press()
        await wait(pilot, lambda: impact.posted)
        await settle(app, pilot)
        assert text(app.query_one("#panel-notice", Static)) == notice
        assert len(impact.reads) == (2 if reloaded else 1) and (app.entity is None) == reloaded
    assert len(impact.posted) == 1


@pytest.mark.asyncio
async def test_a_second_press_while_a_verdict_is_sent_neither_cancels_nor_repeats_it(tmp_path, monkeypatch):
    app, impact, _ = started(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 40)) as pilot:
        await open_impact(app, pilot)
        gate = threading.Event()
        impact.post_gates.append(gate)
        offered = []
        script(app, monkeypatch, ["still_valid", {"note": ""}, "not_related", {"note": ""}], offered)
        app.query_one("#impact-verdict", Button).press()
        await wait(pilot, lambda: impact.posted)
        app.query_one("#impact-verdict", Button).press()
        await pilot.pause(.1)
        assert text(app.query_one("#panel-notice", Static)) == "A verdict is being saved. Wait for its answer."
        app.review("confirm")  # another review action does not cancel it either
        gate.set()
        await wait(pilot, lambda: len(impact.reads) == 2)
        await settle(app, pilot)
        assert text(app.query_one("#panel-notice", Static)).startswith("Verdict saved: still valid.")
    assert len(offered) == 2 and [payload["verdict"] for payload in impact.posted] == ["still_valid"]


@pytest.mark.asyncio
async def test_impact_requests_use_a_client_of_their_own_and_never_the_shared_one(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    impact, separate = Impact(broker, workspace), []

    def own_client(self, method, path, *, params=None, json=None, timeout=20):
        separate.append((method, path, timeout))
        return impact.request(method, path, params=params, json=json, timeout=timeout)

    class Shared(BrokerClient):
        def ensure(self):
            pass

        def request(self, method, path, *, params=None, json=None, timeout=20):
            assert "impact" not in path, "the shared client was used for " + path
            return broker.request(method, path, params=params, json=json, timeout=timeout)

        async def events(self, **kwargs):
            if False:
                yield {}

    monkeypatch.setattr(BrokerClient, "request", own_client)
    monkeypatch.setattr(BrokerClient, "connect", lambda self: None)
    app = InspectorApp(settings, broker=Shared(settings), workspace=workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await open_impact(app, pilot)
        script(app, monkeypatch, ["still_valid", {"note": ""}], [])
        app.query_one("#impact-verdict", Button).press()
        await wait(pilot, lambda: len(separate) == 3)
        await settle(app, pilot)
    assert separate == [("GET", "/api/inspector/impact", 120), ("POST", VERDICT, 120),
                        ("GET", "/api/inspector/impact", 120)]


@pytest.mark.asyncio
async def test_repeated_reads_never_stack_and_the_last_one_asked_for_wins(tmp_path, monkeypatch):
    app, impact, workspace = started(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app.set_view("impact")
        gate = threading.Event()
        impact.read_gates.append(gate)
        for since in ("a", "b", "c"):
            app.impact_since = since
            app.refresh_page()
            await pilot.pause(.05)
        assert len(impact.reads) == 1
        # A change of worktree polled meanwhile does not wait for Git inside the workspace poll.
        moved = {**workspace.read(), "worktree": workspace.value["worktree"] + "-other", "revision": 99}
        app.workspace_data = moved
        await asyncio.wait_for(app.workspace_changed(moved), 1)
        gate.set()
        await wait(pilot, lambda: len(impact.reads) == 2)
        await settle(app, pilot)
        assert impact.most_in_flight == 1 and len(impact.reads) == 2
        assert impact.reads[-1]["since"] == "c" and impact.reads[-1]["worktree"].endswith("-other")
        assert app.impact_shown[1:3] == (moved["worktree"], "c") and list(app.rows) == ["rec-rule", "rec-task"]


@pytest.mark.asyncio
async def test_an_answer_without_the_impact_shape_is_not_shown_as_an_empty_comparison(tmp_path, monkeypatch):
    app, impact, _ = started(tmp_path, monkeypatch)
    impact.answer = {"items": [], "next_cursor": None}  # what the preview answers to any inspector read
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app.set_view("impact")
        await app.load_page()
        assert text(app.query_one("#panel-notice", Static)) == "The impact list is not available here."
        assert app.rows == {} and app.query_one("#inspector-rows", DataTable).row_count == 0
        impact.answer = {**REPORT, "changed_files": [], "records": []}
        await app.load_page()
        assert text(app.query_one("#panel-notice", Static)).startswith("Nothing in this worktree differs from HEAD")
