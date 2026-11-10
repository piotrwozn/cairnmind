from __future__ import annotations

import json
import copy
import hashlib
import os
import stat
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import httpx
import pytest

from devbrain.config import Settings
from devbrain import integrations
from devbrain.mcp_server import ServiceClient, create_server

TOOLS = {"ask", "remember", "checkpoint", "save_plan", "next_task", "resume_context", "ack_resume", "check_status"}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    # Never inspect/write the real user's client config in integration tests.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("DEVBRAIN_SESSION_ID", "DEVBRAIN_PACKET_ID", "DEVBRAIN_PROJECT", "DEVBRAIN_CLIENT",
                 "DEVBRAIN_CONTEXT_FILE"):
        monkeypatch.delenv(name, raising=False)
    return Settings.load(tmp_path / "memory", create=True)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    return {"id": "project-a", "name": "Example", "root": str(root)}


@pytest.fixture
def installed(monkeypatch):
    info = {"installed": True, "version": "9.9.9", "mcp": True, "hooks": True,
            "packet_passing": True, "session_binding": True, "verified_resume": True, "notes": []}
    monkeypatch.setattr(integrations, "_probe_cli", lambda *args, **kwargs: dict(info))
    return info


@pytest.fixture
def prepared(project):
    packet = {"id": "packet-one", "session_id": "logical-session", "hash": "exact-hash", "epoch": 1,
              "project_id": project["id"], "worktree": project["root"], "client": "codex",
              "active_plan": None, "active_task": None, "status": "ready"}
    return {"session_id": "logical-session", "epoch": 1, "status": "ready", "packet": packet, "unknowns": []}


@pytest.fixture
def native_backend(monkeypatch):
    from devbrain import ingest, store
    monkeypatch.setattr(store, "Store", lambda *a: object())
    monkeypatch.setattr(ingest, "register_default_histories", lambda *a: {})


def test_install_preserves_native_config_and_is_idempotent(settings, project, installed):
    root = Path(project["root"])
    (root / ".codex").mkdir()
    (root / ".codex/config.toml").write_text('model = "custom"\n[mcp_servers.other]\ncommand = "other"\n')
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "keep"}}, "extra": 1}))
    (root / ".claude").mkdir()
    original_hook = {"hooks": [{"type": "command", "command": "echo existing"}]}
    (root / ".claude/settings.local.json").write_text(json.dumps({
        "permissions": {"allow": ["Read"]}, "hooks": {"SessionStart": [original_hook]}}))
    (root / "AGENTS.md").write_text("# Existing instructions\nKeep this.\n")
    result = integrations.install_integrations(settings, project)
    assert len(result["changed"]) == 6
    config = tomllib.loads((root / ".codex/config.toml").read_text())
    assert config["model"] == "custom"
    assert config["mcp_servers"]["other"]["command"] == "other"
    assert config["mcp_servers"]["devbrain"]["args"][-1] == "mcp"
    assert config["mcp_servers"]["devbrain"]["env_vars"] == list(integrations.SESSION_ENV_VARS)
    claude_mcp = json.loads((root / ".mcp.json").read_text())["mcpServers"]["devbrain"]
    assert claude_mcp["env"] == {name: "${" + name + ":-}" for name in integrations.SESSION_ENV_VARS}
    claude = json.loads((root / ".claude/settings.local.json").read_text())
    assert claude["permissions"] == {"allow": ["Read"]}
    assert original_hook in claude["hooks"]["SessionStart"]
    assert len(claude["hooks"]["SessionStart"]) == 2
    assert "Keep this." in (root / "AGENTS.md").read_text()
    assert "expected_version" in (root / "AGENTS.md").read_text()
    assert "needs_test" in (root / "AGENTS.md").read_text()
    assert "save_plan" in (root / "AGENTS.md").read_text()
    assert "metadata.check_run_ids" in (root / "AGENTS.md").read_text()
    assert "ack_resume" in (root / "AGENTS.md").read_text()
    assert "client-reported" in (root / "AGENTS.md").read_text()
    assert list((root / ".codex").glob("config.toml.devbrain-backup-*"))
    second = integrations.install_integrations(settings, project)
    assert second["changed"] == []
    # Global configuration is never changed as a side effect of adding a project.
    assert not (Path.home() / ".codex").exists()
    assert not (Path.home() / ".claude").exists()


def test_managed_legacy_mcp_config_upgrades_session_forwarding(settings, project, installed):
    root = Path(project["root"])
    integrations.install_integrations(settings, project)
    codex = root / ".codex/config.toml"
    codex.write_text("\n".join(line for line in codex.read_text().splitlines()
                                if not line.startswith("env_vars =")) + "\n")
    claude = root / ".mcp.json"
    previous = json.loads(claude.read_text())
    previous["mcpServers"]["devbrain"].pop("env")
    claude.write_text(json.dumps(previous))
    upgraded = integrations.install_integrations(settings, project)
    assert {str(codex), str(claude)} <= set(upgraded["changed"])
    assert tomllib.loads(codex.read_text())["mcp_servers"]["devbrain"]["env_vars"] == list(integrations.SESSION_ENV_VARS)
    assert set(json.loads(claude.read_text())["mcpServers"]["devbrain"]["env"]) == set(integrations.SESSION_ENV_VARS)
    assert integrations.install_integrations(settings, project)["changed"] == []


@pytest.mark.parametrize("extra", ["", 'env_vars = ["DEVBRAIN_PACKET_ID"]\n',
                                      'env_vars = ["DEVBRAIN_SESSION_ID", "DEVBRAIN_PACKET_ID"]\n'
                                      '[mcp_servers.devbrain.env]\nDEVBRAIN_SESSION_ID = "stale-session"\n'])
def test_unmarked_owned_codex_config_without_live_binding_is_refused(settings, project, installed, extra):
    path = Path(project["root"]) / ".codex/config.toml"
    path.parent.mkdir()
    original = ('model = "keep"\n[mcp_servers.devbrain]\ncommand = ' + json.dumps(sys.executable)
                + '\nargs = ' + json.dumps(integrations._command(settings, "mcp")[1:]) + '\n' + extra)
    path.write_text(original)
    result = integrations.install_integrations(settings, project, clients=("codex",))
    assert path.read_text() == original
    assert result["clients"]["codex"]["mcp_configured"] is False
    assert any("must forward the launch environment" in warning for warning in result["warnings"])


def test_unmarked_owned_codex_config_with_live_binding_preserves_user_options(settings, project, installed):
    path = Path(project["root"]) / ".codex/config.toml"
    path.parent.mkdir()
    original = ('model = "keep"\n[mcp_servers.devbrain]\ncommand = ' + json.dumps(sys.executable)
                + '\nargs = ' + json.dumps(integrations._command(settings, "mcp")[1:])
                + '\nenv_vars = ["MY_OTHER_SETTING", "DEVBRAIN_SESSION_ID", "DEVBRAIN_PACKET_ID"]\n'
                'tool_timeout_sec = 99\n')
    path.write_text(original)
    result = integrations.install_integrations(settings, project, clients=("codex",))
    assert result["clients"]["codex"]["mcp_configured"] is True
    assert path.read_text() == original


def _native_approved_codex_config():
    """Exact 514-byte user fixture after Codex moved the end comment."""
    return ('# BEGIN DEVBRAIN\n[mcp_servers.devbrain]\n'
            'command = "/home/piotr/.local/share/uv/tools/devbrain/bin/python"\n'
            'args = ["-m", "devbrain", "--data-dir", "/home/piotr/.local/share/devbrain", "mcp"]\n'
            'env_vars = ["DEVBRAIN_SESSION_ID", "DEVBRAIN_PACKET_ID"]\n'
            'startup_timeout_sec = 15\ntool_timeout_sec = 75\n\n'
            '[mcp_servers.devbrain.tools.resume_context]\napproval_mode = "approve"\n\n'
            '[mcp_servers.devbrain.tools.ack_resume]\napproval_mode = "approve"\n\n'
            '[mcp_servers.devbrain.tools.checkpoint]\napproval_mode = "approve"\n'
            '# END DEVBRAIN\n')


