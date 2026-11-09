#!/usr/bin/env python3
"""Bounded, isolated runtime diagnostics. No model, database or service requests.

Run under Linux. Artifacts stay in --root; runtime archives are pinned to hashes
published by the upstream release APIs. A fatal child stops its entire series.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import resource
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tarfile
import time
import xml.etree.ElementTree as ET

ARCHIVES = {
    "python": {
        "url": "https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.12.16%2B20261026-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz",
        "sha256": "ce4f6d0a6cfdb3bed6e44c2f9affd85943c76a42e25ad3a259002f31d6fb8a20",
        "source": "https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/20261026",
        "directory": "python",
    },
    "java": {
        "url": "https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.13%2B9/OpenJDK21U-jdk_x64_linux_hotspot_21.0.13_9.tar.gz",
        "sha256": "d7f5d5cf865a2902b70ff549b97586a7d3f234e016f041ab447ea740c7d66d1c",
        "source": "https://api.adoptium.net/v3/assets/latest/21/hotspot?architecture=x64&image_type=jdk&os=linux&vendor=eclipse",
        "directory": "jdk-21.0.13+9",
    },
}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def command(argv, timeout=20):
    result = subprocess.run(argv, text=True, capture_output=True, timeout=timeout)
    return {"argv": argv, "returncode": result.returncode,
            "stdout": result.stdout, "stderr": result.stderr}


def metadata():
    import _ssl
    ssl_binary = getattr(_ssl, "__file__", sys.executable)
    packages = {}
    for name in ("httpx", "httpcore", "certifi", "pytest", "cryptography"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            pass
    result = {"time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "python": sys.version, "executable": sys.executable,
              "real_executable": str(Path(sys.executable).resolve()),
              "openssl": ssl.OPENSSL_VERSION, "ssl_module": getattr(_ssl, "__file__", "built-in"),
              "kernel": platform.release(), "machine": platform.machine(),
              "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
              "packages": packages,
              "dpkg": command(["dpkg-query", "-W", "-f=${Package} ${Version}\n",
                               "python3.12", "libpython3.12-stdlib", "libssl3t64", "openssl",
                               "libc6", "openjdk-21-jre-headless"]),
              "ssl_linkage": command(["ldd", ssl_binary])}
    return result


def prepare(root):
    receipts = {}
    for name, spec in ARCHIVES.items():
        archive = root / "downloads" / (name + ".tar.gz")
        archive.parent.mkdir(parents=True, exist_ok=True)
        if not archive.exists():
            partial = archive.with_suffix(".partial")
            subprocess.run(["curl", "--fail", "--location", "--proto", "=https",
                            "--tlsv1.2", "--max-time", "180", "--output", str(partial),
                            spec["url"]], check=True, timeout=190)
            partial.replace(archive)
        digest = hashlib.file_digest(archive.open("rb"), "sha256").hexdigest()
        if digest != spec["sha256"]:
            raise RuntimeError(f"{name} archive failed upstream SHA256 verification")
        destination = root / "runtimes"
        destination.mkdir(exist_ok=True)
        if not (destination / spec["directory"]).exists():
            with tarfile.open(archive) as handle:
                handle.extractall(destination, filter="data")
        receipts[name] = {**spec, "verified_sha256": digest,
                          "path": str(destination / spec["directory"])}
        write(root / "evidence" / "archives.json", receipts)
    print(json.dumps(receipts))


def worker(kind, threads, count):
    # Bound virtual memory and CPU; no network sockets are created.
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (115, 115))
    import certifi
    import httpx
    def run(_):
        for _ in range(count):
            if kind == "context":
                ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            elif kind == "certifi":
                ssl.create_default_context(cafile=certifi.where())
            elif kind == "system_ca":
                ssl.create_default_context(cafile="/etc/ssl/certs/ca-certificates.crt")
            elif kind == "httpx":
                with httpx.Client(trust_env=False):
                    pass
            elif kind == "arcade_http":
                with httpx.Client(trust_env=False, verify=False, follow_redirects=False):
                    pass
        return count
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=threads) as pool:
        completed = sum(pool.map(run, range(threads)))
    print(json.dumps({"kind": kind, "threads": threads, "completed": completed,
                      "seconds": time.monotonic() - started,
                      "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}))


def repro(root, interpreter, label):
    evidence = root / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    result = {"label": label, "interpreter": str(interpreter), "cases": [],
              "status": "running", "constraints": {"timeout_seconds": 120,
              "address_space_mib": 512, "network": False, "database": False}}
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
    env["PYTHONFAULTHANDLER"] = "1"
    result["metadata"] = json.loads(subprocess.check_output(
        [str(interpreter), str(Path(__file__).resolve()), "metadata"], env=env, text=True))
    report = evidence / f"ssl-{label}.json"
    for count in (100, 1000):
        for threads in (1, 2):
            for kind in ("context", "certifi", "system_ca", "httpx", "arcade_http"):
                output = evidence / f"ssl-{label}-{kind}-{threads}t-{count}.log"
                argv = [str(interpreter), "-X", "faulthandler", str(Path(__file__).resolve()),
                        "worker", "--kind", kind, "--threads", str(threads), "--count", str(count)]
                started = time.monotonic()
                with output.open("w") as log:
                    try:
                        completed = subprocess.run(argv, stdout=log, stderr=log, timeout=120,
                                                   env=env, cwd=evidence)
                        code = completed.returncode
                    except subprocess.TimeoutExpired:
                        code = "timeout"
                case = {"kind": kind, "threads": threads, "iterations_per_thread": count,
                        "returncode": code, "seconds": time.monotonic() - started,
                        "log": str(output)}
                result["cases"].append(case)
                if code != 0:
                    result["status"] = "stopped_on_failure"
                    write(report, result)
                    print(json.dumps(case), flush=True)
                    return 1
                write(report, result)
                print(json.dumps(case), flush=True)
    result["status"] = "pass"
    write(report, result)
    return 0


def monitored_pytest(argv, source, env, log, java_pid, crash_dir, known_crashes):
    """A passing parent is insufficient: detect native child crashes immediately."""
    process = subprocess.Popen(argv, cwd=source, env=env, stdout=log, stderr=log, start_new_session=True)
    deadline = time.monotonic() + 1800
    failure = {}
    try:
        while True:
            newly_created = set(crash_dir.glob("*")) - known_crashes
            if newly_created:
                failure = {"fatal_artifacts": sorted(str(path) for path in newly_created)}
            elif not Path(f"/proc/{java_pid}").exists():
                failure = {"managed_java_lost": True}
            elif time.monotonic() >= deadline:
                failure = {"timeout": True}
            code = process.poll()
            if failure or code is not None:
                break
            time.sleep(0.25)
        if failure and process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        return process.returncode, failure
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)


def java_tests(root, project, corrected_setup=False, evaluation_only=False, selection_only=False, repetitions=4,
               tmux=None):
    """Four serial, fresh JVMs: first 56+evaluation modules, then 3x56 only."""
    source = root / "java-test-source"
    if source.exists() and not corrected_setup and not evaluation_only:
        raise RuntimeError("Immutable Java test source already exists; do not silently repeat a run")
    if corrected_setup:
        previous = json.loads((root / "evidence/java-tests.json").read_text())
        if (len(previous["runs"]) != 1 or previous["runs"][0].get("returncode") != 1
                or "ModuleNotFoundError: No module named 'devbrain'" not in
                Path(previous["runs"][0]["pytest_log"]).read_text()):
            raise RuntimeError("Only the recorded missing-install setup error can be corrected here")
        for path in list((root / "evidence").glob("temurin-c2-0-*")) + [root / "evidence/java-tests.json"]:
            path.replace(path.with_name("setup-" + path.name))
    elif not source.exists():
        shutil.copytree(project / "devbrain", source, ignore=shutil.ignore_patterns(
            ".venv", "__pycache__", ".pytest_cache", "*.egg-info"))
    # Native workers intentionally use -I, so PYTHONPATH alone is insufficient.
    subprocess.run(["/home/piotr/.local/bin/uv", "pip", "install", "--python", str(root / "venv/bin/python"),
                    "--no-deps", "--editable", str(source)], check=True, timeout=120)
    sys.path.insert(0, str(source / "src"))
    from devbrain.config import Settings
    from devbrain.engine import install_engine, start_engine, stop_engine
    selected = json.loads((project / "evidence/devbrain-c1-diagnostic-retry-plan-20261107.json").read_text())["remaining"]
    if evaluation_only:
        selected = []
    tag = "evaluation" if evaluation_only else "c2-final" if selection_only else "temurin-c2"
    interpreter = root / "venv/bin/python"
    jdk = root / "runtimes/jdk-21.0.13+9"
    evidence = root / "evidence"
    crash_dir = Path("/mnt/c/Users/piotr/AppData/Local/Temp/wsl-crashes")
    if not crash_dir.is_dir():
        raise RuntimeError("Native-crash observation directory is unavailable")
    known_crashes = set(crash_dir.glob("*"))
    report = {"status": "running", "selection_count": len(selected), "runs": [],
              "kernel": platform.release(), "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
              "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "python": str(interpreter), "java": str(jdk / "bin/java"),
              "tmux": str(tmux) if tmux else None,
              "source_sha256": {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in source.rglob("*.py")}}
    env = {k: v for k, v in os.environ.items() if not k.startswith("DEVBRAIN_")
           and k not in {"JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS", "PYTHONHOME"}}
    env.update(PYTHONPATH=str(source / "src"), PYTHONFAULTHANDLER="1",
               PATH=f"{jdk / 'bin'}:/home/piotr/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    if tmux:
        env.update(DEVBRAIN_TMUX_EXECUTABLE=str(tmux), DEVBRAIN_TEST_TMUX=str(tmux))
    for index in range(1 if evaluation_only else repetitions):
        home = root / f"java-engine-{'evaluation-' if evaluation_only else 'corrected-' if corrected_setup else ''}{index}"
        for key in tuple(os.environ):
            if key.startswith("DEVBRAIN_") or key in {"JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS"}:
                os.environ.pop(key, None)
        os.environ.update(env)
        for key in ("JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS"):
            os.environ.pop(key, None)
        settings = Settings.load(home, create=True)
        settings.java_executable = str(jdk / "bin/java")
        if tmux:
            settings.tmux_executable = str(tmux)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            settings.arcade_url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        settings.save()
        install_engine(settings, Path("/home/piotr/.cache/devbrain-validation-20261107/tooling/arcadedb-26.11.1"))
        compilation = evidence / f"{tag}-{index}-compilation.xml"
        os.environ["JAVA_TOOL_OPTIONS"] = f"-XX:+UnlockDiagnosticVMOptions -XX:+LogCompilation -XX:LogFile={compilation}"
        run = {"index": index, "home": str(home), "c2_compilation_log": str(compilation)}
        started = time.monotonic()
        try:
            start_engine(settings)
            pid = json.loads((home / "arcade/process.json").read_text())["pid"]
            run["pid"] = pid
            flags = command([str(jdk / "bin/jcmd"), str(pid), "VM.flags", "-all"])
            write(evidence / f"{tag}-{index}-flags.json", flags)
            run["tiered_stop_level_4"] = "TieredStopAtLevel" in flags["stdout"] and any(
                "TieredStopAtLevel" in line and "= 4" in line for line in flags["stdout"].splitlines())
            if flags["returncode"] or not run["tiered_stop_level_4"]:
                raise RuntimeError("Could not verify default C2 compilation settings")
            test_env = dict(env, DEVBRAIN_TEST_ARCADE_HOME=str(home),
                            DEVBRAIN_JAVA_EXECUTABLE=str(jdk / "bin/java"))
            tests = list(selected)
            if index == 0 and not selection_only:
                tests += ["tests/test_evaluation.py", "tests/test_native_evaluation.py"]
            log_path = evidence / f"{tag}-{index}-pytest.log"
            junit = evidence / f"{tag}-{index}-pytest.xml"
            with log_path.open("w") as log:
                code, native_failure = monitored_pytest(
                    [str(interpreter), "-m", "pytest", "-q", "-x", *tests, "--junitxml", str(junit)],
                    source, test_env, log, pid, crash_dir, known_crashes)
            run.update(returncode=code, pytest_log=str(log_path), junit=str(junit), **native_failure)
            write(evidence / f"{tag}-{index}-compiler.json", command(
                [str(jdk / "bin/jcmd"), str(pid), "Compiler.codecache"]))
            run["c2_compiled"] = compilation.exists() and "compiler='c2'" in compilation.read_text(errors="replace")
            run["java_memory"] = [line for line in Path(f"/proc/{pid}/status").read_text().splitlines()
                                  if line.startswith(("VmRSS:", "VmHWM:", "VmSize:"))]
            if code != 0 or native_failure:
                report["status"] = "stopped_on_failure"
        except BaseException as exc:
            run["error_type"] = type(exc).__name__
            report["status"] = "stopped_on_failure"
        finally:
            run["seconds"] = time.monotonic() - started
            try:
                run["stop"] = stop_engine(settings)
            except BaseException as exc:
                run["stop_error_type"] = type(exc).__name__
            # Short-lived native children can fail during fixture/engine cleanup
            # even when the parent pytest has already printed PASS.
            time.sleep(2)
            after_cleanup = set(crash_dir.glob("*")) - known_crashes
            if after_cleanup:
                run["fatal_artifacts_after_cleanup"] = sorted(str(path) for path in after_cleanup)
                report["status"] = "stopped_on_failure"
            report["runs"].append(run)
            write(evidence / ("evaluation-tests.json" if evaluation_only else "java-tests.json"), report)
        print(json.dumps(run), flush=True)
        if report["status"] != "running":
            return 1
    report["status"] = "pass"
    write(evidence / ("evaluation-tests.json" if evaluation_only else "java-tests.json"), report)
    return 0


def backtrace(root, core, binary):
    debugger = root / "debugger"
    for package in (debugger / "packages").glob("*.deb"):
        subprocess.run(["dpkg-deb", "-x", str(package), str(debugger / "root")], check=True)
    env = dict(os.environ, LD_LIBRARY_PATH=str(debugger / "root/usr/lib/x86_64-linux-gnu"),
               DEBUGINFOD_URLS="")
    output = root / "evidence" / ("native-" + core.name + ".txt")
    # Never display locals, argument values, arbitrary memory or environment.
    with output.open("w") as log:
        result = subprocess.run([str(debugger / "root/usr/bin/gdb"), "--batch", "--nx", "--nh",
            "-iex", "set auto-load off", "-iex", "set print frame-arguments none",
            "-iex", "set print entry-values no", "-iex", "set debuginfod enabled off",
            "-iex", "set debug-file-directory " + str(debugger / "symbols"),
            "-ex", "set pagination off", "-ex", "info sharedlibrary",
            "-ex", "thread apply all bt 20", str(binary), str(core)],
            env=env, stdout=log, stderr=log, timeout=90)
    print(json.dumps({"returncode": result.returncode, "log": str(output)}))


def gate(root, project):
    """Publish a bounded qualification, never an implicit production/soak approval."""
    def read(path):
        return json.loads(path.read_text())
    def suite(path):
        doc = ET.parse(path).getroot()
        cases = list(doc.iter("testcase"))
        return {"count": len(cases), "failures": len(list(doc.iter("failure"))),
                "errors": len(list(doc.iter("error"))), "skipped": len(list(doc.iter("skipped"))),
                "artifact": str(path)}
    old = read(root / "evidence/ssl-ubuntu3123.json")
    new = read(root / "evidence/ssl-standalone31216.json")
    evaluation = read(root / "evidence/evaluation-tests.json")
    java = read(root / "c2-final/evidence/java-tests.json")
    archives = read(root / "evidence/archives.json")
    unit = suite(project / "evidence/runtime-080-unit.xml")
    native = suite(root / "evidence/evaluation-0-pytest.xml")
    repetitions = []
    live = []
    for run in java["runs"]:
        result = (suite(Path(run["junit"])) if run.get("junit") and Path(run["junit"]).exists()
                  else {"count": 0, "failures": 0, "errors": 1, "skipped": 0, "incomplete": True})
        repetitions.append({**result, "c2": run.get("c2_compiled", False),
                            "tiered_level_4": run.get("tiered_stop_level_4", False),
                            "seconds": run.get("seconds"), "java_memory": run.get("java_memory"),
                            "returncode": run.get("returncode")})
        try:
            command_line = Path(f"/proc/{run['pid']}/cmdline").read_bytes()
            if str(Path(run["home"]) / "arcade").encode() in command_line:
                live.append(run["pid"])
        except OSError:
            pass
    def clean(value, count):
        return value["count"] == count and value["failures"] == value["errors"] == value["skipped"] == 0
    checks = {
        "ssl_baseline": {"pass": old["status"] == "pass", "cases": len(old["cases"]), "not_reproduced": True},
        "ssl_candidate": {"pass": new["status"] == "pass", "cases": len(new["cases"])},
        "unit": {"pass": clean(unit, 40), **unit},
        "native_evaluation": {"pass": evaluation["status"] == "pass" and clean(native, 32)
                              and evaluation["runs"][0].get("c2_compiled", False), **native},
        "c2_repetitions": {"pass": java["status"] == "pass" and len(repetitions) == 3
                           and all(clean(item, 56) and item["c2"] and item["tiered_level_4"]
                                   and item["returncode"] == 0 for item in repetitions),
                           "expected_runs": 3, "runs": repetitions},
        "engines_stopped": {"pass": not live, "live_owned_pids": live},
        "native_children": {"pass": not list((root / "evidence").glob("native-*devbrain-runtime-080*runtimes*.txt")),
                            "fatal_backtraces": [str(p) for p in (root / "evidence").glob("native-*devbrain-runtime-080*runtimes*.txt")]},
    }
    python = (root / "runtimes/python/bin/python3.12").resolve()
    java_binary = (root / "runtimes/jdk-21.0.13+9/bin/java").resolve()
    binary_integrity = {}
    for name, binary, member in (("python", python, "python/bin/python3.12"),
                                  ("java", java_binary, "jdk-21.0.13+9/bin/java")):
        with tarfile.open(root / "downloads" / (name + ".tar.gz")) as archive:
            expected = hashlib.file_digest(archive.extractfile(member), "sha256").hexdigest()
        actual = hashlib.sha256(binary.read_bytes()).hexdigest()
        binary_integrity[name] = {"matches_verified_archive": actual == expected,
                                  "expected_sha256": expected, "actual_sha256": actual}
    checks["binary_integrity"] = {"pass": all(value["matches_verified_archive"]
                                               for value in binary_integrity.values())}
    result = {
        "schema_version": 1, "scope": "bounded_runtime_qualification",
        "status": "pass" if all(value["pass"] for value in checks.values()) else "blocked",
        "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "runtime": {"python_executable": str(python), "python_version": new["metadata"]["python"],
                    "openssl": new["metadata"]["openssl"], "java_executable": str(java_binary),
                    "java_version": command([str(java_binary), "-version"]),
                    "python_binary_sha256": hashlib.sha256(python.read_bytes()).hexdigest(),
                    "java_binary_sha256": hashlib.sha256(java_binary.read_bytes()).hexdigest(),
                    "archives": archives, "binary_integrity": binary_integrity},
        "checks": checks, "c2_source_report": str(root / "c2-final/evidence/java-tests.json"),
        "c2_source_manifest_sha256": hashlib.sha256(json.dumps(java["source_sha256"], sort_keys=True).encode()).hexdigest(),
        "system_package_integrity": command(["dpkg", "-V", "python3.12", "libpython3.12-stdlib",
                                             "libssl3t64", "libc6", "openjdk-21-jre-headless"]),
        "root_cause_established": False, "full_suite_required": True,
        "production_or_soak_authorized": False,
        "limitations": ["Both baseline and candidate SSL repros passed; the original native crash was not reproduced.",
                        "Candidate changes CPython, compiler/build and OpenSSL together; it does not isolate the faulty component.",
                        "Independent kernel/JVM/Python/tmux failures remain unexplained; C1 was not used in these gates.",
                        "Full current-source suite, explicit operator start and monitored soak are separate gates."]}
    write(project / "evidence/runtime-080-gate.json", result)
    print(json.dumps({"status": result["status"], "artifact": str(project / "evidence/runtime-080-gate.json")}))
    return 0 if result["status"] == "pass" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "metadata", "worker", "repro", "java-tests", "backtrace", "gate"))
    parser.add_argument("--root", type=Path, default=Path.home() / ".cache/devbrain-runtime-080")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--label", default="baseline")
    parser.add_argument("--kind", choices=("context", "certifi", "system_ca", "httpx", "arcade_http"))
    parser.add_argument("--threads", type=int, choices=(1, 2), default=1)
    parser.add_argument("--count", type=int, choices=(100, 1000), default=100)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--core", type=Path)
    parser.add_argument("--binary", type=Path, default=Path("/usr/bin/python3.12"))
    parser.add_argument("--corrected-setup", action="store_true")
    parser.add_argument("--evaluation-only", action="store_true")
    parser.add_argument("--selection-only", action="store_true")
    parser.add_argument("--repetitions", type=int, choices=(1, 2, 3, 4), default=4)
    parser.add_argument("--runtime-base", type=Path)
    parser.add_argument("--tmux", type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.root)
    elif args.action == "metadata":
        print(json.dumps(metadata()))
    elif args.action == "worker":
        worker(args.kind, args.threads, args.count)
    elif args.action == "java-tests":
        if args.runtime_base:
            args.root.mkdir(parents=True, exist_ok=True)
            (args.root / "evidence").mkdir(exist_ok=True)
            for name in ("runtimes", "venv"):
                (args.root / name).symlink_to(args.runtime_base / name, target_is_directory=True)
        return java_tests(args.root, args.project, args.corrected_setup, args.evaluation_only,
                          args.selection_only, args.repetitions, args.tmux)
    elif args.action == "backtrace":
        backtrace(args.root, args.core, args.binary)
    elif args.action == "gate":
        return gate(args.root, args.project)
    else:
        return repro(args.root, args.python, args.label)
    return 0


if __name__ == "__main__":
    sys.exit(main())
