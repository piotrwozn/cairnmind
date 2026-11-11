from __future__ import annotations

import asyncio
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from cairnmind.config import Settings
from cairnmind.service import InferenceQueue, create_app
from cairnmind.store import Store


class LocalModelFixture:
    """Protocol fixture only: production always requires real local Ollama."""

    def __init__(self):
        self.calls = 0

    async def models(self):
        return {"models": []}

    async def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]

    async def chat(self, messages, tools=None, **kwargs):
        self.calls += 1
        prompt = json.loads(messages[1]["content"])
        if not prompt["initial_evidence"]:
            return {"message": {"content": json.dumps({"answer": "Brak źródeł.", "citations": [],
                                                       "unknowns": ["Brak aktualnego kodu."]})}}
        evidence = prompt["initial_evidence"][0]
        return {"message": {"content": json.dumps({
            "answer": "Funkcja authenticate obsługuje logowanie.",
            "citations": [{"source_id": evidence["source_id"], "quote": "def authenticate"}],
            "unknowns": [],
        })}}


@pytest.fixture
def workspace(tmp_path):
    settings = Settings.load(tmp_path / "memory", create=True)
    root = tmp_path / "repo"
    root.mkdir()
    (root / "auth.py").write_text("def authenticate(token):\n    return token == 'valid'\n")
    for args in (("init",), ("config", "user.email", "fixture@example.invalid"),
                 ("config", "user.name", "Fixture"), ("add", "."), ("commit", "-m", "fixture")):
        subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)
    store = Store(settings.data_dir)
    project = store.add_project("demo", root)
    return settings, store, project, root


def test_config_concurrent_start_preserves_one_token(tmp_path):
    root = tmp_path / "config"
    with ThreadPoolExecutor(max_workers=12) as pool:
        tokens = list(pool.map(lambda _: Settings.load(root, create=True).token, range(36)))
    assert len(set(tokens)) == 1
    assert Settings.load(root).token == tokens[0]
    assert (root / "config.json").stat().st_mode & 0o777 == 0o600


def test_private_endpoints_require_local_bearer(workspace):
    settings, store, project, root = workspace
    with TestClient(create_app(settings, start_worker=False, ollama=LocalModelFixture())) as client:
        assert client.get("/health").json()["service"] == "cairnmind"
        assert client.get("/projects").status_code == 401
        assert client.get("/projects", headers={"Authorization": "Bearer wrong"}).status_code == 401
        result = client.get("/projects", headers={"Authorization": f"Bearer {settings.token}"})
        assert result.json()[0]["id"] == project["id"]
        assert settings.token not in client.get("/health").text


@pytest.mark.parametrize("expected_field", ["packet_id", "expected_packet_id"])
def test_exact_startup_context_does_not_refresh_a_stale_packet(workspace, expected_field):
    from cairnmind.sessions import SessionManager

    settings, store, project, root = workspace
    initial = SessionManager(store, settings).prepare(project["id"], "codex", timeout=0)
    assert initial["status"] == "ready"
    sid, packet = initial["session_id"], initial["packet"]
    headers = {"Authorization": f"Bearer {settings.token}"}

    def identity():
        return store.query("SELECT epoch,packet_id,ack_id,status FROM sessions WHERE id=:id", {"id": sid})

    before = identity()
    with TestClient(create_app(settings, start_worker=False, ollama=LocalModelFixture())) as client:
        exact = {"session_id": sid, expected_field: packet["id"]}
        result = client.post("/sessions/context", json=exact, headers=headers)
        assert result.status_code == 200 and result.json()["packet"] == packet
        (root / "auth.py").write_text("def authenticate(token):\n    return False\n")
        rejected = client.post("/sessions/context", json=exact, headers=headers)
        assert rejected.status_code == 409
        assert identity() == before
        packets = store.query("SELECT id FROM resume_packets WHERE session_id=:id", {"id": sid})
        assert packets == [{"id": packet["id"]}]
        refreshed = client.post("/sessions/context", json={"session_id": sid}, headers=headers)
        assert refreshed.status_code == 200
        current = refreshed.json()
        assert current["status"] == "ready" and current["packet"]["id"] != packet["id"]
        assert current["epoch"] == initial["epoch"] + 1
        assert store.query("SELECT id FROM resume_acks WHERE session_id=:id", {"id": sid}) == []


