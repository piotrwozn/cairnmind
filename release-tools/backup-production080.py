"""Create one private post-update backup under externally observed ownership.

Prepared release tooling. No execution without --execute, all eleven completed
release proofs, a verified installed 0.8.0 package, and exclusive production
quiescence. Reuses the reviewed production engine/locks without calling the
retry controller, Store.__init__, a service, an inference client, or a job API.
The original 474-tool bundle does not admit this new controller.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time


STATUS = "production_backup_completed_pending_external_watch"
REQUIRED_ARTIFACTS = {"production-backup-admission.json", "production-backup-results.json"}
UPDATES = Path("/home/piotr/.local/state/devbrain-updates")


class Refusal(RuntimeError):
    """Content-free diagnostic codes only."""


def require(condition, code):
    if not condition:
        raise Refusal(code)


def load_helper(name):
    spec = importlib.util.spec_from_file_location("backup_" + name.replace("-", "_"), Path(__file__).with_name(name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_release_input(lib, admission, guard, source, runtime):
    # verify_admission checks only already completed release gates. It never
    # reads/requires a fresh post-update backup, so this is not a recursive gate.
    require(admission.get("operation") == "post_update_backup", "explicit_backup_operation_required")
    lib.verify_admission(admission, guard, source, runtime)
    proof = admission["proofs"]["tooling_guards"]
    root = lib.canonical(proof["artifact_root"], directory=True)
    inputs = lib.document(root / "regression-source.json")
    helpers = inputs.get("guard_sources", {}).get("helper_sha256", {})
    required = {Path(__file__).name, "test_production_backup080_guards.py"}
    require(required <= helpers.keys(), "backup_controller_guards_not_qualified")
    for name in required:
        require(helpers[name] == lib.sha(Path(__file__).with_name(name)), "backup_tool_bytes_not_qualified")
    collection = lib.document(root / "guards-collection.json")
    require(any(node.split("::", 1)[0] == "test_production_backup080_guards.py"
                for node in collection.get("nodeids", [])), "backup_guards_not_collected")


def verify_update(lib, admission, data, runtime):
    path = lib.pinned_reference(admission["update_receipt"], private=True)
    update = lib.document(path, private=True)
    require(update.get("format_version") == 2 and update.get("version") == "0.8.0"
            and update.get("data_dir") == str(data)
            and update.get("payload_sha256") == admission["release_payload_sha256"]
            and update.get("runtime_gate_sha256") == admission["runtime_gate_sha256"]
            and update.get("python_binary_sha256") == runtime["python_binary_sha256"]
            and update.get("target_environment") == str(Path(sys.prefix).resolve()),
            "post_update_installed_receipt_mismatch")
    return path


def new_backup_directory(lib, destination, run_id):
    # Never replace a prior archive/report. A partial directory is retained and
    # blocks the same run ID after any interrupted write or unknown outcome.
    expected = UPDATES / ("post-update-" + run_id) / "database.devbrain.json.gz"
    require(destination == expected, "backup_destination_not_exact_owned_run")
    parent = lib.canonical(UPDATES, directory=True)
    info = parent.stat()
    require(info.st_uid == os.getuid() and not (info.st_mode & 0o022), "backup_parent_not_owned_or_writable")
    destination.parent.mkdir(mode=0o700)
    lib.fsync_directory(parent)
    lib.canonical(destination.parent, directory=True, private=True)
    require(not destination.exists() and not destination.is_symlink(), "fresh_backup_required")
    return destination.parent


def create_verified_backup(lib, store, store_type, destination):
    require(not destination.exists() and not destination.is_symlink(), "fresh_backup_required")
    payload = store.export()
    require(payload.get("format_version") == 5 and payload.get("backend") == "arcadedb", "current_backup_schema_required")
    require(not any(row.get("state") == "running" for row in payload["jobs"]), "production_has_running_jobs")
    # Exact installed Store writer/validator, including vectors and journal.
    # The LockedStore instance bypasses init/ensure_database and owns the outer
    # writer lock. Reads therefore cannot activate the normal queue or scanner.
    store_type._write_backup(destination, payload)
    lib.canonical(destination, private=True)
    validated = store_type._validated_backup(destination)
    require(lib.canonical_export(validated) == lib.canonical_export(payload), "post_update_backup_readback_mismatch")
    require(lib.canonical_export(store.export()) == lib.canonical_export(payload), "production_changed_during_backup")
    return {"backup_sha256": lib.sha(destination), "backup_validation_passed": True}


def owned_backup_lifecycle(engine, produce, quiet, quiescent, report):
    """Cleanup runs independently even if start/export/validation/stop fails."""
    result = None
    try:
        report["production_engine_start_requested"] = True
        engine.start()
        result = produce()
    finally:
        try:
            report.update(engine.stop())
        except Exception:
            report.setdefault("cleanup_errors", []).append("owned_engine_cleanup_failed")
        try:
            report["cleanup_grace_seconds"] = quiet()
            quiescent()
        except Exception:
            report.setdefault("cleanup_errors", []).append("production_quiet_observation_failed")
        report["cleanup_complete"] = (not report.get("cleanup_errors")
                                      and report.get("engine_actual_exit_verified") is True
                                      and report.get("cleanup_grace_seconds", 0) >= 30)
    require(report["cleanup_complete"], "production_backup_cleanup_incomplete")
    return result


def publish_private_report(lib, destination, result, report, admission, update_path, runtime):
    require(report.get("cleanup_complete") is True and report.get("engine_actual_exit_verified") is True
            and report.get("graceful_shutdown_requested") is True, "backup_report_requires_actual_cleanup")
    require(result.get("backup_validation_passed") is True, "backup_report_requires_validated_archive")
    require(lib.sha(destination) == result["backup_sha256"], "backup_changed_before_publication")
    stamp = destination.stat().st_mtime
    require(update_path.stat().st_mtime <= stamp <= time.time() and time.time() - stamp <= 3600,
            "fresh_post_update_backup_required")
    value = {"schema_version": 1, "scope": "devbrain_post_update_backup", "status": "backup_completed",
             "version": "0.8.0", "run_id": report["run_id"], "data_dir": str(lib.PRODUCTION),
             "update_receipt_sha256": lib.sha(update_path), "backup_sha256": result["backup_sha256"],
             "backup_validation_passed": True, "engine_cleanup_complete": True,
             "source_hashes": admission["source_hashes"], "runtime": runtime,
             "external_receipt_required_before_retry_admission": True}
    path = destination.parent / "backup-report.json"
    lib.atomic_private(path, value, create=True)
    return lib.sha(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--release-admission", type=Path)
    parser.add_argument("--release-admission-sha256")
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args(argv)
    if not args.execute:
        print(json.dumps({"status": "prepared_not_run", "production_modified": False,
                          "backup_created": False, "job_execution_allowed": False}))
        return 0
    report = supervision = lib = artifacts = None
    try:
        require(all(getattr(args, key) is not None for key in
                    ("run_id", "artifact_root", "source_root", "data_dir", "release_admission",
                     "release_admission_sha256", "destination")), "explicit_backup_inputs_required")
        require(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.run_id) and re.fullmatch(r"[0-9a-f]{64}", args.release_admission_sha256),
                "backup_identity_invalid")
        require(sys.platform == "linux" and sys.flags.isolated == 1, "isolated_linux_required")
        lib = load_helper("retry-production-queue-080.py")
        supervision, guard = load_helper("supervise-080.py"), load_helper("attest-full-suite-080.py")
        require(supervision.CONTROLLERS.get(Path(__file__).name) == STATUS
                and REQUIRED_ARTIFACTS <= supervision.CONTROLLER_ARTIFACTS.get(Path(__file__).name, set()),
                "backup_controller_not_admitted")
        require(args.data_dir == lib.PRODUCTION, "explicit_production_path_required")
        artifacts = lib.canonical(args.artifact_root, directory=True)
        require(artifacts.is_relative_to(Path("/mnt/c")) and not (artifacts / "controller-report.json").exists(),
                "fresh_external_artifacts_required")
        require(os.environ.get("DEVBRAIN_QUALIFICATION_RUN_ID") == args.run_id, "supervision_run_mismatch")
        proof = lib.document(artifacts / "mapping-proof.json")
        require(proof.get("run_id") == args.run_id, "supervision_scope_mismatch")
        scope = supervision.ScopeOwnership(proof.get("scope_unit"))
        parent = supervision.identity(os.getppid())
        require(parent and scope.owns(parent), "supervisor_parent_required")
        runtime = supervision.qualified_runtime(lib.document(artifacts / "gate.json"))
        require(str(Path(sys.executable).resolve()) == runtime["python_executable"], "interpreter_not_qualified")
        source = lib.canonical(args.source_root, directory=True)
        admission_path = lib.pinned_reference({"path": str(args.release_admission), "sha256": args.release_admission_sha256}, private=True)
        admission = lib.document(admission_path, private=True)
        verify_release_input(lib, admission, guard, source, runtime)
        lib.package_matches_source(source)
        require(admission.get("runtime_gate_sha256") == lib.sha(artifacts / "gate.json"), "admission_gate_mismatch")
        libraries = admission.get("arcade_library_sha256")
        require(isinstance(libraries, dict) and libraries and all(isinstance(k, str) and isinstance(v, str)
                and re.fullmatch(r"[0-9a-f]{64}", v) for k, v in libraries.items()), "admitted_engine_libraries_required")
        # All release/source/runtime proofs precede production settings access.
        data = lib.canonical(args.data_dir, directory=True, private=True)
        update_path = verify_update(lib, admission, data, runtime)
        report = {"schema_version": 1, "scope": "devbrain_post_update_backup_run", "run_id": args.run_id,
                  "status": "running", "runtime": runtime, "python_launcher": sys.executable,
                  "source_hashes": admission["source_hashes"], "runtime_gate_sha256": admission["runtime_gate_sha256"],
                  "controller_exit_verified": False, "cleanup_complete": False, "forced_cleanup": False,
                  "production_engine_start_requested": False, "database_initialization_requested": False,
                  "job_execution_allowed": False, "workload_criteria_passed": False, "errors": []}
        supervision.atomic_json(artifacts / "production-backup-admission.json", {
            "schema_version": 1, "scope": "devbrain_production_backup_admission_binding",
            "admission_sha256": args.release_admission_sha256, "source_final": True,
            "runtime_gate_sha256": admission["runtime_gate_sha256"]})
        supervision.atomic_json(artifacts / "controller-report.json", report)
        lib.prepare_journal_directory(lib.JOURNALS)
        with ExitStack() as stack:
            stack.enter_context(lib.exclusive_file(lib.JOURNALS / "operation.lock"))
            for relative in ("service.lock", "engine.lock", "cockpit/launch.lock", "cockpit/server.lock",
                             "workspace/supervisor.lock", "workspace/ui.lock", "workspace/reconcile.lock"):
                path = data / relative
                if path.parent.exists():
                    stack.enter_context(lib.exclusive_file(path))
            writer_fd = stack.enter_context(lib.exclusive_file(data / "database-writer.lock"))
            lib.require_quiescent(data, {parent["pid"]})
            private_run = new_backup_directory(lib, args.destination, args.run_id)
            from devbrain.config import Settings
            from devbrain.store import Store
            require(not any(key.startswith("DEVBRAIN_ARCADE_") for key in os.environ), "production_database_env_override")
            settings = Settings.load(data, create=False)
            require(settings.arcade_version == "26.11.1", "production_engine_version_not_pinned")
            settings.java_executable, settings.tmux_executable = runtime["java_executable"], runtime["tmux_executable"]
            engine = lib.ProductionEngine(settings, runtime, scope, supervision, private_run, libraries)
            common = load_helper("validate-native-account080.py")
            result = owned_backup_lifecycle(engine,
                lambda: create_verified_backup(lib, lib.existing_store(settings, writer_fd), Store, args.destination),
                lambda: common.quiet_scope(scope, supervision, parent),
                lambda: lib.require_quiescent(data, {parent["pid"]}), report)
            require(guard.source_manifest(source) == admission["source_hashes"], "final_source_changed")
            guard.verify_runtime_files(runtime)
            require(lib.sha(admission_path) == args.release_admission_sha256, "release_admission_changed")
            lib.package_matches_source(source)
            verify_update(lib, admission, data, runtime)
            report["private_backup_report_sha256"] = publish_private_report(
                lib, args.destination, result, report, admission, update_path, runtime)
            report["backup_validation_passed"] = True
            report["workload_criteria_passed"] = True
            report["status"] = STATUS
    except Exception as exc:
        safe = isinstance(exc, Refusal) or (lib is not None and isinstance(exc, lib.Refusal))
        code = str(exc) if safe else "production_backup_precondition_or_execution_failed"
        if report is None:
            print(json.dumps({"status": "refused", "error_code": code, "production_modified": False}))
            return 1
        report["errors"].append(code)
        report["status"] = "failed_backup_not_admitted_no_automatic_replay"
    finally:
        if report is not None:
            supervision.atomic_json(artifacts / "production-backup-results.json", report)
            supervision.atomic_json(artifacts / "controller-report.json", report)
    return 0 if report["status"] == STATUS else 1


if __name__ == "__main__":
    raise SystemExit(main())
