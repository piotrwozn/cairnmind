"""The macOS job of the CI workflow: it exists, it never blocks, and it cannot hang a runner.

The repository has no YAML parser among its dependencies, so the workflow is read as text, one job
block at a time (a job starts at two spaces of indentation, as test_published_memory_diff expects).
"""
import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
REQUIRED = ("lint", "fast-tests", "full-tests")
JOB = "macos-fast-tests"


def job_block(name):
    text = WORKFLOW.read_text()
    match = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  [\w-]+:\n|\Z)", text, re.S | re.M)
    assert match, f"job {name} is missing from ci.yml"
    return match.group(1)


def test_macos_job_runs_the_fast_tier_on_apple_silicon():
    block = job_block(JOB)
    # macos-14 and macos-15 are arm64 runners; the "-intel" and "-large" images are not.
    assert re.search(r"^    runs-on: macos-(14|15)$", block, re.M)
    assert 'pytest' in block and '-m "not arcade and not tmux"' in block
    assert "uv sync --locked --extra dev" in block


def test_macos_job_is_not_blocking_and_is_bounded():
    block = job_block(JOB)
    assert re.search(r"^    continue-on-error: true$", block, re.M)
    minutes = re.search(r"^    timeout-minutes: (\d+)$", block, re.M)
    assert minutes and 0 < int(minutes.group(1)) <= 45


def test_macos_job_keeps_its_report_even_when_tests_fail():
    block = job_block(JOB)
    assert "--junitxml" in block
    upload = block[block.index("actions/upload-artifact@"):]
    assert "if: ${{ !cancelled() }}" in upload.split("with:")[0]


def test_required_checks_do_not_depend_on_the_macos_job():
    for name in REQUIRED:
        block = job_block(name)
        assert "macos" not in block and JOB not in block
