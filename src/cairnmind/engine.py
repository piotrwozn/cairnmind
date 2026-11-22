"""Pinned, checksum-verified ArcadeDB distribution and local Java process lifecycle."""
from __future__ import annotations

from dataclasses import replace
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import urlparse
import urllib.request

from . import process_identity
from .arcade import ArcadeClient, ArcadeError
from .config import Settings
from .first_start import (
    ENGINE_EXITED, ENGINE_NOT_INSTALLED, ENGINE_STARTED, ENGINE_STARTING, ENGINE_TIMED_OUT, JAVA_NEEDED)
from .database_mode import (address, connect_only, describe, engine_mode, is_managed_url, refusal,  # noqa: F401
                            server_version, supported_versions, version_problem)


def runtime_dir(settings: Settings) -> Path:
    return settings.data_dir / "runtime" / f"arcadedb-{settings.arcade_version}"


LOG_BYTES, LOG_COPIES, MAX_LOG_COPIES = 10 * 1024 * 1024, 3, 20


def stopped_engine_advice(error: str) -> str:
    """What `engine status` says for a transport failure: only a refused connection means "not running";
    a timeout or a broken answer may come from an engine that is starting or busy, so it keeps its cause."""
    cause = error.partition("(")[2].partition(")")[0]
    if cause == "ConnectError":
        return "Local ArcadeDB is not running; start it with `cairnmind engine start`."
    return (f"Local ArcadeDB is not reachable ({cause or 'unknown error'}); it may be starting or busy. "
            "If it is stopped, start it with `cairnmind engine start`.")


def rotate_log(path: Path, limit, copies) -> bool:
    """Move a log of at least ``limit`` bytes aside before a new run appends to it (#190); True when moved.

    ``path`` becomes ``path.1``, an earlier ``path.1`` becomes ``path.2`` and so on; ``copies`` old logs are
    kept and the oldest is deleted. With 0 copies the log is only emptied. Called only while the engine is not
    running (start_engine holds engine.lock and found no live engine), so nothing still writes to the file. A
    limit or count that is not a sensible whole number is replaced by the default, so a mistyped setting can
    neither stop the engine from starting nor let the log grow without end. A link is never followed."""
    limit = limit if type(limit) is int and limit > 0 else LOG_BYTES
    copies = copies if type(copies) is int and 0 <= copies <= MAX_LOG_COPIES else LOG_COPIES
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if info.st_size < limit and not path.is_symlink():
        return False
    numbered = [path.with_name(f"{path.name}.{number}") for number in range(1, max(copies, MAX_LOG_COPIES) + 1)]
    for old in numbered[max(copies, 1) - 1:]:
        old.unlink(missing_ok=True)  # the oldest kept copy's place, and any copy beyond the count
    # os.replace keeps a file's mode: an old copy written under a looser umask is made private too (#238).
    for kept in [path, *numbered[:copies - 1]] if copies else []:
        _make_private(kept)
    for number in range(copies - 1, 0, -1):
        if os.path.lexists(numbered[number - 1]):
            os.replace(numbered[number - 1], numbered[number])
    if copies:
        os.replace(path, numbered[0])
    else:
        path.unlink()
    return True


