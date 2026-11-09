"""Pure evidence/selection guards; no model, subprocess, browser or engine starts."""
import copy
import importlib.util
import os
from pathlib import Path
import socket
import sys
import tempfile

import pytest


@pytest.fixture
def runner():
    spec = importlib.util.spec_from_file_location("regression080_guards", Path(__file__).with_name("run-regressions-080.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ux_selection_contains_entire_modules_and_latency(runner, tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    names = ["test_api_workspace_e2e.py", "test_api_worker.py", "test_api_panels.py", "test_client_runtime.py", "test_runtime_pane.py",
             "test_terminal_latency080.py", "test_terminal_workspace_e2e.py", "test_terminal_new_module.py", "test_unrelated.py",
             "test_tui.py", "test_memory_tui.py"]
    for name in names:
        (tests / name).write_text("")
    actual = runner.ux_modules(tmp_path)
    assert "tests/test_terminal_new_module.py" in actual and "tests/test_terminal_latency080.py" in actual
    assert "tests/test_unrelated.py" not in actual and not any("::" in item for item in actual)
    assert "tests/test_tui.py" in actual and "tests/test_memory_tui.py" in actual
    assert "tests/test_api_panels.py" in actual
    (tests / "test_api_panels.py").unlink()
    with pytest.raises(runner.common.ValidationError, match="required_ux_module_missing"):
        runner.ux_modules(tmp_path)


def test_cleanup_snapshot_is_recorded_without_turning_failure_into_success(runner):
    error = runner.common.ValidationError("owned_descendant_remains")
    error.owned_descendant_snapshot = {"capture_status": "verified_owned", "processes": [{"pid": 202}]}
    report = {"errors": [], "cleanup_complete": False, "workload_criteria_passed": True}
    runner.record_failure(report, error)
    assert report["errors"] == ["owned_descendant_remains"]
    assert report["cleanup_observation"] == error.owned_descendant_snapshot
    assert report["cleanup_complete"] is False and report["workload_criteria_passed"] is True
    runner.record_failure(report, RuntimeError("PRIVATE_ERROR_DETAILS"))
    assert report["errors"] == ["owned_descendant_remains", "RuntimeError"]


@pytest.mark.parametrize("phase", ["guards", "ux"])
def test_execution_stops_after_first_failure_without_narrowing_full_collection(runner, tmp_path, phase):
    modules = ["tests/test_terminal_latency080.py", "tests/test_workspace_background_inference.py"]
    base_temp = tmp_path / "p"
    collect = runner.worker_pytest_arguments(modules, tmp_path, phase, "collect", tmp_path, base_temp)
    execute = runner.worker_pytest_arguments(modules, tmp_path, phase, "test", tmp_path, base_temp)
    assert collect[:len(modules)] == execute[:len(modules)] == modules
    assert "--collect-only" in collect and "--collect-only" not in execute
    assert not any(arg == "-x" or arg.startswith("--maxfail") for arg in collect)
    assert execute.count("--maxfail=1") == 1
    assert "--junitxml=" + str(tmp_path / f"{phase}.xml") in execute
    assert collect.count("--basetemp=" + str(base_temp)) == execute.count("--basetemp=" + str(base_temp)) == 1
    assert not any(arg in {"-k", "-m", "--lf", "--ff", "--deselect"} or "::" in arg for arg in collect + execute)
    assert ("--noconftest" in execute) == (phase == "guards")


@pytest.mark.skipif(sys.platform != "linux", reason="Linux filesystem AF_UNIX boundary")
def test_linux_socket_108_bytes_fails_and_owned_pytest_basetemp_binds(runner):
    with tempfile.TemporaryDirectory(prefix="r8-", dir="/tmp") as directory:
        root = Path(directory)
        base = runner.worker_basetemp(root)
        assert not base.exists()
        long_path = root / ("x" * (108 - len(os.fsencode(root)) - 1))
        assert len(os.fsencode(long_path)) == 108
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            with pytest.raises(OSError, match="AF_UNIX path too long"):
                listener.bind(str(long_path))
        base.mkdir(mode=0o700)
        fixture = base / "test_real_tmux_panel_restores_0"
        fixture.mkdir(mode=0o700)
        short_path = fixture / "agent.sock"
        assert len(os.fsencode(short_path)) < 108
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(short_path))
            assert short_path.is_socket()
        short_path.unlink()
        assert not short_path.exists() and not long_path.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux owned private temporary root")
@pytest.mark.parametrize("existing", ["file", "directory", "symlink"])
def test_pytest_basetemp_refuses_existing_paths_without_deleting_them(runner, existing):
    with tempfile.TemporaryDirectory(prefix="r8-", dir="/tmp") as directory:
        root = Path(directory)
        base = runner.worker_basetemp(root)
        if existing == "file":
            base.write_text("keep synthetic data")
        elif existing == "directory":
            base.mkdir()
        else:
            base.symlink_to(root / "absent-target")
        with pytest.raises(runner.common.ValidationError, match="fresh_pytest_basetemp_required"):
            runner.worker_basetemp(root)
        assert base.exists() or base.is_symlink()


