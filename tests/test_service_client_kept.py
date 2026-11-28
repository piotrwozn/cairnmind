"""The MCP bridge keeps one HTTP client to the service for its life; a command, or a model client, builds none of the
TLS machinery a plain http address never uses (#415). What the client may reach is the same either way."""
from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from cairnmind import cli
from cairnmind.agents.ollama import Ollama
from cairnmind.config import Settings, tls_verify
from cairnmind.mcp import mcp_server
from cairnmind.mcp.mcp_server import ServiceClient, create_server

UNAVAILABLE = "Cairnmind service is unavailable (ConnectError); start `cairnmind serve` or use the launcher."


@pytest.fixture
def settings(tmp_path):
    return Settings.load(tmp_path / "data", create=True)


class Clients:
    """Every AsyncClient the bridge makes, each over a recording mock transport."""

    def __init__(self, monkeypatch, handler=None):
        self.made: list[httpx.AsyncClient] = []
        self.requests: list[httpx.Request] = []
        real = httpx.AsyncClient

        def respond(request):
            self.requests.append(request)
            return handler(request) if handler else httpx.Response(200, json={"ok": True})

        def make(**kwargs):
            client = real(transport=httpx.MockTransport(respond), **kwargs)
            self.made.append(client)
            return client
        monkeypatch.setattr(mcp_server.httpx, "AsyncClient", make)
        monkeypatch.setattr(mcp_server, "other_version", lambda settings: None)  # its own health probe is a socket


def bridge(settings):
    return ServiceClient(settings, session_id="s", packet_id="p")


async def test_the_bridge_keeps_one_client_across_requests_and_closes_it_at_the_end(settings, monkeypatch):
    clients = Clients(monkeypatch)
    service = bridge(settings)
    assert clients.made == []  # nothing is made before the first request
    for _ in range(5):
        await service.post("/ask", {"project": "p", "question": "q"})
    assert len(clients.made) == 1 and not clients.made[0].is_closed and len(clients.requests) == 5
    await service.aclose()
    assert clients.made[0].is_closed
    await service.aclose()  # closed twice is nothing


async def test_a_bridge_that_sends_nothing_makes_and_closes_no_client(settings, monkeypatch):
    clients = Clients(monkeypatch)
    service = bridge(settings)
    await service.aclose()
    assert clients.made == []


async def test_the_server_closes_the_bridge_client_when_it_stops(settings, monkeypatch):
    clients = Clients(monkeypatch)
    service = bridge(settings)
    server = create_server(settings, service)
    async with server.settings.lifespan(server):
        await service.post("/ask", {"project": "p", "question": "q"})
        assert not clients.made[0].is_closed
    assert clients.made[0].is_closed


def test_a_client_made_on_one_event_loop_is_never_used_on_another(settings, monkeypatch):
    clients = Clients(monkeypatch)
    service = bridge(settings)
    for _ in range(2):
        asyncio.run(service.post("/ask", {"project": "p", "question": "q"}))
    assert len(clients.made) == 2 and clients.made[0] is not clients.made[1]


async def test_every_request_keeps_its_own_time_limit_token_and_origin(settings, monkeypatch):
    clients = Clients(monkeypatch, lambda request: httpx.Response(200, json={
        "session_id": "s", "packet": {"id": "p", "hash": "h"}}))
    service = bridge(settings)
    await service.post("/ask", {"project": "p", "question": "q"})
    await service.post("/sessions/context", {})
    await service.get("/checks", {"project": "p"})
    seen = [(request.url.path, request.extensions["timeout"]["read"]) for request in clients.requests]
    assert seen == [("/ask", settings.ask_timeout + 15), ("/sessions/context", 45), ("/checks", 15)]
    assert all(request.headers["Authorization"] == f"Bearer {settings.token}" and request.url.host == "127.0.0.1"
               for request in clients.requests)
    assert len(clients.made) == 1 and clients.made[0].follow_redirects is False and clients.made[0].trust_env is False


async def test_a_redirect_is_not_followed_by_the_kept_client(settings, monkeypatch):
    clients = Clients(monkeypatch, lambda request: httpx.Response(307, headers={"Location": "http://elsewhere.test/x"}))
    service = bridge(settings)
    with pytest.raises(RuntimeError, match=r"rejected the request \(307\)"):
        await service.post("/ask", {"project": "p", "question": "q"})
    assert [request.url.host for request in clients.requests] == ["127.0.0.1"]
    await service.aclose()


