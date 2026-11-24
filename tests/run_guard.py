"""Two guards over every test of the run, loaded by conftest as a plugin (#236).

A time limit. A test that hangs fails after its limit, and the run goes on: setup, the test and its
teardown each get the limit; under xdist the wait for the test slot (conftest.run_slot) has its own
bound, and the limit starts again once the slot is held. SIGALRM raises the failure in the test's own
frame, which interrupts sleeps, waits for processes and flock. When that signal cannot act (blocked, or
the process is stuck in C), a backstop (faulthandler) prints every thread's stack and ends the process:
under xdist the worker, whose test is reported as crashed, and whose locks go with it.

The limit is CAIRNMIND_TEST_TIME_LIMIT seconds (0 turns both guards' timers off, for a debugger), or
@pytest.mark.time_limit(seconds) for one test; both are doubled while coverage measures the run.

Left running. After each test, what the test itself started and left running is stopped:
- an engine whose record (arcade/process.json) is in the test's tmp_path: cairnmind's stop_engine,
  which checks that the process is that directory's engine and refuses otherwise;
- a tmux server on a socket the test named with -S and which did not exist before the test used it;
- a child process the test started (subprocess, asyncio), and the process group it leads when it was
  started in a group of its own: SIGTERM, and SIGKILL after STOP_GRACE seconds. Only a child of this
  process that has not been reaped is signalled: its pid cannot belong to anyone else.
Then, by what the system lists (ps), every process of this user whose command line names a path under the
test's tmp_path is stopped whatever its parent or session, and the test fails by name (#457): a service the
product started for the test's data directory (a broker started by a panel) is nobody's child and the rest
cannot see it.
Processes that fixtures of a wider scope (module, session) start are theirs and are not touched; the
default tmux socket, the installation's data directory and its port are never looked at. What was
stopped is reported as a LeftRunning warning naming each process and socket.

Socket directories. At the end of the run (in the xdist controller, after the workers), the workspace and
native-run socket directories that appeared in the product's socket base during the run and hold no running
server are removed: on macOS that base is one per user (/private/tmp/cairnmind-UID), shared with the
installation, and the product leaves a directory behind when its server ends. What was there before the run,
and a directory whose server still answers, are kept.
"""
from __future__ import annotations

import contextlib
import faulthandler
import glob
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import warnings

import pytest

from cairnmind.config import env_value

# The slowest test of a full parallel run on 14 November took 71 s (an engine-bound test of
# test_ask_freshness) and the slowest setup 17 s; the limit leaves room for a CI runner several times slower.
DEFAULT_LIMIT = 300.0
# A process asked to stop with SIGTERM has this long before SIGKILL.
STOP_GRACE = 3.0
LIMIT_KEY = pytest.StashKey[float]()


class LeftRunning(UserWarning):
    """A test ended with processes or a tmux server of its own still running; they were stopped."""


def read_limit(value) -> float:
    """Seconds as a finite number >= 0 (0: no limit); anything else is a usage error."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = math.nan
    if not math.isfinite(seconds) or seconds < 0:
        raise pytest.UsageError(f"A test time limit is a number of seconds >= 0 (0: none), not {value!r}")
    return seconds


def measuring_coverage() -> bool:
    coverage = sys.modules.get("coverage")
    return bool(coverage and coverage.Coverage.current())


class Clock:
    def __init__(self):
        self.default = DEFAULT_LIMIT
        self.signals = False  # the SIGALRM handler is installed: main thread, and a limit is set
        self.limit = 0.0  # the current test's limit; 0 is none
        self.longest = 0.0  # the longest limit of a test this process runs
        self.what, self.seconds = "", 0.0
        self.stderr = None  # a descriptor of the real stderr, for the backstop
        self.previous = None

    def install(self):
        if self.default <= 0:
            return
        with contextlib.suppress(OSError, ValueError, AttributeError):
            self.stderr = os.dup(sys.stderr.fileno())
        if threading.current_thread() is threading.main_thread() and hasattr(signal, "SIGALRM"):
            self.previous = signal.signal(signal.SIGALRM, self.expired)
            self.signals = True

    def uninstall(self):
        self.stop()
        if self.signals:
            signal.signal(signal.SIGALRM, self.previous or signal.SIG_DFL)
            self.signals = False
        if self.stderr is not None:
            os.close(self.stderr)
            self.stderr = None

    def restart(self, seconds: float, what: str):
        self.what, self.seconds = what, seconds
        if seconds <= 0:
            self.stop()
            return
        if self.signals:
            signal.setitimer(signal.ITIMER_REAL, seconds)
        if self.stderr is not None:
            faulthandler.dump_traceback_later(seconds + max(self.limit, 1.0), exit=True, file=self.stderr)

    def stop(self):
        if self.signals:
            signal.setitimer(signal.ITIMER_REAL, 0)
        if self.stderr is not None:
            faulthandler.cancel_dump_traceback_later()

    def expired(self, signum, frame):
        pytest.fail(f"Time limit: {self.what} took longer than {self.seconds:g} s and was stopped (#236). "
                    "A test that needs longer says so with @pytest.mark.time_limit(seconds).")


CLOCK = Clock()


@contextlib.contextmanager
def waiting(seconds: float, what: str):
    """Bound a wait by its own limit; afterwards the current test's limit starts again from now."""
    CLOCK.restart(seconds if CLOCK.limit > 0 else 0, what)
    try:
        yield
    finally:
        CLOCK.restart(CLOCK.limit, "the test")


