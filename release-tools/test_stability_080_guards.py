"""Pure contract tests: no processes, Java, GPU, database, or real kernel probes."""
from __future__ import annotations

import importlib.util
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest


def load(name):
    path = Path(__file__).with_name(name + ".py")
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def monitor(tmp_path, monkeypatch):
    module = load("validate-stability-080")
    state = {"boot": "boot-a", "kernel": {}}
    monkeypatch.setattr(module, "boot_id", lambda: state["boot"])
    monkeypatch.setattr(module, "kernel_fatals", lambda: dict(state["kernel"]))
    return module, state, tmp_path


def test_old_dump_is_baselined_but_new_short_lived_child_dump_fails(monitor):
    module, _, root = monitor
    (root / "wsl-crash-old.dmp").write_bytes(b"old")
    watch = module.CrashWatch(root)
    assert watch.poll()["native_failure"] is False
    # No PID needs to remain alive for this crash to invalidate qualification.
    (root / "wsl-crash-child-already-exited.dmp").write_bytes(b"new")
    result = watch.poll()
    assert result["native_failure"] is True
    assert result["new_wsl_dumps"] == ["wsl-crash-child-already-exited.dmp"]


def test_cleanup_dump_is_latched_even_if_file_is_later_removed(monitor):
    module, _, root = monitor
    watch = module.CrashWatch(root)
    ticks = []
    def advance(_):
        ticks.append(1)
        if len(ticks) == 5:
            (root / "wsl-crash-cleanup.dmp").write_bytes(b"late")
        elif len(ticks) == 10:
            (root / "wsl-crash-cleanup.dmp").unlink()
    result = watch.finish(30, sleep=advance)
    assert len(ticks) == 30 and result["native_failure"]
    assert result["new_wsl_dumps"] == ["wsl-crash-cleanup.dmp"]


def test_changed_boot_and_kernel_only_fatal_are_failures(monitor):
    module, state, root = monitor
    watch = module.CrashWatch(root)
    state["boot"] = "boot-b"
    state["kernel"]["new"] = "[10.0] Out of memory: Killed process 42"
    result = watch.poll()
    assert result["native_failure"] and result["boot_changed"]
    assert result["new_kernel_fatals"] == [state["kernel"]["new"]]


def test_missing_crash_observation_cannot_be_a_pass(monitor):
    module, _, root = monitor
    watch = module.CrashWatch(root)
    root.rmdir()
    with pytest.raises(RuntimeError, match="unavailable"):
        watch.poll()


@pytest.mark.parametrize("change", ["birth", "executable", "zombie"])
def test_pid_reuse_or_exited_process_is_not_owned_identity(change, monkeypatch):
    module = load("validate-stability-080")
    original = {"pid": 42, "birth": "1", "executable": "/qualified/java", "state": "S"}
    observed = dict(original)
    observed[{"birth": "birth", "executable": "executable", "zombie": "state"}[change]] = "Z" if change == "zombie" else "changed"
    monkeypatch.setattr(module, "process", lambda _: observed)
    assert not module.same_process(original)


def test_source_manifest_includes_tests_and_rejects_external_symlink(tmp_path):
    module = load("soak-workload-080")
    checkout = tmp_path / "checkout"
    (checkout / "src/devbrain").mkdir(parents=True)
    (checkout / "tests").mkdir()
    for name in ("pyproject.toml", "uv.lock", "src/devbrain/__init__.py", "src/devbrain/prompt.txt", "tests/test_example.py"):
        (checkout / name).write_text("initial")
    first = module.source_manifest(checkout)
    assert "src/devbrain/prompt.txt" in first
    (checkout / "tests/test_example.py").write_text("changed")
    assert module.source_manifest(checkout) != first
    external = tmp_path / "external.py"
    external.write_text("outside")
    (checkout / "src/devbrain/linked.py").symlink_to(external)
    with pytest.raises(ValueError, match="symlink"):
        module.source_manifest(checkout)