def test_legacy_native_approval_fixture_upgrades_isolation_once_preserving_permissions(settings, project, installed, monkeypatch):
    monkeypatch.setattr(integrations.sys, "executable", "/home/piotr/.local/share/uv/tools/devbrain/bin/python")
    settings.data_dir = Path("/home/piotr/.local/share/devbrain")
    integrations.install_integrations(settings, project, clients=("codex",))
    path = Path(project["root"]) / ".codex/config.toml"
    raw = _native_approved_codex_config().encode()
    assert len(raw) == 514
    assert hashlib.sha256(raw).hexdigest() == "ff97b98755cae40342f90be9edd1da308665a07b00959c1f04f8cf8eac02c858"
    path.write_bytes(raw)
    path.chmod(0o600)
    assert integrations.install_integrations(settings, project, clients=("codex",))["changed"] == [str(path)]
    expected = tomllib.loads(raw.decode())
    expected["mcp_servers"]["devbrain"]["args"].insert(0, "-I")
    assert tomllib.loads(path.read_text()) == expected
    backups = list(path.parent.glob("config.toml.devbrain-backup-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == raw
    upgraded = path.read_bytes()
    before = path.stat()
    assert integrations.install_integrations(settings, project, clients=("codex",))["changed"] == []
    after = path.stat()
    assert path.read_bytes() == upgraded
    assert (after.st_ino, after.st_mtime_ns, after.st_ctime_ns, stat.S_IMODE(after.st_mode)) == (
        before.st_ino, before.st_mtime_ns, before.st_ctime_ns, 0o600)
    assert list(path.parent.glob("config.toml.devbrain-backup-*")) == backups


@pytest.mark.parametrize("mode", [0o600, 0o640])
def test_managed_mcp_update_preserves_native_approvals_options_and_permissions(settings, project, monkeypatch, mode):
    path = Path(project["root"]) / ".codex/config.toml"
    path.parent.mkdir()
    unrelated = ('\n[mcp_servers."other.server"]\ncommand = "another-tool"\n'
                 'args = [\n  "--keep",\n  """two\nlines""",\n]\n')
    original = ('model = "native-choice"\n' + _native_approved_codex_config().replace(
        '[mcp_servers.devbrain]\n',
        '[mcp_servers.devbrain]\nenabled = false\nnotes = """keep \'quotes\'\nand lines"""\n')
        + '\n[mcp_servers.devbrain.env]\nEXTRA_SETTING = "keep"\n' + unrelated)
    path.write_text(original)
    path.chmod(mode)
    previous = tomllib.loads(original)
    executable = '/new folder/py"thon\nnext-line'
    monkeypatch.setattr(integrations.sys, "executable", executable)
    settings.data_dir = Path('/new memory/with "quotes"\nand lines')
    assert integrations._merge_toml_mcp(path, settings)
    updated = tomllib.loads(path.read_text())
    expected = copy.deepcopy(previous)
    expected["mcp_servers"]["devbrain"].update(command=executable, args=integrations._command(settings, "mcp")[1:])
    assert updated == expected
    assert unrelated in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == mode
    backups = list(path.parent.glob("config.toml.devbrain-backup-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == original.encode()
    assert stat.S_IMODE(backups[0].stat().st_mode) == mode
    before = path.stat()
    assert not integrations._merge_toml_mcp(path, settings)
    assert path.stat().st_ino == before.st_ino
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert len(list(path.parent.glob("config.toml.devbrain-backup-*"))) == 1
    assert not list(path.parent.glob(".config.toml.devbrain-*.tmp"))


def test_managed_mcp_quoted_keys_multiline_arrays_and_extra_forwarded_environment(settings, project):
    path = Path(project["root"]) / ".codex/config.toml"
    path.parent.mkdir()
    original = ('# BEGIN DEVBRAIN\n["mcp_servers"."devbrain"]\n'
                '"command" = "old-python"\n"args" = [\n "-m", "devbrain",\n "--data-dir", "old-memory", "mcp",\n]\n'
                'env_vars = ["NATIVE_EXTRA", "DEVBRAIN_SESSION_ID", "DEVBRAIN_PACKET_ID"]\n'
                'startup_timeout_sec = 15\ntool_timeout_sec = 75\n'
                '[mcp_servers.devbrain.tools."checkpoint"]\napproval_mode = "approve"\n# END DEVBRAIN\n')
    path.write_text(original)
    assert integrations._merge_toml_mcp(path, settings)
    server = tomllib.loads(path.read_text())["mcp_servers"]["devbrain"]
    assert server["command"] == sys.executable
    assert server["args"] == integrations._command(settings, "mcp")[1:]
    assert server["env_vars"] == ["NATIVE_EXTRA", *integrations.SESSION_ENV_VARS]
    assert server["tools"] == {"checkpoint": {"approval_mode": "approve"}}
    assert not integrations._merge_toml_mcp(path, settings)


@pytest.mark.parametrize("name", integrations.SESSION_ENV_VARS)
def test_managed_mcp_literal_session_override_is_refused_without_changing_native_config(settings, project, name):
    path = Path(project["root"]) / ".codex/config.toml"
    path.parent.mkdir()
    original = _native_approved_codex_config() + f'\n[mcp_servers.devbrain.env]\n{name} = "stale"\n'
    path.write_text(original)
    path.chmod(0o600)
    with pytest.raises(ValueError, match="must forward the launch environment"):
        integrations._merge_toml_mcp(path, settings)
    assert path.read_text() == original
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(path.parent.glob("config.toml.devbrain-backup-*"))


@pytest.mark.parametrize("original", [
    'mcp_servers = false\n',
    '[mcp_servers]\ndevbrain = false\n',
    '[mcp_servers]\ndevbrain = []\n',
    '# BEGIN DEVBRAIN\n[mcp_servers.devbrain]\n# END DEVBRAIN\n',
    '# BEGIN DEVBRAIN\nmodel = "no-server"\n# END DEVBRAIN\n',
])
def test_invalid_managed_mcp_shapes_are_not_overwritten(settings, project, original):
    path = Path(project["root"]) / ".codex/config.toml"
    path.parent.mkdir()
    path.write_text(original)
    with pytest.raises(ValueError):
        integrations._merge_toml_mcp(path, settings)
    assert path.read_text() == original
    assert not list(path.parent.glob("config.toml.devbrain-backup-*"))


@pytest.mark.parametrize("marked", [False, True])
def test_toml_marker_strings_cannot_claim_an_unrelated_mcp_server(settings, project, marked):
    path = Path(project["root"]) / ".codex/config.toml"
    path.parent.mkdir()
    fake_markers = 'notes = """\n# BEGIN DEVBRAIN\n# END DEVBRAIN\n"""\n'
    original = fake_markers + '[mcp_servers.devbrain]\ncommand = "unrelated"\nargs = ["other"]\n'
    if marked:
        original = '# BEGIN DEVBRAIN\n' + original + '# END DEVBRAIN\n'
    path.write_text(original)
    with pytest.raises(ValueError, match="unrelated server"):
        integrations._merge_toml_mcp(path, settings)
    assert path.read_text() == original
    assert not list(path.parent.glob("config.toml.devbrain-backup-*"))


def test_new_native_configuration_is_private_while_new_instructions_follow_umask(settings, project, installed):
    previous_umask = os.umask(0o022)
    try:
        integrations.install_integrations(settings, project)
    finally:
        os.umask(previous_umask)
    root = Path(project["root"])
    for relative in (".codex/config.toml", ".codex/hooks.json", ".mcp.json", ".claude/settings.local.json"):
        assert stat.S_IMODE((root / relative).stat().st_mode) == 0o600
    for relative in ("AGENTS.md", "CLAUDE.md"):
        assert stat.S_IMODE((root / relative).stat().st_mode) == 0o644


def test_new_empty_scope_recognizes_exact_session_forwarding_config(settings, project, installed):
    from devbrain.sessions import _managed_empty_file

    integrations.install_integrations(settings, project)
    root = Path(project["root"])
    for relative in (".codex/config.toml", ".mcp.json"):
        raw = (root / relative).read_bytes()
        assert _managed_empty_file(relative, raw, settings, project)
        if relative.endswith("toml"):
            changed = raw.replace(b'"DEVBRAIN_SESSION_ID"', b'"SOME_OTHER_VARIABLE"')
        else:
            changed = raw.replace(b'${DEVBRAIN_SESSION_ID:-}', b'stale-session')
        assert not _managed_empty_file(relative, changed, settings, project)


def test_manual_name_collision_is_not_overwritten(settings, project, installed):
    root = Path(project["root"])
    original = '{"mcpServers":{"devbrain":{"command":"unrelated-server"}}}'
    (root / ".mcp.json").write_text(original)
    result = integrations.install_integrations(settings, project, clients=("claude",))
    assert (root / ".mcp.json").read_text() == original
    assert any("unrelated server" in warning for warning in result["warnings"])


def test_unsupported_hook_version_has_explicit_refusal(settings, project, installed):
    installed["hooks"] = False
    result = integrations.install_integrations(settings, project)
    root = Path(project["root"])
    assert not (root / ".codex/hooks.json").exists()
    assert not (root / ".claude/settings.local.json").exists()
    assert (root / ".mcp.json").exists()
    assert any("verified native launch is not supported" in warning for warning in result["warnings"])


def test_missing_cli_does_not_install_unverified_hooks(settings, project, monkeypatch):
    monkeypatch.setattr(integrations, "_probe_cli", lambda *a: {
        "installed": False, "mcp": False, "hooks": False, "notes": []})
    result = integrations.install_integrations(settings, project)
    assert not (Path(project["root"]) / ".codex").exists()
    assert len(result["changed"]) == 2  # Managed instructions remain useful after a later client install.
    assert any("not installed" in warning for warning in result["warnings"])


def test_codex_hooks_have_bounded_end_time_and_preserve_existing(settings, project, installed):
    root = Path(project["root"])
    path = root / ".codex/hooks.json"
    path.parent.mkdir()
    existing = {"description": "mine", "hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "guard"}]}]}}
    path.write_text(json.dumps(existing))
    integrations.install_integrations(settings, project, clients=("codex",))
    hooks = json.loads(path.read_text())
    assert hooks["description"] == "mine"
    assert hooks["hooks"]["PreToolUse"] == existing["hooks"]["PreToolUse"]
    assert "compact" in hooks["hooks"]["SessionStart"][0]["matcher"]
    assert hooks["hooks"]["SessionStart"][0]["hooks"][0]["additionalContextLimit"] == 0
    assert hooks["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"] == 3
    assert "--dangerously" not in path.read_text()


def test_probe_detects_codex_hooks_feature_without_launching_a_session(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[-1] == "--version":
            output = "codex-cli 0.166.0-alpha.1"
        elif command[-1] == "--help":
            output = "Usage: codex [PROMPT]\nCommands: exec mcp features\nOptions: --no-daemon"
        else:
            output = "hooks stable true\nother stable false\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(integrations.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(integrations.subprocess, "run", fake_run)
    info = integrations._probe_cli("codex")
    assert info["hooks"] and info["mcp"] and info["verified_resume"]
    assert all(command[-1] in {"--version", "--help", "list"} for command in calls)


@pytest.mark.parametrize("version,help_text", [
    ("codex-cli 0.100.0", "[PROMPT] mcp --no-daemon"),
    ("codex-cli 0.166.0", "[PROMPT] mcp"),
    ("unknown", "[PROMPT] mcp --no-daemon"),
])
def test_probe_does_not_claim_verified_binding_for_unknown_or_shared_process(monkeypatch, version, help_text):
    def fake_run(command, **kwargs):
        output = version if command[-1] == "--version" else help_text if command[-1] == "--help" else "hooks stable true"
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(integrations.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(integrations.subprocess, "run", fake_run)
    assert not integrations._probe_cli("codex")["verified_resume"]


def test_native_launch_preserves_args_stdio_and_permissions(settings, project, installed, prepared,
                                                           native_backend, monkeypatch):
    from devbrain import ingest

    monkeypatch.setattr(integrations.shutil, "which", lambda name: f"/native/{name}")
    monkeypatch.setattr(integrations, "_prepare_session", lambda *a: copy.deepcopy(prepared))
    monkeypatch.setattr(ingest, "register_default_histories", lambda *a: {})
    calls = []

    def fake_call(command, **kwargs):
        calls.append((command, kwargs))
        return 7

    monkeypatch.setattr(integrations.subprocess, "call", fake_call)
    result = integrations.run_client(settings, project, "codex", ["resume", "--last"])
    assert result == 7
    command, kwargs = calls[0]
    assert command == ["/native/codex", "--no-daemon", "resume", "--last"]
    assert not {"stdout", "stderr", "stdin", "shell"}.intersection(kwargs)
    assert kwargs["cwd"] == Path(project["root"])
    assert kwargs["env"]["DEVBRAIN_PROJECT"] == project["id"]
    assert json.loads(Path(kwargs["env"]["DEVBRAIN_CONTEXT_FILE"]).read_text()) == prepared["packet"]
    assert kwargs["env"]["DEVBRAIN_SESSION_ID"] == prepared["session_id"]
    assert kwargs["env"]["DEVBRAIN_PACKET_ID"] == prepared["packet"]["id"]


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_each_native_launch_binds_new_ids_without_persisting_them(settings, project, installed, prepared,
                                                                native_backend, monkeypatch, client):
    sequence = []
    for number in (1, 2):
        current = copy.deepcopy(prepared)
        current["session_id"] = current["packet"]["session_id"] = f"launch-session-{number}"
        current["packet"]["id"] = f"launch-packet-{number}"
        current["packet"]["client"] = client
        sequence.append(current)
    packets = iter(sequence)
    monkeypatch.setattr(integrations, "_prepare_session", lambda *a: next(packets))
    monkeypatch.setattr(integrations.shutil, "which", lambda name: f"/native/{name}")
    captured = []
    path = Path(project["root"]) / (".codex/config.toml" if client == "codex" else ".mcp.json")

    def launch(command, **kwargs):
        captured.append(({name: kwargs["env"][name] for name in integrations.SESSION_ENV_VARS}, path.read_bytes()))
        return 0

    monkeypatch.setattr(integrations.subprocess, "call", launch)
    assert integrations.run_client(settings, project, client) == 0
    assert integrations.run_client(settings, project, client) == 0
    assert captured[0][0] != captured[1][0]
    assert captured[0][1] == captured[1][1]
    for current in sequence:
        assert current["session_id"].encode() not in path.read_bytes()
        assert current["packet"]["id"].encode() not in path.read_bytes()


def test_first_native_launch_installs_project_memory_before_client_starts(settings, project, installed, prepared, native_backend,
                                                                         monkeypatch, capsys):
    from devbrain import ingest

    root = Path(project["root"])
    probes = []
    real_probe = integrations._probe_cli

    def counted_probe(*args, **kwargs):
        probes.append(args)
        return real_probe(*args, **kwargs)

    monkeypatch.setattr(integrations, "_probe_cli", counted_probe)
    monkeypatch.setattr(integrations.shutil, "which", lambda name: f"/native/{name}")
    monkeypatch.setattr(integrations, "_prepare_session", lambda *a: copy.deepcopy(prepared))
    monkeypatch.setattr(ingest, "register_default_histories", lambda *a: {})

    def check_launch(command, **kwargs):
        assert (root / ".codex/config.toml").exists()
        assert (root / ".codex/hooks.json").exists()
        assert integrations.BEGIN in (root / "AGENTS.md").read_text()
        assert command[0] == "/native/codex"
        assert command[1] == "--no-daemon"
        assert "resume_context" in command[2] and "ack_resume" in command[2]
        assert kwargs["env"]["DEVBRAIN_CONTEXT_FILE"] in command[2]
        assert command[2].index("resume_context") < command[2].index(kwargs["env"]["DEVBRAIN_CONTEXT_FILE"])
        assert "returned packet is authoritative" in command[2]
        assert "partial or unavailable" in command[2] and "do not attempt ACK, checkpoint" in command[2]
        assert "--dangerously" not in str(command)
        return 0

    monkeypatch.setattr(integrations.subprocess, "call", check_launch)
    assert integrations.run_client(settings, project, "codex") == 0
    assert len(probes) == 1
    assert "trust must be reviewed" in capsys.readouterr().err


def test_failed_hook_install_refuses_verified_launch(settings, project, installed, monkeypatch):
    root = Path(project["root"])
    (root / ".codex").mkdir()
    (root / ".codex/hooks.json").write_text("malformed")
    monkeypatch.setattr(integrations.shutil, "which", lambda name: f"/native/{name}")
    monkeypatch.setattr(integrations, "_prepare_session", lambda *a: pytest.fail("Unsupported launch prepared a session"))
    monkeypatch.setattr(integrations.subprocess, "call", lambda *a, **kw: pytest.fail("Unsupported client started"))
    with pytest.raises(ValueError, match="launch refused"):
        integrations.run_client(settings, project, "codex")
    assert (root / ".codex/hooks.json").read_text() == "malformed"


@pytest.mark.parametrize("status", ["partial", "unavailable"])
def test_unready_preflight_blocks_native_process(settings, project, installed, prepared, monkeypatch, status):
    monkeypatch.setattr(integrations.shutil, "which", lambda name: f"/native/{name}")
    prepared.update(status=status, unknowns=["known transcript EOF is not archived"])
    monkeypatch.setattr(integrations, "_prepare_session", lambda *a: prepared)
    monkeypatch.setattr(integrations.subprocess, "call", lambda *a, **kw: pytest.fail("Native process started before ready"))
    with pytest.raises(ValueError, match="native launch blocked"):
        integrations.run_client(settings, project, "codex")
    assert not (settings.data_dir / "contexts").exists()


def test_unverified_capability_refuses_before_preparation(settings, project, installed, monkeypatch):
    installed["verified_resume"] = False
    installed["packet_passing"] = False
    monkeypatch.setattr(integrations.shutil, "which", lambda name: f"/native/{name}")
    monkeypatch.setattr(integrations, "_prepare_session", lambda *a: pytest.fail("Unknown client prepared a session"))
    monkeypatch.setattr(integrations.subprocess, "call", lambda *a, **kw: pytest.fail("Unknown client started"))
    with pytest.raises(ValueError, match="launch refused"):
        integrations.run_client(settings, project, "codex")


def test_verified_launch_refuses_remote_server_before_preparing(settings, project, installed, monkeypatch):
    monkeypatch.setattr(integrations.shutil, "which", lambda name: f"/native/{name}")
    monkeypatch.setattr(integrations, "_prepare_session", lambda *a: pytest.fail("Remote client prepared a local session"))
    monkeypatch.setattr(integrations.subprocess, "call", lambda *a, **kw: pytest.fail("Remote client started"))
    with pytest.raises(ValueError, match="remote/shared"):
        integrations.run_client(settings, project, "codex", ["--remote", "ws://other:1234"])


def test_prepare_session_ensures_service_and_preserves_selected_worktree(settings, project, prepared, monkeypatch):
    from devbrain import cli, sessions, store

    calls = []
    root = Path(project["root"])
    sentinel = object()
    monkeypatch.setattr(cli, "ensure_service", lambda value: calls.append(("service", value)))
    monkeypatch.setattr(store, "Store", lambda *a: sentinel)

    class Manager:
        def __init__(self, selected_store, selected_settings):
            assert selected_store is sentinel
            assert selected_settings is settings

        def prepare(self, project_id, client, **kwargs):
            calls.append(("prepare", project_id, client, kwargs))
            return prepared

    monkeypatch.setattr(sessions, "SessionManager", Manager)
    assert integrations._prepare_session(settings, project, "codex", root, ["resume", "--last"]) is prepared
    assert calls == [("service", settings), ("prepare", project["id"], "codex", {
        "worktree": str(root), "reason": "resume"})]


def test_saved_packet_is_exact_immutable_and_session_scoped(settings, project, prepared):
    path = integrations._packet_file(settings, prepared, project, "codex", Path(project["root"]))
    assert json.loads(path.read_text()) == prepared["packet"]
    assert path.parent.name == prepared["session_id"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert integrations._packet_file(settings, prepared, project, "codex", Path(project["root"])) == path
    changed = copy.deepcopy(prepared)
    changed["packet"]["hash"] = "different-hash"
    with pytest.raises(ValueError, match="Immutable"):
        integrations._packet_file(settings, changed, project, "codex", Path(project["root"]))
    assert json.loads(path.read_text()) == prepared["packet"]


def test_end_hook_only_enqueues_durable_import(settings, tmp_path, monkeypatch):
    from devbrain.store import Store

    root = tmp_path / "repo"
    root.mkdir()
    store = Store(settings.data_dir)
    project = store.add_project("demo", root)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"type":"user","message":{"role":"user","content":"Decision"}}\n')
    monkeypatch.setattr(integrations, "_hook_startup_context", lambda *a: pytest.fail("End hook requested context"))
    payload = {"hook_event_name": "SessionEnd", "cwd": str(root), "transcript_path": str(transcript)}
    assert integrations.hook(settings, payload, "claude") == {}
    assert integrations.hook(settings, payload, "claude") == {}
    assert len(list((settings.data_dir / "hook-events").glob("*.json"))) == 2
    integrations.drain_hook_spool(settings, store)
    jobs = store.query("SELECT FROM jobs WHERE kind=:kind", {"kind": "import"})
    assert len(jobs) == 1
    assert jobs[0]["project_id"] == project["id"]
    assert store.query("SELECT count(*) AS total FROM sources")[0]["total"] == 0


def test_end_hook_never_initializes_the_database(settings, tmp_path, monkeypatch):
    from devbrain import store

    monkeypatch.setattr(store, "Store", lambda *args: pytest.fail("SessionEnd touched the database"))
    assert integrations.hook(settings, {"hook_event_name": "SessionEnd", "cwd": str(tmp_path)}, "codex") == {}
    assert len(list((settings.data_dir / "hook-events").glob("*.json"))) == 1


def test_end_hook_captures_bounded_eof_and_logical_identity_without_network(settings, tmp_path, monkeypatch):
    from devbrain import store

    monkeypatch.setattr(store, "Store", lambda *a: pytest.fail("End hook opened a database"))
    monkeypatch.setattr(integrations.httpx, "Client", lambda **kw: pytest.fail("End hook opened HTTP"))
    monkeypatch.setenv("DEVBRAIN_SESSION_ID", "logical-session")
    monkeypatch.setenv("DEVBRAIN_PACKET_ID", "packet-one")
    transcript = tmp_path / "large-session.jsonl"
    content = b"x" * 10000 + b"\n"
    transcript.write_bytes(content)
    integrations.hook(settings, {"hook_event_name": "SessionEnd", "cwd": str(tmp_path),
                                 "session_id": "native-session", "transcript_path": str(transcript)}, "codex")
    path = next((settings.data_dir / "hook-events").glob("*.json"))
    event = json.loads(path.read_text())
    watermark = event["transcript_watermark"]
    assert event["event_id"] == path.stem
    assert event["session_id"] == "native-session"
    assert event["devbrain_session_id"] == "logical-session"
    assert event["packet_id"] == "packet-one"
    assert watermark["size"] == len(content)
    assert watermark["observed_size"] == len(content)
    assert watermark["pending_tail_bytes"] == 0
    assert watermark["pending_tail_sha256"] is None
    assert watermark["head_bytes"] == 4096
    assert watermark["head_sha256"] == hashlib.sha256(content[:4096]).hexdigest()
    assert watermark["stable"]
    assert watermark["mtime_ns"] == transcript.stat().st_mtime_ns


@pytest.mark.parametrize("complete,tail", [(b"", b""), (b"", b'{"unfinished":'),
                                         (b'{"complete":true}\n', b'{"unfinished":')])
def test_watermark_binds_complete_prefix_and_keeps_unfinished_tail_visible(tmp_path, complete, tail):
    path = tmp_path / "current.jsonl"
    path.write_bytes(complete + tail)
    watermark = integrations._transcript_watermark(path)
    assert watermark["size"] == len(complete)
    assert watermark["observed_size"] == len(complete + tail)
    assert watermark["pending_tail_bytes"] == len(tail)
    assert watermark["pending_tail_sha256"] == (hashlib.sha256(tail).hexdigest() if tail else None)
    assert watermark["head_bytes"] == min(4096, len(complete))
    assert watermark["head_sha256"] == hashlib.sha256(complete[:4096]).hexdigest()
    assert path.read_bytes() == complete + tail
    if tail:
        with path.open("ab") as handle:
            handle.write(b'true}\n')
        following = integrations._transcript_watermark(path)
        assert following["size"] == following["observed_size"] == path.stat().st_size
        assert following["pending_tail_bytes"] == 0
        assert watermark["pending_tail_bytes"] == len(tail)


def test_watermark_refuses_tail_larger_than_capture_budget(tmp_path):
    path = tmp_path / "large-current.jsonl"
    path.write_bytes(b'{}\n' + b"x" * (integrations.HOOK_TAIL_SCAN_LIMIT_BYTES + 1))
    with pytest.raises(ValueError, match="unfinished tail exceeds bounded"):
        integrations._transcript_watermark(path)


def test_end_hook_retains_explicit_watermark_capture_failure(settings, tmp_path, monkeypatch):
    path = tmp_path / "current.jsonl"
    path.write_bytes(b'{}\n')

    def failed_capture(path):
        raise ValueError("capture failed")

    monkeypatch.setattr(integrations, "_transcript_watermark", failed_capture)
    integrations.hook(settings, {"hook_event_name": "SessionEnd", "cwd": str(tmp_path),
                                 "transcript_path": str(path)}, "codex")
    receipt = json.loads(next((settings.data_dir / "hook-events").glob("*.json")).read_text())
    assert receipt["transcript_watermark_error"] == "ValueError"
    assert "transcript_watermark" not in receipt


def test_spool_drain_records_logical_receipt_before_removing_event(settings, tmp_path, monkeypatch):
    from devbrain import sessions
    from devbrain.store import Store

    root = tmp_path / "repo"
    root.mkdir()
    store = Store(settings.data_dir)
    store.add_project("demo", root)
    monkeypatch.setenv("DEVBRAIN_SESSION_ID", "logical-session")
    monkeypatch.setenv("DEVBRAIN_PACKET_ID", "packet-one")
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{}\n')
    seen = []

    class Manager:
        def __init__(self, *args):
            pass

        def note_event(self, payload, client):
            assert list((settings.data_dir / "hook-events").glob("*.json"))
            seen.append((payload, client))

    monkeypatch.setattr(sessions, "SessionManager", Manager)
    integrations.hook(settings, {"hook_event_name": "SessionEnd", "cwd": str(root),
                                 "session_id": "native-session", "transcript_path": str(transcript)}, "codex")
    result = integrations.drain_hook_spool(settings, store)
    assert result["events"] == result["queued"] == 1
    assert seen[0][0]["devbrain_session_id"] == "logical-session"
    assert list((settings.data_dir / "hook-events").glob("*.json")) == []


def test_end_hook_survives_unavailable_backend_and_restart(settings, tmp_path, monkeypatch):
    from devbrain.store import Store
    from devbrain import store as store_module

    root = tmp_path / "repo"
    root.mkdir()
    store = Store(settings.data_dir)
    store.add_project("contention", root)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"type":"user","message":{"role":"user","content":"remember"}}\n')
    monkeypatch.setattr(store_module, "Store", lambda *a: pytest.fail("End hook initialized unavailable engine"))
    start = time.monotonic()
    integrations.hook(settings, {"hook_event_name": "SessionEnd", "cwd": str(root),
                                 "transcript_path": str(transcript)}, "claude")
    assert time.monotonic() - start < 1

    class UnavailableBackend:
        def resolve_project(self, value):
            raise RuntimeError("ArcadeDB is temporarily unavailable")

    failed = integrations.drain_hook_spool(settings, UnavailableBackend())
    assert failed["errors"]
    assert len(list((settings.data_dir / "hook-events").glob("*.json"))) == 1
    # A fresh Store/service can recover the exact durable event after restart.
    counts = integrations.drain_hook_spool(settings, Store(settings.data_dir))
    assert counts["queued"] == 1
    assert list((settings.data_dir / "hook-events").glob("*.json")) == []


def test_session_start_returns_exact_prepared_packet(settings, project, prepared, monkeypatch):
    root = Path(project["root"])
    prepared["packet"]["constraints"] = ["Complete bounded packet " * 500]
    monkeypatch.setenv("DEVBRAIN_SESSION_ID", prepared["session_id"])
    monkeypatch.setenv("DEVBRAIN_PACKET_ID", prepared["packet"]["id"])
    real_client = httpx.Client

    def service(request):
        assert request.url.path == "/sessions/context"
        assert json.loads(request.content) == {"session_id": prepared["session_id"], "packet_id": "packet-one"}
        return httpx.Response(200, json=prepared)

    monkeypatch.setattr(integrations.httpx, "Client", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(service)))
    output = integrations.hook(settings, {"hook_event_name": "SessionStart", "source": "startup", "cwd": str(root)}, "codex")
    assert json.loads(output["hookSpecificOutput"]["additionalContext"]) == prepared["packet"]


def test_start_hook_exact_http_read_preserves_packet_when_project_state_changed(settings, project, monkeypatch):
    from fastapi.testclient import TestClient
    from devbrain.service import create_app
    from devbrain.sessions import SessionManager

    root = Path(project["root"])
    (root / "app.py").write_text("VALUE = 1\n")
    for arguments in (("init",), ("config", "user.email", "fixture@example.invalid"),
                      ("config", "user.name", "Fixture"), ("add", "app.py"), ("commit", "-m", "initial")):
        subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, check=True)
    # No inference worker is started; this test exercises only native-hook HTTP
    # routing and durable session state against the real local database.
    app = create_app(settings, start_worker=False, ollama=object())
    store, manager = app.state.store, app.state.sessions
    selected = store.add_project("hook-exact-read", root)
    prepared = manager.prepare(selected["id"], "codex", timeout=0)
    assert prepared["status"] == "ready"
    packet = prepared["packet"]
    monkeypatch.setenv("DEVBRAIN_SESSION_ID", prepared["session_id"])
    monkeypatch.setenv("DEVBRAIN_PACKET_ID", packet["id"])
    (root / "app.py").write_text("VALUE = 2\n")
    count_before = len(store.query("SELECT id FROM resume_packets WHERE session_id=:id",
                                  {"id": prepared["session_id"]}))

    def unexpected_refresh(*args, **kwargs):
        pytest.fail("The exact startup hook refreshed or replaced its prepared packet")

    monkeypatch.setattr(SessionManager, "resume_context", unexpected_refresh)
    real_client = httpx.Client
    with TestClient(app) as api:
        def service(request):
            body = json.loads(request.content)
            assert body == {"session_id": prepared["session_id"], "packet_id": packet["id"]}
            response = api.post(request.url.path, json=body, headers=dict(request.headers))
            return httpx.Response(response.status_code, json=response.json())

        def client_factory(**kwargs):
            # The real database uses the same httpx module. Preserve its
            # explicit base URL/transport instead of routing it to this hook.
            if "base_url" not in kwargs:
                kwargs["transport"] = httpx.MockTransport(service)
            return real_client(**kwargs)

        monkeypatch.setattr(integrations.httpx, "Client", client_factory)
        result = integrations.hook(settings, {"hook_event_name": "SessionStart", "source": "startup",
                                              "cwd": str(root), "session_id": "native-hook-fixture"}, "codex")
    assert "pending" in result["hookSpecificOutput"]["additionalContext"]
    assert "No ACK was created" in result["hookSpecificOutput"]["additionalContext"]
    session = store.query("SELECT FROM sessions WHERE id=:id", {"id": prepared["session_id"]})[0]
    assert session["packet_id"] == packet["id"] and session["epoch"] == prepared["epoch"]
    assert session.get("ack_id") is None
    assert len(store.query("SELECT id FROM resume_packets WHERE session_id=:id",
                           {"id": prepared["session_id"]})) == count_before
    assert not store.query("SELECT id FROM resume_acks WHERE session_id=:id", {"id": prepared["session_id"]})


def test_session_start_oversize_packet_is_pending_instead_of_truncated(settings, project, prepared, monkeypatch):
    monkeypatch.setenv("DEVBRAIN_SESSION_ID", prepared["session_id"])
    monkeypatch.setenv("DEVBRAIN_PACKET_ID", prepared["packet"]["id"])
    prepared["packet"]["constraints"] = ["x" * integrations.HOOK_PACKET_LIMIT_BYTES]
    real_client = httpx.Client
    monkeypatch.setattr(integrations.httpx, "Client", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(lambda request: httpx.Response(200, json=prepared))))
    output = integrations.hook(settings, {"hook_event_name": "SessionStart", "cwd": project["root"]}, "codex")
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "pending" in context and "exceeds" in context
    assert "No ACK" in context
    assert len(context.encode()) < 2000


def test_compact_start_only_spools_pending_epoch_never_acks_or_fetches(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("DEVBRAIN_SESSION_ID", "logical-session")
    monkeypatch.setenv("DEVBRAIN_PACKET_ID", "packet-one")
    monkeypatch.setattr(integrations.httpx, "Client", lambda **kw: pytest.fail("Compact hook fetched an old packet"))
    output = integrations.hook(settings, {"hook_event_name": "SessionStart", "source": "compact", "cwd": str(tmp_path)}, "codex")
    assert "new packet epoch" in output["hookSpecificOutput"]["additionalContext"]
    assert "No ACK was created" in output["hookSpecificOutput"]["additionalContext"]
    event = json.loads(next((settings.data_dir / "hook-events").glob("*.json")).read_text())
    assert event["resume_reason"] == "compact"


def test_start_hook_never_silently_delivers_replacement_packet(settings, project, prepared, monkeypatch):
    monkeypatch.setenv("DEVBRAIN_SESSION_ID", "logical-session")
    monkeypatch.setenv("DEVBRAIN_PACKET_ID", "packet-one")
    replacement = copy.deepcopy(prepared)
    replacement["packet"]["id"] = "packet-other"
    real_client = httpx.Client
    monkeypatch.setattr(integrations.httpx, "Client", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(lambda req: httpx.Response(200, json=replacement))))
    output = integrations.hook(settings, {"hook_event_name": "SessionStart", "cwd": project["root"]}, "codex")
    assert "no longer the exact" in output["hookSpecificOutput"]["additionalContext"]
    assert "packet-other" not in str(output)


def test_hook_does_not_mix_unknown_directory_into_project(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("DEVBRAIN_SESSION_ID", "logical-session")
    monkeypatch.setenv("DEVBRAIN_PACKET_ID", "packet-one")
    real_client = httpx.Client
    monkeypatch.setattr(integrations.httpx, "Client", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(lambda req: httpx.Response(404, json={"detail": "Unknown project"}))))
    result = integrations.hook(settings, {"hook_event_name": "SessionStart", "cwd": str(tmp_path / "other")}, "codex")
    assert "verified resume pending" in result["hookSpecificOutput"]["additionalContext"]


def test_start_hook_offline_never_initializes_database(settings, tmp_path, monkeypatch):
    from devbrain import store

    monkeypatch.setattr(store, "Store", lambda *args: pytest.fail("Start hook initialized an engine"))
    monkeypatch.setenv("DEVBRAIN_SESSION_ID", "logical-session")
    monkeypatch.setenv("DEVBRAIN_PACKET_ID", "packet-one")
    real_client = httpx.Client

    def offline(request):
        raise httpx.ConnectError("offline", request=request)

    monkeypatch.setattr(integrations.httpx, "Client", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(offline)))
    result = integrations.hook(settings, {"hook_event_name": "SessionStart", "cwd": str(tmp_path)}, "codex")
    assert "verified resume pending" in result["hookSpecificOutput"]["additionalContext"]
    assert "No database engine was started" in result["hookSpecificOutput"]["additionalContext"]


def test_start_hook_never_substitutes_legacy_rolling_context(settings, tmp_path, monkeypatch):
    real_client = httpx.Client

    def offline(request):
        raise httpx.ConnectError("offline", request=request)

    monkeypatch.setattr(integrations.httpx, "Client", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(offline)))
    root = tmp_path / "repo"
    root.mkdir()
    directory = settings.data_dir / "contexts"
    directory.mkdir()
    (directory / "demo.md").write_text("Private project state")
    (directory / "demo.json").write_text(json.dumps({"project": "demo", "root": str(root)}))
    monkeypatch.setenv("DEVBRAIN_PROJECT", "demo")
    output = integrations.hook(settings, {"hook_event_name": "SessionStart", "cwd": str(root)}, "codex")
    assert "Private project state" not in output["hookSpecificOutput"]["additionalContext"]
    assert "no launcher-bound" in output["hookSpecificOutput"]["additionalContext"]
    other = integrations.hook(settings, {"hook_event_name": "SessionStart", "cwd": str(tmp_path / "other")}, "codex")
    assert "Private project state" not in str(other)


def test_doctor_reports_arcadedb_without_lifecycle_work_or_credentials(settings, monkeypatch):
    from devbrain import store

    monkeypatch.setattr(settings, "java_executable", "")
    monkeypatch.setattr(store, "Store", lambda *a: pytest.fail("Doctor initialized a database"))
    monkeypatch.setattr(integrations, "_probe_cli", lambda *a: {"installed": False})
    monkeypatch.setattr(integrations.shutil, "which", lambda name: None)
    real_client = httpx.Client
    calls = []

    def backend(request):
        calls.append(request)
        if request.url.path == "/api/v1/server":
            assert request.url.params["mode"] == "basic"
            assert request.headers["Authorization"].startswith("Basic ")
            return httpx.Response(200, json={"version": settings.arcade_version, "serverName": "test"})
        if request.url.path.startswith("/api/v1/query/"):
            assert json.loads(request.content)["command"] == "SELECT 1 AS ok"
            return httpx.Response(200, json={"result": [{"ok": 1}]})
        return httpx.Response(503)

    monkeypatch.setattr(integrations.httpx, "Client", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(backend)))
    result = integrations.doctor(settings)
    assert result["arcadedb"]["available"]
    assert result["arcadedb"]["database_available"]
    assert result["arcadedb"]["version_matches"]
    assert result["java"] == {"on_path": False, "minimum_major": 21}
    assert "sqlite" not in result
    assert "sqlite_vec" not in result
    assert settings.arcade_password not in json.dumps(result)
    assert all("create" not in request.url.path and "command" not in request.url.path for request in calls)