@pytest.mark.parametrize("body", [{"packet_id": "wrong-packet"},
                                 {"expected_packet_id": "wrong-packet"},
                                 {"packet_id": ""}, {"expected_packet_id": ""},
                                 {"packet_id": "one", "expected_packet_id": "another"}])
def test_wrong_expected_context_identity_cannot_replace_current_packet(workspace, body):
    from cairnmind.sessions import SessionManager

    settings, store, project, root = workspace
    manager = SessionManager(store, settings)
    initial = manager.prepare(project["id"], "codex", timeout=0)
    sid = initial["session_id"]
    before = store.query("SELECT epoch,packet_id,ack_id,status FROM sessions WHERE id=:id", {"id": sid})
    with TestClient(create_app(settings, start_worker=False, ollama=LocalModelFixture())) as client:
        result = client.post("/sessions/context", json={"session_id": sid, **body},
                             headers={"Authorization": f"Bearer {settings.token}"})
        assert result.status_code == 409
    assert store.query("SELECT epoch,packet_id,ack_id,status FROM sessions WHERE id=:id", {"id": sid}) == before
    assert manager.status(sid)["packet"]["id"] == initial["packet"]["id"]


def test_real_http_agent_retrieval_cache_and_uncommitted_change(workspace):
    settings, store, project, root = workspace
    model = LocalModelFixture()
    headers = {"Authorization": f"Bearer {settings.token}"}
    with TestClient(create_app(settings, start_worker=False, ollama=model)) as client:
        body = {"project": "demo", "question": "Gdzie jest authenticate?"}
        first = client.post("/ask", json=body, headers=headers)
        assert first.status_code == 200, first.text
        data = first.json()
        assert data["status"] == "complete", data
        assert data["sources"][0]["quote"] == "def authenticate"
        assert data["cached"] is False
        repeated = client.post("/ask", json=body, headers=headers).json()
        assert repeated["cached"] is True
        assert model.calls == 1
        (root / "auth.py").write_text("def authenticate(token):\n    return token == 'changed'\n")
        changed = client.post("/ask", json=body, headers=headers).json()
        assert changed["cached"] is False
        assert changed["sources"][0]["sha256"] != data["sources"][0]["sha256"]
        assert model.calls == 2


def test_typed_answer_contract_is_validated_and_part_of_cache_key(workspace):
    class TypedModel(LocalModelFixture):
        async def chat(self, messages, tools=None, **kwargs):
            self.calls += 1
            prompt = json.loads(messages[1]["content"])
            schema = prompt.get("answer_schema")
            facts = ({"symbol": "authenticate"} if "symbol" in schema["properties"] else {"line": 1}) if schema else "authenticate"
            source = prompt["initial_evidence"][0]
            return {"message": {"content": json.dumps({"answer": facts,
                "citations": [{"source_id": source["source_id"], "quote": "def authenticate"}], "unknowns": []})}}

    settings, store, project, root = workspace
    model = TypedModel()
    headers = {"Authorization": f"Bearer {settings.token}"}
    def contract(field, kind):
        return {"type": "object", "properties": {field: {"type": kind}},
                "required": [field], "additionalProperties": False}
    with TestClient(create_app(settings, start_worker=False, ollama=model)) as client:
        body = {"project": "demo", "question": "Podaj symbol lub numer linii funkcji authenticate.",
                "answer_schema": contract("symbol", "string")}
        first = client.post("/ask", json=body, headers=headers)
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "complete"
        assert json.loads(first.json()["answer"]) == {"symbol": "authenticate"}
        assert client.post("/ask", json=body, headers=headers).json()["cached"] is True
        body["answer_schema"] = contract("line", "integer")
        second = client.post("/ask", json=body, headers=headers).json()
        assert second["cached"] is False
        assert json.loads(second["answer"]) == {"line": 1}
        body.pop("answer_schema")
        third = client.post("/ask", json=body, headers=headers).json()
        assert third["cached"] is False and third["answer"] == "authenticate"
        assert model.calls == 3
        invalid = client.post("/ask", json={**body, "answer_schema": {"$ref": "https://example.invalid/private"}},
                              headers=headers)
        assert invalid.status_code == 400
        assert model.calls == 3


