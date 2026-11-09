#!/usr/bin/env python3
"""Software-only comparison: fresh imports, eval/GC and minimal headless Textual.

No DevBrain imports, database, Java, tmux, models, GPU or network requests in
workers. One child at a time; 120 seconds total; 512 MiB address-space cap.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import tarfile
import time

URL313 = "https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.13.18%2B20261026-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
SHA313 = "df15fe60d3ff55bf9dcf25824b5572756bb8816ff93865a273d3301d7066a8b7"
CRASHES = Path("/mnt/c/Users/piotr/AppData/Local/Temp/wsl-crashes")


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def prepare(root):
    target = root / "python313"
    target.mkdir(parents=True, exist_ok=True)
    archive = target / "python313.tar.gz"
    if not archive.exists():
        subprocess.run(["curl", "--fail", "--location", "--proto", "=https", "--max-time", "120",
                        "--output", str(archive), URL313], check=True, timeout=125)
    with archive.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if digest != SHA313:
        raise RuntimeError("Python 3.13 upstream archive SHA mismatch")
    runtime = target / "runtime"
    runtime.mkdir(exist_ok=True)
    if not (runtime / "python").exists():
        with tarfile.open(archive) as handle:
            handle.extractall(runtime, filter="data")
    python = runtime / "python/bin/python3.13"
    venv = target / "venv"
    subprocess.run([str(python), "-m", "venv", str(venv)], check=True, timeout=30)
    subprocess.run(["/home/piotr/.local/bin/uv", "pip", "install", "--python", str(venv / "bin/python"),
                    "--only-binary", ":all:", "--requirement", str(root / "requirements.txt")],
                   check=True, timeout=120)
    receipt = {"url": URL313, "sha256": digest, "python": str(python), "venv": str(venv),
               "release_source": "https://www.python.org/downloads/release/python-31318/",
               "asset_digest_source": "https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/20261026",
               "dependency_pins_sha256": hashlib.sha256((root / "requirements.txt").read_bytes()).hexdigest()}
    write(root / "evidence/python313-archive.json", receipt)
    print(json.dumps(receipt), flush=True)


def worker(kind):
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    import socket
    import ssl
    import sysconfig
    import gc
    def forbidden(*args, **kwargs):
        raise AssertionError("Diagnostic workers must not connect or load CA certificates")
    original_connect = socket.socket.connect
    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            forbidden()
        return original_connect(self, address)
    socket.socket.connect = connect
    socket.create_connection = forbidden
    ssl.create_default_context = forbidden
    from textual.app import App, ComposeResult
    from textual.widgets import DataTable, Input, Static
    import importlib.metadata
    started = time.monotonic()
    if kind == "eval":
        code = compile("sum(i * i for i in range(20))", "<synthetic-eval>", "eval")
        for index in range(10000):
            assert eval(code) == 2470
            if index % 100 == 0:
                gc.collect()
    elif kind == "textual":
        import asyncio
        class MinimalApp(App):
            def compose(self) -> ComposeResult:
                yield Static("Synthetic runtime probe")
                yield Input(id="input")
                yield DataTable(id="table")
            def on_mount(self):
                table = self.query_one(DataTable)
                table.add_columns("name", "value")
                table.add_row("alpha", "one")
                self.query_one(Input).focus()
        async def exercise():
            for _ in range(10):
                app = MinimalApp()
                async with app.run_test(size=(80, 24)) as pilot:
                    await pilot.press("a", "b", "c", "tab", "down", "up")
                    await pilot.pause()
                    assert app.query_one(Input).value == "abc"
                del app
                gc.collect()
        asyncio.run(exercise())
    metadata = {"python": sys.version, "executable": str(Path(sys.executable).resolve()),
                "gil_enabled": sys._is_gil_enabled() if hasattr(sys, "_is_gil_enabled") else True,
                "py_gil_disabled": sysconfig.get_config_var("Py_GIL_DISABLED"),
                "openssl": ssl.OPENSSL_VERSION, "textual": importlib.metadata.version("textual"),
                "kind": kind, "seconds": time.monotonic() - started,
                "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
    assert metadata["gil_enabled"] and not metadata["py_gil_disabled"]
    print(json.dumps(metadata), flush=True)


def compare(root, python, label):
    evidence = root / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    report_path = evidence / f"imports-{label}.json"
    if report_path.exists():
        raise RuntimeError("Do not repeat a completed diagnostic series under the same label")
    known = set(CRASHES.glob("*"))
    started = time.monotonic()
    deadline = started + 120
    report = {"status": "running", "python": str(python), "label": label, "cases": [],
              "max_concurrent_children": 1, "address_space_mib": 512, "total_timeout_seconds": 120,
              "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
              "network": False, "database": False, "java": False, "models": False}
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("DEVBRAIN_") and key not in {"PYTHONPATH", "PYTHONHOME"}}
    env["PYTHONFAULTHANDLER"] = "1"
    for index, kind in enumerate(["import"] * 30 + ["eval", "textual"]):
        output = evidence / f"imports-{label}-{index}-{kind}.log"
        case = {"index": index, "kind": kind, "log": str(output)}
        with output.open("w") as log:
            child = subprocess.Popen([str(python), "-I", "-X", "faulthandler", str(Path(__file__).resolve()),
                                      "worker", "--kind", kind], env=env, stdout=log, stderr=log,
                                     start_new_session=True)
            try:
                while True:
                    new = set(CRASHES.glob("*")) - known
                    if new:
                        case["fatal_artifacts"] = sorted(str(path) for path in new)
                        break
                    if child.poll() is not None:
                        break
                    if time.monotonic() >= deadline:
                        case["timeout"] = True
                        break
                    time.sleep(0.05)
            finally:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait(timeout=3)
            case["returncode"] = child.wait()
        report["cases"].append(case)
        report["seconds"] = time.monotonic() - started
        if case["returncode"] != 0 or case.get("fatal_artifacts") or case.get("timeout"):
            report["status"] = "stopped_on_failure"
            write(report_path, report)
            print(json.dumps(case), flush=True)
            return 1
        write(report_path, report)
        print(json.dumps(case), flush=True)
    report["status"] = "pass"
    write(report_path, report)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare313", "worker", "compare"))
    parser.add_argument("--root", type=Path, default=Path.home() / ".cache/devbrain-runtime-080")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--label", default="31216")
    parser.add_argument("--kind", choices=("import", "eval", "textual"), default="import")
    args = parser.parse_args()
    if args.action == "prepare313":
        prepare(args.root)
    elif args.action == "worker":
        worker(args.kind)
    else:
        return compare(args.root, args.python, args.label)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