@pytest.mark.asyncio
async def test_mcp_tools_forward_project_scope_and_source_ids(settings):
    class FakeService:
        def __init__(self):
            self.calls = []

        async def post(self, endpoint, payload):
            self.calls.append((endpoint, payload))
            return {"ok": True}

        async def get(self, endpoint, params):
            self.calls.append((endpoint, params))
            return {"checks": []}

    bridge = FakeService()
    server = create_server(settings, bridge)
    tools = await server.list_tools()
    assert {tool.name for tool in tools} == TOOLS
    await server.call_tool("ask", {"project": "demo", "question": "Where is authentication?"})
    await server.call_tool("remember", {"project": "demo", "record": {"kind": "decision", "content": "x"}, "sources": ["source-a"]})
    await server.call_tool("checkpoint", {"project": "demo", "state": {"next": "tests"}})
    assert bridge.calls[0] == ("/ask", {"project": "demo", "question": "Where is authentication?"})
    assert bridge.calls[1][1]["sources"] == ["source-a"]
    assert bridge.calls[2][1]["state"] == {"next": "tests"}
    await server.call_tool("save_plan", {"project": "demo", "plan": {"title": "Implement login"},
                                         "steps": [{"key": "code", "content": "Implement"}],
                                         "idempotency_key": "request-a"})
    await server.call_tool("next_task", {"project": "demo", "worktree": "/repo"})
    await server.call_tool("resume_context", {})
    await server.call_tool("ack_resume", {"packet_id": "packet-a", "packet_hash": "hash-a",
                                          "readback": {"plan": None, "task": None}})
    await server.call_tool("check_status", {"project": "demo", "task_id": "task-a", "worktree": "/repo"})
    assert bridge.calls[3][0] == "/plans/save"
    assert bridge.calls[3][1]["idempotency_key"] == "request-a"
    assert bridge.calls[4] == ("/plans/next", {"project": "demo", "plan_id": None, "worktree": "/repo"})
    assert bridge.calls[5] == ("/sessions/context", {})
    assert bridge.calls[6][0] == "/sessions/ack"
    assert bridge.calls[7] == ("/checks", {"project": "demo", "task_id": "task-a", "worktree": "/repo"})
    schema = {"type": "object", "properties": {"version": {"type": "integer"}},
              "required": ["version"], "additionalProperties": False}
    await server.call_tool("ask", {"project": "demo", "question": "Version?", "answer_schema": schema})
    assert bridge.calls[8] == ("/ask", {"project": "demo", "question": "Version?", "answer_schema": schema})
    from mcp.server.fastmcp.exceptions import ToolError
    with pytest.raises(ToolError):
        await server.call_tool("ask", {"project": "demo", "question": "Version?",
                                      "answer_schema": {"$ref": "https://example.invalid/private"}})
    assert len(bridge.calls) == 9


