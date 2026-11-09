"""Publish the stopped candidate qualification without overwriting historical gates."""
import hashlib
import json
from pathlib import Path
import shutil
import time
import xml.etree.ElementTree as ET

base = Path.home() / ".cache/devbrain-runtime-080"
root = base / "software313-tmux38"
destination = Path("/mnt/c/Users/piotr/OneDrive/Dokumenty/ChatGPT/devbrain/evidence")
read = lambda path: json.loads(path.read_text())
run = read(root / "evidence/java-tests.json")
integrity = read(base / "evidence/python313-fatal-integrity.json")
imports = read(base / "evidence/imports-31318-bounded1.json")
ssl = read(base / "evidence/ssl-standalone31318.json")
tmux = read(base / "evidence/tmux38-build.json")
teardown = read(base / "evidence/tmux38-teardown.json")
unit = ET.parse(base / "evidence/selection-tmux-unit.xml")
cases = list(unit.iter("testcase"))
unit_ok = len(cases) == 51 and not any(list(unit.iter(tag)) for tag in ("failure", "error", "skipped"))
runtime = {"python_executable": str((root / "venv/bin/python").resolve()),
           "java_executable": str((base / "runtimes/jdk-21.0.13+9/bin/java").resolve()),
           "tmux_executable": str((base / "runtimes/tmux-3.8/bin/tmux").resolve()),
           "python_version": "3.13.18", "java_version": "21.0.13+9", "tmux_version": "3.8"}
for name in ("python", "java", "tmux"):
    runtime[name + "_binary_sha256"] = hashlib.sha256(Path(runtime[name + "_executable"]).read_bytes()).hexdigest()
for source in (base / "evidence").iterdir():
    if source.name.startswith(("python313-", "imports-31318-", "imports-31216-", "ssl-standalone31318-",
                               "native-wsl-crash-1794050652", "tmux38-", "selection-tmux-")):
        shutil.copy2(source, destination / ("runtime-080-" + source.name))
for source in (root / "evidence").iterdir():
    if source.suffix in {".log", ".xml", ".json"}:
        shutil.copy2(source, destination / ("runtime-080-software-" + source.name))
checks = {
    "ssl_candidate": {"pass": ssl["status"] == "pass", "cases": len(ssl["cases"])},
    "unit": {"pass": unit_ok, "cases": len(cases)},
    "imports": {"pass": imports["status"] == "pass", "cases": len(imports["cases"])},
    "tmux_teardown": {"pass": teardown["status"] == "pass" and not teardown["cores"],
                      "fresh_servers": 3, "zoomed_destroyed_windows": 63},
    "native_evaluation": {"pass": False, "reason": "Series stopped before evaluation modules"},
    "c2_repetitions": {"pass": False, "expected_fresh_runs": 3, "attempted": len(run["runs"])},
    "native_children": {"pass": False, "fatal_core_sha256": integrity["core_sha256"],
                        "signal": "SIGABRT", "reason": "Executing a cache."},
    "engines_stopped": {"pass": not integrity["owned_live_pids"], "owned_live_pids": integrity["owned_live_pids"]},
}
report = {"schema_version": 1, "scope": "bounded_runtime_qualification", "status": "blocked",
          "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "runtime": runtime,
          "checks": checks, "source_manifest_sha256": hashlib.sha256(json.dumps(run["source_sha256"], sort_keys=True).encode()).hexdigest(),
          "source_manifest": run["source_sha256"], "source_report": "runtime-080-software-java-tests.json",
          "root_cause_established": False, "production_or_soak_authorized": False,
          "limitations": ["tmux fix has source and bounded teardown evidence; it does not resolve Python crashes.",
                          "A new CPython 3.13 child aborted during Textual CSS tokenizer import/regex compilation.",
                          "The first native fatal stopped this series; no retry, later repetitions or evaluation completed.",
                          "The first 25 pytest dots are not a completed JUnit run and are not qualified passes.",
                          "CPython/JDK binaries and selected sources match verified archives; cause remains unresolved.",
                          "User excluded BIOS/firmware changes. No Windows restart or power/CPU changes performed or scheduled."]}
(destination / "runtime-080-software-gate.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps({"status": report["status"], "runtime": runtime, "checks": checks}))
