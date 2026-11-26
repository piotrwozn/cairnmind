"""Fast-tier coverage tests for cairnmind.cockpit routes, WebSocket terminal, and lifecycle helpers."""
from __future__ import annotations

import asyncio
import base64
import json

from fastapi.testclient import TestClient
import httpx
import pytest

from cairnmind import cockpit
from cairnmind.config import Settings


class _FakeAttachment:
    def __init__(self, events=None):
        self.controller = False
        self._initial_events = list(events or ())
        self.queue: asyncio.Queue | None = None
        self.written: list[bytes] = []
        self.resized: list[tuple[int, int]] = []
        self.closed = False

    def _bind_loop(self):
        self.queue = asyncio.Queue()
        for ev in self._initial_events:
            self.queue.put_nowait(ev)

    def take_control(self):
        self.controller = True

    async def write(self, data: bytes):
        self.written.append(data)

    def resize(self, cols: int, rows: int):
        self.resized.append((cols, rows))

    async def close(self):
        self.closed = True


class _FakeRuntime:
    def __init__(self, attachment=None):
        self.attachment = attachment or _FakeAttachment()
        self.stopped = []

    def list_runs(self):
        return [{"id": "run1", "project_id": "p1", "generation": 1, "lifecycle": "running"}]

    def get_run(self, run_id: str):
        if run_id == "missing":
            raise ValueError("unknown run")
        return {"id": run_id, "project_id": "p1", "generation": 1, "lifecycle": "running"}

    def stop(self, run_id: str, generation=None):
        self.stopped.append((run_id, generation))
        return {"id": run_id, "lifecycle": "stopped"}

    def interrupt(self, run_id: str, generation=None):
        return {"id": run_id, "interrupted": True}

    def start_auth(self, client: str):
        return {"id": f"auth-{client}", "client": client}

    async def subscribe(self, run_id, generation, conn_id):
        self.attachment._bind_loop()
        return self.attachment


def _make_auth_client(app, token: str) -> tuple[TestClient, str]:
    client = TestClient(app, base_url="http://127.0.0.1:8766")
    res_t = client.post("/_control/ticket", headers={"authorization": f"Bearer {token}"})
    assert res_t.status_code == 200
    res_e = client.post("/auth/exchange", json={"ticket": res_t.json()["ticket"]}, headers={"origin": "http://127.0.0.1:8766"})
    assert res_e.status_code == 200
    csrf = res_e.json()["csrf"]
    return client, csrf


def test_cockpit_routes_discovery_plans_events_and_control(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data", create=True)
    runtime = _FakeRuntime()

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/plans":
            return httpx.Response(200, json={"plans": [{"id": "pl1"}]})
        if request.url.path == "/plans/pl1":
            return httpx.Response(200, json={"id": "pl1", "steps": []})
        if request.url.path == "/events":
            return httpx.Response(503, text="unavailable")
        if request.url.path == "/projects":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={})

    app = cockpit.create_cockpit_app(
        settings,
        public_url="http://127.0.0.1:8766",
        runtime=runtime,
        proxy_transport=httpx.MockTransport(upstream_handler),
    )
    client, csrf = _make_auth_client(app, settings.token)
    headers = {"x-csrf-token": csrf, "origin": "http://127.0.0.1:8766"}

    # Discovery GET / POST / DELETE
    assert "candidates" in client.get("/api/discovery").json()
    res_post_disc = client.post("/api/discovery", json={"roots": [str(tmp_path)]}, headers=headers)
    assert res_post_disc.status_code == 200
    assert client.delete("/api/discovery", headers=headers).status_code == 200

    # Auth probe & unsupported login client
    monkeypatch.setattr("cairnmind.client_auth.probe_client", lambda c, force=False: {"client": c, "ok": True})
    assert client.get("/api/auth", params={"client": "codex"}).json()["ok"] is True
    assert client.post("/api/auth/login", json={"client": "bad"}, headers=headers).status_code == 422
    assert client.post("/api/auth/login", json={"client": "codex"}, headers=headers).status_code == 200

    # Control run: 404 unsupported action, 422 bad generation, 200 stop/interrupt
    assert client.post("/api/client-runs/run1/bogus", json={"generation": 1}, headers=headers).status_code == 404
    assert client.post("/api/client-runs/run1/stop", json={"generation": True}, headers=headers).status_code == 422
    assert client.post("/api/client-runs/run1/stop", json={"generation": 1}, headers=headers).status_code == 200
    assert client.post("/api/client-runs/run1/interrupt", json={"generation": 1}, headers=headers).status_code == 200

    # Plans list & detail
    assert client.get("/api/plans").json()["plans"][0]["id"] == "pl1"
    assert client.get("/api/plans/pl1").json()["id"] == "pl1"

    # /api/events: invalid Last-Event-ID 400, upstream 503 emits unavailable event
    assert client.get("/api/events", headers={"last-event-id": "bad cursor!"}).status_code == 400
    ev_resp = client.get("/api/events", headers={"last-event-id": "evt:1"})
    assert ev_resp.status_code == 200 and "memory_service" in ev_resp.text