class Restartable:
    """A real HTTP/1.1 server with kept-alive connections that can be stopped and started again on its port."""

    def __init__(self, port=0):
        owner = self
        self.connections: list[socket.socket] = []
        self.seen: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                owner.seen.append(self.path)
                data = json.dumps({"answer": str(owner.port)}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def setup(self):
                super().setup()
                owner.connections.append(self.connection)

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        for connection in self.connections:  # a stopped service takes its kept connections with it
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)


async def test_a_service_restarted_between_two_requests_is_reconnected_to(settings, monkeypatch):
    monkeypatch.setattr(mcp_server, "other_version", lambda settings: None)  # its own health probe is a socket
    first = Restartable()
    settings.port = first.port
    service = bridge(settings)
    assert await service.post("/ask", {"project": "p", "question": "q"}) == {"answer": str(first.port)}
    assert await service.post("/ask", {"project": "p", "question": "q"}) == {"answer": str(first.port)}
    first.stop()
    # nothing listens: the same refusal as with a client for each request
    with pytest.raises(RuntimeError) as caught:
        await service.post("/ask", {"project": "p", "question": "q"})
    assert str(caught.value) == UNAVAILABLE
    second = Restartable(first.port)
    try:
        assert await service.post("/ask", {"project": "p", "question": "q"}) == {"answer": str(first.port)}
        assert second.seen == ["/ask"]
    finally:
        second.stop()
        await service.aclose()


def test_only_a_plain_http_address_goes_without_the_certificate_store():
    assert tls_verify("http://127.0.0.1:8765") is False
    assert tls_verify("HTTP://localhost:11434") is False
    assert tls_verify("https://models.example.com") is True
    assert tls_verify("ftp://elsewhere") is True  # anything else is verified, as before


def test_a_plain_http_client_builds_no_certificate_store_and_an_https_one_does(settings, monkeypatch):
    loaded = []
    real = ssl.create_default_context
    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **k: (loaded.append(1), real(*a, **k))[1])
    Ollama(settings)._new_client()
    assert loaded == []
    Ollama(settings, base_url="https://models.example.com")._new_client()
    assert loaded == [1]


def test_a_command_builds_its_client_without_the_certificate_store_for_the_service_address(settings, monkeypatch):
    made = []
    real = httpx.Client

    def make(**kwargs):
        made.append(kwargs)
        return real(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})), **kwargs)
    monkeypatch.setattr(cli.httpx, "Client", make)
    monkeypatch.setattr(cli, "_refuse_other_version", lambda settings: None)  # its own health probe is a socket
    settings.port = 1  # a request a test forgot to fake is refused at once and never reaches a real service
    assert settings.url.startswith("http://127.0.0.1:")
    assert cli.request(settings, "GET", "/jobs", start_service=False, session=False) == {"ok": True}
    assert made and all(kwargs["verify"] is False and kwargs["trust_env"] is False for kwargs in made)


def test_the_kept_client_drops_an_idle_connection_before_the_service_closes_it(settings):
    import uvicorn
    assert uvicorn.Config(lambda *a: None).timeout_keep_alive > mcp_server.KEEPALIVE_EXPIRY  # what cli.py serves with

    async def expiry():
        return bridge(settings)._client()._transport._pool._keepalive_expiry
    assert asyncio.run(expiry()) == mcp_server.KEEPALIVE_EXPIRY


def test_requests_at_the_moment_the_service_closes_an_idle_connection_are_never_unavailable(settings, monkeypatch):
    """Server keep-alive scaled to 0.2 s; idle gaps sweep 0.94 to 1.06 times of it. Without the shorter client expiry about
    5 in 100 of these failed with a false 'unavailable (ReadError)'."""
    import time

    import uvicorn

    from cairnmind import __version__
    from cairnmind.cli_cmds.service_version import instance_id
    keep = 0.2
    monkeypatch.setattr(mcp_server, "KEEPALIVE_EXPIRY", keep * 0.8)
    health = json.dumps({"service": "cairnmind", "version": __version__, "instance": instance_id(settings)}).encode()

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        while (await receive()).get("more_body"):
            pass
        body = health if scope["path"] == "/health" else b'{"answer": "x"}'
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        settings.port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=settings.port, log_level="error",
                                           timeout_keep_alive=keep))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)

    async def sweep():
        service = bridge(settings)
        refused = []
        try:
            for i in range(120):
                try:
                    await service.post("/ask", {"project": "p", "question": "q"})
                except RuntimeError as exc:
                    refused.append(str(exc))
                await asyncio.sleep(keep * (0.94 + 0.12 * (i % 25) / 24))
        finally:
            await service.aclose()
        return refused
    try:
        assert asyncio.run(sweep()) == []
    finally:
        server.should_exit = True
        thread.join(5)
