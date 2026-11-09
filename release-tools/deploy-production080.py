"""One explicitly admitted production deployment; inert without --execute.

The controller starts a NEW, retained-pidfd JVM before the unchanged updater.
Legacy backup and installation use that exact owned server. No old PID cache or
engine-stop command is trusted. No service/queue worker, login or model turn is
requested; the unchanged installer's doctor still performs local readiness and
native CLI metadata probes. A permanent journal forbids automatic repetition.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import time


STATUS = "production_deployment_completed_pending_external_watch"
REQUIRED_ARTIFACTS = {"production-deployment-admission.json", "production-deployment-results.json"}
INSTALL_SOURCE = Path("/home/piotr/src/devbrain")
UPDATES = Path("/home/piotr/.local/state/devbrain-updates")
PROTECTED = ("models.lock.json", "cockpit/preferences.json", "workspace/state.json")


class Refusal(RuntimeError):
    """Only fixed, content-free error codes may be published."""


def require(condition, code):
    if not condition:
        raise Refusal(code)


def load_helper(name):
    return load_module(Path(__file__).with_name(name))


def load_module(path):
    spec = importlib.util.spec_from_file_location("deployment_" + path.stem.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_release(lib, admission, guard, source, runtime, updater_path, backup_python):
    require(admission.get("operation") == "production_deployment", "explicit_deployment_operation_required")
    lib.verify_admission(admission, guard, source, runtime)
    proof_root = lib.canonical(admission["proofs"]["tooling_guards"]["artifact_root"], directory=True)
    helpers = lib.document(proof_root / "regression-source.json")["guard_sources"]["helper_sha256"]
    for name in (Path(__file__).name, "test_production_deploy080_guards.py"):
        require(helpers.get(name) == lib.sha(Path(__file__).with_name(name)), "deployment_tools_not_qualified")
    collection = lib.document(proof_root / "guards-collection.json")["nodeids"]
    require(any(node.split("::", 1)[0] == "test_production_deploy080_guards.py" for node in collection),
            "deployment_guards_not_collected")
    updater = lib.pinned_reference(admission["updater"])
    require(updater == updater_path and updater.name == "devbrain-update-0.8.0.py", "exact_updater_required")
    smoke_root = lib.canonical(admission["proofs"]["upgrade_smoke"]["artifact_root"], directory=True)
    smoke = lib.document(smoke_root / "controller-report.json")
    require(smoke.get("updater_sha256") == lib.sha(updater)
            and smoke.get("release_zip_sha256") == admission["release_zip_sha256"], "updater_not_final_smoke")
    expected_backup = Path(smoke["fixture_directory"]) / "backup-072-venv/bin/python"
    require(backup_python.is_absolute() and backup_python == expected_backup
            and smoke.get("checks", {}).get("backup_uses_independent_072_wheel_on_qualified_base") is True,
            "backup_interpreter_not_final_smoke")
    return updater


class DeploymentJournal:
    """Reserve the payload permanently before starting even the owned engine."""
    def __init__(self, lib, payload_sha, run_id, admission_sha):
        require(re.fullmatch(r"[0-9a-f]{64}", payload_sha), "payload_digest_invalid")
        self.lib = lib
        self.path = lib.JOURNALS / ("deploy-" + payload_sha + ".json")
        self.value = {"schema_version": 1, "scope": "devbrain_single_deployment_journal",
                      "run_id": run_id, "data_dir": str(lib.PRODUCTION), "install_source": str(INSTALL_SOURCE),
                      "payload_sha256": payload_sha, "admission_sha256": admission_sha,
                      "state": "prepared", "automatic_replay_allowed": False}
        lib.atomic_private(self.path, self.value, create=True)

    def advance(self, state):
        next_states = {"prepared": "apply_pending", "apply_pending": "applied",
                       "applied": "config_pinned", "config_pinned": "verified"}
        require(next_states.get(self.value["state"]) == state, "deployment_journal_transition")
        value = {**self.value, "state": state}
        self.lib.atomic_private(self.path, value)
        self.value = value


def protected_snapshot(lib, data):
    # These are preferences/model lock/assignments, not credential stores.
    require(not any((data / name).is_symlink() for name in PROTECTED), "protected_path_symlink")
    return {name: lib.sha(lib.canonical(data / name)) if (data / name).exists() else None for name in PROTECTED}


@contextmanager
def config_lock(lib, data):
    """Use the application's existing empty config lock without chmod/replacing it."""
    import fcntl
    path = data / "config.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        row = os.fstat(fd)
        require(stat.S_ISREG(row.st_mode) and row.st_uid == os.getuid() and row.st_nlink == 1
                and row.st_size == 0 and stat.S_IMODE(row.st_mode) in {0o600, 0o644}, "config_lock_unsafe")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = path.stat()
        require((current.st_dev, current.st_ino) == (row.st_dev, row.st_ino), "config_lock_changed")
        yield
    finally:
        os.close(fd)


