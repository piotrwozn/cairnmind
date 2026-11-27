"""Who a process is, on Linux through procfs and on macOS through sysctl (#226).

macOS has no /proc. Its path is selected here on Linux: the kernel's answers are replaced by records laid out
as sysctl(3) returns them, and the parsing and every guard above it run for real. A stop of a process whose id
now names another process is refused on both paths; when the identity cannot be read, nothing is signalled.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from cairnmind.terminal import cockpit
from cairnmind.storage import engine
from cairnmind.pal import process_identity
from cairnmind.memory import verification
from cairnmind.agents.agent_tools import ToolExecutor
from cairnmind.storage.arcade import ArcadeError
from cairnmind.config import Settings
from cairnmind.mcp.mcp_host import McpServer

linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="procfs is read on Linux only")
RUNNING, ZOMBIE = 2, 5  # SRUN, SZOMB of <sys/proc.h>


def kinfo(pid, seconds, micros=0, status=RUNNING):
    """One struct kinfo_proc as KERN_PROC_PID returns it: p_starttime, p_stat and p_pid filled in."""
    record = bytearray(648)
    struct.pack_into("=qi", record, 0, seconds, micros)
    record[36] = status
    struct.pack_into("=i", record, 40, pid)
    return bytes(record)


def procargs(argv, path="/usr/bin/program"):
    """A KERN_PROCARGS2 buffer: argc, the executable path, padding, the arguments, then the environment."""
    return (struct.pack("=i", len(argv)) + path.encode() + b"\0\0\0\0"
            + b"".join(argument.encode() + b"\0" for argument in argv) + b"HOME=/Users/someone\0")


class Mac:
    """The kernel of a Mac as process_identity asks it; pids not in the table do not exist."""

    def __init__(self, monkeypatch):
        self.births, self.argv, self.groups = {}, {}, {}
        monkeypatch.setattr(process_identity, "_platform", lambda: "darwin")
        monkeypatch.setattr(process_identity, "_darwin_kinfo",
                            lambda pid: kinfo(pid, *self.births[pid]) if pid in self.births else b"")
        monkeypatch.setattr(process_identity, "_darwin_procargs",
                            lambda pid: procargs(self.argv[pid]) if pid in self.argv else None)
        monkeypatch.setattr(process_identity, "_darwin_path",
                            lambda pid: "/Library/Java/bin/java" if pid in self.argv else None)
        monkeypatch.setattr(process_identity, "_darwin_group", lambda pgid: self.groups.get(pgid))


@pytest.fixture
def mac(monkeypatch):
    return Mac(monkeypatch)


@pytest.fixture
def sleeper():
    if sys.platform == "win32":
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    else:
        process = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        yield process
    finally:
        process.kill()
        process.wait()


def ended(process, seconds=5):
    try:
        process.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        return False
    return True


# --- Linux, for real -----------------------------------------------------------------------------------

@linux
def test_linux_reads_the_birth_command_line_and_program_of_a_process(sleeper):
    raw = Path(f"/proc/{sleeper.pid}/stat").read_text(errors="replace")
    assert process_identity.start(sleeper.pid) == raw.rsplit(")", 1)[1].split()[19]
    assert process_identity.start(sleeper.pid, running=True) == process_identity.start(sleeper.pid)
    assert process_identity.command_line(sleeper.pid) == ["sleep", "30"]
    assert process_identity.executable(sleeper.pid) == str(Path(subprocess.check_output(
        ["sh", "-c", "command -v sleep"], text=True).strip()).resolve())
    assert process_identity.alive(sleeper.pid)
    assert process_identity.group_running(sleeper.pid) is True


@linux
def test_linux_a_zombie_has_a_birth_but_does_not_run():
    process = subprocess.Popen(["true"], start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while process_identity.start(process.pid, running=True) and time.monotonic() < deadline:
            time.sleep(.01)
        assert process_identity.start(process.pid) and process_identity.start(process.pid, running=True) is None
        assert process_identity.alive(process.pid)
        assert process_identity.group_running(process.pid) is False
    finally:
        process.wait()
    assert process_identity.start(process.pid) is None and process_identity.command_line(process.pid) is None
    assert not process_identity.alive(process.pid)


@pytest.mark.parametrize("system", ["darwin", sys.platform])
@pytest.mark.parametrize("pid", [0, -1, None, True, "1", 1.0, 2**31, 2**32 + 1, 2**63])
def test_an_id_that_names_no_single_process_has_no_identity(pid, system, mac, monkeypatch):
    # 0 is the kernel on a Mac, -1 every process; 2**32 + 1 would reach process 1 through a C int.
    mac.births.update({0: (100,), 1: (100,)})
    mac.argv[1] = ["/sbin/launchd"]
    monkeypatch.setattr(process_identity, "_platform", lambda: system)
    assert process_identity.start(pid) is None and process_identity.command_line(pid) is None
    assert process_identity.executable(pid) is None and not process_identity.alive(pid)
    assert process_identity.group_running(pid) is None and process_identity.recorded(pid, "100.000000") is None


def test_another_system_establishes_no_identity(monkeypatch, sleeper):
    monkeypatch.setattr(process_identity, "_platform", lambda: "freebsd14")
    assert process_identity.start(sleeper.pid) is None and process_identity.command_line(sleeper.pid) is None
    assert process_identity.executable(sleeper.pid) is None and process_identity.group_running(sleeper.pid) is None
    assert process_identity.alive(sleeper.pid)


# --- macOS: the records as the kernel returns them --------------------------------------------------------

def test_mac_birth_is_the_start_time_of_the_record_of_that_process(mac):
    mac.births[4321] = (1_763_400_000, 123)
    assert process_identity.start(4321) == "1763400000.000123"
    assert cockpit._birth(4321) == verification._process_identity(4321) == "1763400000.000123"
    mac.births[4321] = (1_763_400_000, 123, ZOMBIE)
    assert process_identity.start(4321) == "1763400000.000123"
    assert process_identity.start(4321, running=True) is None and verification._process_identity(4321) is None
    assert process_identity.start(4322) is None  # no such process: sysctl answers with no record


@pytest.mark.parametrize("record", [b"", kinfo(1234, 100)[:600], kinfo(9999, 100), kinfo(1234, 0),
                                    kinfo(1234, 100, 1_000_000)])
def test_mac_a_record_that_is_not_the_process_gives_no_birth(record):
    # Short, of another pid (a layout that is not the expected one), or without a start time.
    assert process_identity._darwin_birth(record, 1234, False) is None


def test_mac_command_line_is_the_exact_argument_vector(mac):
    argv = ["/opt/java/bin/java", "-Darcadedb.server.rootPath=/Users/some one/data dir/arcade", "x y"]
    mac.argv[4321] = argv
    assert process_identity.command_line(4321) == argv
    assert process_identity.executable(4321) == "/Library/Java/bin/java"
    assert process_identity.command_line(4322) is None and process_identity.executable(4322) is None


@pytest.mark.parametrize("buffer", [None, b"", b"\1\0", procargs(["a", "b"])[:-30],
                                    struct.pack("=i", 0) + b"/bin/x\0\0", struct.pack("=i", 1) + b"/bin/x"])
def test_mac_a_cut_or_empty_argument_buffer_gives_no_command_line(buffer):
    assert process_identity._darwin_arguments(buffer) is None


def test_mac_group_runs_while_one_member_is_not_a_zombie(mac):
    mac.groups[77] = kinfo(77, 100, status=ZOMBIE) + kinfo(78, 100)
    assert process_identity.group_running(77) is True
    mac.groups[77] = kinfo(77, 100, status=ZOMBIE)
    assert process_identity.group_running(77) is False
    mac.groups[77] = b""
    assert process_identity.group_running(77) is False
    for unreadable in (None, kinfo(77, 100)[:-8], bytes(648)):
        mac.groups[77] = unreadable
        assert process_identity.group_running(77) is None


# --- A reused process id is refused, on both systems ------------------------------------------------------

@pytest.fixture
def engine_record(tmp_path, monkeypatch, sleeper):
    settings = Settings(tmp_path)
    state = tmp_path / "arcade"
    state.mkdir()
    (state / "process.json").write_text(json.dumps({"pid": sleeper.pid}))
    monkeypatch.setattr(engine, "engine_status", lambda settings: {"available": False})
    return settings, state


def engine_argv(state):
    return ["/opt/java/bin/java", f"-Darcadedb.server.rootPath={state}", "com.arcadedb.server.ArcadeDBServer"]


def test_mac_engine_stop_refuses_a_reused_id_and_stops_its_own_engine(mac, engine_record, sleeper, tmp_path):
    settings, state = engine_record
    mac.argv[sleeper.pid] = engine_argv(tmp_path / "another" / "arcade")  # the id now names another engine
    with pytest.raises(ArcadeError, match="no longer belongs"):
        engine.stop_engine(settings)
    assert not ended(sleeper, .3) and (state / "process.json").exists()
    del mac.argv[sleeper.pid]  # alive, but its command line cannot be read: refused too
    with pytest.raises(ArcadeError, match="no longer belongs"):
        engine.stop_engine(settings)
    assert not ended(sleeper, .3)
    mac.argv[sleeper.pid] = engine_argv(state)
    assert engine.managed_java_executable(settings) == "/Library/Java/bin/java"
    assert engine.stop_engine(settings) == {"stopping": True}
    assert ended(sleeper) and (sys.platform == "win32" or sleeper.returncode == -signal.SIGTERM)
    assert not (state / "process.json").exists()


def test_mac_engine_stop_of_a_process_already_gone_clears_its_record(mac, engine_record, sleeper):
    settings, state = engine_record
    sleeper.kill()
    sleeper.wait()
    assert engine.stop_engine(settings) == {"stopping": True}
    assert not (state / "process.json").exists()


@linux
def test_linux_engine_stop_refuses_a_process_that_is_not_its_engine(engine_record, sleeper):
    settings, state = engine_record
    with pytest.raises(ArcadeError, match="no longer belongs"):
        engine.stop_engine(settings)
    assert not ended(sleeper, .3)
    assert engine.managed_java_executable(settings) is None


@pytest.mark.parametrize("system", ["linux", "darwin"])
def test_a_command_group_whose_leader_id_was_reused_is_not_killed(system, monkeypatch, sleeper):
    if system == "linux" and not sys.platform.startswith("linux"):
        pytest.skip("procfs is read on Linux only")
    if system == "darwin":
        mac = Mac(monkeypatch)
        mac.births[sleeper.pid] = (1_763_400_500, 42)
    birth = process_identity.start(sleeper.pid)
    assert birth
    ToolExecutor._kill_group(sleeper, birth + "0")  # the id now names a process born at another time
    assert not ended(sleeper, .3)
    ToolExecutor._kill_group(sleeper, birth)
    assert ended(sleeper) and (sys.platform == "win32" or sleeper.returncode == -signal.SIGKILL)


def test_mac_a_command_or_mcp_group_is_not_signalled_while_its_leader_cannot_be_identified(monkeypatch, sleeper):
    # sysctl fails (libSystem not loaded, another struct size): the leader runs, but its birth cannot be read.
    monkeypatch.setattr(process_identity, "_platform", lambda: "darwin")
    monkeypatch.setattr(process_identity, "_darwin_kinfo", lambda pid: None)
    birth = "1763400000.000042"
    assert process_identity.recorded(sleeper.pid, birth) is None
    assert process_identity.recorded(sleeper.pid, None) is None
    sig = getattr(signal, "SIGKILL", signal.SIGTERM)
    ToolExecutor._kill_group(sleeper, birth)
    assert asyncio.run(ToolExecutor._signal_group(sleeper.pid, sleeper, birth)) is False  # not confirmed
    McpServer._signal(SimpleNamespace(birth=birth), sleeper, sig)
    assert not ended(sleeper, .3)


def test_the_group_of_a_leader_that_is_gone_is_still_its_own(monkeypatch):
    # The kernel affirms that no process has the id: what is left of the group may be signalled.
    monkeypatch.setattr(process_identity, "_platform", lambda: "darwin")
    monkeypatch.setattr(process_identity, "_darwin_kinfo", lambda pid: None)
    process = subprocess.Popen([sys.executable, "-c", "pass"] if sys.platform == "win32" else ["true"])
    process.wait()
    assert process_identity.recorded(process.pid, "1763400000.000042") is True


@pytest.mark.parametrize("system", ["linux", "darwin"])
def test_a_leader_is_its_own_only_with_the_recorded_birth(system, monkeypatch, sleeper):
    if system == "linux" and not sys.platform.startswith("linux"):
        pytest.skip("procfs is read on Linux only")
    if system == "darwin":
        Mac(monkeypatch).births[sleeper.pid] = (1_763_400_500, 42)
    birth = process_identity.start(sleeper.pid)
    assert process_identity.recorded(sleeper.pid, birth) is True
    assert process_identity.recorded(sleeper.pid, birth + "0") is False
    assert process_identity.recorded(sleeper.pid, None) is False  # no birth was recorded at the spawn
    sig = getattr(signal, "SIGKILL", signal.SIGTERM)
    McpServer._signal(SimpleNamespace(birth=birth + "0"), sleeper, sig)
    assert not ended(sleeper, .3)
    McpServer._signal(SimpleNamespace(birth=birth), sleeper, sig)
    assert ended(sleeper) and (sys.platform == "win32" or sleeper.returncode == -signal.SIGKILL)


def test_mac_an_interrupted_check_runner_is_told_apart_from_a_reused_id(mac):
    mac.births[os.getpid()] = (1_763_400_000, 1)
    recorded = verification._process_identity(os.getpid())
    mac.births[os.getpid()] = (1_763_400_900, 1)  # the runner died, its id was given to a new process
    assert verification._process_identity(os.getpid()) != recorded


@pytest.mark.skipif(sys.platform != "win32", reason="Reads Windows process identity via Win32/NtQuery APIs")
def test_windows_reads_birth_command_line_and_executable_for_real(sleeper):
    birth = process_identity.start(sleeper.pid)
    assert birth is not None and birth.isdigit()
    assert process_identity.start(sleeper.pid, running=True) == birth
    assert process_identity.alive(sleeper.pid) is True
    exe = process_identity.executable(sleeper.pid)
    assert exe is not None and exe.lower().endswith(".exe")
    argv = process_identity.command_line(sleeper.pid)
    assert argv is not None and any("time.sleep(30)" in part for part in argv)
    assert process_identity.recorded(sleeper.pid, birth) is True
    assert process_identity.recorded(sleeper.pid, birth + "0") is False