def slot_wait() -> float:
    """The bound of a wait for the test slot under xdist. An exclusive test waits for the tests that run
    (each within its limit), then runs within its own; a test behind it at the gate waits for both."""
    return 3 * max(CLOCK.longest * (2 if measuring_coverage() else 1), CLOCK.limit)


def tmux_socket(args, cwd) -> tuple[str, str] | None:
    """(binary, absolute socket path) of a tmux command that names its socket with -S, else None."""
    if isinstance(args, (str, bytes, os.PathLike)) or not args:
        return None
    args = [os.fsdecode(arg) if isinstance(arg, (bytes, os.PathLike)) else arg for arg in args]
    if not all(isinstance(arg, str) for arg in args) or Path(args[0]).name != "tmux":
        return None
    position = 1
    while position < len(args) and args[position].startswith("-") and args[position] != "--":
        flag = args[position]
        if flag in ("-S", "-L", "-f", "-c", "-T") and position + 1 < len(args):
            if flag == "-S":
                path = Path(args[position + 1])
                if not path.is_absolute():
                    path = Path(os.fsdecode(cwd) if cwd else os.getcwd()) / path
                return args[0], os.path.abspath(path)
            position += 2
        else:
            position += 1
    return None


def default_tmux_socket(path: str) -> bool:
    """tmux's own socket directory (tmux-UID under TMUX_TMPDIR or /tmp): never the test's."""
    return Path(path).parent.name == f"tmux-{os.getuid()}"


class Spawned:
    """What the running test started: child processes, and tmux sockets it was first to use."""

    def __init__(self):
        self.phase = None  # "setup" or "call" while the test's own work runs
        self.scopes: list[str] = []  # scopes of the fixtures being set up, innermost last
        self.processes: list[tuple[subprocess.Popen, bool]] = []
        self.sockets: dict[str, str] = {}
        self.lock = threading.Lock()
        self.original_init = None

    def recording(self) -> bool:
        if self.scopes:
            return self.scopes[-1] == "function" and self.phase in ("setup", "call")
        return self.phase == "call"

    def clear(self):
        with self.lock:
            self.processes, self.sockets = [], {}

    def install(self):
        if self.original_init is not None:
            return
        original = self.original_init = subprocess.Popen.__init__
        spawned = self

        def __init__(self, *args, **kwargs):
            socket = None
            if spawned.recording():
                with contextlib.suppress(Exception):
                    socket = tmux_socket(args[0] if args else kwargs.get("args"), kwargs.get("cwd"))
                if socket and (os.path.exists(socket[1]) or default_tmux_socket(socket[1])):
                    socket = None  # a server the test did not start
            original(self, *args, **kwargs)
            if not spawned.recording():
                return
            own_group = bool(kwargs.get("start_new_session")) or kwargs.get("process_group") == 0
            with spawned.lock:
                spawned.processes.append((self, own_group))
                if socket:
                    spawned.sockets.setdefault(socket[1], socket[0])

        subprocess.Popen.__init__ = __init__

    def uninstall(self):
        if self.original_init is not None:
            subprocess.Popen.__init__ = self.original_init
            self.original_init = None

    def stop_all(self, node) -> list[str]:
        with self.lock:
            processes, sockets = list(self.processes), dict(self.sockets)
            self.processes, self.sockets = [], {}
        stopped = stop_engines(getattr(node, "funcargs", None) or {})
        stopped += stop_tmux_servers(sockets)
        stopped += stop_processes(processes)
        return stopped


SPAWNED = Spawned()


def child_state(process: subprocess.Popen) -> str:
    """"alive" while `process` is an unreaped child of this process; "exited" when it was just reaped
    here; "gone" when it was reaped before, by anyone, and its pid may now belong to someone else."""
    if process.returncode is not None:
        return "gone"
    try:
        pid, status = os.waitpid(process.pid, os.WNOHANG)
    except ChildProcessError:
        return "gone"
    if pid == 0:
        return "alive"
    process.returncode = os.waitstatus_to_exitcode(status)
    return "exited"


