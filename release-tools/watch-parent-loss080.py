"""Read-only, bounded pidfd oracle for the intentional Windows-parent-loss fixture.

Start with the qualified Python BEFORE the tested Windows parent. This process
keeps WSL in the foreground; it never signals a process or starts an engine.
Its own exit must still be observed by the separate Windows fixture oracle.
"""
from __future__ import annotations

import argparse
import errno
import importlib.util
import json
import os
from pathlib import Path
import select
import stat
import subprocess
import sys
import time


def require(value, message):
    if not value:
        raise RuntimeError(message)


def read_ready_json(path):
    """Read one atomic publication, allowing only a not-yet-published pathname.

    The controller replaces its initial report when its child becomes ready.
    Retain a single no-follow descriptor so pathname replacement between
    metadata checks cannot turn a complete publication into a false failure.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError as exc:
        if exc.errno != errno.ENOENT:
            raise
        return None
    try:
        metadata = os.fstat(descriptor)
        limit = 8 * 1024 * 1024
        require(stat.S_ISREG(metadata.st_mode) and metadata.st_size <= limit,
                "Readiness JSON is nonregular or oversized")
        chunks, size = [], 0
        while True:
            chunk = os.read(descriptor, min(65536, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            require(size <= limit, "Readiness JSON grew beyond its bound")
        result = json.loads(b"".join(chunks).decode("utf-8-sig"))
        require(isinstance(result, dict), "Readiness JSON must be an object")
        return result
    finally:
        os.close(descriptor)


def scope_empty(cgroup, identity, group, properties, returncode):
    """Absence and access failure are different observations, never aliases."""
    state = properties.get("LoadState")
    require(state in {"loaded", "not-found"}
            and properties.get("ControlGroup", "") in {"", group}, "Scope unit evidence changed or unavailable")
    require(returncode == 0 or state == "not-found", "Scope unit probe failed")
    require(state != "loaded" or properties.get("ControlGroup") == group, "Loaded scope lost its group binding")
    if cgroup.exists():
        current = cgroup.stat()
        require((current.st_dev, current.st_ino) == identity, "Scope identity was replaced")
        events = dict(line.split() for line in (cgroup / "cgroup.events").read_text().splitlines())
        require(events.get("populated") in {"0", "1"}, "Missing readable cgroup population")
        return events["populated"] == "0"
    require(state == "not-found" and not properties.get("ControlGroup"),
            "Missing cgroup without confirmed absent UUID unit")
    return True


def verify_negative_result(result, run_id):
    require(result.get("run_id") == run_id and result.get("passed") is False
            and result.get("forced_cleanup") is True and result.get("cleanup_complete") is True
            and result.get("controller_exit_verified") is True
            and type(result.get("child_returncode")) is int and result["child_returncode"] != 0
            and all(type(result.get(key)) is list and not result[key]
                    for key in ("remaining_scope_processes", "remaining_tagged_processes"))
            and result.get("native_failure") is False, "Expected negative supervisor verdict/cleanup missing")
    errors = result.get("errors")
    require(type(errors) is list and all(isinstance(error, str) for error in errors)
            and any("heartbeat" in error.lower() for error in errors), "Lost Windows heartbeat was not observed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--supervisor", type=Path, required=True)
    parser.add_argument("--crash-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    require(args.execute, "Explicit execution required")
    root = args.artifact_root
    require(root.is_absolute() and root.resolve() == root and str(root).startswith("/mnt/c/")
            and not (root / "parent-loss-linux.json").exists(), "Fresh mapped oracle root required")
    require(args.supervisor.is_absolute() and args.supervisor.resolve() == args.supervisor, "Canonical supervisor required")
    spec = importlib.util.spec_from_file_location("parent_loss_supervision", args.supervisor)
    supervision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(supervision)
    runtime = supervision.qualified_runtime(supervision.read_json(root / "gate.json"))
    initial_hashes = {str(path): supervision.digest(path) for path in (Path(__file__).resolve(), args.supervisor, root / "gate.json")}
    observation = supervision.Observation(args.crash_dir)
    started = time.monotonic()
    deadline = started + 180
    handles, identities = [], []
    report = {"scope": "devbrain_parent_loss_pidfd_oracle", "schema_version": 1, "run_id": args.run_id,
              "passed": False, "application_qualification": False, "errors": [], "runtime": runtime,
              "initial_boot_id": observation.initial_boot, "hashes": initial_hashes,
              "child_nondumpable_tested": False}
    supervision.atomic_json(root / "oracle-linux-ready.json", {"run_id": args.run_id, "identity": supervision.identity(os.getpid()),
                            "initial_boot_id": observation.initial_boot, "initial_kernel_fatals": sorted(observation.kernel),
                            "foreground_before_parent": True})
    try:
        # A live env-cleared orphan and its controller/supervisor parent chain
        # establish exactly which three process lifetimes this oracle observes.
        while True:
            require(time.monotonic() < deadline, "Fixture readiness deadline exceeded")
            require(not observation.poll(), "Native failure or boot change before parent loss")
            controller = read_ready_json(root / "controller-report.json")
            if controller is not None and controller.get("environment_cleared_child"):
                break
            time.sleep(.2)
        require(controller.get("run_id") == args.run_id and controller.get("mode") == "parent-loss"
                and controller.get("scope") == "devbrain_supervision_test_fixture", "Wrong fixture controller")
        proof = supervision.read_json(root / "mapping-proof.json")
        proof_hash = supervision.digest(root / "mapping-proof.json")
        unit = proof.get("scope_unit")
        require(proof.get("run_id") == args.run_id and supervision.SCOPE_PATTERN.fullmatch(unit or ""), "Invalid UUID scope proof")
        orphan = controller["environment_cleared_child"]
        direct = supervision.identity(orphan["parent_pid"])
        parent = supervision.identity(direct["parent_pid"]) if direct else None
        identities = [orphan, direct, parent]
        require(all(identities) and len({row["pid"] for row in identities}) == 3, "Three distinct live fixture processes required")
        group = supervision.process_cgroup(orphan["pid"])
        require(group and group.startswith("/user.slice/") and group.endswith("/" + unit)
                and supervision.process_cgroup(os.getpid()) != group, "Oracle must observe the private scope from outside")
        cgroup = Path("/sys/fs/cgroup") / group.lstrip("/")
        birth = cgroup.stat()
        cgroup_identity = (birth.st_dev, birth.st_ino)
        require(cgroup.resolve() == cgroup, "Noncanonical cgroup")
        for row in identities:
            require(row["uid"] == os.getuid() and row["executable"] == runtime["python_executable"]
                    and supervision.same_process(row) and supervision.process_cgroup(row["pid"]) == group,
                    "Fixture process identity or membership changed")
            descriptor = os.pidfd_open(row["pid"])
            handles.append(descriptor)
            require(supervision.same_process(row) and supervision.process_cgroup(row["pid"]) == group,
                    "PID changed while opening pidfd")
        require((Path("/proc") / str(orphan["pid"]) / "environ").read_bytes() == b"", "Orphan did not clear its environment")
        report.update(identities=identities, scope_unit=unit, control_group=group,
                      scope_cgroup_identity=f"{birth.st_dev}:{birth.st_ino}", environment_cleared=True)
        supervision.atomic_json(root / "parent-loss-ready.json", {**report, "pidfds_open": True})
        empty_since = None
        while True:
            now = time.monotonic()
            require(now < deadline, "Parent-loss cleanup deadline exceeded")
            require(not observation.poll(), "Native failure or boot change during parent-loss observation")
            require(supervision.digest(root / "mapping-proof.json") == proof_hash, "Scope mapping changed")
            exited = len(select.select(handles, [], [], 0)[0]) == len(handles)
            unit_probe = subprocess.run(["/usr/bin/systemctl", "--user", "show", unit,
                                         "--property=LoadState", "--property=ControlGroup"],
                                        capture_output=True, text=True, timeout=5, check=False)
            properties = dict(line.split("=", 1) for line in unit_probe.stdout.splitlines() if "=" in line)
            # Unit disappearance never substitutes for the retained pidfds.
            empty = scope_empty(cgroup, cgroup_identity, group, properties, unit_probe.returncode)
            if exited and empty:
                empty_since = empty_since or now
                if now - empty_since >= 30:
                    break
            else:
                require(empty_since is None, "A cleaned scope became populated again")
            time.sleep(1)
        report.update(all_retained_pidfds_exited=True, scope_empty_verified=True,
                      post_cleanup_seconds=time.monotonic() - empty_since)
        result = supervision.read_json(root / "supervisor-report.json")
        verify_negative_result(result, args.run_id)
        trigger = supervision.read_json(root / "parent-loss-trigger.json")
        require(trigger.get("run_id") == args.run_id and trigger.get("scope_unit") == unit
                and trigger.get("windows_parent_exit_verified") is True and trigger.get("retained_handle") is True,
                "External parent loss was not recorded")
        report.update(passed=True, all_retained_pidfds_exited=True, scope_empty_verified=True,
                      post_cleanup_seconds=time.monotonic() - empty_since,
                      supervisor_report_sha256=supervision.digest(root / "supervisor-report.json"))
    except Exception as exc:
        report["errors"].append(type(exc).__name__ + ": " + str(exc))
        report["passed"] = False
    finally:
        for descriptor in handles:
            try:
                os.close(descriptor)
            except OSError as exc:
                report["errors"].append("Pidfd close: " + str(exc))
                report["passed"] = False
        try:
            require(not observation.poll(), "Native failure during oracle cleanup")
            require(supervision.qualified_runtime(supervision.read_json(root / "gate.json")) == runtime,
                    "Oracle runtime changed")
            require(all(supervision.digest(Path(path)) == expected for path, expected in initial_hashes.items()), "Oracle inputs changed")
        except Exception as exc:
            report["errors"].append(type(exc).__name__ + ": " + str(exc))
            report["passed"] = False
        report.update(final_boot_id=observation.final_boot, new_wsl_dumps=sorted(observation.new_files),
                      new_kernel_fatals=sorted(observation.new_kernel), elapsed_seconds=time.monotonic() - started,
                      observer_own_exit_verified=False)
        supervision.atomic_json(root / "parent-loss-linux.json", report)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
