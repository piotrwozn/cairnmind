"""Passive, bounded soak monitor. Never starts a JVM, GPU, model, or database.

An eight-hour observation is the default, but must be started explicitly.
The supplied preview PID/tree, fatal kernel messages and WSL crash-file metadata
are inspected. This passive observation is never an active workload qualification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import signal
import subprocess
import time
from pathlib import Path

FATAL = re.compile(r"segfault|segmentation fault|fatal signal|SIGSEGV|SIGABRT|invalid opcode|general protection fault|oom-kill|Out of memory|Kernel panic", re.I)


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def kernel_fatals():
    result = subprocess.run(["dmesg", "--raw"], capture_output=True, text=True, timeout=5, check=False)
    if result.returncode:
        raise RuntimeError("Kernel fatal-signal observation is unavailable")
    return {hashlib.sha256(line.encode()).hexdigest(): line for line in result.stdout.splitlines() if FATAL.search(line)}


def crash_files(directory):
    if not directory.is_dir():
        raise RuntimeError("WSL crash directory is unavailable")
    # Read metadata only. Dumps can contain private memory and are never copied.
    return {path.name: (path.stat().st_size, path.stat().st_mtime_ns)
            for path in directory.iterdir() if path.is_file() and path.name.startswith("wsl-crash-")}


class CrashWatch:
    """Observe all new WSL native failures, including already exited children."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.initial_boot_id = boot_id()
        self.initial_files = crash_files(self.directory)
        self.initial_kernel = kernel_fatals()
        self.failures = {}

    def poll(self):
        current_boot = boot_id()
        files, kernel = crash_files(self.directory), kernel_fatals()
        new_files = {name: value for name, value in files.items() if self.initial_files.get(name) != value}
        new_kernel = {key: value for key, value in kernel.items() if key not in self.initial_kernel}
        if current_boot != self.initial_boot_id:
            self.failures["boot_changed"] = True
        if new_files:
            self.failures["new_wsl_dumps"] = sorted(set(self.failures.get("new_wsl_dumps", [])) | set(new_files))
        if new_kernel:
            self.failures["new_kernel_fatals"] = sorted(set(self.failures.get("new_kernel_fatals", [])) | set(new_kernel.values()))
        return {"initial_boot_id": self.initial_boot_id, "boot_id": current_boot,
                "native_failure": bool(self.failures), **self.failures}

    def finish(self, seconds=30, *, sleep=time.sleep):
        """Continue after cleanup: Windows may finish writing a dump later."""
        if seconds < 30:
            raise ValueError("At least 30 seconds of post-cleanup observation is required")
        result = self.poll()
        for _ in range(int(seconds)):
            sleep(1)
            result = self.poll()
        return result


def same_process(expected):
    current = process(expected["pid"])
    return bool(current and current["state"] not in {"Z", "X"}
                and (current["birth"], current["executable"]) == (expected["birth"], expected["executable"]))


def owned_jvm(preview_dir, expected_java):
    state = preview_dir.resolve() / "arcade"
    record = json.loads((state / "process.json").read_text())
    value = process(int(record["pid"]))
    if not value or Path(record.get("root", "")).resolve() != state:
        raise RuntimeError("No JVM identity owned by this data directory")
    command = (Path("/proc") / str(value["pid"]) / "cmdline").read_bytes().split(b"\0")
    if (f"-Darcadedb.server.rootPath={state}".encode() not in command
            or b"com.arcadedb.server.ArcadeDBServer" not in command
            or Path(value["executable"]).resolve() != Path(expected_java).resolve()):
        raise RuntimeError("Managed JVM identity or data scope does not match")
    return value


def process(pid):
    path = Path("/proc") / str(pid)
    try:
        raw = (path / "stat").read_text().rpartition(")")[2].split()
        status = dict(line.split(":", 1) for line in (path / "status").read_text().splitlines() if ":" in line)
        return {"pid": pid, "birth": raw[19], "ppid": int(raw[1]), "state": raw[0],
                "executable": str((path / "exe").resolve(strict=True)),
                "cpu_ticks": int(raw[11]) + int(raw[12]), "rss_kb": status.get("VmRSS", "").strip(),
                "threads": status.get("Threads", "").strip()}
    except (OSError, ValueError, IndexError):
        return None


