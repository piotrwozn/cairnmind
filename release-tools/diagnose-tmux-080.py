#!/usr/bin/env python3
"""Build a verified unmodified upstream tmux privately, then bounded teardown trials."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import time

SHA = "e79c699c7e949dccd0a4a125e17b8d1261e311b16979dbc8eb34542f3966d82e"
URL = "https://github.com/tmux/tmux/releases/download/3.8/tmux-3.8.tar.gz"
CRASHES = Path("/mnt/c/Users/piotr/AppData/Local/Temp/wsl-crashes")


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def build(root):
    archive = root / "downloads/tmux-3.8.tar.gz"
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != SHA:
        raise RuntimeError("Upstream tmux SHA mismatch")
    work = root / "tmux38"
    work.mkdir(exist_ok=True)
    with tarfile.open(archive) as stream:
        stream.extractall(work, filter="data")
    source = work / "tmux-3.8"
    window = (source / "window.c").read_text()
    destroy = window.split("window_destroy(struct window *w)", 1)[1].split("\n}\n", 1)[0]
    if "window_unzoom(" in destroy or "w->flags &= ~WINDOW_ZOOMED" not in destroy:
        raise RuntimeError("Expected upstream b25af0 fix absent")
    deps = work / "deps"
    deps.mkdir(exist_ok=True)
    logpath = root / "evidence/tmux38-build.log"
    if logpath.exists():
        logpath.rename(logpath.with_name(f"tmux38-build-setup-{time.time_ns()}.log"))
    with logpath.open("w") as log:
        def run(argv, **kwargs):
            subprocess.run(argv, stdout=log, stderr=log, check=True, timeout=180, **kwargs)
        run(["apt-get", "download", "libncurses-dev", "bison", "pkgconf-bin", "libpkgconf3"], cwd=deps)
        for package in deps.glob("*.deb"):
            run(["dpkg-deb", "-x", str(package), str(deps / "root")])
        lib = deps / "root/usr/lib/x86_64-linux-gnu"
        # Development symlinks normally point to runtime libraries supplied by
        # separate packages. Reuse this machine's libraries without copying or
        # modifying them or the global linker paths.
        for link in lib.iterdir():
            if link.is_symlink() and not link.exists():
                target = Path("/usr/lib/x86_64-linux-gnu") / link.resolve().name
                if target.exists():
                    link.unlink()
                    link.symlink_to(target)
        env = dict(os.environ, PATH=f"{deps / 'root/usr/bin'}:/usr/bin:/bin",
                   LD_LIBRARY_PATH=str(lib), CPPFLAGS=f"-I{deps / 'root/usr/include'}",
                   LDFLAGS=f"-L{lib}", PKG_CONFIG=str(deps / "root/usr/bin/pkgconf"),
                   LIBTINFO_CFLAGS=f"-I{deps / 'root/usr/include'}", LIBTINFO_LIBS="-l:libtinfo.so.6",
                   BISON_PKGDATADIR=str(deps / "root/usr/share/bison"))
        prefix = root / "runtimes/tmux-3.8"
        run(["./configure", f"--prefix={prefix}"], cwd=source, env=env)
        run(["make", "-j2"], cwd=source, env=env)
        run(["make", "install"], cwd=source, env=env)
    binary = prefix / "bin/tmux"
    version = subprocess.check_output([str(binary), "-V"], text=True).strip()
    receipt = {"source_url": URL, "source_sha256": digest, "source_unmodified": True,
               "binary": str(binary), "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
               "version": version, "upstream_fix": "b25af0a08ca7077fab88ec814cc8fcb57016a648",
               "window_destroy_source": "window_destroy(struct window *w)" + destroy + "\n}",
               "window_c_sha256": hashlib.sha256((source / "window.c").read_bytes()).hexdigest(),
               "build_log": str(logpath), "dependency_debs": {
                   p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in deps.glob("*.deb")}}
    write(root / "evidence/tmux38-build.json", receipt)
    print(json.dumps({k: v for k, v in receipt.items() if k != "window_destroy_source"}), flush=True)


def trials(root):
    binary = root / "runtimes/tmux-3.8/bin/tmux"
    output = root / "evidence/tmux38-teardown.json"
    if output.exists():
        raise RuntimeError("Never overwrite or silently repeat a qualification")
    known = set(CRASHES.glob("*"))
    report = {"status": "running", "binary": str(binary), "trials": [], "cores": []}
    started = time.monotonic()
    def cores():
        result = sorted(str(p) for p in set(CRASHES.glob("*")) - known)
        if result:
            report.update(status="stopped_on_native_fatal", cores=result)
            write(output, report)
            raise RuntimeError("New native fatal during teardown qualification")
    for iteration in range(3):
        socket = root / f"tmux38-trial-{iteration}.sock"
        calls = 0
        def run(*args):
            nonlocal calls
            cores()
            result = subprocess.run([str(binary), "-S", str(socket), "-f", "/dev/null", *args],
                                    capture_output=True, text=True, timeout=5)
            calls += 1
            if result.returncode:
                raise RuntimeError("tmux trial command failed: " + args[0])
            return result.stdout.strip()
        try:
            run("new-session", "-d", "-s", "trial", "-x", "100", "-y", "30", "sleep 120")
            pid = int(run("display-message", "-p", "#{pid}"))
            for index in range(20):
                run("new-window", "-d", "-n", "destroy", "sleep 120")
                run("split-window", "-d", "-t", "trial:destroy", "sleep 120")
                # Resize queues the same asynchronous layout notifications;
                # kill-window follows in the same command queue while zoomed.
                run("resize-pane", "-Z", "-t", "trial:destroy", ";",
                    "kill-window", "-t", "trial:destroy")
            run("split-window", "-d", "-t", "trial", "sleep 120")
            run("resize-pane", "-Z", "-t", "trial", ";", "kill-server")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                cores()
                time.sleep(.05)
            report["trials"].append({"index": iteration, "zoomed_windows_destroyed": 21,
                                      "commands": calls, "server_pid": pid,
                                      "server_stopped": not Path(f"/proc/{pid}").exists()})
            if Path(f"/proc/{pid}").exists():
                raise RuntimeError("Owned tmux server survived teardown")
        except BaseException as exc:
            report.update(status="stopped_on_failure", error_type=type(exc).__name__)
            raise
        finally:
            # This socket is created and owned exclusively by this trial.
            subprocess.run([str(binary), "-S", str(socket), "kill-server"], capture_output=True, timeout=5)
            time.sleep(.2)
            report["cores"] = sorted(str(p) for p in set(CRASHES.glob("*")) - known)
            write(output, report)
        cores()
        if time.monotonic() - started > 120:
            raise RuntimeError("Teardown qualification deadline exceeded")
    report.update(status="pass", seconds=time.monotonic() - started)
    write(output, report)
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "trials"))
    parser.add_argument("--root", type=Path, default=Path.home() / ".cache/devbrain-runtime-080")
    args = parser.parse_args()
    (build if args.action == "build" else trials)(args.root)
