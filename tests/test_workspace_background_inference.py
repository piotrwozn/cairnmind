"""Opening the workspace retains memory services without implicitly running models."""
from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest

from cairnmind.config import Settings
from cairnmind.service import create_app, schedule_embeddings
from cairnmind.store import Store


class MemoryModel:
    def __init__(self):
        self.list_calls = 0
        self.extracted = []
        self.embedded = []
        self.called = threading.Event()

    async def models(self):
        self.list_calls += 1
        self.called.set()
        return {"models": []}

    async def model_identity(self, name):
        return name + "@test"

    async def chat(self, messages, **kwargs):
        self.extracted.append(json.loads(messages[-1]["content"])["source_id"])
        return {"message": {"content": '{"records":[]}'}}

    async def embed(self, texts):
        self.embedded.extend(texts)
        return [[1., 0.] for _ in texts]


@pytest.fixture
def memory(tmp_path):
    settings = Settings.load(tmp_path / "memory", create=True)
    settings.poll_seconds = .05
    store = Store(settings.data_dir)
    projects, sources, jobs = [], [], []
    for name in ("B", "A"):  # Older foreign backlog must not win the next claim.
        root = tmp_path / name
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
        (root / "README.md").write_text("Project " + name)
        subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "commit", "-qm", "Fixture"], check=True, capture_output=True)
        project = store.add_project(name, root)
        source = store.snapshot(project["id"], f"manual://{name}", f"Memory for {name}", "manual")
        job = store.enqueue("extract", project["id"], {"source_id": source["id"]}, "extract:" + source["id"])
        projects.append(project)
        sources.append(source)
        jobs.append(job)
    return settings, store, projects, sources, jobs


def headers(settings):
    return {"Authorization": "Bearer " + settings.token}


def eventually(check, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.02)
    assert check(), "The expected bounded background effect did not occur"


def inference_state(client, settings):
    return client.get("/status", headers=headers(settings)).json()["background"]["inference"]


def test_deferred_open_keeps_scanner_snapshots_and_only_processes_explicit_project(memory, monkeypatch):
    settings, store, (b, a), (source_b, source_a), (job_b, job_a) = memory
    model = MemoryModel()
    ticks = threading.Event()
    monkeypatch.setattr("cairnmind.memory.MemoryManager.tick", lambda self: ticks.set() or {"errors": []})
    app = create_app(settings, background_inference=False, ollama=model)
    with TestClient(app) as client:
        eventually(lambda: ticks.is_set() and client.get("/status", headers=headers(settings)).json()["background"].get("last_scan_at"))
        # Real read routes, without a coding launch or memory-processing command.
        assert client.get("/projects", headers=headers(settings)).status_code == 200
        assert client.get("/memory/status", params={"project": a["id"]}, headers=headers(settings)).status_code == 200
        assert inference_state(client, settings)["mode"] == "deferred"
        assert model.list_calls == 0 and model.extracted == [] and model.embedded == []
        result = client.post("/background-inference/enable", json={"project": a["id"], "worktree": a["root"]}, headers=headers(settings))
        assert result.status_code == 200 and result.json()["project_ids"] == [a["id"]]
        eventually(lambda: source_a["id"] in model.extracted)
        assert source_b["id"] not in model.extracted
        assert "Memory for B" not in model.embedded
        assert store.query("SELECT state,attempts FROM jobs WHERE id=:id", {"id": job_b}) == [{"state": "pending", "attempts": 0}]
        assert client.post("/background-inference/enable", json={"project": a["id"]}, headers=headers(settings)).json()["project_ids"] == [a["id"]]
    # Consent is process-local; restarting a deferred service does not replay it.
    fresh = MemoryModel()
    with TestClient(create_app(settings, background_inference=False, ollama=fresh)) as client:
        assert inference_state(client, settings)["project_ids"] == []
        assert not fresh.called.wait(.1)


@pytest.mark.parametrize("start_worker,background,expected", [(True, True, True), (False, True, False), (False, False, False)])
def test_manual_serve_defaults_and_worker_disabled_remain_compatible(memory, start_worker, background, expected):
    settings, _, _, _, _ = memory
    model = MemoryModel()
    with TestClient(create_app(settings, start_worker=start_worker, background_inference=background, ollama=model)):
        assert model.called.wait(4 if expected else .1) is expected