def attestation():
    runtime = {"python_executable": "/qualified/python", "java_executable": "/qualified/java", "tmux_executable": "/qualified/tmux",
               "python_binary_sha256": "p" * 64, "java_binary_sha256": "j" * 64, "tmux_binary_sha256": "m" * 64}
    manifest = {"src/devbrain/__init__.py": "s" * 64, "tests/test_contract.py": "t" * 64,
                "pyproject.toml": "p" * 64, "uv.lock": "u" * 64}
    gate = {"status": "pass", "scope": "bounded_runtime_qualification", "runtime": runtime,
            "checks": {key: {"pass": True} for key in ("ssl_candidate", "unit", "native_evaluation", "c2_repetitions", "native_children", "engines_stopped")}}
    suite = {"schema_version": 1, "scope": "devbrain_final_full_suite_attestation",
             "counts": {"collected": 2, "failures": 0, "errors": 0, "unexpected_skips": 0, "duplicates": 0, "missing": 0},
             "final_full_suite_pass": True, "runtime_gate_sha256": "gate-sha", "runtime": runtime,
             "source_hashes": manifest, "native_fatal_qualification": {
                 "passed": True, "cleanup_complete": True, "native_failure": False,
                 "initial_boot_id": "a", "final_boot_id": "a", "new_wsl_dumps": [], "new_kernel_fatals": [], "errors": []}}
    return deepcopy(gate), deepcopy(suite), deepcopy(runtime), deepcopy(manifest)


@pytest.mark.parametrize("tamper", ["gate_status", "child_fatal", "missing_check", "python_hash", "java_hash", "tmux_hash", "source", "test_source", "gate_link", "suite_status", "suite_fatal", "suite_cleanup"])
def test_stale_source_runtime_or_native_failure_rejects_even_pytest_zero(tamper):
    module = load("soak-workload-080")
    gate, suite, runtime, manifest = attestation()
    module.verify_attestations(gate, suite, runtime, manifest, "gate-sha")
    suite["pytest_returncode"] = 0
    if tamper == "gate_status":
        gate["status"] = "blocked"
    elif tamper == "child_fatal":
        gate["checks"]["native_children"]["pass"] = False
    elif tamper == "missing_check":
        del gate["checks"]["native_children"]
    elif tamper in {"python_hash", "java_hash", "tmux_hash"}:
        runtime[tamper.replace("_hash", "_binary_sha256")] = "changed"
    elif tamper == "source":
        manifest["src/devbrain/__init__.py"] = "changed"
    elif tamper == "test_source":
        manifest["tests/new_test.py"] = "new"
    elif tamper == "gate_link":
        suite["runtime_gate_sha256"] = "old-gate"
    elif tamper == "suite_fatal":
        suite["native_fatal_qualification"]["new_wsl_dumps"] = ["late-child-core.dmp"]
    elif tamper == "suite_cleanup":
        suite["native_fatal_qualification"]["cleanup_complete"] = False
    else:
        suite["final_full_suite_pass"] = False
    with pytest.raises(ValueError):
        module.verify_attestations(gate, suite, runtime, manifest, "gate-sha")


def test_blocked_gate_is_read_only_and_stops_before_runtime_or_source_access(tmp_path, monkeypatch):
    module = load("soak-workload-080")
    gate, suite = tmp_path / "gate.json", tmp_path / "suite.json"
    gate.write_text('{"status":"blocked","scope":"bounded_runtime_qualification"}')
    suite.write_text('{}')
    before = gate.read_bytes()
    def forbidden(*_):
        raise AssertionError("Must not inspect/create runtime after blocked gate")
    monkeypatch.setattr(module, "source_manifest", forbidden)
    with pytest.raises(ValueError, match="has not passed"):
        module.validate_gate(SimpleNamespace(gate_report=gate, suite_report=suite))
    assert gate.read_bytes() == before


