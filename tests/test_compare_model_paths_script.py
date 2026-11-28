"""scripts/compare_model_paths.sh refuses, before any model call, whatever could touch the installation (#217)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Tests POSIX bash scripts/compare_model_paths.sh")

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


def fake_python(tmp_path, calls, *, fail_v1=False):
    """A python3 that records every `-m cairnmind` call and answers it with a small report; the rest runs for real."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    recall = ('{"scenarios":[{"status":"passed","model_calls":[{"prompt_eval_count":100,"candidate":{"done_reason":"length"}}]}],'
              '"golden_count":1,"elapsed_seconds":2.5}')
    repository = ('{"complete_and_right":4,"asked":5,"complete_and_wrong":1,"questions":[{"model_calls":2}],'
                  '"elapsed_seconds":3.5}')
    python = bin_dir / "python3"
    python.write_text(f"""#!/bin/sh
if [ "$1" = "-m" ]; then
  echo "$*" >> "{calls}"
  case "$*" in
    *"eval run"*) echo '{recall}';;
    *"eval repository"*) echo '{repository}';;
    *openai_compatible*) {'exit 1' if fail_v1 else "echo '{}'"};;
    *) echo '{{}}';;
  esac
  exit 0
fi
exec "{sys.executable}" "$@"
""")
    python.chmod(0o755)
    return bin_dir

def run_to_the_end(tmp_path, **options):
    data = tmp_path / "throwaway"
    data.mkdir()
    (data / "config.json").write_text(json.dumps(OWN))
    calls = tmp_path / "calls.txt"
    bin_dir = fake_python(tmp_path, calls, **options)
    base = {key: value for key, value in os.environ.items() if not key.startswith(("CAIRNMIND_", "DEVBRAIN_"))}
    result = subprocess.run(["bash", str(SCRIPT), str(data), str(tmp_path / "reports"), "http://127.0.0.1:1/v1"],
                            capture_output=True, text=True, timeout=60,
                            env={**base, "HOME": str(tmp_path / "home"), "PATH": f"{bin_dir}:{os.environ['PATH']}"})
    return result, calls.read_text().splitlines() if calls.exists() else []


def test_a_good_directory_runs_both_series_sets_the_role_back_last_and_prints_the_table(tmp_path):
    result, calls = run_to_the_end(tmp_path)
    assert result.returncode == 0, result.stderr
    kinds = [" ".join(call.split()[4:6]) for call in calls]  # after: -m cairnmind --data-dir DIR
    assert kinds.count("eval run") == 6 and kinds.count("eval repository") == 6
    v1 = next(i for i, call in enumerate(calls) if "--provider openai_compatible" in call and "--reasoning-effort none" in call)
    assert kinds[:v1] == ["eval run", "eval repository"] * 3  # the native series first
    assert kinds[v1 + 1:-1] == ["eval run", "eval repository"] * 3  # then the /v1 series
    assert "--provider ollama" in calls[-1] and "--role chat" in calls[-1]
    rows = result.stdout.splitlines()
    assert rows[0].startswith("path | run | passed")
    assert "native | recall 1 | 1 of 1 |  | 1 | 2.5 | 1 | 100" in rows
    assert "v1 | repository 3 | 4 of 5 | 1 | 2 | 3.5 |  | " in rows


def test_a_failed_switch_to_v1_does_not_run_the_restore(tmp_path):
    result, calls = run_to_the_end(tmp_path, fail_v1=True)
    assert result.returncode != 0
    assert sum("eval " in call for call in calls) == 6  # only the native series ran
    assert not any("--provider ollama" in call for call in calls)  # nothing was changed, so nothing is restored