"""scripts/compare_model_paths.sh refuses, before any model call, whatever could touch the installation (#217)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "compare_model_paths.sh"
OWN = {"port": 9101, "arcade_url": "http://127.0.0.1:9102"}


def refusal(tmp_path, config, *, where="throwaway", env=None):
    home = tmp_path / "home"
    data = home / ".local/share/devbrain" if where == "earlier-name" else tmp_path / "throwaway"
    data.mkdir(parents=True)
    (data / "config.json").write_text(json.dumps(config))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python = bin_dir / "python3"  # the script's `python3` is the interpreter that has cairnmind
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python.chmod(0o755)
    base = {key: value for key, value in os.environ.items() if not key.startswith(("CAIRNMIND_", "DEVBRAIN_"))}
    environment = {**base, "HOME": str(home), "PATH": f"{bin_dir}:{os.environ['PATH']}", **(env or {})}
    environment = {key: value.replace("DATA", str(data)) for key, value in environment.items()}
    reports = tmp_path / "reports"
    result = subprocess.run(["bash", str(SCRIPT), str(data), str(reports), "http://127.0.0.1:1/v1"],
                            capture_output=True, text=True, env=environment, timeout=60)
    assert not reports.exists()  # refused before anything ran or was written
    return result


@pytest.mark.parametrize("case, config, options, words", [
    ("the earlier-name installation", OWN, {"where": "earlier-name"}, "installation's data directory"),
    ("CAIRNMIND_HOME", OWN, {"env": {"CAIRNMIND_HOME": "DATA"}}, "installation's data directory"),
    ("a port from the environment", OWN, {"env": {"CAIRNMIND_PORT": "8765"}}, "Unset CAIRNMIND_PORT"),
    ("an engine address from the environment", OWN,
     {"env": {"DEVBRAIN_ARCADE_URL": "http://127.0.0.1:2480"}}, "Unset DEVBRAIN_ARCADE_URL"),
    ("the port 8765", {**OWN, "port": 8765}, {}, "its own integer port"),
    ("the port as a string", {**OWN, "port": "9101"}, {}, "its own integer port"),
    ("the engine on 2480", {**OWN, "arcade_url": "http://127.0.0.1:2480"}, {}, "its own integer port"),
    ("a chat role already set", {**OWN, "memory_chat": {"provider": "ollama", "base_url": "http://127.0.0.1:11434"}},
     {}, "default chat role"),
    ("another chat model", {**OWN, "chat_model": "other:1b"}, {}, "default chat role"),
])
def test_the_script_refuses_what_could_touch_the_installation(tmp_path, case, config, options, words):
    result = refusal(tmp_path, config, **options)
    assert result.returncode == 1 and words in result.stderr, (case, result.stderr)
