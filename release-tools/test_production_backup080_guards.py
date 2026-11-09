"""Synthetic backup/admission contracts; no production, JVM, model or auth IO."""
from contextlib import contextmanager
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace

import pytest


def sibling(name):
    spec = importlib.util.spec_from_file_location("backup_guard_" + name.replace("-", "_"), Path(__file__).with_name(name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def backup():
    return sibling("backup-production080.py")


@pytest.fixture
def lib():
    return sibling("retry-production-queue-080.py")


def test_default_backup_is_inert_without_loading_helpers(backup, monkeypatch, capsys):
    monkeypatch.setattr(backup, "load_helper", lambda name: pytest.fail("no filesystem admission without execute"))
    assert backup.main([]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "prepared_not_run", "production_modified": False,
                                                  "backup_created": False, "job_execution_allowed": False}


def test_missing_explicit_inputs_fails_before_any_helper_or_production_read(backup, monkeypatch, capsys):
    monkeypatch.setattr(backup, "load_helper", lambda name: pytest.fail("incomplete invocation"))
    assert backup.main(["--execute"]) == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == "explicit_backup_inputs_required"


def test_backup_reuses_all_release_proofs_without_requiring_a_previous_backup(backup, lib):
    admission = {"schema_version": 1, "scope": "devbrain_production_retry_admission", "operation": "post_update_backup",
                 "source_final": True, "version": "0.8.0", "data_dir": str(lib.PRODUCTION), "source_hashes": {"a": "b"},
                 "runtime": {key: "synthetic" for key in lib.RUNTIME_KEYS}, "proofs": {}}
    assert not any(name.startswith("post_update_backup") for name in admission)
    with pytest.raises(lib.Refusal, match="all_release_proofs_required"):
        backup.verify_release_input(lib, admission, SimpleNamespace(source_manifest=lambda _: {"a": "b"}),
                                    Path("/synthetic"), admission["runtime"])


@pytest.mark.parametrize("operation", [None, "retry", "restore"])
def test_other_operations_never_enter_release_admission(backup, operation):
    with pytest.raises(backup.Refusal, match="explicit_backup_operation_required"):
        backup.verify_release_input(SimpleNamespace(verify_admission=lambda *a: pytest.fail("wrong operation")),
                                    {"operation": operation}, None, None, None)


@pytest.mark.parametrize("defect", [None, "missing_controller", "missing_tests", "changed_bytes", "not_collected"])
def test_old_guard_receipt_cannot_admit_new_backup_code(backup, lib, tmp_path, monkeypatch, defect):
    called = []
    monkeypatch.setattr(lib, "verify_admission", lambda *args: called.append("all_eleven_verified"))
    names = ("backup-production080.py", "test_production_backup080_guards.py")
    helpers = {name: lib.sha(Path(__file__).with_name(name)) for name in names}
    if defect in {"missing_controller", "missing_tests"}:
        helpers.pop(names[defect == "missing_tests"])
    elif defect == "changed_bytes":
        helpers[names[0]] = "0" * 64
    (tmp_path / "regression-source.json").write_text(json.dumps({"guard_sources": {"helper_sha256": helpers}}))
    (tmp_path / "guards-collection.json").write_text(json.dumps({"nodeids": [] if defect == "not_collected" else
                                                                [names[1] + "::test_default_backup_is_inert_without_loading_helpers"]}))
    admission = {"operation": "post_update_backup", "proofs": {"tooling_guards": {"artifact_root": str(tmp_path)}}}
    if defect:
        with pytest.raises(backup.Refusal):
            backup.verify_release_input(lib, admission, None, None, None)
    else:
        backup.verify_release_input(lib, admission, None, None, None)
    assert called == ["all_eleven_verified"]


@pytest.mark.parametrize("field", [None, "format_version", "version", "data_dir", "payload_sha256",
                                  "runtime_gate_sha256", "python_binary_sha256", "target_environment"])
def test_update_receipt_binds_actual_installed_prefix_and_payload(backup, lib, tmp_path, monkeypatch, field):
    path = tmp_path / "installed.json"
    admission = {"release_payload_sha256": "payload", "runtime_gate_sha256": "gate"}
    runtime = {"python_binary_sha256": "python"}
    value = {"format_version": 2, "version": "0.8.0", "data_dir": str(lib.PRODUCTION), "payload_sha256": "payload",
             "runtime_gate_sha256": "gate", "python_binary_sha256": "python", "target_environment": str(Path(sys.prefix).resolve())}
    if field:
        value[field] = "wrong"
    path.write_text(json.dumps(value)); path.chmod(0o600)
    admission["update_receipt"] = {"path": str(path), "sha256": lib.sha(path)}
    if field:
        with pytest.raises(backup.Refusal, match="installed_receipt_mismatch"):
            backup.verify_update(lib, admission, lib.PRODUCTION, runtime)
    else:
        assert backup.verify_update(lib, admission, lib.PRODUCTION, runtime) == path


def test_backup_run_directory_is_private_and_cannot_be_reused(backup, lib, tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "UPDATES", tmp_path)
    destination = tmp_path / "post-update-synthetic" / "database.devbrain.json.gz"
    before_mode = tmp_path.stat().st_mode
    assert backup.new_backup_directory(lib, destination, "synthetic") == destination.parent
    assert destination.parent.stat().st_mode & 0o777 == 0o700 and tmp_path.stat().st_mode == before_mode
    (destination.parent / "partial").write_text("preserve")
    with pytest.raises(FileExistsError):
        backup.new_backup_directory(lib, destination, "synthetic")
    assert (destination.parent / "partial").read_text() == "preserve"


@pytest.mark.parametrize("defect", ["other_name", "other_run", "writable_parent", "symlink_parent"])
def test_backup_destination_refuses_unowned_or_noncanonical_paths(backup, lib, tmp_path, monkeypatch, defect):
    root = tmp_path / "updates"; root.mkdir(mode=0o700)
    monkeypatch.setattr(backup, "UPDATES", root)
    destination = root / "post-update-synthetic" / "database.devbrain.json.gz"
    if defect == "other_name":
        destination = destination.with_name("other.gz")
    elif defect == "other_run":
        destination = root / "post-update-other" / destination.name
    elif defect == "writable_parent":
        root.chmod(0o777)
    else:
        target = tmp_path / "real"; root.rename(target); root.symlink_to(target, target_is_directory=True)
    with pytest.raises((backup.Refusal, lib.Refusal)):
        backup.new_backup_directory(lib, destination, "synthetic")
    assert not destination.parent.exists()


def fake_export(tmp_path, *, changed=None, running=False):
    payload = {"format_version": 5, "backend": "arcadedb", "exported_at": "synthetic",
               "jobs": [{"id": "job", "state": "running" if running else "failed", "attempts": 2}],
               "records": [{"id": "record", "content": "synthetic"}],
               "vector_data": {"vector_test": [{"id": "vector", "vector": [0.1, 0.2]}]},
               "journal_events": [{"id": "event", "seq": 1}]}
    events, reads = [], []
    class FakeStore:
        def export(self):
            reads.append("read")
            result = deepcopy(payload)
            if changed == "after_export" and len(reads) > 1:
                result["records"][0]["content"] = "changed"
            return result
    class Writer:
        @staticmethod
        def _write_backup(path, value):
            events.append("write_backup_file")
            path.write_text("synthetic archive"); path.chmod(0o600)
            if changed == "fsync":
                raise OSError("synthetic directory fsync failure")

        @staticmethod
        def _validated_backup(path):
            result = deepcopy(payload)
            if changed in {"records", "journal_events", "vector_data"}:
                result[changed] = {} if changed == "vector_data" else []
            return result
    return FakeStore(), Writer, events, reads


@pytest.mark.parametrize("defect", [None, "records", "journal_events", "vector_data", "after_export", "fsync", "running"])
def test_backup_requires_exact_domain_vector_journal_and_second_readback(backup, lib, tmp_path, defect):
    destination = tmp_path / "database.devbrain.json.gz"
    store, writer, events, reads = fake_export(tmp_path, changed=defect, running=defect == "running")
    if defect:
        with pytest.raises((backup.Refusal, OSError)):
            backup.create_verified_backup(lib, store, writer, destination)
    else:
        proof = backup.create_verified_backup(lib, store, writer, destination)
        assert proof == {"backup_sha256": lib.sha(destination), "backup_validation_passed": True}
        assert reads == ["read", "read"] and events == ["write_backup_file"]
    if defect == "running":
        assert events == [] and not destination.exists()


def test_existing_backup_is_never_overwritten_or_exported(backup, lib, tmp_path):
    path = tmp_path / "existing.gz"; path.write_text("keep")
    with pytest.raises(backup.Refusal, match="fresh_backup_required"):
        backup.create_verified_backup(lib, SimpleNamespace(export=lambda: pytest.fail("must not read DB")), None, path)
    assert path.read_text() == "keep"


@pytest.mark.parametrize("failure", [None, "start", "export", "stop", "quiet", "quiescent"])
def test_lifecycle_always_observes_cleanup_and_never_claims_failed_production_backup(backup, failure):
    order, report = [], {}
    def step(name, result=None):
        order.append(name)
        if failure == name:
            raise RuntimeError("synthetic failure")
        return result
    engine = SimpleNamespace(start=lambda: step("start"), stop=lambda: step("stop", {
        "engine_actual_exit_verified": True, "graceful_shutdown_requested": True}))
    invoke = lambda: backup.owned_backup_lifecycle(engine, lambda: step("export", {"verified": True}),
        lambda: step("quiet", 30.1), lambda: step("quiescent"), report)
    if failure:
        with pytest.raises((backup.Refusal, RuntimeError)):
            invoke()
    else:
        assert invoke() == {"verified": True} and report["cleanup_complete"]
    assert "stop" in order and "quiet" in order and order.index("quiet") > order.index("stop")
    if failure in {"stop", "quiet", "quiescent"}:
        assert report["cleanup_complete"] is False


def test_private_report_has_exact_retry_contract_and_fsync_failure_is_not_success(backup, lib, tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    update = tmp_path / "installed.json"; update.write_text("synthetic receipt"); update.chmod(0o600)
    destination = tmp_path / "database.devbrain.json.gz"; destination.write_text("synthetic archive"); destination.chmod(0o600)
    result = {"backup_sha256": lib.sha(destination), "backup_validation_passed": True}
    report = {"run_id": "synthetic", "cleanup_complete": True, "engine_actual_exit_verified": True,
              "graceful_shutdown_requested": True}
    real_fsync = lib.fsync_directory
    def fail_after_replace(path):
        output = path / "backup-report.json"
        if output.exists() and output.stat().st_size:
            raise OSError("power cut after rename")
        real_fsync(path)
    monkeypatch.setattr(lib, "fsync_directory", fail_after_replace)
    with pytest.raises(OSError, match="after rename"):
        backup.publish_private_report(lib, destination, result, report, {"source_hashes": {"a": "b"}}, update, {"python": "synthetic"})
    private = json.loads((tmp_path / "backup-report.json").read_text())
    assert private["scope"] == "devbrain_post_update_backup" and private["status"] == "backup_completed"
    assert private["backup_validation_passed"] and private["engine_cleanup_complete"]
    assert private["external_receipt_required_before_retry_admission"]
    with pytest.raises(FileExistsError):
        backup.publish_private_report(lib, destination, result, report, {"source_hashes": {"a": "b"}}, update, {})


@pytest.mark.parametrize("defect", ["cleanup", "actual_exit", "graceful", "unvalidated", "modified", "too_old", "before_update"])
def test_report_refuses_unclean_or_stale_backup(backup, lib, tmp_path, monkeypatch, defect):
    tmp_path.chmod(0o700)
    update = tmp_path / "installed.json"; update.write_text("update"); update.chmod(0o600)
    path = tmp_path / "backup.gz"; path.write_text("archive"); path.chmod(0o600)
    report = {"run_id": "synthetic", "cleanup_complete": True, "engine_actual_exit_verified": True,
              "graceful_shutdown_requested": True}
    for key, field in {"cleanup": "cleanup_complete", "actual_exit": "engine_actual_exit_verified", "graceful": "graceful_shutdown_requested"}.items():
        if defect == key:
            report[field] = False
    result = {"backup_sha256": "wrong" if defect == "modified" else lib.sha(path),
              "backup_validation_passed": defect != "unvalidated"}
    if defect == "too_old":
        os.utime(update, (1, 1)); os.utime(path, (2, 2))
    elif defect == "before_update":
        os.utime(path, (1, 1))
    with pytest.raises(backup.Refusal):
        backup.publish_private_report(lib, path, result, report, {"source_hashes": {}}, update, {})
    assert not (tmp_path / "backup-report.json").exists()


@pytest.mark.parametrize("failure", [None, "export", "stop", "quiet", "publication"])
def test_controller_final_status_requires_successful_production_cleanup_and_durable_report(
        backup, tmp_path, monkeypatch, failure):
    # Exercise the real main/lifecycle/error publication with synthetic services.
    # No process, production path, credential, or live database is accessed.
    events, published = [], {}
    art = tmp_path / "artifacts"; art.mkdir()
    data = tmp_path / "synthetic-data"; data.mkdir()
    source = tmp_path / "source"; source.mkdir()
    admission_path = tmp_path / "admission.json"
    runtime = {"python_executable": str(Path(sys.executable).resolve()), "java_executable": "/synthetic/java",
               "tmux_executable": "/synthetic/tmux"}
    admission = {"source_hashes": {"a": "b"}, "runtime_gate_sha256": "a" * 64,
                 "arcade_library_sha256": {"synthetic.jar": "a" * 64}}
    class Artifact(type(art)):
        def is_relative_to(self, other):
            return str(other) == "/mnt/c" or super().is_relative_to(other)
    def step(name, result=None):
        events.append(name)
        if failure == name:
            raise OSError("PRIVATE_SYNTHETIC_DIAGNOSTIC")
        return result
    @contextmanager
    def locked(path):
        events.append("lock:" + path.name)
        try:
            yield 123
        finally:
            events.append("unlock:" + path.name)
    lib = SimpleNamespace(
        PRODUCTION=data, JOURNALS=tmp_path / "journal", Refusal=RuntimeError,
        canonical=lambda p, **kw: Artifact(p) if p == art else Path(p),
        pinned_reference=lambda ref, **kw: Path(ref["path"]),
        document=lambda p, **kw: admission if p == admission_path else {"run_id": "synthetic", "scope_unit": "synthetic"},
        sha=lambda p: "a" * 64,
        package_matches_source=lambda p: step("installed_package_verified"),
        prepare_journal_directory=lambda p: step("prepare_private_journal"),
        exclusive_file=locked, require_quiescent=lambda *a: step("quiescent"),
        existing_store=lambda *a: step("existing_store_no_initialization", object()),
        ProductionEngine=lambda *a: SimpleNamespace(start=lambda: step("start"), stop=lambda: step("stop", {
            "engine_actual_exit_verified": True, "graceful_shutdown_requested": True})))
    def atomic(path, value):
        published[path.name] = deepcopy(value)
    supervision = SimpleNamespace(
        CONTROLLERS={"backup-production080.py": backup.STATUS},
        CONTROLLER_ARTIFACTS={"backup-production080.py": backup.REQUIRED_ARTIFACTS},
        ScopeOwnership=lambda name: SimpleNamespace(owns=lambda row: True), identity=lambda pid: {"pid": pid},
        qualified_runtime=lambda gate: runtime, atomic_json=atomic)
    guard = SimpleNamespace(source_manifest=lambda p: {"a": "b"}, verify_runtime_files=lambda r: step("runtime_end_verified"))
    common = SimpleNamespace(quiet_scope=lambda *a: step("quiet", 30.1))
    table = {"retry-production-queue-080.py": lib, "supervise-080.py": supervision,
             "attest-full-suite-080.py": guard, "validate-native-account080.py": common}
    monkeypatch.setattr(backup, "load_helper", table.__getitem__)
    monkeypatch.setattr(backup, "sys", SimpleNamespace(platform="linux", flags=SimpleNamespace(isolated=1),
                                                       executable=sys.executable))
    monkeypatch.setenv("DEVBRAIN_QUALIFICATION_RUN_ID", "synthetic")
    for key in list(os.environ):
        if key.startswith("DEVBRAIN_ARCADE_"):
            monkeypatch.delenv(key)
    monkeypatch.setattr(backup, "verify_release_input", lambda *a: step("all_eleven_proofs_verified"))
    monkeypatch.setattr(backup, "verify_update", lambda *a: step("update_receipt_verified", tmp_path / "installed.json"))
    monkeypatch.setattr(backup, "new_backup_directory", lambda *a: step("new_backup_directory", tmp_path))
    monkeypatch.setattr(backup, "create_verified_backup", lambda *a: step("export", {"backup_validation_passed": True}))
    monkeypatch.setattr(backup, "publish_private_report", lambda *a: step("publication", "a" * 64))
    settings = SimpleNamespace(arcade_version="26.11.1")
    config_module, store_module = ModuleType("devbrain.config"), ModuleType("devbrain.store")
    def load_settings(path, *, create):
        assert path == data and create is False
        return step("load_existing_settings", settings)
    config_module.Settings = SimpleNamespace(load=load_settings)
    store_module.Store = type("NoInitializationStore", (), {"__init__": lambda self: pytest.fail("no Store constructor")})
    monkeypatch.setitem(sys.modules, "devbrain.config", config_module)
    monkeypatch.setitem(sys.modules, "devbrain.store", store_module)
    argv = ["--execute", "--run-id", "synthetic", "--artifact-root", str(art), "--source-root", str(source),
            "--data-dir", str(data), "--release-admission", str(admission_path), "--release-admission-sha256", "a" * 64,
            "--destination", str(tmp_path / "backup.gz")]
    assert backup.main(argv) == (1 if failure else 0)
    result = published["controller-report.json"]
    assert result == published["production-backup-results.json"]
    assert events.index("all_eleven_proofs_verified") < events.index("load_existing_settings") < events.index("start")
    assert events.index("stop") < events.index("quiet") < events.index("unlock:database-writer.lock")
    assert result["controller_exit_verified"] is False and result["job_execution_allowed"] is False
    assert result["database_initialization_requested"] is False
    if failure:
        assert result["status"] == "failed_backup_not_admitted_no_automatic_replay"
        assert result["workload_criteria_passed"] is False and "PRIVATE_SYNTHETIC_DIAGNOSTIC" not in json.dumps(published)
    else:
        assert result["status"] == backup.STATUS and result["cleanup_complete"] and result["workload_criteria_passed"]
        assert events.index("quiet") < events.index("publication") < events.index("unlock:database-writer.lock")


def test_registration_and_transitive_hash_dependencies_match_all_observers(backup):
    supervision = sibling("supervise-080.py"); raw = Path(__file__).with_name("run-supervised-080.ps1").read_text()
    name = "backup-production080.py"
    deps = {"retry-production-queue-080.py", "validate-native-account080.py"}
    assert supervision.CONTROLLERS[name] == backup.STATUS
    assert set(supervision.CONTROLLER_DEPENDENCIES[name]) == deps
    assert supervision.controller_artifacts(name, []) == backup.REQUIRED_ARTIFACTS
    for block, expected in (("controllerDependencies", deps), ("controllerArtifacts", backup.REQUIRED_ARTIFACTS)):
        body = re.search(r"\$" + block + r"\s*=\s*@\{(.*?)\n\}", raw, re.S)
        rows = re.findall(r"'" + re.escape(name) + r"'\s*=\s*@\(([^)]*)\)", body[1])
        assert len(rows) == 1 and set(re.findall(r"'([^']+)'", rows[0])) == expected
    allow = re.search(r"if \(\$controllerName -cnotin @\(([^)]*)\)\) \{ throw 'Unsupported controller\.' \}", raw)
    assert re.findall(r"'([^']+)'", allow[1]).count(name) == 1


@pytest.mark.parametrize("windows", [False, True])
def test_backup_receipt_requires_both_transitive_production_and_common_hashes(backup, monkeypatch, windows):
    audit = sibling("attest-full-suite-080.py")
    names = {"backup-production080.py", "attest-full-suite-080.py", "validate-stability-080.py",
             "supervise-080.py", "qualification-power-080.ps1"}
    if windows:
        names.add("run-supervised-080.ps1")
    report = {"helper_sha256_start": {n: "a" * 64 for n in names}, "helper_sha256_end": {n: "a" * 64 for n in names}}
    monkeypatch.setattr(audit, "digest", lambda path: "a" * 64)
    for missing in ("retry-production-queue-080.py", "validate-native-account080.py"):
        with pytest.raises(ValueError, match="lack start/end hashes"):
            audit.verify_helper_files(report, "backup-production080.py", windows=windows)
        report["helper_sha256_start"][missing] = report["helper_sha256_end"][missing] = "a" * 64
    audit.verify_helper_files(report, "backup-production080.py", windows=windows)


def test_new_backup_code_and_tests_are_in_the_full_current_guard_inventory(tmp_path):
    runner, supervision = sibling("run-regressions-080.py"), sibling("supervise-080.py")
    root = Path(__file__).parent.resolve()
    modules = runner.guard_modules(root)
    assert len(modules) == 12 and modules.count("test_production_backup080_guards.py") == 1
    updater = tmp_path / "devbrain-update-0.8.0.py"; updater.write_text("# inert synthetic candidate")
    manifest = runner.guard_manifest(root, supervision, updater.resolve())["helper_sha256"]
    for name in ("backup-production080.py", "test_production_backup080_guards.py", "retry-production-queue-080.py"):
        assert manifest[name] == runner.common.sha(root / name)