def pin_runtime_config(lib, data, original, runtime):
    # Raw merge preserves unknown keys and every model/token/endpoint value.
    # Do not call Settings.save(), which intentionally filters unknown fields.
    path = data / "config.json"
    with config_lock(lib, data):
        require(lib.document(path, private=True) == original, "production_config_changed")
        proposed = {**original, "java_executable": runtime["java_executable"],
                    "tmux_executable": runtime["tmux_executable"]}
        lib.atomic_private(path, proposed)
        require(lib.document(path, private=True) == proposed, "runtime_config_readback_failed")
    return proposed


def observe_database(lib, settings):
    # No Store constructor, ensure_database, initialization or queue scheduling.
    with lib.exclusive_file(settings.data_dir / "database-writer.lock") as writer:
        return lib.canonical_export(lib.existing_store(settings, writer).export())


def command_for(runtime, updater, backup_python, gate):
    return [runtime["python_executable"], "-I", "-B", str(updater), "--source-dir", str(INSTALL_SOURCE),
            "--python", runtime["python_executable"], "--backup-python", str(backup_python),
            "--runtime-gate", str(gate), "--data-dir", "/home/piotr/.local/share/devbrain"]


def other_owned_children(scope, parent, engine_row):
    result = []
    for row in scope.members(exclude={parent["pid"], engine_row["pid"]}):
        # Only the supervisor's exact ongoing kernel probe is outside this
        # controller's workload. The outer scope-empty proof still covers it.
        if row["parent_pid"] == parent["pid"] and row["executable"] == str(Path("/usr/bin/dmesg").resolve()):
            try:
                if (Path("/proc") / str(row["pid"]) / "cmdline").read_bytes() == b"/usr/bin/dmesg\0--raw\0" and scope.owns(row):
                    continue
            except FileNotFoundError:
                continue
        result.append(row)
    return result


def stop_owned_children(scope, parent, engine_row, report):
    """Failure-only bounded cleanup; never signal supervisor, self or own JVM."""
    for number, seconds in ((signal.SIGTERM, 3), (signal.SIGKILL, 2)):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            rows = other_owned_children(scope, parent, engine_row)
            if not rows:
                return
            report["forced_cleanup"] = True
            for row in rows:
                try:
                    fd = os.pidfd_open(row["pid"])
                except ProcessLookupError:
                    continue
                try:
                    if scope.owns(row):
                        signal.pidfd_send_signal(fd, number)
                except ProcessLookupError:
                    pass
                finally:
                    os.close(fd)
            time.sleep(.1)
    require(not other_owned_children(scope, parent, engine_row), "deployment_children_remain")


def run_updater(command, env, log_path, engine, scope, supervision, parent, timeout=900):
    require(not other_owned_children(scope, parent, engine.row), "unexpected_child_before_update")
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as log:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   env=env, start_new_session=True)
        handle = os.pidfd_open(process.pid)
        try:
            row = supervision.identity(process.pid)
            require(row and row["parent_pid"] == os.getpid() and scope.owns(row)
                    and row["executable"] == command[0], "updater_process_identity_unavailable")
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                require(time.monotonic() < deadline, "updater_timeout")
                require(engine.process.poll() is None and scope.owns(engine.row), "owned_engine_exited_during_update")
                require(supervision.same_process(parent) and os.getppid() == parent["pid"], "supervisor_parent_changed")
                for child in other_owned_children(scope, parent, engine.row):
                    if child["executable"] == engine.runtime["java_executable"]:
                        try:
                            command_parts = (Path("/proc") / str(child["pid"]) / "cmdline").read_bytes().split(b"\0")
                        except FileNotFoundError:
                            continue
                        require(b"com.arcadedb.server.ArcadeDBServer" not in command_parts,
                                "second_engine_forbidden")
                time.sleep(.1)
            require(process.wait(timeout=1) == 0, "updater_nonzero")
            poller = select.poll()
            poller.register(handle, select.POLLIN)
            require(bool(poller.poll(0)), "updater_actual_exit_unverified")
        finally:
            os.close(handle)


