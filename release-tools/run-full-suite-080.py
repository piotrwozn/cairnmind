"""Explicitly gated full-suite controller; qualification requires the external receipt.

Run only under the qualified isolated installation and the supervised wrapper.
This file is prepared tooling, not evidence that a full suite has run.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def attestor():
    spec = importlib.util.spec_from_file_location("full_suite_attestation", Path(__file__).with_name("attest-full-suite-080.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(path, value):
    temporary = path.with_suffix(path.suffix + ".pending")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def isolated_origin(source, guard):
    guard.require(sys.flags.isolated == 1, "Package identity must be probed with Python -I")
    spec = importlib.util.find_spec("devbrain")
    guard.require(spec is not None and spec.origin is not None, "DevBrain is not installed in the isolated interpreter")
    origin = Path(spec.origin).resolve(strict=True)
    guard.require(origin == source / "src/devbrain/__init__.py", "Isolated child imports a different DevBrain checkout")
    return {"origin": str(origin), "sha256": guard.digest(origin),
            "python_executable": str(Path(sys.executable).resolve()), "python_prefix": str(Path(sys.prefix).resolve())}


def await_owned_exit(root, run_id, guard):
    """Allow fixture JVM shutdown to finish; never signal a process here."""
    spec = importlib.util.spec_from_file_location("suite_cleanup_observer", Path(__file__).with_name("supervise-080.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    proof = module.read_json(root / "mapping-proof.json")
    guard.require(proof.get("run_id") == run_id, "Scope mapping belongs to a different run")
    scope = module.ScopeOwnership(proof.get("scope_unit"))
    supervisor = module.identity(os.getppid())
    guard.require(supervisor is not None and scope.owns(supervisor), "Qualified supervisor is no longer the controller parent")
    def remaining():
        guard.require(module.same_process(supervisor) and os.getppid() == supervisor["pid"], "Supervisor identity changed")
        result = []
        for row in scope.members(exclude={supervisor["pid"]}):
            # The supervisor's bounded dmesg observation is not a fixture leak.
            # Exclude only its exact, still-identical direct probe child. The
            # outer Windows scope-empty observation still covers that probe.
            if row.get("parent_pid") == supervisor["pid"] and row["executable"] == str(Path("/usr/bin/dmesg").resolve()):
                try:
                    command = (Path("/proc") / str(row["pid"]) / "cmdline").read_bytes()
                except (FileNotFoundError, ProcessLookupError):
                    continue
                if command == b"/usr/bin/dmesg\0--raw\0" and scope.owns(row):
                    continue
            result.append(row)
        return result
    deadline = time.monotonic() + 10
    while remaining() and time.monotonic() < deadline:
        time.sleep(.1)
    guard.require(not remaining(), "Owned fixture descendants remained after pytest exit")


class CollectionProof:
    def __init__(self, root, run_id, collect, guard):
        self.root, self.run_id, self.collect, self.guard = root, run_id, collect, guard
        self.nodeids, self.errors, self.deselected = [], [], []

    def pytest_collection_modifyitems(self, session, config, items):
        for item in items:
            self.guard.require(not any(key == "nodeid" for key, _ in item.user_properties), "Conflicting nodeid property")
            item.user_properties.append(("nodeid", item.nodeid))

    def pytest_deselected(self, items):
        self.deselected.extend(item.nodeid for item in items)

    def pytest_collectreport(self, report):
        if report.failed:
            self.errors.append(report.nodeid)

    def pytest_collection_finish(self, session):
        self.nodeids = [item.nodeid for item in session.items]
        if not self.collect:
            expected = self.guard.document(self.root / "collection.json")
            self.guard.require(self.nodeids == expected["nodeids"] and not self.errors and not self.deselected,
                               "Execution collection differs from the complete frozen collection")

    def pytest_sessionfinish(self, session, exitstatus):
        if self.collect:
            write(self.root / "collection.json", {"schema_version": 1, "scope": "devbrain_full_suite_collection",
                  "run_id": self.run_id, "nodeids": self.nodeids, "deselected": self.deselected,
                  "collection_errors": self.errors, "full_test_directory": "tests",
                  "frozen_manifest_sha256": self.guard.digest(self.root / "frozen-source.json")})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--gate-report", type=Path, required=True)
    parser.add_argument("--java-home", type=Path, required=True)
    parser.add_argument("--tmux-bin", type=Path, required=True)
    parser.add_argument("--arcade-dist", type=Path, required=True)
    parser.add_argument("--expected-skips", type=Path)
    parser.add_argument("--phase", choices=("origin", "collect", "test"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    guard = attestor()
    guard.require(args.execute, "Explicit --execute is required")
    guard.require(os.environ.get("DEVBRAIN_QUALIFICATION_RUN_ID") == args.run_id, "Run under the external supervised wrapper")
    root, source = args.artifact_root.resolve(strict=True), args.source_root.resolve(strict=True)
    guard.require(root.is_relative_to(Path("/mnt/c")), "Evidence must be on the Windows /mnt/c filesystem")
    runtime = {}
    for name, path in (("python", Path(sys.executable)), ("java", args.java_home / "bin/java"), ("tmux", args.tmux_bin)):
        path = path.resolve(strict=True)
        runtime[name + "_executable"], runtime[name + "_binary_sha256"] = str(path), guard.digest(path)
    gate = guard.document(args.gate_report)
    guard.verify_gate(gate, runtime)
    if args.phase:
        origin = isolated_origin(source, guard)
        if args.phase == "origin":
            print(json.dumps(origin))
            return 0
        frozen = guard.document(root / "frozen-source.json")
        guard.require(frozen["source_hashes"] == guard.source_manifest(source), "Frozen source changed before pytest")
        os.chdir(source)
        import pytest
        plugin = CollectionProof(root, args.run_id, args.phase == "collect", guard)
        command = ["tests", "-q", "-o", "cache_dir=" + str(root / "pytest-cache")]
        command += ["--collect-only"] if args.phase == "collect" else ["--junitxml=" + str(root / "pytest.xml")]
        return int(pytest.main(command, plugins=[plugin]))

    guard.require(not (root / "controller-report.json").exists() and not (root / "frozen-source.json").exists(),
                  "Never reuse an existing full-suite run")
    copied_gate = root / "gate.json"
    if copied_gate.exists():
        guard.require(copied_gate.read_bytes() == args.gate_report.read_bytes(), "Wrapper and controller gate differ")
    else:
        copied_gate.write_bytes(args.gate_report.read_bytes())
    manifest = guard.source_manifest(source)
    report = {"schema_version": 1, "scope": "devbrain_final_full_suite_run", "run_id": args.run_id,
              "python_launcher": str(Path(sys.executable).absolute()),
              "status": "running", "runtime": runtime, "runtime_gate_sha256": guard.digest(copied_gate),
              "source_root": str(source), "source_hashes": manifest, "source_unchanged": False,
              "cleanup_complete": False, "controller_exit_verified": False,
              "controller_sha256": guard.digest(Path(__file__)), "final_full_suite_pass": False}
    write(root / "controller-report.json", report)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("DEVBRAIN_") and key not in {"PYTHONPATH", "PYTHONHOME", "PYTHONOPTIMIZE", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS", "LD_PRELOAD", "LD_LIBRARY_PATH"}}
    executable_dirs = [Path(runtime["tmux_executable"]).parent, Path(runtime["java_executable"]).parent,
                       Path(sys.executable).parent, Path.home() / ".local/bin"]
    linux_path = [str(path) for path in executable_dirs] + ["/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin"]
    env.update(DEVBRAIN_QUALIFICATION_RUN_ID=args.run_id, DEVBRAIN_ARCADE_DIST=str(args.arcade_dist.resolve()),
               DEVBRAIN_JAVA_EXECUTABLE=runtime["java_executable"], DEVBRAIN_TMUX_EXECUTABLE=runtime["tmux_executable"],
               DEVBRAIN_TEST_TMUX=runtime["tmux_executable"], JAVA_HOME=str(args.java_home.resolve()),
               PATH=os.pathsep.join(dict.fromkeys(linux_path)))
    command = [sys.executable, "-I", "-B", str(Path(__file__).resolve()), *sys.argv[1:]]
    try:
        probe = subprocess.run([*command, "--phase", "origin"], env=env, capture_output=True, text=True, timeout=15, check=True)
        report["isolated_package"] = json.loads(probe.stdout)
        skips = guard.document(args.expected_skips) if args.expected_skips else {}
        write(root / "frozen-source.json", {"schema_version": 1, "scope": "devbrain_frozen_full_suite_source",
              "run_id": args.run_id, "source_hashes": manifest, "runtime": runtime,
              "runtime_gate_sha256": report["runtime_gate_sha256"], "expected_skips": skips})
        report["frozen_manifest_sha256"] = guard.digest(root / "frozen-source.json")
        for phase in ("collect", "test"):
            with (root / (phase + ".log")).open("w", encoding="utf-8") as log:
                # The outer Linux supervisor owns cancellation, timeout and
                # pidfd cleanup for all descendants with this run marker.
                process = subprocess.Popen([*command, "--phase", phase], env=env, stdout=log, stderr=subprocess.STDOUT)
                code = process.wait()
            report["collection_returncode" if phase == "collect" else "pytest_returncode"] = code
            write(root / "controller-report.json", report)
            guard.require(code == 0, "Pytest phase failed: " + phase)
        collection = guard.document(root / "collection.json")
        report["counts"] = guard.junit_results(root / "pytest.xml", collection["nodeids"], skips)
        report["collection_sha256"] = guard.digest(root / "collection.json")
        report["junit_sha256"] = guard.digest(root / "pytest.xml")
        report["source_unchanged"] = manifest == guard.source_manifest(source)
        guard.require(report["source_unchanged"], "Source changed while the full suite ran")
        guard.verify_runtime_files(runtime)
        guard.require(guard.digest(args.gate_report) == report["runtime_gate_sha256"], "Runtime gate changed during suite")
        await_owned_exit(root, args.run_id, guard)
        report.update(status="full_suite_completed_pending_attestation", cleanup_complete=True)
    except Exception as exc:  # noqa: BLE001 - retain incomplete evidence without granting qualification
        report.update(status="failed", error_type=type(exc).__name__, error=str(exc))
    finally:
        write(root / "controller-report.json", report)
    return 0 if report["status"] == "full_suite_completed_pending_attestation" else 1


if __name__ == "__main__":
    raise SystemExit(main())
