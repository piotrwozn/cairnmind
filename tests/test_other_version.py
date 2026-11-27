"""A client that reaches a knowledge service of another version sends it nothing, and `stop` and `doctor` still
work against it (#319)."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from typer.testing import CliRunner

from cairnmind import __version__, cli
from cairnmind.memory import integrations
from cairnmind.mcp import mcp_server
from cairnmind.config import Settings
from cairnmind.cli_cmds.service_version import OTHER_VERSION, instance_id
from test_external_mcp_start import Network

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
    assert [path for _, path, _ in seen] == ["/health"]  # one answer is both alive and the version (#405): no write


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
    ran = invoke(settings, "doctor")
    report = json.loads(ran.output)
    assert report["service_running"] is True
    assert ran.exit_code == 1  # required and missing, so doctor exits 1 by #313's rule
    assert " ".join(sentence(settings).split()) in [" ".join(line.split()) for line in report["environment_notes"]]
    # In a terminal it is listed as required and not working, once, and the next step is to fix it (#319).
    from cairnmind.cli_cmds.doctor_attention import summary
    from cairnmind.cli_cmds.first_start import NEXT_FIX, REQUIRED_MISSING
    assert report["environment_blockers"] == [sentence(settings)]
    lines = summary(report)
    shown = [index for index, line in enumerate(lines) if line == f"  - {' '.join(sentence(settings).split())}"]
    assert len(shown) == 1 and lines.index(REQUIRED_MISSING) < shown[0] and lines[-1] == NEXT_FIX
    seen.clear()
    result = invoke(settings, "stop")
    assert result.exit_code == 0, result.output
    assert ("POST", "/shutdown", f"Bearer {settings.token}") in seen
    assert json.loads(result.output) == {"stopping": True}


def mcp_bridge(settings, tmp_path, kind):
    if kind == "native":
        return mcp_server.ServiceClient(settings)
    return mcp_server.ServiceClient(settings, external={"project": str(tmp_path), "worktree": None})


@pytest.mark.parametrize("kind", ["native", "external"])
def test_an_mcp_server_sends_nothing_to_a_service_of_another_version(tmp_path, monkeypatch, kind):
    settings = Settings.load(tmp_path / "data", create=True)
    old_service(monkeypatch, settings)
    network = Network(monkeypatch)
    network.up = True
    monkeypatch.setattr(cli, "ensure_service", forbidden)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(mcp_bridge(settings, tmp_path, kind).post("/ask", {"project": "p", "question": "q"}))
    assert str(caught.value) == sentence(settings)
    assert network.seen == []


def test_an_mcp_server_of_the_same_version_goes_on(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data", create=True)
    old_service(monkeypatch, settings, version=__version__)
    network = Network(monkeypatch)
    network.up = True
    answer = asyncio.run(mcp_bridge(settings, tmp_path, "native").post("/ask", {"project": "p", "question": "q"}))
    assert answer == {"answer": "yes"} and network.seen == ["/ask"]


def test_an_external_server_that_starts_a_service_names_a_version_it_meets(tmp_path, monkeypatch):
    """The start (after a refused connection) meets a service of another version: its own sentence, not
    `cairnmind serve`."""
    settings = Settings.load(tmp_path / "data", create=True)
    monkeypatch.setattr(cli, "service_alive", lambda settings: True)
    old_service(monkeypatch, settings)
    with pytest.raises(RuntimeError) as caught:
        mcp_server.start_service(settings)
    assert str(caught.value) == sentence(settings)


def test_the_workspace_refuses_a_service_of_another_version_before_a_broker_starts(tmp_path, monkeypatch):
    from cairnmind.terminal import cockpit
    settings = Settings.load(tmp_path / "data", create=True)
    old_service(monkeypatch, settings)
    monkeypatch.setattr(cockpit.subprocess, "Popen", forbidden)
    with pytest.raises(cli.ServiceOfOtherVersion) as refused:
        cockpit.ensure_cockpit(settings)
    assert isinstance(refused.value, cli.ServiceDidNotStart) and str(refused.value) == sentence(settings)


def test_run_refuses_before_anything_starts(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data", create=True)
    old_service(monkeypatch, settings)
    monkeypatch.setattr(cli.subprocess, "Popen", forbidden)
    monkeypatch.setattr(cli, "store_for", forbidden)
    result = invoke(settings, "run", "demo", "claude")
    assert result.exit_code == 2, result.output
    assert " ".join(sentence(settings).split()) in " ".join(result.output.split())
