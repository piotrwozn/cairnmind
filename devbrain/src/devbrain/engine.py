"""Pinned, checksum-verified ArcadeDB distribution and local Java process lifecycle."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
from urllib.parse import urlparse
import urllib.request

from .arcade import ArcadeClient, ArcadeError
from .config import Settings


def runtime_dir(settings: Settings) -> Path:
    return settings.data_dir / "runtime" / f"arcadedb-{settings.arcade_version}"


def resolve_java(settings: Settings) -> str | None:
    """Resolve one executable, never silently replace an explicit runtime."""
    configured = settings.java_executable
    if not isinstance(configured, str):
        raise ArcadeError("java_executable must be an absolute executable path")
    if configured:
        if not Path(configured).is_absolute():
            raise ArcadeError("java_executable must be an absolute executable path")
        candidate = Path(configured)
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise ArcadeError("Configured java_executable is unavailable or not executable")
        return str(candidate.resolve())
    candidate = shutil.which("java")
    return str(Path(candidate).resolve()) if candidate else None


def managed_java_executable(settings: Settings) -> str | None:
    """Observe the managed process, rather than infer its binary from today's PATH."""
    state = settings.data_dir / "arcade"
    try:
        record = json.loads((state / "process.json").read_text())
        pid = int(record["pid"])
        if pid <= 0:
            return None
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        if (b"com.arcadedb.server.ArcadeDBServer" not in command
                or f"-Darcadedb.server.rootPath={state}".encode() not in command):
            return None
        return str(Path(f"/proc/{pid}/exe").resolve(strict=True))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def install_engine(settings: Settings, distribution: Path | None = None) -> Path:
    destination = runtime_dir(settings)
    if (destination / "lib").is_dir():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (destination.parent / "install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (destination / "lib").is_dir():
            return destination
        if distribution is not None:
            distribution = distribution.resolve(strict=True)
            if not (distribution / "lib").is_dir():
                raise ValueError("ArcadeDB distribution must contain lib/")
            shutil.copytree(distribution, destination)
            return destination
        version = settings.arcade_version
        if not version.replace(".", "").isdigit():
            raise ValueError("Invalid ArcadeDB release version")
        name = f"arcadedb-{version}-base.tar.gz"
        base = f"https://github.com/ArcadeData/arcadedb/releases/download/{version}/"
        with tempfile.TemporaryDirectory(prefix="arcade-install-", dir=destination.parent) as temp:
            temp = Path(temp)
            archive = temp / name
            # Keep the environment's proxy and CA trust for release downloads.
            with urllib.request.urlopen(base + name + ".sha256", timeout=30) as response:
                expected = response.read().decode().strip().split()[0]
            digest = hashlib.sha256()
            with urllib.request.urlopen(base + name, timeout=120) as response, archive.open("wb") as handle:
                while chunk := response.read(1024 * 1024):
                    digest.update(chunk)
                    handle.write(chunk)
            if digest.hexdigest() != expected:
                raise ArcadeError("ArcadeDB distribution checksum mismatch; installation was not applied")
            with tarfile.open(archive) as handle:
                handle.extractall(temp / "unpacked", filter="data")
            extracted = temp / "unpacked" / f"arcadedb-{version}"
            if not (extracted / "lib").is_dir():
                raise ArcadeError("ArcadeDB release has no server libraries")
            extracted.replace(destination)
    return destination


def engine_status(settings: Settings) -> dict:
    result = {"installed": (runtime_dir(settings) / "lib").is_dir(), "available": False,
              "url": settings.arcade_url, "expected_version": settings.arcade_version,
              "java": None, "managed_java": managed_java_executable(settings)}
    try:
        result["java"] = resolve_java(settings)
    except ArcadeError as exc:
        result["java_error"] = str(exc)
    try:
        info = ArcadeClient(settings).server_info()
        actual_version = str(info.get("version", "")).split(" ", 1)[0]
        result.update(available=True, server=info, version_matches=actual_version == settings.arcade_version)
    except ArcadeError as exc:
        result["error"] = str(exc)
    return result


def start_engine(settings: Settings) -> dict:
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (settings.data_dir / "engine.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = engine_status(settings)
        if existing["available"]:
            if not existing.get("version_matches"):
                raise ArcadeError(f"ArcadeDB release does not match the pinned {settings.arcade_version}")
            return existing
        home = runtime_dir(settings)
        if not (home / "lib").is_dir():
            raise ArcadeError("ArcadeDB is not installed. Run devbrain engine install first.")
        java = resolve_java(settings)
        if not java:
            raise ArcadeError("Install Java 21 or newer in WSL (openjdk-21-jre-headless)")
        probe = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=5)
        import re
        version = re.search(r'version "(\d+)', probe.stderr + probe.stdout)
        if probe.returncode != 0 or not version or int(version[1]) < 21:
            raise ArcadeError("ArcadeDB requires Java 21 or newer")
        state = settings.data_dir / "arcade"
        state.mkdir(exist_ok=True, mode=0o700)
        for name in ("config", "databases", "backups", "log"):
            (state / name).mkdir(exist_ok=True, mode=0o700)
        for source in (home / "config").glob("*"):
            target = state / "config" / source.name
            if source.is_file() and not target.exists():
                shutil.copy2(source, target)
        password_file = state / "root-password"
        fd = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(settings.arcade_password)
        os.chmod(password_file, 0o600)
        parsed = urlparse(settings.arcade_url)
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.scheme != "http":
            raise ValueError("ArcadeDB must bind locally")
        port = parsed.port or 2480
        command = [java, "-Xms64m", "-Xmx1024m", "--add-modules=jdk.incubator.vector",
                   "--enable-native-access=ALL-UNNAMED", "-Djava.awt.headless=true", "-Dfile.encoding=UTF8",
                   "-Darcadedb.profile=low-ram", f"-Darcadedb.server.rootPath={state}",
                   f"-Darcadedb.server.configDirectory={state / 'config'}",
                   f"-Darcadedb.server.databaseDirectory={state / 'databases'}",
                   f"-Darcadedb.server.backupDirectory={state / 'backups'}",
                   f"-Darcadedb.server.logsDirectory={state / 'log'}",
                   f"-Darcadedb.server.rootPasswordPath={password_file}",
                   f"-Darcadedb.server.httpIncomingHost={parsed.hostname if parsed.hostname != 'localhost' else '127.0.0.1'}",
                   f"-Darcadedb.server.httpIncomingPort={port}",
                   f"-Djava.util.logging.config.file={state / 'config' / 'arcadedb-log.properties'}",
                   "-cp", str(home / "lib" / "*"), "com.arcadedb.server.ArcadeDBServer"]
        with (state / "engine.log").open("ab") as log:
            process = subprocess.Popen(command, cwd=state, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=log, start_new_session=True)
        pid_file = state / "process.json"
        pid_file.write_text(json.dumps({"pid": process.pid, "url": settings.arcade_url,
                                       "root": str(state), "java_executable": java,
                                       "started_at": time.time()}))
        for _ in range(120):
            if process.poll() is not None:
                raise ArcadeError(f"ArcadeDB exited during startup. See {state / 'engine.log'}")
            status = engine_status(settings)
            if status["available"]:
                return status
            time.sleep(0.25)
        process.terminate()
        raise ArcadeError(f"ArcadeDB startup timed out. See {state / 'engine.log'}")


def ensure_engine(settings: Settings) -> None:
    status = engine_status(settings)
    if status["available"] and not status.get("version_matches"):
        raise ArcadeError(f"ArcadeDB release does not match the pinned {settings.arcade_version}")
    if not status["available"]:
        start_engine(settings)


def stop_engine(settings: Settings) -> dict:
    """Stop only the process launched for this data directory, after its service stops."""
    state = settings.data_dir / "arcade"
    file = state / "process.json"
    if not file.exists():
        return {"stopping": False, "reason": "No managed process for this data directory"}
    info = json.loads(file.read_text())
    pid = int(info["pid"])
    # Linux/WSL /proc identity check prevents killing an unrelated reused PID.
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes()
        if b"com.arcadedb.server.ArcadeDBServer" not in command or str(state).encode() not in command:
            raise ArcadeError("Managed ArcadeDB PID no longer belongs to this environment")
        os.kill(pid, signal.SIGTERM)
        for _ in range(100):
            if not Path(f"/proc/{pid}").exists() or not engine_status(settings)["available"]:
                break
            time.sleep(0.1)
    except ProcessLookupError:
        pass
    file.unlink(missing_ok=True)
    return {"stopping": True}
