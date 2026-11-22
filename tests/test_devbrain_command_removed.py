"""The devbrain command is gone in 0.13.0; what an upgrade needs stays (#314)."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from cairnmind import first_start, integrations
from cairnmind.config import Settings

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install-wsl.sh"
BASH = "/bin/bash" if Path("/bin/bash").exists() else shutil.which("bash")


def test_the_package_installs_one_command():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["scripts"] == {"cairnmind": "cairnmind.cli:entrypoint"}
    # The import alias that DevBrain 0.8.0 entries run through is still packaged.
    assert data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/cairnmind", "src/devbrain"]


def _leftover_function():
    script = SCRIPT.read_text(encoding="utf-8")
    start = script.index("leftover_devbrain() {")
    return script[start:script.index("\n}\n", start) + 3]


def test_a_devbrain_left_in_the_tool_folder_is_named_and_not_removed(tmp_path):
    function = _leftover_function()
    folder = tmp_path / "bin"
    folder.mkdir()
    empty_path = tmp_path / "no-tools"
    empty_path.mkdir()
    leftover = folder / "devbrain"

    def run():
        return subprocess.run(
            [BASH, "-c", "set -euo pipefail\n" + function + f'\nleftover_devbrain "{folder}"\necho continued'],
            env={"PATH": str(empty_path)}, capture_output=True, text=True, timeout=30)

    result = run()
    assert (result.returncode, result.stdout, result.stderr) == (0, "continued\n", "")

    named = (f'Cairnmind no longer installs the devbrain command, but {leftover} is still there. '
             f'Use cairnmind instead; if nothing else uses that file, remove it: rm "{leftover}"\n')
    leftover.symlink_to(tmp_path / "gone" / "devbrain")  # a link that points nowhere
    result = run()
    assert (result.returncode, result.stdout, result.stderr) == (0, "continued\n", named)
    assert os.path.lexists(leftover)

    leftover.unlink()
    leftover.write_text("#!/bin/sh\n")
    leftover.chmod(0o755)
    result = run()
    assert (result.returncode, result.stdout, result.stderr) == (0, "continued\n", named)
    assert leftover.exists()


def test_an_unlisted_earlier_uv_tool_stops_the_installer(tmp_path):
    script = SCRIPT.read_text(encoding="utf-8")
    start = script.index("refuse_unlisted_devbrain_tool() {")
    function = script[start:script.index("\n}\n", start) + 3]
    folder = tmp_path / "tools" / "devbrain"

    def run():
        return subprocess.run([BASH, "-c", "set -euo pipefail\n" + function + f'\nrefuse_unlisted_devbrain_tool "{folder}"\necho continued'],
                              env={"PATH": str(tmp_path)}, capture_output=True, text=True, timeout=30)

    assert (run().returncode, run().stdout) == (0, "continued\n")
    folder.parent.mkdir()
    folder.symlink_to(tmp_path / "gone")  # damaged: points nowhere
    result = run()
    assert (result.returncode, result.stdout) == (1, "")
    assert str(folder) in result.stderr and "uv tool uninstall devbrain" in result.stderr
    # What the tool started is stopped with its own command before it is removed, as for a listed tool.
    stop = f'"{folder}/bin/devbrain" stop'
    assert stop in result.stderr and result.stderr.index(stop) < result.stderr.index("uv tool uninstall devbrain")
    folder.unlink()
    folder.mkdir()
    assert run().returncode == 1
    # It runs after the listed 0.8.0 tool was removed and before anything is stopped or installed.
    assert script.index("uv tool uninstall devbrain >") < script.index('refuse_unlisted_devbrain_tool "$(uv tool dir)') \
        < script.index("uv tool install --python")


def test_hand_written_bare_devbrain_entries_are_named_not_rewritten(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(integrations, "_probe_cli", lambda *args, **kwargs: {
        "installed": True, "version": "9.9.9", "mcp": True, "hooks": True, "packet_passing": True,
        "session_binding": True, "verified_resume": True, "notes": []})
    settings = Settings.load(tmp_path / "data", create=True)
    root = tmp_path / "project"
    (root / ".claude").mkdir(parents=True)
    (root / ".codex").mkdir()
    mcp = {"mcpServers": {"devbrain": {"command": "devbrain", "args": ["mcp", "--client", "external"]},
                          "fine": {"command": "/usr/bin/tool"}}}
    hooks = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "~/.local/bin/devbrain hook --client claude"}]}]}}
    (root / ".mcp.json").write_text(json.dumps(mcp))
    (root / ".claude/settings.local.json").write_text(json.dumps(hooks))
    (root / ".codex/config.toml").write_text('[mcp_servers.old]\ncommand = "devbrain"\nargs = ["mcp"]\n')

    result = integrations.install_integrations(settings, {"id": "p", "name": "P", "root": str(root)})

    named = [w for w in result["warnings"] if "no longer installs" in w]
    assert len(named) == 3
    assert any("mcp_servers.old" in w and "config.toml" in w for w in named)
    assert any("mcpServers.devbrain" in w for w in named) and any("hooks.SessionStart" in w for w in named)
    kept = json.loads((root / ".mcp.json").read_text())["mcpServers"]
    assert kept["devbrain"] == mcp["mcpServers"]["devbrain"] and "cairnmind" in kept  # theirs stays, ours is added
    assert first_start.TRUST_NOTE in result["warnings"]
    # install wrote its own entry beside each one, so the advice is to remove it, not to add a second cairnmind
    assert all("remove this one" in w for w in named)
    assert first_start.REMOVED_COMMAND_BESIDE.format(path=root / ".mcp.json", entry="mcpServers.devbrain") in named

    # A client that is not installed gets nothing written (#313): then writing cairnmind there is the fix.
    monkeypatch.setattr(integrations, "_probe_cli", lambda *args, **kwargs: {
        "installed": False, "version": None, "mcp": False, "hooks": False, "packet_passing": False,
        "session_binding": False, "verified_resume": False, "notes": []})
    absent = integrations.install_integrations(settings, {"id": "p", "name": "P", "root": str(root)})
    assert first_start.REMOVED_COMMAND.format(path=root / ".codex/config.toml", entry="mcp_servers.old") in (
        absent["warnings"])
    assert not any("remove this one" in w for w in absent["warnings"])
    assert first_start.MESSAGES[first_start.REMOVED_COMMAND_BESIDE] != first_start.REMOVED_COMMAND_BESIDE


def test_the_installer_looks_for_the_leftover_after_installing_and_still_replaces_devbrain_0_8_0():
    script = SCRIPT.read_text(encoding="utf-8")
    install = script.index("uv tool install --python")
    assert install < script.index('leftover_devbrain "$tool_bin_dir"') < script.index("stored_engine_mode() {")
    stop = script.index('stop_installed "$(uv tool dir --bin)/devbrain" devbrain "$(uv tool dir)/devbrain/bin/python"')
    assert stop < script.index("uv tool uninstall devbrain") < install
    subprocess.run([BASH, "-n", str(SCRIPT)], check=True)


def test_devbrain_era_client_files_are_rewritten_so_none_calls_a_missing_command(tmp_path, monkeypatch):
    # The files are the ones DevBrain 0.8.0 wrote (-m devbrain), which an installation upgraded through 0.12.0 may
    # still carry; files 0.9.0 to 0.12.0 wrote (-m cairnmind) are covered by test_integrations.py. This guards the
    # rewrite path; it does not run uv's removal of the link.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in (*integrations.SESSION_ENV_VARS, "CAIRNMIND_HOME", "DEVBRAIN_HOME",
                 "CAIRNMIND_CONTEXT_FILE", "DEVBRAIN_CONTEXT_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(integrations, "_probe_cli", lambda *args, **kwargs: {
        "installed": True, "version": "9.9.9", "mcp": True, "hooks": True, "packet_passing": True,
        "session_binding": True, "verified_resume": True, "notes": []})
    data = tmp_path / "home/.local/share/devbrain"
    Settings.load(data, create=True)
    settings = Settings.load()
    assert settings.data_dir == data.resolve()  # the data directory keeps its name

    root = tmp_path / "project"
    (root / ".codex").mkdir(parents=True)
    (root / ".claude").mkdir()
    project = {"id": "project-a", "name": "Example", "root": str(root)}
    earlier_mcp = integrations._earlier_command(settings, "mcp")[1:]
    block = "# Notes\n\n<!-- BEGIN DEVBRAIN -->\nold text\n<!-- END DEVBRAIN -->\n"
    (root / "AGENTS.md").write_text(block)
    (root / "CLAUDE.md").write_text(block)
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "devbrain": {"command": sys.executable, "args": earlier_mcp,
                     "env": {n: "${" + n + ":-}" for n in integrations.EARLIER_SESSION_ENV_VARS}},
        "other": {"command": "keep"}}}))
    (root / ".codex/config.toml").write_text(
        'model = "keep"\n# BEGIN DEVBRAIN\n[mcp_servers.devbrain]\ncommand = ' + json.dumps(sys.executable)
        + "\nargs = " + json.dumps(earlier_mcp)
        + '\nenv_vars = ["DEVBRAIN_SESSION_ID", "DEVBRAIN_PACKET_ID"]\nstartup_timeout_sec = 15\n'
        'tool_timeout_sec = 75\n# END DEVBRAIN\n')
    for path, client in ((".codex/hooks.json", "codex"), (".claude/settings.local.json", "claude")):
        (root / path).write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [
            {"type": "command",
             "command": shlex.join(integrations._earlier_command(settings, "hook", "--client", client))},
            {"type": "command", "command": "echo user hook"}]}]}}))

    result = integrations.install_integrations(settings, project)

    assert [w for w in result["warnings"] if w != first_start.TRUST_NOTE] == []
    servers = json.loads((root / ".mcp.json").read_text())["mcpServers"]
    assert list(servers) == ["cairnmind", "other"]
    assert servers["cairnmind"]["command"] == sys.executable
    assert servers["cairnmind"]["args"] == integrations._command(settings, "mcp")[1:]
    assert ["--data-dir", str(data.resolve())] == servers["cairnmind"]["args"][3:5]

    text = (root / ".codex/config.toml").read_text()
    codex = tomllib.loads(text)["mcp_servers"]
    assert set(codex) == {"cairnmind"}
    assert codex["cairnmind"]["args"] == integrations._command(settings, "mcp")[1:]
    assert "# BEGIN CAIRNMIND" in text
    # The earlier session variable names are still forwarded on purpose.
    assert "DEVBRAIN" not in text.replace("DEVBRAIN_SESSION_ID", "").replace("DEVBRAIN_PACKET_ID", "")

    commands = [servers["cairnmind"]["command"], codex["cairnmind"]["command"]]
    for path in (".codex/hooks.json", ".claude/settings.local.json"):
        handlers = [h["command"] for entry in json.loads((root / path).read_text())["hooks"]["SessionStart"]
                    for h in entry["hooks"]]
        ours = [c for c in handlers if c != "echo user hook"]
        assert ours and "echo user hook" in handlers
        assert all(shlex.split(c)[:4] == [sys.executable, "-I", "-m", "cairnmind"] for c in ours)
        assert not any("-m devbrain " in c for c in ours)
        commands += [shlex.split(c)[0] for c in ours]
    for name in ("AGENTS.md", "CLAUDE.md"):
        body = (root / name).read_text()
        assert "<!-- BEGIN CAIRNMIND -->" in body and "BEGIN DEVBRAIN" not in body
    # The rule: no client file starts a command that is gone with the executable.
    assert all(c == sys.executable and Path(c).is_file() and Path(c).name != "devbrain" for c in commands)
