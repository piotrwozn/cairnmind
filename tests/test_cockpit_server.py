"""Browser control authentication and server-only native/memory boundaries."""
from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
import json
from pathlib import Path
import time

from fastapi.testclient import TestClient
import httpx
import pytest
from starlette.websockets import WebSocketDisconnect

from cairnmind.cockpit import BrowserSessions, create_cockpit_app
from cairnmind.config import Settings


ORIGIN = "http://127.0.0.1:8766"


class Attachment:
    def __init__(self):
        self.controller = True
        self.queue = asyncio.Queue()
        self.queue.put_nowait({"type": "output", "data": "native λ permission?".encode()})
        self.written = []
        self.size = None
        self.closed = False

    async def write(self, data):
        self.written.append(data)
        self.queue.put_nowait({"type": "output", "data": b"echo:" + data})

    def resize(self, cols, rows):
        self.size = (cols, rows)

    def take_control(self):
        self.controller = True
        self.queue.put_nowait({"type": "control", "controller": "private-connection-id"})

    async def close(self):
        self.closed = True


class Runtime:
    def __init__(self):
        self.runs = []
        self.starts = []
        self.stopped = []
        self.attachment = None
        self.closed = False

    def diagnostics(self):
        return {"tmux": {"available": True}}

    def list_runs(self, project_id=None):
        return deepcopy([r for r in self.runs if project_id is None or r["project_id"] == project_id])

    def start_project(self, project, *, worktree=None, slots=None, resume=False):
        self.starts.append({"project": project, "worktree": worktree, "slots": slots, "resume": resume})
        self.runs = [{"id": f"run-{slot['id']}", "run_id": f"run-{slot['id']}", "generation": "generation-one",
                      "project_id": project["id"], "client": slot["client"], "slot_id": slot["id"],
                      "worktree": worktree, "lifecycle": "running", "state": "running", "session_id": "logical"}
                     for slot in slots]
        return {"runs": self.list_runs(), "errors": []}

    def get_run(self, identifier):
        return next((deepcopy(r) for r in self.runs if r["id"] == identifier), None) or (_ for _ in ()).throw(ValueError("Unknown run"))

    def stop(self, identifier, *, generation=None):
        run = self.get_run(identifier)
        if run["generation"] != generation:
            raise ValueError("stale generation")
        self.stopped.append(identifier)
        return {**run, "state": "exited"}

    interrupt = stop

    def start_auth(self, client):
        return {"id": "auth-run", "generation": "auth-generation", "client": client, "purpose": "auth", "session_id": None}

    async def subscribe(self, identifier, generation, connection_id):
        self.get_run(identifier)
        self.attachment = Attachment()
        return self.attachment

    async def aclose(self):
        self.closed = True


