"""Headless Textual tests for the native Windows standalone workspace (terminal_standalone.py)."""
from __future__ import annotations

import sys

import pytest
from textual.widgets import DataTable, Static, TabbedContent

from cairnmind import first_start, person_pipe, person_proof, terminal_standalone, workspace
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
