"""Pure evidence-contract regressions. Never launch pytest children or runtimes."""
from __future__ import annotations

import importlib.util
import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


def load():
    spec = importlib.util.spec_from_file_location("attestor080_tests", Path(__file__).with_name("attest-full-suite-080.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def proof(tmp_path):
    module = load()
    source, root = tmp_path / "source", tmp_path / "evidence"
    (source / "src/devbrain").mkdir(parents=True)
    (source / "tests").mkdir()
    (source / "data").mkdir()
    root.mkdir()
    for name in ("pyproject.toml", "uv.lock", "src/devbrain/__init__.py", "src/devbrain/theme.tcss", "tests/test_one.py", "data/sample.json"):
        (source / name).write_text("fixture")
    runtime = {}
    for name in module.RUNTIME_NAMES:
        binary = tmp_path / (name + "-fixture")
        binary.write_bytes(b"Not an executable; hash-only synthetic fixture")
        runtime[name + "_executable"], runtime[name + "_binary_sha256"] = str(binary.resolve()), module.digest(binary)
    gate = {"schema_version": 1, "scope": "bounded_runtime_qualification", "status": "pass", "runtime": runtime,
            "checks": {name: {"pass": True} for name in module.GATE_CHECKS}}
    write(root / "gate.json", gate)
    manifest = module.source_manifest(source)
    frozen = {"schema_version": 1, "scope": "devbrain_frozen_full_suite_source", "run_id": "fixture-run",
              "runtime": runtime, "runtime_gate_sha256": module.digest(root / "gate.json"), "source_hashes": manifest, "expected_skips": {}}
    write(root / "frozen-source.json", frozen)
    nodes = ["tests/test_one.py::test_one", "tests/test_one.py::test_two[Unicode-ą]"]
    write(root / "collection.json", {"schema_version": 1, "scope": "devbrain_full_suite_collection", "run_id": "fixture-run",
          "nodeids": nodes, "full_test_directory": "tests", "deselected": [], "collection_errors": [],
          "frozen_manifest_sha256": module.digest(root / "frozen-source.json")})
    xml = ET.Element("testsuites")
    suite = ET.SubElement(xml, "testsuite", tests="2", failures="0", errors="0", skipped="0")
    for node in nodes:
        case = ET.SubElement(suite, "testcase", name=node.rsplit("::", 1)[-1])
        ET.SubElement(ET.SubElement(case, "properties"), "property", name="nodeid", value=node)
    ET.ElementTree(xml).write(root / "pytest.xml", encoding="utf-8")
    run = {"schema_version": 1, "scope": "devbrain_final_full_suite_run", "run_id": "fixture-run",
           "python_launcher": runtime["python_executable"],
           "status": "full_suite_completed_pending_attestation", "pytest_returncode": 0, "collection_returncode": 0,
           "source_unchanged": True, "cleanup_complete": True, "source_hashes": manifest, "runtime": runtime,
           "isolated_package": {"origin": str(source / "src/devbrain/__init__.py"), "sha256": manifest["src/devbrain/__init__.py"],
                                "python_executable": runtime["python_executable"], "python_prefix": "/fixture/venv"}}
    write(root / "controller-report.json", run)
    helpers = {name: module.digest(Path(__file__).with_name(name)) for name in (
        "run-full-suite-080.py", "attest-full-suite-080.py", "validate-stability-080.py", "supervise-080.py", "run-supervised-080.ps1", "qualification-power-080.ps1")}
    supervisor = {"schema_version": 1, "scope": "devbrain_linux_process_supervision", "run_id": "fixture-run",
                  "passed": True, "native_failure": False, "initial_boot_id": "boot", "final_boot_id": "boot",
                  "new_wsl_dumps": [], "new_kernel_fatals": [],
                  "python_launcher": runtime["python_executable"], "python_launcher_base_sha256": runtime["python_binary_sha256"],
                  "child_returncode": 0, "controller_exit_verified": True, "cleanup_complete": True,
                  "forced_cleanup": False, "errors": [], "runtime": runtime, "controller_report": "controller-report.json",
                  "helper_sha256_start": helpers, "helper_sha256_end": helpers}
    scope = {"scope_unit": "devbrain-q-" + "a" * 32 + ".scope",
             "control_group": "/user.slice/user-1000.slice/devbrain-q-" + "a" * 32 + ".scope",
             "scope_cgroup_identity": "29:1234", "scope_membership_verified": True}
    supervisor.update(scope, scope_descendants_stopped=True, remaining_scope_processes=[], remaining_tagged_processes=[])
    write(root / "supervisor-report.json", supervisor)
    receipt = {"schema_version": 1, "scope": "devbrain_external_process_watch", "run_id": "fixture-run", "passed": True,
               "runtime": runtime, "supervisor_exit_verified": True,
               "python_launcher": runtime["python_executable"], "python_launcher_base_sha256": runtime["python_binary_sha256"],
               "child_returncode": 0, "supervisor_returncode": 0, "controller_exit_verified": True, "cleanup_complete": True,
               "cleanup_grace_seconds": 30, "initial_boot_id": "boot", "final_boot_id": "boot",
               "forced_cleanup": False, "native_failure": False, "new_wsl_dumps": [], "new_kernel_fatals": [], "errors": [],
               "helper_sha256_start": helpers, "helper_sha256_end": helpers}
    receipt.update(scope, scope_initially_absent=True, scope_empty_verified=True, scope_cleanup_grace_seconds=30)
    canonical = "scheme=" + module.BALANCED_GUID + "\nfixture/setting=00000001,00000001\n"
    power = {"active_guid": module.BALANCED_GUID, "canonical_format": "powercfg-qh-acdc-v1", "setting_count": 1,
             "canonical_settings": canonical, "settings_sha256": hashlib.sha256(canonical.encode()).hexdigest()}
    gate["power_profile"] = power
    write(root / "gate.json", gate)
    frozen["runtime_gate_sha256"] = module.digest(root / "gate.json")
    write(root / "frozen-source.json", frozen)
    receipt.update(power_profile_start=power, power_profile_end=power,
                   sleep_inhibition={"acquired": True, "restored": True, "requested_flags": 0x80000001},
                   power_monitor={"interval_seconds": 30, "maximum_gap_seconds": 45, "changed": False,
                                  "sample_count": 3, "samples_artifact": "power-samples.jsonl", "supervisor_exit_elapsed_seconds": 10,
                                  "scope_empty_elapsed_seconds": 10})
    (root / "power-samples.jsonl").write_text("".join(json.dumps({"elapsed_seconds": elapsed, "state": power}) + "\n"
                                                    for elapsed in (0, 30, 40)), encoding="utf-8")
    write(root / "receipt.json", receipt)

    def bind():
        collection = module.document(root / "collection.json")
        collection["frozen_manifest_sha256"] = module.digest(root / "frozen-source.json")
        write(root / "collection.json", collection)
        run = module.document(root / "controller-report.json")
        for key, name in (("runtime_gate_sha256", "gate.json"), ("frozen_manifest_sha256", "frozen-source.json"),
                          ("collection_sha256", "collection.json"), ("junit_sha256", "pytest.xml")):
            run[key] = module.digest(root / name)
        write(root / "controller-report.json", run)
        supervisor = module.document(root / "supervisor-report.json")
        supervisor["controller_report_sha256"] = module.digest(root / "controller-report.json")
        write(root / "supervisor-report.json", supervisor)
        receipt = module.document(root / "receipt.json")
        receipt["artifact_sha256"] = {path.name: module.digest(path) for path in root.iterdir() if path.name != "receipt.json"}
        write(root / "receipt.json", receipt)

    bind()
    return module, root, source, bind


def test_complete_frozen_suite_with_external_exit_evidence_qualifies(proof):
    module, root, source, _ = proof
    result = module.attest_full_suite(root, source)
    assert result["final_full_suite_pass"] and result["counts"]["passed"] == 2
    assert "src/devbrain/theme.tcss" in result["source_hashes"] and "data/sample.json" in result["source_hashes"]


def test_missing_external_receipt_never_qualifies(proof):
    module, root, source, _ = proof
    (root / "receipt.json").unlink()
    with pytest.raises(ValueError, match="Missing"):
        module.attest_full_suite(root, source)


@pytest.mark.parametrize("controller,dependency", [
    ("validate-copilot-account080.py", "validate-native-account080.py"),
    ("validate-copilot-account080.py", "validate-copilot-account-e2e.py"),
    ("replay-failed-queue-080.py", "run-full-suite-080.py"),
    ("run-interactive-ux080.py", "validate-native-account080.py"),
    ("run-interactive-ux080.py", "open-interactive-ux080.ps1"),
    ("run-regressions-080.py", "validate-local-recall.py"),
])
@pytest.mark.parametrize("problem", ["missing", "changed"])
def test_transitive_controller_dependency_must_be_bound_after_cleanup(tmp_path, monkeypatch, controller, dependency, problem):
    module = load()
    monkeypatch.setattr(module, "__file__", str(tmp_path / "attest-full-suite-080.py"))
    names = {controller, dependency, "validate-native-account080.py", "validate-copilot-account-e2e.py", "open-interactive-ux080.ps1", "validate-local-recall.py",
             "attest-full-suite-080.py", "validate-stability-080.py", "supervise-080.py", "qualification-power-080.ps1"}
    for name in names:
        (tmp_path / name).write_text("synthetic helper " + name)
    hashes = {name: module.digest(tmp_path / name) for name in names}
    report = {"helper_sha256_start": dict(hashes), "helper_sha256_end": dict(hashes)}
    module.verify_helper_files(report, controller, windows=False)
    if problem == "missing":
        report["helper_sha256_start"].pop(dependency)
        report["helper_sha256_end"].pop(dependency)
    else:
        (tmp_path / dependency).write_text("changed after observed cleanup")
    with pytest.raises(ValueError, match="helper"):
        module.verify_helper_files(report, controller, windows=False)


@pytest.mark.parametrize("problem", ["not_empty", "scope_reused", "short_grace", "wrong_inode", "remaining_unmarked_child", "power_ends_before_cleanup"])
def test_scope_descendants_and_post_cleanup_observation_are_required(proof, problem):
    module, root, source, bind = proof
    receipt = module.document(root / "receipt.json")
    if problem == "not_empty":
        receipt["scope_empty_verified"] = False
    elif problem == "scope_reused":
        receipt["scope_initially_absent"] = False
    elif problem == "short_grace":
        receipt["scope_cleanup_grace_seconds"] = 29
    elif problem == "wrong_inode":
        receipt["scope_cgroup_identity"] = "29:9999"
    elif problem == "power_ends_before_cleanup":
        receipt["power_monitor"]["scope_empty_elapsed_seconds"] = 11
    else:
        supervisor = module.document(root / "supervisor-report.json")
        supervisor["remaining_scope_processes"] = [{"pid": 123, "environment_cleared": True}]
        write(root / "supervisor-report.json", supervisor)
    write(root / "receipt.json", receipt)
    bind()
    with pytest.raises(ValueError):
        module.attest_full_suite(root, source)


@pytest.mark.parametrize("problem", ["missing", "changed", "settings", "sleep", "sample_changed", "gap", "short_grace"])
def test_power_contract_cannot_be_covered_by_otherwise_passing_receipt(proof, problem):
    module, root, source, bind = proof
    receipt = module.document(root / "receipt.json")
    if problem == "missing":
        receipt.pop("power_profile_start")
    elif problem == "changed":
        receipt["power_monitor"]["changed"] = True
    elif problem == "settings":
        receipt["power_profile_end"]["settings_sha256"] = "0" * 64
    elif problem == "sleep":
        receipt["sleep_inhibition"]["restored"] = False
    else:
        samples = [json.loads(line) for line in (root / "power-samples.jsonl").read_text().splitlines()]
        if problem == "sample_changed":
            samples[1]["state"]["settings_sha256"] = "0" * 64
        elif problem == "gap":
            samples[1]["elapsed_seconds"], samples[2]["elapsed_seconds"] = 60, 70
        else:
            samples[2]["elapsed_seconds"] = 39
        (root / "power-samples.jsonl").write_text("".join(json.dumps(sample) + "\n" for sample in samples))
    write(root / "receipt.json", receipt)
    bind()
    with pytest.raises(ValueError):
        module.attest_full_suite(root, source)


def test_only_exact_skip_reason_frozen_before_run_is_permitted(proof):
    module, root, source, bind = proof
    tree = ET.parse(root / "pytest.xml")
    suite = tree.getroot().find("testsuite")
    case = suite.find("testcase")
    node = case.find("./properties/property").get("value")
    ET.SubElement(case, "skipped", message="approved unavailable platform")
    suite.set("skipped", "1")
    tree.write(root / "pytest.xml", encoding="utf-8")
    frozen = module.document(root / "frozen-source.json")
    frozen["expected_skips"] = {node: "approved unavailable platform"}
    write(root / "frozen-source.json", frozen)
    bind()
    counts = module.attest_full_suite(root, source)["counts"]
    assert counts["passed"] == counts["skipped"] == 1 and counts["unexpected_skips"] == 0


@pytest.mark.parametrize("problem", ["failure", "error", "skip", "duplicate", "missing", "counter", "missing_nodeid"])
def test_junit_problems_cannot_be_covered_by_zero_exit_or_pass_receipt(proof, problem):
    module, root, source, bind = proof
    tree = ET.parse(root / "pytest.xml")
    suite = tree.getroot().find("testsuite")
    case = suite.find("testcase")
    if problem in {"failure", "error", "skip"}:
        tag = "skipped" if problem == "skip" else problem
        ET.SubElement(case, tag, message="unexpected outcome")
        suite.set({"failure": "failures", "error": "errors", "skip": "skipped"}[problem], "1")
    elif problem == "duplicate":
        suite.findall("testcase")[1].find("./properties/property").set("value", case.find("./properties/property").get("value"))
    elif problem == "missing":
        suite.remove(case)
        suite.set("tests", "1")
    elif problem == "counter":
        suite.set("errors", "1")
    else:
        case.remove(case.find("properties"))
    tree.write(root / "pytest.xml", encoding="utf-8")
    bind()
    with pytest.raises(ValueError):
        module.attest_full_suite(root, source)


@pytest.mark.parametrize("problem", ["exit", "supervisor_exit", "grace", "core", "cleanup", "boot", "hash", "helper", "supervisor_runtime"])
def test_external_lifecycle_receipt_is_required_not_just_passed_boolean(proof, problem):
    module, root, source, bind = proof
    receipt = module.document(root / "receipt.json")
    if problem == "exit":
        receipt["child_returncode"] = -6
    elif problem == "supervisor_exit":
        receipt["supervisor_returncode"] = 134
    elif problem == "grace":
        receipt["cleanup_grace_seconds"] = 2
    elif problem == "core":
        receipt["new_wsl_dumps"] = ["controller-finalization.dmp"]
    elif problem == "cleanup":
        receipt["cleanup_complete"] = False
    elif problem == "boot":
        receipt["final_boot_id"] = "another-boot"
    elif problem == "hash":
        receipt["artifact_sha256"]["pytest.xml"] = "0" * 64
    elif problem == "helper":
        receipt["helper_sha256_end"]["run-full-suite-080.py"] = "0" * 64
    else:
        supervisor = module.document(root / "supervisor-report.json")
        supervisor["runtime"]["python_binary_sha256"] = "0" * 64
        write(root / "supervisor-report.json", supervisor)
        bind()
        receipt = module.document(root / "receipt.json")
    write(root / "receipt.json", receipt)
    with pytest.raises(ValueError):
        module.attest_full_suite(root, source)


@pytest.mark.parametrize("problem", ["source", "test", "data", "runtime", "collection_filter", "collection_duplicate", "stale_origin"])
def test_frozen_source_runtime_and_full_collection_must_remain_exact(proof, problem):
    module, root, source, bind = proof
    if problem in {"source", "test", "data"}:
        name = {"source": "src/devbrain/theme.tcss", "test": "tests/test_one.py", "data": "data/sample.json"}[problem]
        (source / name).write_text("modified after the run")
    elif problem == "runtime":
        Path(module.document(root / "controller-report.json")["runtime"]["python_executable"]).write_bytes(b"changed")
    elif problem == "stale_origin":
        run = module.document(root / "controller-report.json")
        run["isolated_package"]["origin"] = "/old/install/devbrain/__init__.py"
        write(root / "controller-report.json", run)
        bind()
    else:
        collection = module.document(root / "collection.json")
        if problem == "collection_filter":
            collection["deselected"] = ["tests/test_hidden.py::test_hidden"]
        else:
            collection["nodeids"].append(collection["nodeids"][0])
        write(root / "collection.json", collection)
        bind()
    with pytest.raises(ValueError):
        module.attest_full_suite(root, source)