@pytest.fixture
def cockpit(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data", create=True)
    root = tmp_path / "repo"
    root.mkdir()
    project = {"id": "project-one", "name": "Example", "root": str(root)}
    runtime = Runtime()
    requests = []
    outage = {"enabled": False}

    def upstream(request):
        assert request.headers["authorization"] == "Bearer " + settings.token
        requests.append((request.method, request.url.path, request.content))
        if outage["enabled"]:
            raise httpx.ConnectError("private runtime unavailable", request=request)
        if request.url.path == "/projects":
            return httpx.Response(200, json=[project] if request.method == "GET" else {"project": project})
        if request.url.path == "/project-roots":
            return httpx.Response(200, json={"roots": [str(root)]})
        if request.url.path == "/background-inference/enable":
            return httpx.Response(200, json={"accepted": True, "project_id": project["id"], "worker_running": True})
        if request.url.path.startswith("/sessions/"):
            return httpx.Response(200, json={"status": "ready", "acknowledged": False, "stale": False, "unknowns": []})
        if request.url.path.startswith("/inspector/"):
            return httpx.Response(200, json={"items": [{"content": "archived literal <script>"}], "cursor": "epoch:1"})
        if request.url.path.startswith("/memory/"):
            return httpx.Response(409, json={"detail": "memory conflict"})
        if request.url.path == "/events":
            assert request.headers["last-event-id"] == "epoch:9"
            return httpx.Response(200, text='id: epoch:10\nevent: change\ndata: {"operation":"upsert"}\n\n',
                                  headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr("cairnmind.client_auth.probe_client", lambda client, **kwargs: {
        "client": client, "installation": "installed", "auth": "reported_signed_in", "ready_to_launch": True})
    app = create_cockpit_app(settings, runtime=runtime, proxy_transport=httpx.MockTransport(upstream), public_url=ORIGIN)
    with TestClient(app, base_url=ORIGIN) as client:
        yield settings, app, client, runtime, project, requests, outage
    assert runtime.closed is True


def login(app, client):
    ticket = app.state.browser_sessions.ticket()
    response = client.post("/auth/exchange", json={"ticket": ticket}, headers={"Origin": ORIGIN})
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"] and "SameSite=strict" in response.headers["set-cookie"]
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf"]}


def test_bootstrap_tickets_expire_are_one_use_and_sessions_expire():
    clock = [0.]
    sessions = BrowserSessions(clock=lambda: clock[0])
    ticket = sessions.ticket()
    token, session = sessions.exchange(ticket)
    assert sessions.get(token) is session
    with pytest.raises(Exception) as rejected:
        sessions.exchange(ticket)
    assert rejected.value.status_code == 401
    expired = sessions.ticket()
    clock[0] = 61
    with pytest.raises(Exception):
        sessions.exchange(expired)
    clock[0] = 12 * 60 * 60 + 1
    assert sessions.get(token) is None


def test_browser_requires_cookie_origin_host_and_csrf(cockpit):
    settings, app, client, runtime, project, requests, outage = cockpit
    assert client.get("/api/state").status_code == 401
    ticket = app.state.browser_sessions.ticket()
    assert client.post("/auth/exchange", json={"ticket": ticket}).status_code == 403
    assert client.post("/auth/exchange", json={"ticket": ticket}, headers={"Origin": "https://evil.invalid"}).status_code == 403
    assert client.get("/health", headers={"Host": "evil.invalid"}).status_code == 403
    headers = login(app, client)
    assert client.put("/api/settings", json={"last_project": project["id"]}, headers={"Origin": ORIGIN}).status_code == 403
    assert client.put("/api/settings", json={"last_project": project["id"]}, headers=headers).status_code == 200
    response = client.get("/api/state")
    assert response.status_code == 200
    assert settings.token not in response.text
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_control_bearer_is_not_browser_cookie_and_never_in_bootstrap(cockpit):
    settings, app, client, *_ = cockpit
    assert client.post("/_control/ticket").status_code == 401
    assert client.post("/_control/ticket", headers={"Authorization": "Bearer " + settings.token, "Origin": ORIGIN}).status_code == 403
    issued = client.post("/_control/ticket", headers={"Authorization": "Bearer " + settings.token})
    assert issued.status_code == 200 and settings.token not in issued.text
    ticket = issued.json()["ticket"]
    response = client.post("/auth/exchange", json={"ticket": ticket}, headers={"Origin": ORIGIN})
    assert response.status_code == 200 and settings.token not in response.text
    assert client.cookies.get(app.state.cookie_name)


def test_launch_is_idempotent_and_never_accepts_arbitrary_executable(cockpit):
    settings, app, client, runtime, project, *_ = cockpit
    headers = login(app, client)
    payload = {"project": project["id"], "idempotency_key": "first-action"}
    one = client.post("/api/client-runs", json=payload, headers=headers)
    two = client.post("/api/client-runs", json=payload, headers=headers)
    assert one.status_code == two.status_code == 200 and one.json() == two.json()
    assert len(runtime.starts) == 1 and len(runtime.starts[0]["slots"]) == 2
    assert runtime.starts[0]["slots"][0]["profile_id"] == "codex"
    bad = client.post("/api/client-runs", json={**payload, "argv": ["sh", "-c", "echo BAD"]}, headers=headers)
    assert bad.status_code == 422
    changed = client.post("/api/client-runs", json={**payload, "profile_id": "codex"}, headers=headers)
    assert changed.status_code == 409
    listed = client.get("/api/client-runs").json()["runs"]
    assert listed[0]["memory"]["acknowledged"] is False
    wrong = client.post("/api/client-runs/run-codex/stop", json={"generation": "old"}, headers=headers)
    assert wrong.status_code == 409 and not runtime.stopped
    good = client.post("/api/client-runs/run-codex/stop", json={"generation": "generation-one"}, headers=headers)
    assert good.status_code == 200 and runtime.stopped == ["run-codex"]


def test_proxy_preserves_conflict_and_does_not_expose_token_or_recover_checks(cockpit):
    settings, app, client, runtime, project, requests, _ = cockpit
    headers = login(app, client)
    inspected = client.get("/api/inspector/sources", params={"project": project["id"]})
    assert inspected.status_code == 200 and settings.token not in inspected.text
    assert "<script>" in inspected.json()["items"][0]["content"]
    conflict = client.post("/api/memory/snapshot", json={"project": project["id"]}, headers=headers)
    assert conflict.status_code == 409 and conflict.json()["detail"] == "memory conflict"
    assert client.get("/api/checks").status_code == 404
    assert client.post("/api/memory/../shutdown", json={}, headers=headers).status_code in {404, 405}
    assert client.post("/api/inspector/%2e%2e/%2e%2e/shutdown", json={}, headers=headers).status_code == 404
    assert not any(path in {"/checks", "/shutdown"} for _, path, _ in requests)


def test_memory_outage_keeps_live_terminals_and_cached_projects_available(cockpit):
    settings, app, client, runtime, project, requests, outage = cockpit
    headers = login(app, client)
    assert client.get("/api/state").json()["memory_available"] is True
    client.post("/api/client-runs", json={"project": project["id"], "idempotency_key": "launch"}, headers=headers)
    outage["enabled"] = True
    state = client.get("/api/state")
    assert state.status_code == 200 and state.json()["memory_available"] is False
    assert len(state.json()["runs"]) == 2 and len(state.json()["projects"]) == 1
    listed = client.get("/api/client-runs")
    assert listed.status_code == 200 and listed.json()["runs"][0]["memory"]["status"] == "unavailable"
    scoped = client.get("/api/client-runs", params={"project": project["id"]})
    assert scoped.status_code == 200 and len(scoped.json()["runs"]) == 2


def test_event_reconnect_forwards_cursor_and_rejects_oversized_headers(cockpit):
    _, app, client, *_ = cockpit
    login(app, client)
    response = client.get("/api/events?project=project-one&after=epoch:1", headers={"Last-Event-ID": "epoch:9"})
    assert response.status_code == 200 and "id: epoch:10" in response.text
    assert client.get("/api/events", headers={"Last-Event-ID": "x" * 201}).status_code == 400


def test_body_limit_precedes_memory_or_runtime_mutation(cockpit):
    _, app, client, runtime, _, requests, _ = cockpit
    headers = login(app, client)
    response = client.post("/api/client-runs", content='{"input":"' + "a" * (256 * 1024) + '"}', headers=headers)
    assert response.status_code == 413 and not runtime.starts and not requests


def test_operational_auth_has_no_logical_session(cockpit):
    _, app, client, runtime, *_ = cockpit
    headers = login(app, client)
    response = client.post("/api/auth/login", json={"client": "codex"}, headers=headers)
    assert response.json()["run"]["purpose"] == "auth"
    assert response.json()["run"]["session_id"] is None
    assert client.post("/api/auth/login", json={"client": "sh"}, headers=headers).status_code == 422


def test_project_directory_preflight_detects_corrupt_memory_without_registration(cockpit):
    _, app, client, runtime, project, requests, _ = cockpit
    headers = login(app, client)
    root = Path(project["root"])
    clean = client.post("/api/projects/inspect", json={"root": str(root / ".." / root.name)}, headers=headers)
    assert clean.status_code == 200 and clean.json() == {"root": str(root), "memory_snapshot": None}
    manifest = root / ".memory/devbrain/manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(b"corrupt snapshot: not JSON")
    detected = client.post("/api/projects/inspect", json={"root": str(root)}, headers=headers)
    assert detected.status_code == 200
    assert detected.json() == {"root": str(root), "memory_snapshot": str(manifest.parent)}
    assert client.post("/api/projects/inspect", json={"root": str(root / "missing")}, headers=headers).status_code == 422
    assert client.post("/api/projects/inspect", json={"root": [str(root)]}, headers=headers).status_code == 422
    assert client.post("/api/projects/inspect", json={"root": str(root), "register": True}, headers=headers).status_code == 422
    assert not requests and not runtime.starts


def test_terminal_websocket_requires_cookie_origin_generation_and_transmits_literal_bytes(cockpit):
    _, app, client, runtime, project, *_ = cockpit
    headers = login(app, client)
    client.post("/api/client-runs", json={"project": project["id"], "idempotency_key": "launch"}, headers=headers)
    csrf = headers["X-CSRF-Token"]
    with pytest.raises(Exception):
        with client.websocket_connect(f"ws://127.0.0.1:8766/api/client-runs/run-codex/terminal?csrf={csrf}&generation=generation-one",
                                      headers={"Origin": "https://evil.invalid"}):
            pass
    with client.websocket_connect(f"ws://127.0.0.1:8766/api/client-runs/run-codex/terminal?csrf={csrf}&generation=generation-one",
                                  headers={"Origin": ORIGIN}) as ws:
        assert ws.receive_json() == {"type": "control", "controller": True}
        output = ws.receive_json()
        assert base64.b64decode(output["data"]).decode() == "native λ permission?"
        ws.send_json({"type": "input", "text": "literal \"; DROP \\ λ"})
        echoed = ws.receive_json()
        assert base64.b64decode(echoed["data"]).startswith(b"echo:literal")
    deadline = time.monotonic() + 1
    while not runtime.attachment.closed and time.monotonic() < deadline:
        time.sleep(.01)
    assert runtime.attachment.closed is True


def test_terminal_takeover_translates_private_broker_lease_to_browser_boolean(cockpit, monkeypatch):
    _, app, client, runtime, project, *_ = cockpit
    headers = login(app, client)
    client.post("/api/client-runs", json={"project": project["id"], "idempotency_key": "launch"}, headers=headers)
    attachment = Attachment()
    attachment.controller = False

    async def subscribe(*args):
        return attachment

    monkeypatch.setattr(runtime, "subscribe", subscribe)
    with client.websocket_connect(
        "ws://127.0.0.1:8766/api/client-runs/run-codex/terminal?csrf=" + headers["X-CSRF-Token"] + "&generation=generation-one",
        headers={"Origin": ORIGIN},
    ) as ws:
        assert ws.receive_json() == {"type": "control", "controller": False}
        assert ws.receive_json()["type"] == "output"
        ws.send_json({"type": "input", "text": "ignored observer keystroke"})
        ws.send_json({"type": "takeover"})
        assert ws.receive_json() == {"type": "control", "controller": True}
        ws.send_json({"type": "input", "text": "owned"})
        assert base64.b64decode(ws.receive_json()["data"]) == b"echo:owned"
    assert attachment.written == [b"owned"]


@pytest.mark.parametrize("frame", [
    {"type": "input", "text": "must-not-reach-native-after-expiry"},
    {"type": "resize", "cols": 100, "rows": 30},
    {"type": "takeover"},
])
def test_existing_terminal_expires_before_native_input_resize_or_takeover(cockpit, frame):
    _, app, client, runtime, project, *_ = cockpit
    clock = [0.0]
    app.state.browser_sessions.clock = lambda: clock[0]
    headers = login(app, client)
    client.post("/api/client-runs", json={"project": project["id"], "idempotency_key": "expiry"}, headers=headers)
    csrf = headers["X-CSRF-Token"]
    url = f"ws://127.0.0.1:8766/api/client-runs/run-codex/terminal?csrf={csrf}&generation=generation-one"
    with client.websocket_connect(url, headers={"Origin": ORIGIN}) as ws:
        assert ws.receive_json() == {"type": "control", "controller": True}
        assert ws.receive_json()["type"] == "output"
        if frame["type"] == "takeover":
            runtime.attachment.controller = False
        clock[0] = 12 * 60 * 60 + 1
        assert client.get("/auth/session").status_code == 401
        ws.send_json(frame)
        with pytest.raises(WebSocketDisconnect) as rejected:
            ws.receive_json()
        assert rejected.value.code == 1008
    deadline = time.monotonic() + 1
    while not runtime.attachment.closed and time.monotonic() < deadline:
        time.sleep(.01)
    assert runtime.attachment.closed is True
    assert runtime.attachment.written == []
    assert runtime.attachment.size is None
    assert runtime.attachment.controller is (frame["type"] != "takeover")


def test_inspector_proxy_allows_only_review_and_conflict_decisions_to_write(cockpit):
    settings, app, client, runtime, project, requests, _ = cockpit
    headers = login(app, client)
    body = {"project": project["id"], "record_id": "a", "other_id": "b", "action": "dismiss",
            "expected_version": 1, "other_expected_version": 1}
    assert client.post("/api/inspector/conflicts/resolve", json=body, headers=headers).status_code == 200
    method, path, content = requests[-1]
    assert (method, path) == ("POST", "/inspector/conflicts/resolve") and json.loads(content) == body
    sent = len(requests)
    for path in ("conflicts", "conflicts/resolve/extra", "records/a", "sources/a/resolve"):
        assert client.post("/api/inspector/" + path, json=body, headers=headers).status_code == 405
    assert len(requests) == sent
    assert client.get("/api/inspector/conflicts", params={"project": project["id"]}, headers=headers).status_code == 200
    assert requests[-1][:2] == ("GET", "/inspector/conflicts")


def test_brief_proxy_forwards_only_project_and_worktree(cockpit):
    settings, app, client, runtime, project, requests, _ = cockpit
    headers = login(app, client)
    answered = client.get("/api/brief", params={"project": project["id"], "worktree": "/repo"}, headers=headers)
    assert answered.status_code == 200 and settings.token not in answered.text
    method, path, _ = requests[-1]
    assert (method, path) == ("GET", "/brief")
    sent = len(requests)
    assert client.get("/api/brief", params={"worktree": "/repo"}, headers=headers).status_code == 422
    assert client.get("/api/brief", params={"project": project["id"], "session_id": "forged"},
                      headers=headers).status_code == 422
    assert len(requests) == sent
    client.cookies.clear()
    assert client.get("/api/brief", params={"project": project["id"]},
                      headers={"Origin": ORIGIN}).status_code in {401, 403}
    assert len(requests) == sent


def test_ask_proxy_forwards_only_bounded_question_fields(cockpit):
    settings, app, client, runtime, project, requests, _ = cockpit
    headers = login(app, client)
    answered = client.post("/api/ask", json={"project": project["id"], "question": "Why ArcadeDB?"},
                           headers=headers)
    assert answered.status_code == 200 and settings.token not in answered.text
    method, path, content = requests[-1]
    assert (method, path) == ("POST", "/ask")
    assert json.loads(content) == {"project": project["id"], "question": "Why ArcadeDB?"}
    sent = len(requests)
    for body in ({"project": project["id"], "question": "  "},
                 {"project": project["id"], "question": "x" * 4001},
                 {"project": project["id"], "question": "Why?", "session_id": "forged"},
                 {"question": "Why?"}):
        assert client.post("/api/ask", json=body, headers=headers).status_code == 422
    assert len(requests) == sent
    assert client.post("/api/ask", json={"project": project["id"], "question": "Why?"},
                       headers={"Origin": ORIGIN}).status_code in {401, 403}
