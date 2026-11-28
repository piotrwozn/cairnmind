"""Changed weights behind the same model name on an OpenAI-compatible embedding server (#212).

No model and no network: the embedding server is an httpx.MockTransport whose answer the test changes.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import replace

import httpx
import pytest
from test_knowledge import document
from typer.testing import CliRunner

from cairnmind.agents.ollama import Ollama, OllamaUnavailable
from cairnmind.agents.retrieval import Retriever
from cairnmind.config import Settings
from cairnmind.memory import memory_fingerprint, memory_models
from cairnmind.memory.memory_openai import OpenAICompatible
from cairnmind.store import Store

LMSTUDIO = {"provider": "openai_compatible", "base_url": "http://127.0.0.1:1234/v1"}
OLD, NEW = [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]


class Server:
    """An OpenAI-compatible embedding server; ``vector`` is what its weights answer for every text."""

    def __init__(self, vector):
        self.vector, self.bodies, self.paths = list(vector), [], []

    def __call__(self, request):
        self.paths.append(request.url.path)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "text-embed"}]})
        body = json.loads(request.content)
        self.bodies.append(body)
        return httpx.Response(200, json={"data": [{"index": i, "embedding": self.vector}
                                                  for i, _ in enumerate(body["input"])]})


def settings_for(tmp_path):
    return Settings(data_dir=tmp_path / "data", embedding_model="text-embed", memory_embedding=dict(LMSTUDIO))


def client_for(settings, server):
    http = httpx.AsyncClient(base_url=LMSTUDIO["base_url"], transport=httpx.MockTransport(server))
    return OpenAICompatible(settings, "embedding", memory_models.profile(settings, "embedding"), http)


async def test_a_server_that_changes_its_answer_is_detected_at_the_next_start(tmp_path):
    settings, server = settings_for(tmp_path), Server(OLD)
    first = client_for(settings, server)
    assert (await memory_fingerprint.check(first))["status"] == "recorded"
    base = await first.model_identity("text-embed")
    assert base.startswith("text-embed@openai_compatible:")
    await first.aclose()

    server.vector = NEW  # other weights, the same name and the same dimension
    second = client_for(settings, server)  # the next start of the service
    result = await memory_fingerprint.check(second)
    assert result["status"] == "changed" and result["identity"] is None and result["similarity"] == 0.0
    with pytest.raises(OllamaUnavailable) as stopped:
        await second.model_identity("text-embed")
    assert stopped.value.error_code == memory_fingerprint.CHANGED
    # Another process (doctor, a command) reads the same mark.
    identity, block = memory_fingerprint.doctor_block(settings.data_dir, base)
    assert identity is None and block["changed"] and "rebuild-vectors" in block["next"]

    # rebuild-vectors accepts the new weights: a new namespace, never the earlier vectors.
    accepted = await memory_fingerprint.check(second, accept=True)
    assert accepted["status"] == "accepted" and accepted["generation"] == 1
    assert await second.model_identity("text-embed") == base + "~w1"
    assert (await memory_fingerprint.check(second))["status"] == "same"
    await second.aclose()


async def test_float_noise_is_the_same_weights_and_earlier_weights_coming_back_clear_the_mark(tmp_path):
    settings, server = settings_for(tmp_path), Server(OLD)
    client = client_for(settings, server)
    await memory_fingerprint.check(client)
    base = await client.model_identity("text-embed")
    server.vector = [1.0, 1e-5, -1e-5]
    assert (await memory_fingerprint.check(client))["status"] == "same"
    server.vector = NEW
    assert (await memory_fingerprint.check(client))["status"] == "changed"
    server.vector = OLD  # the earlier weights are served again before anyone rebuilt
    assert (await memory_fingerprint.check(client))["status"] == "same"
    assert await client.model_identity("text-embed") == base
    server.vector = [1.0, 0.0]  # another dimension is other weights too
    assert (await memory_fingerprint.check(client))["status"] == "changed"
    await client.aclose()


async def test_the_fixed_request_holds_no_memory_content(tmp_path):
    settings, server = settings_for(tmp_path), Server(OLD)
    client = client_for(settings, server)
    await memory_fingerprint.check(client)
    await client.aclose()
    assert server.bodies == [{"model": "text-embed", "input": [memory_fingerprint.FINGERPRINT_TEXT],
                              "encoding_format": "float"}]
    assert "Cairnmind" in memory_fingerprint.FINGERPRINT_TEXT and len(memory_fingerprint.FINGERPRINT_TEXT) < 120
    stored = json.loads((settings.data_dir / memory_fingerprint.FILE).read_text())
    assert memory_fingerprint.FINGERPRINT_TEXT not in json.dumps(stored)  # only the vector is kept
    assert str(settings.data_dir) not in json.dumps(stored) and "127.0.0.1" not in json.dumps(stored)


async def test_an_unreadable_record_file_is_a_first_check_and_a_failed_request_stores_nothing(tmp_path):
    settings = settings_for(tmp_path)
    settings.data_dir.mkdir(parents=True)
    (settings.data_dir / memory_fingerprint.FILE).write_text('{"version": 1, "records": {"x": {"vector": []}}}')
    client = client_for(settings, lambda request: httpx.Response(503, json={"error": "busy"}))
    with pytest.raises(OllamaUnavailable):
        await memory_fingerprint.check(client)
    await client.aclose()
    assert "x" in (settings.data_dir / memory_fingerprint.FILE).read_text()  # untouched
    good = client_for(settings, Server(OLD))
    assert (await memory_fingerprint.check(good))["status"] == "recorded"
    await good.aclose()


def test_the_local_ollama_needs_no_fingerprint(tmp_path):
    settings = Settings(data_dir=tmp_path)
    assert memory_fingerprint.client_of(memory_models.from_settings(settings)) is None
    ollama_profile = Settings(data_dir=tmp_path, memory_embedding={"provider": "ollama",
                                                                  "base_url": "http://127.0.0.1:11435"})
    assert memory_fingerprint.client_of(memory_models.from_settings(ollama_profile)) is None
    chat_only = Settings(data_dir=tmp_path, memory_chat=dict(LMSTUDIO))
    models = memory_models.from_settings(chat_only)
    assert type(models.embedding_client) is Ollama and memory_fingerprint.client_of(models) is None
    assert memory_fingerprint.client_of(memory_models.from_settings(settings_for(tmp_path))) is not None


def test_the_service_start_with_the_local_ollama_sends_no_extra_request(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from cairnmind.service import create_app
    paths = []

    def ollama(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={"models": [{"name": "x", "digest": "a" * 64}]})

    real = httpx.AsyncClient
    monkeypatch.setattr("cairnmind.agents.ollama.httpx.AsyncClient",
                        lambda **kwargs: real(**kwargs, transport=httpx.MockTransport(ollama)))
    Store(tmp_path / "data")
    settings = Settings.load(tmp_path / "data", create=True)
    with TestClient(create_app(settings, start_worker=False)) as client:
        client.get("/health")
    assert "/api/embed" not in paths and not (tmp_path / "data" / memory_fingerprint.FILE).exists()


def wait_for_check(client, headers):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        found = client.get("/status", headers=headers).json()["background"].get("embedding_fingerprint")
        if found and found["status"] != "unchecked":
            return found
        time.sleep(0.05)
    raise AssertionError("the start check did not answer")


def test_the_service_checks_at_start_and_holds_new_vectors_while_changed(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from cairnmind.service import create_app
    server = Server(OLD)
    real = httpx.AsyncClient
    monkeypatch.setattr("cairnmind.agents.ollama.httpx.AsyncClient",
                        lambda **kwargs: real(**kwargs, transport=httpx.MockTransport(server)))
    Store(tmp_path / "data")
    settings = replace(Settings.load(tmp_path / "data", create=True), embedding_model="text-embed",
                       memory_embedding=dict(LMSTUDIO))
    headers = {"Authorization": f"Bearer {settings.token}"}
    for start, vector, status in ((1, OLD, "recorded"), (2, NEW, "changed")):
        server.vector = vector
        app = create_app(settings, start_worker=False)
        with TestClient(app) as client:
            assert wait_for_check(client, headers)["status"] == status, start
    assert server.bodies == [{"model": "text-embed", "input": [memory_fingerprint.FINGERPRINT_TEXT],
                              "encoding_format": "float"}] * 2


class Chat:
    """The chat role, on its own server: it keeps working while the embedding role is held."""

    def __init__(self):
        self.extracted = []

    async def models(self):
        return {"models": []}

    async def model_identity(self, name):
        return name + "@chat"

    async def chat(self, messages, tools=None, **kwargs):
        self.extracted.append(json.loads(messages[-1]["content"])["source_id"])
        return {"message": {"content": '{"records":[]}'}}

    async def aclose(self):
        pass


def test_the_worker_makes_no_vector_while_the_weights_are_changed(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from cairnmind.service import create_app
    server = Server(NEW)
    settings = replace(Settings.load(tmp_path / "data", create=True), embedding_model="text-embed",
                       memory_embedding=dict(LMSTUDIO), poll_seconds=0.05)
    root = tmp_path / "project"
    root.mkdir()
    store = Store(settings.data_dir)
    project = store.add_project("example", root)
    source, chunk = document(store, project, "Refresh token expiration policy")
    store.enqueue("extract", project["id"], {"source_id": source["id"]}, "extract:" + source["id"])
    embedding = client_for(settings, server)
    base = f"text-embed@{embedding.tag}"
    settings.data_dir.joinpath(memory_fingerprint.FILE).write_text(json.dumps({"version": 1, "records": {
        base: {"vector": OLD, "generation": 0}}}))  # what the earlier weights answered
    chat = Chat()
    headers = {"Authorization": f"Bearer {settings.token}"}
    with TestClient(create_app(settings, ollama=memory_models.MemoryModels(settings, chat, embedding))) as client:
        assert wait_for_check(client, headers)["status"] == "changed"
        deadline = time.monotonic() + 20
        while source["id"] not in chat.extracted and time.monotonic() < deadline:
            time.sleep(0.05)
        assert source["id"] in chat.extracted  # the chat role was not held
        time.sleep(0.5)
        background = client.get("/status", headers=headers).json()["background"]
    assert [body["input"] for body in server.bodies] == [[memory_fingerprint.FINGERPRINT_TEXT]]
    assert store.query("SELECT count(*) AS total FROM embeddings")[0]["total"] == 0
    assert "rebuild-vectors" in background["model_error"] and background.get("embedding_namespace") is None
    assert not store.query("SELECT id FROM jobs WHERE kind='embed' AND payload.chunk_id=:chunk AND state<>'pending'",
                           {"chunk": chunk})


class HeldServer(Server):
    """Holds back its answer to the fixed request until ``release`` is set; chunks are embedded at once."""

    def __init__(self, vector):
        super().__init__(vector)
        self.release = threading.Event()

    async def __call__(self, request):
        if request.url.path != "/v1/models" and json.loads(request.content)["input"] == [
                memory_fingerprint.FINGERPRINT_TEXT]:
            while not self.release.is_set():
                await asyncio.sleep(0.02)
        return super().__call__(request)


def test_no_vector_is_made_before_the_start_check_answers(tmp_path, monkeypatch):
    # A restart after a swap: the earlier gen-0 namespace must not get vectors of the new weights while
    # the check is still on its way.
    from fastapi.testclient import TestClient

    from cairnmind.service import create_app
    server = HeldServer(OLD)
    settings = replace(Settings.load(tmp_path / "data", create=True), embedding_model="text-embed",
                       memory_embedding=dict(LMSTUDIO), poll_seconds=0.05)
    root = tmp_path / "project"
    root.mkdir()
    store = Store(settings.data_dir)
    project = store.add_project("example", root)
    source, chunk = document(store, project, "Refresh token expiration policy")
    store.enqueue("extract", project["id"], {"source_id": source["id"]}, "extract:" + source["id"])
    embedding = client_for(settings, server)
    settings.data_dir.joinpath(memory_fingerprint.FILE).write_text(json.dumps({"version": 1, "records": {
        f"text-embed@{embedding.tag}": {"vector": OLD, "generation": 0}}}))
    chat = Chat()
    headers = {"Authorization": f"Bearer {settings.token}"}
    total = "SELECT count(*) AS total FROM embeddings"
    with TestClient(create_app(settings, ollama=memory_models.MemoryModels(settings, chat, embedding))) as client:
        deadline = time.monotonic() + 20
        while source["id"] not in chat.extracted and time.monotonic() < deadline:
            time.sleep(0.05)
        assert source["id"] in chat.extracted  # the jobs loop has run while the check was held
        time.sleep(0.5)
        held = client.get("/status", headers=headers).json()["background"]
        assert held.get("embedding_fingerprint") is None and held.get("embedding_namespace") is None
        assert store.query(total)[0]["total"] == 0
        assert [body["input"] for body in server.bodies] == []  # the held request has not been answered
        server.release.set()
        assert wait_for_check(client, headers)["status"] == "same"
        deadline = time.monotonic() + 20
        while store.query(total)[0]["total"] == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
    assert store.query(total)[0]["total"] == 1  # the same weights: the chunk is embedded once checked
    assert server.bodies[0]["input"] == [memory_fingerprint.FINGERPRINT_TEXT]


async def test_search_never_mixes_vectors_of_the_earlier_weights(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = Store(tmp_path / "data")
    project = store.add_project("example", root)
    settings, server = settings_for(tmp_path), Server(OLD)
    client = client_for(settings, server)
    await memory_fingerprint.check(client)
    source, chunk = document(store, project, "Refresh token expiration policy")
    retriever = Retriever(store, settings, client)
    await retriever.embed_pending({"project_id": project["id"], "payload": {"chunk_id": chunk}})
    assert (await retriever.asearch(project["id"], "wygasanie uwierzytelnienia"))[0]["source_id"] == source["id"]

    server.vector = NEW
    assert (await memory_fingerprint.check(client))["status"] == "changed"
    retriever = Retriever(store, settings, client)
    assert await retriever.asearch(project["id"], "wygasanie uwierzytelnienia") == []  # text search only
    assert any("Semantic query unavailable" in warning for warning in retriever.warnings)
    with pytest.raises(OllamaUnavailable):  # no vector of the new weights joins the earlier ones
        await retriever.embed_pending({"project_id": project["id"], "payload": {"chunk_id": chunk}})

    await memory_fingerprint.check(client, accept=True)
    await retriever.embed_pending({"project_id": project["id"], "payload": {"chunk_id": chunk}})
    namespaces = {row["namespace"] for row in store.query("SELECT namespace FROM embeddings")}
    assert len(namespaces) == 2 and any(name.endswith("~w1") for name in namespaces)
    assert (await retriever.asearch(project["id"], "anything"))[0]["source_id"] == source["id"]
    await client.aclose()


def test_rebuild_vectors_checks_first_and_accepts_the_new_weights(tmp_path, monkeypatch):
    from cairnmind import cli
    server = Server(OLD)
    real = httpx.AsyncClient
    monkeypatch.setattr("cairnmind.agents.ollama.httpx.AsyncClient",
                        lambda **kwargs: real(**kwargs, transport=httpx.MockTransport(server)))
    data, root = tmp_path / "data", tmp_path / "project"
    root.mkdir()
    Store(data).add_project("example", root)
    Settings.load(data, create=True)
    memory_models.save_role(data, "embedding", dict(LMSTUDIO), "text-embed")
    runner = CliRunner()
    first = runner.invoke(cli.app, ["--data-dir", str(data), "rebuild-vectors", "example"])
    assert first.exit_code == 0, first.output
    assert json.loads(first.output)["embedding_fingerprint"]["status"] == "recorded"
    server.vector = NEW
    second = runner.invoke(cli.app, ["--data-dir", str(data), "rebuild-vectors", "example"])
    result = json.loads(second.output)
    assert result["embedding_fingerprint"]["status"] == "accepted" and result["namespace"].endswith("~w1")
    assert server.bodies[0]["input"] == [memory_fingerprint.FINGERPRINT_TEXT]


def test_doctor_reports_a_change_and_the_identity_in_use(tmp_path):
    settings = settings_for(tmp_path)

    def server(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "text-embed"}]})
        return httpx.Response(200, json={"models": [{"name": settings.chat_model, "digest": "abc"}]})

    with httpx.Client(transport=httpx.MockTransport(server)) as http:
        before = memory_models.doctor_models(settings, http)["embedding"]
    assert before["fingerprint"]["recorded"] is False and before["identity"].startswith("text-embed@")
    base = before["identity"]
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / memory_fingerprint.FILE).write_text(json.dumps({"version": 1, "records": {
        base: {"vector": OLD, "generation": 2, "changed": True, "changed_at": "2026-11-15T10:00:00Z"}}}))
    with httpx.Client(transport=httpx.MockTransport(server)) as http:
        changed = memory_models.doctor_models(settings, http)["embedding"]
    assert changed["fingerprint"]["changed"] is True and "identity" not in changed
    assert "rebuild-vectors" in changed["fingerprint"]["next"]
    (settings.data_dir / memory_fingerprint.FILE).write_text(json.dumps({"version": 1, "records": {
        base: {"vector": OLD, "generation": 2}}}))
    with httpx.Client(transport=httpx.MockTransport(server)) as http:
        later = memory_models.doctor_models(settings, http)["embedding"]
    assert later["identity"] == base + "~w2" and later["fingerprint"]["changed"] is False
    # The mark holds embeddings whether or not the server answers doctor: it is shown either way.
    (settings.data_dir / memory_fingerprint.FILE).write_text(json.dumps({"version": 1, "records": {
        base: {"vector": OLD, "generation": 2, "changed": True}}}))
    for down in (lambda request: httpx.Response(503), lambda request: httpx.Response(200, json={"data": []})):
        with httpx.Client(transport=httpx.MockTransport(down)) as http:
            unreachable = memory_models.doctor_models(settings, http)["embedding"]
        assert unreachable["fingerprint"]["changed"] is True and "identity" not in unreachable
        assert "rebuild-vectors" in unreachable["fingerprint"]["next"]


async def test_an_evaluation_copy_names_the_models_by_the_installations_record(tmp_path):
    # eval run builds its client for a copy of the settings in a temporary data directory.
    settings = settings_for(tmp_path)
    client = memory_models.from_settings(settings).embedding_client
    client._client = httpx.AsyncClient(base_url=LMSTUDIO["base_url"], transport=httpx.MockTransport(Server(OLD)))
    base = await client.listed_identity("text-embed")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / memory_fingerprint.FILE).write_text(json.dumps({"version": 1, "records": {
        base: {"vector": OLD, "generation": 1}}}))
    copy = replace(settings, data_dir=tmp_path / "evaluation-copy")
    evaluated = memory_models.from_settings(copy, credentials=settings).embedding_client
    evaluated._client = client._client
    assert await evaluated.model_identity("text-embed") == base + "~w1"
    await client.aclose()
