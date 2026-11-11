"""Scoped fixture permissions, including the real Codex parser without inference."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib
from types import SimpleNamespace

import pytest

from cairnmind.config import Settings
from cairnmind import evaluation


FIXTURE_TOOLS = {"memory", "read_source", "resume_context", "ack_resume"}


def fixture_server():
    return {"command": '/tmp/Python "fixture"/bin/python',
            "args": ["-m", "cairnmind.evaluation", "mcp", "--data-dir", '/tmp/path "with" spaces\\fixture',
                     "--session-id", "logical-fixture-session"],
            "env": {"DEVBRAIN_ARCADE_DATABASE": "cairnmind_eval_fixture"}}


def assert_scoped_configuration(configuration, server):
    parsed = tomllib.loads(configuration)
    assert set(parsed) == {"mcp_servers"}
    assert set(parsed["mcp_servers"]) == {"cairnmind_eval"}
    configured = parsed["mcp_servers"]["cairnmind_eval"]
    for field in ("command", "args", "env"):
        assert configured[field] == server[field]
    assert len(configured["enabled_tools"]) == len(FIXTURE_TOOLS)
    assert set(configured["enabled_tools"]) == FIXTURE_TOOLS
    assert configured["tools"] == {name: {"approval_mode": "approve"} for name in FIXTURE_TOOLS}
    assert configured.get("default_tools_approval_mode") != "approve"
    assert "approval_policy" not in parsed and "sandbox_mode" not in parsed
    return configured


def test_codex_configuration_preapproves_only_named_fixture_tools():
    # Supported by the public rust-v0.166.0 config.schema.json. Transport strings
    # intentionally contain quotes/backslashes so JSON-to-TOML escaping is tested.
    server = fixture_server()
    before = json.dumps(server, sort_keys=True)
    configuration = evaluation._codex_configuration(server)
    assert_scoped_configuration(configuration, server)
    assert json.dumps(server, sort_keys=True) == before


def test_native_codex_keeps_read_only_isolated_process_and_fixture_permissions(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data", create=True)
    settings.arcade_database = "cairnmind_eval_fixture"
    expected = {"task_id": "actual-fixture-task", "version": 2, "state": "needs_test"}
    monkeypatch.setattr(evaluation.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-forward")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "do-not-forward")
    inherited = {name: os.environ.get(name) for name in ("HOME", "CODEX_HOME")}
    commands = []

    def execute(argv, **kwargs):
        commands.append(argv)
        assert kwargs["env"].get("HOME") == inherited["HOME"]
        assert kwargs["env"].get("CODEX_HOME") == inherited["CODEX_HOME"]
        assert "OPENAI_API_KEY" not in kwargs["env"]
        assert "ANTHROPIC_API_KEY" not in kwargs["env"]
        if "exec" not in argv:
            assert argv[1:] == ["login", "status"]
            return SimpleNamespace(returncode=0, stdout="Logged in using ChatGPT", stderr="")
        assert argv[1:3] == ["--no-daemon", "exec"]
        for flag in ("--ignore-user-config", "--strict-config", "--ephemeral", "--json"):
            assert flag in argv and argv.index(flag) > argv.index("exec")
        assert argv[argv.index("--sandbox") + 1] == "read-only"
        assert argv[argv.index("--cd") + 1] == str(tmp_path)
        assert not any("dangerously" in value for value in argv)
        assert "--approve-for-me" not in argv and "--ignore-rules" not in argv
        overrides = [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "-c"]
        parsed_overrides = [tomllib.loads(value) for value in overrides]
        assert any(value.get("approval_policy") == "never" for value in parsed_overrides)
        assert all("ask_for_approval" not in value for value in parsed_overrides)
        configuration = next(value for value in overrides if "mcp_servers" in tomllib.loads(value))
        configured = tomllib.loads(configuration)["mcp_servers"]["cairnmind_eval"]
        server = {field: configured[field] for field in ("command", "args", "env")}
        assert_scoped_configuration(configuration, server)
        assert configured["command"] == sys.executable
        assert configured["env"] == {"CAIRNMIND_ARCADE_DATABASE": settings.arcade_database}
        assert configured["args"][-2:] == ["--session-id", "bound-fixture-session"]
        events = [
            {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(expected)}},
            {"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 5}},
        ]
        return SimpleNamespace(returncode=0, stdout="\n".join(map(json.dumps, events)), stderr="")

    monkeypatch.setattr(evaluation.subprocess, "run", execute)
    result = evaluation._native_client("codex", tmp_path, settings, "fixture-project", expected,
                                       session={"session_id": "bound-fixture-session"})
    assert result["status"] == "passed" and result["answer"] == expected
    assert len(commands) == 2


def test_fixture_session_writes_are_not_mislabeled_read_only(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data", create=True)
    settings.arcade_database = "cairnmind_eval_metadata_only"
    settings.save()
    captured = {}

    class FixtureMCP:
        def __init__(self, name):
            assert name == "cairnmind_eval"

        def tool(self, **kwargs):
            def register(function):
                captured[function.__name__] = kwargs
                return function
            return register

        def run(self, transport):
            assert transport == "stdio"

    monkeypatch.setattr(evaluation, "Store", lambda *args, **kwargs: object())
    monkeypatch.setattr("mcp.server.fastmcp.FastMCP", FixtureMCP)
    evaluation.fixture_mcp(settings.data_dir, "bound-fixture-session")
    assert set(captured) == FIXTURE_TOOLS
    for name in ("resume_context", "ack_resume"):
        annotation = captured[name].get("annotations")
        if isinstance(annotation, dict):
            readonly = annotation.get("readOnlyHint")
        else:
            readonly = getattr(annotation, "readOnlyHint", None)
        assert readonly is not True


def test_installed_codex_accepts_fixture_configuration_without_model_calls(tmp_path):
    binary = shutil.which("codex")
    if not binary:
        pytest.skip("Codex parser is not installed")
    version = subprocess.run([binary, "--version"], capture_output=True, text=True, check=False, timeout=10)
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)", version.stdout)
    if not match or tuple(map(int, match.groups())) < (0, 166, 0):
        pytest.skip("Native parser check requires Codex >=0.166.0")
    configured_home = os.environ.get("CODEX_HOME")
    config = (Path(configured_home) if configured_home else Path.home() / ".codex") / "config.toml"

    def snapshot():
        return (hashlib.sha256(config.read_bytes()).hexdigest(), config.stat().st_mode) if config.is_file() else None

    before = snapshot()
    # --ignore-user-config is exec-only. --help exercises its actual placement
    # and never starts inference. A missing output schema subsequently lets us
    # validate strict exec configuration while stopping before model requests.
    help_result = subprocess.run([binary, "--no-daemon", "exec", "--ignore-user-config", "--strict-config", "--help"],
                                 capture_output=True, text=True, check=False, timeout=10, cwd=tmp_path)
    assert help_result.returncode == 0, help_result.stderr[:500]
    assert "--ignore-user-config" in help_result.stdout and "--strict-config" in help_result.stdout
    server = {"command": sys.executable, "args": ["-c", "pass"],
              "env": {"DEVBRAIN_ARCADE_DATABASE": "cairnmind_eval_parser_only"}}
    configuration = evaluation._codex_configuration(server)
    missing_schema = tmp_path / "missing-output-schema.json"
    assert not missing_schema.exists()
    checked = subprocess.run([
        binary, "--no-daemon", "exec", "--ignore-user-config", "--strict-config",
        "--ephemeral", "--sandbox", "read-only", "--json",
        "--output-schema", str(missing_schema),
        "-c", 'approval_policy="never"', "-c", configuration, "Parser check only",
    ], capture_output=True, text=True, input="", check=False, timeout=20, cwd=tmp_path)
    assert checked.returncode != 0
    assert f"Failed to read output schema file {missing_schema}" in checked.stderr, checked.stderr[:500]
    assert not re.search(r"unknown|unrecognized|unsupported", checked.stderr, re.IGNORECASE)
    # `mcp list` rejects --strict-config, but independently exposes the parsed
    # fixture server without starting it or requesting a model response.
    listed = subprocess.run([binary, "mcp", "list", "--json", "-c", configuration],
                            capture_output=True, text=True, check=False, timeout=20, cwd=tmp_path)
    assert listed.returncode == 0, listed.stderr[:500]
    assert not re.search(r"unknown|unrecognized|unsupported", listed.stderr, re.IGNORECASE)
    fixture = [item for item in json.loads(listed.stdout) if item.get("name") == "cairnmind_eval"]
    assert len(fixture) == 1 and fixture[0]["enabled"] is True
    assert fixture[0]["transport"]["command"] == sys.executable
    assert fixture[0]["transport"]["args"] == ["-c", "pass"]
    assert snapshot() == before
