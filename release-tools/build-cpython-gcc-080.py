#!/usr/bin/env python3
"""Private GCC CPython/OpenSSL comparison, hard 2GiB build scopes, no global install."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tarfile
import time

BASE = Path.home() / ".cache/devbrain-runtime-080"
ROOT = BASE / "gcc31318"
CRASHES = Path("/mnt/c/Users/piotr/AppData/Local/Temp/wsl-crashes")
SOURCES = {
    "Python-3.13.18.tar.xz": ("https://www.python.org/ftp/python/3.13.18/Python-3.13.18.tar.xz",
                             "abb0102e6d4ae027509bcb82327d9dd85515b44a3ba8ad538cfc5e9847029f68"),
    "openssl-3.5.9.tar.gz": ("https://github.com/openssl/openssl/releases/download/openssl-3.5.9/openssl-3.5.9.tar.gz",
                            "603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a"),
}


def write(report):
    (ROOT / "report.json").write_text(json.dumps(report, indent=2) + "\n")


def main():
    if ROOT.exists():
        raise RuntimeError("Build root already exists; never silently repeat a failed series")
    ROOT.mkdir()
    for name in ("logs", "downloads", "deps"):
        (ROOT / name).mkdir()
    known = set(CRASHES.iterdir())
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    report = {"status": "running", "boot_id": boot, "stages": [], "sources": {},
              "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "memory_limit_bytes": 2 * 1024**3, "make_jobs": 2,
              "bytecode_install_jobs": 1, "global_changes": False}
    write(report)
    env = {key: value for key, value in os.environ.items()
           if key not in {"PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "LD_LIBRARY_PATH", "MAKEFLAGS", "CFLAGS", "LDFLAGS", "CPPFLAGS"}
           and not key.startswith("DEVBRAIN_")}
    env.update(PATH="/usr/bin:/bin", CC="gcc", CFLAGS="-O2 -g -fno-omit-frame-pointer")

    def observe():
        new = set(CRASHES.iterdir()) - known
        if new:
            report.update(status="stopped_on_native_fatal", fatal_artifacts=sorted(str(p) for p in new))
            write(report)
            raise RuntimeError("New native core; entire build series stopped")
        if Path("/proc/sys/kernel/random/boot_id").read_text().strip() != boot:
            report["status"] = "stopped_on_boot_change"
            write(report)
            raise RuntimeError("Kernel boot changed")

    def run(label, argv, cwd=ROOT, scoped=False, timeout=600, extra=None):
        observe()
        logpath = ROOT / "logs" / (label + ".log")
        unit = f"devbrain-gcc080-{len(report['stages'])}-{time.time_ns()}.scope"
        actual = argv
        if scoped:
            actual = ["systemd-run", "--user", "--scope", "--quiet", "--unit=" + unit,
                      "--property=MemoryMax=2147483648", "--property=MemorySwapMax=0", "--", *argv]
        stage = {"label": label, "argv": argv, "cwd": str(cwd), "log": str(logpath),
                 "scoped_memory_limit": scoped}
        report["stages"].append(stage)
        write(report)
        started = time.monotonic()
        with logpath.open("w") as log:
            child = subprocess.Popen(actual, cwd=cwd, env={**env, **(extra or {})},
                                     stdout=log, stderr=log, start_new_session=True)
            try:
                while child.poll() is None:
                    observe()
                    if time.monotonic() - started > timeout:
                        raise TimeoutError("Bounded build stage timed out")
                    if scoped and "control_group" not in stage:
                        query = subprocess.run(["systemctl", "--user", "show", unit,
                                                "--property=ControlGroup", "--value"],
                                               capture_output=True, text=True, timeout=3)
                        group = query.stdout.strip()
                        if group.startswith("/user.slice/"):
                            stage["control_group"] = group
                    if scoped and stage.get("control_group"):
                        path = Path("/sys/fs/cgroup") / stage["control_group"].lstrip("/")
                        if (path / "memory.max").exists():
                            stage["memory_max"] = (path / "memory.max").read_text().strip()
                            stage["memory_peak"] = max(stage.get("memory_peak", 0), int((path / "memory.peak").read_text()))
                            if stage["memory_max"] != "2147483648":
                                raise RuntimeError("Build memory limit did not apply")
                    time.sleep(.1)
            finally:
                if child.poll() is None:
                    if scoped:
                        subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True, timeout=10)
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait(timeout=3)
                stage.update(returncode=child.returncode, seconds=time.monotonic() - started)
                write(report)
        observe()
        if child.returncode:
            text = logpath.read_text(errors="replace")
            stage["compiler_native_failure"] = any(token in text for token in
                ("internal compiler error", "Segmentation fault", "Aborted", "core dumped"))
            raise RuntimeError("Build stage failed: " + label)
        print(json.dumps({"stage": label, "seconds": stage["seconds"], "memory_peak": stage.get("memory_peak")}), flush=True)

    try:
        # apt-get download validates package hashes against the signed local
        # apt metadata. Verify its InRelease signatures again for the receipt.
        for index, release in enumerate(Path("/var/lib/apt/lists").glob("*ubuntu*dists*noble*InRelease")):
            run(f"apt-signature-{index}", ["gpgv", "--keyring", "/usr/share/keyrings/ubuntu-archive-keyring.gpg", str(release)])
        packages = ["libffi-dev", "libbz2-dev", "liblzma-dev", "libsqlite3-dev", "libreadline-dev", "libncurses-dev", "uuid-dev"]
        run("headers-download", ["apt-get", "download", *packages], cwd=ROOT / "deps", timeout=120)
        report["dependency_debs"] = {}
        for index, package in enumerate((ROOT / "deps").glob("*.deb")):
            report["dependency_debs"][package.name] = hashlib.sha256(package.read_bytes()).hexdigest()
            run(f"headers-extract-{index}", ["dpkg-deb", "-x", str(package), str(ROOT / "deps/root")])
        lib = ROOT / "deps/root/usr/lib/x86_64-linux-gnu"
        for link in lib.iterdir():
            if link.is_symlink() and not link.exists():
                installed = Path("/usr/lib/x86_64-linux-gnu") / link.resolve().name
                if installed.exists():
                    link.unlink()
                    link.symlink_to(installed)
        for filename, (url, expected) in SOURCES.items():
            archive = ROOT / "downloads" / filename
            run("download-" + filename, ["curl", "--fail", "--location", "--proto", "=https", "--max-time", "120", "--output", str(archive), url], timeout=125)
            actual = hashlib.sha256(archive.read_bytes()).hexdigest()
            if actual != expected:
                raise RuntimeError("Official source SHA mismatch: " + filename)
            report["sources"][filename] = {"url": url, "sha256": actual}
            with tarfile.open(archive) as stream:
                stream.extractall(ROOT, filter="data")
        openssl = ROOT / "openssl-3.5.9"
        ssl_prefix = ROOT / "openssl"
        run("openssl-configure", ["./Configure", "linux-x86_64", "shared", "no-tests", "--prefix=" + str(ssl_prefix),
                                  "--openssldir=" + str(ssl_prefix / "ssl"), "--libdir=lib", "-O2", "-g"], cwd=openssl, scoped=True)
        run("openssl-make", ["make", "-j2", "build_sw"], cwd=openssl, scoped=True)
        run("openssl-install", ["make", "-j1", "install_sw"], cwd=openssl, scoped=True)
        source = ROOT / "Python-3.13.18"
        prefix = ROOT / "runtime"
        includes = ROOT / "deps/root/usr/include"
        extra = {"CPPFLAGS": f"-I{includes} -I{includes / 'x86_64-linux-gnu'}",
                 "LDFLAGS": f"-L{lib}", "LIBFFI_CFLAGS": f"-I{includes / 'x86_64-linux-gnu'}",
                 "LIBFFI_LIBS": f"-L{lib} -lffi"}
        configure = ["./configure", "--prefix=" + str(prefix), "--with-ensurepip=no", "--disable-test-modules",
                     "--disable-optimizations", "--with-lto=no", "--disable-bolt", "--enable-experimental-jit=no",
                     "--with-openssl=" + str(ssl_prefix), "--with-openssl-rpath=auto"]
        report["configure"] = configure
        report["build_environment"] = {**{key: env[key] for key in ("CC", "CFLAGS", "PATH")}, **extra}
        run("python-configure", configure, cwd=source, scoped=True, extra=extra)
        run("python-make", ["make", "-j2"], cwd=source, scoped=True, extra=extra)
        run("python-install", ["make", "-j1", "install", "COMPILEALL_OPTS=-j1"], cwd=source, scoped=True, extra=extra)
        python = prefix / "bin/python3.13"
        probe = "import json,ssl,_ssl,sys,sysconfig; print(json.dumps({'python':sys.version,'ssl':ssl.OPENSSL_VERSION,'ssl_module':_ssl.__file__,'gil':sys._is_gil_enabled(),'config':sysconfig.get_config_var('CONFIG_ARGS'),'cflags':sysconfig.get_config_var('CFLAGS')}))"
        run("python-metadata", [str(python), "-I", "-c", probe], scoped=True)
        metadata = json.loads((ROOT / "logs/python-metadata.log").read_text().strip())
        if "3.5.9" not in metadata["ssl"] or not metadata["gil"]:
            raise RuntimeError("Private OpenSSL/GIL metadata mismatch")
        report["metadata"] = metadata
        report["python_binary_sha256"] = hashlib.sha256(python.read_bytes()).hexdigest()
        run("ssl-linkage", ["ldd", metadata["ssl_module"]])
        linkage = (ROOT / "logs/ssl-linkage.log").read_text()
        if str(ssl_prefix / "lib/libssl.so.3") not in linkage or str(ssl_prefix / "lib/libcrypto.so.3") not in linkage:
            raise RuntimeError("Python did not link the private OpenSSL libraries")
        uv = "/home/piotr/.local/bin/uv"
        run("venv", [uv, "venv", "--python", str(python), str(ROOT / "venv")], scoped=True)
        run("dependencies", [uv, "pip", "install", "--python", str(ROOT / "venv/bin/python"), "--only-binary", ":all:",
                             "-r", str(BASE / "requirements.txt")], scoped=True, timeout=120)
        run("frozen-project", [uv, "pip", "install", "--python", str(ROOT / "venv/bin/python"), "--no-deps", "--editable",
                               str(BASE / "software313-tmux38/java-test-source")], scoped=True, timeout=120)
        report["status"] = "built_diagnostic_only"
    except BaseException as exc:
        if report["status"] == "running":
            report["status"] = "stopped_on_failure"
        report["error"] = str(exc)
    finally:
        # Native compiler/core writers can finish after the parent process exits.
        for _ in range(300):
            try:
                observe()
            except RuntimeError:
                pass
            time.sleep(.1)
        write(report)
    print(json.dumps({key: value for key, value in report.items() if key not in {"stages", "build_environment"}}), flush=True)
    return 0 if report["status"] == "built_diagnostic_only" else 1


if __name__ == "__main__":
    raise SystemExit(main())
