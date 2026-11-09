"""Opt-in controller for bounded supervisor tests; never an application qualification.

Run only through run-supervised-080.ps1 with a passed runtime gate. No database,
tmux, Java or model is started. `orphan` deliberately makes an incorrect local
cleanup claim; the independent cgroup observer must reject it and stop its child.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--mode", choices=("normal", "nonzero", "timeout", "orphan", "parent-loss"), required=True)
    args = parser.parse_args()
    if not args.execute:
        parser.error("Use --execute only after runtime qualification, through the external wrapper")
    root = args.artifact_root.resolve(strict=True)
    if not root.is_relative_to(Path("/mnt/c")) or os.environ.get("DEVBRAIN_QUALIFICATION_RUN_ID") != args.run_id:
        parser.error("A fresh mapped fixture directory and qualification parent are required")
    spec = importlib.util.spec_from_file_location("fixture_supervision", Path(__file__).with_name("supervise-080.py"))
    supervision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(supervision)
    runtime = supervision.qualified_runtime(supervision.read_json(root / "gate.json"))
    proof = supervision.read_json(root / "mapping-proof.json")
    if proof.get("run_id") != args.run_id:
        raise ValueError("Scope proof belongs to a different fixture")
    scope = supervision.ScopeOwnership(proof.get("scope_unit"))
    report = {"schema_version": 1, "scope": "devbrain_supervision_test_fixture", "run_id": args.run_id,
              "runtime": runtime, "python_launcher": sys.executable, "mode": args.mode,
              "status": "fixture_running", "cleanup_complete": False, "application_qualification": False}
    supervision.atomic_json(root / "controller-report.json", report)
    if args.mode in {"orphan", "parent-loss"}:
        ready = root / "fixture-child.json"
        if ready.exists():
            raise ValueError("Fixture child evidence already exists")
        code = ("import json,os,sys,time;from pathlib import Path;os.environ.clear();"
                "p=Path(sys.argv[1]);t=p.with_suffix('.pending');"
                "t.write_text(json.dumps({'pid':os.getpid(),'environment_empty':not os.environ}));t.replace(p);time.sleep(180)")
        child = subprocess.Popen([runtime["python_executable"], "-I", "-B", "-c", code, str(ready)],
                                 env={}, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        deadline = time.monotonic() + 5
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        observed = json.loads(ready.read_text())
        if (observed.get("pid") != child.pid or observed.get("environment_empty") is not True
                or supervision.process_cgroup(child.pid) != scope.group):
            raise RuntimeError("Environment-cleared child was not established in this exact scope")
        report["environment_cleared_child"] = supervision.identity(child.pid)
    if args.mode in {"timeout", "parent-loss"}:
        supervision.atomic_json(root / "controller-report.json", report)
        time.sleep(180)  # outer timeout / lost-parent lease must stop this fixture
        raise RuntimeError("Expected outer cancellation did not stop the fixture within its bound")
    report.update(status="fixture_completed_pending_external_watch", cleanup_complete=True,
                  intentionally_false_cleanup_claim=args.mode == "orphan")
    supervision.atomic_json(root / "controller-report.json", report)
    return 7 if args.mode == "nonzero" else 0


if __name__ == "__main__":
    raise SystemExit(main())
