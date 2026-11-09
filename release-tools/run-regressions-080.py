"""Gated current guards, UX or real recall, never a full-suite claim.

Prepared tooling only. Run through run-supervised-080.ps1 after a passed runtime
gate; each phase needs a new artifact root and an independent external receipt.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
from uuid import uuid4
import xml.etree.ElementTree as ET


def helper(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


common = helper("validate-native-account080")
require = common.require
GUARD_MODULES = (
    "test_account080_guards.py", "test_devbrain_update080_guards.py", "test_full_suite_080_guards.py",
    "test_interactive_ux080_guards.py", "test_queue_replay080_guards.py", "test_regressions080_guards.py",
    "test_scope_080_guards.py", "test_stability_080_guards.py", "test_parent_loss080_guards.py",
    "test_production_queue080_guards.py",
    "test_production_backup080_guards.py",
    "test_production_deploy080_guards.py",
)
GUARD_CODE_DEPENDENCIES = ("watch-parent-loss080.py", "run-parent-loss080.ps1",
                           "test_devbrain_update071_smoke.py")


def guard_modules(directory):
    historical = {"test_devbrain_update071_guards.py", "test_devbrain_update072_guards.py"}
    available = {path.name for path in directory.glob("test_*guards.py")} - historical
    require(available == set(GUARD_MODULES), "current_guard_inventory_changed")
    require(all((directory / name).is_file() and not (directory / name).is_symlink() for name in GUARD_MODULES),
            "guard_module_missing_or_linked")
    return list(GUARD_MODULES)


def guard_manifest(directory, supervision, updater):
    names = set(guard_modules(directory)) | set(supervision.CONTROLLERS) | set(GUARD_CODE_DEPENDENCIES)
    names.update({"attest-full-suite-080.py", "validate-stability-080.py", "supervise-080.py",
                  "run-supervised-080.ps1", "qualification-power-080.ps1"})
    for dependencies in supervision.CONTROLLER_DEPENDENCIES.values():
        names.update(dependencies)
    require(updater is not None and updater.is_absolute() and updater.resolve() == updater
            and updater.name == "devbrain-update-0.8.0.py" and updater.is_file(), "explicit_candidate080_required")
    result = {}
    for name in sorted(names):
        path = directory / name
        require(path.is_file() and not path.is_symlink(), "guard_dependency_missing_or_linked")
        result[name] = common.sha(path)
    return {"helper_sha256": result, "updater": str(updater), "updater_sha256": common.sha(updater)}


def ux_modules(source):
    paths = set()
    for pattern in ("test_terminal*.py", "test_workspace*.py", "test_cockpit*.py", "test_graph*.py",
                    "test_tui*.py", "test_*tui*.py"):
        paths.update((source / "tests").glob(pattern))
    for name in ("test_api_workspace_e2e.py", "test_api_worker.py", "test_api_panels.py", "test_client_runtime.py", "test_runtime_pane.py"):
        path = source / "tests" / name
        require(path.is_file(), "required_ux_module_missing")
        paths.add(path)
    result = sorted(path.relative_to(source).as_posix() for path in paths)
    require("tests/test_terminal_latency080.py" in result and "tests/test_terminal_workspace_e2e.py" in result,
            "complete_ux_and_latency_modules_required")
    return result


def worker_basetemp(temporary_root):
    temporary_root = common.private_directory(temporary_root)
    require(temporary_root.parent == Path("/tmp") and temporary_root.name.startswith("r8-"),
            "owned_pytest_temporary_root_required")
    base = temporary_root / "p"
    # pytest may delete basetemp; admit only a nonexistent child of the private
    # temporary root freshly created by this controller, never a caller's path.
    require(not base.exists() and not base.is_symlink(), "fresh_pytest_basetemp_required")
    return base


def worker_pytest_arguments(modules, root, phase, worker, directory, base_temp):
    require(phase in {"guards", "ux"} and worker in {"collect", "test"}, "invalid_pytest_worker_phase")
    argv = [*modules, "-q", "-o", "cache_dir=" + str(root / f"{phase}-pytest-cache"),
            "--basetemp=" + str(base_temp)]
    if phase == "guards":
        argv += ["--noconftest", "--rootdir", str(directory)]
    if worker == "collect":
        argv += ["--collect-only"]
    else:
        # Preserve the first failure's traceback, JUnit and fixture teardown.
        # Collection still covers every selected module and is compared below.
        argv += ["--maxfail=1", "--junitxml=" + str(root / f"{phase}.xml")]
    return argv


def record_failure(report, error):
    report["errors"].append(str(error) if isinstance(error, common.ValidationError) else type(error).__name__)
    if isinstance(error, common.ValidationError) and str(error) == "owned_descendant_remains":
        snapshot = getattr(error, "owned_descendant_snapshot", None)
        if isinstance(snapshot, dict):
            report["cleanup_observation"] = snapshot


class ModuleCollection:
    def __init__(self, root, run_id, collect, modules, supervision, phase):
        self.root, self.run_id, self.collect, self.modules, self.supervision = root, run_id, collect, modules, supervision
        self.phase = phase
        self.errors, self.deselected = [], []

    def pytest_collection_modifyitems(self, session, config, items):
        for item in items:
            require(not any(key == "nodeid" for key, _ in item.user_properties), "conflicting_test_nodeid")
            item.user_properties.append(("nodeid", item.nodeid))

    def pytest_deselected(self, items):
        self.deselected.extend(item.nodeid for item in items)

    def pytest_collectreport(self, report):
        if report.failed:
            self.errors.append(report.nodeid)

    def pytest_collection_finish(self, session):
        nodeids = [item.nodeid for item in session.items]
        require(nodeids and not self.errors and not self.deselected, "ux_collection_incomplete")
        require(set(item.split("::", 1)[0] for item in nodeids) == set(self.modules), "ux_module_missing_from_collection")
        if self.collect:
            self.supervision.atomic_json(self.root / f"{self.phase}-collection.json", {
                "schema_version": 1, "scope": f"devbrain_complete_{self.phase}_module_collection", "run_id": self.run_id,
                "modules": self.modules, "nodeids": nodeids, "collection_errors": self.errors, "deselected": self.deselected})
        else:
            expected = self.supervision.read_json(self.root / f"{self.phase}-collection.json")
            require(expected["nodeids"] == nodeids and expected["modules"] == self.modules, "ux_execution_collection_changed")


def process_step(argv, cwd, env, output, check, *, timeout, accepted_codes=(0,)):
    check()
    with output.open("xb") as log:
        child = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        code = child.wait(timeout=timeout)
    require(code in accepted_codes, "regression_subprocess_nonzero")
    check()
    return {"returncode": code, "actual_exit_verified": True}


def module_phase(args, root, source, env, check, supervision, guard):
    modules = ux_modules(source) if args.phase == "ux" else guard_modules(Path(__file__).parent)
    cwd = source if args.phase == "ux" else Path(__file__).parent
    command = [sys.executable, "-I", "-B", str(Path(__file__).resolve()), *sys.argv[1:]]
    results = {}
    for phase in ("collect", "test"):
        results[phase] = process_step([*command, "--worker", phase], cwd, env, root / f"{args.phase}-{phase}.log", check,
                                     timeout=120 if phase == "collect" else 2400)
    collection = supervision.read_json(root / f"{args.phase}-collection.json")
    counts = guard.junit_results(root / f"{args.phase}.xml", collection["nodeids"], {})
    latency = []
    for case in ET.parse(root / f"{args.phase}.xml").getroot().iter("testcase"):
        properties = {item.get("name"): item.get("value") for item in case.findall("./properties/property")}
        if "test_terminal_latency080.py::" in properties.get("nodeid", ""):
            latency.append(properties)
    require(args.phase != "ux" or len(latency) >= 2, "both_latency_sizes_required")
    return {"status": "passed", "modules": modules, "individual_test_selectors": [], "normal_conftest": args.phase == "ux",
            "historical_guard_modules_excluded": args.phase == "guards", "skips_permitted": 0,
            "counts": counts, "processes": results, "latency_properties": latency, "human_ux_tested": False}


def recall_passed(result, cases):
    return (bool(result) and result.get("status") == "passed" and result.get("golden_count") == result.get("attempted") == 20
            and len(result.get("scenarios", [])) == 20 and len(cases) == 20 and len({row["id"] for row in cases}) == 20
            and {row["id"] for row in result["scenarios"]} == {row["id"] for row in cases}
            and all(row.get("status") in {"passed", "failed"}
                    and row.get("answer", {}).get("status") in {"complete", "partial"} for row in result["scenarios"])
            and all(row.get("status") in {"passed", "failed"} and row.get("answer_status") in {"complete", "partial"}
                    and row.get("checkpoint_source") == "original_score_answer" for row in cases)
            and result.get("factual_accuracy", 0) >= .9 and result.get("required_accuracy") == .9
            and result.get("critical_facts_passed") is True
            and all(row["status"] == "passed" for row in result["scenarios"] if row["critical"])
            and bool(result.get("deterministic_checks")) and all(result["deterministic_checks"].values()))


def recall_phase(args, root, work, settings, supervision):
    from devbrain import evaluation
    progress_helper = helper("validate-local-recall")
    directory = root / "recall"
    directory.mkdir()
    progress = progress_helper.Progress(directory, {"devbrain_version": "0.8.0", "clients": False,
        "real_ollama": True, "scoring_unchanged": True, "model": settings.chat_model,
        "embedding_model": settings.embedding_model, "num_ctx": settings.num_ctx,
        "project_content_in_checkpoints": False})
    original_score, original_tempdir = evaluation.score_answer, tempfile.tempdir
    monitor = progress_helper.ResourceMonitor(directory / "resources.jsonl")
    result = None
    try:
        tempfile.tempdir = os.environ["TMPDIR"]
        evaluation.score_answer = progress.wrap_score(original_score)
        monitor.thread.start()
        # No injected model/answer/scorer result; the wrapper returns the exact
        # existing score_answer result once for each actual completed case.
        result = evaluation.run_evaluation(settings, clients=False, report_dir=directory)
        completed = {row["id"] for row in progress.cases}
        for row in result["scenarios"]:
            if row["id"] not in completed:
                progress.record_case({"id": row["id"], "status": row["status"], "critical": row["critical"],
                                      "score_passed": None, "checkpoint_source": "final_report_not_scored"})
        shutil.copyfile(result["report_path"], root / "recall-results.json")
        progress.state.update({key: result[key] for key in ("status", "attempted", "factual_accuracy", "required_accuracy",
                                                          "critical_facts_passed", "deterministic_checks")})
    except Exception as exc:
        progress.state.update(status="failed", exception_type=type(exc).__name__)
        raise
    finally:
        evaluation.score_answer = original_score
        tempfile.tempdir = original_tempdir
        progress.state["monitoring"] = monitor.finish() if monitor.thread.ident is not None else {"stopped": True, "samples": 0}
        progress.state["finished_at"] = progress_helper.utc_now()
        progress_helper.durable_json(directory / "checkpoint.json", progress.state)
    require(recall_passed(result, progress.cases), "real_recall_threshold_not_met")
    require(progress.state["completed_cases"] == 20 and progress.state["monitoring"].get("stopped")
            and monitor.samples > 0 and not monitor.errors,
            "recall_resource_monitor_incomplete")
    return {"status": "passed", "attempted": 20, "factual_accuracy": result["factual_accuracy"],
            "critical_facts_passed": True, "scoring_unchanged": True, "real_ollama": True,
            "monitoring": progress.state["monitoring"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--phase", choices=("guards", "ux", "recall"), required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--arcade-dist", type=Path)
    parser.add_argument("--updater", type=Path)
    parser.add_argument("--chat-model")
    parser.add_argument("--embedding-model")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--num-ctx", type=int, default=16384)
    parser.add_argument("--worker", choices=("collect", "test"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({"status": "not_run", "execute": False, "phase": args.phase}))
        return 0
    supervision, guard = helper("supervise-080"), helper("attest-full-suite-080")
    root, source = args.artifact_root.resolve(strict=True), args.source_root.resolve(strict=True)
    require(root == args.artifact_root and root.is_relative_to(Path("/mnt/c")), "mapped_artifact_root_required")
    runtime = supervision.qualified_runtime(supervision.read_json(root / "gate.json"))
    require(sys.flags.isolated == 1 and sys.prefix != sys.base_prefix and
            os.environ.get("DEVBRAIN_QUALIFICATION_RUN_ID") == args.run_id, "isolated_supervised_venv_required")
    origin = importlib.util.find_spec("devbrain")
    require(origin and Path(origin.origin).resolve() == source / "src/devbrain/__init__.py"
            and importlib.metadata.version("devbrain") == "0.8.0", "frozen_installed_devbrain080_required")
    proof = supervision.read_json(root / "mapping-proof.json")
    require(proof.get("run_id") == args.run_id, "scope_run_id_mismatch")
    scope = supervision.ScopeOwnership(proof.get("scope_unit"))
    parent = supervision.identity(os.getppid())
    require(parent and scope.owns(parent), "owned_controller_parent_required")
    source_hashes, gate_hash = guard.source_manifest(source), common.sha(root / "gate.json")
    frozen_guards = guard_manifest(Path(__file__).parent, supervision, args.updater) if args.phase == "guards" else None
    def check():
        require(supervision.qualified_runtime(supervision.read_json(root / "gate.json")) == runtime
                and common.sha(root / "gate.json") == gate_hash, "runtime_or_gate_changed")
        require(guard.source_manifest(source) == source_hashes, "frozen_application_changed")
        if frozen_guards is not None:
            require(guard_manifest(Path(__file__).parent, supervision, args.updater) == frozen_guards, "frozen_guard_helpers_or_updater_changed")
        require(supervision.same_process(parent) and scope.owns(parent), "controller_parent_changed")
    if args.worker:
        require(args.phase in {"guards", "ux"}, "worker_is_only_for_explicit_modules")
        frozen = supervision.read_json(root / "regression-source.json")
        require(frozen["source_hashes"] == source_hashes and frozen["run_id"] == args.run_id, "ux_source_changed")
        require(frozen.get("guard_sources") == frozen_guards, "guard_helpers_changed_before_worker")
        import pytest
        modules = ux_modules(source) if args.phase == "ux" else guard_modules(Path(__file__).parent)
        plugin = ModuleCollection(root, args.run_id, args.worker == "collect", modules, supervision, args.phase)
        directory = source if args.phase == "ux" else Path(__file__).parent
        os.chdir(directory)
        base_temp = worker_basetemp(Path(os.environ["TMPDIR"]))
        argv = worker_pytest_arguments(modules, root, args.phase, args.worker, directory, base_temp)
        if args.phase == "guards":
            require(os.environ.get("DEVBRAIN_TEST_UPDATER_PATH") == str(args.updater), "guard_updater_environment_mismatch")
        check()
        code = int(pytest.main(argv, plugins=[plugin]))
        check()
        return code
    require(not (root / "controller-report.json").exists(), "fresh_artifact_root_required")
    report = {"schema_version": 1, "scope": "devbrain_targeted_regressions", "phase": args.phase, "run_id": args.run_id,
              "runtime": runtime, "python_launcher": sys.executable, "runtime_gate_sha256": gate_hash,
              "source_hashes": source_hashes, "status": "running", "cleanup_complete": False,
              "guard_sources": frozen_guards,
              "controller_exit_verified": False, "full_suite_pass": False, "eight_hour_qualification": False,
              "workload_criteria_passed": False, "forced_cleanup": False, "errors": []}
    supervision.atomic_json(root / "controller-report.json", report)
    supervision.atomic_json(root / "regression-source.json", {"run_id": args.run_id, "phase": args.phase,
                                                              "source_hashes": source_hashes, "runtime": runtime,
                                                              "guard_sources": frozen_guards})
    previous, old_umask, engine = dict(os.environ), os.umask(0o077), None
    previous_tempdir = tempfile.tempdir
    try:
        cache = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".cache"
        private_parent = common.private_directory(args.private_root.resolve(strict=True))
        require(private_parent == args.private_root and private_parent.is_relative_to(cache), "private_linux_cache_required")
        work = common.private_directory(private_parent / (args.phase + "-" + uuid4().hex), create=True)
        for name in ("home", "codex-auth", "claude-auth", "tmp", "bin", "engine-home"):
            common.private_directory(work / name, create=True)
        report["private_work_root"] = str(work)
        env = common.clean_environment(work, runtime, args.run_id)
        short_tmp = common.private_directory(Path("/tmp") / ("r8-" + uuid4().hex), create=True)
        require(len(os.fsencode(short_tmp / f"devbrain-native-{os.getuid()}-{'0' * 16}/tmux.sock")) <= 100,
                "temporary_socket_path_too_long")
        env["TMPDIR"] = str(short_tmp)
        tempfile.tempdir = str(short_tmp)
        report["temporary_root"] = str(short_tmp)
        os.environ.clear()
        os.environ.update(env)
        settings = None
        if args.phase in {"ux", "recall"}:
            require(args.arcade_dist is not None, "pinned_arcade_distribution_required")
            from devbrain.config import Settings
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            settings = Settings(data_dir=work / "engine-home", arcade_url=f"http://127.0.0.1:{port}",
                                token=secrets.token_urlsafe(32), arcade_password=secrets.token_urlsafe(32),
                                arcade_database="devbrain_regression_" + uuid4().hex,
                                java_executable=runtime["java_executable"], tmux_executable=runtime["tmux_executable"])
            if args.phase == "recall":
                require(args.chat_model and args.embedding_model and args.num_ctx > 0, "explicit_recall_models_required")
                require(re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):[0-9]{1,5}", args.ollama_url), "local_ollama_required")
                settings.chat_model, settings.embedding_model = args.chat_model, args.embedding_model
                settings.ollama_url, settings.num_ctx = args.ollama_url, args.num_ctx
            settings.save()
            engine = common.OwnedEngine(settings, args.arcade_dist.resolve(strict=True), runtime, scope, supervision, work)
            check()
            engine.start()
        if args.phase in {"guards", "ux"}:
            if args.phase == "ux":
                env.update(DEVBRAIN_TEST_ARCADE_HOME=str(settings.data_dir), DEVBRAIN_ARCADE_DIST=str(args.arcade_dist.resolve()),
                           DEVBRAIN_TEST_TMUX=runtime["tmux_executable"])
            else:
                env["DEVBRAIN_TEST_UPDATER_PATH"] = str(args.updater)
            result = module_phase(args, root, source, env, check, supervision, guard)
        else:
            check()
            result = recall_phase(args, root, work, settings, supervision)
        check()
        supervision.atomic_json(root / "regressions-results.json", {"phase": args.phase, "run_id": args.run_id, **result})
        report["workload_criteria_passed"] = result["status"] == "passed"
    except Exception as exc:
        record_failure(report, exc)
    finally:
        try:
            stopped = engine.stop() if engine is not None else True
            report["engine"] = engine.report if engine is not None else {"started": False, "cleanup_complete": True}
            report["forced_cleanup"] = bool(report["engine"].get("forced_cleanup"))
            require(stopped, "regression_engine_cleanup_failed")
            report["cleanup_grace_seconds"] = common.quiet_scope(scope, supervision, parent)
            report["cleanup_complete"] = True
            check()
        except Exception as exc:
            record_failure(report, exc)
        os.environ.clear()
        os.environ.update(previous)
        tempfile.tempdir = previous_tempdir
        os.umask(old_umask)
        report["status"] = ("regressions_completed_pending_external_watch" if report["workload_criteria_passed"]
                            and report["cleanup_complete"] and not report["forced_cleanup"] and not report["errors"] else "failed")
        supervision.atomic_json(root / "controller-report.json", report)
    return 0 if report["status"] == "regressions_completed_pending_external_watch" else 1


if __name__ == "__main__":
    raise SystemExit(main())
