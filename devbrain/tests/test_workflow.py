"""Cross-component control plane: real ArcadeDB, real Git and actual named checks."""
from __future__ import annotations

import json
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from devbrain.config import Settings
from devbrain.service import create_app
from devbrain.store import Store


class NoGeneration:
    async def models(self):
        return {"models": []}

    async def embed(self, texts):
        raise AssertionError("Control-plane workflow must not require a language model")

    async def chat(self, *args, **kwargs):
        raise AssertionError("Control-plane workflow must not require a language model")


def repository(path, marker="first"):
    path.mkdir()
    (path / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    (path / "auth.py").write_text("def authenticate(token):\n    return token == 'valid'\n")
    (path / "marker.txt").write_text(marker)
    (path / ".devbrain").mkdir()
    argv = [sys.executable, "-B", "-c", "from auth import authenticate; assert authenticate('valid'); "
            "assert not authenticate('bad'); print('login verified')"]
    (path / ".devbrain/checks.toml").write_text("[checks.unit]\nargv=" + json.dumps(argv) + "\n")
    for args in (("init",), ("config", "user.email", "fixture@example.invalid"),
                 ("config", "user.name", "Fixture"), ("add", "."), ("commit", "-m", "fixture")):
        subprocess.run(["git", "-C", str(path), *args], capture_output=True, check=True)


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "repo"
    repository(root)
    settings = Settings.load(tmp_path / "memory", create=True)
    store = Store(settings.data_dir)
    project = store.add_project("workflow", root)
    return settings, store, project, root


def readback(packet):
    return {key: ({"id": packet[field]["id"], "version": packet[field]["version"]}
                  if packet.get(field) else None)
            for key, field in (("plan", "active_plan"), ("task", "active_task"))}


def test_codex_claude_fresh_codex_restore_plan_check_and_compaction(workspace, monkeypatch, tmp_path):
    from devbrain import integrations
    from devbrain.ingest import import_history
    from devbrain.verification import run_check

    settings, store, project, root = workspace
    headers = {"Authorization": f"Bearer {settings.token}"}
    with TestClient(create_app(settings, start_worker=False, ollama=NoGeneration())) as client:
        def post(path, body, code=200):
            response = client.post(path, json=body, headers=headers)
            assert response.status_code == code, response.text
            return response.json()

        saved = post("/plans/save", {"project": project["id"], "plan": {
            "title": "Login", "goal": "Finish login with source-backed checks", "constraints": ["Local only"]},
            "steps": [{"key": "login", "content": "Verify login", "metadata": {
                "requires_test": True, "required_profiles": ["unit"]}},
                {"key": "docs", "content": "Document login", "depends_on": ["login"]}],
            "idempotency_key": "initial", "activate": True})
        task = saved["tasks"][0]
        initial = post("/sessions/prepare", {"project": project["id"], "client": "codex"})
        assert initial["status"] == "ready"
        packet = initial["packet"]
        assert packet["active_plan"]["id"] == saved["plan"]["id"]
        assert packet["active_task"]["id"] == task["id"]
        update = {"project": project["id"], "record": {"id": task["id"],
            "expected_version": task["version"], "expected_plan_version": saved["plan"]["version"],
            "metadata": {"state": "needs_test"}}, "sources": task["source_ids"],
            "session_id": initial["session_id"], "packet_id": packet["id"]}
        post("/remember", update, 409)
        wrong = readback(packet)
        wrong["task"]["version"] += 1
        post("/sessions/ack", {"session_id": initial["session_id"], "packet_id": packet["id"],
                              "packet_hash": packet["hash"], "readback": wrong}, 409)
        post("/sessions/ack", {"session_id": initial["session_id"], "packet_id": packet["id"],
                              "packet_hash": packet["hash"], "readback": readback(packet)})
        waiting = post("/remember", update)
        assert waiting["metadata"]["state"] == "needs_test"
        post("/checkpoint", {"project": project["id"], "state": {"goal": "old state"},
                             "session_id": initial["session_id"], "packet_id": packet["id"]}, 409)

        claude = post("/sessions/prepare", {"project": project["id"], "client": "claude"})
        assert claude["packet"]["active_task"]["state"] == "needs_test"
        assert claude["packet"]["active_task"]["version"] == waiting["version"]
        cp = claude["packet"]
        post("/sessions/ack", {"session_id": claude["session_id"], "packet_id": cp["id"],
                              "packet_hash": cp["hash"], "readback": readback(cp)})
        check = run_check(store, settings, project["id"], "unit", task["id"])
        assert check["passed"], check
        reports = client.get("/checks", params={"project": project["id"], "task_id": task["id"],
                                               "session_id": claude["session_id"]}, headers=headers).json()
        assert reports["verification"]["status"] == "fresh"
        assert reports["checks"][0]["passed"] is True
        done = post("/remember", {"project": project["id"], "record": {"id": task["id"],
            "expected_version": waiting["version"], "expected_plan_version": cp["active_plan"]["version"],
            "metadata": {"state": "done", "check_run_ids": [check["id"]]}}, "sources": waiting["source_ids"],
            "session_id": claude["session_id"], "packet_id": cp["id"]})
        assert done["metadata"]["state"] == "done"
        fresh = post("/sessions/prepare", {"project": project["id"], "client": "codex"})
        assert fresh["packet"]["active_task"]["id"] == saved["tasks"][1]["id"]
        fp = fresh["packet"]
        post("/sessions/ack", {"session_id": fresh["session_id"], "packet_id": fp["id"],
                              "packet_hash": fp["hash"], "readback": readback(fp)})

        transcript = tmp_path / "codex.jsonl"
        transcript.write_text(json.dumps({"type": "event_msg", "payload": {
            "type": "user_message", "message": "Continue the documentation task", "cwd": str(root)}}) + "\n")
        monkeypatch.setenv("DEVBRAIN_SESSION_ID", fresh["session_id"])
        integrations.hook(settings, {"hook_event_name": "PreCompact", "session_id": "native-codex",
            "cwd": str(root), "transcript_path": str(transcript)}, "codex")
        post("/checkpoint", {"project": project["id"], "state": {"goal": "must not use old ACK"},
                             "session_id": fresh["session_id"], "packet_id": fp["id"]}, 409)
        pending = post("/sessions/context", {"session_id": fresh["session_id"]})
        assert pending["status"] != "ready"
        import_history(store, transcript, project["id"], "codex")
        restored = post("/sessions/context", {"session_id": fresh["session_id"]})
        assert restored["status"] == "ready", restored
        assert restored["epoch"] > fresh["epoch"]
        assert restored["packet"]["archive"]["status"] == "complete"
        post("/sessions/ack", {"session_id": fresh["session_id"], "packet_id": restored["packet"]["id"],
            "packet_hash": restored["packet"]["hash"], "readback": readback(restored["packet"])})


def test_api_session_reads_and_writes_stay_in_selected_worktree(workspace, tmp_path):
    from devbrain.plans import PlanManager
    settings, store, project, first = workspace
    second = tmp_path / "second"
    repository(second, "second")
    store.add_project_root(project["id"], second)
    manager = PlanManager(store)
    a = manager.save(project["id"], {"title": "First", "goal": "First worktree"},
                     [{"key": "a", "content": "First task"}], "first", worktree=first, activate=True)
    b = manager.save(project["id"], {"title": "Second", "goal": "Second worktree"},
                     [{"key": "b", "content": "Second task"}], "second", worktree=second, activate=True)
    headers = {"Authorization": f"Bearer {settings.token}"}
    with TestClient(create_app(settings, start_worker=False, ollama=NoGeneration())) as client:
        result = client.post("/sessions/prepare", json={"project": project["id"], "client": "claude",
                             "worktree": str(second)}, headers=headers).json()
        assert result["status"] == "ready", result
        assert result["packet"]["active_plan"]["id"] == b["plan"]["id"]
        context = client.get("/context", params={"project": project["id"], "session_id": result["session_id"]},
                             headers=headers).json()
        assert context["workflow"]["plan"]["id"] == b["plan"]["id"]
        assert context["workflow"]["plan"]["id"] != a["plan"]["id"]
        selected = client.post("/plans/next", json={"project": project["id"],
                               "session_id": result["session_id"]}, headers=headers).json()
        assert selected["task"]["id"] == b["tasks"][0]["id"]
        rejected = client.post("/plans/next", json={"project": project["id"], "session_id": result["session_id"],
                               "worktree": str(first)}, headers=headers)
        assert rejected.status_code == 400


def test_cli_read_views_inherit_session_worktree_before_ack(workspace, tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from devbrain.cli import app
    from devbrain.plans import PlanManager
    from devbrain.sessions import SessionManager
    from devbrain.verification import run_check
    settings, store, project, first = workspace
    second = tmp_path / "second"
    repository(second, "second")
    config = second / ".devbrain/checks.toml"
    config.write_text(config.read_text() + '\n[checks.second_only]\nargv=["true"]\n')
    store.add_project_root(project["id"], second)
    manager = PlanManager(store)
    a = manager.save(project["id"], {"title": "First"}, [{"key": "a", "content": "First task"}],
                     "first", worktree=first, activate=True)
    b = manager.save(project["id"], {"title": "Second"}, [{"key": "b", "content": "Second task",
                     "metadata": {"requires_test": True, "required_profiles": ["unit"]}}],
                     "second", worktree=second, activate=True)
    assert run_check(store, settings, project["id"], "unit", a["tasks"][0]["id"], first)["passed"]
    run = run_check(store, settings, project["id"], "unit", b["tasks"][0]["id"], second)
    assert run["passed"]
    session = SessionManager(store, settings).prepare(project["id"], "codex", second)
    monkeypatch.setenv("DEVBRAIN_SESSION_ID", session["session_id"])
    base = ["--data-dir", str(settings.data_dir)]
    runner = CliRunner()

    def read(*args):
        result = runner.invoke(app, [*base, *args])
        assert result.exit_code == 0, result.output or repr(result.exception)
        return json.loads(result.output)

    assert read("plan", "next", "workflow")["task"]["id"] == b["tasks"][0]["id"]
    assert "second_only" in read("check", "profiles", "workflow")
    tasks = read("task", "list", "workflow")
    assert next(t for t in tasks if t["id"] == b["tasks"][0]["id"])["verification"]["status"] == "fresh"
    checks = read("check", "list", "workflow")["checks"]
    assert [row["id"] for row in checks] == [run["id"]]
    for args in (("plan", "next"), ("task", "list"), ("check", "profiles"), ("check", "list")):
        rejected = runner.invoke(app, [*base, *args, "workflow", "--worktree", str(first)])
        assert rejected.exit_code != 0
        assert "worktree differs" in str(rejected.exception)
    headers = {"Authorization": f"Bearer {settings.token}"}
    with TestClient(create_app(settings, start_worker=False, ollama=NoGeneration())) as client:
        rows = client.get("/checks", params={"project": project["id"], "session_id": session["session_id"]},
                          headers=headers).json()["checks"]
        assert [row["id"] for row in rows] == [run["id"]]