def test_empty_claim_filter_never_consumes_any_project_and_scoped_repair_stays_scoped(memory):
    settings, store, (b, a), _, (job_b, job_a) = memory
    assert store.claim_job("test", kinds=["extract"], project_ids=[]) is None
    claimed = store.claim_job("test", kinds=["extract"], project_ids=[a["id"]])
    assert claimed["id"] == job_a and claimed["project_id"] == a["id"]
    assert store.query("SELECT state FROM jobs WHERE id=:id", {"id": job_b}) == [{"state": "pending"}]
    before = store.query("SELECT id FROM jobs")
    assert schedule_embeddings(store, "empty-filter", project_ids=[]) == 0
    assert store.query("SELECT id FROM jobs") == before
    schedule_embeddings(store, "only-a", project_ids=[a["id"]])
    assert not store.query("SELECT id FROM jobs WHERE project_id=:p AND dedupe_key LIKE :key",
                           {"p": b["id"], "key": "embedding-namespace:only-a:%"})


def test_enable_requires_auth_registered_scope_and_current_ack_but_ack_alone_does_not_enable(memory):
    settings, store, (b, a), _, _ = memory
    model = MemoryModel()
    with TestClient(create_app(settings, start_worker=False, background_inference=False, ollama=model)) as client:
        endpoint = "/background-inference/enable"
        assert client.post(endpoint, json={"project": a["id"]}).status_code == 401
        assert client.post(endpoint, json={"project": a["id"], "worktree": b["root"]}, headers=headers(settings)).status_code == 400
        prepared = client.post("/sessions/prepare", json={"project": a["id"], "client": "codex"}, headers=headers(settings)).json()
        assert prepared["status"] == "ready", prepared
        sid, packet = prepared["session_id"], prepared["packet"]
        actor = {"project": a["id"], "session_id": sid, "packet_id": packet["id"]}
        assert client.post(endpoint, json=actor, headers=headers(settings)).status_code == 409
        ack = client.post("/sessions/ack", json={"session_id": sid, "packet_id": packet["id"],
            "packet_hash": packet["hash"], "readback": {"plan": None, "task": None}}, headers=headers(settings))
        assert ack.status_code == 200, ack.text
        assert inference_state(client, settings)["project_ids"] == []
        assert client.post(endpoint, json={**actor, "project": b["id"]}, headers=headers(settings)).status_code == 409
        assert client.post(endpoint, json={**actor, "packet_id": "stale"}, headers=headers(settings)).status_code == 409
        assert inference_state(client, settings)["project_ids"] == []
        assert client.post(endpoint, json=actor, headers=headers(settings)).json()["project_ids"] == [a["id"]]
        assert model.list_calls == 0  # start_worker=False is still authoritative.


def test_only_successful_explicit_model_job_retry_enables_its_project(memory):
    settings, store, (b, a), _, (job_b, job_a) = memory
    for pid in (a["id"], b["id"]):
        row = store.claim_job("fixture", kinds=["extract"], project_ids=[pid])
        store.finish_job(row["id"], "synthetic prior failure", worker=row["worker"], expected_attempts=row["attempts"])
    with TestClient(create_app(settings, start_worker=False, background_inference=False, ollama=MemoryModel())) as client:
        body = {"project": a["id"], "expected_attempts": 0}
        assert client.post(f"/jobs/{job_a}/retry", json=body, headers=headers(settings)).status_code == 409
        assert inference_state(client, settings)["project_ids"] == []
        body["expected_attempts"] = 1
        result = client.post(f"/jobs/{job_a}/retry", json=body, headers=headers(settings))
        assert result.status_code == 200, result.text
        assert inference_state(client, settings)["project_ids"] == [a["id"]]
        assert store.query("SELECT state FROM jobs WHERE id=:id", {"id": job_b}) == [{"state": "failed"}]


def test_explicit_ask_and_remember_work_without_enabling_background(memory, monkeypatch):
    settings, _, (_, a), (_, source), _ = memory
    foreground = threading.Event()

    async def answer(self, *args, **kwargs):
        foreground.set()
        await asyncio.sleep(0)
        return {"status": "complete", "answer": "Explicit request processed", "sources": []}

    monkeypatch.setattr("cairnmind.agent.KnowledgeAgent.ask", answer)
    model = MemoryModel()
    with TestClient(create_app(settings, background_inference=False, ollama=model)) as client:
        response = client.post("/ask", json={"project": a["id"], "question": "Explicit question"}, headers=headers(settings))
        assert response.status_code == 200 and foreground.is_set(), response.text
        remembered = client.post("/remember", json={"project": a["id"], "record": {"kind": "fact", "content": "Memory for A"},
                                                     "sources": [source["id"]]}, headers=headers(settings))
        assert remembered.status_code == 200, remembered.text
        assert inference_state(client, settings)["project_ids"] == []
        assert model.list_calls == 0 and model.extracted == []