@pytest.mark.asyncio
async def test_mcp_question_limit_matches_http(settings):
    from mcp.server.fastmcp.exceptions import ToolError

    class FakeService:
        async def post(self, endpoint, payload):
            return {"ok": True}

    server = create_server(settings, FakeService())
    await server.call_tool("ask", {"project": "demo", "question": "x" * 16000})
    with pytest.raises(ToolError, match="too long"):
        await server.call_tool("ask", {"project": "demo", "question": "x" * 16001})


@pytest.mark.asyncio
async def test_actual_stdio_mcp_initialization_is_protocol_clean(settings):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    server = StdioServerParameters(command=sys.executable,
                                  args=["-m", "devbrain", "--data-dir", str(settings.data_dir), "mcp"])
    async with stdio_client(server) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            result = await session.initialize()
            assert result.serverInfo.name == "devbrain"
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == TOOLS


@pytest.mark.asyncio
async def test_service_bridge_uses_bearer_and_local_transport(settings, monkeypatch):
    seen = []
    real_client = httpx.AsyncClient

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"status": "complete", "answer": "Evidence"})

    def client_factory(**kwargs):
        assert kwargs["trust_env"] is False
        return real_client(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr("devbrain.mcp_server.httpx.AsyncClient", client_factory)
    result = await ServiceClient(settings).post("/ask", {"project": "demo", "question": "what?"})
    assert result["status"] == "complete"
    assert seen[0].headers["Authorization"] == f"Bearer {settings.token}"
    assert seen[0].url.host == "127.0.0.1"


@pytest.mark.asyncio
async def test_service_bridge_reports_http_error_without_token(settings, monkeypatch):
    real_client = httpx.AsyncClient
    monkeypatch.setattr("devbrain.mcp_server.httpx.AsyncClient", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(lambda req: httpx.Response(401, json={"detail": "Unauthorized"}))))
    with pytest.raises(RuntimeError, match="401") as error:
        await ServiceClient(settings, session_id="logical-session", packet_id="packet-one").post("/remember", {})
    assert settings.token not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["/remember", "/checkpoint", "/plans/save", "/sessions/context", "/sessions/ack"])
