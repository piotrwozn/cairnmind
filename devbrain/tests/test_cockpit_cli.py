"""The optional browser launcher remains explicit and noninitializing on reads."""
from __future__ import annotations

import sys
from types import ModuleType

from typer.testing import CliRunner

from devbrain.cli import app


def cockpit_stub(monkeypatch, calls):
    module = ModuleType("devbrain.cockpit")

    def opened(settings, open_browser=True):
        assert settings.token and (settings.data_dir / "config.json").is_file()
        calls.append(("open", settings.data_dir, open_browser))
        return {"running": True, "url": "http://127.0.0.1:8766/"}

    def status(settings):
        assert not (settings.data_dir / "config.json").exists()
        calls.append(("status", settings.data_dir))
        return {"running": False}

    def stop(settings, stop_clients=False):
        assert not (settings.data_dir / "config.json").exists()
        calls.append(("stop", settings.data_dir, stop_clients))
        return {"stopping": False}

    module.open_cockpit = opened
    module.cockpit_status = status
    module.stop_cockpit = stop
    monkeypatch.setitem(sys.modules, "devbrain.cockpit", module)


def test_explicit_browser_opens_same_global_cockpit_from_unrelated_working_directory(tmp_path, monkeypatch):
    calls = []
    cockpit_stub(monkeypatch, calls)
    work = tmp_path / "unrelated-project"
    work.mkdir()
    monkeypatch.chdir(work)
    data = tmp_path / "global-data"
    result = CliRunner().invoke(app, ["--data-dir", str(data), "cockpit", "open"])
    assert result.exit_code == 0, repr(result.exception)
    assert calls == [("open", data, True)]
    assert not (work / ".devbrain").exists()


def test_no_browser_open_and_readonly_control_do_not_initialize_missing_data(tmp_path, monkeypatch):
    calls = []
    cockpit_stub(monkeypatch, calls)
    data = tmp_path / "missing"
    runner = CliRunner()
    for args in (["cockpit", "status"], ["cockpit", "stop", "--clients"], ["cockpit", "--help"]):
        result = runner.invoke(app, ["--data-dir", str(data), *args])
        assert result.exit_code == 0, repr(result.exception)
        assert not data.exists()
    result = runner.invoke(app, ["--data-dir", str(data), "cockpit", "open", "--no-browser"])
    assert result.exit_code == 0, repr(result.exception)
    assert calls == [("status", data), ("stop", data, True), ("open", data, False)]