def signal_child(process: subprocess.Popen, own_group: bool, number: int) -> None:
    """Signal a child (and its own group) that child_state has just found unreaped."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        if own_group:
            os.killpg(process.pid, number)
        else:
            os.kill(process.pid, number)


def kill_group_of_exited(process: subprocess.Popen, own_group: bool) -> None:
    # Reaped by child_state an instant ago: what is left of its group is the test's too.
    if own_group:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)


def describe(process: subprocess.Popen) -> str:
    args = process.args if isinstance(process.args, (list, tuple)) else [process.args]
    text = " ".join(os.fsdecode(arg) if isinstance(arg, (bytes, os.PathLike)) else str(arg) for arg in args)
    return f"process {process.pid} ({text[:160]})"


def stop_processes(processes) -> list[str]:
    stopped, running = [], []
    for process, own_group in processes:
        state = child_state(process)
        if state == "alive":
            signal_child(process, own_group, signal.SIGTERM)
            running.append((process, own_group))
            stopped.append(describe(process))
        elif state == "exited":
            kill_group_of_exited(process, own_group)
    deadline = time.monotonic() + STOP_GRACE
    while running:
        for process, own_group in list(running):
            if child_state(process) != "alive":
                kill_group_of_exited(process, own_group)
                running.remove((process, own_group))
        if not running or time.monotonic() >= deadline:
            break
        time.sleep(.05)
    for process, own_group in running:
        if child_state(process) == "alive":
            signal_child(process, own_group, signal.SIGKILL)
        else:
            kill_group_of_exited(process, own_group)
    deadline = time.monotonic() + 5
    while running and time.monotonic() < deadline:
        running = [(process, own_group) for process, own_group in running if child_state(process) == "alive"]
        if running:
            time.sleep(.05)
    stopped += [describe(process) + " did not end even after SIGKILL" for process, _ in running]
    return stopped


def stop_tmux_servers(sockets: dict[str, str]) -> list[str]:
    stopped = []
    environment = {key: value for key, value in os.environ.items() if key not in ("TMUX", "TMUX_PANE")}
    for socket, binary in sockets.items():
        if not os.path.exists(socket) or default_tmux_socket(socket):
            continue
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            ended = subprocess.run([binary, "-S", socket, "kill-server"], capture_output=True, timeout=5,
                                   env=environment, stdin=subprocess.DEVNULL)
            if ended.returncode == 0:
                stopped.append(f"tmux server on {socket}")
    return stopped


def engine_records(root: Path) -> list[Path]:
    """arcade/process.json files up to three directories below the test's tmp_path."""
    found = []
    for depth in range(4):
        found += root.glob("*/" * depth + "arcade/process.json")
    return found


def stop_engines(funcargs) -> list[str]:
    root = funcargs.get("tmp_path")
    if not isinstance(root, Path) or not root.is_dir():
        return []
    stopped = []
    for record in engine_records(root):
        data_dir = record.parent.parent
        try:
            pid = int(json.loads(record.read_text())["pid"])
            # A record a test wrote by hand may name any process, this one too: never stopped here.
            if pid <= 0 or pid == os.getpid() or not data_dir.resolve().is_relative_to(root.resolve()):
                continue
            os.kill(pid, 0)
        except (OSError, ValueError, KeyError, TypeError):
            continue  # no such process (or not ours): nothing is left running
        from cairnmind.config import Settings
        from cairnmind.engine import stop_engine
        try:
            stop_engine(Settings.load(data_dir))
            stopped.append(f"engine {pid} of {data_dir}")
        except Exception as exc:  # the identity was not established: refused, not killed
            stopped.append(f"engine {pid} of {data_dir}, NOT stopped: {type(exc).__name__}: {exc}")
    return stopped


def processes_under(root: Path) -> list[tuple[int, str]]:
    """(pid, command line) of every process of this user, other than this one and its parent, whose command
    line names `root` or a path below it, whatever its parent or session: a service the product started
    for the test's data directory is nobody's child (#457)."""
    names = {str(root), str(root.resolve())}
    pattern = re.compile("(?:" + "|".join(re.escape(name) for name in names) + r")(?=[/\s]|$)")
    try:
        listing = subprocess.run(["ps", "-U", str(os.getuid()), "-ww", "-o", "pid=,args="], capture_output=True,
                                 text=True, timeout=10, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in listing.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit() and int(pid) not in (os.getpid(), os.getppid()) and pattern.search(args):
            found.append((int(pid), args.strip()))
    return found


def stop_strays(funcargs) -> list[str]:
    """Stop what still runs with the test's tmp_path in its command line, after everything else was stopped."""
    root = funcargs.get("tmp_path")
    if not isinstance(root, Path) or not root.is_dir():
        return []
    found = processes_under(root)
    for number in (signal.SIGTERM, signal.SIGKILL):
        for pid, _ in found:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, number)
        deadline = time.monotonic() + (STOP_GRACE if number == signal.SIGTERM else 0)
        while time.monotonic() < deadline and {pid for pid, _ in processes_under(root)} & {pid for pid, _ in found}:
            time.sleep(.05)
    return [f"process {pid} ({args[:160]})" for pid, args in found]


