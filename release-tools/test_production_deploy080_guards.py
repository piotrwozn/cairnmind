"""Synthetic deployment contracts; never read production or start a process."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace

import pytest


def sibling(name):
    spec = importlib.util.spec_from_file_location("deploy_guard_" + name.replace("-", "_"), Path(__file__).with_name(name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def deploy():
    return sibling("deploy-production080.py")


@pytest.fixture
def lib():
    return sibling("retry-production-queue-080.py")


def test_default_deployment_is_inert_without_loading_helpers(deploy, monkeypatch, capsys):
    monkeypatch.setattr(deploy, "load_helper", lambda *a: pytest.fail("no helper or production admission"))
    assert deploy.main([]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "prepared_not_run", "production_modified": False,
                                                 "automatic_replay_allowed": False}


def test_incomplete_execute_fails_before_any_helper_or_private_io(deploy, monkeypatch, capsys):
    monkeypatch.setattr(deploy, "load_helper", lambda *a: pytest.fail("no helper before complete arguments"))
    assert deploy.main(["--execute"]) == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == "explicit_deployment_inputs_required"


@pytest.mark.parametrize("operation", [None, "post_update_backup", "retry", "restore"])
def test_other_operations_cannot_enter_deployment_admission(deploy, operation):
    with pytest.raises(deploy.Refusal, match="explicit_deployment_operation_required"):
        deploy.verify_release(SimpleNamespace(verify_admission=lambda *a: pytest.fail("wrong operation")),
                              {"operation": operation}, None, None, None, None, None)


def test_deployment_requires_all_eleven_proofs_but_no_post_update_receipt(deploy, lib):
    runtime = {key: "synthetic" for key in lib.RUNTIME_KEYS}
    admission = {"schema_version": 1, "scope": "devbrain_production_retry_admission", "operation": "production_deployment",
                 "source_final": True, "data_dir": str(lib.PRODUCTION), "version": "0.8.0",
                 "source_hashes": {"a": "b"}, "runtime": runtime, "proofs": {}}
    with pytest.raises(lib.Refusal, match="all_release_proofs_required"):
        deploy.verify_release(lib, admission, SimpleNamespace(source_manifest=lambda _: {"a": "b"}), None, runtime, None, None)
    assert "update_receipt" not in admission and "post_update_backup" not in admission


@pytest.mark.parametrize("defect", [None, "missing_helper", "missing_tests", "changed_helper", "not_collected",
                                    "different_updater", "different_zip", "other_backup_venv", "unproved_backup"])
def test_final_smoke_and_new_guard_bytes_bind_deployment_inputs(deploy, lib, tmp_path, monkeypatch, defect):
    events = []
    monkeypatch.setattr(lib, "verify_admission", lambda *a: events.append("eleven_proofs_verified"))
    helper_names = ("deploy-production080.py", "test_production_deploy080_guards.py")
    hashes = {name: lib.sha(Path(__file__).with_name(name)) for name in helper_names}
    if defect in {"missing_helper", "missing_tests"}:
        hashes.pop(helper_names[defect == "missing_tests"])
    elif defect == "changed_helper":
        hashes[helper_names[0]] = "0" * 64
    (tmp_path / "regression-source.json").write_text(json.dumps({"guard_sources": {"helper_sha256": hashes}}))
    (tmp_path / "guards-collection.json").write_text(json.dumps({"nodeids": [] if defect == "not_collected" else
        ["test_production_deploy080_guards.py::test_default_deployment_is_inert_without_loading_helpers"]}))
    updater = tmp_path / "devbrain-update-0.8.0.py"
    updater.write_text("# synthetic updater, never imported\n")
    backup = tmp_path / "synthetic-fixture/backup-072-venv/bin/python"
    smoke = {"updater_sha256": lib.sha(updater), "release_zip_sha256": "a" * 64,
             "fixture_directory": str(tmp_path / "synthetic-fixture"),
             "checks": {"backup_uses_independent_072_wheel_on_qualified_base": True}}
    if defect == "different_updater":
        smoke["updater_sha256"] = "b" * 64
    elif defect == "different_zip":
        smoke["release_zip_sha256"] = "b" * 64
    elif defect == "other_backup_venv":
        backup = tmp_path / "older-smoke/backup-072-venv/bin/python"
    elif defect == "unproved_backup":
        smoke["checks"] = {}
    (tmp_path / "controller-report.json").write_text(json.dumps(smoke))
    admission = {"operation": "production_deployment", "release_zip_sha256": "a" * 64,
                 "updater": {"path": str(updater), "sha256": lib.sha(updater)},
                 "proofs": {name: {"artifact_root": str(tmp_path)} for name in ("tooling_guards", "upgrade_smoke")}}
    if defect:
        with pytest.raises(deploy.Refusal):
            deploy.verify_release(lib, admission, None, None, {}, updater, backup)
    else:
        assert deploy.verify_release(lib, admission, None, None, {}, updater, backup) == updater
    assert events == ["eleven_proofs_verified"]


@pytest.mark.parametrize("state", ["prepared", "apply_pending", "applied", "config_pinned", "verified"])
def test_per_payload_journal_blocks_another_run_at_every_stage(deploy, lib, tmp_path, monkeypatch, state):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(lib, "JOURNALS", tmp_path)
    journal = deploy.DeploymentJournal(lib, "a" * 64, "first-run", "b" * 64)
    for next_state in ("apply_pending", "applied", "config_pinned", "verified"):
        if journal.value["state"] == state:
            break
        journal.advance(next_state)
    before = journal.path.read_bytes()
    with pytest.raises(FileExistsError):
        deploy.DeploymentJournal(lib, "a" * 64, "different-run-id", "b" * 64)
    assert journal.path.read_bytes() == before and not journal.value["automatic_replay_allowed"]


def test_pending_journal_fsync_failure_never_reauthorizes_apply(deploy, lib, tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(lib, "JOURNALS", tmp_path)
    journal = deploy.DeploymentJournal(lib, "a" * 64, "run", "b" * 64)
    monkeypatch.setattr(lib, "fsync_directory", lambda p: (_ for _ in ()).throw(OSError("synthetic fsync")))
    with pytest.raises(OSError):
        journal.advance("apply_pending")
    assert json.loads(journal.path.read_text())["state"] == "apply_pending"
    assert journal.value["state"] == "prepared"
    with pytest.raises(FileExistsError):
        deploy.DeploymentJournal(lib, "a" * 64, "new-run", "b" * 64)


@pytest.mark.parametrize("state", ["verified", "config_pinned", "applied", "unknown"])
def test_journal_cannot_skip_mutation_or_verification_stages(deploy, lib, tmp_path, monkeypatch, state):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(lib, "JOURNALS", tmp_path)
    journal = deploy.DeploymentJournal(lib, "a" * 64, "run", "b" * 64)
    with pytest.raises(deploy.Refusal, match="journal_transition"):
        journal.advance(state)
    assert lib.document(journal.path, private=True)["state"] == "prepared"


def write_config(tmp_path):
    tmp_path.chmod(0o700)
    original = {"chat_model": "synthetic-model", "token": "SYNTHETIC_LOCAL_TOKEN",
                "arcade_password": "SYNTHETIC_PASSWORD", "unknown_future_field": {"preserve": [1, 2]}}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(original)); path.chmod(0o600)
    return path, original


def test_config_pin_preserves_all_unknown_model_and_secret_fields(deploy, lib, tmp_path):
    path, original = write_config(tmp_path)
    runtime = {"java_executable": "/qualified/java", "tmux_executable": "/qualified/tmux"}
    result = deploy.pin_runtime_config(lib, tmp_path, original, runtime)
    assert result == {**original, **runtime} == lib.document(path, private=True)
    assert original == {key: result[key] for key in original}


def test_config_cas_conflict_preserves_newer_values(deploy, lib, tmp_path):
    path, original = write_config(tmp_path)
    newer = {**original, "chat_model": "changed-after-admission"}
    path.write_text(json.dumps(newer))
    with pytest.raises(deploy.Refusal, match="production_config_changed"):
        deploy.pin_runtime_config(lib, tmp_path, original, {"java_executable": "/j", "tmux_executable": "/t"})
    assert json.loads(path.read_text()) == newer


def test_config_replace_then_fsync_failure_is_unknown_not_success(deploy, lib, tmp_path, monkeypatch):
    path, original = write_config(tmp_path)
    runtime = {"java_executable": "/j", "tmux_executable": "/t"}
    monkeypatch.setattr(lib, "fsync_directory", lambda p: (_ for _ in ()).throw(OSError("synthetic fsync")))
    with pytest.raises(OSError):
        deploy.pin_runtime_config(lib, tmp_path, original, runtime)
    assert json.loads(path.read_text()) == {**original, **runtime}


@pytest.mark.parametrize("mode", [0o600, 0o644])
def test_existing_empty_config_lock_is_never_replaced_or_chmodded(deploy, lib, tmp_path, mode):
    tmp_path.chmod(0o700)
    path = tmp_path / "config.lock"; path.touch(); path.chmod(mode)
    old = path.stat()
    with deploy.config_lock(lib, tmp_path):
        assert path.stat().st_ino == old.st_ino and path.stat().st_mode == old.st_mode
    assert path.read_bytes() == b""


@pytest.mark.parametrize("defect", ["writable", "nonempty", "hardlink", "symlink"])
def test_config_lock_rejects_unsafe_existing_files(deploy, lib, tmp_path, defect):
    tmp_path.chmod(0o700)
    path = tmp_path / "config.lock"; path.touch(); path.chmod(0o666 if defect == "writable" else 0o600)
    if defect == "nonempty":
        path.write_text("not a lock")
    elif defect == "hardlink":
        os.link(path, tmp_path / "other")
    elif defect == "symlink":
        path.rename(tmp_path / "other"); path.symlink_to(tmp_path / "other")
    with pytest.raises((deploy.Refusal, OSError)):
        with deploy.config_lock(lib, tmp_path):
            pytest.fail("unsafe lock entered")


def test_dangling_protected_preference_link_is_not_treated_as_absent(deploy, lib, tmp_path):
    (tmp_path / "models.lock.json").symlink_to(tmp_path / "missing")
    with pytest.raises(deploy.Refusal, match="protected_path_symlink"):
        deploy.protected_snapshot(lib, tmp_path)


@pytest.mark.parametrize("failure", [None, "start", "apply", "children", "stop", "quiet", "quiescent"])
def test_every_failure_still_attempts_child_then_jvm_cleanup_and_quiet(deploy, failure):
    events, report = [], {"forced_cleanup": False}
    def step(name, value=None):
        events.append(name)
        if name == failure:
            raise RuntimeError("SYNTHETIC_PRIVATE_FAILURE")
        return value
    engine = SimpleNamespace(start=lambda: step("start"), stop=lambda: step("stop", {
        "engine_actual_exit_verified": True, "graceful_shutdown_requested": True}))
    call = lambda: deploy.deployment_lifecycle(engine, lambda: step("apply"), lambda: step("children"),
        lambda: step("quiet", 30.1), lambda: step("quiescent"), report)
    if failure:
        with pytest.raises((RuntimeError, deploy.Refusal)):
            call()
    else:
        call()
        assert report["cleanup_complete"]
    assert events.index("children") < events.index("stop") < events.index("quiet")
    if failure != "quiet":
        assert "quiescent" in events
    assert "SYNTHETIC_PRIVATE_FAILURE" not in json.dumps(report)


@pytest.mark.parametrize("defect", ["forced", "no_actual_exit", "no_graceful_request", "short_grace"])
def test_cleanup_metadata_cannot_turn_partial_deployment_into_pass(deploy, defect):
    report = {"forced_cleanup": defect == "forced"}
    stopped = {"engine_actual_exit_verified": defect != "no_actual_exit",
               "graceful_shutdown_requested": defect != "no_graceful_request"}
    with pytest.raises(deploy.Refusal, match="cleanup_incomplete"):
        deploy.deployment_lifecycle(SimpleNamespace(start=lambda: None, stop=lambda: stopped), lambda: None,
            lambda: None, lambda: 29.9 if defect == "short_grace" else 30.1, lambda: None, report)
    assert report["cleanup_complete"] is False


def test_updater_argv_uses_qualified_base_legacy_literal_and_no_open_or_generic_stop(deploy):
    command = deploy.command_for({"python_executable": "/qualified/python"}, Path("/final/devbrain-update-0.8.0.py"),
                                 Path("/private/backup-072-venv/bin/python"), Path("/artifacts/gate.json"))
    assert command[:4] == ["/qualified/python", "-I", "-B", "/final/devbrain-update-0.8.0.py"]
    assert command[command.index("--backup-python") + 1] == "/private/backup-072-venv/bin/python"
    assert command[command.index("--source-dir") + 1] == "/home/piotr/src/devbrain"
    assert command[command.index("--data-dir") + 1] == "/home/piotr/.local/share/devbrain"
    assert "--open" not in command and "engine" not in command and "stop" not in command


def test_failure_cleanup_signals_only_rechecked_owned_pidfd(deploy, monkeypatch):
    row = {"pid": 41, "birth": "123", "executable": "/qualified/child", "parent_pid": 12}
    alive, calls = [True], []
    scope = SimpleNamespace(members=lambda **k: [row] if alive[0] else [], owns=lambda r: r is row)
    monkeypatch.setattr(deploy.os, "pidfd_open", lambda pid: calls.append(("open", pid)) or 991)
    monkeypatch.setattr(deploy.os, "close", lambda fd: calls.append(("close", fd)))
    def signal(fd, number):
        calls.append(("signal", fd, number)); alive[0] = False
    monkeypatch.setattr(deploy.signal, "pidfd_send_signal", signal)
    monkeypatch.setattr(deploy.time, "sleep", lambda n: None)
    report = {"forced_cleanup": False}
    deploy.stop_owned_children(scope, {"pid": 1}, {"pid": 2}, report)
    assert report["forced_cleanup"]
    assert calls == [("open", 41), ("signal", 991, deploy.signal.SIGTERM), ("close", 991)]


def test_changed_identity_is_never_signalled_after_pidfd_open(deploy, monkeypatch):
    row = {"pid": 41, "birth": "123", "executable": "/qualified/child", "parent_pid": 12}
    rows = [[row]]
    scope = SimpleNamespace(members=lambda **k: rows[0], owns=lambda r: False)
    def opening(pid):
        rows[0] = []; return 991
    monkeypatch.setattr(deploy.os, "pidfd_open", opening)
    monkeypatch.setattr(deploy.os, "close", lambda fd: None)
    monkeypatch.setattr(deploy.signal, "pidfd_send_signal", lambda *a: pytest.fail("changed process must not be signalled"))
    monkeypatch.setattr(deploy.time, "sleep", lambda n: None)
    report = {"forced_cleanup": False}
    deploy.stop_owned_children(scope, {"pid": 1}, {"pid": 2}, report)
    assert report["forced_cleanup"]  # An unexpected child prevents qualification regardless of its later exit.


@pytest.mark.parametrize("defect", [None, "unowned_updater", "wrong_executable", "nonzero", "timeout",
                                    "engine_exited", "parent_changed", "second_engine", "exit_unverified"])
def test_updater_monitor_requires_exact_process_own_live_jvm_and_real_exit(deploy, tmp_path, monkeypatch, defect):
    closed, calls = [], []
    code = 1 if defect == "nonzero" else 0
    polls = iter([None, code])
    process = SimpleNamespace(pid=41, poll=lambda: next(polls, code), wait=lambda **k: code)
    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return process
    monkeypatch.setattr(deploy.subprocess, "Popen", popen)
    monkeypatch.setattr(deploy.os, "pidfd_open", lambda pid: 991)
    close = deploy.os.close
    monkeypatch.setattr(deploy.os, "close", lambda fd: closed.append(fd) if fd == 991 else close(fd))
    monkeypatch.setattr(deploy.select, "poll", lambda: SimpleNamespace(register=lambda *a: None,
        poll=lambda *a: [] if defect == "exit_unverified" else [(991, 1)]))
    monkeypatch.setattr(deploy.time, "sleep", lambda seconds: None)
    ticks = iter([0, 2])
    monkeypatch.setattr(deploy.time, "monotonic", lambda: next(ticks, 2) if defect == "timeout" else 0)
    row = {"pid": 41, "parent_pid": os.getpid(), "executable": "/wrong" if defect == "wrong_executable" else "/qualified/python"}
    engine_row = {"pid": 42}
    engine = SimpleNamespace(row=engine_row, runtime={"java_executable": "/qualified/java"},
                             process=SimpleNamespace(poll=lambda: 1 if defect == "engine_exited" else None))
    scope = SimpleNamespace(owns=lambda r: not (defect == "unowned_updater" and r is row))
    supervision = SimpleNamespace(identity=lambda pid: row, same_process=lambda r: defect != "parent_changed")
    seen = [0]
    def other(*args):
        seen[0] += 1
        return [{"pid": 43, "executable": "/qualified/java"}] if defect == "second_engine" and seen[0] > 1 else []
    monkeypatch.setattr(deploy, "other_owned_children", other)
    if defect == "second_engine":
        folder = tmp_path / "proc/43"; folder.mkdir(parents=True)
        (folder / "cmdline").write_bytes(b"/qualified/java\0com.arcadedb.server.ArcadeDBServer\0")
        monkeypatch.setattr(deploy, "Path", lambda value: tmp_path / "proc" if value == "/proc" else Path(value))
    log = tmp_path / "private-updater.log"
    invoke = lambda: deploy.run_updater(["/qualified/python", "-I", "-B", "/synthetic/updater.py"],
        {"LANG": "C.UTF-8"}, log, engine, scope, supervision, {"pid": os.getppid()}, timeout=1)
    if defect:
        with pytest.raises(deploy.Refusal):
            invoke()
    else:
        invoke()
    assert closed == [991] and len(calls) == 1
    assert calls[0][1]["start_new_session"] is True
    assert log.stat().st_mode & 0o777 == 0o600 and log.read_bytes() == b""


def test_supervisor_and_windows_register_only_named_deployment_contract(deploy):
    supervision = sibling("supervise-080.py")
    name = "deploy-production080.py"
    deps = {"retry-production-queue-080.py", "validate-native-account080.py"}
    assert supervision.CONTROLLERS[name] == deploy.STATUS
    assert set(supervision.CONTROLLER_DEPENDENCIES[name]) == deps
    assert supervision.controller_artifacts(name, []) == deploy.REQUIRED_ARTIFACTS
    raw = Path(__file__).with_name("run-supervised-080.ps1").read_text(encoding="utf-8")
    for block, expected in (("controllerDependencies", deps), ("controllerArtifacts", deploy.REQUIRED_ARTIFACTS)):
        body = re.search(r"\$" + block + r"\s*=\s*@\{(.*?)\n\}", raw, re.S)
        rows = re.findall(r"'" + re.escape(name) + r"'\s*=\s*@\(([^)]*)\)", body[1])
        assert len(rows) == 1 and set(re.findall(r"'([^']+)'", rows[0])) == expected
    allow = re.search(r"if \(\$controllerName -cnotin @\(([^)]*)\)\) \{ throw 'Unsupported controller\.' \}", raw)
    assert re.findall(r"'([^']+)'", allow[1]).count(name) == 1


@pytest.mark.parametrize("windows", [False, True])
@pytest.mark.parametrize("dependency", ["retry-production-queue-080.py", "validate-native-account080.py"])
def test_attestor_rejects_missing_or_changed_transitive_dependency(monkeypatch, windows, dependency):
    audit = sibling("attest-full-suite-080.py")
    name = "deploy-production080.py"
    names = {name, "attest-full-suite-080.py", "supervise-080.py", "validate-stability-080.py",
             "qualification-power-080.ps1", "retry-production-queue-080.py", "validate-native-account080.py"}
    if windows:
        names.add("run-supervised-080.ps1")
    hashes = {n: "a" * 64 for n in names}
    report = {"helper_sha256_start": dict(hashes), "helper_sha256_end": dict(hashes)}
    monkeypatch.setattr(audit, "digest", lambda p: "a" * 64)
    audit.verify_helper_files(report, name, windows=windows)
    missing = deepcopy(report); missing["helper_sha256_start"].pop(dependency); missing["helper_sha256_end"].pop(dependency)
    with pytest.raises(ValueError, match="lack start/end hashes"):
        audit.verify_helper_files(missing, name, windows=windows)
    monkeypatch.setattr(audit, "digest", lambda p: "b" * 64 if p.name == dependency else "a" * 64)
    with pytest.raises(ValueError, match="differs from observed bytes"):
        audit.verify_helper_files(report, name, windows=windows)


def test_full_guard_inventory_binds_new_controller_and_cases(tmp_path):
    runner, supervision = sibling("run-regressions-080.py"), sibling("supervise-080.py")
    directory = Path(__file__).parent.resolve()
    modules = runner.guard_modules(directory)
    assert len(modules) == 12 and modules.count("test_production_deploy080_guards.py") == 1
    updater = tmp_path / "devbrain-update-0.8.0.py"; updater.write_text("# synthetic inert candidate")
    manifest = runner.guard_manifest(directory, supervision, updater.resolve())["helper_sha256"]
    for name in ("deploy-production080.py", "test_production_deploy080_guards.py", "retry-production-queue-080.py"):
        assert manifest[name] == runner.common.sha(directory / name)
