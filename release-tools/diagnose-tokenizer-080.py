#!/usr/bin/env python3
"""Single bounded Textual import comparison; no application, DB, JVM, tmux or models."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import time

BASE = Path.home() / ".cache/devbrain-runtime-080"
CRASHES = Path("/mnt/c/Users/piotr/AppData/Local/Temp/wsl-crashes")
PYTHON = BASE / "software313-tmux38/venv/bin/python"


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def worker(module):
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (55, 55))
    import socket
    import ssl
    import sysconfig
    import importlib
    def forbidden(*args, **kwargs):
        raise AssertionError("No network, subprocess or SSL context is allowed in this import diagnostic")
    original_socket = socket.socket
    class LocalSocket(original_socket):
        def __init__(self, family=socket.AF_INET, *args, **kwargs):
            if family in (socket.AF_INET, socket.AF_INET6):
                forbidden()
            super().__init__(family, *args, **kwargs)
    socket.socket = LocalSocket
    socket.create_connection = forbidden
    ssl.create_default_context = forbidden
    subprocess.Popen = forbidden
    os.system = forbidden
    os.fork = forbidden
    os.posix_spawn = forbidden
    os.posix_spawnp = forbidden
    metadata = {"stage": "before_import", "module": module, "pid": os.getpid(),
                "python": sys.version, "binary": str(Path(sys.executable).resolve()),
                "gil_enabled": sys._is_gil_enabled(), "py_gil_disabled": sysconfig.get_config_var("Py_GIL_DISABLED"),
                "isolated": sys.flags.isolated, "ignore_environment": sys.flags.ignore_environment,
                "pycache_prefix": sys.pycache_prefix,
                "address_space_limit": resource.getrlimit(resource.RLIMIT_AS),
                "cpu_limit": resource.getrlimit(resource.RLIMIT_CPU),
                "personality": Path("/proc/self/personality").read_text().strip(),
                "process_security": [line for line in Path("/proc/self/status").read_text().splitlines()
                                     if line.startswith(("NoNewPrivs:", "Seccomp:", "Seccomp_filters:"))]}
    print(json.dumps(metadata), flush=True)
    assert metadata["gil_enabled"] and metadata["py_gil_disabled"] == 0
    imported = importlib.import_module(module)
    print(json.dumps({"stage": "import_complete", "module": module, "file": imported.__file__,
                      "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}), flush=True)


def compare(python=PYTHON, label="tokenizer-diagnostic-1", build="CPython3.13.18 classicGIL Clang23.1.3; verified official PBS archive, ELF BOLT text/warm/cold sections"):
    if not label or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in label):
        raise ValueError("Invalid unique diagnostic label")
    target = BASE / label
    if target.exists():
        raise RuntimeError("Do not repeat this diagnostic silently")
    target.mkdir()
    output = target / "report.json"
    known = set(CRASHES.iterdir())
    started = time.monotonic()
    deadline = started + 60
    report = {"status": "running", "diagnostic_only": True, "gate_pass": False, "cases": [],
              "limits": {"workload_seconds": 60, "post_cleanup_watch_seconds": 30,
                         "address_space_mib_each": 512, "concurrent_children": 1},
              "network_guard": "Python socket AF_INET/AF_INET6 constructors and SSL context blocked; no OS seccomp added",
              "subprocesses_in_workers": False, "database": False, "java": False, "tmux": False,
              "app_run": False, "gpu_or_models": False,
              "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
              "kernel_aslr_randomize_va_space": Path("/proc/sys/kernel/randomize_va_space").read_text().strip(),
              "binary_sha256": hashlib.sha256(python.resolve().read_bytes()).hexdigest(),
              "build": build,
              "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    plan = [("textual.css.tokenize", "existing_cache")] * 5
    plan += [("textual.css.tokenize", "fresh_cache")] * 5
    plan += [("devbrain.terminal_panels", "existing_cache")] * 5
    for index, (module, cache) in enumerate(plan):
        logpath = target / f"{index:02}-{module}-{cache}.log"
        argv = [str(python), "-I", "-X", "faulthandler", "-X", "importtime"]
        if cache == "fresh_cache":
            prefix = target / f"empty-cache-{index}"
            prefix.mkdir()
            argv += ["-X", "pycache_prefix=" + str(prefix)]
        argv += [str(Path(__file__).resolve()), "--worker", module]
        case = {"index": index, "module": module, "cache": cache, "argv": argv, "log": str(logpath)}
        with logpath.open("w") as log:
            child = subprocess.Popen(argv, stdout=log, stderr=log, start_new_session=True)
            try:
                while child.poll() is None:
                    new = set(CRASHES.iterdir()) - known
                    if new:
                        case["fatal_artifacts"] = sorted(str(path) for path in new)
                        break
                    if time.monotonic() >= deadline:
                        case["timeout"] = True
                        break
                    time.sleep(.05)
            finally:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait(timeout=3)
        case["returncode"] = child.wait()
        text = logpath.read_text(errors="replace")
        case["import_completed"] = '"stage": "import_complete"' in text
        report["cases"].append(case)
        write(output, report)
        print(json.dumps(case), flush=True)
        if case["returncode"] != 0 or case.get("fatal_artifacts") or case.get("timeout") or not case["import_completed"]:
            report["status"] = "stopped_on_failure"
            break
    # Keep watching after all process cleanup; no new child or load is started.
    report["workload_seconds"] = time.monotonic() - started
    write(output, report)
    for _ in range(300):
        new = set(CRASHES.iterdir()) - known
        if new:
            report["fatal_artifacts"] = sorted(str(path) for path in new)
            report["status"] = "stopped_on_native_fatal"
        time.sleep(.1)
    if report["status"] == "running":
        report["status"] = "pass_diagnostic_only"
    report["completed_imports"] = sum(case["import_completed"] for case in report["cases"])
    report["seconds_including_post_cleanup_watch"] = time.monotonic() - started
    write(output, report)
    print(json.dumps({key: value for key, value in report.items() if key != "cases"}), flush=True)
    return 0 if report["status"] == "pass_diagnostic_only" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=("textual.css.tokenize", "devbrain.terminal_panels"))
    parser.add_argument("--python", type=Path, default=PYTHON)
    parser.add_argument("--label", default="tokenizer-diagnostic-1")
    parser.add_argument("--build-description", default="CPython3.13.18 classicGIL Clang23.1.3; verified official PBS archive, ELF BOLT text/warm/cold sections")
    args = parser.parse_args()
    if args.worker:
        worker(args.worker)
    else:
        sys.exit(compare(args.python, args.label, args.build_description))
