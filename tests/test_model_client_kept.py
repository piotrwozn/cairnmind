"""The service keeps one HTTP client per model server for its life and closes it when it stops; a command-line process
keeps the simple path, a client for each request (#407). Limits, redirects and the origin are the client's, either way."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from cairnmind import service
from cairnmind.config import Settings
from cairnmind.memory.memory_models import MemoryModels
from cairnmind.agents.ollama import Ollama, OllamaUnavailable

VECTOR = {"embeddings": [[0.5, 0.25]]}


class Clients:
    """Every AsyncClient the model client makes: counted, each backed by a recording mock transport."""

    def __init__(self, monkeypatch, handler=None):
        self.made: list[httpx.AsyncClient] = []
        self.requests: list[httpx.Request] = []
        real = httpx.AsyncClient

        def respond(request):
            self.requests.append(request)
            return handler(request) if handler else httpx.Response(200, json=VECTOR)

        def make(**kwargs):
            client = real(transport=httpx.MockTransport(respond), **kwargs)
            self.made.append(client)
            return client
        monkeypatch.setattr("cairnmind.agents.ollama.httpx.AsyncClient", make)


async def test_a_command_line_process_opens_and_closes_a_client_for_each_request(tmp_path, monkeypatch):
    clients = Clients(monkeypatch)
    model = Ollama(Settings(data_dir=tmp_path))
    for _ in range(3):
        await model.embed(["text"])
    assert len(clients.made) == 3 and all(client.is_closed for client in clients.made)


async def test_an_opened_client_is_reused_by_every_request_and_closed_once(tmp_path, monkeypatch):
    clients = Clients(monkeypatch)
    model = Ollama(Settings(data_dir=tmp_path))
    await model.open()
    await model.open()  # already open: nothing new
    for _ in range(4):
        await model.embed(["text"])
    assert len(clients.made) == 1 and not clients.made[0].is_closed and len(clients.requests) == 4
    await model.aclose()
    assert clients.made[0].is_closed
    await model.aclose()  # closed twice is nothing
    await model.embed(["text"])  # after the stop a request opens its own again, as without open()
    assert len(clients.made) == 2 and clients.made[1].is_closed


async def test_the_kept_client_has_the_limits_and_the_origin_of_the_per_request_one(tmp_path, monkeypatch):
    clients = Clients(monkeypatch)
    settings = Settings(data_dir=tmp_path, ask_timeout=120)
    simple, kept = Ollama(settings), Ollama(settings)
    await kept.open()
    await simple.embed(["text"])
    await kept.embed(["text"])
    for client in clients.made:
        assert str(client.base_url).rstrip("/") == settings.ollama_url and client.timeout == httpx.Timeout(120)
        assert client.follow_redirects is False and client.trust_env is False
    # the read time limit of one call (#266) is still that call's own on the kept client
    await kept._request("POST", "/api/embed", json={}, read_timeout=7)
    assert clients.requests[-1].extensions["timeout"]["read"] == 7
    await kept.aclose()


async def test_a_redirect_is_not_followed_on_the_kept_client(tmp_path, monkeypatch):
    clients = Clients(monkeypatch, lambda request: httpx.Response(307, headers={"Location": "http://elsewhere.test/x"}))
    model = Ollama(Settings(data_dir=tmp_path))
    await model.open()
    with pytest.raises(OllamaUnavailable) as caught:
        await model.embed(["private text"])
    assert caught.value.status_code == 307 and [r.url.host for r in clients.requests] == ["127.0.0.1"]
    await model.aclose()


async def test_both_roles_of_split_servers_are_opened_and_closed(tmp_path, monkeypatch):
    clients = Clients(monkeypatch)
    settings = Settings(data_dir=tmp_path)
    chat, embedding = Ollama(settings), Ollama(settings)
    models = MemoryModels(settings, chat, embedding)
    await models.open()
    await models.embed(["text"])
    await models.embed(["text"])
    assert len(clients.made) == 2 and chat._client is not None and embedding._client is not None
    await models.aclose()
    assert all(client.is_closed for client in clients.made) and chat._client is None and embedding._client is None
    same = Ollama(settings)
    await MemoryModels(settings, same, same).open()  # one server for both roles: one client
    assert len(clients.made) == 3
    await same.aclose()


def app_with(tmp_path, **kwargs):
    store = SimpleNamespace(not_writer=None, relative_paths=True)
    return service.create_app(Settings(data_dir=tmp_path, token="t"), start_worker=False, store=store, **kwargs)


def test_the_service_opens_its_models_when_it_starts_and_closes_them_when_it_stops(tmp_path, monkeypatch):
    clients = Clients(monkeypatch)
    app = app_with(tmp_path)
    model = app.state.agent.ollama
    assert isinstance(model, Ollama) and model._client is None  # nothing opened before the service runs
    with TestClient(app):
        assert model._client is not None and clients.made == [model._client]
        kept = model._client
    assert kept.is_closed and model._client is None


def test_a_model_the_caller_passed_in_is_not_opened_or_closed_by_the_service(tmp_path):
    class Caller:
        def open(self):
            raise AssertionError("the service opened a model it does not own")

        aclose = open
    app = app_with(tmp_path, ollama=Caller())
    with TestClient(app):
        pass
