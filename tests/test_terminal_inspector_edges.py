"""Edge-case tests for terminal_inspector (InspectorApp actions, revisions, memory, journal)."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest
from textual.widgets import Button, DataTable, Input, Select, Static

from cairnmind.config import Settings
from cairnmind.terminal.terminal_client import BrokerError
from cairnmind.terminal.terminal_inspector import InspectorApp
from test_terminal_panels import FakeBroker, FakeWorkspace, settle


class InspectorBroker(FakeBroker):
    def __init__(self, project):
        super().__init__(project)
        self.pending_events = []
        self.fail_events = False
        self.fail_once = {}
        self.conflicts = [{
            "id": "conf-1",
            "can_supersede": True,
            "candidate": {"id": "rec-a", "kind": "fact", "content": "Use PG", "version": 2, "status": "proposed"},
            "existing": {"id": "rec-b", "kind": "fact", "content": "Use MySQL", "version": 1, "status": "proposed"},
        }]
        self.revisions_pages = {
            None: {"items": [{"id": "record-a:2", "version": 2, "snapshot": {"content": "v2"}}], "next_cursor": "rev-p2"},
            "rev-p2": {"items": [{"id": "record-a:1", "version": 1, "snapshot": {"content": "v1"}}], "next_cursor": None},
        }
        self.records_next_cursor = "page-2"

    async def events(self, **kwargs):
        if self.fail_events:
            raise BrokerError(503, "stream dropped")
        while self.pending_events:
            yield self.pending_events.pop(0)

    def request(self, method, path, *, params=None, json=None, timeout=20):
        params = params or {}
        if (method, path) in self.fail_once:
            exc = self.fail_once.pop((method, path))
            raise exc
        if path == "/api/inspector/records":
            self.calls.append((method, path, deepcopy(params), deepcopy(json)))
            if params.get("cursor") == "page-2":
                return {"items": [{**deepcopy(self.record), "id": "record-b", "content": "Second record"}], "next_cursor": None, "total": 2}
            return {"items": [deepcopy(self.record)], "next_cursor": self.records_next_cursor, "total": 2}
        if path == "/api/inspector/conflicts":
            self.calls.append((method, path, deepcopy(params), deepcopy(json)))
            return {"items": deepcopy(self.conflicts), "next_cursor": None}
        if path == "/api/inspector/conflicts/resolve":
            self.calls.append((method, path, deepcopy(params), deepcopy(json)))
            self.conflicts = []
            return {"ok": True}
        if path == "/api/inspector/inbox":
            self.calls.append((method, path, deepcopy(params), deepcopy(json)))
            return {"items": [{"id": "inbox-1", "kind": "transcript", "uri": "inbox://1"}], "next_cursor": None}
        if path == "/api/inspector/inbox/inbox-1":
            self.calls.append((method, path, deepcopy(params), deepcopy(json)))
            return {"source": {"id": "inbox-1", "uri": "inbox://1"}, "text": "raw inbox body", "next_offset": None}
        if path.endswith("/revisions"):
            self.calls.append((method, path, deepcopy(params), deepcopy(json)))
            return deepcopy(self.revisions_pages.get(params.get("cursor"), {"items": [], "next_cursor": None}))
        if path == "/api/memory/preview":
            self.calls.append((method, path, deepcopy(params), deepcopy(json)))
            return {"counts": {"records": 1}, "path": "/tmp/snap"}
        if path == "/api/memory/snapshot":
            self.calls.append((method, path, deepcopy(params), deepcopy(json)))
            return {"saved": True, "path": "/tmp/snap"}
        if path == "/api/memory/settings" and method == "PATCH":
            self.calls.append((method, path, deepcopy(params), deepcopy(json)))
            return {"auto": {"enabled": json["enabled"], "interval_minutes": json["interval_minutes"]}}
        return super().request(method, path, params=params, json=json, timeout=timeout)


def _setup(tmp_path, monkeypatch, *, project=True):
    proj = {"id": "project-a", "name": "Project A", "root": str(tmp_path / "project")}
    broker = InspectorBroker(proj)
    workspace = FakeWorkspace(proj if project else None)
    settings = Settings(data_dir=tmp_path / "data")
    monkeypatch.setattr("cairnmind.terminal.workspace.refresh_workspace", lambda *args, **kwargs: workspace.read())
    monkeypatch.setattr("cairnmind.terminal.terminal_ui.language_for", lambda s: "en")
    return settings, broker, workspace


@pytest.mark.asyncio
async def test_inspector_navigation_filters_revisions_sources_and_redraw(tmp_path, monkeypatch):
    settings, broker, workspace = _setup(tmp_path, monkeypatch)
    app = InspectorApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(100, 40)) as pilot:
        await settle(app, pilot)
        assert app.selected_id() == "record-a"

        # Filter input hides non-matching rows
        app.query_one("#inspector-filter", Input).value = "nomatch-xyz"
        await pilot.pause()
        assert app.query_one("#inspector-rows", DataTable).row_count == 0
        assert app.selected_id() is None
        app.query_one("#inspector-filter", Input).value = ""
        await pilot.pause()
        assert app.query_one("#inspector-rows", DataTable).row_count == 1

        # Pagination via more() loads page 2
        await pilot.click("#entity-more")
        await settle(app, pilot)
        assert "record-b" in app.rows

        # Tasks view sets kind=task
        app.query_one("#inspector-view", Select).value = "tasks"
        await settle(app, pilot)
        assert any(p.get("kind") == "task" for _, path, p, _ in broker.calls if path == "/api/inspector/records")

        # edit_filters submits filters
        async def push_filters(screen):
            return {"kind": "decision", "status": "confirmed", "state": ""}
        monkeypatch.setattr(app, "push_screen_wait", push_filters)
        await pilot.click("#entity-filters")
        await settle(app, pilot)
        assert app.filters == {"kind": "decision", "status": "confirmed"}

        # action_history without open entity notices; with open entity loads revisions + paginates + opens revision
        app.action_history()
        await settle(app, pilot)
        app.open_selected("record-a")
        await settle(app, pilot)
        assert app.readable_detail(app.detail_data)
        await pilot.click("#entity-technical")
        await pilot.pause()
        assert app._technical_details is True
        app.redraw_detail()
        assert app._technical_details is True
        await pilot.click("#entity-history")
        await settle(app, pilot)
        assert app.history_owner == ("records", "record-a")
        await pilot.click("#entity-more")
        await settle(app, pilot)
        assert "record-a:1" in app.rows
        app.open_selected("record-a:1")
        await settle(app, pilot)
        app.redraw_detail()

        # open_revision: missing owner_id, found on page 2, not found
        await app.open_revision({"entity_type": "revision", "id": "x"})
        await app.open_revision({"entity_type": "revision", "owner_type": "record", "owner_id": "record-a", "id": "record-a:1", "revision": 1})
        assert app.detail_data["id"] == "record-a:1"
        await app.open_revision({"entity_type": "revision", "owner_type": "record", "owner_id": "record-a", "id": "missing", "revision": 99})

        # choose_source on record -> opens source -> choose_source on source -> switches view
        app.action_back()
        await settle(app, pilot)
        app.open_selected("record-a")
        await settle(app, pilot)
        choices = iter(["source-a", "metadata"])
        monkeypatch.setattr(app, "push_screen_wait", lambda s: asyncio.sleep(0, result=next(choices, None)))
        app.query_one("#entity-sources", Button).press()
        await settle(app, pilot)
        assert app.entity_kind == "sources"
        app.redraw_detail()
        app.query_one("#entity-sources", Button).press()
        await settle(app, pilot)
        assert app.source_view == "metadata"

        # inbox_group & memory via workspace_changed, plus inbox normalized -> raw fallback
        workspace.update({"selected_entity": {"entity_type": "inbox_group", "id": "inbox"}})
        await app._poll_workspace()
        await settle(app, pilot)
        assert app.view == "inbox"
        app.source_view = "normalized"
        await app.open_entity("inbox-1", "inbox")
        assert app.source_view == "raw"
        choices = iter(["raw"])
        app.choose_source()
        await settle(app, pilot)

        workspace.update({"selected_entity": {"entity_type": "memory", "id": "memory"}})
        await app._poll_workspace()
        await settle(app, pilot)
        assert app.view == "memory"

        workspace.update({"selected_entity": {"entity_type": "revision", "owner_type": "record", "owner_id": "record-a", "id": "record-a:2", "revision": 2}})
        await app._poll_workspace()
        await settle(app, pilot)
        assert app.detail_data["id"] == "record-a:2"

        # action_memory returns to records view
        app.action_memory()
        await settle(app, pilot)
        assert app.view == "records"


@pytest.mark.asyncio
async def test_inspector_review_conflict_memory_ops_and_journal(tmp_path, monkeypatch):
    settings, broker, workspace = _setup(tmp_path, monkeypatch)
    app = InspectorApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(100, 40)) as pilot:
        await settle(app, pilot)

        # review on superseded record & cancelled form
        broker.record["status"] = "superseded"
        app.open_selected("record-a")
        await settle(app, pilot)
        app.review("confirm")
        await settle(app, pilot)
        broker.record["status"] = "proposed"
        app.open_selected("record-a")
        await settle(app, pilot)
        monkeypatch.setattr(app, "push_screen_wait", lambda s: asyncio.sleep(0, result=None))
        app.review("dispute")
        await settle(app, pilot)
        # action_refresh with open entity
        app.action_refresh()
        await settle(app, pilot)

        # resolve_conflict: without open conflict, then open conflict -> cancel -> resolve -> reload_conflict missing
        app.resolve_conflict()
        await settle(app, pilot)
        app.query_one("#inspector-view", Select).value = "conflicts"
        await settle(app, pilot)
        app.open_selected("conf-1")
        await settle(app, pilot)
        app.redraw_detail()
        answers = iter([None, "keep-first", None])
        monkeypatch.setattr(app, "push_screen_wait", lambda s: asyncio.sleep(0, result=next(answers, None)))
        app.resolve_conflict()
        await settle(app, pilot)
        app.resolve_conflict()
        await settle(app, pilot)
        broker.conflicts = []
        app.reload_conflict("conf-1")
        await settle(app, pilot)
        assert "no longer pending" in str(app.query_one("#panel-notice", Static).render())

        # memory_operation: save without preview fails; preview succeeds; save succeeds; auto validates & saves
        app.action_snapshot()
        await settle(app, pilot)
        app.memory_operation("save")
        await settle(app, pilot)
        assert "preview first" in str(app.query_one("#panel-notice", Static).render())
        await pilot.click("#memory-preview")
        await settle(app, pilot)
        assert not app.query_one("#memory-save", Button).disabled
        await pilot.click("#memory-save")
        await settle(app, pilot)
        assert any(path == "/api/memory/snapshot" for _, path, _, _ in broker.calls)

        # memory_operation("auto") form validation + valid submission
        async def drive_auto(modal):
            with pytest.raises(Exception):
                await modal.on_submit({"enabled": "maybe", "interval": "15"})
            with pytest.raises(Exception):
                await modal.on_submit({"enabled": "yes", "interval": "0"})
            await modal.on_submit({"enabled": "yes", "interval": "30"})
            return {"enabled": "yes", "interval": "30"}
        monkeypatch.setattr(app, "push_screen_wait", drive_auto)
        app.memory_operation("auto")
        await settle(app, pilot)
        assert any(path == "/api/memory/settings" and m == "PATCH" for m, path, _, _ in broker.calls)

        # memory_operation("apply") without pending, and fallback else branch
        app.pending_import = None
        app.memory_operation("apply")
        await settle(app, pilot)
        app.memory_operation("refresh")
        await settle(app, pilot)

        # journal event resets preview_scope and disables save/apply buttons
        broker.pending_events.append({"event": "change", "data": {"project_id": "project-a", "entity_type": "record"}})
        app.watch_journal()
        await settle(app, pilot)
        await pilot.pause(0.3)
        assert app.preview_scope is None
        assert app.query_one("#memory-save", Button).disabled is True
        broker.fail_events = True
        app.watch_journal()
        await settle(app, pilot)
        app.action_memory()
        await settle(app, pilot)
        app.refresh_if_list()
        await settle(app, pilot)

        # open_entity empty id no-op & refresh_page error
        await app.open_entity("")
        broker.fail_once[("GET", "/api/inspector/records")] = BrokerError(500, "list failed")
        app.refresh_page()
        await settle(app, pilot)
        assert "list failed" in str(app.query_one("#panel-notice", Static).render())
