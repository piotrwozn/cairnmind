"""Gated current guards, UX, frontend or real recall, never a full-suite claim.

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
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
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


def frontend_manifest(root):
    result = {}
    for directory, subdirs, names in os.walk(root):
        subdirs[:] = sorted(name for name in subdirs if name not in {"node_modules", ".git", "dist", "test-results", "playwright-report"})
        for name in [*subdirs, *names]:
            require(not (Path(directory) / name).is_symlink(), "frontend_source_symlink")
        for name in sorted(names):
            if name.endswith(".tsbuildinfo"):
                continue
            path = Path(directory) / name
            require(path.is_file(), "frontend_source_not_regular")
            result[path.relative_to(root).as_posix()] = common.sha(path)
    require("package.json" in result and "package-lock.json" in result, "frontend_lock_required")
    return result


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


def junit_counts(path):
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    require(cases and not any(list(root.iter(tag)) for tag in ("failure", "error", "skipped")), "frontend_junit_not_all_passed")
    identities = [(case.get("classname"), case.get("name")) for case in cases]
    require(len(identities) == len(set(identities)), "frontend_junit_duplicate_cases")
    for suite in root.iter("testsuite"):
        for name in ("failures", "errors", "skipped"):
            require(int(suite.get(name, "0")) == 0, "frontend_junit_failed_totals")
        if "tests" in suite.attrib:
            require(int(suite.attrib["tests"]) == len(list(suite.iter("testcase"))), "frontend_junit_incomplete_totals")
    return {"passed": len(cases), "failures": 0, "errors": 0, "skipped": 0}


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


def executable(path, *, elf=False):
    require(path is not None and path.is_absolute(), "absolute_frontend_runtime_required")
    path = path.resolve(strict=True)
    require(path.is_file(), "frontend_runtime_missing")
    if elf:
        with path.open("rb") as stream:
            require(stream.read(4) == b"\x7fELF" and os.access(path, os.X_OK), "actual_elf_runtime_required")
    return path


def frontend_phase(args, root, source, work, env, check, scope, supervision):
    frontend = source / "frontend"
    manifest = frontend_manifest(frontend)
    scratch = common.private_directory(work / "frontend", create=True)
    for relative in manifest:
        target = scratch / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(frontend / relative, target)
    node, npm, chromium = executable(args.node, elf=True), executable(args.npm_cli), executable(args.chromium, elf=True)
    binaries = {"node": {"path": str(node), "sha256": common.sha(node)},
                "npm": {"path": str(npm), "sha256": common.sha(npm), "hash_scope": "cli_entrypoint"},
                "chromium": {"path": str(chromium), "sha256": common.sha(chromium)}}
    env = {**env, "PATH": str(node.parent) + os.pathsep + env["PATH"], "CI": "1",
           "CHROMIUM_PATH": str(chromium), "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1",
           "npm_config_cache": str(work / "npm-cache"), "npm_config_userconfig": str(work / "empty-npmrc")}
    (work / "empty-npmrc").write_text("")
    def bound_check():
        check()
        require(frontend_manifest(frontend) == manifest, "frozen_frontend_changed")
        require(all((scratch / path).is_file() and not (scratch / path).is_symlink()
                    and common.sha(scratch / path) == digest for path, digest in manifest.items()),
                "frontend_working_copy_changed")
        require(all(common.sha(Path(row["path"])) == row["sha256"] for row in binaries.values()), "frontend_runtime_changed")
    for name, argv in (("node", [str(node), "--version"]), ("npm", [str(node), str(npm), "--version"]),
                       ("chromium", [str(chromium), "--version"])):
        bound_check()
        result = subprocess.run(argv, cwd=scratch, env=env, capture_output=True, text=True, timeout=15)
        require(result.returncode == 0, "frontend_version_probe_failed")
        binaries[name]["version"] = result.stdout.strip()[:200]
    version = re.match(r"v?(\d+)\.", binaries["node"]["version"])
    require(version and (int(version[1]) in (20, 22) or int(version[1]) >= 24), "unsupported_node_version")
    steps = {}
    npm_command = [str(node), str(npm)]
    steps["install"] = process_step([*npm_command, "ci", "--no-audit", "--fund=false"], scratch, env,
                                    root / "frontend-install.log", bound_check, timeout=600)
    require(common.sha(scratch / "package-lock.json") == manifest["package-lock.json"], "npm_lock_changed")
    # Override only the scratch build/server: no write to frozen web_static and
    # no fallback proxy to a production service if a browser mock is missing.
    config = scratch / "vite.validation.config.ts"
    config.write_text("import {mergeConfig} from 'vite'; import original from './vite.config';\n"
                      "export default mergeConfig(original,{server:{proxy:{'/api':'http://127.0.0.1:1',"
                      "'/auth':'http://127.0.0.1:1'}},build:{outDir:" + json.dumps(str(scratch / "dist")) + "}});\n")
    steps["unit"] = process_step([*npm_command, "test", "--", "--reporter=junit", "--outputFile=" + str(root / "frontend-unit.xml")],
                                 scratch, env, root / "frontend-unit.log", bound_check, timeout=300)
    unit = junit_counts(root / "frontend-unit.xml")
    steps["build"] = process_step([*npm_command, "run", "build", "--", "--config", str(config), "--outDir", str(scratch / "dist")],
                                  scratch, env, root / "frontend-build.log", bound_check, timeout=300)
    dist = scratch / "dist"
    require((dist / "index.html").is_file(), "frontend_build_missing")
    require(all(not path.is_symlink() and path.resolve().is_relative_to(dist) for path in dist.rglob("*")), "frontend_build_symlink")
    built = {path.relative_to(dist).as_posix(): common.sha(path) for path in sorted(dist.rglob("*")) if path.is_file()}
    supervision.atomic_json(root / "frontend-build-manifest.json", built)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    browser_config = scratch / "playwright.validation.config.ts"
    browser_config.write_text("import original from './playwright.config';\nexport default {...original,forbidOnly:true,"
        "webServer:undefined,retries:0,workers:1,use:{...original.use,launchOptions:{...original.use?.launchOptions,"
        "args:[...(original.use?.launchOptions?.args??[]),'--disable-gpu']},baseURL:"
        + json.dumps(f"http://127.0.0.1:{port}") + "}};\n")
    handle, child, row = None, None, None
    try:
        bound_check()
        with (root / "frontend-server.log").open("xb") as log:
            child = subprocess.Popen([str(node), str(scratch / "node_modules/vite/bin/vite.js"), "--config", str(config),
                                      "--host", "127.0.0.1", "--port", str(port), "--strictPort"], cwd=scratch, env=env,
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            row = supervision.identity(child.pid)
            require(row and row["executable"] == str(node) and scope.owns(row), "frontend_server_not_owned")
            handle = os.pidfd_open(child.pid)
            require(scope.owns(row), "frontend_server_identity_changed")
            ready, deadline = False, time.monotonic() + 30
            while child.poll() is None and time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}", timeout=1) as response:
                        ready = response.status == 200
                    if ready:
                        break
                except (OSError, urllib.error.URLError):
                    time.sleep(.2)
            require(ready, "frontend_server_not_ready")
            browser_env = {**env, "PLAYWRIGHT_JUNIT_OUTPUT_FILE": str(root / "frontend-browser.xml")}
            steps["browser"] = process_step([str(node), str(scratch / "node_modules/@playwright/test/cli.js"), "test",
                                             "--config", str(browser_config), "--reporter=junit"], scratch, browser_env,
                                            root / "frontend-browser.log", bound_check, timeout=300)
            browser = junit_counts(root / "frontend-browser.xml")
    finally:
        if child is not None:
            try:
                require(child.poll() is None and handle is not None and scope.owns(row), "frontend_server_exit_or_identity_changed")
                signal.pidfd_send_signal(handle, signal.SIGTERM)
                code = child.wait(timeout=10)
                require(code in (0, 143, -15), "frontend_server_shutdown_failed")
                steps["server_shutdown"] = {"actual_exit_verified": True, "returncode": code}
            finally:
                if handle is not None:
                    os.close(handle)
    for suffix, extra in (("all", []), ("runtime", ["--omit=dev"])):
        steps["audit_" + suffix] = process_step([*npm_command, "audit", "--json", *extra], scratch, env,
                                               root / f"frontend-audit-{suffix}.json", bound_check, timeout=120,
                                               accepted_codes=(0, 1))
    # A vulnerability exit from the first audit must not hide the independent
    # runtime-only result; neither nonzero result grants a passing phase.
    require(all(steps["audit_" + suffix]["returncode"] == 0 for suffix in ("all", "runtime")), "frontend_audit_not_clean")
    bound_check()
    return {"status": "passed", "runtime": binaries, "source_hashes": manifest, "steps": steps,
            "unit": unit, "browser": browser, "synthetic_browser_mocks": True, "filters": [],
            "build_directory": str(dist), "frozen_web_static_modified": False}


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
    parser.add_argument("--phase", choices=("guards", "ux", "frontend", "recall"), required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--arcade-dist", type=Path)
    parser.add_argument("--updater", type=Path)
    parser.add_argument("--node", type=Path)
    parser.add_argument("--npm-cli", type=Path)
    parser.add_argument("--chromium", type=Path)
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
    frozen_frontend = frontend_manifest(source / "frontend") if args.phase == "frontend" else None
    frozen_guards = guard_manifest(Path(__file__).parent, supervision, args.updater) if args.phase == "guards" else None
    def check():
        require(supervision.qualified_runtime(supervision.read_json(root / "gate.json")) == runtime
                and common.sha(root / "gate.json") == gate_hash, "runtime_or_gate_changed")
        require(guard.source_manifest(source) == source_hashes, "frozen_application_changed")
        if frozen_frontend is not None:
            require(frontend_manifest(source / "frontend") == frozen_frontend, "frozen_frontend_changed")
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
              "frontend_source_hashes": frozen_frontend,
              "guard_sources": frozen_guards,
              "controller_exit_verified": False, "full_suite_pass": False, "eight_hour_qualification": False,
              "workload_criteria_passed": False, "forced_cleanup": False, "errors": []}
    supervision.atomic_json(root / "controller-report.json", report)
    supervision.atomic_json(root / "regression-source.json", {"run_id": args.run_id, "phase": args.phase,
                                                              "source_hashes": source_hashes, "runtime": runtime,
                                                              "frontend_source_hashes": frozen_frontend,
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
        elif args.phase == "frontend":
            result = frontend_phase(args, root, source, work, env, check, scope, supervision)
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