@pytest.mark.parametrize("failure", ["late_dump", "cleanup_error", "boot", "missing_boot", "jvm_alive", "source", "zero_work", "too_few_cycles", "workload_short", "cadence", "late_jvm_fatal", "late_thread_failure", "missing_readback", "missing_reconnect", "missing_gpu", "failed_gpu", "gpu_before_cleanup"])
def test_pass_is_decided_only_after_cleanup_and_real_work(failure):
    module = load("soak-workload-080")
    report = {"status": "workload_complete_pending_cleanup", "initial_boot_id": "a", "final_boot_id": "a",
              "post_cleanup_native_observation": {"native_failure": False}, "cycles": 32, "model_cycles": 32,
              "gpu_monitor": {"passed": True, "final_after_cleanup": True},
              "committed_readback_cycles": 32, "terminal_reconnect_cycles": 32,
              "workload_seconds": 28801, "duration_requested": 28800, "cycle_seconds": 900,
              **dict.fromkeys(["jvm_stopped", "tmux_stopped", "source_unchanged", "runtime_unchanged", "qualification_reports_unchanged", "owned_fatal_reports_absent", "controllers_unchanged"], True)}
    good = module.qualify_completion(deepcopy(report))
    assert good["status"] == "workload_passed_pending_external_watch"
    assert good["workload_criteria_passed"] and good["eight_hour_qualification"] is False
    if failure == "late_dump":
        report["post_cleanup_native_observation"]["native_failure"] = True
    elif failure == "cleanup_error":
        report["cleanup_errors"] = ["signal failure"]
    elif failure == "boot":
        report["final_boot_id"] = "b"
    elif failure == "missing_boot":
        report["initial_boot_id"] = report["final_boot_id"] = None
    elif failure == "jvm_alive":
        report["jvm_stopped"] = False
    elif failure == "source":
        report["source_unchanged"] = False
    elif failure == "too_few_cycles":
        report["cycles"] = report["model_cycles"] = 1
    elif failure == "workload_short":
        report["workload_seconds"] = 10
    elif failure == "cadence":
        report["cycle_seconds"] = 28800
    elif failure == "late_jvm_fatal":
        report["owned_fatal_reports_absent"] = False
    elif failure == "late_thread_failure":
        report["runtime_failure"] = ["Probe failed while stopping"]
    elif failure == "missing_readback":
        report["committed_readback_cycles"] = 31
    elif failure == "missing_reconnect":
        report["terminal_reconnect_cycles"] = 31
    elif failure == "missing_gpu":
        report.pop("gpu_monitor")
    elif failure == "failed_gpu":
        report["gpu_monitor"]["passed"] = False
    elif failure == "gpu_before_cleanup":
        report["gpu_monitor"]["final_after_cleanup"] = False
    else:
        report["cycles"] = report["model_cycles"] = 0
    assert module.qualify_completion(report)["status"] == "failed"


@pytest.mark.parametrize("changed", ["source_content", "source_hash", "record_content", "record_version", "source_ids", "duplicate_record"])
def test_committed_database_readback_checks_actual_values(changed):
    module = load("soak-workload-080")
    source = dict(id="source", project_id="p", uri="soak://1", sha256="hash", content="original")
    record = dict(id="record", project_id="p", version=1, content="expected", source_ids=["source"])
    actual_source, actual_records = deepcopy(source), [deepcopy(record)]
    assert module.require_committed_values(actual_source, actual_records, source, record)["record_version"] == 1
    if changed == "source_content":
        actual_source["content"] = "stale or lost"
    elif changed == "source_hash":
        actual_source["sha256"] = "different"
    elif changed == "duplicate_record":
        actual_records.append(deepcopy(record))
    else:
        key = {"record_content": "content", "record_version": "version", "source_ids": "source_ids"}[changed]
        actual_records[0][key] = None
    with pytest.raises(RuntimeError, match="readback"):
        module.require_committed_values(actual_source, actual_records, source, record)


@pytest.mark.parametrize("changed", ["server", "panes", "selection", "ack", "operation_counts"])
def test_reconnect_preserves_identity_selection_ack_and_does_not_duplicate(changed):
    module = load("soak-workload-080")
    before = {key: "original" for key in ("server", "panes", "selection", "ack", "operation_counts")}
    module.require_reconnect_continuity(before, deepcopy(before))
    after = {**before, changed: "different"}
    with pytest.raises(RuntimeError, match="reconnect"):
        module.require_reconnect_continuity(before, after)