def socket_directories() -> set[str]:
    """The workspace and native-run socket directories now in the product's socket base (#235)."""
    from cairnmind.runtime_sockets import KINDS, socket_directory
    return {path for kind in KINDS for path in glob.glob(str(socket_directory(kind, "[0-9a-f]" * 16)))}


def listening(path: str) -> bool:
    """Whether a server answers on this socket; when that cannot be told, it is taken to answer."""
    import socket  # here: this module names tmux sockets `socket`
    with socket.socket(socket.AF_UNIX) as probe:
        try:
            probe.connect(path)
        except (FileNotFoundError, ConnectionRefusedError):
            return False
        except OSError:
            return True
    return True


def remove_socket_directories(before: set[str]) -> list[str]:
    """Remove the socket directories that are not in `before`, belong to this user, are not links, have not
    changed for SOCKET_DIRECTORY_SETTLE seconds and hold no server that answers. The age keeps a directory
    that another run on this machine has just made and whose server does not listen yet. shortcut: such a
    directory older than that, or one this run left in its last seconds, is still misjudged; a per-run socket
    base in the product would end that."""
    removed = []
    for path in sorted(socket_directories() - before):
        with contextlib.suppress(OSError):
            info = os.lstat(path)
            if (stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                    and time.time() - info.st_mtime >= SOCKET_DIRECTORY_SETTLE
                    and not listening(os.path.join(path, "tmux.sock"))):
                shutil.rmtree(path)
                removed.append(path)
    return removed


SOCKET_DIRECTORY_SETTLE = 10.0  # seconds without change before a left socket directory is taken as left
SOCKETS_BEFORE: set[str] | None = None  # in the process that ends the run: the directories there before it


def pytest_configure(config):
    global SOCKETS_BEFORE
    config.addinivalue_line("markers", "time_limit(seconds): this test's own time limit (#236); 0 is none")
    CLOCK.default = read_limit(env_value("TEST_TIME_LIMIT") or DEFAULT_LIMIT)  # empty: the default
    CLOCK.install()
    SPAWNED.install()
    # A serial run or the xdist controller, and not a run a test started (pytest names the running test there).
    if not hasattr(config, "workerinput") and "PYTEST_CURRENT_TEST" not in os.environ:
        SOCKETS_BEFORE = socket_directories()


def pytest_unconfigure(config):
    SPAWNED.uninstall()
    CLOCK.uninstall()
    if SOCKETS_BEFORE is not None:
        remove_socket_directories(SOCKETS_BEFORE)


def pytest_collection_finish(session):
    longest = 0.0
    for item in session.items:
        marker = item.get_closest_marker("time_limit")
        seconds = CLOCK.default if marker is None or CLOCK.default <= 0 else read_limit(
            marker.args[0] if marker.args else marker.kwargs.get("seconds"))
        item.stash[LIMIT_KEY] = seconds
        longest = max(longest, seconds)
    CLOCK.longest = longest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    seconds = item.stash.get(LIMIT_KEY, CLOCK.default)
    CLOCK.limit = seconds * 2 if seconds > 0 and measuring_coverage() else seconds
    SPAWNED.clear()
    CLOCK.restart(CLOCK.limit, "the test")
    try:
        yield
    finally:
        CLOCK.stop()
        SPAWNED.clear()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    SPAWNED.phase = "setup"
    try:
        yield
    finally:
        SPAWNED.phase = None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    SPAWNED.phase = "call"
    try:
        yield
    finally:
        SPAWNED.phase = None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    CLOCK.restart(CLOCK.limit, "the teardown of the test")
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    SPAWNED.scopes.append(fixturedef.scope)
    try:
        yield
    finally:
        SPAWNED.scopes.pop()


@pytest.fixture(autouse=True)
def leftover_processes_are_stopped(request):
    """Set up before every other fixture of the test, so torn down after them: what is still running
    then was left behind."""
    yield
    # Its own bound, which each step keeps to (STOP_GRACE, then a few seconds per kind), so that a short
    # limit does not cut it off; what is torn down after it (fixtures of a wider scope) has it too.
    CLOCK.restart(max(CLOCK.limit, 120.0) if CLOCK.limit > 0 else 0, "stopping what the test left running")
    stopped = SPAWNED.stop_all(request.node)
    if stopped:
        warnings.warn(LeftRunning("The test left running, and it was stopped: " + "; ".join(stopped)))
    strays = stop_strays(request.node.funcargs)
    if strays:
        pytest.fail("The test left a process of its own data directory running, with no parent to stop it "
                    "(stopped now): " + "; ".join(strays), pytrace=False)