def test_cockpit_terminal_websocket_protocol_paths(tmp_path):
    settings = Settings.load(tmp_path / "data", create=True)
    att = _FakeAttachment(events=[
        {"type": "output", "data": b"hello"},
        {"type": "control"},
    ])
    runtime = _FakeRuntime(attachment=att)
    app = cockpit.create_cockpit_app(
        settings,
        public_url="http://127.0.0.1:8766",
        runtime=runtime,
        proxy_transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={})),
    )
    client, csrf = _make_auth_client(app, settings.token)

    base_ws = "ws://127.0.0.1:8766/api/client-runs/run1/terminal"
    # Missing generation -> close 1008
    with pytest.raises(Exception):
        with client.websocket_connect(f"{base_ws}?csrf={csrf}", headers={"origin": "http://127.0.0.1:8766"}):
            pass

    # Mismatched generation -> close 1008
    with pytest.raises(Exception):
        with client.websocket_connect(f"{base_ws}?csrf={csrf}&generation=99", headers={"origin": "http://127.0.0.1:8766"}):
            pass

    # Happy path: output + control + takeover + text/base64 input + resize + unsupported frame -> error
    with client.websocket_connect(
        f"{base_ws}?csrf={csrf}&generation=1",
        headers={"origin": "http://127.0.0.1:8766"},
    ) as ws:
        msg0 = ws.receive_json()
        assert msg0["type"] == "control"
        msg1 = ws.receive_json()
        assert msg1["type"] == "output" and base64.b64decode(msg1["data"]) == b"hello"
        msg2 = ws.receive_json()
        assert msg2["type"] == "control"

        ws.send_text(json.dumps({"type": "takeover"}))
        ws.send_text(json.dumps({"type": "input", "text": "ls\n"}))
        ws.send_text(json.dumps({"type": "input", "data": base64.b64encode(b"pwd\n").decode()}))
        ws.send_text(json.dumps({"type": "resize", "cols": 100, "rows": 30}))
        ws.send_text(json.dumps({"type": "unsupported_kind"}))
        err = ws.receive_json()
        assert err["type"] == "error" and err["code"] == "terminal_protocol_error"

    assert att.written == [b"ls\n", b"pwd\n"]
    assert att.resized == [(100, 30)]
    assert att.closed is True

    # Gap event closes cleanly with 1013
    att_gap = _FakeAttachment(events=[{"type": "gap"}])
    runtime.attachment = att_gap
    with client.websocket_connect(
        f"{base_ws}?csrf={csrf}&generation=1",
        headers={"origin": "http://127.0.0.1:8766"},
    ) as ws:
        assert ws.receive_json()["type"] == "control"
        assert ws.receive_json()["type"] == "gap"


def test_cockpit_status_stop_and_serve_locked_helpers(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data", create=True)

    # Bad URL in server.json refused safely
    from cairnmind.private_files import ensure_private_dir, make_private

    runtime_file = cockpit._runtime_file(settings)
    ensure_private_dir(runtime_file.parent)
    runtime_file.write_text(json.dumps({
        "instance": cockpit.instance_id(settings),
        "pid": cockpit.os.getpid(),
        "birth": cockpit._birth(cockpit.os.getpid()),
        "url": "https://evil.example:8766",
    }), encoding="utf-8")
    make_private(runtime_file)
    assert cockpit.cockpit_status(settings)["available"] is False

    # stop_cockpit when unavailable returns clean dict
    assert cockpit.stop_cockpit(settings) == {"stopping": False, "available": False}

    # serve_cockpit / _serve_locked writes & cleans up server.json (with mocked uvicorn.Server)
    import uvicorn

    ran = []

    class FakeUvicornServer:
        def __init__(self, config):
            self.config = config
            self.should_exit = False

        def run(self, sockets=None):
            ran.append(len(sockets or []))
            assert runtime_file.is_file()

    monkeypatch.setattr(uvicorn, "Server", FakeUvicornServer)
    cockpit.serve_cockpit(settings, port=0)
    assert ran == [1]
    assert not runtime_file.exists()
