"""Tests for universal MCP presets, JSON/JSONC/TOML deep-merge, security consent, and stdio probe."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tomllib

import pytest
from typer.testing import CliRunner

from cairnmind import cli, ingest, mcp_presets, mcp_probe
from cairnmind.config import Settings

runner = CliRunner()


@pytest.fixture
def settings(tmp_path):
    return Settings.load(tmp_path / "memory", create=False)


@pytest.mark.parametrize("client", mcp_presets.SUPPORTED_CLIENTS)
def test_all_client_adapters_snapshot_schema_and_no_consent_bypass(tmp_path, settings, client):
    repo = tmp_path / "my-project"
    repo.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    preset = mcp_presets.generate_preset(
        client,
        settings,
        repo,
        write=False,
        home=home,
        env={"HOME": str(home), "APPDATA": str(home / "AppData" / "Roaming")},
        system="linux",
    )
    assert preset["client"] == client
    spec = mcp_presets.CLIENT_SPECS[client]
    assert preset["root_key"] == spec.root_key
    entry = preset["entry"]

    # Every adapter uses an absolute binary path and an argument list (never a shell string).
    assert Path(entry["command"]).is_absolute()
    assert isinstance(entry["args"], list) and "mcp" in entry["args"]

    # Security consent invariant: ZERO auto-approval flags in any client preset!
    assert not (mcp_presets.FORBIDDEN_CONSENT_KEYS & set(entry.keys()))
    for forbidden in mcp_presets.FORBIDDEN_CONSENT_KEYS:
        assert forbidden not in preset["content"]

    # Parse output with the client's real format parser and verify root key & structure.
    if spec.file_format == "toml":
        parsed = tomllib.loads(preset["content"])
        assert "mcp_servers" in parsed and "cairnmind" in parsed["mcp_servers"]
        assert parsed["mcp_servers"]["cairnmind"]["startup_timeout_sec"] == 15
    else:
        parsed = json.loads(preset["content"])
        assert spec.root_key in parsed and "cairnmind" in parsed[spec.root_key]
        if client == "vscode":
            assert parsed["servers"]["cairnmind"]["type"] == "stdio"
        if client == "zed":
            assert "env" in parsed["context_servers"]["cairnmind"]


def test_jsonc_merge_preserves_siblings_and_rejects_broken_syntax(tmp_path, settings):
    repo = tmp_path / "repo"
    repo.mkdir()
    vscode_mcp = repo / ".vscode" / "mcp.json"
    vscode_mcp.parent.mkdir()
    original_jsonc = """{
  // Keep user comment and trailing commas
  "servers": {
    "github": {
      "type": "stdio",
      "command": "/usr/local/bin/github-mcp",
      "args": ["https://api.github.com/v1"], // URL with // inside string!
    },
  },
  /* multi-line
     block comment */
  "inputs": [],
}
"""
    vscode_mcp.write_text(original_jsonc, encoding="utf-8")

    result = mcp_presets.generate_preset("vscode", settings, repo, scope="project", write=True)
    assert result["written"] is True
    assert result["backup"] == str(vscode_mcp.with_name("mcp.json.cairnmind.bak"))
    assert Path(result["backup"]).read_text(encoding="utf-8") == original_jsonc

    merged = json.loads(vscode_mcp.read_text(encoding="utf-8"))
    assert merged["inputs"] == []
    assert merged["servers"]["github"]["args"] == ["https://api.github.com/v1"]
    assert merged["servers"]["cairnmind"]["type"] == "stdio"

    # Idempotent second write does nothing.
    second = mcp_presets.generate_preset("vscode", settings, repo, scope="project", write=True)
    assert second["written"] is False

    # Broken JSONC syntax leaves file untouched!
    broken = "{ invalid jsonc /* unterminated"
    zed_cfg = repo / ".zed" / "settings.json"
    zed_cfg.parent.mkdir()
    zed_cfg.write_text(broken, encoding="utf-8")
    with pytest.raises(mcp_presets.ConfigSyntaxError):
        mcp_presets.generate_preset("zed", settings, repo, scope="project", write=True)
    assert zed_cfg.read_text(encoding="utf-8") == broken


def test_toml_merge_preserves_comments_and_sibling_servers(tmp_path, settings):
    repo = tmp_path / "repo"
    repo.mkdir()
    codex_cfg = repo / ".codex" / "config.toml"
    codex_cfg.parent.mkdir()
    original = (
        '# User Codex config\n'
        'model = "o3"\n\n'
        '[mcp_servers.other]\n'
        'command = "/usr/bin/other"\n'
        'args = ["--flag"]\n'
    )
    codex_cfg.write_text(original, encoding="utf-8")

    result = mcp_presets.generate_preset("codex", settings, repo, scope="project", write=True)
    assert result["written"] is True
    updated_text = codex_cfg.read_text(encoding="utf-8")
    assert "# User Codex config" in updated_text
    parsed = tomllib.loads(updated_text)
    assert parsed["model"] == "o3"
    assert parsed["mcp_servers"]["other"]["command"] == "/usr/bin/other"
    assert parsed["mcp_servers"]["cairnmind"]["command"] == str(Path(sys.executable).absolute())
    assert not list(codex_cfg.parent.glob(".config.toml.cairnmind-*.tmp"))


def test_user_scope_multi_project_protection_and_custom_name(tmp_path, settings):
    home = tmp_path / "home"
    home.mkdir()
    repo_a = tmp_path / "repo-alpha"
    repo_b = tmp_path / "repo-beta"
    repo_a.mkdir()
    repo_b.mkdir()

    first = mcp_presets.generate_preset("cursor", settings, repo_a, scope="user", write=True, home=home)
    assert first["written"] is True

    # Attempting to overwrite global `cairnmind` with a different project must fail safely!
    with pytest.raises(mcp_presets.MultiProjectConflictError, match="cairnmind-repo-beta"):
        mcp_presets.generate_preset("cursor", settings, repo_b, scope="user", write=True, home=home)

    # Using `--name cairnmind-repo-beta` keeps BOTH projects cleanly.
    second = mcp_presets.generate_preset(
        "cursor", settings, repo_b, scope="user", server_name="cairnmind-repo-beta", write=True, home=home
    )
    assert second["written"] is True
    cfg = json.loads((home / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert "cairnmind" in cfg["mcpServers"]
    assert "cairnmind-repo-beta" in cfg["mcpServers"]
    assert str(repo_a.resolve()) in cfg["mcpServers"]["cairnmind"]["args"]
    assert str(repo_b.resolve()) in cfg["mcpServers"]["cairnmind-repo-beta"]["args"]

    # Explicit `--force` allows overwriting the default key.
    forced = mcp_presets.generate_preset("cursor", settings, repo_b, scope="user", write=True, force=True, home=home)
    assert forced["written"] is True
    cfg2 = json.loads((home / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert str(repo_b.resolve()) in cfg2["mcpServers"]["cairnmind"]["args"]


def test_foreign_server_conflict_never_overwritten(tmp_path, settings):
    repo = tmp_path / "repo"
    repo.mkdir()
    cursor_cfg = repo / ".cursor" / "mcp.json"
    cursor_cfg.parent.mkdir()
    original = json.dumps({"mcpServers": {"cairnmind": {"command": "/usr/bin/unrelated-server", "args": []}}})
    cursor_cfg.write_text(original, encoding="utf-8")

    with pytest.raises(mcp_presets.ForeignServerConflictError):
        mcp_presets.generate_preset("cursor", settings, repo, scope="project", write=True)
    assert cursor_cfg.read_text(encoding="utf-8") == original


def test_windows_paths_with_spaces_and_temp_executable_warning(tmp_path, settings):
    repo = tmp_path / "Jan Kowalski Project"
    repo.mkdir()
    win_exe = r"C:\Users\Jan Kowalski\AppData\Local\Temp\venv\Scripts\python.exe"
    preset_json = mcp_presets.generate_preset("cursor", settings, repo, scope="project", executable=win_exe)
    assert any("temporary directory" in w for w in preset_json["warnings"])
    parsed_json = json.loads(preset_json["content"])
    assert "Jan Kowalski" in parsed_json["mcpServers"]["cairnmind"]["command"]
    assert str(repo.resolve()) in parsed_json["mcpServers"]["cairnmind"]["args"]

    preset_toml = mcp_presets.generate_preset("codex", settings, repo, scope="project", executable=win_exe)
    parsed_toml = tomllib.loads(preset_toml["content"])
    assert "Jan Kowalski" in parsed_toml["mcp_servers"]["cairnmind"]["command"]


def test_cross_platform_user_config_paths(tmp_path):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    win_env = {"APPDATA": r"C:\Users\Alice\AppData\Roaming"}
    mac_path = mcp_presets.config_path_for_client("claude-desktop", "user", repo, home=home, system="darwin")
    lin_path = mcp_presets.config_path_for_client("claude-desktop", "user", repo, home=home, env={}, system="linux")
    win_path = mcp_presets.config_path_for_client("claude-desktop", "user", repo, home=home, env=win_env, system="win32")
    assert "Library/Application Support/Claude/claude_desktop_config.json" in mac_path.as_posix()
    assert ".config/Claude/claude_desktop_config.json" in lin_path.as_posix()
    assert "claude_desktop_config.json" in str(win_path)


def test_cli_mcp_generate_dry_run_stdout_write_and_auto(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".cursor").mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("PATH", "")

    data_dir = tmp_path / "memory"
    # Dry-run writes nothing.
    dry = runner.invoke(
        cli.app,
        ["--data-dir", str(data_dir), "mcp", "generate", "--client", "cursor", "--project", str(repo), "--dry-run"],
    )
    assert dry.exit_code == 0, dry.output
    assert "mcpServers" in dry.stdout
    assert not (repo / ".cursor" / "mcp.json").exists()

    # Generic prints canonical JSON to stdout.
    gen = runner.invoke(
        cli.app,
        ["--data-dir", str(data_dir), "mcp", "generate", "--client", "generic", "--project", str(repo)],
    )
    assert gen.exit_code == 0, gen.output
    assert "cairnmind" in json.loads(gen.stdout)["mcpServers"]

    # Auto mode detects `.cursor` in repo.
    auto = runner.invoke(
        cli.app,
        ["--data-dir", str(data_dir), "mcp", "generate", "--auto", "--project", str(repo), "--json"],
    )
    assert auto.exit_code == 0, auto.output
    auto_data = json.loads(auto.stdout)
    assert any(item["client"] == "cursor" for item in auto_data["detected"])
    assert not (repo / ".cursor" / "mcp.json").exists()

    # Write creates `.cursor/mcp.json`.
    written = runner.invoke(
        cli.app,
        ["--data-dir", str(data_dir), "mcp", "generate", "--client", "cursor", "--project", str(repo), "--write"],
    )
    assert written.exit_code == 0, written.output
    assert (repo / ".cursor" / "mcp.json").is_file()


def test_indexer_ignores_generated_mcp_preset_files_and_backups(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    for rel in (
        ".mcp.json",
        ".cursor/mcp.json",
        ".vscode/mcp.json",
        ".roo/mcp.json",
        ".gemini/settings.json",
        ".zed/settings.json",
        ".cursor/mcp.json.cairnmind.bak",
    ):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}\n", encoding="utf-8")

    indexed = [p.relative_to(root).as_posix() for p in ingest._files(root)]
    assert indexed == ["app.py"]


def test_mcp_probe_live_clean_stdio_handshake(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    data_dir = tmp_path / "memory"
    res = runner.invoke(
        cli.app,
        ["--data-dir", str(data_dir), "mcp", "test-connection", "--project", str(repo), "--json"],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["ok"] is True
    assert data["stdout_clean"] is True
    assert data["tools_count"] >= 8
    assert mcp_probe.EXPECTED_CORE_TOOLS <= set(data["tools"])
    assert data["latency_ms"] > 0


def test_mcp_probe_fails_hard_on_dirty_stdout(tmp_path):
    dirty_script = tmp_path / "dirty_server.py"
    dirty_script.write_text(
        'import sys\n'
        'sys.stdout.write("Starting Cairnmind banner...\\n")\n'
        'sys.stdout.flush()\n',
        encoding="utf-8",
    )
    result = mcp_probe.probe_mcp_stdio(sys.executable, [str(dirty_script)], timeout=3.0)
    assert result["ok"] is False
    assert result["stdout_clean"] is False
    assert "Dirty stdout" in (result["error"] or "")


def test_mcp_probe_enforces_hard_timeout_and_kills_child(tmp_path):
    hang_script = tmp_path / "hang_server.py"
    hang_script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    result = mcp_probe.probe_mcp_stdio(sys.executable, [str(hang_script)], timeout=0.6)
    assert result["ok"] is False
    assert "Timed out" in (result["error"] or "")
    assert result["latency_ms"] < 4000