def test_tui_service_autostart_is_deferred_and_existing_service_is_never_restarted(tmp_path, monkeypatch):
    from cairnmind import cli
    settings = Settings.load(tmp_path / "data", create=True)
    commands = []
    alive = iter([False, True])
    monkeypatch.setattr(cli, "service_alive", lambda settings: next(alive))
    monkeypatch.setattr(cli.subprocess, "Popen", lambda command, **kwargs: commands.append(command) or SimpleNamespace())
    cli.ensure_service(settings, background_inference=False)
    assert commands[0][-2:] == ["serve", "--defer-background-inference"]
    monkeypatch.setattr(cli, "service_alive", lambda settings: True)
    cli.ensure_service(settings, background_inference=False)
    assert len(commands) == 1
    alive = iter([False, True])
    monkeypatch.setattr(cli, "service_alive", lambda settings: next(alive))
    cli.ensure_service(settings)
    assert commands[-1][-1] == "serve"


@pytest.fixture
def broker(tmp_path, monkeypatch):
    from cairnmind.cockpit import create_cockpit_app
    settings = Settings.load(tmp_path / "broker", create=True)
    project = {"id": "project-a", "name": "A", "root": str(tmp_path / "A")}
    calls, starts = [], []
    behavior = {"enable_status": 200, "launch_ok": True, "enable_hook": None}

    async def upstream(request):
        calls.append((request.url.path, json.loads(request.content) if request.content else None))
        if request.url.path == "/projects":
            return httpx.Response(200, json=[project])
        if request.url.path == "/project-roots":
            return httpx.Response(200, json={"roots": [project["root"]]})
        if request.url.path == "/background-inference/enable":
            if behavior["enable_hook"]:
                await behavior["enable_hook"]()
            return httpx.Response(behavior["enable_status"], json={"accepted": True,
                "project_id": project["id"], "worker_running": True})
        return httpx.Response(200, json={"status": "ready"})

    def start(*args, **kwargs):
        starts.append(kwargs)
        return {"runs": [{"id": "one-owned-run"}] if behavior["launch_ok"] else [],
                "errors": [] if behavior["launch_ok"] else [{"code": "login_required"}]}

    runtime = SimpleNamespace(start_project=start, list_runs=lambda: [], diagnostics=lambda: {})
    monkeypatch.setattr("cairnmind.client_auth.probe_client",
                        lambda client, *args, **kwargs: {"client": client, "ready_to_launch": True})
    app = create_cockpit_app(settings, runtime=runtime, proxy_transport=httpx.MockTransport(upstream),
                             public_url="http://127.0.0.1:8766")
    return app, project, calls, starts, behavior


def broker_login(app, client):
    origin = "http://127.0.0.1:8766"
    result = client.post("/auth/exchange", json={"ticket": app.state.browser_sessions.ticket()}, headers={"Origin": origin})
    return {"Origin": origin, "X-CSRF-Token": result.json()["csrf"]}


def test_start_work_enables_only_selected_project_and_cached_failure_never_relaunches(broker):
    app, project, calls, starts, behavior = broker
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        auth = broker_login(app, client)
        assert client.get("/api/state").status_code == 200
        assert not [row for row in calls if row[0] == "/background-inference/enable"]
        behavior["enable_status"] = 503
        payload = {"project": project["id"], "idempotency_key": "explicit-start"}
        first = client.post("/api/client-runs", json=payload, headers=auth)
        assert first.status_code == 200 and len(first.json()["runs"]) == 1
        assert first.json()["errors"][0]["code"] == "background_inference_deferred"
        assert "More → Queue → Process memory" in first.json()["errors"][0]["message"]
        behavior["enable_status"] = 200
        again = client.post("/api/client-runs", json=payload, headers=auth)
        assert again.json() == first.json() and len(starts) == 1
        assert [body for path, body in calls if path == "/background-inference/enable"] == [
            {"project": project["id"], "worktree": project["root"]}]
        manual = client.post("/api/background-inference/enable", json={"project": project["id"]}, headers=auth)
        assert manual.json()["accepted"] is True and len(starts) == 1
        assert client.post("/api/background-inference/enable", json={"project": project["id"]}, headers=auth).status_code == 200
        assert len(starts) == 1