def test_clients_share_task_and_cannot_overwrite_or_skip_verification(workspace):
    settings, store, project, root = workspace
    source = store.snapshot(project["id"], "manual://plan", "Add tests before completion.", "manual")
    headers = {"Authorization": f"Bearer {settings.token}"}
    with TestClient(create_app(settings, start_worker=False, ollama=LocalModelFixture())) as client:
        payload = {"project": "demo", "record": {
            "kind": "task", "content": "Add login", "metadata": {"state": "todo", "requires_test": True}},
            "sources": [source["id"]]}
        task = client.post("/remember", json=payload, headers=headers).json()
        assert task["metadata"]["state"] == "todo"
        # Claude sees the record written by Codex, including its shared optimistic version.
        restored = client.get("/context", params={"project": "demo"}, headers=headers).json()
        assert any(r["id"] == task["id"] for r in restored["records"])
        payload["record"] = {"id": task["id"], "expected_version": task["version"],
                             "metadata": {"state": "needs_test"}}
        testing = client.post("/remember", json=payload, headers=headers)
        assert testing.status_code == 200, testing.text
        assert testing.json()["metadata"]["state"] == "needs_test"
        assert client.post("/remember", json=payload, headers=headers).status_code == 409
        payload["record"]["expected_version"] = testing.json()["version"]
        payload["record"]["metadata"]["state"] = "done"
        rejected = client.post("/remember", json=payload, headers=headers)
        assert rejected.status_code == 400
        assert "fresh" in rejected.text.lower() or "verification" in rejected.text.lower()


def test_removed_source_invalidates_cached_answer(workspace):
    settings, store, project, root = workspace
    model = LocalModelFixture()
    headers = {"Authorization": f"Bearer {settings.token}"}
    with TestClient(create_app(settings, start_worker=False, ollama=model)) as client:
        body = {"project": "demo", "question": "Gdzie jest authenticate?"}
        assert client.post("/ask", json=body, headers=headers).json()["status"] == "complete"
        (root / "auth.py").unlink()
        result = client.post("/ask", json=body, headers=headers).json()
        assert result["cached"] is False
        assert result["status"] == "partial"
        assert model.calls == 2


def test_cache_tracks_profiles_and_ignored_verification_inputs(workspace):
    settings, store, project, root = workspace
    (root / ".gitignore").write_text(".devbrain/\nsecret-input.txt\n")
    (root / "secret-input.txt").write_text("first")
    (root / ".devbrain").mkdir()
    profile = root / ".devbrain/checks.toml"
    profile.write_text('[checks.unit]\nargv=["true"]\nextra_inputs=["secret-input.txt"]\n')
    model = LocalModelFixture()
    headers = {"Authorization": f"Bearer {settings.token}"}
    with TestClient(create_app(settings, start_worker=False, ollama=model)) as client:
        body = {"project": "demo", "question": "Gdzie jest authenticate?"}
        assert client.post("/ask", json=body, headers=headers).json()["cached"] is False
        assert client.post("/ask", json=body, headers=headers).json()["cached"] is True
        stamp = store.cache_stamp(project["id"])
        profile.write_text('[checks.unit]\nargv=["false"]\nextra_inputs=["secret-input.txt"]\n')
        assert client.post("/ask", json=body, headers=headers).json()["cached"] is False
        assert store.cache_stamp(project["id"]) == stamp  # Both changes are outside RAG indexing.
        assert client.post("/ask", json=body, headers=headers).json()["cached"] is True
        (root / "secret-input.txt").write_text("changed")
        assert client.post("/ask", json=body, headers=headers).json()["cached"] is False
        assert model.calls == 3