@pytest.mark.parametrize("changed", [False, True])
def test_actual_detach_exit_precedes_fresh_attachment_and_failure_closes_it(changed):
    module = load("soak-workload-080")
    events = []
    old = SimpleNamespace(process=SimpleNamespace(pid=101, wait=lambda **_: events.append("old_exit") or 0),
                          send=lambda value: events.append(value), close=lambda: events.append("old_close"))
    fresh = SimpleNamespace(process=SimpleNamespace(pid=202, poll=lambda: None), close=lambda: events.append("fresh_close"))
    manager = SimpleNamespace(clients=lambda: [{"pid": 202}])
    def factory(*args, **kwargs):
        assert events == [b"\x02d", "old_exit", "old_close"]
        events.append("fresh_attach")
        return fresh
    calls = iter([{"ack": 1}, {"ack": 2 if changed else 1}])
    if changed:
        with pytest.raises(RuntimeError, match="reconnect"):
            module.reconnect_terminal(old, manager, factory, lambda: next(calls))
        assert events[-1] == "fresh_close"
    else:
        result, proof = module.reconnect_terminal(old, manager, factory, lambda: next(calls))
        assert result is fresh and proof["actual_detach_reattach"] and events[-1] == "fresh_attach"


def test_fresh_terminal_death_during_readback_is_failure():
    module = load("soak-workload-080")
    old = SimpleNamespace(process=SimpleNamespace(pid=101, wait=lambda **_: 0), send=lambda _: None, close=lambda: None)
    state = {"dead": False, "closed": False}
    fresh = SimpleNamespace(process=SimpleNamespace(pid=202, poll=lambda: 1 if state["dead"] else None),
                            close=lambda: state.update(closed=True))
    calls = 0
    def readback():
        nonlocal calls
        calls += 1
        if calls == 2:
            state["dead"] = True
        return {"ack": 1}
    with pytest.raises(RuntimeError, match="exited during"):
        module.reconnect_terminal(old, SimpleNamespace(clients=lambda: [{"pid": 202}]), lambda *a, **k: fresh, readback)
    assert state["closed"]


@pytest.mark.parametrize("owned", [True, False])
def test_soak_jvm_shutdown_uses_retained_pidfd_and_actual_exit(monkeypatch, owned):
    module = load("soak-workload-080")
    sent, calls = [], []
    def observed(handles, *_):
        calls.append(handles)
        return ([], [], []) if len(calls) == 1 else (handles, [], [])
    monkeypatch.setattr(module.select, "select", observed)
    monkeypatch.setattr(module.signal, "pidfd_send_signal", lambda *args: sent.append(args))
    monkeypatch.setattr(module.os, "waitpid", lambda *args: (0, 0))
    scope = SimpleNamespace(owns=lambda row: owned)
    if owned:
        assert module.stop_owned_jvm(42, {"pid": 123}, scope)
        assert sent == [(42, module.signal.SIGTERM)] and calls == [[42], [42]]
    else:
        with pytest.raises(RuntimeError, match="changed"):
            module.stop_owned_jvm(42, {"pid": 123}, scope)
        assert not sent


GPU_ROW = "0, GPU-fixture, NVIDIA GeForce RTX 4080, 16376, 4096\n"


@pytest.mark.parametrize("text", ["", "0, GPU-a, NVIDIA RTX 3060, 12000, 1\n",
    "0, GPU-a, NVIDIA RTX 4080, 16376, N/A\n", "0, GPU-a, NVIDIA RTX 4080, nan, 1\n",
    "0, GPU-a, NVIDIA RTX 4080, 16376, -1\n", "0, GPU-a, NVIDIA RTX 4080, 16376, 20000\n",
    GPU_ROW + GPU_ROW, "0, GPU-a, NVIDIA RTX 4080\n"])
def test_required_gpu_rejects_missing_device_or_invalid_vram(text):
    module = load("soak-workload-080")
    with pytest.raises(ValueError):
        module.RequiredGpuMonitor.parse(text)


