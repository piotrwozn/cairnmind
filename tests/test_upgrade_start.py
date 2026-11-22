"""While the installer replaces Cairnmind, no automatic start brings up the service (#298)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from cairnmind import cli, upgrade_mark
from cairnmind.config import Settings

runner = CliRunner()
RUNNING = ("running", 4242)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("CAIRNMIND_PORT", "1")  # nothing here may reach the person's own service
    for name in ("CAIRNMIND_SESSION_ID", "DEVBRAIN_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)


def forbidden(*args, **kwargs):
    raise AssertionError("a service was started")


def test_ensure_service_starts_nothing_during_an_upgrade(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data")
    monkeypatch.setattr(cli, "service_alive", lambda settings: False)
    monkeypatch.setattr(upgrade_mark, "state", lambda: RUNNING)
    monkeypatch.setattr(cli.subprocess, "Popen", forbidden)
    with pytest.raises(cli.UpgradeInProgress) as refused:
        cli.ensure_service(settings)
    assert isinstance(refused.value, cli.ServiceDidNotStart)
    assert str(refused.value) == upgrade_mark.NOTE_RUNNING.format(pid=4242)
    assert not (settings.data_dir / "service.log").exists()


def test_a_running_service_is_still_used_during_an_upgrade_so_stop_works(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data")
    monkeypatch.setattr(cli, "service_alive", lambda settings: True)
    monkeypatch.setattr(upgrade_mark, "state", lambda: RUNNING)
    monkeypatch.setattr(cli.subprocess, "Popen", forbidden)
    cli.ensure_service(settings)


def began_while_starting(tmp_path, monkeypatch, status):
    """No mark at the first check, a running installer once the started service answers."""
    settings = Settings.load(tmp_path / "data", create=True)
    answers = iter([None, upgrade_mark.NOTE_RUNNING.format(pid=4242)])
    monkeypatch.setattr(upgrade_mark, "refusal", lambda: next(answers))
    alive = iter([False, True])
    monkeypatch.setattr(cli, "service_alive", lambda settings: next(alive))
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(poll=lambda: None))
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.headers.get("authorization")))
        return httpx.Response(status)

    real = httpx.Client
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: real(transport=httpx.MockTransport(handler)))
    return settings, seen


def test_an_upgrade_that_begins_while_the_service_starts_stops_that_service(tmp_path, monkeypatch):
    settings, seen = began_while_starting(tmp_path, monkeypatch, 200)
    with pytest.raises(cli.UpgradeInProgress) as refused:
        cli.ensure_service(settings)
    assert str(refused.value) == upgrade_mark.NOTE_RUNNING.format(pid=4242)
    assert seen == [("POST", "/shutdown", f"Bearer {settings.token}")]


def test_a_service_that_does_not_stop_is_named_with_the_stop_command(tmp_path, monkeypatch):
    settings, seen = began_while_starting(tmp_path, monkeypatch, 500)
    with pytest.raises(cli.UpgradeInProgress) as refused:
        cli.ensure_service(settings)
    command = f"cairnmind --data-dir {settings.data_dir} stop"
    assert str(refused.value) == upgrade_mark.UPGRADE_STOP_FAILED.format(command=command)


def test_ask_during_an_upgrade_is_refused_with_the_reason(tmp_path, monkeypatch):
    Settings.load(tmp_path / "data", create=True)
    monkeypatch.setattr(upgrade_mark, "state", lambda: RUNNING)
    monkeypatch.setattr(cli.subprocess, "Popen", forbidden)
    result = runner.invoke(cli.app, ["--data-dir", str(tmp_path / "data"), "ask", "p", "q"])
    assert result.exit_code == 2, result.output
    assert result.output.startswith("Error: An upgrade of Cairnmind is in progress"), result.output


def test_status_during_an_upgrade_names_it_instead_of_cairnmind_serve(tmp_path, monkeypatch):
    Settings.load(tmp_path / "data", create=True)
    monkeypatch.setattr(upgrade_mark, "state", lambda: RUNNING)
    result = runner.invoke(cli.app, ["--data-dir", str(tmp_path / "data"), "status"])
    assert result.exit_code == 2, result.output
    assert upgrade_mark.NOTE_RUNNING.format(pid=4242) in " ".join(result.output.split())
    assert "cairnmind serve" not in result.output


@pytest.mark.parametrize("state, note", [
    (("running", 4242), upgrade_mark.NOTE_RUNNING.format(pid=4242)),
    (("stale", 4242), upgrade_mark.NOTE_STALE.format(pid=4242)),
    (("unreadable", None), upgrade_mark.NOTE_UNREADABLE),
    (("none", None), None),
])
def test_doctor_notes_the_upgrade_state(tmp_path, monkeypatch, state, note):
    from cairnmind import integrations
    Settings.load(tmp_path / "data", create=True)
    monkeypatch.setattr(integrations, "doctor", lambda settings: {})
    monkeypatch.setattr(upgrade_mark, "state", lambda: state)
    report = json.loads(runner.invoke(cli.app, ["--data-dir", str(tmp_path / "data"), "doctor"]).output)
    notes = [" ".join(line.split()) for line in report["environment_notes"]]
    assert (note in notes) if note else not any("upgrade" in line.lower() for line in notes)
