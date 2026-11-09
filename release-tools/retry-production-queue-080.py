"""One explicitly admitted historical retry; inert without --execute.

Prepared release tooling, not a production approval or a qualification result.
The frozen 403-tool supervisor deliberately does not admit this controller.
Before use it needs peer review, new supervised guards, normal allowlist/hash
integration, final release evidence and a new post-update backup admission.
No mode drains the queue, retries an uncertain attempt, restores a database,
starts a service/scanner, or adopts an existing JVM.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import ExitStack, contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse
from uuid import UUID, uuid4


PRODUCTION = Path("/home/piotr/.local/share/devbrain")
JOURNALS = Path("/home/piotr/.local/state/devbrain-production-retry-080")
HISTORICAL_SHA = "698591d33252a1f0e802a01fac78feb90f56ef37fc0c5f717ed26667f635ac70"
STATUS = "production_one_job_completed_pending_external_watch"
REQUIRED_ARTIFACTS = {"production-retry-results.json", "production-admission.json"}
RUNTIME_KEYS = tuple(f"{name}_{suffix}" for name in ("python", "java", "tmux")
                     for suffix in ("executable", "binary_sha256"))
GATE_CONTROLLERS = {
    "ux": ("run-regressions-080.py", "regressions_completed_pending_external_watch", "ux"),
    "frontend": ("run-regressions-080.py", "regressions_completed_pending_external_watch", "frontend"),
    "recall": ("run-regressions-080.py", "regressions_completed_pending_external_watch", "recall"),
    "native": ("validate-native-account080.py", "native_account_completed_pending_attestation", None),
    "copilot": ("validate-copilot-account080.py", "copilot_account_completed_pending_attestation", None),
    "interactive": ("run-interactive-ux080.py", "interactive_ux_completed_pending_external_watch", None),
    "queue_copy": ("replay-failed-queue-080.py", "queue_copy_completed_pending_external_watch", None),
    "upgrade_smoke": ("test_devbrain_update080_smoke.py", "upgrade_smoke_completed_pending_attestation", None),
    "tooling_guards": ("run-regressions-080.py", "regressions_completed_pending_external_watch", "guards"),
}


class Refusal(RuntimeError):
    """Only fixed, content-free codes may leave the private production context."""


def require(ok, code):
    if not ok:
        raise Refusal(code)


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical(path, *, directory=False, private=False):
    path = Path(path)
    require(path.is_absolute() and path.resolve(strict=True) == path, "path_not_canonical")
    for part in (path, *path.parents):
        require(not part.is_symlink(), "path_symlink")
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode), "path_wrong_type")
    if private:
        require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600)
                and (directory or info.st_nlink == 1), "private_path_permissions")
    return path


def document(path, *, private=False):
    path = canonical(path, private=private)
    require(path.stat().st_size <= 16 * 1024 * 1024, "json_size_limit")
    result = json.loads(path.read_text(encoding="utf-8-sig"))
    require(isinstance(result, dict), "json_object_required")
    return result


def atomic_private(path, value, *, create=False):
    """Durable replace; a failed directory fsync must propagate, never mean done."""
    path = Path(path)
    canonical(path.parent, directory=True, private=True)
    require(not path.is_symlink(), "journal_symlink")
    if create:
        # The permanent O_EXCL reservation survives every later failure. It is
        # intentionally never removed, including failed admission/partial JSON.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        fsync_directory(path.parent)
    else:
        canonical(path, private=True)
    temporary = path.with_name("." + path.name + "." + uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def load_helper(name):
    path = canonical(Path(__file__).with_name(name))
    spec = importlib.util.spec_from_file_location("production_retry_" + name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pinned_reference(reference, *, private=False):
    require(isinstance(reference, dict) and set(reference) == {"path", "sha256"}, "reference_schema")
    path = canonical(reference["path"], private=private)
    require(sha(path) == reference["sha256"], "reference_hash_changed")
    return path


def runtime_equal(left, right):
    return all(isinstance(left.get(key), str) and left[key] and left[key] == right.get(key) for key in RUNTIME_KEYS)


def canonical_export(value):
    """Row order is not a DB invariant; nested field/array order still is."""
    result = dict(value)
    result.pop("exported_at", None)
    encode = lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    for key, rows in result.items():
        if isinstance(rows, list):
            result[key] = sorted(rows, key=encode)
    if isinstance(result.get("vector_data"), dict):
        result["vector_data"] = {key: sorted(rows, key=encode) for key, rows in result["vector_data"].items()}
    return result


def row_digest(row):
    return hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def historical_jobs(payload):
    jobs = [row for row in payload["jobs"] if row.get("state") == "failed"]
    require(len(jobs) == 6 and len({row["id"] for row in jobs}) == 6
            and Counter(row.get("kind") for row in jobs) == {"import": 3, "extract": 3}, "historical_six_mismatch")
    return {row["id"]: row for row in jobs}


def selected_job(payload, historical, job_id, expected_attempts):
    require(type(expected_attempts) is int and expected_attempts >= 0, "attempt_invalid")
    require(job_id in historical, "job_not_in_historical_six")
    rows = [row for row in payload["jobs"] if row.get("id") == job_id]
    require(len(rows) == 1, "selected_job_not_unique")
    row, original = rows[0], historical[job_id]
    require(row.get("state") == "failed" and row.get("attempts", 0) == expected_attempts
            and row.get("lease_until") is None, "selected_attempt_conflict")
    require(all(row.get(key) == original.get(key) for key in ("id", "project_id", "kind", "job_key", "payload")),
            "historical_job_identity_changed")
    # Failed jobs legitimately retain their last worker. The complete row hash
    # below binds it rather than assuming it is absent or silently discarding it.
    return row


class AttemptJournal:
    def __init__(self, root, job, run_id, admission_sha):
        self.path = Path(root) / (str(UUID(job["id"])) + ".json")
        self.value = {"schema_version": 1, "scope": "devbrain_production_single_attempt_journal",
                      "run_id": run_id, "data_dir": str(PRODUCTION), "job_id": job["id"],
                      "expected_attempts": job.get("attempts", 0), "baseline_row_sha256": row_digest(job),
                      "admission_sha256": admission_sha, "state": "prepared", "automatic_replay_allowed": False}
        atomic_private(self.path, self.value, create=True)

    def advance(self, state):
        transitions = {"prepared": {"claim_pending"}, "claim_pending": {"claimed"},
                       "claimed": {"execution_started"}, "execution_started": {"attempt_finished"},
                       "attempt_finished": {"verified"}}
        require(state in transitions.get(self.value["state"], set()), "journal_transition_invalid")
        candidate = {**self.value, "state": state}
        atomic_private(self.path, candidate)
        self.value = candidate


def claim_failed_selected(store, baseline, worker):
    """Official retry bookkeeping and claim commit in one transaction, by ID."""
    from devbrain.agent import inference_lease_seconds
    from devbrain.store import _one, _set, _now
    with store.transaction() as tx:
        row = _one(tx, "SELECT FROM jobs WHERE id=:id", {"id": baseline["id"]}, False)
        require(row is not None and row_digest(row) == row_digest(baseline), "claim_compare_and_swap_conflict")
        require(row["state"] == "failed" and row.get("lease_until") is None, "claim_ineligible")
        store._require_project(tx, row["project_id"])
        store._retry_job_locked(tx, row)
        pending = _one(tx, "SELECT FROM jobs WHERE id=:id", {"id": row["id"]}, False)
        require(pending is not None and pending.get("state") == "pending"
                and pending.get("attempts", 0) == baseline.get("attempts", 0)
                and pending.get("retry_count", 0) == baseline.get("retry_count", 0) + 1
                and pending.get("worker") is None and pending.get("lease_until") is None, "retry_bookkeeping_conflict")
        pending.update(state="running", worker=worker, lease_until=time.time() + inference_lease_seconds(),
                       attempts=baseline.get("attempts", 0) + 1, diagnostics={}, updated_at=_now())
        _set(tx, "jobs", pending, "id=:id AND state='pending'", {"id": pending["id"]})
        return pending


def unrelated_jobs(payload, selected):
    return {row["id"]: row_digest(row) for row in payload["jobs"] if row["id"] != selected}


def duplicate_counts(payload):
    result = {}
    for table, keys in (("jobs", ("job_key",)), ("sources", ("project_id", "uri", "sha256"))):
        counts = Counter(tuple(row.get(key) for key in keys) for row in payload[table])
        result[table] = sum(count - 1 for count in counts.values() if count > 1)
    return result


def verify_attempt_result(before, after, job):
    rows = [row for row in after["jobs"] if row.get("id") == job["id"]]
    require(len(rows) == 1, "result_job_not_unique")
    result = rows[0]
    require(result.get("attempts") == job["attempts"] and result.get("worker") == job["worker"]
            and result.get("lease_until") is None and result.get("retry_count") == job["retry_count"], "result_attempt_changed")
    existing = unrelated_jobs(before, job["id"])
    current = unrelated_jobs(after, job["id"])
    require(all(current.get(key) == value for key, value in existing.items()), "unrelated_job_changed")
    new = [row for row in after["jobs"] if row["id"] not in existing and row["id"] != job["id"]]
    require(all(row.get("state") == "pending" and row.get("attempts", 0) == 0
                and row.get("worker") is None and row.get("lease_until") is None
                and row.get("project_id") == job["project_id"] for row in new), "dependent_job_executed_or_wrong_project")
    require(duplicate_counts(before) == duplicate_counts(after), "new_duplicate_job_or_source")
    require(result.get("state") == "completed", "selected_job_failed")
    return {"job_id": job["id"], "kind": job["kind"], "state": result["state"], "attempts": result["attempts"],
            "retry_count": result["retry_count"], "unrelated_jobs_unchanged": True,
            "new_pending_jobs": len(new), "duplicate_counts_unchanged": True}


def frontend_manifest(root):
    """Same source boundary as the qualified frontend controller, not src/ only."""
    canonical(root, directory=True)
    result = {}
    for directory, subdirs, names in os.walk(root):
        subdirs[:] = sorted(name for name in subdirs if name not in
                            {"node_modules", ".git", "dist", "test-results", "playwright-report"})
        for name in [*subdirs, *names]:
            require(not (Path(directory) / name).is_symlink(), "frontend_source_symlink")
        for name in sorted(names):
            if name.endswith(".tsbuildinfo"):
                continue
            path = canonical(Path(directory) / name)
            result[path.relative_to(root).as_posix()] = sha(path)
    require("package.json" in result and "package-lock.json" in result, "frontend_lock_required")
    return result


def verify_frontend_source(run, source):
    require(run.get("frontend_source_hashes") == frontend_manifest(source / "frontend"), "frontend_not_final_source")


def recompute_final_attestation(audit, name, root, ref, final_source_hashes):
    qualified_source = canonical(ref["qualified_source_root"], directory=True)
    require(audit.source_manifest(qualified_source) == final_source_hashes, "qualified_source_bytes_differ")
    return (audit.attest_full_suite(root, qualified_source) if name == "full_suite"
            else audit.attest_soak(root, qualified_source))


def verify_qualified_tools(helpers, directory):
    required = {"retry-production-queue-080.py", "test_production_queue080_guards.py", "supervise-080.py",
                "run-supervised-080.ps1", "attest-full-suite-080.py", "validate-native-account080.py",
                "validate-stability-080.py", "qualification-power-080.ps1", "run-regressions-080.py"}
    require(isinstance(helpers, dict) and required <= helpers.keys(), "new_production_tool_not_qualified")
    for name, digest in helpers.items():
        require(isinstance(name, str) and Path(name).name == name and name not in {".", ".."}
                and "/" not in name and "\\" not in name, "qualified_tool_name_invalid")
        require(sha(canonical(directory / name)) == digest, "qualified_tool_bundle_changed")


def verify_admission(admission, guard, source, runtime):
    require(admission.get("schema_version") == 1 and admission.get("scope") == "devbrain_production_retry_admission"
            and admission.get("source_final") is True and admission.get("data_dir") == str(PRODUCTION)
            and admission.get("version") == "0.8.0", "final_production_admission_required")
    require(admission.get("source_hashes") == guard.source_manifest(source)
            and runtime_equal(admission.get("runtime", {}), runtime), "admission_source_runtime_mismatch")
    proofs = admission.get("proofs", {})
    require(set(proofs) == {"full_suite", "soak", *GATE_CONTROLLERS}, "all_release_proofs_required")
    for name, ref in proofs.items():
        # Each old gate is checked by its own frozen, hash-bound verifier. This
        # does not reinterpret historical receipts with newly edited helpers.
        verifier = pinned_reference(ref["verifier"])
        require(verifier.name == "attest-full-suite-080.py", "proof_verifier_name")
        spec = importlib.util.spec_from_file_location("frozen_gate_" + name, verifier)
        audit = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(audit)
        root = canonical(ref["artifact_root"], directory=True)
        receipt = pinned_reference({"path": str(root / "receipt.json"), "sha256": ref["receipt_sha256"]})
        if name in {"full_suite", "soak"}:
            expected = document(pinned_reference(ref["attestation"]))
            actual = recompute_final_attestation(audit, name, root, ref, admission["source_hashes"])
            require(actual == expected, "attestation_recompute_mismatch")
            require(runtime_equal(actual.get("runtime", {}), runtime), "attestation_runtime_mismatch")
            require(sha(root / "gate.json") == admission["runtime_gate_sha256"], "attestation_gate_mismatch")
            if name == "full_suite":
                require(actual["counts"]["skipped"] == 0, "full_suite_skips_not_allowed")
            continue
        run = audit.document(audit.artifact(root, "controller-report.json"))
        controller, status, phase = GATE_CONTROLLERS[name]
        require(run.get("status") == status and runtime_equal(run.get("runtime", {}), runtime), "gate_controller_incomplete")
        require(sha(root / "gate.json") == admission["runtime_gate_sha256"], "proof_runtime_gate_mismatch")
        if phase:
            require(run.get("phase") == phase, "gate_phase_mismatch")
        if name not in {"upgrade_smoke", "tooling_guards"}:
            require(run.get("source_hashes") == admission["source_hashes"], "gate_not_final_source")
        if name == "frontend":
            verify_frontend_source(run, source)
        observed = audit.document(receipt)
        require(isinstance(observed.get("artifact_sha256"), dict) and observed["artifact_sha256"], "gate_artifacts_missing")
        audit.verify_external(root, run["run_id"], run["runtime"], set(observed["artifact_sha256"]), controller)
        if name == "upgrade_smoke":
            require(run.get("release_zip_sha256") == admission["release_zip_sha256"], "smoke_not_final_release")
        if name == "tooling_guards":
            inputs = audit.document(audit.artifact(root, "regression-source.json"))
            helpers = inputs.get("guard_sources", {}).get("helper_sha256", {})
            verify_qualified_tools(helpers, Path(__file__).parent)
            collection = audit.document(audit.artifact(root, "guards-collection.json"))
            require(any(node.split("::", 1)[0] == "test_production_queue080_guards.py"
                        for node in collection.get("nodeids", [])), "production_guards_not_collected")
            audit.junit_results(audit.artifact(root, "guards.xml"), collection["nodeids"], {})


def package_matches_source(source):
    import devbrain
    require(devbrain.__version__ == "0.8.0" and sys.flags.isolated == 1 and sys.prefix != sys.base_prefix,
            "isolated_installed_080_required")
    package = canonical(Path(devbrain.__file__).parent, directory=True)
    require(package.is_relative_to(Path(sys.prefix).resolve()), "noneditable_installed_package_required")
    def manifest(root):
        result = {}
        for path in root.rglob("*"):
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            require(not path.is_symlink(), "package_symlink")
            if path.is_file():
                result[path.relative_to(root).as_posix()] = sha(canonical(path))
        return result
    require(manifest(package) == manifest(source / "src/devbrain"), "installed_package_not_final_source")


@contextmanager
def exclusive_file(path):
    import fcntl
    canonical(path.parent, directory=True, private=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        legacy = path.parent == PRODUCTION and path.name in {"service.lock", "engine.lock", "database-writer.lock"}
        mode = stat.S_IMODE(info.st_mode)
        permissions = (mode in {0o600, 0o644} and info.st_size == 0) if legacy else mode == 0o600
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
                and permissions, "unsafe_lock_file")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(path.stat().st_ino == info.st_ino and path.stat().st_dev == info.st_dev, "lock_identity_changed")
        yield fd
    finally:
        os.close(fd)


def prepare_journal_directory(root):
    """Create only our dedicated private child; never chmod its legacy parent."""
    parent = canonical(root.parent, directory=True)
    info = parent.stat()
    require(info.st_uid == os.getuid() and not (stat.S_IMODE(info.st_mode) & 0o022), "journal_parent_not_owned_or_writable")
    if not root.exists():
        root.mkdir(mode=0o700)
        fsync_directory(parent)
    return canonical(root, directory=True, private=True)


def require_quiescent(data, ignored=()):
    """Never emit /proc command/environment contents; no stale PID-cache trust."""
    needle = os.fsencode(data)
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) in {os.getpid(), *ignored}:
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            first = proc_birth((entry / "stat").read_bytes())
            command = (entry / "cmdline").read_bytes()
            environment = (entry / "environ").read_bytes()
            last = proc_birth((entry / "stat").read_bytes())
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            raise Refusal("quiescence_process_unreadable") from None
        require(first == last, "quiescence_pid_changed")
        require(needle not in command and b"DEVBRAIN_HOME=" + needle not in environment.split(b"\0"),
                "production_process_still_running")


def proc_birth(raw):
    return raw[raw.rfind(b")") + 2:].split()[19]


def tree_manifest(root):
    result = {}
    for path in canonical(root, directory=True).rglob("*"):
        require(not path.is_symlink(), "engine_library_symlink")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = sha(canonical(path))
    require(result, "engine_libraries_missing")
    return result


def listener_owned(row, scope, port, *, proc=Path("/proc")):
    """Read socket inode metadata to exclude the free-port/startup adoption race."""
    require(scope.owns(row), "engine_listener_identity_changed")
    sockets = set()
    for fd in (proc / str(row["pid"]) / "fd").iterdir():
        try:
            target = os.readlink(fd)
        except FileNotFoundError:
            continue
        match = re.fullmatch(r"socket:\[([0-9]+)\]", target)
        if match:
            sockets.add(match[1])
    listening = set()
    for protocol in ("tcp", "tcp6"):
        for line in (proc / str(row["pid"]) / "net" / protocol).read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 10 and fields[3] == "0A" and int(fields[1].rsplit(":", 1)[1], 16) == port:
                listening.add(fields[9])
    require(scope.owns(row), "engine_listener_identity_changed")
    return bool(listening) and listening <= sockets


class ProductionEngine:
    """Only a newly spawned, retained, scope-bound JVM; no package start/adopt."""
    def __init__(self, settings, runtime, scope, supervision, log_directory, library_manifest):
        self.settings, self.runtime, self.scope, self.supervision = settings, runtime, scope, supervision
        self.logs = log_directory
        self.library_manifest = library_manifest
        self.process = self.row = self.handle = None
        self.forced = False

    def start(self):
        from devbrain.arcade import ArcadeClient
        from devbrain.engine import runtime_dir
        state = canonical(self.settings.data_dir / "arcade", directory=True, private=True)
        home = canonical(runtime_dir(self.settings), directory=True)
        for part in ("config", "databases", "backups", "log"):
            canonical(state / part, directory=True)
        canonical(state / "root-password", private=True)
        require(tree_manifest(home / "lib") == self.library_manifest, "engine_libraries_not_admitted")
        parsed = urlparse(self.settings.arcade_url)
        require(parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and not parsed.username
                and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment, "production_endpoint_invalid")
        # Refuse an occupied port instead of adopting its server. Version/HTTP
        # probes happen only after our child is admitted as the new process.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", parsed.port or 2480))
        command = [self.runtime["java_executable"], "-Xms64m", "-Xmx1024m", "--add-modules=jdk.incubator.vector",
                   "--enable-native-access=ALL-UNNAMED", "-Djava.awt.headless=true", "-Dfile.encoding=UTF8",
                   "-Darcadedb.profile=low-ram", f"-Darcadedb.server.rootPath={state}"]
        for option, value in (("configDirectory", state / "config"), ("databaseDirectory", state / "databases"),
                              ("backupDirectory", state / "backups"), ("logsDirectory", state / "log"),
                              ("rootPasswordPath", state / "root-password"), ("httpIncomingHost", "127.0.0.1"),
                              ("httpIncomingPort", parsed.port or 2480)):
            command.append(f"-Darcadedb.server.{option}={value}")
        command += [f"-Djava.util.logging.config.file={state / 'config/arcadedb-log.properties'}",
                    "-cp", str(home / "lib/*"), "com.arcadedb.server.ArcadeDBServer"]
        env = {key: value for key, value in os.environ.items() if key not in
               {"JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS", "CLASSPATH"}}
        java_home = str(Path(self.runtime["java_executable"]).parent.parent)
        env.update(JAVA_HOME=java_home, PATH=str(Path(java_home) / "bin") + os.pathsep + env.get("PATH", ""))
        with (self.logs / "private-engine.log").open("xb") as log:
            os.chmod(log.name, 0o600)
            self.process = subprocess.Popen(command, cwd=state, stdin=subprocess.DEVNULL,
                                            stdout=log, stderr=log, env=env, start_new_session=True)
        self.handle = os.pidfd_open(self.process.pid)
        self.row = self.supervision.identity(self.process.pid)
        require(self.row and self.row["parent_pid"] == os.getpid() and self.scope.owns(self.row)
                and self.row["executable"] == self.runtime["java_executable"], "engine_identity_not_owned")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            require(self.process.poll() is None and self.scope.owns(self.row), "owned_engine_exited")
            try:
                if not listener_owned(self.row, self.scope, parsed.port or 2480):
                    time.sleep(.25)
                    continue
                version = str(ArcadeClient(self.settings).server_info().get("version", "")).split(" ", 1)[0]
                require(version == self.settings.arcade_version, "production_engine_version_mismatch")
                return
            except Refusal:
                raise
            except Exception:
                time.sleep(.25)
        raise Refusal("owned_engine_start_timeout")

    def stop(self):
        if self.process is None:
            return {"engine_actual_exit_verified": True, "engine_started": False}
        require(self.handle is not None and self.row is not None, "engine_cleanup_identity_unavailable")
        require(self.process.poll() is None and self.scope.owns(self.row), "engine_died_before_shutdown")
        signal.pidfd_send_signal(self.handle, signal.SIGTERM)
        poller = select.poll()
        poller.register(self.handle, select.POLLIN)
        require(bool(poller.poll(30000)), "engine_graceful_shutdown_timeout")
        code = self.process.wait(timeout=1)
        os.close(self.handle)
        self.handle = None
        require(code in (0, 143, -15), "engine_shutdown_failed")
        from devbrain.engine import runtime_dir
        require(tree_manifest(runtime_dir(self.settings) / "lib") == self.library_manifest, "engine_libraries_changed")
        return {"engine_actual_exit_verified": True, "engine_started": True, "engine_returncode": code,
                "graceful_shutdown_requested": True}


def existing_store(settings, writer_fd):
    """Use the existing schema only, under a lifetime outer writer lock."""
    from devbrain.store import Store
    from devbrain.arcade import ArcadeClient
    fd_info = os.fstat(writer_fd)
    local_lock = threading.RLock()
    class LockedStore(Store):
        def __init__(self):
            self.data_dir, self.settings = settings.data_dir, settings
            self.client, self._local = ArcadeClient(settings), threading.local()

        @contextmanager
        def _lock(self):
            current = (self.data_dir / "database-writer.lock").stat()
            retained = os.fstat(writer_fd)
            require((current.st_dev, current.st_ino) == (fd_info.st_dev, fd_info.st_ino)
                    == (retained.st_dev, retained.st_ino), "writer_lock_changed")
            with local_lock:
                yield
    return LockedStore()


async def execute_selected(store, settings, job):
    from devbrain.agent import KnowledgeAgent, extraction_outer_timeout
    from devbrain.ingest import import_history
    error = None
    agent = None
    try:
        if job["kind"] == "import":
            payload = json.loads(job["payload"]) if isinstance(job["payload"], str) else job["payload"]
            path = canonical(Path(payload["path"]))
            # Synchronous bounded-by-supervisor execution: no cancelled thread
            # can keep writing after the controller starts JVM cleanup.
            result = import_history(store, path, job["project_id"], payload.get("client", "auto"))
            if result.get("errors"):
                error = "production_retry_import_failed"
        elif job["kind"] == "extract":
            agent = KnowledgeAgent(store, settings)
            await asyncio.wait_for(agent.extract(job), extraction_outer_timeout())
        else:
            raise Refusal("job_kind_not_authorized")
    except Exception:
        error = "production_retry_attempt_failed"
    finally:
        if agent is not None:
            await agent.ollama.aclose()
    store.finish_job(job["id"], error, worker=job["worker"], expected_attempts=job["attempts"])


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--admission", type=Path)
    parser.add_argument("--admission-sha256")
    parser.add_argument("--job-id")
    parser.add_argument("--expected-attempts", type=int)
    args = parser.parse_args(argv)
    if not args.execute:
        print(json.dumps({"status": "prepared_not_run", "production_modified": False,
                          "execution_requires_new_qualified_supervisor_integration": True,
                          "one_explicit_historical_job_only": True}))
        return 0
    report = None
    supervision = None
    artifacts = None
    try:
        require(all(getattr(args, name) is not None for name in
                    ("run_id", "artifact_root", "source_root", "data_dir", "admission", "admission_sha256", "job_id", "expected_attempts")),
                "explicit_single_job_inputs_required")
        require(sum(value == "--job-id" or value.startswith("--job-id=") for value in argv) == 1,
                "exactly_one_job_argument_required")
        require(str(UUID(args.job_id)) == args.job_id and args.data_dir == PRODUCTION, "production_job_or_path_invalid")
        require(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.run_id), "run_id_invalid")
        require(sys.platform == "linux" and sys.flags.isolated == 1, "isolated_linux_required")
        supervision, guard = load_helper("supervise-080.py"), load_helper("attest-full-suite-080.py")
        require(supervision.CONTROLLERS.get(Path(__file__).name) == STATUS
                and REQUIRED_ARTIFACTS <= supervision.CONTROLLER_ARTIFACTS.get(Path(__file__).name, set()),
                "production_controller_not_qualified_or_admitted")
        artifacts = canonical(args.artifact_root, directory=True)
        require(artifacts.is_relative_to(Path("/mnt/c")) and not (artifacts / "controller-report.json").exists(), "fresh_external_artifacts_required")
        require(os.environ.get("DEVBRAIN_QUALIFICATION_RUN_ID") == args.run_id, "supervision_run_mismatch")
        proof = document(artifacts / "mapping-proof.json")
        require(proof.get("run_id") == args.run_id, "supervision_scope_mismatch")
        scope = supervision.ScopeOwnership(proof.get("scope_unit"))
        parent = supervision.identity(os.getppid())
        require(parent and scope.owns(parent), "supervisor_parent_required")
        gate = document(artifacts / "gate.json")
        runtime = supervision.qualified_runtime(gate)
        require(str(Path(sys.executable).resolve()) == runtime["python_executable"], "interpreter_not_qualified")
        source = canonical(args.source_root, directory=True)
        admission_path = pinned_reference({"path": str(args.admission), "sha256": args.admission_sha256}, private=True)
        admission = document(admission_path, private=True)
        require(admission.get("job_id") == args.job_id and admission.get("expected_attempts") == args.expected_attempts,
                "admission_job_mismatch")
        verify_admission(admission, guard, source, runtime)
        package_matches_source(source)
        require(admission.get("runtime_gate_sha256") == sha(artifacts / "gate.json"), "admission_gate_mismatch")
        library_manifest = admission.get("arcade_library_sha256")
        require(isinstance(library_manifest, dict) and library_manifest
                and all(isinstance(name, str) and isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                        for name, value in library_manifest.items()), "admitted_engine_libraries_required")
        # Inputs are admitted before any private production settings/backup read.
        data = canonical(args.data_dir, directory=True, private=True)
        update_path = pinned_reference(admission["update_receipt"], private=True)
        update = document(update_path, private=True)
        require(update.get("format_version") == 2 and update.get("version") == "0.8.0"
                and update.get("data_dir") == str(data) and update.get("payload_sha256") == admission["release_payload_sha256"]
                and update.get("runtime_gate_sha256") == admission["runtime_gate_sha256"]
                and update.get("python_binary_sha256") == runtime["python_binary_sha256"], "production_update_receipt_mismatch")
        backup_path = pinned_reference(admission["post_update_backup"], private=True)
        require(backup_path.is_relative_to(Path("/home/piotr/.local/state/devbrain-updates"))
                and update_path.stat().st_mtime <= backup_path.stat().st_mtime <= time.time()
                and time.time() - backup_path.stat().st_mtime <= 3600, "fresh_post_update_backup_required")
        backup_report = document(pinned_reference(admission["post_update_backup_report"], private=True), private=True)
        require(backup_report.get("schema_version") == 1 and backup_report.get("scope") == "devbrain_post_update_backup"
                and backup_report.get("status") == "backup_completed" and backup_report.get("version") == "0.8.0"
                and backup_report.get("data_dir") == str(data)
                and backup_report.get("update_receipt_sha256") == sha(update_path)
                and backup_report.get("backup_sha256") == sha(backup_path)
                and backup_report.get("backup_validation_passed") is True
                and backup_report.get("engine_cleanup_complete") is True
                and backup_report.get("source_hashes") == admission["source_hashes"]
                and runtime_equal(backup_report.get("runtime", {}), runtime), "post_update_backup_proof_missing")
        historical_path = pinned_reference(admission["historical_backup"], private=True)
        require(sha(historical_path) == HISTORICAL_SHA, "historical_backup_mismatch")
        from devbrain.store import Store
        before = Store._validated_backup(backup_path)
        baseline = selected_job(before, historical_jobs(Store._validated_backup(historical_path)), args.job_id, args.expected_attempts)
        require(not any(row.get("state") == "running" for row in before["jobs"]), "backup_has_running_jobs")
        prepare_journal_directory(JOURNALS)
        # Do not export the private admission: only its immutable hash and the
        # public proof references are allowed in external artifacts.
        public_admission = {"schema_version": 1, "scope": "devbrain_production_retry_admission_binding",
                            "admission_sha256": args.admission_sha256, "job_id": args.job_id,
                            "expected_attempts": args.expected_attempts, "source_final": True,
                            "runtime_gate_sha256": admission["runtime_gate_sha256"]}
        supervision.atomic_json(artifacts / "production-admission.json", public_admission)
        report = {"schema_version": 1, "scope": "devbrain_production_one_job_retry", "run_id": args.run_id,
                  "status": "running", "runtime": runtime, "python_launcher": sys.executable,
                  "source_hashes": admission["source_hashes"], "runtime_gate_sha256": admission["runtime_gate_sha256"],
                  "cleanup_complete": False, "forced_cleanup": False, "workload_criteria_passed": False,
                  "controller_exit_verified": False,
                  "production_mutation_possible": False, "selected_job_claim_verified": False,
                  "errors": [], "job_id": args.job_id}
        supervision.atomic_json(artifacts / "controller-report.json", report)
        engine = None
        with ExitStack() as stack:
            stack.enter_context(exclusive_file(JOURNALS / "operation.lock"))
            # Held for the entire engine+attempt+quiet period, not just a check.
            for relative in ("service.lock", "engine.lock", "cockpit/launch.lock", "cockpit/server.lock",
                             "workspace/supervisor.lock", "workspace/ui.lock", "workspace/reconcile.lock"):
                path = data / relative
                if path.parent.exists():
                    stack.enter_context(exclusive_file(path))
            writer_fd = stack.enter_context(exclusive_file(data / "database-writer.lock"))
            require_quiescent(data, {parent["pid"]})
            journal = AttemptJournal(JOURNALS, baseline, args.run_id, args.admission_sha256)
            private_run = JOURNALS / args.run_id
            private_run.mkdir(mode=0o700)
            canonical(private_run, directory=True, private=True)
            from devbrain.config import Settings
            require(not any(key.startswith("DEVBRAIN_ARCADE_") for key in os.environ), "production_database_env_override")
            settings = Settings.load(data, create=False)
            require(settings.arcade_version == "26.11.1", "production_engine_version_not_pinned")
            settings.java_executable, settings.tmux_executable = runtime["java_executable"], runtime["tmux_executable"]
            engine = ProductionEngine(settings, runtime, scope, supervision, private_run, library_manifest)
            try:
                require_quiescent(data, {parent["pid"]})
                report["production_mutation_possible"] = True
                supervision.atomic_json(artifacts / "controller-report.json", report)
                engine.start()
                store = existing_store(settings, writer_fd)
                require(canonical_export(store.export()) == canonical_export(before), "production_changed_since_backup")
                journal.advance("claim_pending")
                job = claim_failed_selected(store, baseline, "production-retry-" + args.run_id)
                report["selected_job_claim_verified"] = True
                journal.advance("claimed")
                journal.advance("execution_started")
                asyncio.run(execute_selected(store, settings, job))
                journal.advance("attempt_finished")
                report["result"] = verify_attempt_result(before, store.export(), job)
                journal.advance("verified")
                report["workload_criteria_passed"] = True
            finally:
                report.update(engine.stop())
                common = load_helper("validate-native-account080.py")
                report["cleanup_grace_seconds"] = common.quiet_scope(scope, supervision, parent)
                require_quiescent(data, {parent["pid"]})
                report["cleanup_complete"] = True
        require(guard.source_manifest(source) == admission["source_hashes"], "final_source_changed")
        guard.verify_runtime_files(runtime)
        require(sha(admission_path) == args.admission_sha256, "admission_changed")
        package_matches_source(source)
        report["status"] = STATUS
    except Exception as exc:
        code = str(exc) if isinstance(exc, Refusal) else "production_retry_precondition_or_execution_failed"
        if report is None:
            print(json.dumps({"status": "refused", "error_code": code, "production_modified": False}))
            return 1
        report["errors"].append(code)
        report["status"] = "failed_unknown_or_incomplete_no_automatic_retry"
    finally:
        if report is not None:
            supervision.atomic_json(artifacts / "production-retry-results.json", report)
            supervision.atomic_json(artifacts / "controller-report.json", report)
    return 0 if report["status"] == STATUS else 1


if __name__ == "__main__":
    raise SystemExit(main())
