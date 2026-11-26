"""Record a request of the test engine that has no answer yet, while it is still pending (#221).

A stalled request once showed an idle engine in a thread dump taken after the client gave up, which cannot tell
a slow request from a lost one. When a request has waited STALL_AFTER seconds (well under the client's 30 s),
a timer thread of the test process writes what the request was, the sockets on the engine's port with their
queues, the end of engine.log and the engine's threads, without touching the request. Unread bytes on the
server's side mean the engine never read the request; on the client's side, that the answer was never read;
both empty with idle workers, that a handler never answered. A request answered late is reported as slow.
The connection is not named (httpx hides it): the header gives this worker's pid, and the request's row is the
one of that pid in the socket table (ss shows `pid=`) whose lastsnd is about STALL_AFTER seconds, if several.
Statements are printed without values (see statement()).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
import warnings

from cairnmind import file_locks, process_identity

STALL_AFTER = 20.0
DUMP_EVERY = 10.0  # at most one thread dump in this many seconds for all workers sharing the engine
LOG_LINES = 100


def engine_threads(log: Path, wait: float = 1.5) -> str:
    """What the test engine's Java threads are doing right now, for the stall report (#221).

    On POSIX, SIGQUIT makes a JVM print its threads to its output, which is engine.log, and go on running.
    On Windows, jcmd <pid> Thread.print (from the JDK) prints the same JVM thread dump without killing it.
    Only run against a process whose command line is the ArcadeDB server.
    """
    pid = int(json.loads((log.parent / "process.json").read_text())["pid"])
    cmdline = process_identity.command_line(pid) or []
    if not any("arcadedb" in part.lower() for part in cmdline):
        return f"(none taken: process {pid} is not the engine)"
    if sys.platform == "win32":
        jcmd = None
        if cmdline:
            candidate = Path(cmdline[0]).with_name("jcmd.exe")
            if candidate.is_file():
                jcmd = str(candidate)
        jcmd = jcmd or shutil.which("jcmd")
        if not jcmd:
            return "(not captured on Windows: jcmd not found)"
        try:
            done = subprocess.run([jcmd, str(pid), "Thread.print"], capture_output=True, text=True, timeout=3)
            out = (done.stdout + done.stderr)[:80_000]
            return out if done.returncode == 0 and out.strip() else "(not captured on Windows)"
        except (OSError, subprocess.SubprocessError):
            return "(not captured on Windows)"
    before = log.stat().st_size
    os.kill(pid, signal.SIGQUIT)
    time.sleep(wait)
    with log.open("rb") as handle:
        handle.seek(before)
        return handle.read(80_000).decode(errors="replace")


def thread_dump(log: Path) -> str:
    """The engine's threads, taken once per DUMP_EVERY seconds across the workers (a file lock and a time stamp)."""
    with (log.parent / "stall-dump.stamp").open("a+") as handle:
        try:
            file_locks.lock_exclusive(handle, blocking=False)
        except BlockingIOError:
            return "(not taken: another worker is taking one now, it is in engine.log)"
        try:
            handle.seek(0)
            ago = time.time() - float(handle.read() or 0)
            if ago < DUMP_EVERY:
                return f"(not taken: another worker took one {ago:.0f}s ago, it is in engine.log)"
            handle.seek(0)
            handle.truncate()
            handle.write(str(time.time()))
            handle.flush()
            return engine_threads(log)
        finally:
            file_locks.unlock(handle)


def sockets(port: int) -> str:
    """Both ends of every connection on the port, with the receive and send queues (Linux: ss, else /proc)."""
    if sys.platform == "darwin":
        return "(not captured on macOS)"
    if sys.platform == "win32":
        return "(not captured on Windows)"
    if shutil.which("ss"):
        done = subprocess.run(["ss", "-tnpi", f"( sport = :{port} or dport = :{port} )"],
                              capture_output=True, text=True, timeout=5)
        return done.stdout + done.stderr
    lines = []  # tx_queue:rx_queue are hex byte counts, st 01 is ESTABLISHED
    for name in ("tcp", "tcp6"):
        try:
            rows = Path(f"/proc/net/{name}").read_text().splitlines()
        except OSError:
            continue
        lines += [f"/proc/net/{name}: {row}" for row in rows[:1] + [r for r in rows if f":{port:04X} " in r]]
    return "\n".join(lines) or "(no ss and no /proc/net/tcp: not captured)"


def log_end(log: Path) -> str:
    if not log.exists():
        return "none"
    with log.open("rb") as handle:
        handle.seek(max(0, log.stat().st_size - 65_536))
        tail = handle.read().decode(errors="replace").splitlines()[-LOG_LINES:]
    return f"last line written {time.time() - log.stat().st_mtime:.0f}s ago, ends:\n" + "\n".join(tail)


def statement(command, path: str) -> str:
    """The statement without values: some carry them in the text ('create user {...password...}', 'CONTENT {...}')."""
    if path == "/api/v1/server":  # administration commands: the verb and its object only
        return " ".join(str(command).split()[:2])
    return re.sub(r"'[^']*'|\"[^\"]*\"", "?", str(command).split(" CONTENT ")[0])[:500]


def capture(settings, client, method, path, kwargs, started) -> str:
    log = settings.data_dir / "arcade" / "engine.log"
    command = statement((kwargs.get("json") or {}).get("command", ""), path)
    session = f"HTTP transaction session {client.session_id}" if client.session_id else "no HTTP transaction session"
    parts = [f"ENGINE REQUEST PENDING {time.monotonic() - started:.0f}s: {method} {path} {command!r}, {session}, worker pid {os.getpid()}"]
    port = urlparse(settings.arcade_url).port
    for title, take in ((f"sockets on port {port}", lambda: sockets(port)), ("engine.log", lambda: log_end(log)),
                        ("thread dump", lambda: thread_dump(log))):
        try:  # a sick engine may fail the report too: nothing here may replace the request's own outcome
            parts.append(f"{title}: {take()}")
        except Exception as problem:
            parts.append(f"{title}: not captured ({problem!r})")
    return "\n".join(parts)


def watch(settings, original, after: float = STALL_AFTER):
    """Wrap ArcadeClient._request: a timer per request, cancelled and joined when it ends, so no thread outlives it."""
    def request(client, method, path, **kwargs):
        record = []
        started = time.monotonic()

        def fire():
            record.append(capture(settings, client, method, path, kwargs, started))
            print(record[0], file=sys.stderr, flush=True)  # the failing test's captured stderr reaches the CI log

        timer = threading.Timer(after, fire)
        timer.daemon = True
        timer.start()
        try:
            response = original(client, method, path, **kwargs)
        finally:
            timer.cancel()
            timer.join()
        if record:  # a passing test shows no stderr: a warning reaches the summary
            warnings.warn(f"SLOW REQUEST, answered after {time.monotonic() - started:.0f}s, not lost:\n{record[0]}")
        return response
    return request
