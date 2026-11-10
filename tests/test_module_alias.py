"""The module is `cairnmind`; `devbrain` is an alias of the same modules for one release (issue #40)."""
from __future__ import annotations

import importlib
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

import cairnmind
from cairnmind import integrations
from cairnmind.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_the_earlier_name_gives_the_very_same_module_objects():
    import devbrain
    import devbrain.config
    from devbrain.cli import app
    import cairnmind.cli
    import cairnmind.config
    assert devbrain.__version__ == cairnmind.__version__
    assert devbrain.config is cairnmind.config and sys.modules["devbrain.config"] is cairnmind.config
    assert app is cairnmind.cli.app
    assert importlib.import_module("devbrain.terminal_safe") is importlib.import_module("cairnmind.terminal_safe")
    # The real module keeps its real identity; no state exists twice.
    assert cairnmind.config.__name__ == "cairnmind.config" and cairnmind.config.Settings is Settings
    for module in (cairnmind.config, cairnmind.cli, importlib.import_module("cairnmind.terminal_safe")):
        assert module.__spec__.name == module.__name__ and module.__spec__.origin == module.__file__


def test_the_alias_does_not_break_reload_or_running_a_module_after_an_alias_import():
    import devbrain.terminal_safe as aliased
    import cairnmind.terminal_safe as real
    assert aliased is real
    real.marker_for_reload = "set before reload"
    reloaded = importlib.reload(real)
    assert reloaded is real and real.__spec__.name == "cairnmind.terminal_safe"
    assert real.safe_text("a\x1bb") == "a\\x1bb"
    # The loader of the alias still finds the real code after the alias import.
    import devbrain
    code = devbrain._Redirect().find_spec("devbrain.terminal_safe").loader.get_code("devbrain.terminal_safe")
    assert code is not None and code.co_filename.endswith("terminal_safe.py")


def test_an_unknown_module_under_the_earlier_name_is_still_an_import_error():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("devbrain.no_such_module")
    assert importlib.util.find_spec("devbrain.no_such_module") is None


def test_python_m_with_the_earlier_name_runs_the_real_program(tmp_path):
    for module in ("devbrain", "cairnmind"):
        result = subprocess.run([sys.executable, "-I", "-m", module, "--data-dir", str(tmp_path / "absent"), "version"],
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"version": cairnmind.__version__}
        assert result.stderr == ""  # a hook or an MCP launch must not get extra output
    assert not (tmp_path / "absent").exists()
    # `python -m devbrain.<module>` finds the real module's code.
    import devbrain
    loader = devbrain._Redirect().find_spec("devbrain.workspace").loader
    assert loader.get_code("devbrain.workspace") is not None


def test_no_source_or_test_imports_the_earlier_name():
    pattern = re.compile(r"^\s*(from|import)\s+devbrain\b|(__import__|import_module|find_spec)\(\s*[\"']devbrain\b",
                         re.M)
    offenders = [str(path.relative_to(ROOT)) for folder in ("src/cairnmind", "tests")
                 for path in sorted((ROOT / folder).rglob("*.py"))
                 if path.name != "test_module_alias.py" and pattern.search(path.read_text(encoding="utf-8"))]
    assert offenders == []
    assert sorted(path.name for path in (ROOT / "src" / "devbrain").glob("*.py")) == ["__init__.py", "__main__.py"]


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return Settings.load(tmp_path / "memory", create=True)


def earlier(arguments):
    return ["devbrain" if value == "cairnmind" else value for value in arguments]


def test_new_entries_start_the_new_module_and_entries_of_both_names_are_recognised(settings):
    command = integrations._command(settings, "mcp")
    assert command[1:4] == ["-I", "-m", "cairnmind"]
    assert integrations._is_ours({"args": command[1:]}) and integrations._is_ours({"args": earlier(command[1:])})
    assert not integrations._is_ours({"args": ["-m", "another_tool", "mcp"]})
    hook = " ".join(integrations._command(settings, "hook", "--client", "codex"))
    assert integrations._managed_hook({"command": hook}, "codex")
    assert integrations._managed_hook({"command": hook.replace("-m cairnmind ", "-m devbrain ")}, "codex")
    assert not integrations._managed_hook({"command": hook.replace("-m cairnmind ", "-m another_tool ")}, "codex")


def test_install_rewrites_entries_written_under_the_earlier_name(settings, tmp_path):
    mcp = tmp_path / "project" / ".mcp.json"
    mcp.parent.mkdir()
    old_args = earlier(integrations._command(settings, "mcp")[1:])
    mcp.write_text(json.dumps({"mcpServers": {"devbrain": {"command": "/old/python", "args": old_args},
                                              "other": {"command": "keep"}}}))
    assert integrations._merge_json_mcp(mcp, settings) is True
    written = json.loads(mcp.read_text())["mcpServers"]
    assert written["devbrain"]["args"] == integrations._command(settings, "mcp")[1:] and written["other"] == {"command": "keep"}

    hooks = tmp_path / "project" / "hooks.json"
    old_hook = " ".join(earlier(integrations._command(settings, "hook", "--client", "claude")))
    hooks.write_text(json.dumps({"hooks": {"SessionStart": [
        {"hooks": [{"type": "command", "command": old_hook}, {"type": "command", "command": "echo user hook"}]}]}}))
    assert integrations._merge_hooks(hooks, settings, "claude") is True
    commands = [handler["command"] for group in json.loads(hooks.read_text())["hooks"]["SessionStart"]
                for handler in group["hooks"]]
    assert "echo user hook" in commands and old_hook not in commands
    assert sum("-m cairnmind " in command for command in commands) == 1


def test_untouched_files_generated_under_the_earlier_name_still_count_as_ours(settings, tmp_path):
    from cairnmind.sessions import _managed_empty_file
    project = {"id": "project-a", "name": "Example", "root": str(tmp_path / "project")}
    for builder in (integrations._command, integrations._earlier_command):
        command = builder(settings, "mcp")
        generated = json.dumps({"mcpServers": {"devbrain": {
            "command": command[0], "args": command[1:],
            "env": {name: "${" + name + ":-}" for name in integrations.SESSION_ENV_VARS}}}})
        assert _managed_empty_file(".mcp.json", generated.encode(), settings, project) is True
    foreign = json.dumps({"mcpServers": {"devbrain": {"command": sys.executable, "args": ["-m", "another_tool"]}}})
    assert _managed_empty_file(".mcp.json", foreign.encode(), settings, project) is False
    assert _managed_empty_file(".mcp.json", b"not json", settings, project) is False


def test_an_unmarked_codex_entry_under_the_earlier_name_is_left_working(settings, tmp_path):
    path = tmp_path / "project" / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    original = ('model = "keep"\n[mcp_servers.devbrain]\ncommand = ' + json.dumps(sys.executable)
                + "\nargs = " + json.dumps(earlier(integrations._command(settings, "mcp")[1:]))
                + '\nenv_vars = ["DEVBRAIN_SESSION_ID", "DEVBRAIN_PACKET_ID"]\n')
    path.write_text(original)
    # The earlier module name still starts the same program, so the entry is not an error.
    assert integrations._merge_toml_mcp(path, settings) is False
    assert path.read_text() == original
