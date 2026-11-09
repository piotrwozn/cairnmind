"""Explicitly gated active soak: isolated ArcadeDB, real TUI, local model calls.

Preparing this script does not start a workload. --execute, a passed runtime gate,
a final full-suite attestation and --enable-local-models are all required to run.
Only synthetic data is used. Production settings, services and accounts are unused.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import importlib.util
import json
import math
import os
import secrets
import select
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit


def document(path):
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise TypeError(f"Expected an object in {path.name}")
    return value


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_manifest(root):
    return load_attestor().source_manifest(root)


def load_attestor():
    spec = importlib.util.spec_from_file_location("soak_attestation", Path(__file__).with_name("attest-full-suite-080.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_attestations(gate, suite, runtime, manifest, gate_sha):
    if gate.get("status") != "pass" or gate.get("scope") != "bounded_runtime_qualification":
        raise ValueError("Runtime qualification has not passed")
    required_checks = {"ssl_candidate", "unit", "native_evaluation", "c2_repetitions", "native_children", "engines_stopped"}
    if (not required_checks <= gate.get("checks", {}).keys()
            or any(value.get("pass") is not True for value in gate["checks"].values())):
        raise ValueError("Every runtime qualification check must explicitly pass")
    if suite.get("final_full_suite_pass") is not True:
        raise ValueError("A final_full_suite_pass=true report is required")
    if suite.get("schema_version") != 1 or suite.get("scope") != "devbrain_final_full_suite_attestation":
        raise ValueError("The final suite must be produced by the full-suite attestor")
    counts = suite.get("counts", {})
    if (not isinstance(counts.get("collected"), int) or counts["collected"] <= 0
            or any(counts.get(key) != 0 for key in ("failures", "errors", "unexpected_skips", "duplicates", "missing"))):
        raise ValueError("The final suite is not complete and clean")
    if suite.get("runtime_gate_sha256") != gate_sha:
        raise ValueError("Final suite is not bound to this exact runtime gate")
    native = suite.get("native_fatal_qualification", {})
    if (native.get("passed") is not True or native.get("cleanup_complete") is not True
            or native.get("native_failure") is not False or not native.get("initial_boot_id")
            or native.get("initial_boot_id") != native.get("final_boot_id")
            or any(native.get(key) != [] for key in ("new_wsl_dumps", "new_kernel_fatals", "errors"))):
        raise ValueError("Final suite requires crash-free observation through cleanup, regardless of pytest exit code")
    if not manifest or suite.get("source_hashes") != manifest:
        raise ValueError("Final suite hashes do not match the complete current source/test checkout")
    for key, value in runtime.items():
        if gate.get("runtime", {}).get(key) != value or suite.get("runtime", {}).get(key) != value:
            raise ValueError("Runtime identity/hash differs from gate or final suite: " + key)


def validate_gate(args):
    gate, suite = document(args.gate_report), document(args.suite_report)
    # Check blocked status before probing runtime paths or creating any state.
    if gate.get("status") != "pass" or gate.get("scope") != "bounded_runtime_qualification":
        raise ValueError("Runtime qualification has not passed")
    if any(os.environ.get(key) for key in ("LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONHOME", "PYTHONPATH")):
        raise ValueError("Runtime injection overrides must be absent for qualification")
    python = Path(sys.executable).resolve(strict=True)
    java = (args.java_home / "bin/java").resolve(strict=True)
    tmux = args.tmux_bin.resolve(strict=True)
    runtime = {"python_executable": str(python), "java_executable": str(java), "tmux_executable": str(tmux),
               "python_binary_sha256": digest(python), "java_binary_sha256": digest(java), "tmux_binary_sha256": digest(tmux)}
    manifest = source_manifest(args.source_root)
    gate_sha = digest(args.gate_report)
    verify_attestations(gate, suite, runtime, manifest, gate_sha)
    guard = load_attestor()
    if suite != guard.attest_full_suite(Path(suite["artifact_root"]), args.source_root):
        raise ValueError("Final-suite artifacts no longer validate against their external receipt")
    probe_code = ("import hashlib,importlib.util,json,sys;from pathlib import Path;"
                  "p=Path(importlib.util.find_spec('devbrain').origin).resolve();"
                  "print(json.dumps({'origin':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),"
                  "'python_executable':str(Path(sys.executable).resolve()),'python_prefix':str(Path(sys.prefix).resolve())}))")
    probe = subprocess.run([sys.executable, "-I", "-B", "-c", probe_code], capture_output=True, text=True, timeout=15, check=True)
    isolated_package = json.loads(probe.stdout)
    if isolated_package != suite.get("isolated_package"):
        raise ValueError("Isolated panel child resolves a different package or installation than the final suite")
    if not args.enable_local_models:
        raise ValueError("Active soak requires explicit --enable-local-models")
    endpoint = urlsplit(args.ollama_url)
    if endpoint.scheme != "http" or endpoint.hostname not in {"127.0.0.1", "::1", "localhost"} or endpoint.username or endpoint.password:
        raise ValueError("Only a local unauthenticated Ollama endpoint is allowed")
    return {"runtime_gate_sha256": gate_sha, "suite_report_sha256": digest(args.suite_report),
            "controller_hashes": {name: digest(Path(__file__).with_name(name))
                                  for name in ("soak-workload-080.py", "validate-stability-080.py", "attest-full-suite-080.py")},
            "isolated_package": isolated_package,
            "qualified_runtime": runtime, "qualified_source_hashes": manifest}


def load_monitor():
    path = Path(__file__).with_name("validate-stability-080.py")
    spec = importlib.util.spec_from_file_location("devbrain_soak_monitor", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RequiredGpuMonitor:
    """Read only device/VRAM metadata, including the entire cleanup interval."""
    INTERVAL = 5
    QUERY_TIMEOUT = 3
    MAXIMUM_GAP = 15
    FIELDS = "index,uuid,name,memory.total,memory.used"

    def __init__(self, path, abort):
        self.path, self.abort = path, abort
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="soak-required-gpu", daemon=True)
        self.stream = self.binary = self.binary_sha256 = self.identity = None
        self.started = time.monotonic()
        self.last_sample = None
        self.maximum_gap = 0.0
        self.samples = 0
        self.errors = []

    @staticmethod
    def parse(text):
        rows = []
        for values in csv.reader(text.splitlines()):
            if len(values) != 5:
                raise ValueError("Invalid GPU metadata shape")
            index, uuid, name, total, used = (value.strip() for value in values)
            total, used = float(total), float(used)
            if (not index.isdecimal() or not uuid.startswith("GPU-") or not name
                    or not math.isfinite(total) or not math.isfinite(used)
                    or total <= 0 or not 0 <= used <= total):
                raise ValueError("Invalid GPU identity or VRAM measurement")
            rows.append({"index": int(index), "uuid": uuid, "name": name,
                         "memory_total_mib": total, "memory_used_mib": used})
        if (not any("rtx 4080" in row["name"].lower() for row in rows)
                or len({row["uuid"] for row in rows}) != len(rows)
                or len({row["index"] for row in rows}) != len(rows)):
            raise ValueError("Required NVIDIA RTX4080 identity is unavailable")
        return sorted(rows, key=lambda row: row["index"])

    def _write(self, row):
        self.stream.write(json.dumps(row, allow_nan=False) + "\n")
        self.stream.flush()
        os.fsync(self.stream.fileno())

    def sample(self, stage):
        try:
            if digest(self.binary) != self.binary_sha256:
                raise RuntimeError("GPU query executable changed")
            result = subprocess.run([str(self.binary), "--query-gpu=" + self.FIELDS,
                                     "--format=csv,noheader,nounits"], capture_output=True,
                                    text=True, timeout=self.QUERY_TIMEOUT, check=True)
            rows = self.parse(result.stdout)
            identity = [{key: row[key] for key in ("index", "uuid", "name", "memory_total_mib")} for row in rows]
            if self.identity is not None and identity != self.identity:
                raise RuntimeError("GPU identity disappeared or changed")
            observed = time.monotonic()
            gap = 0 if self.last_sample is None else observed - self.last_sample
            if gap > self.MAXIMUM_GAP:
                raise RuntimeError("GPU sampling continuity was lost")
            self._write({"kind": "gpu_sample", "stage": stage,
                         "elapsed_seconds": observed - self.started, "time": time.time(), "devices": rows})
            self.identity = identity
            self.last_sample = observed
            self.maximum_gap = max(self.maximum_gap, gap)
            self.samples += 1
        except Exception as exc:
            self.errors.append("gpu_sample_" + type(exc).__name__)
            self.abort()
            raise

    def start(self):
        self.stream = self.path.open("x", encoding="utf-8")
        candidate = Path("/usr/lib/wsl/lib/nvidia-smi")
        found = str(candidate) if candidate.is_file() else shutil.which("nvidia-smi")
        if not found:
            self.errors.append("gpu_query_executable_missing")
            self.abort()
            raise RuntimeError("Required NVIDIA GPU sampler is unavailable")
        self.binary = Path(found).resolve(strict=True)
        self.binary_sha256 = digest(self.binary)
        self.sample("initial")
        self.thread.start()

    def _run(self):
        while not self.stop_event.wait(self.INTERVAL):
            try:
                self.sample("periodic")
            except Exception:
                return  # failure is latched; no missing sample becomes a zero

    def finish(self, post_cleanup_at):
        self.stop_event.set()
        if self.thread.ident is not None:
            self.thread.join(self.QUERY_TIMEOUT + 2)
        stopped = not self.thread.is_alive()
        if not stopped:
            self.errors.append("gpu_monitor_did_not_stop")
        final_after_cleanup = False
        if stopped and self.binary is not None:
            try:
                self.sample("post_cleanup_final")
                final_after_cleanup = self.last_sample >= post_cleanup_at
            except Exception:
                pass
        if stopped and self.stream is not None:
            try:
                self.stream.close()
            except OSError:
                self.errors.append("gpu_samples_close_failed")
        artifact_sha = digest(self.path) if stopped and self.path.is_file() else None
        return {"passed": stopped and not self.errors and self.samples >= 2 and final_after_cleanup and artifact_sha is not None,
                "stopped": stopped, "errors": list(self.errors), "samples": self.samples,
                "interval_seconds": self.INTERVAL, "query_timeout_seconds": self.QUERY_TIMEOUT,
                "maximum_gap_seconds": self.MAXIMUM_GAP, "observed_maximum_gap_seconds": self.maximum_gap,
                "final_after_cleanup": final_after_cleanup, "devices": self.identity,
                "executable": str(self.binary) if self.binary else None, "binary_sha256": self.binary_sha256,
                "samples_artifact": self.path.name, "samples_sha256": artifact_sha}


def qualify_completion(report):
    """A zero process exit or elapsed clock alone is never a soak pass."""
    duration = report.get("duration_requested")
    valid_duration = isinstance(duration, (int, float)) and math.isfinite(duration) and duration > 0
    required_cycles = max(1, math.ceil(duration / 900)) if valid_duration else float("inf")
    required = ("jvm_stopped", "tmux_stopped", "source_unchanged", "runtime_unchanged", "qualification_reports_unchanged", "owned_fatal_reports_absent", "controllers_unchanged")
    eligible = (report.get("status") == "workload_complete_pending_cleanup"
                and not report.get("cleanup_errors")
                and not report.get("runtime_failure")
                and report.get("post_cleanup_native_observation", {}).get("native_failure") is False
                and report.get("gpu_monitor", {}).get("passed") is True
                and report.get("gpu_monitor", {}).get("final_after_cleanup") is True
                and bool(report.get("initial_boot_id"))
                and report.get("initial_boot_id") == report.get("final_boot_id")
                and all(report.get(key) is True for key in required)
                and report.get("cycle_seconds") == 900
                and valid_duration
                and report.get("cycles", 0) >= required_cycles
                and report.get("model_cycles") == report.get("cycles")
                and report.get("committed_readback_cycles") == report.get("cycles")
                and report.get("terminal_reconnect_cycles") == report.get("cycles")
                and report.get("workload_seconds", 0) >= duration)
    if eligible:
        report["status"] = "workload_passed_pending_external_watch"
        report["workload_criteria_passed"] = True
        report["eight_hour_qualification"] = False
    elif report.get("status") != "interrupted":
        report["status"] = "failed"
        report["workload_criteria_passed"] = False
        report["eight_hour_qualification"] = False
    return report


def require_committed_values(actual_source, actual_records, expected_source, expected_record):
    """Validate fresh database reads, not the objects returned by the writes."""
    source_fields = ("id", "project_id", "uri", "sha256", "content")
    record_fields = ("id", "project_id", "version", "content", "source_ids")
    if (not actual_source or any(actual_source.get(key) != expected_source[key] for key in source_fields)
            or len(actual_records) != 1
            or any(actual_records[0].get(key) != expected_record[key] for key in record_fields)):
        raise RuntimeError("Committed synthetic source/record readback differs")
    return {"source_id": actual_source["id"], "source_sha256": actual_source["sha256"],
            "record_id": actual_records[0]["id"], "record_version": actual_records[0]["version"]}


def require_reconnect_continuity(before, after):
    if before != after:
        raise RuntimeError("Terminal reconnect changed server/panes, selection, ACK or operation counts")


def reconnect_terminal(terminal, manager, terminal_factory, continuity, *, size=(180, 55)):
    """Exercise actual prefix-d, wait/reap old client, then attach a fresh PTY."""
    old_pid = terminal.process.pid
    try:
        before = continuity()
        terminal.send(b"\x02d")
        if terminal.process.wait(timeout=5) != 0:
            raise RuntimeError("Detached terminal client did not exit cleanly")
    finally:
        terminal.close()  # success: already exited; failure is never qualified
    fresh = terminal_factory(manager, size, readonly=False)
    try:
        deadline = time.monotonic() + 5
        while not any(row["pid"] == fresh.process.pid for row in manager.clients()):
            if fresh.process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("Fresh terminal client did not attach")
            time.sleep(.05)
        require_reconnect_continuity(before, continuity())
        if fresh.process.poll() is not None or not any(row["pid"] == fresh.process.pid for row in manager.clients()):
            raise RuntimeError("Fresh terminal client exited during continuity readback")
        return fresh, {"old_client_pid": old_pid, "old_client_exit": 0,
                       "new_client_pid": fresh.process.pid, "actual_detach_reattach": True,
                       "pane_server_selection_ack_and_counts_preserved": True}
    except BaseException:
        fresh.close()
        raise


def stop_owned_jvm(handle, row, scope):
    if select.select([handle], [], [], 0)[0] or not scope.owns(row):
        raise RuntimeError("Owned JVM exited or changed before shutdown")
    signal.pidfd_send_signal(handle, signal.SIGTERM)
    if not select.select([handle], [], [], 30)[0]:
        raise RuntimeError("Owned JVM did not complete graceful shutdown")
    try:
        pid, status = os.waitpid(row["pid"], os.WNOHANG)
    except ChildProcessError:
        pid = 0  # a retained Popen reaper may already have collected the child
    if pid and os.waitstatus_to_exitcode(status) not in (0, 143, -15):
        raise RuntimeError("Owned JVM shutdown returned an unexpected result")
    return True


def run(args):
    if os.environ.get("DEVBRAIN_QUALIFICATION_RUN_ID") != args.run_id:
        raise ValueError("Run the active soak through the external supervised wrapper")
    qualification = validate_gate(args)
    artifact_root = args.artifact_root.resolve(strict=True)
    if not artifact_root.is_relative_to(Path("/mnt/c")) or (artifact_root / "controller-report.json").exists():
        raise ValueError("A fresh Windows-backed artifact directory is required")
    spec = importlib.util.spec_from_file_location("soak_supervision", Path(__file__).with_name("supervise-080.py"))
    supervision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(supervision)
    proof = supervision.read_json(artifact_root / "mapping-proof.json")
    if proof.get("run_id") != args.run_id:
        raise ValueError("Soak scope proof belongs to another run")
    owned_scope = supervision.ScopeOwnership(proof.get("scope_unit"))
    for original, name in ((args.gate_report, "gate.json"), (args.suite_report, "final-full-suite.json")):
        target = artifact_root / name
        if target.exists() and target.read_bytes() != original.read_bytes():
            raise ValueError("Wrapper/controller evidence inputs differ: " + name)
        if not target.exists():
            target.write_bytes(original.read_bytes())
    monitor = load_monitor()
    watch = monitor.CrashWatch(args.wsl_crash_dir)
    from devbrain.engine import engine_status, install_engine, start_engine
    from devbrain.memory import MemoryManager, inspect_snapshot
    from devbrain.ollama import Ollama
    from devbrain.sessions import SessionManager
    from devbrain.store import Store, _clean
    from devbrain.terminal_preview import (
        FixtureData,
        PreviewBroker,
        PreviewServer,
        PreviewTerminal,
        PreviewWorkspace,
        capture_workspace,
        preview_settings,
        source_hashes,
    )

    settings = preview_settings(args.data_dir, create=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        settings.arcade_url = "http://127.0.0.1:" + str(sock.getsockname()[1])
    settings.arcade_password = secrets.token_urlsafe(32)
    settings.arcade_database = "devbrain_soak080_" + secrets.token_hex(8)
    settings.java_executable = str((args.java_home / "bin/java").resolve())
    settings.tmux_executable = str(args.tmux_bin.resolve())
    settings.ollama_url = args.ollama_url
    settings.chat_model, settings.embedding_model = args.chat_model, args.embedding_model
    settings.answer_tokens, settings.num_ctx, settings.ask_timeout = 64, 2048, 180
    settings.save()
    root = settings.data_dir
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    identity = monitor.process(os.getpid())
    report = {"schema_version": 1, "scope": "devbrain_active_soak_run", "run_id": args.run_id,
              "python_launcher": str(Path(sys.executable).absolute()),
              "runtime": qualification["qualified_runtime"], "controller_exit_verified": False, "cleanup_complete": False,
              "status": "starting", "started": time.time(), "duration_requested": args.duration,
              "cycle_seconds": args.cycle_seconds, "python": sys.version, "source_hashes": source_hashes(),
              "java_executable": settings.java_executable, "database": settings.arcade_database,
              "data_scope": "synthetic_data_in_owned_real_database", "models": [args.chat_model, args.embedding_model],
              "cycles": 0, "model_cycles": 0, "committed_readback_cycles": 0,
              "terminal_reconnect_cycles": 0, **qualification}
    report["initial_boot_id"] = watch.initial_boot_id
    report["initial_core_inventory_count"] = len(watch.initial_files)
    report["eight_hour_qualification"] = False
    summary_path = artifact_root / "controller-report.json"
    summary_path.write_text(json.dumps(report, indent=2))
    events = (root / "workload.jsonl").open("x")
    event_lock = threading.Lock()
    def emit(kind, **data):
        with event_lock:
            events.write(json.dumps({"time": time.time(), "kind": kind, **data}, ensure_ascii=False) + "\n")
            events.flush()
    manager = terminal = server = broker = store = None
    supervisor = health_thread = None
    runtime_failure = []
    jvm_identity = tmux_identity = None
    jvm_handle = jvm_row = None
    started = None
    gpu_monitor = RequiredGpuMonitor(artifact_root / "gpu-samples.jsonl", stopped.set)

    class StoreProjection(FixtureData):
        """The TUI reads a refreshed projection of committed real database rows."""
        def publish(self):
            self.projects = store.list_projects()
            self.records = {p["id"]: store.list_records(p["id"]) for p in self.projects}
            self.sources = {p["id"]: [_clean(row) for row in store.query(
                "SELECT FROM sources WHERE project_id=:project", {"project": p["id"]})] for p in self.projects}
            self.revision += 1

        def read(self, path, params):
            if path.startswith("/api/inspector/sources/"):
                identifier = path.rsplit("/", 1)[-1]
                row = next((s for s in self.sources.get(params.get("project"), []) if s["id"] == identifier), None)
                if row:
                    return {"source": {key: value for key, value in row.items() if key != "content"},
                            "text": row["content"], "next_offset": None}
            return super().read(path, params)

    async def model_cycle(number):
        client = Ollama(settings)
        vector = await client.embed([f"Synthetic DevBrain soak cycle {number}; evidence remains local."])
        reply = await client.chat([{"role": "user", "content": "Synthetic stability check. Reply with the word READY."}])
        content = reply["message"].get("content", "")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("Local chat returned no text")
        return {"embedding_dimension": len(vector[0]), "response_sha256": hashlib.sha256(content.encode()).hexdigest(),
                "input_tokens": reply.get("prompt_eval_count"), "output_tokens": reply.get("eval_count")}

    try:
        gpu_monitor.start()
        # Clearing inherited storage overrides is process-local. No shell profile
        # or production configuration is modified.
        for key in ("DEVBRAIN_ARCADE_URL", "DEVBRAIN_ARCADE_USER", "DEVBRAIN_ARCADE_PASSWORD",
                    "DEVBRAIN_ARCADE_DATABASE", "DEVBRAIN_ARCADE_DB_PREFIX", "JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS"):
            os.environ.pop(key, None)
        install_engine(settings, args.arcade_dist)
        start_engine(settings)
        jvm_identity = monitor.owned_jvm(root, settings.java_executable)
        jvm_row = supervision.identity(jvm_identity["pid"])
        if not jvm_row or not owned_scope.owns(jvm_row):
            raise RuntimeError("Started JVM is outside the owned scope")
        jvm_handle = os.pidfd_open(jvm_row["pid"])
        if not owned_scope.owns(jvm_row):
            raise RuntimeError("JVM identity changed during admission")
        report["jvm_identity"] = jvm_identity
        store = Store(root, settings=settings)
        project_root = root / "synthetic-project"
        project_root.mkdir()
        (project_root / "fixture.py").write_text("SYNTHETIC_SOAK = True\n")
        for command in (["git", "init", "-q", str(project_root)],
                        ["git", "-C", str(project_root), "add", "fixture.py"],
                        ["git", "-C", str(project_root), "-c", "user.name=DevBrain Soak", "-c",
                         "user.email=soak@example.invalid", "commit", "-qm", "Synthetic soak fixture"]):
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=15)
        project = store.add_project("Synthetic 0.8.0 stability", project_root)
        fixture = StoreProjection(settings, "normal")
        fixture.publish()
        server = PreviewServer(settings, "normal", fixture=fixture).start()
        broker = PreviewBroker(settings)
        broker.ensure()
        manager = PreviewWorkspace(settings, broker=broker)
        manager.create(start_supervisor=False)
        tmux_pid = int(manager.tmux("display-message", "-p", "#{pid}").stdout.strip())
        tmux_identity = monitor.process(tmux_pid)
        if not tmux_identity or tmux_identity["executable"] != qualification["qualified_runtime"]["tmux_executable"]:
            raise RuntimeError("Cannot establish owned tmux process identity")
        report["tmux_identity"] = tmux_identity
        manager.state.select_project(project, intent="inspect")
        manager.refresh()
        terminal = PreviewTerminal(manager, readonly=False)
        status = {"pid": os.getpid(), "birth": identity["birth"], "socket": str(manager.socket), "status": "running",
                  "data_scope": report["data_scope"], "source_hashes": report["source_hashes"]}
        (root / "preview-status.json").write_text(json.dumps(status))

        def reconcile():
            try:
                while not stopped.wait(.5):
                    manager.refresh()
            except Exception as exc:  # noqa: BLE001 - report any owned worker failure and stop the soak
                runtime_failure.append(type(exc).__name__)
                stopped.set()
        supervisor = threading.Thread(target=reconcile, daemon=True)
        supervisor.start()
        def observe_health():
            try:
                while not stopped.is_set():
                    sample = monitor.snapshot(os.getpid(), identity["birth"], root, tmux_binary=settings.tmux_executable)
                    sample["native_failures"] = watch.poll()
                    sample["jvm_identity_matches"] = monitor.same_process(jvm_identity)
                    emit("health", **sample)
                    if not sample["root_alive"] or sample.get("tmux_status") != 0 or any(p[2] == "1" for p in sample["panes"]):
                        raise RuntimeError("Owned runtime or TUI pane exited")
                    if not engine_status(settings).get("available"):
                        raise RuntimeError("Owned ArcadeDB became unavailable")
                    if sample.get("fatal_reports"):
                        raise RuntimeError("JVM fatal report appeared")
                    if sample["native_failures"]["native_failure"] or not sample["jvm_identity_matches"]:
                        raise RuntimeError("Native failure, changed boot, or replaced JVM detected")
                    if stopped.wait(30):
                        break
            except Exception as exc:  # noqa: BLE001 - failures during shutdown still invalidate qualification
                runtime_failure.append(type(exc).__name__ + ": " + str(exc))
                stopped.set()
        health_thread = threading.Thread(target=observe_health, daemon=True)
        health_thread.start()
        started = time.monotonic()
        next_cycle = started
        report["status"] = "running"
        summary_path.write_text(json.dumps(report, indent=2))
        while not stopped.is_set() and time.monotonic() - started < args.duration:
            now = time.monotonic()
            if now >= next_cycle:
                number = report["cycles"] + 1
                cycle_start = time.monotonic()
                source_text = f"Synthetic evidence cycle {number}"
                source = store.snapshot(project["id"], f"soak://cycle/{number}", source_text, "manual")
                record = store.remember(project["id"], {"kind": "decision", "key": f"cycle-{number}",
                                        "content": f"Verify synthetic evidence cycle {number}"}, [source["id"]])
                expected_source = {**source, "project_id": project["id"], "uri": f"soak://cycle/{number}",
                                   "content": source_text, "sha256": hashlib.sha256(source_text.encode()).hexdigest()}
                expected_record = {**record, "project_id": project["id"], "version": 1,
                                   "content": f"Verify synthetic evidence cycle {number}", "source_ids": [source["id"]]}
                def committed_readback():
                    if store.query("SELECT count(*) AS total FROM sources WHERE project_id=:project AND uri=:uri",
                                   {"project": project["id"], "uri": expected_source["uri"]})[0]["total"] != 1:
                        raise RuntimeError("Synthetic source was duplicated")
                    return require_committed_values(store.get_source(source["id"]), store.query(
                        "SELECT FROM records WHERE id=:id AND project_id=:project",
                        {"id": record["id"], "project": project["id"]}), expected_source, expected_record)
                committed_readback()
                sessions = SessionManager(store, settings)
                prepared = sessions.prepare(project["id"], "api", timeout=0)
                if prepared.get("status") != "ready":
                    raise RuntimeError("Resume packet not ready")
                current = sessions.resume_context(prepared["session_id"])
                packet = current["packet"]
                readback = {key: ({"id": packet[field]["id"], "version": packet[field]["version"]} if packet.get(field) else None)
                            for key, field in (("plan", "active_plan"), ("task", "active_task"))}
                sessions.ack(prepared["session_id"], packet["id"], packet["hash"], readback)
                if not sessions.status(prepared["session_id"])["acknowledged"]:
                    raise RuntimeError("Exact ACK was not retained")
                snapshot = MemoryManager(store).snapshot(project["id"], str(project_root))
                inspected = inspect_snapshot(snapshot["path"])
                if inspected["project"]["id"] != project["id"]:
                    raise RuntimeError("Snapshot identity changed")
                fixture.publish()
                models = asyncio.run(model_cycle(number))
                if stopped.is_set():
                    break
                report["model_cycles"] += 1
                # A controlled broker outage affects only this fixture. The
                # underlying database and unrelated processes are never stopped.
                before = {role: manager.tmux("display-message", "-p", "-t", pane, "#{pane_pid}").stdout
                          for role, pane in manager.panes().items()}
                fixture.scenario = "offline"
                stopped.wait(3)
                fixture.scenario = "normal"
                broker.reconnect()
                manager.refresh()
                for width, height in ((80, 24), (120, 36), (180, 55)):
                    terminal.resize(width, height)
                    stopped.wait(.3)
                    manager.refresh()
                    for key in ("p", "i", "g"):
                        terminal.send(b"\x02" + key.encode())
                        stopped.wait(.1)
                after = {role: manager.tmux("display-message", "-p", "-t", pane, "#{pane_pid}").stdout
                         for role, pane in manager.panes().items()}
                if before != after:
                    raise RuntimeError("UI process identity changed during controlled reconnect/resize")
                def continuity():
                    state = manager.state.read()
                    selection = {key: state.get(key) for key in ("project_id", "worktree", "run_ids", "selected_entity",
                                                                "work_request", "handled_work_request")}
                    def process_key(pid):
                        row = monitor.process(pid)
                        if not row:
                            raise RuntimeError("Owned terminal process exited before continuity readback")
                        return {key: row[key] for key in ("pid", "birth", "executable")}
                    panes = {role: {"pane": pane, "process": process_key(int(manager.tmux(
                        "display-message", "-p", "-t", pane, "#{pane_pid}").stdout.strip()))}
                        for role, pane in manager.panes().items()}
                    acknowledgment = store.query("SELECT id,packet_id,packet_hash,readback,status FROM resume_acks WHERE session_id=:session",
                                                 {"session": prepared["session_id"]})
                    if len(acknowledgment) != 1 or not sessions.status(prepared["session_id"])["acknowledged"]:
                        raise RuntimeError("Exact single ACK is not retained across reconnect")
                    return {"server": process_key(tmux_pid), "panes": panes, "selection": selection,
                            "ack": acknowledgment, "readback": committed_readback(),
                            "operation_counts": {table: store.query("SELECT count(*) AS total FROM " + table)[0]["total"]
                                                 for table in ("sources", "records", "resume_acks", "jobs")}}
                # This is a real client detach/exit/attach, distinct from the
                # HTTP broker outage above. No model query or write is repeated.
                previous_terminal, terminal = terminal, None
                terminal, reconnect = reconnect_terminal(previous_terminal, manager, PreviewTerminal, continuity)
                committed = committed_readback()
                frames = capture_workspace(manager, root / "captures", f"cycle-{number:03}")
                emit("cycle", number=number, seconds=round(time.monotonic() - cycle_start, 3), record_id=record["id"],
                     snapshot_id=inspected["snapshot_id"], exact_ack=True, process_identity_preserved=True,
                     model_observation=models, committed_readback=committed, terminal_reconnect=reconnect,
                     frame_hashes={key: value["sha256"] for key, value in frames.items()})
                report["committed_readback_cycles"] += 1
                report["terminal_reconnect_cycles"] += 1
                report["cycles"] = number
                summary_path.write_text(json.dumps(report, indent=2))
                next_cycle = cycle_start + args.cycle_seconds
            stopped.wait(.2)
        report["workload_seconds"] = time.monotonic() - started
        enough_cycles = report["cycles"] >= math.ceil(args.duration / args.cycle_seconds)
        report["status"] = ("failed" if runtime_failure or not enough_cycles else
                            "interrupted" if stopped.is_set() else "workload_complete_pending_cleanup")
        if runtime_failure:
            report["runtime_failure"] = runtime_failure
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__, error=str(exc)[:1000])
        emit("failure", error_type=type(exc).__name__)
        raise
    finally:
        stopped.set()
        if supervisor:
            supervisor.join(10)
        if health_thread:
            health_thread.join(10)
        if (supervisor and supervisor.is_alive()) or (health_thread and health_thread.is_alive()):
            report.setdefault("cleanup_errors", []).append("Owned monitoring thread did not stop")
        if runtime_failure:
            report["runtime_failure"] = list(runtime_failure)
        # Startup can fail after a child was spawned but before its identity was
        # recorded. Recover only an exact owned root/socket, never a global PID.
        if jvm_identity is None and (root / "arcade/process.json").is_file():
            try:
                jvm_identity = monitor.owned_jvm(root, settings.java_executable)
            except (OSError, ValueError, RuntimeError) as exc:
                report.setdefault("cleanup_errors", []).append("Cannot prove startup JVM ownership: " + type(exc).__name__)
        if tmux_identity is None and manager is not None:
            try:
                if manager.exists():
                    tmux_identity = monitor.process(int(manager.tmux("display-message", "-p", "#{pid}").stdout.strip()))
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                report.setdefault("cleanup_errors", []).append("Cannot prove startup tmux ownership: " + type(exc).__name__)
        for component in (terminal, manager, server):
            if component:
                try:
                    if component is manager:
                        if tmux_identity and monitor.same_process(tmux_identity):
                            manager.tmux("kill-server", check=False)
                        elif tmux_identity:
                            raise RuntimeError("Owned tmux identity changed before cleanup; refusing to signal it")
                    component.close()
                except Exception as exc:  # noqa: BLE001 - finish cleaning the other owned components
                    report.setdefault("cleanup_errors", []).append(type(exc).__name__)
        try:
            if jvm_identity:
                if jvm_handle is None:
                    jvm_row = supervision.identity(jvm_identity["pid"])
                    if not jvm_row or not owned_scope.owns(jvm_row) or not monitor.same_process(jvm_identity):
                        raise RuntimeError("Cannot admit the startup JVM for cleanup")
                    jvm_handle = os.pidfd_open(jvm_row["pid"])
                    if not owned_scope.owns(jvm_row):
                        raise RuntimeError("Startup JVM changed during cleanup admission")
                report["jvm_actual_exit_verified"] = stop_owned_jvm(jvm_handle, jvm_row, owned_scope)
        except Exception as exc:  # noqa: BLE001 - retain engine cleanup failure in the final evidence
            report.setdefault("cleanup_errors", []).append(type(exc).__name__)
        finally:
            if jvm_handle is not None:
                os.close(jvm_handle)
        try:
            events.close()
        except Exception as exc:  # noqa: BLE001 - flushing evidence is also part of cleanup
            report.setdefault("cleanup_errors", []).append("Evidence close failed: " + type(exc).__name__)
        report["ended"] = time.time()
        report["seconds_observed"] = report["ended"] - report["started"]
        report["database_retained_for_diagnosis"] = True
        try:
            report["post_cleanup_native_observation"] = watch.finish(args.cleanup_grace)
            report["final_boot_id"] = monitor.boot_id()
            report["jvm_stopped"] = bool(report.get("jvm_actual_exit_verified")) and not monitor.same_process(jvm_identity)
            report["tmux_stopped"] = not tmux_identity or not monitor.same_process(tmux_identity)
            report["owned_fatal_reports"] = sorted(str(path.relative_to(root)) for path in root.rglob("hs_err_pid*.log"))
            report["owned_fatal_reports_absent"] = not report["owned_fatal_reports"]
            report["source_unchanged"] = source_manifest(args.source_root) == qualification["qualified_source_hashes"]
            report["runtime_unchanged"] = all(
                digest(Path(qualification["qualified_runtime"][name + "_executable"])) == qualification["qualified_runtime"][name + "_binary_sha256"]
                for name in ("python", "java", "tmux"))
            report["qualification_reports_unchanged"] = (
                digest(args.gate_report) == qualification["runtime_gate_sha256"]
                and digest(args.suite_report) == qualification["suite_report_sha256"])
            report["controllers_unchanged"] = all(digest(Path(__file__).with_name(name)) == value
                                                  for name, value in qualification["controller_hashes"].items())
            report["post_cleanup_native_observation"] = watch.poll()
            report["final_boot_id"] = monitor.boot_id()
            if (report["post_cleanup_native_observation"]["native_failure"]
                    or not all(report[key] for key in ("jvm_stopped", "tmux_stopped", "source_unchanged", "runtime_unchanged", "qualification_reports_unchanged"))):
                report["status"] = "failed"
        except Exception as exc:  # noqa: BLE001 - absent crash evidence cannot become a pass
            report.setdefault("cleanup_errors", []).append("Final observation failed: " + type(exc).__name__)
        try:
            # Its independent thread remains active while panes/JVM shut down
            # and while CrashWatch completes the full post-cleanup grace.
            report["gpu_monitor"] = gpu_monitor.finish(time.monotonic())
            if not report["gpu_monitor"]["passed"]:
                report.setdefault("cleanup_errors", []).append("Required GPU observation failed")
            report["post_cleanup_native_observation"] = watch.poll()
            report["final_boot_id"] = monitor.boot_id()
        except Exception as exc:
            report.setdefault("cleanup_errors", []).append("GPU final observation failed: " + type(exc).__name__)
        report["cleanup_complete"] = (not report.get("cleanup_errors") and report.get("jvm_stopped") is True
                                      and report.get("tmux_stopped") is True)
        qualify_completion(report)
        report["ended"] = time.time()
        report["seconds_observed"] = report["ended"] - report["started"]
        summary_path.write_text(json.dumps(report, indent=2))
    return 0 if report["status"] == "workload_passed_pending_external_watch" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--gate-report", type=Path, required=True)
    parser.add_argument("--suite-report", type=Path, required=True)
    parser.add_argument("--java-home", type=Path, required=True)
    parser.add_argument("--tmux-bin", type=Path, required=True)
    parser.add_argument("--arcade-dist", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--wsl-crash-dir", type=Path, required=True)
    parser.add_argument("--cleanup-grace", type=int, default=30)
    parser.add_argument("--enable-local-models", action="store_true")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--chat-model", default="qwen3.5:9b")
    parser.add_argument("--embedding-model", default="qwen3-embedding:0.6b")
    parser.add_argument("--duration", type=float, default=28800)
    parser.add_argument("--cycle-seconds", type=float, default=900)
    args = parser.parse_args()
    if not math.isfinite(args.duration) or args.duration <= 0 or args.cycle_seconds != 900 or args.cleanup_grace < 30:
        parser.error("Finite positive duration, cycle-seconds=900 and cleanup-grace>=30 are required")
    if not args.execute:
        print(json.dumps({"status": "prepared_only", "will_start": False, "duration": args.duration,
                          "cycle_seconds": args.cycle_seconds, "requires": ["runtime gate", "final full suite", "explicit execution"]}))
        return
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
