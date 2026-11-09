"""Broker control commands stay noninitializing on reads; the browser UI was removed."""
from __future__ import annotations

import sys
from types import ModuleType

from typer.testing import CliRunner

from devbrain.cli import app


def cockpit_stub(monkeypatch, calls):
    module = ModuleType("devbrain.cockpit")

    def status(settings):
        assert not (settings.data_dir / "config.json").exists()
        calls.append(("status", settings.data_dir))
        return {"running": False}

    def stop(settings, stop_clients=False):
        assert not (settings.data_dir / "config.json").exists()
        calls.append(("stop", settings.data_dir, stop_clients))
        return {"stopping": False}

    module.cockpit_status = status
    module.stop_cockpit = stop
    monkeypatch.setitem(sys.modules, "devbrain.cockpit", module)


def test_browser_open_command_was_removed(tmp_path):
    result = CliRunner().invoke(app, ["--data-dir", str(tmp_path / "data"), "cockpit", "open"])
    assert result.exit_code != 0


def test_readonly_control_does_not_initialize_missing_data(tmp_path, monkeypatch):
    calls = []
    cockpit_stub(monkeypatch, calls)
    data = tmp_path / "missing"
    runner = CliRunner()
    for args in (["cockpit", "status"], ["cockpit", "stop", "--clients"], ["cockpit", "--help"]):
        result = runner.invoke(app, ["--data-dir", str(data), *args])
        assert result.exit_code == 0, repr(result.exception)
        assert not data.exists()
    assert calls == [("status", data), ("stop", data, True)]