def test_invalid_or_failed_launch_and_missing_csrf_never_enable_memory(broker):
    app, project, calls, starts, behavior = broker
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        auth = broker_login(app, client)
        body = {"project": project["id"], "idempotency_key": "bad"}
        assert client.post("/api/client-runs", json=body, headers={"Origin": auth["Origin"]}).status_code == 403
        assert client.post("/api/background-inference/enable", json={"project": project["id"]},
                           headers={"Origin": auth["Origin"]}).status_code == 403
        assert client.post("/api/client-runs", json={**body, "worktree": "/foreign"}, headers=auth).status_code == 409
        behavior["launch_ok"] = False
        assert client.post("/api/client-runs", json=body, headers=auth).json()["runs"] == []
        assert not [row for row in calls if row[0] == "/background-inference/enable"]


@pytest.mark.asyncio
async def test_cancellation_after_launch_keeps_idempotency_and_unconfirmed_warning(broker):
    app, project, calls, starts, behavior = broker
    entered = asyncio.Event()

    async def blocked_enable():
        entered.set()
        await asyncio.Event().wait()

    behavior["enable_hook"] = blocked_enable
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8766") as client:
        auth = {"Origin": "http://127.0.0.1:8766"}
        response = await client.post("/auth/exchange", json={"ticket": app.state.browser_sessions.ticket()}, headers=auth)
        auth["X-CSRF-Token"] = response.json()["csrf"]
        payload = {"project": project["id"], "idempotency_key": "cancel-after-start"}
        request = asyncio.create_task(client.post("/api/client-runs", json=payload, headers=auth))
        await asyncio.wait_for(entered.wait(), 3)
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        result = await client.post("/api/client-runs", json=payload, headers=auth)
        assert len(starts) == 1
        assert result.json()["errors"][0]["code"] == "background_inference_deferred"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["process", "cancel", "scope_changed"])
async def test_queue_button_is_explicit_scoped_localized_and_never_starts_an_assistant(tmp_path, monkeypatch, action):
    from textual.widgets import Button, Static
    from cairnmind.terminal_panels import SidebarApp
    from test_terminal_panels import setup, settle

    settings, broker, workspace = setup(tmp_path, monkeypatch)
    original = broker.request

    def request(method, path, **kwargs):
        result = original(method, path, **kwargs)
        if path == "/api/background-inference/enable":
            return {"accepted": True, "project_id": kwargs["json"]["project"], "worker_running": True}
        return result

    monkeypatch.setattr(broker, "request", request)
    workspace.update({"launch_errors": [{"code": "background_inference_deferred"}]})
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(100, 45)) as pilot:
        await settle(app, pilot)
        await pilot.click("#project-inspect")
        await settle(app, pilot)
        notice = str(app.query_one("#panel-notice", Static).render())
        assert "Asystent został uruchomiony" in notice
        assert "Więcej → Kolejka → Przetwórz pamięć" in notice
        await pilot.click("#more-open")
        await pilot.pause()
        await pilot.press("down", "down", "down", "enter")
        await pilot.pause()
        assert app.screen.query_one("#confirm-yes", Button).label.plain == "Przetwórz pamięć"
        assert not [row for row in broker.calls if row[1] == "/api/background-inference/enable"]
        if action == "scope_changed":
            workspace.update({"project_id": "project-b", "worktree": "/other-project"})
        await pilot.click("#confirm-no" if action == "cancel" else "#confirm-yes")
        await settle(app, pilot)
        enabled = [row for row in broker.calls if row[1] == "/api/background-inference/enable"]
        assert len(enabled) == (1 if action == "process" else 0)
        assert not [row for row in broker.calls if row[1] == "/api/client-runs"]
        if action == "process":
            assert enabled[0][3] == {"project": "project-a", "worktree": broker.project["root"]}
            assert "Włączono przetwarzanie pamięci" in str(app.query_one("#panel-notice", Static).render())
            assert workspace.read()["launch_errors"] == []
