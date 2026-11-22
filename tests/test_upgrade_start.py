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


def test_a_coding_session_is_refused_during_an_upgrade_even_beside_a_running_service(tmp_path, monkeypatch, capsys):
    import typer
    from cairnmind import integrations
    settings = Settings.load(tmp_path / "data")
    monkeypatch.setattr(cli, "service_alive", lambda settings: True)
    monkeypatch.setattr(upgrade_mark, "state", lambda: RUNNING)
    monkeypatch.setattr(cli.subprocess, "Popen", forbidden)
    with pytest.raises(typer.Exit):
        cli._refuse_during_upgrade()  # what `cairnmind run` calls first
    assert upgrade_mark.NOTE_RUNNING.format(pid=4242) in capsys.readouterr().err
    with pytest.raises(cli.UpgradeInProgress):  # a session the workspace prepares
        integrations._prepare_session(settings, {"id": "p"}, "claude", tmp_path, None)


def began_while_starting(tmp_path, monkeypatch, status):
    """No mark at the first check, a running installer once the started service answers."""
    settings = Settings.load(tmp_path / "data", create=True)
    calls = []
    monkeypatch.setattr(upgrade_mark, "refusal", lambda: calls.append(1) or (
        upgrade_mark.NOTE_RUNNING.format(pid=4242) if len(calls) > 1 else None))
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


def test_an_installer_that_began_and_ended_while_the_service_started_still_stops_that_service(tmp_path, monkeypatch):
    """The mark is there for one pass of the wait only: the service answers after the installer has removed it."""
    settings, seen = began_while_starting(tmp_path, monkeypatch, 200)
    marks = iter([None, upgrade_mark.NOTE_RUNNING.format(pid=4242), None])
    monkeypatch.setattr(upgrade_mark, "refusal", lambda: next(marks, None))
    alive = iter([False, False, True])
    monkeypatch.setattr(cli, "service_alive", lambda settings: next(alive))
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    with pytest.raises(cli.UpgradeInProgress):
        cli.ensure_service(settings)
    assert seen == [("POST", "/shutdown", f"Bearer {settings.token}")]


def test_an_upgrade_whose_mark_is_gone_by_then_is_told_as_one_that_happened_not_one_in_progress(tmp_path, monkeypatch):
    """A mark seen once during the wait, gone when the service answers: the service is still stopped (no old code
    survives), and the text is true: call again (#298)."""
    settings, seen = began_while_starting(tmp_path, monkeypatch, 200)
    marks = iter([None, upgrade_mark.NOTE_RUNNING.format(pid=4242)])
    monkeypatch.setattr(upgrade_mark, "refusal", lambda: next(marks, None))
    with pytest.raises(cli.UpgradeInProgress) as refused:
        cli.ensure_service(settings)
    assert str(refused.value) == upgrade_mark.UPGRADE_ENDED and "in progress" not in str(refused.value)
    assert seen == [("POST", "/shutdown", f"Bearer {settings.token}")]


def test_a_start_that_outlasts_the_wait_while_an_upgrade_began_is_ended_and_says_why(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data", create=True)
    calls = []  # none at the gate, an installer from the first pass of the wait
    monkeypatch.setattr(upgrade_mark, "refusal", lambda: calls.append(1) or (
        upgrade_mark.NOTE_RUNNING.format(pid=4242) if len(calls) > 1 else None))
    monkeypatch.setattr(cli, "service_alive", lambda settings: False)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    ended = []
    monkeypatch.setattr(cli.subprocess, "Popen",
                        lambda *args, **kwargs: SimpleNamespace(poll=lambda: None, terminate=lambda: ended.append(1)))
    with pytest.raises(cli.UpgradeInProgress) as refused:
        cli.ensure_service(settings)
    assert str(refused.value) == upgrade_mark.NOTE_RUNNING.format(pid=4242)
    assert ended == [1]


def test_no_broker_starts_during_an_upgrade_even_beside_a_running_service(tmp_path, monkeypatch):
    from cairnmind import cockpit
    settings = Settings.load(tmp_path / "data", create=True)
    monkeypatch.setattr(cli, "service_alive", lambda settings: True)
    monkeypatch.setattr(upgrade_mark, "state", lambda: RUNNING)
    monkeypatch.setattr(cli, "ensure_service", forbidden)
    monkeypatch.setattr(cockpit.subprocess, "Popen", forbidden)
    with pytest.raises(cli.UpgradeInProgress) as refused:
        cockpit.ensure_cockpit(settings)
    assert str(refused.value) == upgrade_mark.NOTE_RUNNING.format(pid=4242)


def broker_started_while_an_upgrade_began(tmp_path, monkeypatch):
    """No mark at the gate, a running installer once the broker answers."""
    from cairnmind import cockpit
    settings = Settings.load(tmp_path / "data", create=True)
    marks = iter([None, upgrade_mark.NOTE_RUNNING.format(pid=4242)])
    monkeypatch.setattr(upgrade_mark, "refusal", lambda: next(marks, None))
    monkeypatch.setattr(cli, "ensure_service", lambda settings, **kwargs: None)
    answers = iter([{"available": False}, {"available": True, "version_matches": True}])
    monkeypatch.setattr(cockpit, "cockpit_status", lambda settings: next(answers))
    monkeypatch.setattr(cockpit.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(cockpit.time, "sleep", lambda seconds: None)
    return cockpit, settings


def test_a_broker_started_while_an_upgrade_began_is_stopped(tmp_path, monkeypatch):
    cockpit, settings = broker_started_while_an_upgrade_began(tmp_path, monkeypatch)
    stopped = []
    monkeypatch.setattr(cockpit, "stop_cockpit", lambda settings: stopped.append(settings))
    with pytest.raises(cli.UpgradeInProgress) as refused:
        cockpit.ensure_cockpit(settings)
    assert str(refused.value) == upgrade_mark.NOTE_RUNNING.format(pid=4242)
    assert stopped == [settings]


def test_a_broker_that_does_not_stop_is_named_with_the_command(tmp_path, monkeypatch):
    cockpit, settings = broker_started_while_an_upgrade_began(tmp_path, monkeypatch)

    def refuses(settings):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(cockpit, "stop_cockpit", refuses)
    with pytest.raises(cli.UpgradeInProgress) as refused:
        cockpit.ensure_cockpit(settings)
    command = f"cairnmind --data-dir {settings.data_dir} cockpit stop"
    assert str(refused.value) == upgrade_mark.BROKER_STOP_FAILED.format(command=command)


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
