"""Server mode of the engine tests (#218): the leave-out list stays true, and the CI job stays beside the required ones.

No server is needed here: the `server-tests` CI job is the one that runs the engine tests against one.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from conftest import LEFT_OUT_ON_A_SERVER, left_out_reason, server_under_test

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github" / "workflows" / "ci.yml").read_text()


@pytest.mark.parametrize("entry", sorted(LEFT_OUT_ON_A_SERVER))
def test_every_left_out_entry_still_exists_and_has_a_reason(entry):
    module, _, name = entry.partition("::")
    source = ROOT / "tests" / f"{module}.py"
    assert source.is_file(), f"{entry}: {source.name} is gone, remove it from LEFT_OUT_ON_A_SERVER"
    assert not name or f"def {name}(" in source.read_text(), f"{entry}: no such test, remove it from LEFT_OUT_ON_A_SERVER"
    reason = LEFT_OUT_ON_A_SERVER[entry]
    assert len(reason.split()) >= 4 and not reason.endswith("."), f"{entry}: one plain sentence, naming the local engine"


def test_a_module_or_one_test_is_left_out_by_name():
    assert left_out_reason("test_store_prune", "test_anything")
    assert left_out_reason("test_cli", "test_cli_evaluation_skip_claude_is_explicit_and_remains_not_run")
    assert left_out_reason("test_cli", "test_another") is None


def test_server_mode_needs_the_address_and_the_password_file_together(monkeypatch, tmp_path):
    for name in ("CAIRNMIND_TEST_ARCADE_SERVER_URL", "DEVBRAIN_TEST_ARCADE_SERVER_URL",
                 "CAIRNMIND_TEST_ARCADE_SERVER_PASSWORD_FILE", "DEVBRAIN_TEST_ARCADE_SERVER_PASSWORD_FILE"):
        monkeypatch.delenv(name, raising=False)
    assert server_under_test() is None
    monkeypatch.setenv("CAIRNMIND_TEST_ARCADE_SERVER_URL", "http://127.0.0.1:1")
    with pytest.raises(pytest.UsageError):
        server_under_test()
    secret = tmp_path / "root-password"
    secret.write_text("a-long-enough-secret\n")
    secret.chmod(0o600)
    monkeypatch.setenv("CAIRNMIND_TEST_ARCADE_SERVER_PASSWORD_FILE", str(secret))
    assert server_under_test() == ("http://127.0.0.1:1", "a-long-enough-secret")


def test_ci_runs_the_server_tests_in_a_job_nothing_waits_for_and_that_needs_no_secret():
    job = WORKFLOW.split("\n  server-tests:\n", 1)[1].split("\n  coverage-badge:\n", 1)[0]
    full = WORKFLOW.split("\n  full-tests:\n", 1)[1].split("\n  coverage-tests:\n", 1)[0]
    assert "server-tests" not in full
    assert "\n    needs:" not in job and "secrets." not in job
    assert "config.ARCADE_IMAGE_DIGEST" in job and "-m arcade" in job
    assert "CAIRNMIND_TEST_ARCADE_SERVER_URL" in job and "CAIRNMIND_TEST_ARCADE_SERVER_PASSWORD_FILE" in job
    assert "docker rm -f" in job and "if: ${{ always() }}" in job