@pytest.fixture
def gpu_monitor(tmp_path, monkeypatch):
    module = load("soak-workload-080")
    state = {"aborted": False, "time": 0.0, "calls": [], "output": GPU_ROW}
    monkeypatch.setattr(module.time, "monotonic", lambda: state["time"])
    sampler = module.RequiredGpuMonitor(tmp_path / "gpu-samples.jsonl", lambda: state.update(aborted=True))
    sampler.binary = tmp_path / "nvidia-smi"
    sampler.binary.write_bytes(b"fake query binary, never executed")
    sampler.binary_sha256 = module.digest(sampler.binary)
    sampler.stream = sampler.path.open("x", encoding="utf-8")
    def query(argv, **kwargs):
        state["calls"].append((argv, kwargs))
        return SimpleNamespace(stdout=state["output"])
    monkeypatch.setattr(module.subprocess, "run", query)
    try:
        yield module, sampler, state
    finally:
        if not sampler.stream.closed:
            sampler.stream.close()


def test_gpu_probe_is_bounded_read_only_and_zero_is_a_real_measurement(gpu_monitor):
    module, sampler, state = gpu_monitor
    state["output"] = GPU_ROW.replace("4096", "0")
    sampler.sample("initial")
    argv, options = state["calls"][0]
    assert argv == [str(sampler.binary), "--query-gpu=index,uuid,name,memory.total,memory.used",
                    "--format=csv,noheader,nounits"]
    assert options == {"capture_output": True, "text": True, "timeout": 3, "check": True}
    data = module.json.loads(sampler.path.read_text().splitlines()[0])
    assert data["devices"][0]["memory_used_mib"] == 0
    assert data["devices"][0]["memory_total_mib"] == 16376 and not state["aborted"]


@pytest.mark.parametrize("failure", ["timeout", "nonzero", "unavailable"])
def test_gpu_query_failures_remain_failed_after_later_success(gpu_monitor, monkeypatch, failure):
    module, sampler, state = gpu_monitor
    good_query = module.subprocess.run
    error = (module.subprocess.TimeoutExpired("fake-gpu", 3) if failure == "timeout" else
             module.subprocess.CalledProcessError(1, "fake-gpu") if failure == "nonzero" else OSError("missing"))
    def bad_query(*args, **kwargs):
        raise error
    monkeypatch.setattr(module.subprocess, "run", bad_query)
    with pytest.raises(type(error)):
        sampler.sample("periodic")
    assert state["aborted"] and sampler.samples == 0 and sampler.errors
    monkeypatch.setattr(module.subprocess, "run", good_query)
    sampler.sample("recovered")
    result = sampler.finish(0)
    assert result["samples"] == 2 and result["stopped"] and result["final_after_cleanup"]
    assert result["passed"] is False and result["errors"]


@pytest.mark.parametrize("change", ["identity", "sampling_gap", "executable"])
def test_gpu_identity_gap_or_executable_change_cannot_pass(gpu_monitor, change):
    _, sampler, state = gpu_monitor
    sampler.sample("initial")
    if change == "identity":
        state["output"] = GPU_ROW.replace("GPU-fixture", "GPU-replaced")
    elif change == "sampling_gap":
        state["time"] = 16
    else:
        sampler.binary.write_bytes(b"changed")
    with pytest.raises(RuntimeError):
        sampler.sample("periodic")
    assert state["aborted"] and sampler.errors and sampler.samples == 1


def test_required_gpu_missing_binary_fails_before_monitor_thread(tmp_path, monkeypatch):
    module = load("soak-workload-080")
    aborted = []
    sampler = module.RequiredGpuMonitor(tmp_path / "gpu.jsonl", lambda: aborted.append(True))
    monkeypatch.setattr(module.Path, "is_file", lambda _: False)
    monkeypatch.setattr(module.shutil, "which", lambda _: None)
    try:
        with pytest.raises(RuntimeError, match="unavailable"):
            sampler.start()
        assert aborted == [True] and sampler.thread.ident is None
        assert sampler.finish(0)["passed"] is False
    finally:
        if sampler.stream is not None and not sampler.stream.closed:
            sampler.stream.close()


def test_gpu_monitor_cannot_close_or_pass_while_probe_thread_is_alive(gpu_monitor):
    _, sampler, state = gpu_monitor
    joined = []
    sampler.thread = SimpleNamespace(ident=1, join=lambda timeout: joined.append(timeout), is_alive=lambda: True)
    result = sampler.finish(0)
    assert joined == [5] and not result["passed"] and not result["stopped"]
    assert "gpu_monitor_did_not_stop" in result["errors"]
    assert not sampler.stream.closed and not state["calls"]