async def test_unbound_mcp_cannot_use_manual_admin_write_path(settings, monkeypatch, endpoint):
    monkeypatch.setattr("devbrain.mcp_server.httpx.AsyncClient", lambda **kw: pytest.fail("Unbound client reached HTTP"))
    with pytest.raises(RuntimeError, match="resume_required"):
        await ServiceClient(settings).post(endpoint, {})


@pytest.mark.asyncio
async def test_bound_bridge_rejects_forged_session_identity(settings, monkeypatch):
    monkeypatch.setattr("devbrain.mcp_server.httpx.AsyncClient", lambda **kw: pytest.fail("Forged session reached HTTP"))
    client = ServiceClient(settings, session_id="logical-session", packet_id="packet-one")
    with pytest.raises(RuntimeError, match="Session mismatch"):
        await client.post("/checkpoint", {"session_id": "different", "state": {}})
    with pytest.raises(RuntimeError, match="Session mismatch"):
        await client.post("/sessions/context", {"session_id": "different"})
    with pytest.raises(RuntimeError, match="Session mismatch"):
        await client.get("/checks", {"project": "demo", "session_id": "different"})


@pytest.mark.asyncio
async def test_bound_project_reads_include_session_scope_without_requiring_ack(settings, monkeypatch):
    real_client = httpx.AsyncClient
    seen = []

    def backend(request):
        payload = json.loads(request.content) if request.method == "POST" else dict(request.url.params)
        seen.append((request.url.path, payload))
        assert payload["session_id"] == "logical-session"
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr("devbrain.mcp_server.httpx.AsyncClient", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(backend)))
    client = ServiceClient(settings, session_id="logical-session", packet_id="packet-one")
    body = {"project": "demo", "question": "What differs on this branch?"}
    query = {"project": "demo"}
    await client.post("/ask", body)
    await client.post("/plans/next", {"project": "demo", "worktree": None})
    await client.get("/checks", query)
    await client.get("/context", query)
    assert [path for path, _ in seen] == ["/ask", "/plans/next", "/checks", "/context"]
    assert "session_id" not in body and "session_id" not in query
    assert all(path != "/sessions/ack" for path, _ in seen)


