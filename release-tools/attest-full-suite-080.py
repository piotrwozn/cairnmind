"""Read-only qualification of a frozen full suite after external process observation.

This program never runs tests, starts services, or changes an input report. It
writes one new attestation. Missing or inconsistent evidence fails closed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

RUNTIME_NAMES = ("python", "java", "tmux")
GATE_CHECKS = {"ssl_candidate", "unit", "native_evaluation", "c2_repetitions", "native_children", "engines_stopped"}
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
BALANCED_GUID = "381b4222-f694-41f0-9685-ff5bb260df2e"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def document(path):
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    require(isinstance(value, dict), "Evidence must contain a JSON object")
    return value


def source_manifest(root):
    root = Path(root).resolve(strict=True)
    files = [root / "pyproject.toml", root / "uv.lock"]
    directories = [root / "src", root / "tests"]
    if (root / "data").exists() or (root / "data").is_symlink():
        directories.append(root / "data")
    for directory in directories:
        require(directory.is_dir() and not directory.is_symlink(), "Frozen source directories must be real directories")
        for path in sorted(directory.rglob("*")):
            require(not path.is_symlink(), "Frozen sources cannot contain symlinks")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}:
                files.append(path)
    result = {}
    for path in files:
        require(not path.is_symlink() and path.resolve(strict=True).is_relative_to(root), "Unsafe frozen source path")
        result[path.relative_to(root).as_posix()] = digest(path)
    return result


def artifact(root, relative):
    require(isinstance(relative, str) and "\\" not in relative, "Artifact names must be relative POSIX paths")
    name = PurePosixPath(relative)
    require(relative and not name.is_absolute() and ".." not in name.parts and ":" not in relative,
            "Artifact escaped its evidence directory")
    root = Path(root).resolve(strict=True)
    path = root.joinpath(*name.parts)
    require(not any(parent.is_symlink() for parent in (path, *path.parents) if parent != root and parent.is_relative_to(root)),
            "Artifact symlinks are not allowed")
    require(path.is_file() and path.resolve(strict=True).is_relative_to(root), "Missing or unsafe artifact: " + relative)
    return path


def verify_gate(gate, runtime):
    require(gate.get("schema_version") == 1 and gate.get("scope") == "bounded_runtime_qualification"
            and gate.get("status") == "pass", "Runtime gate has not passed")
    checks = gate.get("checks", {})
    require(GATE_CHECKS <= checks.keys() and all(item.get("pass") is True for item in checks.values()),
            "Every required runtime check must pass")
    for name in RUNTIME_NAMES:
        for suffix in ("_executable", "_binary_sha256"):
            key = name + suffix
            require(isinstance(runtime.get(key), str) and runtime[key] == gate.get("runtime", {}).get(key),
                    "Runtime differs from qualified gate: " + key)
        require(SHA256.fullmatch(runtime[name + "_binary_sha256"]), "Invalid runtime SHA256")


def verify_runtime_files(runtime):
    for name in RUNTIME_NAMES:
        path = Path(runtime[name + "_executable"])
        require(path.is_absolute() and str(path.resolve(strict=True)) == str(path), "Runtime path is not canonical")
        require(digest(path) == runtime[name + "_binary_sha256"], "Runtime binary changed: " + name)


def verify_helper_files(report, controller, *, windows):
    before, after = report.get("helper_sha256_start", {}), report.get("helper_sha256_end", {})
    required = {controller, "attest-full-suite-080.py", "validate-stability-080.py", "supervise-080.py", "qualification-power-080.ps1"}
    required.update({
        "test_devbrain_update080_smoke.py": {"validate-native-account080.py", "test_devbrain_update071_smoke.py"},
        "validate-native-account080.py": {"validate-native-account080.py"},
        "validate-copilot-account080.py": {"validate-native-account080.py", "validate-copilot-account-e2e.py"},
        "replay-failed-queue-080.py": {"run-full-suite-080.py"},
        "retry-production-queue-080.py": {"validate-native-account080.py"},
        "backup-production080.py": {"retry-production-queue-080.py", "validate-native-account080.py"},
        "deploy-production080.py": {"retry-production-queue-080.py", "validate-native-account080.py"},
        "run-interactive-ux080.py": {"validate-native-account080.py", "open-interactive-ux080.ps1"},
        "run-regressions-080.py": {"validate-native-account080.py", "validate-local-recall.py"},
        "run-baseline-ux072.py": {"run-interactive-ux080.py", "validate-native-account080.py", "open-interactive-ux080.ps1",
                                   "build-devbrain-update-080.py", "devbrain-update080-template.py"},
    }.get(controller, set()))
    if windows:
        required.add("run-supervised-080.ps1")
    require(isinstance(before, dict) and before == after and required <= before.keys(),
            "Qualification helpers changed or lack start/end hashes")
    for name in required:
        require(before[name] == digest(Path(__file__).with_name(name)), "Qualification helper differs from observed bytes: " + name)


def verify_power_observation(root, receipt):
    before, after = receipt.get("power_profile_start"), receipt.get("power_profile_end")
    require(isinstance(before, dict) and before == after and before.get("active_guid") == BALANCED_GUID
            and before.get("canonical_format") == "powercfg-qh-acdc-v1", "Stable Balanced power evidence is required")
    require(document(artifact(root, "gate.json")).get("power_profile") == before,
            "Power configuration differs from the qualified runtime gate")
    canonical = before.get("canonical_settings", "")
    require(isinstance(canonical, str) and canonical.startswith("scheme=" + BALANCED_GUID + "\n")
            and hashlib.sha256(canonical.encode("utf-8")).hexdigest() == before.get("settings_sha256")
            and type(before.get("setting_count")) is int and before["setting_count"] > 0
            and len(canonical.splitlines()) == before["setting_count"] + 1, "Invalid canonical AC/DC settings")
    inhibitor = receipt.get("sleep_inhibition", {})
    require(inhibitor.get("acquired") is True and inhibitor.get("restored") is True
            and inhibitor.get("requested_flags") == 0x80000001, "System sleep inhibition was not restored")
    monitor = receipt.get("power_monitor", {})
    require(monitor.get("changed") is False and monitor.get("interval_seconds") == 30
            and monitor.get("maximum_gap_seconds") == 45 and monitor.get("samples_artifact") == "power-samples.jsonl",
            "Power monitoring is incomplete")
    path = artifact(root, "power-samples.jsonl")
    require(receipt.get("artifact_sha256", {}).get("power-samples.jsonl") == digest(path), "Power sample digest differs")
    samples = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    require(len(samples) >= 2 and len(samples) == monitor.get("sample_count"), "Missing power samples")
    times = []
    for sample in samples:
        elapsed = sample.get("elapsed_seconds")
        require(type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0 and sample.get("state") == before,
                "Power observation changed or has invalid timing")
        times.append(elapsed)
    require(all(0 <= second - first <= 45 for first, second in zip(times, times[1:])), "Power observation has a gap")
    exit_at, empty_at = monitor.get("supervisor_exit_elapsed_seconds"), monitor.get("scope_empty_elapsed_seconds")
    require(type(exit_at) in (int, float) and math.isfinite(exit_at) and exit_at >= times[0]
            and type(empty_at) in (int, float) and math.isfinite(empty_at) and empty_at >= exit_at
            and times[-1] >= empty_at + 30, "Power observation ended before the post-cleanup grace")


def verify_owned_scope(receipt, supervisor):
    unit = receipt.get("scope_unit", "")
    require(isinstance(unit, str) and re.fullmatch(r"devbrain-q-[0-9a-f]{32}\.scope", unit), "Missing UUID scope identity")
    group = receipt.get("control_group", "")
    require(isinstance(group, str) and group.startswith("/user.slice/") and group.endswith("/" + unit)
            and not any(part in {".", ".."} for part in group.split("/")), "Unsafe cgroup ownership path")
    identity = receipt.get("scope_cgroup_identity", "")
    require(isinstance(identity, str) and re.fullmatch(r"[0-9]+:[0-9]+", identity), "Missing cgroup device/inode identity")
    require(all(receipt.get(key) is True for key in ("scope_initially_absent", "scope_membership_verified", "scope_empty_verified")),
            "Windows observer did not establish scope ownership and emptiness")
    grace = receipt.get("scope_cleanup_grace_seconds")
    require(type(grace) in (int, float) and math.isfinite(grace) and grace >= 30, "Scope was not observed empty for 30 seconds")
    require(all(supervisor.get(key) == receipt[key] for key in ("scope_unit", "control_group", "scope_cgroup_identity"))
            and supervisor.get("scope_membership_verified") is True and supervisor.get("scope_descendants_stopped") is True
            and supervisor.get("remaining_scope_processes") == [] and supervisor.get("remaining_tagged_processes") == [],
            "Supervisor scope identity or descendant cleanup differs")


def verify_external(root, run_id, runtime, required_artifacts, controller):
    receipt = document(artifact(root, "receipt.json"))
    supervisor = document(artifact(root, "supervisor-report.json"))
    require(receipt.get("schema_version") == 1 and receipt.get("scope") == "devbrain_external_process_watch",
            "An external process-watch receipt v1 is required")
    require(receipt.get("run_id") == run_id and receipt.get("passed") is True, "External watch did not pass for this run")
    for key in ("child_returncode", "supervisor_returncode"):
        require(type(receipt.get(key)) is int and receipt[key] == 0, "External process exit was not zero")
    for key in ("controller_exit_verified", "supervisor_exit_verified", "cleanup_complete"):
        require(receipt.get(key) is True, "External lifecycle evidence is incomplete: " + key)
    for key in ("forced_cleanup", "native_failure"):
        require(receipt.get(key) is False, "External lifecycle reported a failure: " + key)
    require(isinstance(receipt.get("cleanup_grace_seconds"), (int, float))
            and math.isfinite(receipt["cleanup_grace_seconds"]) and receipt["cleanup_grace_seconds"] >= 30,
            "External post-exit grace must be at least 30 seconds")
    require(bool(receipt.get("initial_boot_id")) and receipt["initial_boot_id"] == receipt.get("final_boot_id"),
            "External observation crossed a boot boundary")
    require(all(receipt.get(key) == [] for key in ("new_wsl_dumps", "new_kernel_fatals", "errors")),
            "External watch recorded a native failure or observation error")
    require(supervisor.get("schema_version") == 1 and supervisor.get("scope") == "devbrain_linux_process_supervision"
            and supervisor.get("run_id") == run_id, "Wrong Linux supervision report")
    require(supervisor.get("passed") is True and type(supervisor.get("child_returncode")) is int and supervisor["child_returncode"] == 0
            and supervisor.get("controller_exit_verified") is True and supervisor.get("cleanup_complete") is True
            and supervisor.get("forced_cleanup") is False and supervisor.get("native_failure") is False
            and all(supervisor.get(key) == [] for key in ("errors", "new_wsl_dumps", "new_kernel_fatals"))
            and supervisor.get("initial_boot_id") == receipt["initial_boot_id"]
            and supervisor.get("final_boot_id") == receipt["final_boot_id"],
            "Linux supervisor did not observe a clean controller exit")
    require(supervisor.get("controller_report") == "controller-report.json"
            and supervisor.get("controller_report_sha256") == digest(artifact(root, "controller-report.json")),
            "Supervisor is not bound to this controller report")
    require(all(supervisor.get("runtime", {}).get(key) == value for key, value in runtime.items()),
            "Supervisor/runtime identity differs from controller")
    require(all(receipt.get("runtime", {}).get(key) == value for key, value in runtime.items()),
            "Windows receipt/runtime identity differs from controller")
    launcher = supervisor.get("python_launcher")
    require(isinstance(launcher, str) and Path(launcher).is_absolute()
            and str(Path(launcher).resolve(strict=True)) == runtime["python_executable"]
            and digest(Path(launcher)) == runtime["python_binary_sha256"], "Unqualified literal Python launcher")
    require(receipt.get("python_launcher") == launcher
            and receipt.get("python_launcher_base_sha256") == supervisor.get("python_launcher_base_sha256") == runtime["python_binary_sha256"]
            and document(artifact(root, "controller-report.json")).get("python_launcher") == launcher,
            "Controller/supervisor/Windows literal launchers differ")
    verify_helper_files(supervisor, controller, windows=False)
    verify_helper_files(receipt, controller, windows=True)
    verify_owned_scope(receipt, supervisor)
    verify_power_observation(root, receipt)
    hashes = receipt.get("artifact_sha256", {})
    for relative in set(required_artifacts) | {"supervisor-report.json", "controller-report.json"}:
        require(hashes.get(relative) == digest(artifact(root, relative)), "External artifact hash mismatch: " + relative)
    return receipt


def junit_results(path, expected_nodeids, expected_skips):
    require(isinstance(expected_nodeids, list) and expected_nodeids and all(isinstance(node, str) and node for node in expected_nodeids),
            "Full collection must contain nodeids")
    require(len(expected_nodeids) == len(set(expected_nodeids)), "Collection contains duplicate nodeids")
    require(isinstance(expected_skips, dict) and set(expected_skips) <= set(expected_nodeids)
            and all(isinstance(reason, str) and reason for reason in expected_skips.values()), "Invalid expected-skip plan")
    raw = Path(path).read_bytes()
    require(b"<!DOCTYPE" not in raw.upper() and b"<!ENTITY" not in raw.upper(), "JUnit DTD/entities are forbidden")
    root = ET.fromstring(raw)
    require(root.tag in {"testsuite", "testsuites"}, "Unrecognized JUnit document")
    require(not list(root.iter("failure")) and not list(root.iter("error")), "JUnit contains failures or errors")
    for suite in (element for element in root.iter() if element.tag in {"testsuite", "testsuites"}):
        for key, tag in (("tests", "testcase"), ("failures", "failure"), ("errors", "error"), ("skipped", "skipped")):
            if key in suite.attrib:
                require(suite.attrib[key].isdigit() and int(suite.attrib[key]) == len(list(suite.iter(tag))),
                        "JUnit counters disagree with their actual elements")
    observed, skipped = {}, {}
    for case in root.iter("testcase"):
        nodeids = [item.get("value") for item in case.findall("./properties/property") if item.get("name") == "nodeid"]
        require(len(nodeids) == 1 and nodeids[0], "Every JUnit case requires one exact nodeid property")
        nodeid = nodeids[0]
        require(nodeid not in observed, "JUnit contains duplicate nodeids")
        observed[nodeid] = True
        skip = case.findall("skipped")
        require(len(skip) <= 1, "Duplicate JUnit skip outcome")
        if skip:
            reason = skip[0].get("message", "")
            require(expected_skips.get(nodeid) == reason, "Unexpected skipped/xfail test: " + nodeid)
            skipped[nodeid] = reason
    require(set(observed) == set(expected_nodeids), "JUnit nodeids do not exactly cover the full collection")
    return {"collected": len(expected_nodeids), "passed": len(observed) - len(skipped), "skipped": len(skipped),
            "failures": 0, "errors": 0, "unexpected_skips": 0, "duplicates": 0, "missing": 0,
            "expected_skips": expected_skips, "observed_skips": skipped}


def attest_full_suite(root, source_root):
    names = ("gate.json", "frozen-source.json", "collection.json", "pytest.xml", "controller-report.json")
    paths = {name: artifact(root, name) for name in names}
    gate, frozen, collection, run = (document(paths[name]) for name in ("gate.json", "frozen-source.json", "collection.json", "controller-report.json"))
    require(run.get("schema_version") == 1 and run.get("scope") == "devbrain_final_full_suite_run"
            and run.get("status") == "full_suite_completed_pending_attestation", "Full suite controller did not complete")
    run_id = run.get("run_id")
    require(isinstance(run_id, str) and bool(run_id), "Missing run identity")
    require(all(type(run.get(key)) is int and run[key] == 0 for key in ("pytest_returncode", "collection_returncode")),
            "Collection and pytest must both exit zero")
    require(run.get("source_unchanged") is True and run.get("cleanup_complete") is True, "Incomplete suite cleanup/source proof")
    require(frozen.get("schema_version") == collection.get("schema_version") == 1
            and frozen.get("scope") == "devbrain_frozen_full_suite_source"
            and collection.get("scope") == "devbrain_full_suite_collection"
            and frozen.get("run_id") == collection.get("run_id") == run_id, "Collection/frozen source belongs to another run")
    for key, name in (("runtime_gate_sha256", "gate.json"), ("frozen_manifest_sha256", "frozen-source.json"),
                      ("collection_sha256", "collection.json"), ("junit_sha256", "pytest.xml")):
        require(run.get(key) == digest(paths[name]), "Suite artifact binding differs: " + name)
    require(collection.get("frozen_manifest_sha256") == digest(paths["frozen-source.json"])
            and collection.get("deselected") == [] and collection.get("collection_errors") == []
            and collection.get("full_test_directory") == "tests", "Collection is filtered or incomplete")
    manifest = source_manifest(source_root)
    require(frozen.get("source_hashes") == manifest and run.get("source_hashes") == manifest,
            "Final source/test/package data differs from the frozen run")
    runtime = run.get("runtime", {})
    verify_gate(gate, runtime)
    verify_runtime_files(runtime)
    package = run.get("isolated_package", {})
    require(package.get("origin") == str(Path(source_root).resolve() / "src/devbrain/__init__.py")
            and package.get("sha256") == manifest.get("src/devbrain/__init__.py")
            and package.get("python_executable") == runtime["python_executable"]
            and bool(package.get("python_prefix")), "Isolated child package origin was not qualified")
    require(frozen.get("runtime") == runtime and frozen.get("runtime_gate_sha256") == digest(paths["gate.json"]),
            "Frozen source was not bound to the qualified runtime/gate")
    counts = junit_results(paths["pytest.xml"], collection.get("nodeids"), frozen.get("expected_skips", {}))
    receipt = verify_external(root, run_id, runtime, names, "run-full-suite-080.py")
    native = {key: receipt[key] for key in ("passed", "cleanup_complete", "native_failure", "initial_boot_id", "final_boot_id", "new_wsl_dumps", "new_kernel_fatals", "errors")}
    return {"schema_version": 1, "scope": "devbrain_final_full_suite_attestation", "run_id": run_id,
            "final_full_suite_pass": True, "runtime_gate_sha256": digest(paths["gate.json"]),
            "source_hashes": manifest, "runtime": runtime, "isolated_package": package,
            "counts": counts, "native_fatal_qualification": native,
            "external_receipt_sha256": digest(artifact(root, "receipt.json")),
            "artifact_root": str(Path(root).resolve()), "artifact_sha256": {name: digest(paths[name]) for name in names}}


def verify_soak_gpu_observation(root, report, receipt):
    gpu = report.get("gpu_monitor", {})
    require(all(gpu.get(key) is True for key in ("passed", "stopped", "final_after_cleanup"))
            and gpu.get("errors") == [], "Required GPU monitoring did not complete cleanly")
    samples, gap = gpu.get("samples"), gpu.get("observed_maximum_gap_seconds")
    require(type(samples) is int and samples >= 2
            and gpu.get("interval_seconds") == 5 and gpu.get("query_timeout_seconds") == 3
            and gpu.get("maximum_gap_seconds") == 15
            and type(gap) in (int, float) and math.isfinite(gap) and 0 <= gap <= 15,
            "Required GPU sampling continuity is incomplete")
    devices = gpu.get("devices")
    require(isinstance(devices, list) and any(isinstance(row, dict)
            and "rtx 4080" in str(row.get("name", "")).lower()
            and str(row.get("uuid", "")).startswith("GPU-") for row in devices),
            "Required RTX4080 identity is missing")
    require(gpu.get("samples_artifact") == "gpu-samples.jsonl", "Unexpected GPU sample artifact")
    actual = digest(artifact(root, "gpu-samples.jsonl"))
    require(actual == gpu.get("samples_sha256") == receipt.get("artifact_sha256", {}).get("gpu-samples.jsonl"),
            "GPU sample artifact does not match controller and external receipt")


def attest_soak(root, source_root):
    report = document(artifact(root, "controller-report.json"))
    require(report.get("status") == "workload_passed_pending_external_watch"
            and report.get("workload_criteria_passed") is True and report.get("eight_hour_qualification") is False,
            "Soak must finish locally before external qualification")
    require(report.get("duration_requested", 0) >= 28800 and report.get("workload_seconds", 0) >= 28800
            and report.get("cycle_seconds") == 900 and report.get("cycles", 0) >= math.ceil(report["duration_requested"] / 900)
            and report.get("model_cycles") == report["cycles"]
            and report.get("committed_readback_cycles") == report["cycles"]
            and report.get("terminal_reconnect_cycles") == report["cycles"],
            "An eight-hour active workload with committed readback and terminal reconnect was not completed")
    require(report.get("qualified_source_hashes") == source_manifest(source_root), "Soak source changed after execution")
    runtime = report["qualified_runtime"]
    gate = document(artifact(root, "gate.json"))
    verify_gate(gate, runtime)
    verify_runtime_files(runtime)
    require(report.get("runtime_gate_sha256") == digest(artifact(root, "gate.json")), "Soak gate changed")
    require(report.get("suite_report_sha256") == digest(artifact(root, "final-full-suite.json")), "Soak suite proof changed")
    suite = document(artifact(root, "final-full-suite.json"))
    require(suite == attest_full_suite(suite["artifact_root"], source_root), "Soak depended on invalid full-suite evidence")
    require(report.get("isolated_package") == suite.get("isolated_package"), "Soak isolated package differs from its qualified full suite")
    receipt = verify_external(root, report["run_id"], runtime,
                              ("gate.json", "final-full-suite.json", "controller-report.json", "gpu-samples.jsonl"),
                              "soak-workload-080.py")
    verify_soak_gpu_observation(root, report, receipt)
    return {"schema_version": 1, "scope": "devbrain_soak_attestation", "run_id": report["run_id"],
            "passed": True, "eight_hour_qualification": True, "runtime": runtime,
            "source_hashes": report["qualified_source_hashes"], "external_receipt_sha256": digest(artifact(root, "receipt.json")),
            "artifact_sha256": receipt["artifact_sha256"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("full-suite", "soak"), default="full-suite")
    args = parser.parse_args()
    require(not args.output.exists(), "Attestation output already exists; never overwrite evidence")
    try:
        result = (attest_full_suite if args.mode == "full-suite" else attest_soak)(args.artifact_root, args.source_root)
    except Exception as exc:  # noqa: BLE001 - malformed evidence must produce an explicit non-passing result
        result = {"schema_version": 1, "scope": "devbrain_attestation_rejected", "passed": False,
                  "final_full_suite_pass": False, "eight_hour_qualification": False,
                  "error_type": type(exc).__name__, "reason": str(exc)}
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps({"output": str(args.output), "qualified": result.get("passed", result.get("final_full_suite_pass", False))}))
    return 0 if result.get("passed", result.get("final_full_suite_pass", False)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