def test_gpu_monitor_samples_cleanup_and_final_after_thread_exit(gpu_monitor):
    module, sampler, state = gpu_monitor
    sampler.sample("initial")
    for elapsed in range(5, 36, 5):
        state["time"] = elapsed
        sampler.sample("periodic")  # covers shutdown and the native-watch grace
    joined = []
    alive = {"value": True}
    def join(timeout):
        assert sampler.stop_event.is_set()
        joined.append(timeout)
        alive["value"] = False
    sampler.thread = SimpleNamespace(ident=1, join=join, is_alive=lambda: alive["value"])
    state["time"] = 36
    result = sampler.finish(36)
    rows = [module.json.loads(line) for line in sampler.path.read_text().splitlines()]
    assert joined == [5] and sampler.stream.closed
    assert result["passed"] and result["final_after_cleanup"] and result["samples"] == 9
    assert rows[-1]["stage"] == "post_cleanup_final" and rows[-1]["elapsed_seconds"] >= 36
    assert result["samples_sha256"] == module.digest(sampler.path)


def test_background_gpu_failure_requests_workload_stop(gpu_monitor, monkeypatch):
    module, sampler, state = gpu_monitor
    monkeypatch.setattr(sampler.stop_event, "wait", lambda _: False)
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("missing")))
    sampler._run()
    assert state["aborted"] and sampler.errors and sampler.samples == 0


def test_missing_gpu_sample_artifact_prevents_monitor_pass(gpu_monitor):
    _, sampler, _ = gpu_monitor
    sampler.sample("initial")
    sampler.path.unlink()  # retained stream alone is not durable path evidence
    result = sampler.finish(0)
    assert result["samples"] == 2 and result["final_after_cleanup"]
    assert result["samples_sha256"] is None and result["passed"] is False


@pytest.mark.parametrize("change", ["monitor_failed", "before_cleanup", "thread_alive", "errors", "identity",
    "sampling_gap", "interval", "missing_artifact", "changed_artifact", "controller_hash", "receipt_hash"])
def test_soak_attestation_binds_required_gpu_evidence_through_external_receipt(tmp_path, change):
    module = load("attest-full-suite-080")
    path = tmp_path / "gpu-samples.jsonl"
    path.write_text('{"stage":"initial"}\n{"stage":"post_cleanup_final"}\n')
    checksum = module.digest(path)
    gpu = {"passed": True, "stopped": True, "final_after_cleanup": True, "errors": [], "samples": 2,
           "interval_seconds": 5, "query_timeout_seconds": 3, "maximum_gap_seconds": 15,
           "observed_maximum_gap_seconds": 5,
           "devices": [{"name": "NVIDIA GeForce RTX 4080", "uuid": "GPU-fixture"}],
           "samples_artifact": "gpu-samples.jsonl", "samples_sha256": checksum}
    report, receipt = {"gpu_monitor": gpu}, {"artifact_sha256": {"gpu-samples.jsonl": checksum}}
    module.verify_soak_gpu_observation(tmp_path, report, receipt)
    if change == "monitor_failed":
        gpu["passed"] = False
    elif change == "before_cleanup":
        gpu["final_after_cleanup"] = False
    elif change == "thread_alive":
        gpu["stopped"] = False
    elif change == "errors":
        gpu["errors"] = ["gpu_sample_TimeoutExpired"]
    elif change == "identity":
        gpu["devices"] = [{"name": "unavailable", "uuid": "GPU-fixture"}]
    elif change == "sampling_gap":
        gpu["observed_maximum_gap_seconds"] = 16
    elif change == "interval":
        gpu["interval_seconds"] = 60
    elif change == "missing_artifact":
        path.unlink()
    elif change == "changed_artifact":
        path.write_text("changed after the controller and external watcher exited\n")
    elif change == "controller_hash":
        gpu["samples_sha256"] = "0" * 64
    else:
        receipt["artifact_sha256"]["gpu-samples.jsonl"] = "0" * 64
    with pytest.raises(ValueError):
        module.verify_soak_gpu_observation(tmp_path, report, receipt)
