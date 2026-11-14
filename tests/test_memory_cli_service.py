"""Portable-memory transport preserves authentication, actor scope and diagnostic purity."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

from fastapi.testclient import TestClient
import httpx
import pytest
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.config import Settings
from cairnmind.service import create_app
from cairnmind.sessions import SessionManager
from cairnmind.store import Store


class NoInference:
    async def models(self):
        pytest.fail("Portable-memory HTTP operations must not invoke a model")

    async def chat(self, *args, **kwargs):
        pytest.fail("Portable-memory HTTP operations must not invoke a model")

    async def embed(self, *args, **kwargs):
        pytest.fail("Portable-memory HTTP operations must not invoke a model")


@pytest.fixture
def memory_workspace(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("VALUE = 1\n")
    for args in (("init", "-q"), ("config", "user.name", "Memory fixture"),
                 ("config", "user.email", "fixture@example.invalid"),
                 ("add", "app.py"), ("commit", "-qm", "initial")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    store = Store(tmp_path / "data")
    project = store.add_project("transport", root)
    source = store.snapshot(project["id"], "manual://memory", "The release identifier is zephyr_214.", "manual")
    store.remember(project["id"], {"kind": "fact", "content": source["content"]}, [source["id"]])
    return store, project, root


def _headers(store):
    return {"Authorization": f"Bearer {store.settings.token}"}


def _snapshot(store, project):
    from cairnmind.memory import MemoryManager
    return MemoryManager(store).snapshot(project["id"])


def _unchanging_export(store):
    result = store.export()
    result.pop("exported_at", None)
    return result


def _no_diagnostic_mutations(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("A diagnostic initialized storage or started a service")
    original = Settings.load

    def load(cls, data_dir=None, *, create=False):
        assert create is False
        return original(data_dir, create=False)

    monkeypatch.setattr(Settings, "load", classmethod(load))
    monkeypatch.setattr(cli, "ensure_service", forbidden)
    monkeypatch.setattr(cli, "store_for", forbidden)
    monkeypatch.setattr(cli.subprocess, "Popen", forbidden)


def _transport(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("args", [
    ["memory", "--help"], ["memory", "snapshot", "--help"],
    ["memory", "inspect", "--help"], ["memory", "import", "--help"],
    ["memory", "auto", "--help"], ["memory", "status", "--help"],
    ["eval", "memory", "--help"],
])
def test_memory_help_is_noninitializing(tmp_path, monkeypatch, args):
    data = tmp_path / "missing" / "data"
    _no_diagnostic_mutations(monkeypatch)
    result = CliRunner().invoke(cli.app, ["--data-dir", str(data), *args])
    assert result.exit_code == 0, result.output
    assert not data.parent.exists()


def test_memory_inspect_is_offline_and_does_not_initialize(memory_workspace, tmp_path, monkeypatch):
    store, project, _ = memory_workspace
    saved = _snapshot(store, project)
    missing = tmp_path / "fresh" / "uninitialized"
    _no_diagnostic_mutations(monkeypatch)
    monkeypatch.delenv("DEVBRAIN_SESSION_ID", raising=False)
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: pytest.fail("Offline inspect made an HTTP call"))
    result = CliRunner().invoke(cli.app, ["--data-dir", str(missing), "memory", "inspect", saved["path"]])
    assert result.exit_code == 0, result.output or repr(result.exception)
    assert json.loads(result.output)["project"]["id"] == project["id"]
    assert not missing.parent.exists()


def test_memory_show_is_offline_read_only_and_does_not_initialize(memory_workspace, tmp_path, monkeypatch):
    store, project, root = memory_workspace
    saved = _snapshot(store, project)
    package = Path(saved["path"]).parent
    before = {path: path.read_bytes() for path in package.rglob("*") if path.is_file()}
    missing = tmp_path / "fresh" / "uninitialized"
    _no_diagnostic_mutations(monkeypatch)
    monkeypatch.delenv("DEVBRAIN_SESSION_ID", raising=False)
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: pytest.fail("Offline show made an HTTP call"))
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: pytest.fail("Offline show started a process"))
    result = CliRunner().invoke(cli.app, ["--data-dir", str(missing), "memory", "show", saved["path"],
                                          "--kind", "fact", "--grep", "ZEPHYR"])
    assert result.exit_code == 0, result.output or repr(result.exception)
    assert f"snapshot:  {saved['snapshot_id']}" in result.output
    assert "[fact] proposed, version 1, updated " in result.output
    assert "  | The release identifier is zephyr_214." in result.output
    assert "  sources: manual://memory" in result.output
    assert not missing.parent.exists()
    assert before == {path: path.read_bytes() for path in package.rglob("*") if path.is_file()}
    nothing = CliRunner().invoke(cli.app, ["--data-dir", str(missing), "memory", "show", saved["path"],
                                           "--grep", "no-such-text"])
    assert nothing.exit_code == 0 and "Nothing in this snapshot matches the filters." in nothing.output


def test_memory_show_rejects_a_tampered_package_like_inspect(memory_workspace, tmp_path, monkeypatch):
    store, project, _ = memory_workspace
    saved = _snapshot(store, project)
    monkeypatch.delenv("DEVBRAIN_SESSION_ID", raising=False)
    target = next(path for path in sorted((Path(saved["path"]).parent / "objects").glob("*.json"))
                  if b"zephyr_214" in path.read_bytes())
    target.write_bytes(target.read_bytes().replace(b"zephyr_214", b"zephyr_999"))
    outcomes = [CliRunner().invoke(cli.app, ["--data-dir", str(tmp_path / "none"), "memory", command, saved["path"]])
                for command in ("inspect", "show")]
    assert all(result.exit_code == 1 and isinstance(result.exception, ValueError) for result in outcomes)
    assert str(outcomes[0].exception) == str(outcomes[1].exception)
    assert "zephyr" not in outcomes[1].output


def test_memory_show_in_a_session_is_authorized_by_the_service_first(memory_workspace, tmp_path, monkeypatch):
    store, project, _ = memory_workspace
    saved = _snapshot(store, project)
    monkeypatch.setenv("DEVBRAIN_SESSION_ID", "session-1")
    calls = []

    def allowed(settings, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"snapshot_id": saved["snapshot_id"]}

    monkeypatch.setattr(cli, "request", allowed)
    result = CliRunner().invoke(cli.app, ["--data-dir", str(tmp_path / "none"), "memory", "show", saved["path"]])
    assert result.exit_code == 0, result.output or repr(result.exception)
    assert calls == [("POST", "/memory/inspect", {"start_service": False, "json": {"path": saved["path"]}})]

    def refused(settings, method, path, **kwargs):
        raise ValueError("resume_required: command project differs from the session")

    monkeypatch.setattr(cli, "request", refused)
    denied = CliRunner().invoke(cli.app, ["--data-dir", str(tmp_path / "none"), "memory", "show", saved["path"]])
    assert denied.exit_code == 1 and "zephyr" not in denied.output

    monkeypatch.setattr(cli, "request", lambda *a, **k: {"snapshot_id": "0" * 64})
    swapped = CliRunner().invoke(cli.app, ["--data-dir", str(tmp_path / "none"), "memory", "show", saved["path"]])
    assert swapped.exit_code == 1 and "changed while it was being authorized" in str(swapped.exception)
    assert "zephyr" not in swapped.output


def test_memory_status_requires_explicit_existing_configuration(tmp_path, monkeypatch):
    missing = tmp_path / "fresh" / "data"
    _no_diagnostic_mutations(monkeypatch)
    result = CliRunner().invoke(cli.app, ["--data-dir", str(missing), "memory", "status", "demo"])
    assert result.exit_code == 2
    assert "cairnmind init" in result.output
    assert not missing.parent.exists()


def test_memory_status_is_authenticated_scoped_read_without_start(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data", create=True)
    before = (settings.data_dir / "config.json").read_bytes()
    _no_diagnostic_mutations(monkeypatch)
    monkeypatch.setenv("DEVBRAIN_SESSION_ID", "actor")

    def handler(request):
        assert request.method == "GET" and request.url.path == "/memory/status"
        assert dict(request.url.params) == {"project": "demo", "session_id": "actor"}
        assert request.headers["Authorization"] == f"Bearer {settings.token}"
        return httpx.Response(200, json={"auto": {"enabled": False}})

    _transport(monkeypatch, handler)
    result = CliRunner().invoke(cli.app, ["--data-dir", str(settings.data_dir), "memory", "status", "demo"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["auto"]["enabled"] is False
    assert (settings.data_dir / "config.json").read_bytes() == before


def test_cli_dry_run_uses_readonly_existing_service(memory_workspace, tmp_path, monkeypatch):
    store, project, _ = memory_workspace
    saved = _snapshot(store, project)
    before = _unchanging_export(store)
    _no_diagnostic_mutations(monkeypatch)
    monkeypatch.setenv("DEVBRAIN_SESSION_ID", "actor")
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "POST" and request.url.path == "/memory/import"
        body = json.loads(request.content)
        assert body == {"path": str(Path(saved["path"]).resolve()), "root": str(tmp_path / "destination"),
                        "dry_run": True, "session_id": "actor"}
        return httpx.Response(200, json={"status": "dry_run"})

    _transport(monkeypatch, handler)
    result = CliRunner().invoke(cli.app, ["--data-dir", str(store.data_dir), "memory", "import",
        saved["path"], "--root", str(tmp_path / "destination"), "--dry-run"])
    assert result.exit_code == 0, result.output or repr(result.exception)
    assert len(calls) == 1
    monkeypatch.undo()  # Restore the real HTTP transport before reading ArcadeDB.
    assert _unchanging_export(store) == before


@pytest.mark.parametrize(("args", "method", "path", "fields"), [
    (["snapshot", "demo"], "POST", "/memory/snapshot", {"project": "demo", "worktree": None}),
    (["snapshot", "demo", "--preview"], "POST", "/memory/preview", {"project": "demo", "worktree": None}),
    (["auto", "demo", "--enable"], "PATCH", "/memory/settings",
     {"project": "demo", "enabled": True, "interval_minutes": 15, "worktree": None}),
])
def test_cli_memory_operations_forward_current_actor_without_stale_environment_packet(
        tmp_path, monkeypatch, args, method, path, fields):
    calls = []
    monkeypatch.setenv("DEVBRAIN_SESSION_ID", "actor")
    monkeypatch.setenv("DEVBRAIN_PACKET_ID", "old-launcher-packet")
    monkeypatch.setattr(cli, "ensure_service", lambda settings: calls.append(settings))

    def handler(request):
        assert request.method == method and request.url.path == path
        assert json.loads(request.content) == {**fields, "session_id": "actor"}
        assert request.headers["Authorization"] == f"Bearer {calls[0].token}"
        return httpx.Response(200, json={"status": "accepted"})

    _transport(monkeypatch, handler)
    result = CliRunner().invoke(cli.app, ["--data-dir", str(tmp_path / "data"), "memory", *args])
    assert result.exit_code == 0, result.output or repr(result.exception)
    assert json.loads(result.output)["status"] == "accepted"
    assert len(calls) == 1


def test_a_snapshot_longer_than_the_wait_says_it_goes_on_and_how_to_check(tmp_path, monkeypatch):
    # #158: a large project took longer than the ordinary 150 s wait; the CLI said only "timed out".
    waited = []
    monkeypatch.setattr(cli, "ensure_service", lambda settings: None)

    def handler(request):
        waited.append(request.extensions["timeout"]["read"])
        raise httpx.ReadTimeout("timed out", request=request)

    _transport(monkeypatch, handler)
    result = CliRunner().invoke(cli.app, ["--data-dir", str(tmp_path / "data"), "memory", "snapshot", "demo"])
    assert result.exit_code == 2
    assert waited[0] >= 1800
    text = " ".join(result.output.replace("│", " ").split())  # the error box wraps the message
    assert "still being written by the service" in text and "cairnmind memory status demo" in text


@pytest.mark.parametrize("args", [[], ["--enable", "--disable"], ["--enable", "--interval-minutes", "0"]])
def test_invalid_auto_options_do_not_initialize(tmp_path, monkeypatch, args):
    _no_diagnostic_mutations(monkeypatch)
    target = tmp_path / "missing"
    result = CliRunner().invoke(cli.app, ["--data-dir", str(target), "memory", "auto", "demo", *args])
    assert result.exit_code == 2
    assert not target.exists()


@pytest.mark.parametrize(("method", "path", "body"), [
    ("POST", "/memory/preview", {"project": "transport"}),
    ("POST", "/memory/snapshot", {"project": "transport"}),
    ("POST", "/memory/inspect", {"path": "/does-not-exist"}),
    ("POST", "/memory/import", {"path": "/does-not-exist", "root": "/new-root"}),
    ("PATCH", "/memory/settings", {"project": "transport", "enabled": True}),
    ("GET", "/memory/status?project=transport", None),
])
def test_all_memory_endpoints_require_authentication(memory_workspace, method, path, body):
    store, _, _ = memory_workspace
    with TestClient(create_app(store.settings, start_worker=False, ollama=NoInference())) as client:
        result = client.request(method, path, json=body)
        assert result.status_code == 401


def test_http_preview_snapshot_and_auto_never_call_models(memory_workspace):
    store, project, root = memory_workspace
    with TestClient(create_app(store.settings, start_worker=False, ollama=NoInference())) as client:
        headers = _headers(store)
        preview = client.post("/memory/preview", json={"project": project["id"]}, headers=headers)
        assert preview.status_code == 200, preview.text
        assert preview.json()["counts"]["records"] == 1
        assert not (root / ".memory" / "cairnmind" / "manifest.json").exists()
        saved = client.post("/memory/snapshot", json={"project": project["id"]}, headers=headers)
        assert saved.status_code == 200, saved.text
        assert saved.json()["status"] == "created"
        assert (root / ".memory" / "cairnmind" / "manifest.json").is_file()
        config = client.patch("/memory/settings", json={"project": project["id"], "enabled": True}, headers=headers)
        assert config.status_code == 200, config.text
        status = client.get("/memory/status", params={"project": project["id"]}, headers=headers)
        assert status.status_code == 200, status.text
        assert status.json()["auto"]["enabled"] is True
        assert status.json()["auto"]["interval_minutes"] == 15


def test_invalid_memory_packages_return_structured_errors_without_writes(memory_workspace, tmp_path):
    store, _, root = memory_workspace
    before = _unchanging_export(store)
    with TestClient(create_app(store.settings, start_worker=False, ollama=NoInference())) as client:
        headers = _headers(store)
        body = {"path": str(tmp_path / "missing-manifest.json")}
        inspected = client.post("/memory/inspect", json=body, headers=headers)
        assert inspected.status_code == 400, inspected.text
        assert isinstance(inspected.json()["detail"], str)
        imported = client.post("/memory/import", json={**body, "root": str(root)}, headers=headers)
        assert imported.status_code == 400, imported.text
        invalid_flag = client.post("/memory/import", json={**body, "root": str(root), "dry_run": "true"}, headers=headers)
        assert invalid_flag.status_code == 422
        assert _unchanging_export(store) == before


def test_external_memory_change_is_http_conflict_and_never_overwritten(memory_workspace):
    store, project, _ = memory_workspace
    saved = _snapshot(store, project)
    path = Path(saved["path"])
    modified = path.read_bytes() + b" \n"
    path.write_bytes(modified)
    before = _unchanging_export(store)
    with TestClient(create_app(store.settings, start_worker=False, ollama=NoInference())) as client:
        result = client.post("/memory/snapshot", json={"project": project["id"]}, headers=_headers(store))
        assert result.status_code == 409, result.text
        assert "externally" in result.json()["detail"]
        assert path.read_bytes() == modified
        assert _unchanging_export(store) == before


def test_snapshot_and_settings_require_current_actor_ack_and_worktree(memory_workspace, tmp_path):
    store, project, root = memory_workspace
    manager = SessionManager(store, store.settings)
    prepared = manager.prepare(project["id"], "codex", timeout=0)
    sid = prepared["session_id"]
    with TestClient(create_app(store.settings, start_worker=False, ollama=NoInference())) as client:
        headers = _headers(store)
        body = {"project": project["id"], "session_id": sid}
        denied = client.post("/memory/snapshot", json=body, headers=headers)
        assert denied.status_code == 409 and "resume_required" in denied.text
        assert not (root / ".memory" / "cairnmind" / "manifest.json").exists()
        denied = client.patch("/memory/settings", json={**body, "enabled": True}, headers=headers)
        assert denied.status_code == 409
        packet = manager.resume_context(sid)["packet"]
        manager.ack(sid, packet["id"], packet["hash"], {"plan": None, "task": None})
        mismatch = client.post("/memory/snapshot", json={**body, "worktree": str(tmp_path / "elsewhere")},
                               headers=headers)
        assert mismatch.status_code == 409
        accepted = client.post("/memory/snapshot", json=body, headers=headers)
        assert accepted.status_code == 200, accepted.text


def test_snapshot_rechecks_ack_after_preflight_before_capture(memory_workspace, monkeypatch):
    store, project, root = memory_workspace
    sessions = SessionManager(store, store.settings)
    prepared = sessions.prepare(project["id"], "codex", timeout=0)
    packet = prepared["packet"]
    sessions.ack(prepared["session_id"], packet["id"], packet["hash"], {"plan": None, "task": None})
    app = create_app(store.settings, start_worker=False, ollama=NoInference())
    capture = app.state.memory._capture
    changed = []

    def concurrent_change(*args, **kwargs):
        if kwargs.get("authorize"):
            source = store.snapshot(project["id"], "manual://concurrent", "A new decision changed project state.", "manual")
            store.remember(project["id"], {"kind": "decision", "content": source["content"]}, [source["id"]])
            changed.append(True)
        return capture(*args, **kwargs)

    monkeypatch.setattr(app.state.memory, "_capture", concurrent_change)
    with TestClient(app) as client:
        result = client.post("/memory/snapshot", json={"project": project["id"],
            "session_id": prepared["session_id"]}, headers=_headers(store))
        assert result.status_code == 409, result.text
        assert "resume_required" in result.text
        assert changed == [True]
        assert not (root / ".memory" / "cairnmind" / "manifest.json").exists()


def test_http_import_is_atomic_project_scoped_and_dry_run_noop(memory_workspace, tmp_path):
    store, project, _ = memory_workspace
    saved = _snapshot(store, project)
    other = Store(tmp_path / "other-data")
    unrelated_root = tmp_path / "unrelated"
    unrelated_root.mkdir()
    other_project = other.add_project("untouched", unrelated_root)
    source = other.snapshot(other_project["id"], "manual://private", "other_project_private", "manual")
    other.remember(other_project["id"], {"kind": "fact", "content": source["content"]}, [source["id"]])
    before = _unchanging_export(other)
    destination = tmp_path / "destination"
    destination.mkdir()
    body = {"path": saved["path"], "root": str(destination)}
    with TestClient(create_app(other.settings, start_worker=False, ollama=NoInference())) as client:
        headers = _headers(other)
        dry = client.post("/memory/import", json={**body, "dry_run": True}, headers=headers)
        assert dry.status_code == 200, dry.text
        assert dry.json()["status"] == "dry_run"
        assert _unchanging_export(other) == before
        result = client.post("/memory/import", json=body, headers=headers)
        assert result.status_code == 200, result.text
        assert result.json()["project"]["id"] == project["id"]
        assert other.resolve_project(project["id"])["root"] == str(destination)
        again = client.post("/memory/import", json=body, headers=headers)
        assert again.status_code == 200 and again.json()["status"] == "unchanged"
        assert other.list_records(other_project["id"])[0]["content"] == "other_project_private"


def test_actor_cannot_import_another_project_or_another_root(memory_workspace, tmp_path):
    store, project, root = memory_workspace
    manager = SessionManager(store, store.settings)
    prepared = manager.prepare(project["id"], "codex", timeout=0)
    sid = prepared["session_id"]
    packet = prepared["packet"]
    manager.ack(sid, packet["id"], packet["hash"], {"plan": None, "task": None})
    saved = _snapshot(store, project)
    foreign = Store(tmp_path / "foreign-data")
    foreign_root = tmp_path / "foreign-root"
    foreign_root.mkdir()
    foreign_project = foreign.add_project("foreign", foreign_root)
    foreign_saved = _snapshot(foreign, foreign_project)
    before = _unchanging_export(store)
    with TestClient(create_app(store.settings, start_worker=False, ollama=NoInference())) as client:
        headers = _headers(store)
        wrong_root = client.post("/memory/import", json={"path": saved["path"],
            "root": str(tmp_path / "wrong-root"), "session_id": sid}, headers=headers)
        assert wrong_root.status_code == 409, wrong_root.text
        wrong_project = client.post("/memory/import", json={"path": foreign_saved["path"],
            "root": str(root), "session_id": sid}, headers=headers)
        assert wrong_project.status_code == 409, wrong_project.text
        assert _unchanging_export(store) == before


@pytest.mark.parametrize("body", [{"enabled": "true"}, {"enabled": True, "interval_minutes": True},
                                  {"enabled": True, "interval_minutes": 0}])
def test_memory_settings_reject_coerced_or_invalid_values(memory_workspace, body):
    store, project, _ = memory_workspace
    with TestClient(create_app(store.settings, start_worker=False, ollama=NoInference())) as client:
        result = client.patch("/memory/settings", json={"project": project["id"], **body}, headers=_headers(store))
        assert result.status_code == 422


@pytest.mark.parametrize(("status", "exit_code"), [("passed", 0), ("failed", 1), ("not_run", 3)])
def test_memory_evaluation_cli_preserves_exit_contract(tmp_path, monkeypatch, status, exit_code):
    from cairnmind import memory_evaluation
    calls = []

    def evaluate(settings, *, report_dir=None):
        calls.append((settings.data_dir, report_dir))
        return {"status": status, "model_calls": 0}

    monkeypatch.setattr(memory_evaluation, "run_memory_evaluation", evaluate)
    data, report = tmp_path / "data", tmp_path / "reports"
    result = CliRunner().invoke(cli.app, ["--data-dir", str(data), "eval", "memory", "--report-dir", str(report)])
    assert result.exit_code == exit_code, result.output or repr(result.exception)
    assert json.loads(result.output)["status"] == status
    assert calls == [(data, report)]
