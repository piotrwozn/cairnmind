"""Headless Textual tests for the native Windows standalone workspace (terminal_standalone.py)."""
from __future__ import annotations

import sys

import pytest
from textual.widgets import DataTable, Static, TabbedContent

from cairnmind.cli_cmds import first_start
from cairnmind.pal import person_pipe
from cairnmind.terminal import person_proof, terminal_standalone, workspace
from cairnmind.config import Settings


class FakeBroker:
    def __init__(self):
        self.calls: list[tuple[str, str, dict | None, dict | None]] = []
        self.projects = [{"id": "p1", "name": "demo", "root": "C:\\work\\demo"}]
        self.records = [{"id": "r1", "kind": "fact", "status": "proposed",
                         "content": "Uses Win32 Named Pipes", "version": 1}]

    def request(self, method, path, *, params=None, json=None, timeout=20):
        self.calls.append((method, path, params, json))
        if path == "/api/state":
            return {"projects": self.projects}
        if path == "/api/inspector/records":
            return {"items": self.records}
        if path == "/api/inspector/plans":
            return {"items": [{"id": "pl1", "status": "active", "title": "Windows support"}]}
        if path == "/api/inspector/tasks":
            return {"items": [{"id": "t1", "status": "proposed", "content": "Verify pipe",
                               "metadata": {"state": "in_progress"}}]}
        if path == "/api/inspector/graph":
            return {"nodes": [{"id": "n1", "kind": "fact", "label": "Win32 pipe"}], "edges": []}
        if path == "/api/findings":
            return {"id": "f1", "saved": True}
        return {"ok": True}

    def reconnect(self):
        pass

    def close(self):
        pass


@pytest.mark.asyncio
async def test_standalone_workspace_loads_projects_memory_activity_and_graph(tmp_path):
    settings = Settings.load(tmp_path / "data", create=True)
    broker = FakeBroker()
    app = terminal_standalone.StandaloneWorkspaceApp(settings, broker=broker)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.2)
        await app.workers.wait_for_complete()
        await pilot.pause(0.05)
        pt = app.query_one("#projects-table", DataTable)
        assert pt.row_count == 1
        it = app.query_one("#inspector-table", DataTable)
        assert it.row_count == 1
        assert "Win32 Named Pipes" in str(app.query_one("#inspector-detail", Static).render())
        assert "Windows support" in str(app.query_one("#activity-text", Static).render())
        assert "Nodes: 1" in str(app.query_one("#graph-text", Static).render())

        # Keyboard tab switching works without tmux.
        await pilot.press("2")
        assert app.query_one("#workspace-tabs", TabbedContent).active == "tab-inspector"
        await pilot.press("5")
        assert app.query_one("#workspace-tabs", TabbedContent).active == "tab-settings"
        assert "Standalone Textual" in str(app.query_one("#settings-summary", Static).render())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows workspace_status Named Pipe check")
def test_windows_workspace_status_reflects_live_person_pipe(tmp_path):
    settings = Settings.load(tmp_path / "data", create=True)
    closed = workspace.workspace_status(settings)
    assert closed["mode"] == "standalone" and closed["available"] is False
    assert closed["person_key"] == "unavailable"

    key = person_proof.new_key()
    with person_pipe.PersonPipeServer(settings.data_dir, key):
        opened = workspace.workspace_status(settings)
        assert opened["mode"] == "standalone" and opened["available"] is True
        assert opened["person_key"] == "present"


def test_cockpit_status_treats_standalone_windows_as_ready():
    assert first_start.cockpit_status({"code": "standalone_windows", "mode": "standalone"}) is None


