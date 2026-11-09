"""Terminal entry points and lifecycle diagnostics share one global workspace."""
from __future__ import annotations

import json
import sys
from types import ModuleType

import pytest
from typer.testing import CliRunner

from devbrain import cli


def workspace_stub(monkeypatch, calls):
    module = ModuleType("devbrain.workspace")

    def launch(settings, takeover=False):
        assert settings.token
        assert (settings.data_dir / "config.json").is_file()
        calls.append(("launch", settings.data_dir, takeover))
        return 0

    def status(settings):
        assert not settings.data_dir.exists()
        calls.append(("status", settings.data_dir))
        return {"available": False}

    def detach(settings):
        assert not settings.data_dir.exists()
        calls.append(("detach", settings.data_dir))
        return {"detached": False}

    def stop(settings, stop_clients=False):
        assert not settings.data_dir.exists()
        calls.append(("stop", settings.data_dir, stop_clients))
        return {"stopped": False}

    module.launch_workspace = launch
    module.workspace_status = status
    module.detach_workspace = detach
    module.stop_workspace = stop
    monkeypatch.setitem(sys.modules, "devbrain.workspace", module)


@pytest.mark.parametrize("arguments", [[], ["tui"], ["workspace", "takeover"]])
def test_terminal_entry_is_global_from_any_directory_and_never_opens_browser(tmp_path, monkeypatch, arguments):
    calls = []
    workspace_stub(monkeypatch, calls)
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: True)
    monkeypatch.setattr("webbrowser.open", lambda *a, **kw: pytest.fail("Terminal launcher opened a browser"))
    elsewhere = tmp_path / "unrelated"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    data = tmp_path / "global-data"
    result = CliRunner().invoke(cli.app, ["--data-dir", str(data), *arguments])
    assert result.exit_code == 0, result.output or repr(result.exception)
    assert calls == [("launch", data, arguments == ["workspace", "takeover"])]
    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize("arguments", [[], ["tui"], ["workspace", "takeover"]])
def test_no_tty_is_actionable_and_does_not_initialize(tmp_path, monkeypatch, arguments):
    calls = []
    workspace_stub(monkeypatch, calls)
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: False)
    data = tmp_path / "absent"
    result = CliRunner().invoke(cli.app, ["--data-dir", str(data), *arguments])
    assert result.exit_code == 2
    assert "interaktywnym terminalu" in result.output
    assert not calls and not data.exists()


@pytest.mark.parametrize("arguments,expected", [
    (["workspace", "status"], "status"),
    (["workspace", "detach"], "detach"),
    (["workspace", "stop"], "stop"),
    (["workspace", "stop", "--clients"], "stop"),
])
def test_lifecycle_diagnostics_do_not_initialize(tmp_path, monkeypatch, arguments, expected):
    calls = []
    workspace_stub(monkeypatch, calls)
    data = tmp_path / "absent"
    result = CliRunner().invoke(cli.app, ["--data-dir", str(data), *arguments])
    assert result.exit_code == 0, result.output or repr(result.exception)
    assert json.loads(result.output)
    assert calls[0][0] == expected
    if expected == "stop":
        assert calls[0][2] is ("--clients" in arguments)
    assert not data.exists()


@pytest.mark.parametrize("arguments", [
    ["--help"], ["tui", "--help"], ["workspace", "--help"],
    ["workspace", "status", "--help"], ["workspace", "detach", "--help"],
    ["workspace", "stop", "--help"], ["workspace", "takeover", "--help"],
])
def test_terminal_help_does_not_initialize(tmp_path, monkeypatch, arguments):
    data = tmp_path / "absent"
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: pytest.fail("Help checked interactive runtime"))
    result = CliRunner().invoke(cli.app, ["--data-dir", str(data), *arguments])
    assert result.exit_code == 0, result.output or repr(result.exception)
    assert not data.exists()


def test_version_does_not_initialize_or_launch_anything(tmp_path, monkeypatch):
    from devbrain import __version__
    data = tmp_path / "absent"
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: pytest.fail("Version entered terminal runtime"))
    result = CliRunner().invoke(cli.app, ["--data-dir", str(data), "version"])
    assert result.exit_code == 0
    assert json.loads(result.output) == {"version": __version__}
    assert not data.exists()
