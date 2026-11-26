"""The Windows jobs of the CI workflow: they exist, fail the workflow on regressions, and are bounded."""
from __future__ import annotations

import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
REQUIRED = ("lint", "fast-tests", "full-tests")
FAST_JOB = "windows-fast-tests"
ARCADE_JOB = "windows-arcade-tests"


def job_block(name: str) -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  [\w-]+:\n|\Z)", text, re.S | re.M)
    assert match, f"job {name} is missing from ci.yml"
    return match.group(1)


def test_windows_fast_job_runs_lint_and_fast_tier():
    block = job_block(FAST_JOB)
    assert re.search(r"^    runs-on: windows-latest$", block, re.M)
    assert "uv sync --locked --extra dev" in block
    assert "uv run ruff check src tests" in block
    assert "pytest" in block and '-m "not arcade and not tmux"' in block


def test_windows_fast_job_fails_workflow_and_is_bounded():
    block = job_block(FAST_JOB)
    assert "continue-on-error" not in block
    job_minutes = int(re.search(r"^    timeout-minutes: (\d+)$", block, re.M).group(1))
    step_minutes = int(re.search(r"^        timeout-minutes: (\d+)$", block, re.M).group(1))
    assert 0 < step_minutes < job_minutes <= 45
    assert "--junitxml" in block
    upload = block[block.index("actions/upload-artifact@"):]
    assert "if: ${{ always() }}" in upload.split("with:")[0]


def test_windows_arcade_job_sets_up_java_21_and_runs_arcade_tier():
    block = job_block(ARCADE_JOB)
    assert re.search(r"^    runs-on: windows-latest$", block, re.M)
    assert "actions/setup-java@" in block and 'java-version: "21"' in block
    assert "install_engine" in block and "CAIRNMIND_ARCADE_DIST" in block
    assert "pytest" in block and '-m "arcade and not tmux"' in block
    assert "continue-on-error" not in block
    job_minutes = int(re.search(r"^    timeout-minutes: (\d+)$", block, re.M).group(1))
    step_minutes = int(re.search(r"^        timeout-minutes: (\d+)$", block, re.M).group(1))
    assert 0 < step_minutes < job_minutes <= 60


def test_required_checks_do_not_depend_on_windows_jobs():
    for name in REQUIRED:
        block = job_block(name)
        assert FAST_JOB not in block and ARCADE_JOB not in block