def snapshot(pid, birth, preview_dir=None, *, tmux_binary="tmux"):
    root = process(pid)
    alive = root is not None and root["birth"] == birth and root["state"] not in {"Z", "X"}
    rows = [row for path in Path("/proc").iterdir() if path.name.isdigit() and (row := process(int(path.name)))]
    owned = {pid} if alive else set()
    for _ in range(20):
        added = {row["pid"] for row in rows if row["ppid"] in owned}
        if added <= owned:
            break
        owned |= added
    meminfo = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    value = {"time": time.time(), "boot_id": boot_id(),
            "root_alive": alive, "processes": [row for row in rows if row["pid"] in owned],
            "memory_available": meminfo.get("MemAvailable", "").strip(),
            "loadavg": Path("/proc/loadavg").read_text().strip(),
            "memory_pressure": Path("/proc/pressure/memory").read_text().strip()}
    if preview_dir:
        info = json.loads((preview_dir / "preview-status.json").read_text())
        result = subprocess.run([tmux_binary, "-S", info["socket"], "list-panes", "-a", "-F",
                                 "#{@devbrain_role}\t#{pane_pid}\t#{pane_dead}\t#{pane_width}\t#{pane_height}"],
                                capture_output=True, text=True, timeout=3, check=False)
        value["panes"] = [line.split("\t") for line in result.stdout.splitlines()]
        value["tmux_status"] = result.returncode
        pane_ids = {int(row[1]) for row in value["panes"] if len(row) == 5 and row[1].isdigit()}
        value["detached_pane_processes"] = [row for pid in pane_ids - owned if (row := process(pid))]
        value["fatal_reports"] = sorted(str(path.relative_to(preview_dir)) for path in preview_dir.rglob("hs_err_pid*.log"))
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=8 * 3600)
    parser.add_argument("--interval", type=float, default=30)
    parser.add_argument("--preview-dir", type=Path)
    parser.add_argument("--tmux-bin", default="tmux")
    parser.add_argument("--wsl-crash-dir", type=Path, required=True)
    parser.add_argument("--cleanup-grace", type=int, default=30)
    args = parser.parse_args()
    if args.duration <= 0 or not 1 <= args.interval <= 60 or args.cleanup_grace < 30:
        parser.error("duration must be positive; interval must be 1..60 seconds")
    root = process(args.pid)
    if root is None:
        parser.error("supplied PID does not exist")
    if args.output.exists():
        parser.error("output already exists; provide a new evidence filename")
    if args.preview_dir:
        marker = json.loads((args.preview_dir / "terminal-preview.json").read_text())
        if marker.get("kind") != "devbrain-synthetic-terminal-preview":
            parser.error("preview-dir does not identify a synthetic preview")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    watch = CrashWatch(args.wsl_crash_dir)
    stopped = False
    def stop(*_):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    report = {"pid": args.pid, "birth": root["birth"], "requested_seconds": args.duration,
              "monitor_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "started": time.time(),
              "passive_only": True, "status": "running", "samples": 0}
    report["initial_boot_id"] = watch.initial_boot_id
    summary = args.output.with_suffix(".summary.json")
    summary.write_text(json.dumps(report, indent=2))
    deadline = time.monotonic() + args.duration
    try:
        with args.output.open("x") as stream:
            while not stopped:
                sample = snapshot(args.pid, root["birth"], args.preview_dir, tmux_binary=args.tmux_bin)
                sample["native_failures"] = watch.poll()
                stream.write(json.dumps(sample) + "\n")
                stream.flush()
                report["samples"] += 1
                if sample["native_failures"]["native_failure"]:
                    report["status"] = "native_failure"
                    break
                if not sample["root_alive"]:
                    report["status"] = "observed_process_exit"
                    break
                if time.monotonic() >= deadline:
                    report["status"] = "completed_observation"
                    break
                for _ in range(max(1, int(args.interval * 10))):
                    if stopped:
                        break
                    time.sleep(.1)
        if stopped:
            report["status"] = "interrupted"
    except Exception as exc:  # noqa: BLE001 - observation failure is explicit evidence, never success
        report.update(status="observation_error", error_type=type(exc).__name__)
    finally:
        try:
            report["post_observation"] = watch.finish(args.cleanup_grace)
            report["target_stopped_at_end"] = not same_process(root)
            report["final_boot_id"] = boot_id()
            if report["post_observation"]["native_failure"]:
                report["status"] = "native_failure"
        except Exception as exc:  # noqa: BLE001 - missing final observation cannot be a pass
            report.update(status="observation_error", final_error_type=type(exc).__name__)
        report["active_workload_qualified"] = False
        report["ended"] = time.time()
        summary.write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
    return 1 if report["status"] in {"native_failure", "observation_error"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
