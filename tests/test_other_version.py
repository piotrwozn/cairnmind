"""A client that reaches a knowledge service of another version sends it nothing, and `stop` and `doctor` still
work against it (#319)."""
from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from cairnmind import __version__, cli, integrations
from cairnmind.config import Settings
from cairnmind.service_version import OTHER_VERSION, instance_id

runner = CliRunner()
REAL = httpx.Client
OLD = "0.11.0"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("CAIRNMIND_PORT", "1")  # nothing here may reach the person's own service
    for name in ("CAIRNMIND_SESSION_ID", "DEVBRAIN_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)


def old_service(monkeypatch, settings, version=OLD):
    """A service of another version answers on the port; every request it gets is recorded."""
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.url.path == "/health":
            return httpx.Response(200, json={"service": "cairnmind", "version": version,
                                             "instance": instance_id(settings), "backend": "arcadedb"})
        if request.url.path == "/shutdown":
            return httpx.Response(200, json={"stopping": True})
        return httpx.Response(404, json={"detail": "not found"})

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: REAL(transport=httpx.MockTransport(handler), **kwargs))
    return seen


def sentence(settings, version=OLD):
    return OTHER_VERSION.format(service=version, client=__version__,
                                command=f"cairnmind --data-dir {settings.data_dir} stop")


def forbidden(*args, **kwargs):
    raise AssertionError("something was started")


def invoke(settings, *arguments):
    return runner.invoke(cli.app, ["--data-dir", str(settings.data_dir), *arguments])


def test_a_command_sends_nothing_to_a_service_of_another_version(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data", create=True)
    seen = old_service(monkeypatch, settings)
    monkeypatch.setattr(cli.subprocess, "Popen", forbidden)
    result = invoke(settings, "index", "demo")
    assert result.exit_code == 2, result.output
    assert " ".join(result.output.split()).startswith("Error: " + " ".join(sentence(settings).split())[:80])
    assert [path for _, path, _ in seen] == ["/health", "/health"]  # service_alive, then the version: no write


@pytest.mark.parametrize("arguments", [["status"], ["jobs", "list", "demo"], ["memory", "status", "demo"]])
def test_status_and_the_other_diagnostics_refuse_a_service_of_another_version(tmp_path, monkeypatch, arguments):
    settings = Settings.load(tmp_path / "data", create=True)
    seen = old_service(monkeypatch, settings)
    result = invoke(settings, *arguments)
    assert result.exit_code == 2, result.output
    assert " ".join(sentence(settings).split()) in " ".join(result.output.split())
    assert {path for _, path, _ in seen} == {"/health"}


def test_stop_and_doctor_still_reach_a_service_of_another_version(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data", create=True)
    seen = old_service(monkeypatch, settings)
    monkeypatch.setattr(integrations, "doctor", lambda settings: {})
    report = json.loads(invoke(settings, "doctor").output)
    assert report["service_running"] is True
    assert " ".join(sentence(settings).split()) in [" ".join(line.split()) for line in report["environment_notes"]]
    seen.clear()
    result = invoke(settings, "stop")
    assert result.exit_code == 0, result.output
    assert ("POST", "/shutdown", f"Bearer {settings.token}") in seen
    assert json.loads(result.output) == {"stopping": True}


def test_run_refuses_before_anything_starts(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data", create=True)
    old_service(monkeypatch, settings)
    monkeypatch.setattr(cli.subprocess, "Popen", forbidden)
    monkeypatch.setattr(cli, "store_for", forbidden)
    result = invoke(settings, "run", "demo", "claude")
    assert result.exit_code == 2, result.output
    assert " ".join(sentence(settings).split()) in " ".join(result.output.split())
