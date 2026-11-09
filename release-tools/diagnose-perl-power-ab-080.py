#!/usr/bin/env python3
"""PREPARED ONLY: at most 3 serial OpenSSL Configure trials, never make/install.

Without --execute this only describes the proposed phase. Power settings are
owned by the separate Windows script; this helper never changes them.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import time
from uuid import uuid4

BASE = Path.home() / ".cache/devbrain-runtime-080"
ARCHIVE = BASE / "gcc31318/downloads/openssl-3.5.9.tar.gz"
SHA = "603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a"
WORKSPACE = Path("/mnt/c/Users/piotr/OneDrive/Dokumenty/ChatGPT/devbrain")
CRASHES = Path("/mnt/c/Users/piotr/AppData/Local/Temp/wsl-crashes")
POWERCFG = "/mnt/c/Windows/System32/powercfg.exe"
GUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"


def active_plan():
    value = subprocess.check_output([POWERCFG, "/getactivescheme"], timeout=3)
    text = value.decode("utf-16-le" if b"\x00" in value[:50] else "utf-8", errors="replace")
    match = re.search(GUID, text)
    if not match:
        raise RuntimeError("Cannot identify Windows active plan")
    return match.group().lower()


def state_for_no_boost(path, expected):
    if not path or not path.is_absolute() or path.name != "state.json":
        raise ValueError("No-boost phase requires an absolute owned Windows state artifact")
    resolved = path.resolve(strict=True)
    if resolved != path or not resolved.is_relative_to(WORKSPACE / "evidence"):
        raise ValueError("State path escapes workspace or traverses a symlink")
    match = re.fullmatch(r"power-ab-080-([0-9a-f]{32})", resolved.parent.name)
    if not match:
        raise ValueError("Unexpected state directory")
    receipt = json.loads((resolved.parent / "ownership.json").read_text())
    state = json.loads(resolved.read_text())
    if (receipt["owner"] != match.group(1) or state["owner"] != receipt["owner"] or
            state["clone"] != expected or receipt["clone"] != expected or state["status"] != "active"):
        raise ValueError("Power diagnostic ownership/status mismatch")
    expires = datetime.fromisoformat(receipt["deadline_utc"].replace("Z", "+00:00"))
    if (expires - datetime.now(timezone.utc)).total_seconds() < 125:
        raise ValueError("Less than 125 seconds remain before watchdog restore; refusing trial")
    return receipt


def stop_owned_scope(unit, case, child):
    """Stop our UUID scope even after its original command has already exited."""
    started = time.monotonic()
    deadline = started + 12
    case["cleanup_verified"] = False

    def control(*arguments):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Owned scope cleanup deadline exhausted")
        return subprocess.run(["systemctl", "--user", *arguments], capture_output=True,
                              text=True, timeout=min(2, remaining))

    try:
        # A dead systemd-run parent is not proof that its scope has no descendants.
        control("stop", "--no-block", unit)
        if child is not None:
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=1)
        # The launcher may not yet have registered its scope at the first stop.
        # Recheck only after that exact launcher has exited, then stop again.
        control("stop", "--no-block", unit)
        killed = False
        while time.monotonic() < deadline:
            state = control("show", unit, "--property=ControlGroup,LoadState")
            values = dict(line.split("=", 1) for line in state.stdout.splitlines() if "=" in line)
            group = values.get("ControlGroup") or case.get("control_group")
            if group:
                if not group.startswith("/user.slice/") or ".." in Path(group).parts:
                    raise RuntimeError("Unexpected owned scope control group")
                case["control_group"] = group
                cgroup = Path("/sys/fs/cgroup") / group.lstrip("/")
                try:
                    events = dict(line.split() for line in (cgroup / "cgroup.events").read_text().splitlines())
                    if events.get("populated") == "0":
                        case["scope_cleanup_proof"] = "cgroup.events populated=0"
                        break
                except FileNotFoundError:
                    if not cgroup.exists():
                        case["scope_cleanup_proof"] = "recorded control group disappeared"
                        break
            elif values.get("LoadState") == "not-found":
                case["scope_cleanup_proof"] = "UUID scope not found after stop"
                break
            if not killed and time.monotonic() - started >= 2:
                control("kill", "--kill-whom=all", "--signal=SIGKILL", unit)
                killed = True
            time.sleep(.1)
        else:
            raise TimeoutError("Cannot confirm owned scope is empty")
    finally:
        if child is not None:
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                child.kill()  # Only the Popen handle we created, never a guessed PID.
                child.wait(timeout=1)
            finally:
                case["returncode"] = child.poll()
        case["cleanup_seconds"] = time.monotonic() - started
        case["cleanup_verified"] = bool(case.get("scope_cleanup_proof")) and (child is None or child.poll() is not None)


def run(args):
    if not re.fullmatch(GUID, args.expected_plan):
        raise ValueError("Expected active plan must be an explicit GUID")
    expected = args.expected_plan.lower()
    receipt = state_for_no_boost(args.state, expected) if args.phase == "no-boost" else None
    if active_plan() != expected:
        raise RuntimeError("Unexpected active power plan before phase")
    if hashlib.sha256(ARCHIVE.read_bytes()).hexdigest() != SHA:
        raise RuntimeError("Official OpenSSL source SHA mismatch")
    target = BASE / ("power-perl-" + args.phase + "-" + uuid4().hex)
    target.mkdir()
    known = set(CRASHES.iterdir())
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    started = time.monotonic()
    workload_deadline = started + 75  # reserve 15s cleanup and a full 30s observation.
    report = {"status": "running", "phase": args.phase, "diagnostic_only": True,
              "expected_power_plan": expected, "source_sha256": SHA, "cases": [],
              "limits": {"trials": 3, "total_seconds": 120, "workload_seconds": 75,
                         "cleanup_reserve_seconds": 15, "post_cleanup_seconds": 30,
                         "memory_max_bytes": 2147483648, "concurrent_configure": 1},
              "boot_id": boot, "state": str(args.state) if args.state else None,
              "power_setting_is_not_actual_clock_measurement": True}
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "LD_LIBRARY_PATH", "MAKEFLAGS", "CFLAGS", "LDFLAGS", "CPPFLAGS"}
                   and not key.startswith("DEVBRAIN_")}
    environment.update(PATH="/usr/bin:/bin", CC="gcc", CFLAGS="-O2 -g -fno-omit-frame-pointer")
    report["controlled_build_environment"] = {key: environment[key] for key in ("PATH", "CC", "CFLAGS")}
    def save():
        (target / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    def observe():
        new = set(CRASHES.iterdir()) - known
        if new:
            report["fatal_artifacts"] = sorted(str(path) for path in new)
            raise RuntimeError("New native fatal; stop phase, no retry")
        if Path("/proc/sys/kernel/random/boot_id").read_text().strip() != boot:
            raise RuntimeError("Kernel boot changed")
        if active_plan() != expected:
            raise RuntimeError("Windows power plan changed; stop phase")
        if receipt:
            state = json.loads(args.state.read_text())
            if state["status"] != "active":
                raise RuntimeError("Watchdog ended the temporary power phase")
    try:
        for index in range(3):
            observe()
            if time.monotonic() >= workload_deadline:
                raise TimeoutError("Configure workload budget exhausted")
            fresh = target / f"fresh-{index}"
            fresh.mkdir()
            with tarfile.open(ARCHIVE) as stream:
                stream.extractall(fresh, filter="data")
            source = fresh / "openssl-3.5.9"
            prefix = fresh / "unused-prefix"
            command = ["/usr/bin/perl", str(source / "Configure"), "linux-x86_64", "shared", "no-tests",
                       "--prefix=" + str(prefix), "--openssldir=" + str(prefix / "ssl"), "--libdir=lib", "-O2", "-g"]
            unit = "devbrain-perl080-" + uuid4().hex + ".scope"
            argv = ["systemd-run", "--user", "--scope", "--quiet", "--unit=" + unit,
                    "--property=MemoryMax=2147483648", "--property=MemorySwapMax=0", "--", *command]
            case = {"index": index, "argv": command, "scope": unit, "log": str(target / f"configure-{index}.log")}
            report["cases"].append(case)
            save()
            child = None
            try:
                with Path(case["log"]).open("w") as log:
                    child = subprocess.Popen(argv, cwd=source, env=environment, stdout=log, stderr=log, start_new_session=True)
                    while child.poll() is None:
                        observe()
                        if "control_group" not in case:
                            group = subprocess.run(["systemctl", "--user", "show", unit, "--property=ControlGroup", "--value"],
                                                   capture_output=True, text=True, timeout=3).stdout.strip()
                            if group.startswith("/user.slice/"):
                                case["control_group"] = group
                        if case.get("control_group"):
                            cgroup = Path("/sys/fs/cgroup") / case["control_group"].lstrip("/")
                            if (cgroup / "memory.max").exists():
                                case["memory_max"] = (cgroup / "memory.max").read_text().strip()
                                case["memory_peak"] = max(case.get("memory_peak", 0), int((cgroup / "memory.peak").read_text()))
                                if case["memory_max"] != "2147483648":
                                    raise RuntimeError("Memory limit readback failed")
                        if time.monotonic() >= workload_deadline:
                            raise TimeoutError("Configure workload budget exhausted")
                        time.sleep(.1)
                case["returncode"] = child.wait()
                observe()
                if child.returncode:
                    raise RuntimeError("Configure failed; phase stops without retry")
                if case.get("memory_max") != "2147483648":
                    raise RuntimeError("Memory limit was not observed; refusing to qualify this trial")
            finally:
                try:
                    stop_owned_scope(unit, case, child)
                finally:
                    save()
    except BaseException as exc:
        report.update(status="stopped_on_failure", error=str(exc))
    finally:
        # Observe after subprocess/scope cleanup. This does not initiate more work.
        observation_started = time.monotonic()
        end = min(observation_started + 30, started + 120)
        while time.monotonic() < end:
            try:
                observe()
            except BaseException as exc:
                report.update(status="stopped_on_failure", error=str(exc))
            time.sleep(.2)
        report["post_cleanup_observation_seconds"] = time.monotonic() - observation_started
        if report["post_cleanup_observation_seconds"] < 30:
            report.update(status="stopped_on_failure", error="Full 30-second post-cleanup observation was not completed")
        if any(not case.get("cleanup_verified") for case in report["cases"]):
            report.update(status="stopped_on_failure", error="Owned scope cleanup could not be verified")
        if report["status"] == "running":
            report["status"] = "pass_diagnostic_only"
        report["elapsed_seconds"] = time.monotonic() - started
        report["completed_configures"] = sum(case.get("returncode") == 0 for case in report["cases"])
        save()
    print(json.dumps({"report": str(target / "report.json"), "status": report["status"],
                      "completed_configures": report["completed_configures"]}))
    return 0 if report["status"] == "pass_diagnostic_only" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--phase", choices=("baseline", "no-boost"), default="baseline")
    parser.add_argument("--expected-plan")
    parser.add_argument("--state", type=Path)
    args = parser.parse_args()
    if args.execute:
        if not args.expected_plan:
            parser.error("--execute requires --expected-plan")
        raise SystemExit(run(args))
    print(json.dumps({"prepared_only": True, "phase": args.phase, "max_configures": 3,
                      "total_seconds": 120, "memory_limit_bytes": 2147483648,
                      "no_make_install_models_db_java_gpu": True, "changes_power_plan": False}))
