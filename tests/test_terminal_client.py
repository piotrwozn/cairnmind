"""Private terminal broker authentication, session renewal and event cursors."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace

import httpx
import pytest

from devbrain import cockpit
from devbrain.config import Settings
from devbrain.terminal_client import BrokerClient, BrokerError


ORIGIN = "http://127.0.0.1:8766"


@pytest.fixture
def private_client(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data", create=True)
    status = {"available": True, "url": ORIGIN, "version_matches": True, "pid": 123, "birth": "fixture"}
    monkeypatch.setattr(cockpit, "cockpit_status", lambda settings: status)
    monkeypatch.setattr(cockpit, "ensure_cockpit", lambda settings: status)
    state = {"tickets": 0, "exchanges": 0, "calls": [], "expire": False, "disconnect": False, "streams": 0}

    def handler(request):
        path = request.url.path
        if path == "/_control/ticket":
            assert request.headers["Authorization"] == "Bearer " + settings.token
            assert "origin" not in request.headers
            state["tickets"] += 1
            return httpx.Response(200, json={"ticket": "private-ticket-" + str(state["tickets"])})
        if path == "/auth/exchange":
            assert request.headers["Origin"] == ORIGIN
            assert json.loads(request.content)["ticket"] == "private-ticket-" + str(state["tickets"])
            state["exchanges"] += 1
            return httpx.Response(200, json={"csrf": "private-csrf-" + str(state["exchanges"])},
                headers={"Set-Cookie": f"local_session=cookie-{state['exchanges']}; HttpOnly; SameSite=Strict; Path=/"})
        assert request.headers["Origin"] == ORIGIN
        assert request.headers["Cookie"] == "local_session=cookie-" + str(state["exchanges"])
        assert request.headers["X-CSRF-Token"] == "private-csrf-" + str(state["exchanges"])
        state["calls"].append(request)
        if state["disconnect"]:
            raise httpx.ReadError("private transport interruption", request=request)
        if state["expire"]:
            state["expire"] = False
            return httpx.Response(401, json={"detail": "expired"})
        if path == "/api/events":
            state["streams"] += 1
            expected = "epoch:9" if state["streams"] == 1 else "epoch:10"
            assert request.headers["last-event-id"] == expected
            assert request.url.params["project"] == "one-project"
            sequence = 9 + state["streams"]
            return httpx.Response(200, text=f'id: epoch:{sequence}\nevent: change\ndata: {{"seq":{sequence}}}\n\n',
                                  headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json={"ok": True})

    client = BrokerClient(settings, transport=httpx.MockTransport(handler))
    try:
        yield client, state, settings
    finally:
        client.close()


def test_constructor_does_not_start_or_authenticate_anything(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Constructing a terminal client performed I/O")
    monkeypatch.setattr(cockpit, "ensure_cockpit", forbidden)
    monkeypatch.setattr(cockpit, "cockpit_status", forbidden)
    settings = Settings(tmp_path / "missing")
    BrokerClient(settings).close()
    assert not settings.data_dir.exists()


def test_private_exchange_and_mutation_keep_credentials_out_of_urls(private_client):
    client, state, _ = private_client
    assert client.request("PATCH", "/api/preferences", json={"selected_project": "p"}) == {"ok": True}
    assert state["tickets"] == state["exchanges"] == 1
    assert "csrf" not in str(state["calls"][0].url) and "ticket" not in str(state["calls"][0].url)


def test_optional_query_none_is_omitted_without_dropping_false_zero_empty_or_json_null(private_client):
    client, state, _ = private_client
    params = {"project": None, "worktree": None, "zero": 0, "flag": False, "empty": ""}
    client.request("GET", "/api/inspector/graph", params=params)
    assert dict(state["calls"][-1].url.params) == {"zero": "0", "flag": "false", "empty": ""}
    assert params["project"] is None
    client.request("POST", "/api/preferences", json={"selected_project": None})
    assert json.loads(state["calls"][-1].content) == {"selected_project": None}


def test_expired_session_renews_once_before_retrying_rejected_request(private_client):
    client, state, _ = private_client
    client.request("GET", "/api/state")
    state["expire"] = True
    client.request("POST", "/api/client-runs", json={"idempotency_key": "explicit"})
    assert state["tickets"] == state["exchanges"] == 2
    assert len(state["calls"]) == 3


def test_uncertain_mutation_is_never_retried(private_client):
    client, state, _ = private_client
    state["disconnect"] = True
    with pytest.raises(BrokerError) as error:
        client.request("POST", "/api/client-runs", json={"idempotency_key": "explicit"})
    assert error.value.status_code == 503
    assert len(state["calls"]) == 1


def test_parallel_panel_reads_share_one_private_session(private_client):
    client, state, _ = private_client
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: client.request("GET", "/api/state"), range(24)))
    assert results == [{"ok": True}] * 24
    assert state["tickets"] == state["exchanges"] == 1


@pytest.mark.parametrize("path", ["https://other.invalid/api/state", "//other.invalid/api/state", "/_control/stop"])
def test_public_client_cannot_send_session_to_unrelated_endpoint(private_client, path):
    client, state, _ = private_client
    with pytest.raises(ValueError):
        client.request("GET", path)
    assert state["tickets"] == 0


@pytest.mark.asyncio
async def test_event_reconnect_keeps_durable_cursor_and_project(private_client):
    client, state, _ = private_client
    events = client.events(after={"epoch": "epoch", "seq": 9}, project="one-project")
    try:
        first = await anext(events)
        second = await anext(events)
    finally:
        await events.aclose()
    assert first == {"event": "change", "data": {"seq": 10}, "id": "epoch:10"}
    assert second == {"event": "change", "data": {"seq": 11}, "id": "epoch:11"}
    assert state["streams"] == 2


def test_ensure_broker_does_not_mint_display_ticket_or_open_browser(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data", create=True)
    status = {"available": True, "url": ORIGIN, "pid": 42, "birth": "owned", "version_matches": True}
    starts = []
    monkeypatch.setattr("devbrain.cli.ensure_service", lambda settings, **kwargs: starts.append(kwargs))
    monkeypatch.setattr(cockpit, "cockpit_status", lambda settings: status)
    assert not hasattr(cockpit, "_open_browser"), "The browser launcher was removed in 0.8.0"
    monkeypatch.setattr(cockpit.httpx, "Client", lambda **kwargs: pytest.fail("Headless startup minted a ticket"))
    assert cockpit.ensure_cockpit(settings) == status
    assert starts == [{"background_inference": False}]


def test_new_broker_daemon_drops_parent_tmux_and_native_invocation_markers(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data", create=True)
    ready = {"available": True, "url": ORIGIN, "pid": 42, "birth": "owned", "version_matches": True}
    states = iter([{"available": False}, ready])
    monkeypatch.setattr("devbrain.cli.ensure_service", lambda settings, **kwargs: None)
    monkeypatch.setattr(cockpit, "cockpit_status", lambda settings: next(states))
    for key in ("TMUX", "TMUX_PANE", "CLAUDECODE", "CODEX_THREAD_ID", "DEVBRAIN_SESSION_ID"):
        monkeypatch.setenv(key, "parent-invocation")
    monkeypatch.setenv("CODEX_HOME", "/fixture/native-home")
    calls = []
    monkeypatch.setattr(cockpit.subprocess, "Popen", lambda *args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(pid=42))
    assert cockpit.ensure_cockpit(settings) == ready
    argv = calls[0][0][0]
    environment = calls[0][1]["env"]
    assert argv[1:4] == ["-I", "-m", "devbrain"]
    assert environment["CODEX_HOME"] == "/fixture/native-home"
    assert not {"TMUX", "TMUX_PANE", "CLAUDECODE", "CODEX_THREAD_ID", "DEVBRAIN_SESSION_ID"}.intersection(environment)
