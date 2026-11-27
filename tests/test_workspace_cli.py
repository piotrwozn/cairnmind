"""Terminal entry points and lifecycle diagnostics share one global workspace."""
from __future__ import annotations

import re
import json
import sys
from types import ModuleType

import pytest
from typer.testing import CliRunner

from conftest import plain_words

from cairnmind import cli


def workspace_stub(monkeypatch, calls):
    module = ModuleType("cairnmind.workspace")

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
    monkeypatch.setitem(sys.modules, "cairnmind.workspace", module)


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
    assert "interactive terminal" in plain_words(result.output)
    assert not calls and not data.exists()


POLISH = set("\u0105\u0107\u0119\u0142\u0144\u00f3\u015b\u017a\u017c\u0104\u0106\u0118\u0141\u0143\u00d3\u015a\u0179\u017b")


POLISH_WORDS = {"nie", "jest", "oraz", "lub", "albo", "przez", "dla", "bez", "tylko", "lokalne", "lokalny", "modele",
                "silnik", "natywny", "pakiety", "zakres", "historii", "wznowienia", "odbioru", "potwierdzenia",
                "projektu", "projekt", "katalog", "plik", "zadania", "zadanie", "stan", "jawnie", "uruchom", "wymaga",
                "brak", "magazyn", "magazynu", "kokpit", "kokpitu", "sesji", "sesja", "dowody", "pobierz", "odczytaj",
                "zapisz", "zatrzymaj", "sprawdzenia", "pobieranie", "najpierw", "wybierz", "katalogiem"}


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


# Modules that intentionally carry Polish translation catalogs (`MESSAGES`/`CATALOG`/`_PL`),
# Polish language-processing rules, locale-aware OS parsers, or bilingual demo fixtures (#60).
POLISH_ALLOWED_MODULES = frozenset({
    "check_profile_messages.py",
    "engine_docker.py",
    "first_start.py",
    "mcp_failure_notice.py",
    "memory_models.py",
    "platform_hints.py",
    "project_clone.py",
    "project_folders.py",
    "project_forget.py",
    "project_removal.py",
    "recorder_agent.py",
    "restore_claims.py",
    "restore_staged.py",
    "retrieval.py",
    "service_autostart.py",
    "service_version.py",
    "setup_change.py",
    "setup_mark.py",
    "setup_profiles.py",
    "store_prune.py",
    "subject.py",
    "terminal_agent_i18n.py",
    "terminal_connection_hints.py",
    "terminal_i18n.py",
    "terminal_i18n_messages.py",
    "terminal_inspector.py",
    "terminal_legacy_groups.py",
    "terminal_person_act.py",
    "terminal_plan_confirm.py",
    "terminal_preview.py",
    "terminal_project_forget.py",
    "terminal_published_import.py",
    "terminal_record_kinds.py",
    "terminal_release.py",
    "terminal_replace.py",
    "terminal_setup.py",
    "terminal_setup_change.py",
    "terminal_ui.py",
    "topology.py",
    "ui_cockpit.py",
    "upgrade_mark.py",
})

# Core English modules explicitly translated and guarded by #37 and #60.
ENGLISH_ONLY_GUARDED_MODULES = frozenset({
    "cli.py",
    "workspace.py",
    "workspace_state.py",
    "cockpit.py",
    "cockpit_state.py",
    "terminal_client.py",
    "api_providers.py",
    "copilot_adapter.py",
    "api_credentials.py",
    "api_profiles.py",
    "api_runtime.py",
    "service.py",
    "agent.py",
})


def _polish_literals(path) -> list[tuple[int, str]]:
    import ast
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and (POLISH & set(node.value) or POLISH_WORDS & set(re.findall(r"[a-z]+", node.value.lower())))
    ]


def test_no_message_in_the_command_line_module_is_polish():
    from pathlib import Path
    assert _polish_literals(Path(cli.__file__)) == []


def test_no_message_in_non_i18n_modules_is_polish():
    """Guard cli.py, the 12 modules of #60, and all other non-i18n modules against Polish literals."""
    from pathlib import Path
    pkg_dir = Path(cli.__file__).parent
    all_modules = {p.name: p for p in sorted(pkg_dir.glob("*.py"))}
    assert ENGLISH_ONLY_GUARDED_MODULES <= set(all_modules)
    assert ENGLISH_ONLY_GUARDED_MODULES.isdisjoint(POLISH_ALLOWED_MODULES)

    unexpected: dict[str, list[tuple[int, str]]] = {}
    stale_allowed: list[str] = []
    for name, path in all_modules.items():
        hits = _polish_literals(path)
        if name in POLISH_ALLOWED_MODULES:
            if not hits:
                stale_allowed.append(name)
        elif hits:
            unexpected[name] = hits[:5]

    missing_allowed = sorted(POLISH_ALLOWED_MODULES - set(all_modules))
    assert not missing_allowed, f"POLISH_ALLOWED_MODULES names missing files: {missing_allowed}"
    assert not stale_allowed, f"POLISH_ALLOWED_MODULES lists modules with no Polish literals: {stale_allowed}"
    assert not unexpected, (
        "Unexpected Polish literals in English-only modules (translate to English, or if this module "
        f"intentionally stores Polish i18n catalog entries, add it to POLISH_ALLOWED_MODULES): {unexpected}"
    )


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
    from cairnmind import __version__
    data = tmp_path / "absent"
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: pytest.fail("Version entered terminal runtime"))
    result = CliRunner().invoke(cli.app, ["--data-dir", str(data), "version"])
    assert result.exit_code == 0
    assert json.loads(result.output) == {"version": __version__}
    assert not data.exists()