def _make_private(path: Path) -> None:
    """Mode 0600 for a regular file; a link (not followed), a missing file or anything else is left as it is.

    Best effort: a file the owner may not open or chmod (mode 0000, or owned by another user after a run under
    sudo) keeps its mode, because moving it needs only the folder and the size bound matters more (#238)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except (FileNotFoundError, PermissionError):
        return
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return
        raise
    try:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            os.fchmod(fd, 0o600)
    except PermissionError:
        pass
    finally:
        os.close(fd)


def log_settings_problems(settings: Settings) -> list[str]:
    """A line for each engine log setting that rotate_log replaces by its default, so it is not silent (#238)."""
    rules = (("engine_log_bytes", settings.engine_log_bytes, LOG_BYTES, "a whole number of bytes above 0",
              lambda value: type(value) is int and value > 0),
             ("engine_log_copies", settings.engine_log_copies, LOG_COPIES, f"a whole number from 0 to {MAX_LOG_COPIES}",
              lambda value: type(value) is int and 0 <= value <= MAX_LOG_COPIES))
    return [f"cairnmind: {name} in config.json is {repr(value)[:80]}, not {rule}; using {default}"
            for name, value, default, rule, valid in rules if not valid(value)]


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
    # On macOS JAVA_HOME before PATH (#232), as /usr/bin/java there does too. One without bin/java is passed over.
    # Linux keeps PATH only: a JAVA_HOME exported for another project must not replace a working Java 21.
    home = os.environ.get("JAVA_HOME", "") if sys.platform == "darwin" else ""
    if home and Path(home).is_absolute():
        candidate = Path(home) / "bin" / "java"
        if candidate.is_file() and os.access(candidate, os.X_OK):
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
        command = process_identity.command_line(pid) or []
        if ("com.arcadedb.server.ArcadeDBServer" not in command
                or f"-Darcadedb.server.rootPath={state}" not in command):
            return None
        return process_identity.executable(pid)
    except (OSError, ValueError, KeyError, TypeError):
        return None


class ConnectOnly(ArcadeError):
    """An engine command on an installation that only connects to its database (#68): nothing was done."""


def install_engine(settings: Settings, distribution: Path | None = None) -> Path:
    if connect_only(settings):
        raise ConnectOnly(refusal(settings, "install an engine"))
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


def engine_status(settings: Settings, ask_docker: bool = True) -> dict:
    """Whether the database server answers and which version it runs; starts and changes nothing.

    In connect-only mode (#68) nothing local is probed: no Java, no installed engine. The result says
    where the server is, who signs in and how, and ``problem`` when its version is not supported.
    """
    if connect_only(settings):
        from .database_credentials import transport_label
        result = {**describe(settings), "available": False, "url": address(settings),
                  "user": settings.arcade_user, "database": settings.arcade_database,
                  "transport": transport_label(settings), "expected_version": settings.arcade_version,
                  "supported_versions": list(supported_versions(settings))}
        if managed_java_executable(settings):
            result["local_engine"] = ("An engine this data directory started earlier is still running on this "
                                      "machine and is not used; `cairnmind engine stop` stops it.")
    elif engine_mode(settings) == "docker":  # a wrong value raises, naming it (#272)
        from .engine_docker import status_fields
        result = status_fields(settings, ask_docker)  # no Java is looked for
    else:
        result = {"mode": "managed", "engine": "native", "installed": (runtime_dir(settings) / "lib").is_dir(),
                  "available": False, "url": settings.arcade_url, "expected_version": settings.arcade_version,
                  "java": None, "managed_java": managed_java_executable(settings)}
        try:
            result["java"] = resolve_java(settings)
        except ArcadeError as exc:
            result["java_error"] = str(exc)
    try:
        client = ArcadeClient(settings)
        try:
            info = client.server_info()
        finally:
            client.close()
        refused = version_problem(settings, info)
        result.update(available=True, server=info, version=server_version(info), version_matches=refused is None)
        if refused and connect_only(settings):
            result["problem"] = refused
    except ArcadeError as exc:
        result["error"] = str(exc)
        if exc.status_code == 401:
            result["sign_in_refused"] = True  # an engine answers and does not accept this sign-in (#313)
        if not connect_only(settings) and result["error"].startswith("Local ArcadeDB is unavailable"):
            # The client's generic advice names this very command (#238).
            result["error"] = stopped_engine_advice(result["error"])
    except ValueError as exc:
        if not connect_only(settings):
            raise
        # A broken rule of #72: which one, without sending anything. The server was not asked, so whether it
        # answers is not known (None), and `engine status` is refused like every other command (#242).
        result.update(available=None, error=str(exc))
    return result


# The same 30 seconds that start_engine gives the server to come up: a JVM that cannot print its
# version in that time would not get the server up in it either.
JAVA_PROBE_TIMEOUT = 30


def probe_java(java: str) -> subprocess.CompletedProcess:
    """Run ``java -version``, twice at most, because the first start of a cold or busy JVM can be slow."""
    attempts = 2
    for attempt in range(attempts):
        try:
            return subprocess.run([java, "-version"], capture_output=True, text=True, timeout=JAVA_PROBE_TIMEOUT)
        except subprocess.TimeoutExpired:
            if attempt == attempts - 1:
                raise ArcadeError(f"Java did not answer {java} -version within {JAVA_PROBE_TIMEOUT} seconds, "
                                  f"{attempts} times. The machine may be busy; try again.") from None
        except OSError as exc:
            raise ArcadeError(f"Java could not be run ({java}): {exc.strerror or type(exc).__name__}") from None
    raise AssertionError("unreachable")


def java_major(java: str) -> int | None:
    """The major version `java -version` prints, or None when it fails or prints none."""
    probe = probe_java(java)
    found = re.search(r'version "(\d+)', probe.stderr + probe.stdout)
    return int(found[1]) if probe.returncode == 0 and found else None


def write_root_password(settings: Settings) -> Path:
    """arcade/root-password, mode 0600: read by the native engine (rootPasswordPath) and mounted into the
    container (#272)."""
    password_file = settings.data_dir / "arcade" / "root-password"
    fd = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(settings.arcade_password)
    os.chmod(password_file, 0o600)
    return password_file


def native_engine_pid(settings: Settings) -> int | None:
    """The PID of this data directory's native engine while it runs, None when none runs (#272).

    Fails closed: a record that cannot be read, or a live process whose command line cannot be read, raises."""
    from .engine_docker import NATIVE_UNKNOWN
    state = settings.data_dir / "arcade"
    try:
        pid = int(json.loads((state / "process.json").read_text())["pid"])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        raise ArcadeError(NATIVE_UNKNOWN.format(pid="unknown")) from None
    command = process_identity.command_line(pid)
    if command is None:
        if not process_identity.alive(pid):
            return None
        raise ArcadeError(NATIVE_UNKNOWN.format(pid=pid))
    joined = "\0".join(command)
    return pid if "com.arcadedb.server.ArcadeDBServer" in joined and str(state) in joined else None


def start_engine(settings: Settings) -> dict:
    # Any address but plain http on this machine is a server someone else runs, and so is the address
    # arcade_connect_url names: never started here, its password never written to this data directory.
    if connect_only(settings):
        raise ConnectOnly(refusal(settings, "start an engine"))
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (settings.data_dir / "engine.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        from .engine_docker import refuse_if_container_runs
        refuse_if_container_runs(settings)  # never two engines on one data directory (#272)
        existing = engine_status(settings)
        if existing.get("sign_in_refused"):
            raise ArcadeError(existing["error"])  # an engine answers on this port: never a second one (#313)
        if existing["available"]:
            if not existing.get("version_matches"):
                raise ArcadeError(f"ArcadeDB release does not match the pinned {settings.arcade_version}")
            return existing
        home = runtime_dir(settings)
        if not (home / "lib").is_dir():
            raise ArcadeError(ENGINE_NOT_INSTALLED)
        java = resolve_java(settings)
        from .platform_hints import hint
        if not java:  # what doctor says, with Docker as the other way (#313)
            raise ArcadeError(JAVA_NEEDED.format(found="not found", hint=hint("java")))
        major = java_major(java)
        if major is None or major < 21:
            found = f"Java {major}" if major else f"the version of {java} could not be read"
            raise ArcadeError(JAVA_NEEDED.format(found=found, hint=hint("java")))
        state = settings.data_dir / "arcade"
        state.mkdir(exist_ok=True, mode=0o700)
        for name in ("config", "databases", "backups", "log"):
            (state / name).mkdir(exist_ok=True, mode=0o700)
        for source in (home / "config").glob("*"):
            target = state / "config" / source.name
            if source.is_file() and not target.exists():
                shutil.copy2(source, target)
        password_file = write_root_password(settings)
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
        problems = log_settings_problems(settings)
        for line in problems:
            print(line, file=sys.stderr)
        note = "".join(line + "\n" for line in problems).encode()
        try:
            rotate_log(state / "engine.log", settings.engine_log_bytes, settings.engine_log_copies)
        except OSError as exc:
            # Rotation is housekeeping: when the filesystem refuses it, the engine still starts and appends.
            note += (f"cairnmind: engine.log was not rotated ({type(exc).__name__}: {exc.strerror or exc}); "
                     "appending to it\n").encode()
        # Private like the other files of the data directory, whatever the umask (#238); a link is not followed.
        fd = os.open(state / "engine.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "ab") as log:
            if note:
                log.write(note)
                log.flush()
            process = subprocess.Popen(command, cwd=state, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=log, start_new_session=True)
        pid_file = state / "process.json"
        pid_file.write_text(json.dumps({"pid": process.pid, "url": settings.arcade_url,
                                       "root": str(state), "java_executable": java,
                                       "started_at": time.time()}))
        for _ in range(120):
            if process.poll() is not None:
                pid_file.unlink(missing_ok=True)
                raise ArcadeError(ENGINE_EXITED.format(log=state / "engine.log", url=settings.arcade_url))
            status = engine_status(settings)
            if status["available"]:
                return status
            time.sleep(0.25)
        # Wait until the process is really gone: a second start right after must not meet a JVM that
        # is still shutting down on the same port and files. The record goes too, so that
        # stop_engine does not look for a process that no longer exists.
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        pid_file.unlink(missing_ok=True)
        raise ArcadeError(ENGINE_TIMED_OUT.format(log=state / "engine.log"))


def start_command(settings: Settings, *, docker: bool = False, native: bool = False, build: bool = False) -> dict:
    """`cairnmind engine start`: the stored mode, or the one --docker / --native names. The mode a flag names is
    saved only after that engine answers (#272)."""
    if connect_only(settings):
        raise ConnectOnly(refusal(settings, "start an engine"))
    from .engine_docker import MODE_SAVED, start_docker_engine, store_engine_mode
    mode = "docker" if docker else "native" if native else engine_mode(settings)
    # --docker names the image too: the published one, or with --build the one built here.
    chosen = replace(settings, engine_mode=mode, **({"engine_docker_build": build} if docker else {}))
    if mode == "docker":
        status = start_docker_engine(chosen, build=build, pull=True)
    else:
        status = start_engine(chosen)
    if (docker or native) and store_engine_mode(settings, mode, build=bool(docker and build)):
        print(f"cairnmind: {MODE_SAVED.format(mode=mode, config=settings.data_dir / 'config.json')}", file=sys.stderr)
    return status


def ensure_engine(settings: Settings) -> None:
    """The server answers with a supported version, or an error. Only a managed engine is ever started."""
    if connect_only(settings):
        # Never the local probes of engine_status, and never a start: one request, then the verdict.
        # ArcadeClient refuses a broken rule of #72 (plain http to another host, root, no password) first.
        client = ArcadeClient(settings)
        try:
            info = client.server_info()
        finally:
            client.close()
        refused = version_problem(settings, info)
        if refused:
            raise ArcadeError(refused)
        return
    docker = engine_mode(settings) == "docker"
    # An engine in Docker that answers needs no `docker container inspect` on every command.
    status = engine_status(settings, ask_docker=False) if docker else engine_status(settings)
    if status.get("sign_in_refused"):
        raise ArcadeError(status["error"])  # an engine answers on this port but refuses this data directory (#313)
    if status["available"] and not status.get("version_matches"):
        raise ArcadeError(f"ArcadeDB release does not match the pinned {settings.arcade_version}")
    if not status["available"]:
        # Said before it starts, and that it keeps running only once it answers: a command that starts the
        # engine in the background does not do it silently (#287).
        mode = "Docker" if docker else "native"
        print("cairnmind: " + ENGINE_STARTING.format(mode=mode, data_dir=settings.data_dir), file=sys.stderr)
        if docker:
            from .engine_docker import start_docker_engine
            start_docker_engine(settings)  # the stored image; never builds, never pulls
        else:
            start_engine(settings)
        print("cairnmind: " + ENGINE_STARTED.format(mode=mode), file=sys.stderr)


def own_engine_recorded(settings: Settings) -> bool:
    """Whether the engine on arcade_url is the one this data directory started: a verified process recorded for
    that address, or the recorded container in Docker mode. Anything that cannot be established is False, so a
    refusal is never blamed on this directory's own engine without proof (#313)."""
    if engine_mode(settings) == "docker":
        from .engine_docker import container_recorded
        return container_recorded(settings)
    try:
        recorded = json.loads((settings.data_dir / "arcade" / "process.json").read_text())
        return recorded.get("url") == settings.arcade_url and native_engine_pid(settings) is not None
    except (OSError, ValueError, AttributeError, ArcadeError):
        return False


def local_process_recorded(settings: Settings) -> bool:
    """Whether this data directory holds the record of an engine process it started (arcade/process.json)."""
    return (settings.data_dir / "arcade" / "process.json").exists()


def stop_engine(settings: Settings) -> dict:
    """Stop only the process launched for this data directory, after its service stops.

    Also in connect-only mode, where an engine started before the installation was switched may still
    run: it is this data directory's own process. With none, the command says that nothing is managed here.
    """
    from .engine_docker import container_recorded, stop_docker_engine
    if container_recorded(settings) or (not connect_only(settings) and engine_mode(settings) == "docker"):
        return stop_docker_engine(settings)
    state = settings.data_dir / "arcade"
    file = state / "process.json"
    if not local_process_recorded(settings):
        if connect_only(settings):
            raise ConnectOnly(refusal(settings, "stop an engine"))
        return {"stopping": False, "reason": "No managed process for this data directory"}
    info = json.loads(file.read_text())
    pid = int(info["pid"])
    # The command line proves the process is this directory's engine: an unrelated reused PID is never killed,
    # and one whose command line cannot be read is not either (#226). A process that is gone is already stopped.
    try:
        argv = process_identity.command_line(pid)
        if argv is None and not process_identity.alive(pid):
            raise ProcessLookupError
        command = "\0".join(argv or [])
        if "com.arcadedb.server.ArcadeDBServer" not in command or str(state) not in command:
            raise ArcadeError("Managed ArcadeDB PID no longer belongs to this environment")
        os.kill(pid, signal.SIGTERM)
        for _ in range(100):
            if not process_identity.alive(pid) or not engine_status(settings)["available"]:
                break
            time.sleep(0.1)
    except ProcessLookupError:
        pass
    file.unlink(missing_ok=True)
    return {"stopping": True}
