"""CLI diagnostics never initialize or start the local knowledge service."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from devbrain import cli
from devbrain.config import Settings


def no_start(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("A diagnostic initialized storage or started a process")
    monkeypatch.setattr(cli, "ensure_service", forbidden)
    monkeypatch.setattr(cli, "store_for", forbidden)
    monkeypatch.setattr(cli.subprocess, "Popen", forbidden)
    monkeypatch.setattr("devbrain.engine.start_engine", forbidden)


def no_initialization(monkeypatch):
    original = Settings.load

    def load(cls, data_dir=None, *, create=False):
        assert create is False, "Diagnostic settings were initialized"
        return original(data_dir, create=False)

    monkeypatch.setattr(Settings, "load", classmethod(load))


def transport(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(handler)))


def files(root):
    return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("arguments", [
    ["--help"], ["status", "--help"], ["project", "--help"],
    ["project", "add", "--help"], ["models", "pull", "--help"],
    ["engine", "start", "--help"], ["session", "ack", "--help"],
    ["plan", "save", "--help"], ["task", "update", "--help"],
    ["check", "run", "--help"], ["eval", "run", "--help"],
    ["jobs", "--help"], ["jobs", "list", "--help"],
    ["jobs", "show", "--help"], ["jobs", "retry", "--help"],
])
def test_nested_help_does_not_create_settings_or_start_anything(tmp_path, monkeypatch, arguments):
    data = tmp_path / "missing" / "memory"
    no_start(monkeypatch)
    no_initialization(monkeypatch)
    result = CliRunner().invoke(cli.app, ["--data-dir", str(data), *arguments])
    assert result.exit_code == 0, result.output or repr(result.exception)
    assert not data.parent.exists()


def test_settings_read_without_create_leaves_missing_directory_absent(tmp_path):
    data = tmp_path / "missing" / "memory"
    settings = Settings.load(data, create=False)
    assert settings.data_dir == data
    assert not settings.token
    assert not data.parent.exists()


@pytest.mark.parametrize("arguments", [
    ["status"], ["status", "--project", "demo"],
    ["jobs", "list", "demo"], ["jobs", "show", "demo", "job-one"],
])
def test_diagnostics_require_existing_config_without_creating_it(tmp_path, monkeypatch, arguments):
    data = tmp_path / "missing" / "memory"
    no_start(monkeypatch)
    no_initialization(monkeypatch)
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: pytest.fail("No config must mean no HTTP request"))
    result = CliRunner().invoke(cli.app, ["--data-dir", str(data), *arguments])
    assert result.exit_code == 2
    assert "devbrain init" in result.output
    assert not data.parent.exists()


@pytest.mark.parametrize(("arguments", "path", "params"), [
    (["status", "--project", "demo"], "/status", {"project": "demo"}),
    (["jobs", "list", "demo", "--state", "failed", "--kind", "extract", "--limit", "7"],
     "/jobs", {"project": "demo", "state": "failed", "kind": "extract", "limit": "7",
               "session_id": "logical-session"}),
    (["jobs", "list", "demo", "--status", "failed"], "/jobs",
     {"project": "demo", "state": "failed", "limit": "50", "session_id": "logical-session"}),
    (["jobs", "show", "demo", "job-one"], "/jobs/job-one",
     {"project": "demo", "session_id": "logical-session"}),
])
def test_diagnostic_http_reads_are_authenticated_scoped_and_do_not_write_config(
        tmp_path, monkeypatch, arguments, path, params):
    settings = Settings.load(tmp_path / "memory", create=True)
    before = files(settings.data_dir)
    no_start(monkeypatch)
    no_initialization(monkeypatch)
    monkeypatch.setenv("DEVBRAIN_SESSION_ID", "logical-session")
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == path
        assert dict(request.url.params) == params
        assert request.headers["Authorization"] == f"Bearer {settings.token}"
        return httpx.Response(200, json={"diagnostic": True})

    transport(monkeypatch, handler)
    result = CliRunner().invoke(cli.app, ["--data-dir", str(settings.data_dir), *arguments])
    assert result.exit_code == 0, result.output or repr(result.exception)
    assert json.loads(result.output) == {"diagnostic": True}
    assert len(requests) == 1
    assert files(settings.data_dir) == before


@pytest.mark.parametrize("arguments", [
    ["status"], ["jobs", "list", "demo"], ["jobs", "show", "demo", "job-one"],
])
def test_unavailable_diagnostics_do_not_autostart_service(tmp_path, monkeypatch, arguments):
    settings = Settings.load(tmp_path / "memory", create=True)
    before = files(settings.data_dir)
    no_start(monkeypatch)
    no_initialization(monkeypatch)

    def unavailable(request):
        raise httpx.ConnectError("offline", request=request)

    transport(monkeypatch, unavailable)
    result = CliRunner().invoke(cli.app, ["--data-dir", str(settings.data_dir), *arguments])
    assert result.exit_code == 2
    assert "devbrain serve" in result.output
    assert files(settings.data_dir) == before


@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("include_omissions", [False, True])
def test_retry_one_job_sends_expected_attempts_and_inherited_session(
        tmp_path, monkeypatch, bound, include_omissions):
    data = tmp_path / "memory"
    if bound:
        monkeypatch.setenv("DEVBRAIN_SESSION_ID", "logical-session")
        monkeypatch.setenv("DEVBRAIN_PACKET_ID", "exact-packet")
    else:
        monkeypatch.delenv("DEVBRAIN_SESSION_ID", raising=False)
        monkeypatch.delenv("DEVBRAIN_PACKET_ID", raising=False)
    calls = []
    monkeypatch.setattr(cli, "ensure_service", lambda settings: calls.append(settings))
    monkeypatch.setattr(cli, "store_for", lambda *args: pytest.fail("Retry must use the guarded HTTP API"))

    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/jobs/job-one/retry"
        expected = {"project": "demo", "expected_attempts": 3}
        if include_omissions:
            expected["include_omissions"] = True
        if bound:
            # The launcher's environment packet can predate a successful MCP
            # refresh. Let the service verify this session's current ACK.
            expected.update(session_id="logical-session")
        assert json.loads(request.content) == expected
        assert request.headers["Authorization"] == f"Bearer {calls[0].token}"
        return httpx.Response(200, json={"job": {"id": "job-one"}, "retried": True})

    transport(monkeypatch, handler)
    result = CliRunner().invoke(cli.app, ["--data-dir", str(data), "jobs", "retry", "demo", "job-one",
                                         "--expected-attempts", "3",
                                         *(["--include-omissions"] if include_omissions else [])])
    assert result.exit_code == 0, result.output or repr(result.exception)
    assert json.loads(result.output)["retried"] is True
    assert len(calls) == 1 and calls[0].token
    assert (data / "config.json").is_file()


def test_retry_surfaces_server_conflict_without_retrying_the_request(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ensure_service", lambda settings: None)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(409, json={"detail": "stale expected_attempts"})

    transport(monkeypatch, handler)
    result = CliRunner().invoke(cli.app, ["--data-dir", str(tmp_path / "memory"), "jobs", "retry", "demo",
                                         "job-one", "--expected-attempts", "0"])
    assert result.exit_code == 2
    assert "stale expected_attempts" in result.output
    assert len(requests) == 1


@pytest.mark.parametrize("arguments", [
    ["jobs", "list", "demo", "--limit", "0"],
    ["jobs", "list", "demo", "--limit", "101"],
    ["jobs", "retry", "demo", "job-one"],
    ["jobs", "retry", "demo", "job-one", "--expected-attempts", "-1"],
])
def test_invalid_job_arguments_do_not_initialize_settings(tmp_path, monkeypatch, arguments):
    data = tmp_path / "memory"
    no_start(monkeypatch)
    no_initialization(monkeypatch)
    result = CliRunner().invoke(cli.app, ["--data-dir", str(data), *arguments])
    assert result.exit_code == 2
    assert not data.exists()


def test_explicit_init_and_explicit_tui_still_initialize(tmp_path, monkeypatch):
    import sys
    from types import ModuleType

    initialized = []

    def store(settings):
        assert settings.token and (settings.data_dir / "config.json").is_file()
        initialized.append(settings.data_dir)

    module = ModuleType("devbrain.workspace")

    def launch(settings):
        store(settings)
        initialized.append("tui ran")
        return 0

    module.launch_workspace = launch

    monkeypatch.setattr(cli, "store_for", store)
    monkeypatch.setitem(sys.modules, "devbrain.workspace", module)
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: True)
    runner = CliRunner()
    first = runner.invoke(cli.app, ["--data-dir", str(tmp_path / "init"), "init"])
    assert first.exit_code == 0, first.output or repr(first.exception)
    second = runner.invoke(cli.app, ["--data-dir", str(tmp_path / "tui"), "tui"])
    assert second.exit_code == 0, second.output or repr(second.exception)
    assert initialized == [tmp_path / "init", tmp_path / "tui", "tui ran"]


def test_models_pull_preserves_failed_jobs_and_only_schedules_embeddings(tmp_path, monkeypatch):
    data = tmp_path / "memory"
    retries, embeddings = [], []
    store = SimpleNamespace(retry_jobs=lambda: retries.append("implicit broad retry"))
    monkeypatch.setattr(cli, "store_for", lambda settings: store)
    monkeypatch.setattr(cli, "_retry_jobs", lambda store: retries.append("implicit helper retry"))
    monkeypatch.setattr("devbrain.service.schedule_embeddings", lambda store, namespace: embeddings.append(namespace))

    def handler(request):
        if request.url.path == "/api/pull":
            return httpx.Response(200, content='{"status":"success"}\n')
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"name": "qwen3-embedding:0.6b", "digest": "fixture"}]})

    transport(monkeypatch, handler)
    result = CliRunner().invoke(cli.app, ["--data-dir", str(data), "models", "pull"])
    assert result.exit_code == 0, result.output or repr(result.exception)
    assert retries == []
    assert embeddings == ["qwen3-embedding:0.6b@fixture"]
    assert (data / "models.lock.json").is_file()
    assert Settings.load(data).token


def test_legacy_explicit_retry_jobs_still_uses_audited_store_helper(tmp_path, monkeypatch):
    retries = []
    monkeypatch.delenv("DEVBRAIN_SESSION_ID", raising=False)
    monkeypatch.setattr(cli, "store_for", lambda settings: SimpleNamespace(retry_jobs=lambda: retries.append(True)))
    result = CliRunner().invoke(cli.app, ["--data-dir", str(tmp_path / "memory"), "retry-jobs"])
    assert result.exit_code == 0, result.output or repr(result.exception)
    assert retries == [True]


def test_bound_session_cannot_use_legacy_global_retry_jobs(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVBRAIN_SESSION_ID", "logical-session")
    monkeypatch.setattr(cli, "store_for", lambda *args: pytest.fail("Bound actor reached global retry helper"))
    result = CliRunner().invoke(cli.app, ["--data-dir", str(tmp_path / "memory"), "retry-jobs"])
    assert result.exit_code == 2
    assert "jobs retry" in result.output


@pytest.mark.parametrize("include_omissions", [False, True])
def test_retry_uses_current_ack_after_refresh_despite_old_launcher_packet(tmp_path, monkeypatch, include_omissions):
    """Exercise the actual service gate and native ArcadeDB, with a test-client ACK."""
    import subprocess

    from fastapi.testclient import TestClient

    from devbrain.service import create_app
    from devbrain.sessions import SessionManager
    from devbrain.store import Store

    root = tmp_path / "repo"
    root.mkdir()
    code = root / "app.py"
    code.write_text("VALUE = 1\n")
    for args in (("init",), ("config", "user.email", "fixture@example.invalid"),
                 ("config", "user.name", "Fixture"), ("add", "."), ("commit", "-m", "fixture")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    settings = Settings.load(tmp_path / "memory", create=True)
    store = Store(settings.data_dir)
    project = store.add_project("demo", root)
    manager = SessionManager(store, settings)
    initial = manager.prepare(project["id"], "codex", timeout=0)
    assert initial["status"] == "ready", initial
    code.write_text("VALUE = 2\n")
    refreshed = manager.resume_context(initial["session_id"])
    assert refreshed["status"] == "ready", refreshed
    packet = refreshed["packet"]
    assert packet["id"] != initial["packet"]["id"]
    manager.ack(initial["session_id"], packet["id"], packet["hash"], {"plan": None, "task": None})
    job_id = store.enqueue("extract", project["id"], {}, "cli-retry-fixture")
    claimed = store.claim_job("protocol-fixture", kinds=["extract"])
    assert claimed["id"] == job_id
    store.finish_job(job_id, "fixture failure", worker="protocol-fixture")
    if include_omissions:
        store.retry_job(project["id"], job_id, claimed["attempts"])
        claimed = store.claim_job("protocol-fixture", kinds=["extract"])
        assert claimed["id"] == job_id and claimed["attempts"] == 2
        store.set_job_diagnostics(job_id, {
            "stage": "extract", "code": "completed_with_omissions", "validation_codes": ["proposal_rejected"],
            "counts": {"candidate_records": 6, "accepted_records": 0, "written_records": 0, "skipped_records": 6}},
            expected_attempts=claimed["attempts"], worker=claimed["worker"])
        store.finish_job(job_id, worker=claimed["worker"], expected_attempts=claimed["attempts"])
    monkeypatch.setenv("DEVBRAIN_SESSION_ID", initial["session_id"])
    monkeypatch.setenv("DEVBRAIN_PACKET_ID", initial["packet"]["id"])
    monkeypatch.setattr(cli, "ensure_service", lambda settings: None)
    original = httpx.Client
    payloads = []

    with TestClient(create_app(settings, start_worker=False, ollama=object())) as service:
        class LocalService:
            def __init__(self, headers):
                self.headers = headers

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def request(self, method, url, **kwargs):
                payloads.append(kwargs["json"])
                return service.request(method, url, headers=self.headers, **kwargs)

        def client(**kwargs):
            # Native ArcadeDB connections still use the real HTTP client.
            if kwargs.get("base_url"):
                return original(**kwargs)
            return LocalService(kwargs["headers"])

        monkeypatch.setattr(cli.httpx, "Client", client)
        result = CliRunner().invoke(cli.app, ["--data-dir", str(settings.data_dir), "jobs", "retry", "demo",
                                             job_id, "--expected-attempts", str(claimed["attempts"]),
                                             *(["--include-omissions"] if include_omissions else [])])
    assert result.exit_code == 0, result.output or repr(result.exception)
    assert json.loads(result.output)["retried"] is True
    expected = {"project": "demo", "expected_attempts": claimed["attempts"], "session_id": initial["session_id"]}
    if include_omissions:
        expected["include_omissions"] = True
    assert payloads == [expected]
    assert store.query("SELECT state FROM jobs WHERE id=:id", {"id": job_id})[0]["state"] == "pending"
