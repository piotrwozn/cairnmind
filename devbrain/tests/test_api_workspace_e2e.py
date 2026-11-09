"""Production API worker over real tmux, TCP broker, service and ArcadeDB.

Only the model endpoint is controlled. No native coding CLI, remote model,
browser, production test switch or paid request is used by these tests.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import httpx
import pytest
import uvicorn
from websockets.sync.client import connect

from devbrain import __version__
from devbrain.agent_journal import AgentJournal
from devbrain.api_credentials import CredentialStore
from devbrain.api_runtime import RuntimeSupervisor
from devbrain.cockpit import _birth, create_cockpit_app, instance_id
from devbrain.cockpit_state import CockpitState, _atomic
from devbrain.config import Settings
from devbrain.service import create_app
from devbrain.store import Store


CANARY = "E2E070_PRIVATE_PROFILE_KEY_MUST_NOT_ENTER_HISTORY"


def free_port():
    with socket.socket() as socket_:
        socket_.bind(("127.0.0.1", 0))
        return socket_.getsockname()[1]


class LiveServer:
    def __init__(self, app, port):
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
            log_level="warning", access_log=False, proxy_headers=False, timeout_graceful_shutdown=3))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 12
        while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.02)
        assert self.server.started, "Owned TCP fixture did not start"

    def stop(self):
        self.server.should_exit = True
        self.thread.join(8)
        assert not self.thread.is_alive(), "Owned TCP fixture did not stop"


def eventually(check, timeout=15):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return check()
        except AssertionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.04)


def contains_screen(stack, run, text):
    screen = stack.capture(run)
    assert text in screen, screen
    return screen


def terminal_status(terminal):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        event = json.loads(terminal.recv(timeout=max(.01, deadline - time.monotonic())))
        if event.get("type") == "status":
            return event
    raise AssertionError("No terminal status was delivered")


class WorkerStack:
    def __init__(self, root, monkeypatch):
        self.root = Path(root)
        self.tmux = os.environ.get("DEVBRAIN_TEST_TMUX") or shutil.which("tmux")
        if not self.tmux and Path("/tmp/devbrain-tmux-3.4/tmux").is_file():
            self.tmux = "/tmp/devbrain-tmux-3.4/tmux"
        if not self.tmux:
            pytest.skip("Production API worker test requires real tmux >=3.4")
        self.servers = []
        self.runtime = None
        self.http = None
        self.settings = Settings.load(self.root / "data", create=True)
        self.settings.port, self.settings.cockpit_port = free_port(), free_port()
        self.settings.ollama_url = "http://127.0.0.1:1"
        self.settings.save()
        self.origin = f"http://127.0.0.1:{self.settings.cockpit_port}"
        self.calls, self.model_errors = [], []
        self.model_port = free_port()
        bindir = self.root / "bin"
        bindir.mkdir()
        (bindir / "tmux").symlink_to(self.tmux)
        monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
        monkeypatch.setenv("HOME", str(self.root / "home"))
        (self.root / "home").mkdir()
        for key in ("DEVBRAIN_SESSION_ID", "DEVBRAIN_PACKET_ID", "DEVBRAIN_PROJECT", "DEVBRAIN_CLIENT"):
            monkeypatch.delenv(key, raising=False)
        self.store = Store(self.settings.data_dir)
        worktree = self.root / "project"
        worktree.mkdir()
        (worktree / "app.py").write_text("VALUE = 'worker-evidence'\n")
        subprocess.run(["git", "init", "-q", str(worktree)], check=True)
        subprocess.run(["git", "-C", str(worktree), "add", "app.py"], check=True)
        subprocess.run(["git", "-C", str(worktree), "-c", "user.name=Worker fixture",
                        "-c", "user.email=fixture@localhost", "commit", "-qm", "Fixture"], check=True)
        self.project = self.store.add_project("production-api-worker", worktree)
        self.start_models()
        self.servers.append(LiveServer(create_app(self.settings, start_worker=False), self.settings.port))
        profiles = CockpitState(self.settings)
        self.profile = {"id": "fixture-api", "client": "api", "label": "Controlled worker API",
                        "provider": "openai_compatible", "model": "tool-model",
                        "base_url": f"http://127.0.0.1:{self.model_port}/v1",
                        "credential_ref": "profile:fixture-api"}
        profiles.update({"profiles": [*profiles.load()["profiles"], self.profile],
                         "default_slots": [{"id": "api-one", "profile_id": "fixture-api"}]})
        credentials = CredentialStore(self.settings)
        credentials.set("fixture-api", CANARY, persist=False)
        self.runtime = RuntimeSupervisor(self.settings, tmux_binary=self.tmux, credentials=credentials)
        app = create_cockpit_app(self.settings, runtime=self.runtime, public_url=self.origin)
        self.servers.append(LiveServer(app, self.settings.cockpit_port))
        _atomic(self.settings.data_dir / "cockpit/server.json", {
            "url": self.origin, "pid": os.getpid(), "birth": _birth(os.getpid()),
            "instance": instance_id(self.settings), "version": __version__})
        self.http = httpx.Client(base_url=self.origin, timeout=30, trust_env=False)
        response = self.http.post("/_control/ticket", headers={"Authorization": "Bearer " + self.settings.token})
        response.raise_for_status()
        response = self.http.post("/auth/exchange", json={"ticket": response.json()["ticket"]},
                                  headers={"Origin": self.origin})
        response.raise_for_status()
        self.csrf = response.json()["csrf"]

    def start_models(self):
        app = FastAPI()

        @app.post("/v1/chat/completions")
        async def model(request: Request):
            try:
                assert request.headers.get("authorization") == "Bearer " + CANARY
                body = await request.json()
                index = len(self.calls)
                self.calls.append(body)
                if index == 0:
                    assert body["messages"][-1]["role"] == "user"
                    return self.tool(index, "resume_context", {})
                result = json.loads(next(row["content"] for row in reversed(body["messages"])
                                         if row.get("role") == "tool"))
                if index == 1:
                    packet = result["packet"]
                    assert result["status"] == packet["status"] == "ready"
                    readback = {name: ({"id": packet[key]["id"], "version": packet[key]["version"]}
                                       if packet.get(key) else None)
                                for name, key in (("plan", "active_plan"), ("task", "active_task"))}
                    return self.tool(index, "ack_resume", {"packet_id": packet["id"], "packet_hash": packet["hash"],
                                                          "readback": readback})
                if index == 2:
                    assert result["status"] == "acknowledged"
                    return self.tool(index, "read_file", {"path": "app.py"})
                assert index == 3 and result["ok"] and "worker-evidence" in result["content"]
                async def chunks():
                    for text in ("WORKER_", "E2E_CONFIRMED: worker-evidence"):
                        yield "data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": text}}]}) + "\n\n"
                        await asyncio.sleep(.02)
                    yield "data: " + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                                                "usage": {"prompt_tokens": 100, "completion_tokens": 12}}) + "\n\n"
                    yield "data: [DONE]\n\n"
                return StreamingResponse(chunks(), media_type="text/event-stream")
            except Exception as error:
                self.model_errors.append(type(error).__name__ + ": " + str(error))
                return JSONResponse({"error": "Controlled fixture assertion failed"}, status_code=500)

        self.servers.append(LiveServer(app, self.model_port))

    @staticmethod
    def tool(index, name, arguments):
        return {"choices": [{"index": 0, "message": {"role": "assistant", "content": "", "tool_calls": [{
            "id": "worker-call-" + str(index), "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}]}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10}}

    def request(self, method, path, **kwargs):
        response = self.http.request(method, path, headers={"Origin": self.origin, "X-CSRF-Token": self.csrf}, **kwargs)
        response.raise_for_status()
        return response.json()

    def start(self):
        result = self.request("POST", "/api/client-runs", json={"project": self.project["id"],
            "profile_id": "fixture-api", "slot_id": "api-one", "idempotency_key": uuid4().hex})
        assert not result["errors"], result
        assert len(result["runs"]) == 1
        return result["runs"][0]

    def capture(self, run):
        return self.runtime._tmux("capture-pane", "-p", "-J", "-t", run["tmux_session"]).stdout

    def terminal(self, run):
        cookies = "; ".join(key + "=" + value for key, value in self.http.cookies.items())
        return connect(self.origin.replace("http:", "ws:") + f"/api/client-runs/{run['id']}/terminal?generation={run['generation']}",
                       origin=self.origin, additional_headers={"Cookie": cookies, "X-CSRF-Token": self.csrf},
                       proxy=None, open_timeout=5, close_timeout=2, max_size=256 * 1024)

    def close(self):
        if self.runtime:
            for run in self.runtime.list_runs():
                if run["lifecycle"] in {"starting", "running", "stopping"}:
                    self.runtime.stop(run["id"], run["generation"])
        if self.http:
            self.http.close()
        for server in reversed(self.servers):
            server.stop()
        if self.runtime and self.runtime.socket.exists():
            self.runtime._tmux("kill-server", check=False)


@pytest.fixture
def worker_stack(tmp_path, monkeypatch):
    stack = WorkerStack(tmp_path, monkeypatch)
    try:
        yield stack
    finally:
        stack.close()


@pytest.mark.parametrize("language", ["en", "pl"])
def test_production_worker_actual_ack_stream_archive_and_reattach(worker_stack, language):
    stack = worker_stack
    preferences = CockpitState(stack.settings)
    if language == "pl":
        preferences.update({"language": "pl"})
    else:
        # Profile setup persists defaults; remove only this synthetic fixture's
        # language key so the real worker must exercise the EN default itself.
        stored = json.loads(preferences.path.read_text())
        stored.pop("language", None)
        _atomic(preferences.path, stored)
        assert "language" not in json.loads(preferences.path.read_text())
    assert preferences.load()["language"] == language
    readiness = ("Ready. Send a message to start work and restore memory." if language == "en"
                 else "Gotowy. Wyślij wiadomość, aby rozpocząć pracę i protokół odtworzenia pamięci.")
    run = stack.start()
    def ready():
        screen = stack.capture(run)
        assert readiness in screen, screen
        return screen
    eventually(ready)
    assert not stack.calls
    assert stack.store.query("SELECT FROM resume_acks WHERE session_id=:id", {"id": run["session_id"]}) == []
    assert len(stack.runtime.list_runs()) == 1
    public = stack.request("GET", "/api/profiles")
    assert CANARY not in json.dumps(public)
    body = {"run_id": run["id"], "generation": run["generation"] + 1}
    refused = stack.http.post("/_control/agent/credential", json=body,
                              headers={"Authorization": "Bearer " + stack.settings.token})
    assert refused.is_error and CANARY not in refused.text
    with stack.terminal(run) as terminal:
        # Real terminal attach/control; a screen connection grants no ACK.
        assert terminal_status(terminal)["run"]["id"] == run["id"]
        prompt = "Read app.py using the current project memory."
        stack.runtime._tmux("send-keys", "-t", run["tmux_session"], "-l", prompt)
        eventually(lambda: contains_screen(stack, run, prompt))
        stack.runtime._tmux("send-keys", "-t", run["tmux_session"], "-H", "1b", "5b", "31", "33", "3b", "35", "75")
        def finished():
            assert not stack.model_errors, stack.model_errors
            events = AgentJournal(stack.settings, run["session_id"]).events()
            assert any(event["type"] == "message" and event["data"].get("role") == "assistant"
                       and "WORKER_E2E_CONFIRMED" in event["data"].get("content", "") for event in events), stack.capture(run)
            assert any(event["type"] == "turn_finished" for event in events), stack.capture(run)
            return events
        events = eventually(finished, timeout=20)
        assert len(stack.calls) == 4
        acks = stack.store.query("SELECT FROM resume_acks WHERE session_id=:id", {"id": run["session_id"]})
        assert len(acks) == 1 and acks[0]["status"] == "acknowledged"
        assert acks[0]["readback"] == {"plan": None, "task": None}
        eventually(lambda: contains_screen(stack, run, "WORKER_E2E_CONFIRMED"))
        assert next(event["data"]["content"] for event in events
                    if event["type"] == "message" and event["data"].get("role") == "user") == prompt
    def archived():
        sources = stack.store.query("SELECT FROM sources WHERE project_id=:id AND kind='agent_transcript'",
                                    {"id": stack.project["id"]})
        assert sources and any("WORKER_E2E_CONFIRMED" in row["content"] for row in sources)
        return sources
    sources = eventually(archived)
    assert all(CANARY not in row["content"] for row in sources)
    assert CANARY not in json.dumps(events)
    original_pid, original_generation = run["pid"], run["generation"]
    # A new explicit launch of the same live slot and another terminal attach
    # both recover that existing process without starting a new model turn.
    again = stack.start()
    assert again["id"] == run["id"] and again["pid"] == original_pid and again["generation"] == original_generation
    with stack.terminal(again) as terminal:
        assert terminal_status(terminal)["run"]["id"] == run["id"]
        assert "WORKER_E2E_CONFIRMED" in stack.capture(again)
    assert len(stack.calls) == 4
    assert len(stack.runtime.list_runs()) == 1
    stopped = stack.request("POST", f"/api/client-runs/{run['id']}/stop", json={"generation": original_generation})
    assert stopped.get("state", stopped.get("run", {}).get("state")) == "exited"
    assert len(stack.calls) == 4
