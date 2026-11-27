"""How the workspace's key reaches a person's request (#214): the workspace sets it, the panel's client
sends it for a person's POST only, and the broker passes it on. Nothing here is enforced yet; no engine."""
from __future__ import annotations

import json
import sys

from fastapi.testclient import TestClient
import httpx
import pytest

from cairnmind.terminal import cockpit as cockpit_module, person_proof
from cairnmind.terminal.cockpit import create_cockpit_app
from cairnmind.config import Settings
from cairnmind.terminal.terminal_client import BrokerClient
from cairnmind.terminal.workspace import WorkspaceManager
from test_cockpit_server import ORIGIN, Runtime, login
from test_person_proof import KEY, fake_tmux, workspace_socket  # noqa: F401 (the fixture)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Tests POSIX tmux person-key delivery")

SOCKET = "/tmp/cairnmind-ui-1-x/tmux.sock"


@pytest.fixture
def settings(tmp_path):
    return Settings.load(tmp_path / "data", create=True)


def test_the_workspace_sets_its_key_through_standard_input(settings, tmp_path):
    binary, calls = fake_tmux(tmp_path)
    manager = WorkspaceManager(settings, tmux_binary=binary)
    manager._ensure_person_key()
    sets = [call for call in calls() if "source-file" in call["argv"]]
    assert len(sets) == 1 and sets[0]["argv"][-2:] == ["source-file", "-"]
    key = (tmp_path / "fake_key").read_text()
    assert sets[0]["stdin"] == person_proof.set_key_input(key)
    assert not any(key in " ".join(call["argv"]) for call in calls())
    # A server that has a key keeps it; one without gets one.
    manager._ensure_person_key()
    assert len([call for call in calls() if "source-file" in call["argv"]]) == 1
    (tmp_path / "fake_key").write_text("")
    manager._ensure_person_key()
    assert len([call for call in calls() if "source-file" in call["argv"]]) == 2
    assert (tmp_path / "fake_key").read_text() != key


def test_a_workspace_without_a_usable_key_does_not_open_quietly(settings, tmp_path, monkeypatch):
    binary, _ = fake_tmux(tmp_path)
    manager = WorkspaceManager(settings, tmux_binary=binary)
    monkeypatch.setattr(person_proof, "read_key", lambda *args: ("unavailable", None))
    with pytest.raises(ValueError, match="workspace stop"):
        manager._ensure_person_key()


def test_workspace_status_reports_the_key_without_its_value(settings, tmp_path, monkeypatch):
    binary, _ = fake_tmux(tmp_path, KEY)
    manager = WorkspaceManager(settings, tmux_binary=binary)
    monkeypatch.setattr(person_proof, "read_key", lambda *args: ("key", KEY))
    # A socket that the checks of a restore and of a person's request refuse is not "present" (#214 review).
    assert manager.status()["person_key"] == "unavailable"
    monkeypatch.setattr(person_proof, "workspace_socket", lambda settings, value: value)
    for kind, expected in (("key", "present"), ("no_key", "absent"), ("unavailable", "unavailable")):
        monkeypatch.setattr(person_proof, "read_key", lambda *args, kind=kind: (kind, KEY if kind == "key" else None))
        status = manager.status()
        assert status["person_key"] == expected and KEY not in json.dumps(status)


@pytest.fixture
def client_and_calls(settings, monkeypatch):
    status = {"available": True, "url": "http://127.0.0.1:8766", "version_matches": True}
    monkeypatch.setattr(cockpit_module, "cockpit_status", lambda settings: status)
    calls = []

    def handler(request):
        path = request.url.path
        if path == "/_control/ticket":
            return httpx.Response(200, json={"ticket": "t"})
        if path == "/auth/exchange":
            return httpx.Response(200, json={"csrf": "c"}, headers={"Set-Cookie": "local_session=s; Path=/"})
        calls.append((request.method, path, dict(request.headers)))
        return httpx.Response(200, json={"ok": True})
    client = BrokerClient(settings, transport=httpx.MockTransport(handler))
    try:
        yield client, calls
    finally:
        client.close()


PERSON_POSTS = ("/api/inspector/records/r1/review", "/api/inspector/conflicts/resolve",
                "/api/inspector/plans/p1/confirm", "/api/inspector/record-kinds/accept",
                "/api/inspector/record-kinds/withdraw", "/api/findings", "/api/memory/import",
                "/api/inspector/records/r1/release", "/api/inspector/impact/verdict",
                "/api/memory/import-published", "/api/memory/import-commit",
                "/api/inspector/legacy-groups/adopt", "/api/inspector/projects/forget",
                "/api/inspector/records/r1/replace")


