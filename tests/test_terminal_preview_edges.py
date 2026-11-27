"""Fast-tier coverage tests for cairnmind.terminal.terminal_preview (no real tmux required)."""
from __future__ import annotations

import argparse
import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from cairnmind.terminal import terminal_preview as tp
from cairnmind.config import Settings


def _auth_client(app, token: str) -> TestClient:
    client = TestClient(app)
    res_t = client.post("/_control/ticket", headers={"authorization": f"Bearer {token}"})
    assert res_t.status_code == 200
    res_e = client.post("/auth/exchange", json={"ticket": res_t.json()["ticket"]})
    assert res_e.status_code == 200
    return client


def test_preview_app_fixture_routes_auth_writes_offline_and_ws(tmp_path):
    settings = Settings.load(tmp_path / "data", create=True)
    app = tp.create_preview_app(settings, scenario="normal")
    fixture = app.state.fixture

    # Unauthorized calls return 401
    raw_client = TestClient(app)
    assert raw_client.post("/_control/ticket", headers={"authorization": "Bearer wrong"}).status_code == 401
    assert raw_client.get("/auth/session").status_code == 401
    assert raw_client.get("/health").status_code == 200

    client = _auth_client(app, settings.token)
    assert "csrf" in client.get("/auth/session").json()

    # Non-GET writes are refused with 403
    assert client.post("/api/projects", json={"name": "x"}).status_code == 403
    assert app.state.operational_writes_accepted == 0

    # All read routes on FixtureData
    pid = fixture.projects[0]["id"]
    rec_id = fixture.records[pid][0]["id"]
    src_id = fixture.sources[pid][0]["id"]

    assert client.get("/api/auth").json()["status"] == "preview_disabled"
    assert client.get("/api/memory/status").json()["preview"] is True
    assert client.get("/api/client-runs").json() == {"runs": []}
    assert client.get("/api/inspector/graph/groups").json()["items"] == []
    assert client.get("/api/inspector/graph").json()["nodes"]
    assert client.get("/api/inspector/graph", params={"project": pid}).json()["nodes"]
    assert client.get("/api/inspector/records", params={"project": pid, "kind": "decision"}).status_code == 200
    assert client.get(f"/api/inspector/records/{rec_id}", params={"project": pid}).json()["record"]["id"] == rec_id
    assert client.get(f"/api/inspector/records/{rec_id}/revisions", params={"project": pid}).json()["items"]
    assert client.get(f"/api/inspector/sources/{src_id}", params={"project": pid}).json()["source"]["id"] == src_id
    assert client.get("/api/inspector/records/missing-id", params={"project": pid}).status_code == 404
    assert client.get("/api/no-such-route").status_code == 404

    # Audit log trims when > 1000
    app.state.audit.extend([{"m": i} for i in range(1001)])
    assert client.get("/health").status_code == 200
    assert len(app.state.audit) < 600

    # WebSocket closes with 1008
    with pytest.raises(Exception):
        with client.websocket_connect("/ws"):
            pass

    # Offline scenario returns 503 on /api/*
    off_app = tp.create_preview_app(settings, scenario="offline")
    off_client = _auth_client(off_app, settings.token)
    assert off_client.get("/api/state").status_code == 503


def test_preview_parse_size_capture_panel_and_main(tmp_path, monkeypatch):
    assert tp.parse_size("120x40") == (120, 40)
    with pytest.raises(argparse.ArgumentTypeError):
        tp.parse_size("invalid")
    with pytest.raises(argparse.ArgumentTypeError):
        tp.parse_size("10x5")

    # capture_workspace with a fake manager (no tmux required)
    class FakeMgr:
        def panes(self):
            return {"sidebar": "%1"}

        def tmux(self, *args, check=True):
            if args[0] == "capture-pane":
                return SimpleNamespace(stdout="\x1b[32mhello\x1b[0m\n")
            return SimpleNamespace(stdout="80 24 0\n")

    shots = tmp_path / "shots"
    frames = tp.capture_workspace(FakeMgr(), shots, "snap")
    assert frames["sidebar"]["width"] == 80 and (shots / "snap-sidebar.svg").is_file()

    # panel_main instantiates app & closes broker
    settings = Settings.load(tmp_path / "data", create=True)
    closed = []

    class FakeBroker:
        def __init__(self, _s):
            pass

        def close(self):
            closed.append(True)

    ran = []

    class FakeApp:
        def __init__(self, _s, broker=None):
            self.broker = broker

        def run(self):
            ran.append("sidebar")

    monkeypatch.setattr(tp, "PreviewBroker", FakeBroker)
    monkeypatch.setattr("cairnmind.terminal.terminal_panels.SidebarApp", FakeApp)
    tp.panel_main(settings, "sidebar")
    assert ran == ["sidebar"] and closed == [True]

    # main() lifecycle with --panel, --attach-only, non-positive --duration, and full non-attach loop
    tp.preview_settings(tmp_path / "pdir", create=True)
    monkeypatch.setattr(sys, "argv", ["terminal_preview", "--data-dir", str(tmp_path / "pdir"), "--panel", "sidebar"])
    panel_calls = []
    monkeypatch.setattr(tp, "panel_main", lambda s, kind: panel_calls.append(kind))
    tp.main()
    assert panel_calls == ["sidebar"]

    monkeypatch.setattr(sys, "argv", ["terminal_preview", "--data-dir", str(tmp_path / "pdir"), "--duration", "0"])
    with pytest.raises(SystemExit):
        tp.main()

    # Full main() run with mocked PreviewServer, PreviewBroker, PreviewWorkspace, PreviewTerminal
    class MockServer:
        port = 18999
        app = SimpleNamespace(
            state=SimpleNamespace(
                fixture=SimpleNamespace(projects=[{"id": "p1", "name": "demo"}]),
                operational_writes_accepted=0,
            )
        )

        def __init__(self, _s, _sc):
            pass

        def start(self):
            return self

        def close(self):
            pass

    class MockBroker:
        def __init__(self, _s):
            pass

        def ensure(self):
            pass

    class MockWorkspace:
        socket = tmp_path / "tmux.sock"
        state = SimpleNamespace(select_project=lambda *a, **k: None)

        def __init__(self, _s, broker=None, tmux_binary=None):
            pass

        def create(self, start_supervisor=False):
            pass

        def tmux(self, *args, check=True):
            if args[0] == "display-message":
                return SimpleNamespace(stdout="0\n")
            return SimpleNamespace(stdout="")

        def refresh(self):
            pass

        def panes(self):
            return {"sidebar": "%1"}

        def close(self):
            pass

    class MockTerminal:
        def __init__(self, _m, _sz):
            pass

        def close(self):
            pass

    monkeypatch.setattr(tp, "PreviewServer", MockServer)
    monkeypatch.setattr(tp, "PreviewBroker", MockBroker)
    monkeypatch.setattr(tp, "PreviewWorkspace", MockWorkspace)
    monkeypatch.setattr(tp, "PreviewTerminal", MockTerminal)
    monkeypatch.setattr(tp, "capture_workspace", lambda *a: {"sidebar": {"dead": False}})
    monkeypatch.setattr(tp.signal, "signal", lambda *a: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "terminal_preview",
            "--data-dir",
            str(tmp_path / "full_preview"),
            "--view",
            "focus",
            "--language",
            "en",
            "--duration",
            "0.05",
            "--screenshots",
            str(tmp_path / "out_shots"),
        ],
    )
    tp.main()
    status_file = tmp_path / "full_preview" / "preview-status.json"
    assert status_file.is_file()
    assert '"complete"' in status_file.read_text(encoding="utf-8")
