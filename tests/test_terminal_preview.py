"""Synthetic preview contracts; run with --noconftest to avoid the DB fixture."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import time

from fastapi.testclient import TestClient
import pytest
from textual.widgets import DataTable

from conftest import time_budget

from cairnmind.terminal_preview import (FixtureData, PreviewBroker, PreviewServer, PreviewTerminal, PreviewWorkspace,
                                      capture_workspace, create_preview_app, preview_settings)


@pytest.fixture
def preview(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Preview attempted real service/engine/storage initialization")
    monkeypatch.setattr("cairnmind.cli.ensure_service", forbidden)
    monkeypatch.setattr("cairnmind.cockpit.ensure_cockpit", forbidden)
    monkeypatch.setattr("cairnmind.engine.start_engine", forbidden)
    monkeypatch.setattr("cairnmind.store.Store.__init__", forbidden)
    return preview_settings(tmp_path / "preview", create=True)


def test_existing_data_and_symlink_are_never_adopted(tmp_path):
    root = tmp_path / "private"
    root.mkdir()
    sentinel = root / "private.txt"
    sentinel.write_text("untouched")
    with pytest.raises(ValueError, match="empty"):
        preview_settings(root, create=True)
    assert sentinel.read_text() == "untouched"
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        preview_settings(link, create=True)
    with pytest.raises(ValueError, match="owned"):
        preview_settings(root)


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/client-runs"), ("POST", "/api/auth/login"),
    ("POST", "/api/copilot/login"), ("POST", "/api/copilot/prepare"),
    ("POST", "/api/profiles/x/probe"), ("POST", "/api/memory/snapshot"),
    ("POST", "/api/memory/import"), ("POST", "/api/discovery"),
    ("PUT", "/api/settings"), ("DELETE", "/api/profiles/x/credential"),
    ("POST", "/_control/stop"), ("PATCH", "/unknown")])
def test_operational_mutations_are_rejected_at_server(preview, method, path):
    with TestClient(create_preview_app(preview)) as client:
        response = client.request(method, path, json={"root": "/private", "key": "CANARY_NOT_LOGGED"})
        assert response.status_code == 403
    assert "CANARY_NOT_LOGGED" not in (preview.data_dir / "requests.jsonl").read_text()


def test_real_broker_authentication_reads_and_reconnect_never_start_services(preview):
    server = PreviewServer(preview, "normal").start()
    broker = PreviewBroker(preview)
    try:
        broker.ensure()
        assert len(broker.request("GET", "/api/state")["projects"]) == 2
        broker.reconnect()
        rows = broker.request("GET", "/api/inspector/records", params={"project": "preview-0"})
        assert len(rows["items"]) == 8
        with pytest.raises(ValueError, match="403"):
            broker.request("POST", "/api/client-runs", json={})
    finally:
        broker.close()
        server.close()


def test_auth_and_native_websocket_fail_closed(preview):
    with TestClient(create_preview_app(preview)) as client:
        assert client.get("/api/state").status_code == 401
        assert client.post("/_control/ticket").status_code == 401
        ticket = client.post("/_control/ticket", headers={"Authorization": "Bearer " + preview.token}).json()["ticket"]
        assert client.post("/auth/exchange", json={"ticket": ticket}).status_code == 200
        assert client.post("/auth/exchange", json={"ticket": ticket}).status_code == 401
        assert client.get("/api/state").status_code == 200
        from starlette.websockets import WebSocketDisconnect
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/client-runs/example/terminal"):
                pass


@pytest.mark.parametrize("scenario", ["normal", "empty", "unicode-many"])
def test_synthetic_fixture_has_bounded_pages_and_exact_graph_identity(preview, scenario):
    fixture = FixtureData(preview, scenario)
    graph = fixture.read("/api/inspector/graph", {"project": "preview-0", "limit": "100"})
    assert len(graph["nodes"]) <= 100
    if scenario == "empty":
        assert fixture.projects == [] and graph["nodes"] == []
    else:
        node = graph["nodes"][0]
        record = fixture.read("/api/inspector/records/" + node["record_id"], {"project": "preview-0"})
        assert record["record"]["content"] == node["label"]
    if scenario == "unicode-many":
        assert graph["next_cursor"] == "100"


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (120, 36), (180, 55)])
@pytest.mark.parametrize("kind", ["sidebar", "inspector", "graph"])
async def test_actual_panel_render_and_keyboard_without_storage(preview, size, kind):
    from cairnmind.terminal_graph import GraphApp, TerminalGraph
    from cairnmind.terminal_inspector import InspectorApp
    from cairnmind.terminal_panels import SidebarApp
    from cairnmind.workspace_state import WorkspaceState
    server = PreviewServer(preview, "normal").start()
    broker = PreviewBroker(preview)
    workspace = WorkspaceState(preview)
    workspace.select_project(server.app.state.fixture.projects[0], intent="inspect")
    workspace.update({"focus": kind})
    app = {"sidebar": SidebarApp, "inspector": InspectorApp, "graph": GraphApp}[kind](
        preview, broker=broker, workspace=workspace)
    try:
        async with app.run_test(size=size) as pilot:
            for _ in range(100):
                ready = bool(app.model.nodes) if kind == "graph" else bool(app.query_one(DataTable).row_count)
                if ready:
                    break
                await pilot.pause(.05)
            assert ready
            text = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
            assert "Traceback" not in text and len(text.strip()) > 100
            if kind == "graph":
                widget = app.query_one(TerminalGraph)
                widget.focus()
                yaw = app.model.camera.yaw
                await pilot.press("left", "plus", "tab")
                assert app.model.camera.yaw != yaw
                await pilot.press("enter")
            elif kind == "sidebar":
                app.query_one(DataTable).focus()
                await pilot.press("p")
            else:
                app.query_one(DataTable).focus()
                await pilot.press("enter", "escape")
            await pilot.pause(.2)
            app.save_screenshot(filename=f"{kind}-{size[0]}x{size[1]}.svg", path=str(preview.data_dir))
            assert not any(row["path"] == "/api/client-runs" and row["method"] == "POST"
                           for row in server.app.state.audit)
    finally:
        broker.close()
        await asyncio.to_thread(server.close)


def test_real_five_pane_processes_render_and_recover(preview):
    from cairnmind.cockpit_state import CockpitState
    CockpitState(preview).update({"view": "cockpit"})
    binary = shutil.which("tmux")
    if not binary:
        pytest.skip("tmux >= 3.4 required")
    server = PreviewServer(preview, "normal").start()
    broker = PreviewBroker(preview)
    manager = PreviewWorkspace(preview, broker=broker, tmux_binary=binary)
    terminal = None
    if not manager.diagnostics()["available"]:
        server.close()
        pytest.skip("tmux >= 3.4 required")
    try:
        manager.create(start_supervisor=False)
        manager.state.select_project(server.app.state.fixture.projects[0], intent="inspect")
        manager.refresh()
        terminal = PreviewTerminal(manager, readonly=False)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            captures = {role: manager.tmux("capture-pane", "-p", "-t", pane).stdout
                        for role, pane in manager.panes().items()}
            if "Atlas" in captures.get("sidebar", "") and "węzłów" in captures.get("graph", ""):
                break
            time.sleep(.1)
        assert set(captures) == {"sidebar", "inspector", "graph", "client1", "client2"}
        assert "Atlas" in captures["sidebar"] and "SAFE_UI_VIEW" not in "".join(captures.values())
        assert all("Traceback" not in value for value in captures.values())
        for key, role in [("g", "graph"), ("i", "inspector"), ("p", "sidebar")]:
            terminal.send(b"\x02" + key.encode())
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if manager.state.read()["focus"] == role:
                    break
                time.sleep(.02)
            assert manager.state.read()["focus"] == role
        for width, height in [(80, 24), (120, 36), (180, 55)]:
            terminal.resize(width, height)
            time.sleep(.15)
            manager.refresh()
            assert manager.state.read()["layout"] == ("wide" if width == 180 else "compact")
            capture_workspace(manager, preview.data_dir / "captures", f"{width}x{height}")
        metadata = json.loads((preview.data_dir / "captures/180x55.json").read_text())
        assert all(not row["dead"] for row in metadata.values())
        panes = manager.panes()
        def pids():
            return {role: int(manager.tmux("display-message", "-p", "-t", pane, "#{pane_pid}").stdout)
                    for role, pane in panes.items()}
        before = pids()
        os.kill(before["sidebar"], signal.SIGTERM)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if manager.tmux("display-message", "-p", "-t", panes["sidebar"], "#{pane_dead}").stdout.strip() == "1":
                break
            time.sleep(.02)
        manager.create(start_supervisor=False)
        after = pids()
        assert before["sidebar"] != after["sidebar"]
        assert all(before[role] == after[role] for role in before if role != "sidebar")
        assert not any(row["method"] == "POST" and row["path"] == "/api/client-runs"
                       for row in server.app.state.audit)
    finally:
        if terminal is not None:
            terminal.close()
        manager.tmux("kill-server", check=False)
        manager.close()
        server.close()


@pytest.mark.asyncio
async def test_graph_keyboard_stays_live_during_slow_fetch_and_recovers_from_offline(preview):
    from cairnmind.terminal_graph import GraphApp, TerminalGraph
    from cairnmind.workspace_state import WorkspaceState
    server = PreviewServer(preview, "slow").start()
    broker = PreviewBroker(preview)
    workspace = WorkspaceState(preview)
    workspace.select_project(server.app.state.fixture.projects[0], intent="inspect")
    workspace.update({"focus": "graph"})
    app = GraphApp(preview, broker=broker, workspace=workspace, motion=False)
    try:
        async with app.run_test(size=(80, 24)) as pilot:
            for _ in range(100):
                if app._loading:
                    break
                await pilot.pause(.02)
            app.query_one(TerminalGraph).focus()
            yaw = app.model.camera.yaw
            started = time.monotonic()
            await pilot.press("left")
            assert app.model.camera.yaw != yaw
            assert time.monotonic() - started < time_budget(.5)
            for _ in range(100):
                if app.model.nodes:
                    break
                await pilot.pause(.05)
            assert app.model.nodes
            original = [row["id"] for row in app.model.nodes]
            server.app.state.fixture.scenario = "offline"
            app._dirty = True
            await pilot.pause(.7)
            assert [row["id"] for row in app.model.nodes] == original
            text = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
            assert "503" in text or "przerwane" in text
            server.app.state.fixture.scenario = "normal"
            app._dirty = True
            await pilot.pause(.7)
            assert [row["id"] for row in app.model.nodes] == original
    finally:
        broker.close()
        await asyncio.to_thread(server.close)

