"""`cairnmind stop` says that the interface broker keeps running, as it does for the engine (#169)."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from cairnmind import cli, first_start
from cairnmind import cockpit


@pytest.fixture
def stop(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "service_alive", lambda settings: True)
    monkeypatch.setattr(cli, "request", lambda settings, method, path, **kw: {"stopping": True})
    monkeypatch.setattr(cli, "_stdout_is_terminal", lambda: True)

    def invoke(broker_running):
        monkeypatch.setattr(cockpit, "cockpit_status", lambda settings: {"available": broker_running})
        return CliRunner().invoke(cli.app, ["--data-dir", str(tmp_path / "memory"), "stop"]).stdout.splitlines()
    return invoke


def test_stop_names_the_broker_it_leaves_running(stop):
    lines = stop(True)
    assert "is stopping" in lines[0] and lines[-1] == first_start.BROKER_LEFT_RUNNING
    assert "cairnmind cockpit stop" in lines[-1]


def test_stop_says_nothing_about_a_broker_that_is_not_running(stop):
    assert all(first_start.BROKER_LEFT_RUNNING != line for line in stop(False))


def test_the_broker_sentence_has_polish():
    assert first_start.MESSAGES[first_start.BROKER_LEFT_RUNNING] != first_start.BROKER_LEFT_RUNNING