def test_guard_inventory_runs_all_current_twelve_and_excludes_archives_and_workloads(runner, tmp_path):
    for name in (*runner.GUARD_MODULES, "test_devbrain_update071_guards.py", "test_devbrain_update072_guards.py",
                 "test_devbrain_update080_smoke.py", "test_supervision080_fixture.py"):
        (tmp_path / name).write_text("")
    assert runner.guard_modules(tmp_path) == list(runner.GUARD_MODULES)
    assert len(runner.guard_modules(tmp_path)) == 12
    assert "test_parent_loss080_guards.py" in runner.guard_modules(tmp_path)
    assert "test_devbrain_update071_smoke.py" not in runner.guard_modules(tmp_path)


def test_imported_historical_smoke_is_bound_as_code_only(runner):
    assert "test_devbrain_update071_smoke.py" in runner.GUARD_CODE_DEPENDENCIES
    assert "watch-parent-loss080.py" in runner.GUARD_CODE_DEPENDENCIES
    assert "test_devbrain_update071_smoke.py" not in runner.GUARD_MODULES


def test_guard_inventory_requires_parent_loss_predicates(runner, tmp_path):
    for name in runner.GUARD_MODULES:
        if name != "test_parent_loss080_guards.py":
            (tmp_path / name).write_text("")
    with pytest.raises(runner.common.ValidationError, match="inventory_changed"):
        runner.guard_modules(tmp_path)


def test_unreviewed_extra_guard_is_not_silently_omitted(runner, tmp_path):
    for name in (*runner.GUARD_MODULES, "test_new080_guards.py"):
        (tmp_path / name).write_text("")
    with pytest.raises(runner.common.ValidationError, match="inventory_changed"):
        runner.guard_modules(tmp_path)


@pytest.mark.parametrize("body", [
    '<testsuite tests="0"/>',
    '<testsuite tests="1"><testcase name="a"><failure/></testcase></testsuite>',
    '<testsuite tests="1"><testcase name="a"><error/></testcase></testsuite>',
    '<testsuite tests="1"><testcase name="a"><skipped/></testcase></testsuite>',
    '<testsuite tests="2"><testcase name="a"/></testsuite>',
    '<testsuite tests="2"><testcase name="a"/><testcase name="a"/></testsuite>',
])
def test_empty_partial_or_failed_frontend_junit_never_qualifies(runner, tmp_path, body):
    path = tmp_path / "junit.xml"
    path.write_text(body)
    with pytest.raises(runner.common.ValidationError):
        runner.junit_counts(path)


def test_complete_frontend_junit_counts_real_cases(runner, tmp_path):
    path = tmp_path / "junit.xml"
    path.write_text('<testsuites><testsuite tests="2"><testcase name="a"/><testcase name="b"/></testsuite></testsuites>')
    assert runner.junit_counts(path) == {"passed": 2, "failures": 0, "errors": 0, "skipped": 0}


@pytest.mark.parametrize("problem", ["unavailable", "nineteen", "low_accuracy", "weaker_threshold", "critical_failure", "isolation_failure", "duplicate_case", "unavailable_answer", "unscored_case"])
def test_recall_requires_all_real_cases_and_original_threshold(runner, problem):
    result = {"status": "passed", "golden_count": 20, "attempted": 20, "factual_accuracy": .9,
              "required_accuracy": .9, "critical_facts_passed": True, "deterministic_checks": {"isolated": True},
              "scenarios": [{"id": str(index), "status": "passed" if index < 18 else "failed", "critical": index < 5,
                             "answer": {"status": "complete"}}
                            for index in range(20)]}
    cases = [{"id": str(index), "status": "passed" if index < 18 else "failed", "answer_status": "complete",
              "checkpoint_source": "original_score_answer"} for index in range(20)]
    assert runner.recall_passed(copy.deepcopy(result), copy.deepcopy(cases))
    if problem == "unavailable":
        result["status"] = "not_run"
    elif problem == "nineteen":
        result["attempted"] = 19
    elif problem == "low_accuracy":
        result["factual_accuracy"] = .89
    elif problem == "weaker_threshold":
        result["required_accuracy"] = .8
    elif problem == "critical_failure":
        result["scenarios"][0]["status"] = "failed"
    elif problem == "isolation_failure":
        result["deterministic_checks"]["isolated"] = False
    elif problem == "unavailable_answer":
        result["scenarios"][-1]["answer"]["status"] = "unavailable"
    elif problem == "unscored_case":
        cases[-1]["checkpoint_source"] = "final_report_not_scored"
    else:
        cases[-1]["id"] = cases[0]["id"]
    assert not runner.recall_passed(result, cases)


def test_frontend_manifest_refuses_symlink_to_other_checkout(runner, tmp_path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "package-lock.json").write_text("{}")
    other = tmp_path.parent / "unrelated-synthetic-source"
    other.write_text("synthetic")
    (tmp_path / "main.ts").symlink_to(other)
    with pytest.raises(runner.common.ValidationError, match="symlink"):
        runner.frontend_manifest(tmp_path)