@pytest.mark.asyncio
async def test_bound_bridge_refreshes_packet_then_requires_explicit_real_ack(settings, monkeypatch):
    real_client = httpx.AsyncClient
    seen = []

    def backend(request):
        payload = json.loads(request.content)
        seen.append((request.url.path, payload))
        assert payload["session_id"] == "logical-session"
        if request.url.path == "/sessions/context":
            return httpx.Response(200, json={"session_id": "logical-session", "epoch": 2, "status": "ready",
                                             "packet": {"id": "packet-new", "hash": "hash-new"}})
        return httpx.Response(200, json={"ok": True})

    def factory(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["timeout"] in {15, 45}
        return real_client(**kwargs, transport=httpx.MockTransport(backend))

    monkeypatch.setattr("devbrain.mcp_server.httpx.AsyncClient", factory)
    client = ServiceClient(settings, session_id="logical-session", packet_id="packet-one")
    await client.post("/sessions/context", {})
    assert client.packet_id == "packet-new"
    assert len(seen) == 1  # Reading/delivery never sends an ACK automatically.
    with pytest.raises(RuntimeError, match="exact packet"):
        await client.post("/sessions/ack", {"packet_id": "packet-one", "packet_hash": "old"})
    await client.post("/sessions/ack", {"packet_id": "packet-new", "packet_hash": "hash-new",
                                        "readback": {"plan": None, "task": None}})
    await client.post("/remember", {"project": "demo", "record": {"content": "state"}, "sources": ["source-a"]})
    assert seen[1] == ("/sessions/ack", {"session_id": "logical-session", "packet_id": "packet-new",
                                        "packet_hash": "hash-new", "readback": {"plan": None, "task": None}})
    assert seen[2][1]["session_id"] == "logical-session"


@pytest.mark.asyncio
async def test_bound_bridge_rejects_cross_session_context_response(settings, monkeypatch):
    real_client = httpx.AsyncClient
    monkeypatch.setattr("devbrain.mcp_server.httpx.AsyncClient", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(lambda req: httpx.Response(200, json={
            "session_id": "another-session", "packet": {"id": "bad", "hash": "bad"}}))))
    client = ServiceClient(settings, session_id="logical-session", packet_id="packet-one")
    with pytest.raises(RuntimeError, match="Session mismatch"):
        await client.post("/sessions/context", {})
    assert client.packet_id == "packet-one"


