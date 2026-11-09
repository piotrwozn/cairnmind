"""Synthetic one-job safety contracts; no real DB, JVM, auth or production IO."""
from contextlib import contextmanager
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace

import pytest


JOB = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def retry():
    spec = importlib.util.spec_from_file_location("production_retry_guards", Path(__file__).with_name("retry-production-queue-080.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def baseline():
    return {"id": JOB, "project_id": "project", "kind": "extract", "job_key": "unique", "payload": {"source_id": "source"},
            "state": "failed", "attempts": 2, "retry_count": 0, "worker": "previous-worker", "lease_until": None,
            "error": "PRIVATE prior diagnostic", "attempt_history": [{"event": "finished", "attempts": 2}]}


def test_no_execute_performs_no_helper_or_production_reads(retry, monkeypatch, capsys):
    monkeypatch.setattr(retry, "load_helper", lambda name: pytest.fail("must remain inert"))
    monkeypatch.setattr(retry, "canonical", lambda *args, **kwargs: pytest.fail("no filesystem admission yet"))
    assert retry.main([]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "prepared_not_run"


def test_duplicate_job_arguments_refused_before_any_helper(retry, monkeypatch, capsys):
    monkeypatch.setattr(retry, "load_helper", lambda name: pytest.fail("ambiguous selection must not enter admission"))
    argv = ["--execute", "--run-id", "run", "--artifact-root", "/mnt/c/synthetic", "--source-root", "/synthetic",
            "--data-dir", str(retry.PRODUCTION), "--admission", "/synthetic/admission.json", "--admission-sha256", "a" * 64,
            "--job-id", JOB, "--job-id=" + JOB, "--expected-attempts", "2"]
    assert retry.main(argv) == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == "exactly_one_job_argument_required"


@pytest.mark.parametrize("change", [{"state": "pending"}, {"state": "running"}, {"state": "completed"},
                                    {"attempts": 3}, {"lease_until": 1}, {"kind": "embed"}, {"project_id": "other"},
                                    {"payload": {"source_id": "other"}}, {"job_key": "other"}])
def test_selected_job_rejects_stale_attempt_or_changed_identity(retry, baseline, change):
    with pytest.raises(retry.Refusal):
        retry.selected_job({"jobs": [{**baseline, **change}]}, {JOB: baseline}, JOB, 2)


def test_selection_allows_retained_failed_worker_but_rejects_duplicates(retry, baseline):
    assert retry.selected_job({"jobs": [baseline]}, {JOB: baseline}, JOB, 2) == baseline
    with pytest.raises(retry.Refusal, match="selected_job_not_unique"):
        retry.selected_job({"jobs": [baseline, baseline]}, {JOB: baseline}, JOB, 2)


def fake_store(monkeypatch, baseline, *, changed=None, bookkeeping_wrong=False):
    __import__("devbrain.agent")  # Load the real budget policy before the Store-only shim.
    rows = {JOB: deepcopy(baseline), "unrelated": {"id": "unrelated", "state": "pending", "attempts": 0}}
    if changed:
        rows[JOB].update(changed)
    writes, transactions = [], []
    shim = ModuleType("devbrain.store")
    shim._one = lambda tx, sql, params, strict: deepcopy(rows.get(params["id"]))
    def set_row(tx, table, values, where, params):
        assert table == "jobs" and params == {"id": JOB}
        rows[JOB].update(deepcopy(values))
        writes.append((where, params))
    shim._set, shim._now = set_row, lambda: "synthetic-time"
    monkeypatch.setitem(sys.modules, "devbrain.store", shim)
    class FakeStore:
        @contextmanager
        def transaction(self):
            original = deepcopy(rows)
            transactions.append("begin")
            try:
                yield self
            except BaseException:
                rows.clear()
                rows.update(original)
                transactions.append("rollback")
                raise
            else:
                transactions.append("commit")

        def _require_project(self, tx, project):
            assert tx is self and project == "project"

        def _retry_job_locked(self, tx, row):
            assert tx is self and row == baseline
            row.update(state="pending", error=None, worker=None, lease_until=None, retry_count=1,
                       attempt_history=[*row["attempt_history"], {"event": "retry_requested"}])
            if bookkeeping_wrong:
                row["attempts"] += 1
            rows[JOB] = row
    return FakeStore(), rows, writes, transactions


def test_failed_retry_and_exact_claim_are_one_transaction(retry, baseline, monkeypatch):
    store, rows, writes, transactions = fake_store(monkeypatch, baseline)
    before = deepcopy(rows["unrelated"])
    result = retry.claim_failed_selected(store, baseline, "owned-worker")
    assert transactions == ["begin", "commit"]
    assert writes == [("id=:id AND state='pending'", {"id": JOB})]
    assert rows["unrelated"] == before
    assert result["state"] == "running" and result["attempts"] == 3 and result["retry_count"] == 1
    assert result["worker"] == "owned-worker" and result["lease_until"] > retry.time.time()
    assert result["attempt_history"][-1]["event"] == "retry_requested"


@pytest.mark.parametrize("work_budget,expected_lease", [(120.0, 210.0), (180.0, 270.0), (5.0, 120.0)])
def test_atomic_production_claim_uses_current_shared_inference_lease(
        retry, baseline, monkeypatch, work_budget, expected_lease):
    from devbrain import agent

    monkeypatch.setattr(agent, "EXTRACTION_TIMEOUT", work_budget)
    monkeypatch.setattr(retry.time, "time", lambda: 1000.0)
    store, rows, writes, transactions = fake_store(monkeypatch, baseline)
    unrelated = deepcopy(rows["unrelated"])
    claimed = retry.claim_failed_selected(store, baseline, "owned-worker")
    assert claimed["lease_until"] == 1000.0 + expected_lease
    assert claimed["attempts"] == 3 and claimed["retry_count"] == 1
    assert transactions == ["begin", "commit"] and len(writes) == 1
    assert rows["unrelated"] == unrelated


@pytest.mark.parametrize("change", [{"worker": "changed"}, {"attempts": 3}, {"error": "changed"}, {"state": "completed"}])
def test_cas_compares_complete_current_row_before_bookkeeping(retry, baseline, monkeypatch, change):
    store, rows, writes, transactions = fake_store(monkeypatch, baseline, changed=change)
    with pytest.raises(retry.Refusal, match="claim_compare_and_swap_conflict"):
        retry.claim_failed_selected(store, baseline, "owned-worker")
    assert not writes and transactions == ["begin", "rollback"]


def test_bad_retry_bookkeeping_rolls_back_without_pending_window(retry, baseline, monkeypatch):
    store, rows, writes, transactions = fake_store(monkeypatch, baseline, bookkeeping_wrong=True)
    with pytest.raises(retry.Refusal, match="retry_bookkeeping_conflict"):
        retry.claim_failed_selected(store, baseline, "owned-worker")
    assert rows[JOB] == baseline and not writes and transactions == ["begin", "rollback"]


@pytest.fixture
def private_root(tmp_path):
    tmp_path.chmod(0o700)
    return tmp_path


@pytest.mark.parametrize("last_state", ["prepared", "claim_pending", "claimed", "execution_started", "attempt_finished", "verified"])
def test_every_persisted_attempt_blocks_new_invocation(retry, baseline, private_root, last_state):
    journal = retry.AttemptJournal(private_root, baseline, "first", "a" * 64)
    for state in ("claim_pending", "claimed", "execution_started", "attempt_finished", "verified"):
        if journal.value["state"] == last_state:
            break
        journal.advance(state)
    before = journal.path.read_bytes()
    with pytest.raises(FileExistsError):
        retry.AttemptJournal(private_root, baseline, "second", "a" * 64)
    assert journal.path.read_bytes() == before


def test_replace_then_directory_fsync_failure_never_authorizes_reexecution(retry, baseline, private_root, monkeypatch):
    journal = retry.AttemptJournal(private_root, baseline, "first", "a" * 64)
    monkeypatch.setattr(retry, "fsync_directory", lambda path: (_ for _ in ()).throw(OSError("synthetic power cut")))
    with pytest.raises(OSError):
        journal.advance("claim_pending")
    assert journal.value["state"] == "prepared"  # caller did not observe commit
    assert json.loads(journal.path.read_text())["state"] == "claim_pending"  # replacement already happened
    with pytest.raises(FileExistsError):
        retry.AttemptJournal(private_root, baseline, "second", "a" * 64)


def test_cut_after_exclusive_reservation_leaves_permanent_unknown_marker(retry, baseline, private_root, monkeypatch):
    monkeypatch.setattr(retry, "fsync_directory", lambda path: (_ for _ in ()).throw(OSError("synthetic power cut")))
    with pytest.raises(OSError):
        retry.AttemptJournal(private_root, baseline, "first", "a" * 64)
    path = private_root / (JOB + ".json")
    assert path.exists() and path.read_bytes() == b""
    with pytest.raises(FileExistsError):
        retry.AttemptJournal(private_root, baseline, "second", "a" * 64)


def test_journal_rejects_skipping_execution_boundary(retry, baseline, private_root):
    journal = retry.AttemptJournal(private_root, baseline, "first", "a" * 64)
    with pytest.raises(retry.Refusal, match="journal_transition_invalid"):
        journal.advance("verified")
    assert json.loads(journal.path.read_text())["state"] == "prepared"


def test_private_journal_has_no_original_error_payload_or_account_fields(retry, baseline, private_root):
    journal = retry.AttemptJournal(private_root, baseline, "run", "a" * 64)
    raw = journal.path.read_text()
    assert "PRIVATE" not in raw and "payload" not in raw and "attempt_history" not in raw
    assert journal.path.stat().st_mode & 0o777 == 0o600


def test_exclusive_lock_blocks_a_competing_owner(retry, private_root):
    path = private_root / "operation.lock"
    with retry.exclusive_file(path):
        with pytest.raises(BlockingIOError):
            with retry.exclusive_file(path):
                pytest.fail("cannot share production ownership")


def test_export_comparison_ignores_only_export_time_and_collection_order(retry):
    before = {"format_version": 5, "exported_at": "old", "records": [{"id": "b", "steps": [1, 2]}, {"id": "a"}],
              "vector_data": {"vector_x": [{"id": "a", "vector": [.1, .2]}]}}
    after = deepcopy(before)
    after["exported_at"] = "new"
    after["records"].reverse()
    assert retry.canonical_export(before) == retry.canonical_export(after)
    after["records"][1]["steps"].reverse()
    assert retry.canonical_export(before) != retry.canonical_export(after)
    after = deepcopy(before)
    after["vector_data"]["vector_x"][0]["vector"][0] = .9
    assert retry.canonical_export(before) != retry.canonical_export(after)


@pytest.mark.parametrize("change", ["unrelated_attempt", "unrelated_payload", "dependent_running", "dependent_other_project", "duplicate_key", "failed_selected"])
def test_readback_rejects_queue_side_effects_and_failed_selected(retry, baseline, change):
    unrelated = {"id": "unrelated", "job_key": "unrelated-key", "state": "pending", "attempts": 0, "payload": {"value": 1}}
    before = {"jobs": [baseline, unrelated], "sources": []}
    job = {**baseline, "attempts": 3, "retry_count": 1, "worker": "owned", "state": "running"}
    after = {"jobs": [{**job, "state": "completed"}, deepcopy(unrelated)], "sources": []}
    if change == "unrelated_attempt":
        after["jobs"][1]["attempts"] = 1
    elif change == "unrelated_payload":
        after["jobs"][1]["payload"] = {"value": 2}
    elif change == "dependent_running":
        after["jobs"].append({"id": "new", "job_key": "new", "state": "running", "attempts": 1})
    elif change == "dependent_other_project":
        after["jobs"].append({"id": "new", "job_key": "new", "state": "pending", "attempts": 0, "project_id": "other"})
    elif change == "duplicate_key":
        after["jobs"].append({"id": "new", "job_key": "unique", "state": "pending", "attempts": 0, "project_id": "project"})
    else:
        after["jobs"][0]["state"] = "failed"
    with pytest.raises(retry.Refusal):
        retry.verify_attempt_result(before, after, job)


def test_only_new_unexecuted_dependent_jobs_are_permitted(retry, baseline):
    job = {**baseline, "attempts": 3, "retry_count": 1, "worker": "owned", "state": "running"}
    before = {"jobs": [baseline], "sources": []}
    after = {"jobs": [{**job, "state": "completed"}, {"id": "new", "job_key": "new", "state": "pending", "attempts": 0, "project_id": "project"}], "sources": []}
    result = retry.verify_attempt_result(before, after, job)
    assert result["new_pending_jobs"] == 1 and "PRIVATE" not in repr(result)
    assert not {"payload", "worker", "attempt_history", "error"} & result.keys()


@pytest.mark.parametrize("owns,exit_code", [(False, None), (True, 1)])
def test_production_engine_never_signals_changed_or_already_dead_process(retry, monkeypatch, owns, exit_code):
    engine = retry.ProductionEngine(None, {}, SimpleNamespace(owns=lambda row: owns), None, None, {})
    engine.process = SimpleNamespace(poll=lambda: exit_code)
    engine.row, engine.handle = {"pid": 99999}, 17
    monkeypatch.setattr(retry.signal, "pidfd_send_signal", lambda *args: pytest.fail("must not signal"))
    with pytest.raises(retry.Refusal):
        engine.stop()


def test_engine_shutdown_uses_only_retained_handle_and_actual_exit(retry, monkeypatch):
    signals = []
    engine = retry.ProductionEngine(None, {}, SimpleNamespace(owns=lambda row: True), None, None, {})
    engine.process = SimpleNamespace(poll=lambda: None, wait=lambda timeout: 143)
    engine.row, engine.handle = {"pid": 99999}, 17
    class Poll:
        def register(self, handle, events):
            assert handle == 17
        def poll(self, timeout):
            assert timeout == 30000
            return [(17, 1)]
    monkeypatch.setattr(retry.select, "poll", Poll)
    monkeypatch.setattr(retry.signal, "pidfd_send_signal", lambda *args: signals.append(args))
    monkeypatch.setattr(retry.os, "close", lambda fd: None)
    shim = ModuleType("devbrain.engine")
    shim.runtime_dir = lambda settings: Path("/synthetic")
    monkeypatch.setitem(sys.modules, "devbrain.engine", shim)
    monkeypatch.setattr(retry, "tree_manifest", lambda root: {})
    result = engine.stop()
    assert signals == [(17, retry.signal.SIGTERM)]
    assert result["engine_actual_exit_verified"] and result["graceful_shutdown_requested"]


def test_missing_all_release_proofs_is_rejected_before_loading_any_verifier(retry):
    runtime = {key: "synthetic" for key in retry.RUNTIME_KEYS}
    admission = {"schema_version": 1, "scope": "devbrain_production_retry_admission", "source_final": True,
                 "data_dir": str(retry.PRODUCTION), "version": "0.8.0", "source_hashes": {"a": "b"},
                 "runtime": runtime, "proofs": {}}
    with pytest.raises(retry.Refusal, match="all_release_proofs_required"):
        retry.verify_admission(admission, SimpleNamespace(source_manifest=lambda root: {"a": "b"}), Path("/synthetic"), runtime)


def test_missing_runtime_values_never_compare_equal(retry):
    assert not retry.runtime_equal({}, {})


def test_frontend_receipt_cannot_admit_different_frontend_with_same_backend(retry, tmp_path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text('{"name":"synthetic"}')
    (frontend / "package-lock.json").write_text('{"lockfileVersion":3}')
    component = frontend / "Panel.tsx"
    component.write_text("export const view = 'reviewed';")
    run = {"source_hashes": {"src/devbrain/example.py": "unchanged"},
           "frontend_source_hashes": retry.frontend_manifest(frontend)}
    retry.verify_frontend_source(run, tmp_path)
    component.write_text("export const view = 'changed after qualification';")
    with pytest.raises(retry.Refusal, match="frontend_not_final_source"):
        retry.verify_frontend_source(run, tmp_path)


@pytest.mark.parametrize("name", ["full_suite", "soak"])
def test_saved_attestation_uses_original_qualified_path_not_installed_tree(retry, tmp_path, name):
    qualified, installed = tmp_path / "immutable-qualified", tmp_path / "different-installed-prefix"
    qualified.mkdir()
    installed.mkdir()
    calls = []
    manifest = {"src/devbrain/example.py": "same-bytes"}
    audit = SimpleNamespace(source_manifest=lambda root: manifest,
                            attest_full_suite=lambda root, source: calls.append(("full_suite", source)) or {"passed": True},
                            attest_soak=lambda root, source: calls.append(("soak", source)) or {"passed": True})
    result = retry.recompute_final_attestation(audit, name, tmp_path, {"qualified_source_root": str(qualified)}, manifest)
    assert result == {"passed": True} and calls == [(name, qualified)] and qualified != installed


@pytest.mark.parametrize("inodes,owned,expected", [(["12"], ["12"], True), (["99"], ["12"], False),
                                                  (["12", "99"], ["12"], False), ([], ["12"], False)])
def test_port_probe_cannot_adopt_foreign_listener(retry, tmp_path, monkeypatch, inodes, owned, expected):
    process = tmp_path / "123"
    (process / "fd").mkdir(parents=True)
    (process / "net").mkdir()
    for index, inode in enumerate(owned):
        (process / "fd" / str(index)).write_text("")
    monkeypatch.setattr(retry.os, "readlink", lambda path: "socket:[" + owned[int(path.name)] + "]")
    rows = [f"0: 0100007F:09B0 00000000:0000 0A 0:0 0:0 0 1000 0 {inode}" for inode in inodes]
    (process / "net/tcp").write_text("header\n" + "\n".join(rows))
    (process / "net/tcp6").write_text("header\n")
    assert retry.listener_owned({"pid": 123}, SimpleNamespace(owns=lambda row: True), 2480, proc=tmp_path) is expected


def load_sibling(name):
    spec = importlib.util.spec_from_file_location("registration_" + name.replace("-", "_"), Path(__file__).with_name(name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_new_production_name_requires_its_own_status_dependencies_and_artifacts(retry):
    supervisor = load_sibling("supervise-080.py")
    name = "retry-production-queue-080.py"
    assert supervisor.CONTROLLERS[name] == retry.STATUS
    assert supervisor.CONTROLLER_DEPENDENCIES[name] == ("validate-native-account080.py",)
    assert supervisor.controller_artifacts(name, ["--execute", "--job-id", JOB]) == retry.REQUIRED_ARTIFACTS
    assert supervisor.CONTROLLERS["replay-failed-queue-080.py"] == "queue_copy_completed_pending_external_watch"
    assert supervisor.CONTROLLERS[name] != supervisor.CONTROLLERS["replay-failed-queue-080.py"]


def test_windows_launcher_admits_exact_same_name_dependencies_and_required_reports(retry):
    raw = Path(__file__).with_name("run-supervised-080.ps1").read_text()
    name = "retry-production-queue-080.py"
    def entry(block):
        body = re.search(r"\$" + block + r"\s*=\s*@\{(.*?)\n\}", raw, re.S)
        assert body is not None
        rows = re.findall(r"'" + re.escape(name) + r"'\s*=\s*@\(([^)]*)\)", body[1])
        assert len(rows) == 1
        return re.findall(r"'([^']+)'", rows[0])
    assert entry("controllerDependencies") == ["validate-native-account080.py"]
    assert set(entry("controllerArtifacts")) == retry.REQUIRED_ARTIFACTS
    allowlist = re.search(r"if \(\$controllerName -cnotin @\(([^)]*)\)\) \{ throw 'Unsupported controller\.' \}", raw)
    assert allowlist is not None and re.findall(r"'([^']+)'", allowlist[1]).count(name) == 1


def test_whole_guard_inventory_and_hash_manifest_include_new_code_and_cases(tmp_path):
    runner, supervisor = load_sibling("run-regressions-080.py"), load_sibling("supervise-080.py")
    directory = Path(__file__).parent.resolve()
    modules = runner.guard_modules(directory)
    assert len(modules) == 12 and modules.count("test_production_queue080_guards.py") == 1
    updater = tmp_path / "devbrain-update-0.8.0.py"
    updater.write_text("# synthetic inert candidate\n")
    result = runner.guard_manifest(directory, supervisor, updater.resolve())
    assert result["helper_sha256"]["retry-production-queue-080.py"] == runner.common.sha(directory / "retry-production-queue-080.py")
    assert result["helper_sha256"]["test_production_queue080_guards.py"] == runner.common.sha(directory / "test_production_queue080_guards.py")
    assert not {"test_devbrain_update071_guards.py", "test_devbrain_update072_guards.py"} & set(modules)


@pytest.mark.parametrize("changed", ["validate-native-account080.py", "supervise-080.py", "run-supervised-080.ps1"])
def test_prior_guards_cannot_qualify_later_changed_common_or_supervisor(retry, tmp_path, changed):
    names = {"retry-production-queue-080.py", "test_production_queue080_guards.py", "supervise-080.py",
             "run-supervised-080.ps1", "attest-full-suite-080.py", "validate-native-account080.py",
             "validate-stability-080.py", "qualification-power-080.ps1", "run-regressions-080.py"}
    for name in names:
        (tmp_path / name).write_text("synthetic reviewed bytes")
    hashes = {name: retry.sha(tmp_path / name) for name in names}
    retry.verify_qualified_tools(hashes, tmp_path)
    (tmp_path / changed).write_text("later bytes not covered by saved guards")
    with pytest.raises(retry.Refusal, match="qualified_tool_bundle_changed"):
        retry.verify_qualified_tools(hashes, tmp_path)


def test_self_hash_alone_does_not_authorize_unqualified_production_tool(retry, tmp_path):
    with pytest.raises(retry.Refusal, match="new_production_tool_not_qualified"):
        retry.verify_qualified_tools({"retry-production-queue-080.py": "a" * 64}, tmp_path)


@pytest.mark.parametrize("windows", [False, True])
def test_receipt_attestor_requires_the_same_production_dependency_as_supervisors(monkeypatch, windows):
    audit, supervisor = load_sibling("attest-full-suite-080.py"), load_sibling("supervise-080.py")
    controller = "retry-production-queue-080.py"
    core = {controller, "attest-full-suite-080.py", "validate-stability-080.py", "supervise-080.py", "qualification-power-080.ps1"}
    if windows:
        core.add("run-supervised-080.ps1")
    hashes = {name: "a" * 64 for name in core}
    report = {"helper_sha256_start": dict(hashes), "helper_sha256_end": dict(hashes)}
    monkeypatch.setattr(audit, "digest", lambda path: "a" * 64)
    with pytest.raises(ValueError, match="lack start/end hashes"):
        audit.verify_helper_files(report, controller, windows=windows)
    for name in supervisor.CONTROLLER_DEPENDENCIES[controller]:
        report["helper_sha256_start"][name] = report["helper_sha256_end"][name] = "a" * 64
    audit.verify_helper_files(report, controller, windows=windows)
    monkeypatch.setattr(audit, "digest", lambda path: "b" * 64 if path.name == "validate-native-account080.py" else "a" * 64)
    with pytest.raises(ValueError, match="differs from observed bytes"):
        audit.verify_helper_files(report, controller, windows=windows)


@pytest.mark.parametrize("name", ["service.lock", "engine.lock", "database-writer.lock"])
def test_empty_owner_legacy_0644_locks_work_without_chmod(retry, private_root, monkeypatch, name):
    monkeypatch.setattr(retry, "PRODUCTION", private_root)
    path = private_root / name
    path.touch(mode=0o644)
    path.chmod(0o644)
    before = path.stat()
    with retry.exclusive_file(path):
        assert path.stat().st_ino == before.st_ino and path.stat().st_mode == before.st_mode
    assert path.read_bytes() == b"" and path.stat().st_mode == before.st_mode


@pytest.mark.parametrize("defect", ["mode0666", "nonempty", "wrongname", "nlink", "symlink", "foreignroot"])
def test_legacy_lock_exception_is_narrow_and_never_rewrites_existing_files(retry, private_root, monkeypatch, defect):
    monkeypatch.setattr(retry, "PRODUCTION", private_root)
    path = private_root / ("other.lock" if defect == "wrongname" else "engine.lock")
    path.touch(mode=0o644)
    path.chmod(0o666 if defect == "mode0666" else 0o644)
    if defect == "nonempty":
        path.write_text("synthetic non-lock content")
    elif defect == "nlink":
        retry.os.link(path, private_root / "second-link")
    elif defect == "symlink":
        target = private_root / "real-target"
        path.rename(target)
        path.symlink_to(target)
    elif defect == "foreignroot":
        monkeypatch.setattr(retry, "PRODUCTION", private_root / "somewhere-else")
    before = path.read_bytes()
    mode = path.stat().st_mode
    with pytest.raises((retry.Refusal, OSError)):
        with retry.exclusive_file(path):
            pytest.fail("unsafe lock must not be admitted")
    assert path.read_bytes() == before and path.stat().st_mode == mode


def test_0755_owned_state_parent_gets_private_child_without_parent_chmod(retry, tmp_path):
    tmp_path.chmod(0o755)
    root = tmp_path / "owned-production-retry"
    assert retry.prepare_journal_directory(root) == root
    assert root.stat().st_mode & 0o777 == 0o700
    assert tmp_path.stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize("defect", ["group_writable_parent", "world_writable_parent", "linked_child"])
def test_journal_does_not_create_in_shared_parent_or_adopt_symlink(retry, tmp_path, defect):
    tmp_path.chmod(0o775 if defect == "group_writable_parent" else 0o777 if defect == "world_writable_parent" else 0o700)
    root = tmp_path / "owned-production-retry"
    if defect == "linked_child":
        target = tmp_path / "other"
        target.mkdir(mode=0o700)
        root.symlink_to(target, target_is_directory=True)
    with pytest.raises(retry.Refusal):
        retry.prepare_journal_directory(root)
    assert not root.exists() or root.is_symlink()
