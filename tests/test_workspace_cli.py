"""Terminal entry points and lifecycle diagnostics share one global workspace."""
from __future__ import annotations

import re
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
    assert "interactive terminal" in " ".join(result.output.split())
    assert not calls and not data.exists()


POLISH = set("\u0105\u0107\u0119\u0142\u0144\u00f3\u015b\u017a\u017c\u0104\u0106\u0118\u0141\u0143\u00d3\u015a\u0179\u017b")


POLISH_WORDS = {"nie", "jest", "oraz", "lub", "albo", "przez", "dla", "bez", "tylko", "lokalne", "lokalny", "modele",
                "silnik", "natywny", "pakiety", "zakres", "historii", "wznowienia", "odbioru", "potwierdzenia",
                "projektu", "projekt", "katalog", "plik", "zadania", "zadanie", "stan", "jawnie", "uruchom", "wymaga",
                "brak", "magazyn", "magazynu", "kokpit", "kokpitu", "sesji", "sesja", "dowody", "pobierz", "odczytaj",
                "zapisz", "zatrzymaj", "sprawdzenia"}


def _command_paths(group, prefix=()):
    for name, command in group.commands.items():
        yield (*prefix, name)
        if hasattr(command, "commands"):
            yield from _command_paths(command, (*prefix, name))


def test_every_help_page_of_the_command_line_is_english(tmp_path):
    import typer
    root = typer.main.get_command(cli.app)
    paths = [(), *_command_paths(root)]
    assert len(paths) > 40
    runner = CliRunner()
    for path in paths:
        result = runner.invoke(cli.app, ["--data-dir", str(tmp_path / "absent"), *path, "--help"])
        assert result.exit_code == 0, (path, result.output)
        assert not POLISH & set(result.output), (path, sorted(POLISH & set(result.output)))
        # Polish without diacritics would pass the check above, so common Polish words are checked too.
        words = set(re.findall(r"[a-z]+", result.output.lower()))
        assert not words & POLISH_WORDS, (path, sorted(words & POLISH_WORDS))
    assert not (tmp_path / "absent").exists()


def test_no_message_in_the_command_line_module_is_polish():
    import ast
    from pathlib import Path
    tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
    polish = [(node.lineno, node.value) for node in ast.walk(tree)
              if isinstance(node, ast.Constant) and isinstance(node.value, str) and POLISH & set(node.value)]
    assert polish == []


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