def test_plan_tool_views_bound_context_without_echoing_source_contents():
    from devbrain.mcp_server import _next_task_view, _saved_plan_view

    tasks = [{"id": f"task-{n}", "version": 1, "content": "code" * 10000,
              "source_ids": ["source-a"], "metadata": {"state": "todo", "step_key": str(n),
                "unrelated_huge_payload": "x" * 100000, "acceptance_criteria": ["x" * 10000] * 50}}
             for n in range(210)]
    plan = {"id": "plan-a", "version": 1, "title": "Build", "goal": "x" * 100000,
            "step_ids": [t["id"] for t in tasks], "source_ids": ["source-a"]}
    result = _saved_plan_view({"plan": plan, "tasks": tasks, "replayed": False})
    assert len(result["tasks"]) == 200
    assert result["omitted_tasks"] == 10
    assert result["plan"]["source_ids"] == ["source-a"]
    assert "unrelated_huge_payload" not in json.dumps(result)
    assert "content" not in result["tasks"][0]
    next_result = _next_task_view({"plan": plan, "task": tasks[0], "reason": "ready", "blocked": []})
    assert next_result["task"]["content_truncated"]
    assert "acceptance_criteria" in next_result["task"]["metadata_truncated_fields"]
    assert len(json.dumps(next_result)) < 13000