def test_broker_client_adds_the_key_only_for_a_persons_post_inside_the_workspace(
        client_and_calls, settings, monkeypatch):
    client, calls = client_and_calls
    monkeypatch.setattr(person_proof, "read_key", lambda *args: ("key", KEY))
    monkeypatch.setattr(person_proof, "workspace_socket", lambda settings, value: SOCKET if value == SOCKET else None)
    monkeypatch.setenv("TMUX", f"{SOCKET},1,0")
    for path in PERSON_POSTS:
        client.request("POST", path, json={})
        sent = calls[-1][2]
        assert sent["x-cairnmind-person"] == KEY and sent["x-cairnmind-workspace"] == SOCKET, path
    for method, path in (("GET", "/api/inspector/records"), ("POST", "/api/ask"), ("POST", "/api/client-runs"),
                         ("GET", "/api/findings"), ("GET", "/api/inspector/impact"),
                         ("GET", "/api/inspector/legacy-groups"), ("GET", "/api/inspector/projects/deletable"),
                         ("GET", "/api/inspector/projects/forget")):
        client.request(method, path, json=None if method == "GET" else {})
        assert "x-cairnmind-person" not in calls[-1][2] and "x-cairnmind-workspace" not in calls[-1][2], path
    # A coding session's shell: another server's socket, or none.
    monkeypatch.setenv("TMUX", "/tmp/cairnmind-native-1-x/tmux.sock,1,0")
    client.request("POST", PERSON_POSTS[0], json={})
    assert "x-cairnmind-person" not in calls[-1][2]
    monkeypatch.delenv("TMUX")
    client.request("POST", PERSON_POSTS[0], json={})
    assert "x-cairnmind-person" not in calls[-1][2]
    # A workspace without a key sends only its socket, so that the service can say so.
    monkeypatch.setenv("TMUX", f"{SOCKET},1,0")
    monkeypatch.setattr(person_proof, "read_key", lambda *args: ("no_key", None))
    client.request("POST", PERSON_POSTS[0], json={})
    assert calls[-1][2]["x-cairnmind-workspace"] == SOCKET and "x-cairnmind-person" not in calls[-1][2]


@pytest.fixture
def broker_app(settings, monkeypatch):
    seen = []

    def upstream(request):
        seen.append((request.method, request.url.path, dict(request.headers)))
        return httpx.Response(200, json={"ok": True})
    monkeypatch.setattr("cairnmind.agents.client_auth.probe_client", lambda client, **kwargs: {})
    app = create_cockpit_app(settings, runtime=Runtime(), proxy_transport=httpx.MockTransport(upstream),
                             public_url=ORIGIN)
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, login(app, client), seen


def test_the_broker_passes_the_key_on_person_routes_only(broker_app):
    client, headers, seen = broker_app
    proof = {person_proof.KEY_HEADER: KEY, person_proof.SOCKET_HEADER: SOCKET}
    plan = {"project": "p", "expected_plan_version": 1}
    for path, body in (("/api/inspector/records/r1/review", {"project": "p", "action": "confirm"}),
                       ("/api/inspector/conflicts/resolve", {}), ("/api/inspector/plans/p1/confirm", plan),
                       ("/api/inspector/record-kinds/accept", {}),
                       ("/api/memory/import", {}), ("/api/memory/import-published", {}),
                       ("/api/memory/import-commit", {}),
                       ("/api/inspector/records/r1/release", {"project": "p", "reason": "Example."}),
                       ("/api/inspector/records/r1/replace", {"project": "p", "content": "x", "expected_version": 1}),
                       ("/api/inspector/impact/verdict", {"project": "p"}),
                       ("/api/inspector/legacy-groups/adopt", {"project": "p"}),
                       ("/api/inspector/projects/forget", {"project": "p"})):
        assert client.post(path, json=body, headers={**headers, **proof}).status_code == 200, path
        sent = seen[-1][2]
        assert sent["x-cairnmind-person"] == KEY and sent["x-cairnmind-workspace"] == SOCKET, path
        assert sent["authorization"].startswith("Bearer "), path
    finding = {"project": "p", "content": "A finding that holds.", "kind": "fact", "status": "proposed",
               "key": "6f1d1c52-3c0b-4a64-8d0a-1f2a3b4c5d6e"}
    assert client.post("/api/findings", json=finding, headers={**headers, **proof}).status_code == 200
    assert seen[-1][2]["x-cairnmind-person"] == KEY
    # Not a person's route: nothing is passed on.
    assert client.post("/api/ask", json={"project": "p", "question": "Why?"}, headers={**headers, **proof}).status_code == 200
    assert "x-cairnmind-person" not in seen[-1][2] and "x-cairnmind-workspace" not in seen[-1][2]
    assert client.post("/api/memory/preview", json={}, headers={**headers, **proof}).status_code == 200
    assert "x-cairnmind-person" not in seen[-1][2]
    # Malformed values are dropped, not passed on.
    client.post("/api/inspector/conflicts/resolve", json={},
                headers={**headers, person_proof.KEY_HEADER: "x" * 201, person_proof.SOCKET_HEADER: SOCKET})
    assert "x-cairnmind-person" not in seen[-1][2] and seen[-1][2]["x-cairnmind-workspace"] == SOCKET
    # No proof from the panel: none is invented.
    client.post("/api/inspector/conflicts/resolve", json={}, headers=headers)
    assert "x-cairnmind-person" not in seen[-1][2] and "x-cairnmind-workspace" not in seen[-1][2]
    # A plan can be confirmed through the broker; a path that merely looks like it cannot.
    assert client.post("/api/inspector/plans/p1/confirm/extra", json={}, headers=headers).status_code == 405
    assert client.post("/api/inspector/records/r1/release/extra", json={}, headers=headers).status_code == 405
    assert client.post("/api/inspector/legacy-groups/adopt/extra", json={}, headers=headers).status_code == 405
    assert client.post("/api/inspector/projects/forget/extra", json={}, headers=headers).status_code == 405
    assert client.post("/api/inspector/projects/deletable", json={}, headers=headers).status_code == 405