def test_embedding_digest_rebuild_preserves_sources(workspace):
    from cairnmind.ingest import index_project
    from cairnmind.service import schedule_embeddings
    settings, store, project, root = workspace
    index_project(store, project["id"])
    chunk = store.query("SELECT id FROM chunks LIMIT 1")[0]["id"]
    store.command("INSERT INTO embeddings CONTENT :doc", {"doc": {
        "id": "old-vector", "chunk_id": chunk, "project_id": project["id"],
        "model": "embed", "namespace": "embed@old", "dimension": 2, "vector": [1.0, 0.0],
    }})
    before = store.query("SELECT count(*) AS n FROM sources")[0]["n"]
    assert schedule_embeddings(store, "embed@new") > 0
    assert schedule_embeddings(store, "embed@new") > 0  # Queue IDs dedupe, missing vectors still reported.
    jobs = store.query("SELECT FROM jobs WHERE dedupe_key LIKE :pattern",
                       {"pattern": "embedding-namespace:embed@new:%"})
    assert len(jobs) == store.query("SELECT count(*) AS n FROM chunks")[0]["n"]
    assert store.query("SELECT count(*) AS n FROM sources")[0]["n"] == before
    store.command("UPDATE jobs SET state='completed' WHERE dedupe_key LIKE :pattern",
                  {"pattern": "embedding-namespace:embed@new:%"})
    schedule_embeddings(store, "embed@new")
    assert store.query("SELECT count(*) AS n FROM jobs WHERE state='completed' AND dedupe_key LIKE :pattern",
                       {"pattern": "embedding-namespace:embed@new:%"})[0]["n"] == 0


@pytest.mark.asyncio
async def test_inference_serializes_and_prioritizes_foreground():
    scheduler = InferenceQueue()
    runner = asyncio.create_task(scheduler.run())
    started, release = asyncio.Event(), asyncio.Event()
    order = []

    async def first():
        started.set()
        await release.wait()
        order.append("first")

    async def operation(label):
        order.append(label)
        return label

    one = asyncio.create_task(scheduler.submit(first, priority=10))
    await started.wait()
    background = asyncio.create_task(scheduler.submit(lambda: operation("background"), priority=10))
    foreground = asyncio.create_task(scheduler.submit(lambda: operation("foreground"), priority=0))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(one, background, foreground)
    assert order == ["first", "foreground", "background"]
    runner.cancel()
    await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_request_stops_its_active_inference():
    scheduler = InferenceQueue()
    runner = asyncio.create_task(scheduler.run())
    started, stopped = asyncio.Event(), asyncio.Event()

    async def long_operation():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    request = asyncio.create_task(scheduler.submit(long_operation))
    await started.wait()
    request.cancel()
    await asyncio.gather(request, return_exceptions=True)
    await asyncio.wait_for(stopped.wait(), 1)
    assert await scheduler.submit(lambda: asyncio.sleep(0, result="alive")) == "alive"
    runner.cancel()
    await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_cancels_worker_and_active_consumer_without_hanging():
    scheduler = InferenceQueue()
    runner = asyncio.create_task(scheduler.run())
    started = asyncio.Event()

    async def operation():
        started.set()
        await asyncio.Event().wait()

    consumer = asyncio.create_task(scheduler.submit(operation))
    await started.wait()
    runner.cancel()
    consumer.cancel()
    await asyncio.wait_for(asyncio.gather(runner, consumer, return_exceptions=True), 1)


def test_service_refuses_non_loopback_bind(workspace):
    settings, store, project, root = workspace
    settings.host = "0.0.0.0"
    with pytest.raises(ValueError, match="loopback"):
        create_app(settings, start_worker=False)


def test_background_scan_reports_missing_root_in_status(workspace):
    settings, store, project, root = workspace
    root.rename(root.with_name("temporarily-moved"))
    settings.poll_seconds = 0.05
    headers = {"Authorization": f"Bearer {settings.token}"}
    with TestClient(create_app(settings, ollama=LocalModelFixture())) as client:
        for _ in range(60):
            status = client.get("/status", headers=headers).json()
            if status["background"].get("last_scan_at"):
                break
            time.sleep(0.02)
        assert "unavailable" in status["background"]["scanner_error"].lower()
        assert status["background"]["scan_errors"]