@pytest.mark.asyncio
async def test_standalone_workspace_actions_filters_modals_and_errors(tmp_path, monkeypatch):
    from textual.widgets import Button, Input, Select
    from cairnmind.terminal.terminal_client import BrokerError

    settings = Settings.load(tmp_path / "data", create=True)
    broker = FakeBroker()
    broker.projects = [
        {"id": "p1", "name": "demo", "root": "C:\\work\\demo"},
        {"id": "p2", "name": "second", "root": "C:\\work\\second"},
    ]
    broker.records = [
        {"id": "r1", "kind": "fact", "status": "proposed", "content": "Uses Win32 Named Pipes", "version": 1},
        {"id": "r2", "kind": "decision", "status": "confirmed", "content": "Pinned ArcadeDB", "version": 2},
    ]

    app = terminal_standalone.StandaloneWorkspaceApp(settings, broker=broker)

    async def fake_agreed(_app, _title, _prompt, _confirm):
        return True

    monkeypatch.setattr(terminal_standalone, "agreed", fake_agreed)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.2)

        # Filter inspector rows (matching & non-matching)
        flt = app.query_one("#inspector-filter", Input)
        flt.value = "ArcadeDB"
        await pilot.pause(0.1)
        assert app.query_one("#inspector-table", DataTable).row_count == 1
        flt.value = "nomatch-xyz"
        await pilot.pause(0.1)
        assert app.query_one("#inspector-table", DataTable).row_count == 0
        flt.value = ""
        await pilot.pause(0.1)
        assert app.query_one("#inspector-table", DataTable).row_count == 2

        # Change inspector view select
        sel = app.query_one("#inspector-view", Select)
        sel.value = "tasks"
        await pilot.pause(0.15)
        sel.value = "records"
        await pilot.pause(0.15)

        # Toggle language button + refresh_sidebar + action_refresh_all
        app.query_one("#btn-toggle-lang", Button).press()
        await pilot.pause(0.05)
        assert app.language == "pl"
        app.query_one("#btn-toggle-lang", Button).press()
        await pilot.pause(0.05)
        assert app.language == "en"
        app.refresh_sidebar()
        app.action_refresh_all()
        await pilot.pause(0.15)

        # Review actions (confirmed, disputed, release, resolve) via buttons
        app.selected_row = broker.records[0]
        for bid in ("#btn-confirm", "#btn-dispute", "#btn-release", "#btn-resolve"):
            app.query_one(bid, Button).press()
            await pilot.pause(0.15)
        paths = [c[1] for c in broker.calls if c[0] == "POST"]
        assert "/api/inspector/records/r1/review" in paths
        assert "/api/inspector/records/r1/release" in paths
        assert "/api/inspector/conflicts/resolve" in paths

        # Quick finding modal & add project modal (stub push_screen_wait)
        async def fake_push_finding(_modal):
            return {"kind": "decision", "content": "New finding"}

        monkeypatch.setattr(app, "push_screen_wait", fake_push_finding)
        app.action_quick_finding()
        await pilot.pause(0.15)
        assert any(c[1] == "/api/findings" for c in broker.calls)

        async def fake_push_add(_modal):
            return {"name": "proj3", "root": str(tmp_path)}

        monkeypatch.setattr(app, "push_screen_wait", fake_push_add)
        app.query_one("#btn-add-project", Button).press()
        await pilot.pause(0.15)
        assert any(c[1] == "/api/projects" for c in broker.calls)

        # Forget project memory
        app.query_one("#btn-forget-project", Button).press()
        await pilot.pause(0.15)
        assert any(c[1] == "/api/inspector/projects/forget" for c in broker.calls)

        # Setup change success & error modal
        async def fake_change_setup(_a):
            raise RuntimeError("setup err")

        monkeypatch.setattr(terminal_standalone.terminal_setup_change, "change_setup", fake_change_setup)
        shown_modals = []

        async def capture_modal(modal):
            shown_modals.append(modal)
            return None

        monkeypatch.setattr(app, "push_screen_wait", capture_modal)
        app.query_one("#btn-change-setup", Button).press()
        await pilot.pause(0.15)
        assert shown_modals

        # Project selection event + empty projects + broker errors handled gracefully
        broker.projects = []
        app.load_all()
        await pilot.pause(0.15)
        assert app.selected_project is None

        # Actions without selected project show notices instead of crashing
        app.open_finding_form()
        app.review_selected("confirmed")
        app.forget_project_memory()
        await pilot.pause(0.1)

        # BrokerError handling across load_all, _load_inspector, _load_activity, _load_graph
        def failing_req(*_a, **_k):
            raise BrokerError(503, "broker down")

        monkeypatch.setattr(broker, "request", failing_req)
        app.selected_project = {"id": "p1", "name": "demo"}
        app.load_all()
        await app._load_inspector("p1")
        await app._load_activity("p1")
        await app._load_graph("p1")
        await pilot.pause(0.1)

        # action_quit_workspace
        app.action_quit_workspace()


def test_run_standalone_non_tty_refused_and_tty_lifecycle(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data", create=True)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(ValueError, match="interactive terminal"):
        terminal_standalone.run_standalone(settings)

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(terminal_standalone, "BrokerClient", lambda s, ensure=True: FakeBroker())
    monkeypatch.setattr(
        terminal_standalone.StandaloneWorkspaceApp,
        "run",
        lambda self: 0,
    )
    assert terminal_standalone.run_standalone(settings) == 0