def deployment_lifecycle(engine, perform, stop_children, quiet, quiescent, report):
    try:
        report["production_engine_start_requested"] = True
        engine.start()
        perform()
    finally:
        # Children finish before the database server is stopped, including when
        # the updater times out or validation raises after a partial install.
        try:
            stop_children()
        except Exception:
            report.setdefault("cleanup_errors", []).append("deployment_child_cleanup_failed")
        try:
            report.update(engine.stop())
        except Exception:
            report.setdefault("cleanup_errors", []).append("owned_engine_cleanup_failed")
        try:
            report["cleanup_grace_seconds"] = quiet()
            quiescent()
        except Exception:
            report.setdefault("cleanup_errors", []).append("production_quiet_observation_failed")
        report["cleanup_complete"] = (not report.get("cleanup_errors") and not report["forced_cleanup"]
                                      and report.get("engine_actual_exit_verified") is True
                                      and report.get("graceful_shutdown_requested") is True
                                      and report.get("cleanup_grace_seconds", 0) >= 30)
    require(report["cleanup_complete"], "deployment_cleanup_incomplete")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    for name in ("run-id", "admission-sha256"):
        parser.add_argument("--" + name)
    for name in ("artifact-root", "source-root", "install-source", "data-dir", "admission", "updater", "backup-python"):
        parser.add_argument("--" + name, type=Path)
    args = parser.parse_args(argv)
    if not args.execute:
        print(json.dumps({"status": "prepared_not_run", "production_modified": False, "automatic_replay_allowed": False}))
        return 0
    report = supervision = artifacts = lib = None
    try:
        require(all(value is not None for key, value in vars(args).items() if key != "execute"), "explicit_deployment_inputs_required")
        require(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.run_id)
                and re.fullmatch(r"[0-9a-f]{64}", args.admission_sha256), "deployment_identity_invalid")
        require(sys.platform == "linux" and sys.flags.isolated == 1, "isolated_linux_required")
        lib, supervision = load_helper("retry-production-queue-080.py"), load_helper("supervise-080.py")
        guard, common = load_helper("attest-full-suite-080.py"), load_helper("validate-native-account080.py")
        require(supervision.CONTROLLERS.get(Path(__file__).name) == STATUS
                and REQUIRED_ARTIFACTS <= supervision.CONTROLLER_ARTIFACTS.get(Path(__file__).name, set()), "deployment_not_registered")
        require(args.data_dir == lib.PRODUCTION and args.install_source == INSTALL_SOURCE, "explicit_production_paths_required")
        artifacts = lib.canonical(args.artifact_root, directory=True)
        require(artifacts.is_relative_to(Path("/mnt/c")) and not (artifacts / "controller-report.json").exists(), "fresh_external_artifacts_required")
        require(os.environ.get("DEVBRAIN_QUALIFICATION_RUN_ID") == args.run_id, "supervision_run_mismatch")
        mapping = lib.document(artifacts / "mapping-proof.json")
        require(mapping.get("run_id") == args.run_id, "supervision_scope_mismatch")
        scope = supervision.ScopeOwnership(mapping.get("scope_unit"))
        parent = supervision.identity(os.getppid())
        require(parent and scope.owns(parent), "supervisor_parent_required")
        runtime = supervision.qualified_runtime(lib.document(artifacts / "gate.json"))
        require(str(Path(sys.executable).resolve()) == runtime["python_executable"], "interpreter_not_qualified")
        source = lib.canonical(args.source_root, directory=True)
        origin = importlib.util.find_spec("devbrain")
        require(origin and Path(origin.origin).resolve() == source / "src/devbrain/__init__.py", "qualified_source_origin_required")
        admission_path = lib.pinned_reference({"path": str(args.admission), "sha256": args.admission_sha256}, private=True)
        admission = lib.document(admission_path, private=True)
        updater_path = verify_release(lib, admission, guard, source, runtime, args.updater, args.backup_python)
        require(admission["runtime_gate_sha256"] == lib.sha(artifacts / "gate.json"), "runtime_gate_mismatch")
        libraries = admission.get("arcade_library_sha256")
        require(isinstance(libraries, dict) and libraries and all(isinstance(k, str) and isinstance(v, str)
                and re.fullmatch(r"[0-9a-f]{64}", v) for k, v in libraries.items()), "admitted_engine_libraries_required")
        updater = load_module(updater_path)
        package = json.loads(updater.zlib.decompress(updater.base64.b64decode(updater.PAYLOAD)))
        require(updater.digest(updater.PAYLOAD.encode()) == admission["release_payload_sha256"], "release_payload_mismatch")
        backup_identity = updater.verify_package(args.backup_python, runtime, package["backup_version"],
                                                  package["backup_package_sha256"], package["backup_dependency_pins"])
        # Every release proof precedes private production settings access.
        data = lib.canonical(args.data_dir, directory=True, private=True)
        lib.canonical(args.install_source, directory=True)
        require(updater.tomllib.loads((INSTALL_SOURCE / "pyproject.toml").read_text())["project"]["version"]
                == package["backup_version"] == "0.7.2", "existing_072_sources_required")
        lib.canonical(UPDATES, directory=True)
        require(UPDATES.stat().st_uid == os.getuid() and not (UPDATES.stat().st_mode & 0o022), "updates_parent_unsafe")
        require(not source.is_relative_to(INSTALL_SOURCE) and not INSTALL_SOURCE.is_relative_to(source), "qualified_source_must_be_immutable")
        env = updater.runtime_environment(runtime, data)
        env.update(HOME="/home/piotr", XDG_STATE_HOME="/home/piotr/.local/state", LANG="C.UTF-8", LC_ALL="C.UTF-8")
        uv = shutil.which("uv", path=env.get("PATH", ""))
        require(bool(uv), "uv_missing")
        target = updater.tool_environment(uv, env)
        backup_prefix = Path(backup_identity["prefix"])
        require(not any(backup_prefix.is_relative_to(p) or p.is_relative_to(backup_prefix)
                        for p in (target, INSTALL_SOURCE, data)), "backup_environment_overlaps_installation")
        require(not Path(sys.prefix).resolve().is_relative_to(target), "controller_environment_would_be_replaced")
        updater.verify_java_environment(env, runtime)
        report = {"schema_version": 1, "scope": "devbrain_production_deployment", "run_id": args.run_id,
                  "status": "running", "runtime": runtime, "python_launcher": sys.executable,
                  "source_hashes": admission["source_hashes"], "runtime_gate_sha256": admission["runtime_gate_sha256"],
                  "updater_sha256": lib.sha(updater_path), "release_zip_sha256": admission["release_zip_sha256"],
                  "controller_exit_verified": False, "cleanup_complete": False, "forced_cleanup": False,
                  "automatic_replay_allowed": False, "job_execution_allowed": False, "errors": []}
        supervision.atomic_json(artifacts / "production-deployment-admission.json", {
            "schema_version": 1, "scope": "devbrain_production_deployment_admission_binding",
            "admission_sha256": args.admission_sha256, "source_final": True,
            "runtime_gate_sha256": admission["runtime_gate_sha256"]})
        supervision.atomic_json(artifacts / "controller-report.json", report)
        lib.prepare_journal_directory(lib.JOURNALS)
        with ExitStack() as stack:
            stack.enter_context(lib.exclusive_file(lib.JOURNALS / "operation.lock"))
            # The updater needs the writer/config lock itself. Hold the other
            # public launch locks; our direct ProductionEngine needs no engine lock.
            for name in ("service.lock", "engine.lock", "cockpit/launch.lock", "cockpit/server.lock",
                         "workspace/supervisor.lock", "workspace/ui.lock", "workspace/reconcile.lock"):
                path = data / name
                if path.parent.exists():
                    stack.enter_context(lib.exclusive_file(path))
            lib.require_quiescent(data, {parent["pid"]})
            require(not any(key.startswith("DEVBRAIN_ARCADE_") for key in os.environ), "production_database_env_override")
            original_config = lib.document(data / "config.json", private=True)
            protected = protected_snapshot(lib, data)
            journal = DeploymentJournal(lib, admission["release_payload_sha256"], args.run_id, args.admission_sha256)
            private_run = lib.JOURNALS / ("deployment-" + args.run_id)
            private_run.mkdir(mode=0o700)
            lib.fsync_directory(private_run.parent)
            from devbrain.config import Settings
            from devbrain.store import Store
            settings = Settings.load(data, create=False)
            require(settings.arcade_version == "26.11.1", "production_engine_version_not_pinned")
            settings.java_executable, settings.tmux_executable = runtime["java_executable"], runtime["tmux_executable"]
            engine = lib.ProductionEngine(settings, runtime, scope, supervision, private_run, libraries)
            def perform():
                baseline = observe_database(lib, settings)
                require(not any(row.get("state") == "running" for row in baseline["jobs"]), "production_has_running_jobs")
                before_backups = set(UPDATES.glob("*/database.devbrain.json.gz"))
                journal.advance("apply_pending")
                run_updater(command_for(runtime, updater_path, args.backup_python, artifacts / "gate.json"),
                            env, private_run / "updater-private.log", engine, scope, supervision, parent)
                journal.advance("applied")
                require(lib.sha(updater_path) == admission["updater"]["sha256"], "updater_changed")
                installed = updater.installed_target(uv, runtime, package)
                require(installed is not None and installed["prefix"] == str(target), "installed_package_verification_failed")
                require(updater.verify_package(args.backup_python, runtime, package["backup_version"],
                        package["backup_package_sha256"], package["backup_dependency_pins"]) == backup_identity,
                        "immutable_backup_interpreter_changed")
                require(guard.source_manifest(INSTALL_SOURCE) == admission["source_hashes"], "installed_sources_differ")
                receipt_path = UPDATES / ("installed-" + updater.digest(str(INSTALL_SOURCE).encode())[:20] + ".json")
                receipt = lib.document(receipt_path, private=True)
                require(receipt.get("format_version") == 2 and receipt.get("version") == "0.8.0"
                        and receipt.get("source") == str(INSTALL_SOURCE) and receipt.get("data_dir") == str(data)
                        and receipt.get("payload_sha256") == admission["release_payload_sha256"]
                        and receipt.get("runtime_gate_sha256") == admission["runtime_gate_sha256"]
                        and receipt.get("target_environment") == str(target)
                        and receipt.get("backup_identity") == backup_identity, "update_receipt_mismatch")
                created = set(UPDATES.glob("*/database.devbrain.json.gz")) - before_backups
                require(len(created) == 1, "exact_new_preupdate_backup_required")
                backup = lib.canonical(created.pop(), private=True)
                require(lib.canonical_export(Store._validated_backup(backup)) == baseline, "preupdate_backup_not_exact")
                require(observe_database(lib, settings) == baseline, "production_memory_changed_by_deployment")
                require(protected_snapshot(lib, data) == protected, "protected_preferences_or_models_changed")
                pin_runtime_config(lib, data, original_config, runtime)
                journal.advance("config_pinned")
                report.update(installed_package_verified=True, preupdate_backup_exact=True, memory_unchanged=True,
                              preferences_models_assignments_unchanged=True, qualified_runtime_persisted=True,
                              update_receipt_sha256=lib.sha(receipt_path), updater_actual_exit_verified=True)
            deployment_lifecycle(engine, perform,
                lambda: stop_owned_children(scope, parent, engine.row or {"pid": -1}, report),
                lambda: common.quiet_scope(scope, supervision, parent),
                lambda: lib.require_quiescent(data, {parent["pid"]}), report)
            require(lib.sha(admission_path) == args.admission_sha256
                    and guard.source_manifest(source) == admission["source_hashes"], "admitted_inputs_changed")
            guard.verify_runtime_files(runtime)
            journal.advance("verified")
            report["workload_criteria_passed"] = True
            report["status"] = STATUS
    except Exception as exc:
        safe = isinstance(exc, Refusal) or (lib is not None and isinstance(exc, lib.Refusal))
        code = str(exc) if safe else "production_deployment_precondition_or_execution_failed"
        if report is None:
            print(json.dumps({"status": "refused", "error_code": code, "production_modified": False}))
            return 1
        report["errors"].append(code)
        report["status"] = "failed_deployment_no_automatic_replay"
    finally:
        if report is not None:
            supervision.atomic_json(artifacts / "production-deployment-results.json", report)
            supervision.atomic_json(artifacts / "controller-report.json", report)
    return 0 if report["status"] == STATUS else 1


if __name__ == "__main__":
    raise SystemExit(main())