def test_invalid_json_is_not_replaced(settings, project, installed):
    path = Path(project["root"]) / ".mcp.json"
    path.write_text("not JSON")
    result = integrations.install_integrations(settings, project, ("claude",))
    assert path.read_text() == "not JSON"
    assert result["warnings"]


CLIENT_FIXTURES = Path(__file__).parent / "fixtures" / "clients"


@pytest.mark.parametrize("fixture", sorted(path.name for path in CLIENT_FIXTURES.iterdir()))
def test_golden_client_outputs_satisfy_every_capability_check(monkeypatch, fixture):
    """Real outputs of `<client> --version`, `--help` and `codex features list`.

    Add a directory per new client version (see `cairnmind doctor --contract`). A failure
    names the exact capability check that a client update broke.
    """
    folder = CLIENT_FIXTURES / fixture
    client = fixture.split("-")[0]
    files = {"--version": "version.txt", "--help": "help.txt", "list": "features.txt"}

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, (folder / files[command[-1]]).read_text(encoding="utf-8"), "")

    monkeypatch.setattr(integrations.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(integrations.subprocess, "run", fake_run)
    info = integrations._probe_cli(client)
    failed = {name: check for name, check in info["checks"].items() if not check["ok"]}
    assert not failed and info["verified_resume"]
    expected = {"version", "mcp", "prompt", "hooks"} | ({"isolated_process"} if client == "codex" else set())
    assert all(info["checks"][name]["evidence"] for name in expected)


def _golden_probe(monkeypatch, fixture, *, version=None):
    folder = CLIENT_FIXTURES / fixture
    files = {"--version": "version.txt", "--help": "help.txt", "list": "features.txt"}

    def fake_run(command, **kwargs):
        text = (folder / files[command[-1]]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, version if version and command[-1] == "--version" else text, "")

    monkeypatch.setattr(integrations.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(integrations.subprocess, "run", fake_run)
    return integrations._probe_cli(fixture.split("-")[0])


def test_contract_text_renders_one_aligned_line_per_client_and_check_from_golden_outputs(monkeypatch):
    probes = {"codex": _golden_probe(monkeypatch, "codex-0.166.0"), "claude": _golden_probe(monkeypatch, "claude-2.1.319")}
    lines = integrations.contract_text(probes).splitlines()
    table = [line.split(None, 3) for line in lines]
    assert [(row[0], row[1], row[2]) for row in table] == [
        (client, check, "ok") for client in ("codex", "claude")
        for check in ("version", "mcp", "prompt", "isolated_process", "hooks", "verified_resume")]
    evidence = {(row[0], row[1]): row[3] for row in table}
    assert evidence[("codex", "version")] == probes["codex"]["version"]
    assert evidence[("codex", "mcp")] == " ".join(probes["codex"]["checks"]["mcp"]["evidence"].split())
    # The evidence is the line of the mcp command itself, not a flag example that mentions the word.
    assert evidence[("codex", "mcp")].startswith("mcp ") and evidence[("claude", "mcp")].startswith("mcp ")
    assert evidence[("codex", "hooks")].startswith("hooks ") and evidence[("codex", "hooks")].endswith("true")
    # A check that needs no output line says what it rests on instead of showing nothing.
    assert evidence[("claude", "isolated_process")] == "(always)"
    assert evidence[("claude", "verified_resume")] == "(every check above passed)"
    # Columns are aligned: every line has its status at the same offset.
    assert len({line.index(" ok ") for line in lines}) == 1


def test_contract_text_names_what_a_failed_check_needed_and_found(monkeypatch):
    old = _golden_probe(monkeypatch, "claude-2.1.319", version="1.0.100 (Claude Code)\x1b[31m\nforged  line  ok")
    monkeypatch.setattr(integrations.shutil, "which", lambda name: None)
    missing = integrations._probe_cli("codex")
    broken = {"installed": True, "version": None, "verified_resume": False, "checks": {},
              "notes": ["Capability probe failed: TimeoutExpired"]}
    text = integrations.contract_text({"claude": old, "codex": missing, "other": broken})
    lines = text.splitlines()
    assert "\x1b" not in text and len(lines) == 6 + 1 + 2
    rows = {tuple(line.split(None, 3)[:3]): line.split(None, 3)[3] for line in lines}
    assert rows[("claude", "version", "failed")] == "(needs >= 1.0.112; found 1.0.100 (Claude Code)\\x1b[31m forged line ok)"
    assert rows[("claude", "hooks", "failed")].startswith("(needs version >= 1.0.112; found 1.0.100")
    assert rows[("claude", "mcp", "ok")] and rows[("claude", "verified_resume", "failed")] == "(launch is refused)"
    assert rows[("codex", "installed", "failed")] == "(the command was not found on PATH)"
    assert rows[("other", "probe", "failed")] == "(Capability probe failed: TimeoutExpired)"
    assert rows[("other", "verified_resume", "failed")] == "(launch is refused)"


def test_doctor_contract_prints_text_on_request_and_json_by_default(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from devbrain import cli
    probes = {"codex": _golden_probe(monkeypatch, "codex-0.166.0"), "claude": _golden_probe(monkeypatch, "claude-2.1.319")}
    monkeypatch.setattr(integrations, "_probe_cli", lambda name: probes[name])
    runner, base = CliRunner(), ["--data-dir", str(tmp_path / "data"), "doctor", "--contract"]
    as_json = runner.invoke(cli.app, base)
    assert as_json.exit_code == 0 and json.loads(as_json.output)["codex"]["checks"]["mcp"]["ok"] is True
    assert runner.invoke(cli.app, [*base, "--format", "json"]).output == as_json.output
    as_text = runner.invoke(cli.app, [*base, "--format", "text"])
    assert as_text.exit_code == 0 and as_text.output == integrations.contract_text(probes) + "\n"
    probes["claude"] = {**probes["claude"], "verified_resume": False}
    assert runner.invoke(cli.app, [*base, "--format", "text"]).exit_code == 1
    assert runner.invoke(cli.app, [*base, "--format", "table"]).exit_code != 0
    refused = runner.invoke(cli.app, ["--data-dir", str(tmp_path / "data"), "doctor", "--format", "text"])
    assert refused.exit_code != 0 and "only with --contract" in refused.output


@pytest.mark.parametrize("help_text,expected", [
    ("Commands: exec mcp features", True),
    ("  mcp        Manage MCP servers", True),
    ("Options: --mcp-config <file>", False),
    ("Options: --debug=hooks,mcp", False),
    ("  filtering (e.g., --debug=hooks,mcp or\n  --debug='!hooks,!mcp')", False),
    ('  --debug="!mcp"', False),
])
def test_mcp_capability_requires_the_command_word_not_a_flag(help_text, expected):
    import re
    assert bool(re.search(integrations.MCP_COMMAND, help_text)) is expected
