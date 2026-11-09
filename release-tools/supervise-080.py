#!/usr/bin/env python3
"""Linux half of the external qualification launcher. Inert without --execute.

This supervises an already qualified runtime. It cannot certify its own Python
finalization; the Windows parent must observe our real exit and a further grace.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import subprocess
import sys
import time
from uuid import uuid4

SCHEMA = 1
REQUIRED = {"gate.json", "controller-report.json", "supervisor-report.json"}
RUNTIME_KEYS = tuple(f"{name}_{suffix}" for name in ("python", "java", "tmux") for suffix in ("executable", "binary_sha256"))
GATE_CHECKS = {"ssl_candidate", "unit", "native_evaluation", "c2_repetitions", "native_children", "engines_stopped"}
ALLOWED_ENV = {"PATH", "LANG", "LC_ALL", "TZ", "TMPDIR", "UV_CACHE_DIR", "UV_OFFLINE", "UV_PYTHON_DOWNLOADS",
               "JAVA_HOME", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
               "DEVBRAIN_ARCADE_DIST", "DEVBRAIN_TEST_ARCADE_HOME", "DEVBRAIN_TEST_UPDATER_PATH",
               "DEVBRAIN_JAVA_EXECUTABLE", "DEVBRAIN_TMUX_EXECUTABLE"}
FATAL = re.compile(r"segfault|segmentation fault|fatal signal|SIGSEGV|SIGABRT|invalid opcode|general protection fault|oom-kill|Out of memory|Kernel panic", re.I)
CONTROLLERS = {"run-full-suite-080.py": "full_suite_completed_pending_attestation",
               "soak-workload-080.py": "workload_passed_pending_external_watch",
               "test_devbrain_update080_smoke.py": "upgrade_smoke_completed_pending_attestation",
               "validate-native-account080.py": "native_account_completed_pending_attestation",
               "validate-copilot-account080.py": "copilot_account_completed_pending_attestation",
               "replay-failed-queue-080.py": "queue_copy_completed_pending_external_watch",
               "retry-production-queue-080.py": "production_one_job_completed_pending_external_watch",
               "backup-production080.py": "production_backup_completed_pending_external_watch",
               "deploy-production080.py": "production_deployment_completed_pending_external_watch",
               "run-interactive-ux080.py": "interactive_ux_completed_pending_external_watch",
               "run-regressions-080.py": "regressions_completed_pending_external_watch",
               "run-baseline-ux072.py": "baseline_visual_completed_pending_external_watch",
               "test_supervision080_fixture.py": "fixture_completed_pending_external_watch"}
CONTROLLER_DEPENDENCIES = {
    "test_devbrain_update080_smoke.py": ("validate-native-account080.py", "test_devbrain_update071_smoke.py"),
    "validate-native-account080.py": ("validate-native-account080.py",),
    "validate-copilot-account080.py": ("validate-native-account080.py", "validate-copilot-account-e2e.py"),
    "replay-failed-queue-080.py": ("run-full-suite-080.py",),
    "retry-production-queue-080.py": ("validate-native-account080.py",),
    "backup-production080.py": ("retry-production-queue-080.py", "validate-native-account080.py"),
    "deploy-production080.py": ("retry-production-queue-080.py", "validate-native-account080.py"),
    "run-interactive-ux080.py": ("validate-native-account080.py", "open-interactive-ux080.ps1"),
    "run-regressions-080.py": ("validate-native-account080.py", "validate-local-recall.py"),
    "run-baseline-ux072.py": ("run-interactive-ux080.py", "validate-native-account080.py", "open-interactive-ux080.ps1",
                              "build-devbrain-update-080.py", "devbrain-update080-template.py"),
}
CONTROLLER_ARTIFACTS = {
    "validate-native-account080.py": {"native-results.json"},
    "validate-copilot-account080.py": {"copilot-results.json"},
    "retry-production-queue-080.py": {"production-admission.json", "production-retry-results.json"},
    "backup-production080.py": {"production-backup-admission.json", "production-backup-results.json"},
    "deploy-production080.py": {"production-deployment-admission.json", "production-deployment-results.json"},
    "run-interactive-ux080.py": {"interactive-ready.json", "interactive-results.json"},
    "run-regressions-080.py": {"regression-source.json", "regressions-results.json"},
    "run-baseline-ux072.py": {"interactive-ready.json", "interactive-results.json"},
}
SCOPE_PATTERN = re.compile(r"devbrain-q-[0-9a-f]{32}\.scope\Z")


def controller_artifacts(name, argv):
    result = set(CONTROLLER_ARTIFACTS.get(name, ()))
    if name == "run-regressions-080.py":
        # One explicit phase pair keeps Windows/Linux evidence expectations
        # identical; ambiguous duplicate or --phase=value spellings are refused.
        positions = [index for index, value in enumerate(argv) if value == "--phase"]
        if len(positions) != 1 or positions[0] + 1 >= len(argv) or any(value.startswith("--phase=") for value in argv):
            raise ValueError("Regression controller needs one explicit --phase value")
        phase = argv[positions[0] + 1]
        phases = {
            "guards": {"guards-collection.json", "guards.xml"},
            "ux": {"ux-collection.json", "ux.xml"},
            "frontend": {"frontend-unit.xml", "frontend-browser.xml", "frontend-build-manifest.json",
                         "frontend-audit-all.json", "frontend-audit-runtime.json"},
            "recall": {"recall-results.json", "recall/checkpoint.json", "recall/cases.jsonl", "recall/resources.jsonl"},
        }
        if phase not in phases:
            raise ValueError("Unknown regression phase")
        result.update(phases[phase])
    return result


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def atomic_json(path, value):
    temporary = path.with_name("." + path.name + "." + uuid4().hex)
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("Missing, linked or oversized JSON: " + str(path))
    return json.loads(path.read_text(encoding="utf-8-sig"))


def safe_artifact(root, relative):
    if not isinstance(relative, str) or "\0" in relative or any(item in {"", ".", ".."} for item in relative.split("/")):
        raise ValueError("Artifacts must be normalized relative POSIX paths")
    part = PurePosixPath(relative)
    if (not isinstance(relative, str) or "\\" in relative or part.is_absolute()
            or not part.parts or any(item in {"..", "."} for item in part.parts)):
        raise ValueError("Artifacts must be relative POSIX paths")
    path = root.joinpath(*part.parts)
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("Artifact path leaves its root")
    return path


def qualified_runtime(gate):
    if (gate.get("schema_version") != 1 or gate.get("scope") != "bounded_runtime_qualification"
            or gate.get("status") != "pass"):
        raise ValueError("A passed existing runtime gate is required")
    checks = gate.get("checks", {})
    if not isinstance(checks, dict) or not GATE_CHECKS <= checks.keys():
        raise ValueError("Runtime gate is missing mandatory checks")
    if any(not isinstance(value, dict) or value.get("pass") is not True for value in checks.values()):
        raise ValueError("Every runtime gate check must pass")
    runtime = gate.get("runtime", {})
    result = {}
    for name in ("python", "java", "tmux"):
        executable, expected = runtime.get(name + "_executable"), runtime.get(name + "_binary_sha256")
        if (not isinstance(executable, str) or not Path(executable).is_absolute()
                or str(Path(executable).resolve()) != executable
                or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)):
            raise ValueError("Runtime identity is not canonical: " + name)
        path = Path(executable)
        if not path.is_file() or not os.access(path, os.X_OK) or digest(path) != expected:
            raise ValueError("Runtime bytes differ from the gate: " + name)
        result[name + "_executable"], result[name + "_binary_sha256"] = executable, expected
    if (Path(sys._base_executable).resolve() != Path(result["python_executable"])
            or Path("/proc/self/exe").resolve() != Path(result["python_executable"])):
        raise ValueError("Supervisor is not using the gate's exact Python binary")
    return result


def identity(pid):
    path = Path("/proc") / str(pid)
    try:
        fields = (path / "stat").read_text().rpartition(")")[2].split()
        uid = path.stat().st_uid
        executable = str((path / "exe").resolve(strict=True)) if uid == os.getuid() and fields[0] not in {"Z", "X"} else ""
        return {"pid": pid, "birth": fields[19], "state": fields[0], "uid": uid, "executable": executable,
                "parent_pid": int(fields[1])}
    except (FileNotFoundError, ProcessLookupError):
        return None


def same_process(row):
    current = identity(row["pid"])
    return bool(current and current["state"] not in {"Z", "X"}
                and all(current[key] == row[key] for key in ("birth", "uid", "executable")))


def process_cgroup(pid):
    try:
        rows = (Path("/proc") / str(pid) / "cgroup").read_text().splitlines()
    except (FileNotFoundError, ProcessLookupError):
        return None
    groups = [line.removeprefix("0::") for line in rows if line.startswith("0::")]
    if len(groups) != 1:
        raise RuntimeError("A unified cgroup v2 ownership boundary is required")
    return groups[0]


class ScopeOwnership:
    """Own a UUID scope, including descendants that deliberately clear their env.

    Membership never substitutes for process birth/UID identity. Signals still
    use pidfds, with identity and group membership checked after opening them.
    The external Windows parent alone can prove the scope empty after our exit.
    """
    def __init__(self, unit):
        if not isinstance(unit, str) or not SCOPE_PATTERN.fullmatch(unit):
            raise ValueError("Invalid qualification scope identity")
        group = process_cgroup(os.getpid())
        if (not group or not group.startswith("/user.slice/") or not group.endswith("/" + unit)
                or any(part in {".", ".."} for part in PurePosixPath(group).parts)):
            raise RuntimeError("Supervisor/controller is outside its Windows-owned UUID scope")
        self.unit, self.group = unit, group
        self.path = Path("/sys/fs/cgroup") / group.lstrip("/")
        stat = self.path.stat()
        self.inode = f"{stat.st_dev}:{stat.st_ino}"
        self.check()

    def check(self):
        stat = self.path.stat()
        if (f"{stat.st_dev}:{stat.st_ino}" != self.inode or process_cgroup(os.getpid()) != self.group
                or self.path.resolve() != self.path):
            raise RuntimeError("Owned cgroup identity changed")

    def owns(self, row):
        self.check()
        if row.get("uid") != os.getuid() or not same_process(row):
            return False
        group = process_cgroup(row["pid"])
        return bool(group and (group == self.group or group.startswith(self.group + "/")))

    def members(self, *, exclude=()):
        self.check()
        excluded = {os.getpid(), *exclude}
        pids = set()
        for path in (self.path / "cgroup.procs", *self.path.rglob("*/cgroup.procs")):
            try:
                pids.update(int(value) for value in path.read_text().split())
            except FileNotFoundError:  # a finished delegated subgroup disappeared
                if path.parent == self.path:
                    raise
        rows = []
        for pid in sorted(pids - excluded):
            row = identity(pid)
            if not row or row["state"] in {"Z", "X"}:
                continue
            if row["uid"] != os.getuid():
                raise RuntimeError("Unexpected foreign UID in the owned scope; refusing to signal it")
            if self.owns(row):
                rows.append(row)
        return rows

    def stop(self):
        forced = False
        for number in (signal.SIGTERM, signal.SIGKILL):
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                rows = self.members()
                if not rows:
                    return forced, []
                forced = True
                for row in rows:
                    if not self.owns(row):
                        continue
                    try:
                        handle = os.pidfd_open(row["pid"])
                    except ProcessLookupError:
                        continue
                    try:
                        if self.owns(row):
                            signal.pidfd_send_signal(handle, number)
                    except ProcessLookupError:
                        pass
                    finally:
                        os.close(handle)
                time.sleep(.1)
        return forced, self.members()


def tagged_processes(run_id, *, skipped=None):
    """Best-effort global discovery, separate from strict owned-cgroup proof.

    A different UID is never inspected. Inaccessible same-UID processes may be
    skipped only when their readable cgroup proves they are outside our verified
    UUID boundary and no matching run marker has been observed. Known members
    and known matching markers always fail closed on missing process evidence.
    """
    marker = ("DEVBRAIN_QUALIFICATION_RUN_ID=" + run_id).encode()
    own_group = process_cgroup(os.getpid())
    bounded = bool(own_group and own_group.startswith("/user.slice/")
                   and SCOPE_PATTERN.fullmatch(own_group.rsplit("/", 1)[-1]))
    rows = []
    for path in Path("/proc").iterdir():
        if not path.name.isdigit() or int(path.name) == os.getpid():
            continue
        group, marked = None, False
        try:
            # Do not read exe/environ of a foreign UID, including protected
            # service processes that happen to be visible in this /proc mount.
            if path.stat().st_uid != os.getuid():
                continue
            group = process_cgroup(int(path.name))
            if group is None:
                continue
            if marker not in (path / "environ").read_bytes().split(b"\0"):
                continue
            marked = True
            row = identity(int(path.name))
            if not row or row["state"] in {"Z", "X"} or row["uid"] != os.getuid():
                continue
            # Re-read the marker between the two birth/UID/exe observations;
            # discovery before identity alone could race with PID reuse.
            if marker in (path / "environ").read_bytes().split(b"\0") and same_process(row):
                rows.append(row)
        except PermissionError:
            outside = bool(bounded and group and group != own_group and not group.startswith(own_group + "/"))
            if marked or not outside:
                raise
            entry = {"pid": int(path.name), "control_group": group,
                     "reason": "inaccessible_same_uid_outside_owned_scope"}
            if skipped is not None and entry not in skipped:
                skipped.append(entry)
        except (FileNotFoundError, ProcessLookupError):
            continue
    return rows


def stop_tagged(run_id):
    """No numeric PID/PGID signalling. Every signal uses a checked pidfd."""
    forced = False
    for number in (signal.SIGTERM, signal.SIGKILL):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            rows = tagged_processes(run_id)
            if not rows:
                return forced, []
            forced = True
            for row in rows:
                if not same_process(row):
                    continue
                try:
                    handle = os.pidfd_open(row["pid"])
                except ProcessLookupError:
                    continue
                try:
                    if same_process(row):
                        signal.pidfd_send_signal(handle, number)
                except ProcessLookupError:
                    pass
                finally:
                    os.close(handle)
            time.sleep(.1)
    return forced, tagged_processes(run_id)


def boot_id():
    value = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if not re.fullmatch(r"[a-f0-9-]{36}", value):
        raise RuntimeError("Boot identity is unavailable")
    return value


def crash_metadata(directory):
    if not directory.is_dir():
        raise RuntimeError("WSL crash metadata is unavailable")
    return {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in directory.iterdir()
            if p.is_file() and p.name.startswith("wsl-crash-")}


def kernel_fatals():
    result = subprocess.run(["/usr/bin/dmesg", "--raw"], capture_output=True, text=True, timeout=5, check=False)
    if result.returncode or len(result.stdout) > 8 * 1024 * 1024:
        raise RuntimeError("Bounded kernel observation is unavailable")
    return {line for line in result.stdout.splitlines() if FATAL.search(line)}


class Observation:
    def __init__(self, directory):
        self.directory = directory
        self.initial_boot = boot_id()
        self.files = crash_metadata(directory)
        self.kernel = kernel_fatals()
        self.new_files, self.new_kernel = set(), set()
        self.final_boot = self.initial_boot

    def poll(self):
        self.final_boot = boot_id()
        self.new_files.update(name for name, meta in crash_metadata(self.directory).items() if self.files.get(name) != meta)
        self.new_kernel.update(kernel_fatals() - self.kernel)
        return self.final_boot != self.initial_boot or bool(self.new_files or self.new_kernel)


class ParentControl:
    def __init__(self, root, run_id, nonce):
        self.root, self.run_id, self.nonce = root, run_id, nonce
        self.last_sequence, self.last_change = -1, time.monotonic()

    def check(self):
        if (self.root / "cancel.json").exists():
            raise RuntimeError("Windows parent requested cancellation")
        beat = read_json(self.root / "heartbeat.json")
        if beat.get("run_id") != self.run_id or beat.get("nonce") != self.nonce:
            raise RuntimeError("Windows heartbeat identity mismatch")
        sequence, stamp = beat.get("sequence"), beat.get("unix_time")
        if type(sequence) is not int or type(stamp) not in {int, float} or not -3 < time.time() - stamp < 15:
            raise RuntimeError("Windows heartbeat is missing or stale")
        if sequence > self.last_sequence:
            self.last_sequence, self.last_change = sequence, time.monotonic()
        elif sequence < self.last_sequence or time.monotonic() - self.last_change >= 15:
            raise RuntimeError("Windows heartbeat stopped advancing")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--controller", type=Path, required=True)
    parser.add_argument("--arguments-file", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--windows-nonce", required=True)
    parser.add_argument("--scope-unit", required=True)
    parser.add_argument("--crash-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, required=True)
    parser.add_argument("--cleanup-grace", type=int, default=30)
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({"execute": False, "message": "No process or probe started. Supply --execute through the Windows parent."}))
        return 0
    if (not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", args.run_id) or args.timeout_seconds < 1
            or args.timeout_seconds > 24 * 3600 or args.cleanup_grace < 30 or args.cleanup_grace > 120):
        parser.error("Invalid run id, timeout or cleanup grace")
    root = args.artifact_root
    if (not root.is_absolute() or not str(root).startswith("/mnt/c/") or root.resolve() != root or not root.is_dir()
            or (root / "supervisor-report.json").exists()):
        parser.error("Use a canonical fresh /mnt/c artifact directory")
    proof = read_json(root / "mapping-proof.json")
    mapped = "/mnt/c/" + str(proof.get("windows_root", ""))[3:].replace("\\", "/")
    if (not str(proof.get("windows_root", "")).startswith("C:\\") or mapped != str(root)
            or proof.get("linux_root") != str(root) or proof.get("run_id") != args.run_id
            or proof.get("nonce") != args.windows_nonce or proof.get("scope_unit") != args.scope_unit):
        parser.error("Windows/Linux artifact mapping proof does not match")
    controller = args.controller
    if not controller.is_absolute() or controller.resolve() != controller or not controller.is_file() or controller.name not in CONTROLLERS:
        parser.error("Controller must be a canonical absolute file")
    if args.arguments_file.resolve() != args.arguments_file or not args.arguments_file.is_relative_to(root):
        parser.error("Arguments JSON must be inside the mapped artifact directory")
    specification = read_json(args.arguments_file)
    if (specification.get("run_id") != args.run_id
            or not {"run_id", "argv", "env", "required_artifacts"} <= specification.keys()
            or not specification.keys() <= {"run_id", "argv", "env", "required_artifacts", "python_launcher"}):
        parser.error("Controller specification keys or run identity do not match")
    argv, environment, required = specification["argv"], specification["env"], specification["required_artifacts"]
    if (not isinstance(argv, list) or not all(isinstance(value, str) and "\0" not in value for value in argv)
            or not isinstance(environment, dict) or not environment.keys() <= ALLOWED_ENV
            or not all(isinstance(value, str) and "\0" not in value for value in environment.values())
            or not isinstance(required, list) or not all(isinstance(value, str) for value in required)
            or not REQUIRED <= set(required) or "receipt.json" in required or len(required) != len(set(required))):
        parser.error("Invalid argv, environment allowlist or required artifact list")
    for relative in required:
        safe_artifact(root, relative)
    if not controller_artifacts(controller.name, argv) <= set(required):
        parser.error("Missing controller-specific result artifact declaration")
    gate_path = root / "gate.json"
    runtime = qualified_runtime(read_json(gate_path))
    launcher = Path(specification.get("python_launcher", runtime["python_executable"]))
    if not launcher.is_absolute() or launcher.resolve() != Path(runtime["python_executable"]) or digest(launcher) != runtime["python_binary_sha256"]:
        raise ValueError("Controller's literal Python launcher does not resolve to the qualified base binary")
    helper_paths = {path.name: path for path in (controller, Path(__file__).resolve(),
                    Path(__file__).with_name("attest-full-suite-080.py"), Path(__file__).with_name("validate-stability-080.py"),
                    Path(__file__).with_name("qualification-power-080.ps1"))}
    for name in CONTROLLER_DEPENDENCIES.get(controller.name, ()):
        helper_paths[name] = Path(__file__).with_name(name)
    helper_hashes = {name: digest(path) for name, path in helper_paths.items()}
    gate_hash, controller_hash = digest(gate_path), digest(controller)
    supervisor_hash, arguments_hash = digest(Path(__file__)), digest(args.arguments_file)
    parent = ParentControl(root, args.run_id, args.windows_nonce)
    parent.check()
    ownership = ScopeOwnership(args.scope_unit)
    if ownership.members():
        raise RuntimeError("Fresh qualification scope already contains other processes")
    skipped_external = []
    if tagged_processes(args.run_id, skipped=skipped_external):
        raise RuntimeError("Run id already owns live processes; refusing to adopt them")
    observation = Observation(args.crash_dir)
    report = {"schema_version": SCHEMA, "scope": "devbrain_linux_process_supervision", "run_id": args.run_id,
              "passed": False, "child_returncode": None, "controller_exit_verified": False, "cleanup_complete": False,
              "forced_cleanup": False, "native_failure": False, "errors": [], "runtime": runtime,
              "scope_unit": ownership.unit, "control_group": ownership.group,
              "scope_cgroup_identity": ownership.inode, "scope_membership_verified": True,
              "scope_descendants_stopped": False, "remaining_scope_processes": [],
              "unobservable_external_processes": skipped_external,
              "controller": str(controller), "controller_sha256": controller_hash, "supervisor_sha256": supervisor_hash,
              "python_launcher": str(launcher), "python_launcher_base_sha256": runtime["python_binary_sha256"],
              "helper_sha256_start": helper_hashes,
              "gate_sha256": gate_hash, "arguments_sha256": arguments_hash, "controller_report": "controller-report.json",
              "initial_boot_id": observation.initial_boot, "cleanup_grace_seconds": args.cleanup_grace,
              "external_controller_finalization_required": True, "started_unix_time": time.time()}
    interrupted = []
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda value, frame: interrupted.append(value))
    child = None
    try:
        child_env = {key: value for key, value in os.environ.items() if key in {"HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "TZ"}}
        child_env.update(environment)
        child_env["DEVBRAIN_QUALIFICATION_RUN_ID"] = args.run_id
        child_env["DEVBRAIN_JAVA_EXECUTABLE"], child_env["DEVBRAIN_TMUX_EXECUTABLE"] = runtime["java_executable"], runtime["tmux_executable"]
        child_env["JAVA_HOME"] = str(Path(runtime["java_executable"]).parent.parent)
        with (root / "controller.stdout.log").open("xb") as stdout, (root / "controller.stderr.log").open("xb") as stderr:
            child = subprocess.Popen([str(launcher), "-I", "-B", str(controller), *argv], cwd=root,
                                     env=child_env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True)
            report["child_identity"] = identity(child.pid)
            if process_cgroup(child.pid) != ownership.group:
                raise RuntimeError("Controller did not inherit the owned scope")
            deadline = time.monotonic() + args.timeout_seconds
            next_observation = 0
            while child.poll() is None:
                parent.check()
                ownership.check()
                if interrupted or time.monotonic() >= deadline:
                    raise RuntimeError("Supervisor cancelled or controller timed out")
                if time.monotonic() >= next_observation:
                    if observation.poll():
                        report["native_failure"] = True
                        raise RuntimeError("A new native failure or boot change was observed")
                    next_observation = time.monotonic() + 2
                time.sleep(.2)
            report["child_returncode"], report["controller_exit_verified"] = child.returncode, True
            if child.returncode != 0:
                raise RuntimeError("Controller returned a nonzero exit code")
    except Exception as exc:  # noqa: BLE001 — every failure is evidence, never a PASS
        report["errors"].append(type(exc).__name__ + ": " + str(exc))
    finally:
        try:
            forced, remaining = ownership.stop()
            report["forced_cleanup"], report["remaining_scope_processes"] = forced, remaining
            if forced:
                report["errors"].append("Forced cleanup was required")
            if child is not None:
                report["child_returncode"] = child.wait(timeout=5)
                report["controller_exit_verified"] = True
            report["cleanup_complete"] = not remaining and (child is None or child.poll() is not None)
        except Exception as exc:  # noqa: BLE001 — uncertain cleanup cannot pass
            report["errors"].append("Cleanup: " + str(exc))
        grace_deadline = time.monotonic() + args.cleanup_grace
        while time.monotonic() < grace_deadline:
            try:
                parent.check()
                report["native_failure"] = observation.poll() or report["native_failure"]
            except Exception as exc:  # noqa: BLE001 — missing observation fails closed
                report["errors"].append("Observation: " + str(exc))
                break
            time.sleep(1)
        try:
            remaining = ownership.members()
            if remaining:
                report["forced_cleanup"] = True
                ownership.stop()
                report["errors"].append("Scope descendants appeared after cleanup")
            report["remaining_scope_processes"] = ownership.members()
            report["remaining_tagged_processes"] = tagged_processes(args.run_id, skipped=skipped_external)
            report["scope_descendants_stopped"] = not report["remaining_scope_processes"]
            report["cleanup_complete"] = (report["cleanup_complete"] and report["scope_descendants_stopped"]
                                          and not report["remaining_tagged_processes"])
            report["helper_sha256_end"] = {name: digest(path) for name, path in helper_paths.items()}
            report["controller_sha256_end"] = digest(controller)
            if (report["helper_sha256_end"] != helper_hashes
                    or launcher.resolve() != Path(runtime["python_executable"]) or digest(launcher) != runtime["python_binary_sha256"]
                    or qualified_runtime(read_json(gate_path)) != runtime or digest(gate_path) != gate_hash
                    or digest(controller) != controller_hash or digest(Path(__file__)) != supervisor_hash
                    or digest(args.arguments_file) != arguments_hash):
                raise ValueError("Controller, supervisor, arguments, gate or runtime changed")
            result = read_json(root / "controller-report.json")
            if (result.get("run_id") != args.run_id or result.get("python_launcher") != str(launcher)
                    or {key: result.get("runtime", {}).get(key) for key in RUNTIME_KEYS} != runtime):
                raise ValueError("Controller report identity/runtime mismatch")
            status = result.get("status")
            if status != CONTROLLERS[controller.name]:
                raise ValueError("Unsupported or incomplete controller status")
            if result.get("cleanup_complete") is not True or (status == "workload_passed_pending_external_watch" and result.get("workload_criteria_passed") is not True):
                raise ValueError("Controller did not complete its workload and cleanup")
            report["controller_report_sha256"] = digest(root / "controller-report.json")
            for relative in required:
                if relative != "supervisor-report.json" and not safe_artifact(root, relative).is_file():
                    raise ValueError("Missing required artifact: " + relative)
        except Exception as exc:  # noqa: BLE001 — artifact verification must be explicit
            report["errors"].append("Final verification: " + str(exc))
        report.update(final_boot_id=observation.final_boot, new_wsl_dumps=sorted(observation.new_files),
                      new_kernel_fatals=sorted(observation.new_kernel), finished_unix_time=time.time())
        report["passed"] = (report["child_returncode"] == 0 and report["controller_exit_verified"]
                            and report["cleanup_complete"] and not report["forced_cleanup"]
                            and not report["native_failure"] and not report["errors"])
        atomic_json(root / "supervisor-report.json", report)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
