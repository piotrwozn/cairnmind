"""Stopping a command's process group: cases where the stop could end early (#175)."""
import asyncio
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time

import pytest

from cairnmind import agent_tools
from cairnmind.agent_journal import AgentJournal
from cairnmind.agent_tools import ToolExecutor
from cairnmind.config import Settings

# The child ignores SIGTERM, so only SIGKILL ends it.
CHILD_IGNORING_TERM = "(trap '' TERM; echo $BASHPID > child.pid; exec sleep 30) & wait"


@pytest.fixture
def executor(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    settings = Settings.load(tmp_path / "data", create=True)
    executor = ToolExecutor(settings, root, session_id="session", run_id="run", generation=3,
                            journal=AgentJournal(settings, "session"))
    executor.approve = lambda request: True
    return executor


def running(pid):
    """Whether any thread of process pid runs (a zombie main thread does not count alone)."""
    try:
        state = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[1].split()[0]
        return state not in {b"Z", b"X"} or len(os.listdir(f"/proc/{pid}/task")) > 1
    except (OSError, IndexError):
        return False


def kill(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


async def wait_for(path):
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        await asyncio.sleep(.01)
    assert path.exists()


async def started_child(executor, command=CHILD_IGNORING_TERM):
    task = asyncio.create_task(executor.execute("run_command", {"command": command}))
    await wait_for(executor.worktree / "child.pid")
    return task, int((executor.worktree / "child.pid").read_text())


@pytest.fixture
def odd_name_process():
    # An unrelated process whose name is not valid UTF-8: 15 bytes cut inside
    # a two-byte letter, as the kernel cuts a long name.
    code = ("import ctypes, sys, time; ctypes.CDLL(None).prctl(15, 'przetwarzaniżż'.encode()[:15], 0, 0, 0); "
            "print(flush=True); time.sleep(60)")
    process = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE)
    process.stdout.readline()
    assert Path(f"/proc/{process.pid}/comm").read_bytes().startswith(b"przetwarzani\xc5\xbc\xc5")
    yield process
    process.kill()
    process.wait()


@pytest.mark.parametrize("operation", ["timeout", "cancel"])
async def test_a_process_name_that_is_not_utf8_does_not_break_the_stop(executor, odd_name_process, operation):
    executor.command_timeout_seconds = .3 if operation == "timeout" else 30
    task, pid = await started_child(executor)
    try:
        if operation == "timeout":
            result = await asyncio.wait_for(task, 10)
            assert result["timed_out"] and result["error_code"] == "command_timeout"
            assert "processes_remaining" not in result
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10)
            assert executor.journal.events()[-1]["type"] == "action_interrupted"
        assert not running(pid)
    finally:
        kill(pid)


async def test_a_process_whose_main_thread_exited_is_still_killed(executor):
    # /proc/<pid>/stat says Z once the main thread has exited, while another
    # thread of the process still runs and SIGTERM is ignored.
    child = ("import ctypes, pathlib, signal, threading, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
             "threading.Thread(target=time.sleep, args=(30,)).start(); pathlib.Path('ready').touch(); "
             "ctypes.CDLL(None).pthread_exit(None)")
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(child)} >/dev/null 2>&1 & echo $! > child.pid; wait"
    task, pid = await started_child(executor, command)
    try:
        await wait_for(executor.worktree / "ready")
        deadline = time.monotonic() + 5
        while Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[1].split()[0] != b"Z":
            assert time.monotonic() < deadline
            await asyncio.sleep(.01)
        assert running(pid)
        await executor.interrupt()
        result = await asyncio.wait_for(task, 10)
        assert result["interrupted"] and "processes_remaining" not in result
        assert not running(pid)
    finally:
        kill(pid)


async def test_a_second_cancellation_does_not_cut_the_stop_short(executor):
    # A second Ctrl+C cancels the turn again while the stop waits for SIGTERM.
    task, pid = await started_child(executor)
    try:
        task.cancel()
        # Wait for the stop itself, not a fixed time, so the second cancellation lands while it waits.
        deadline = time.monotonic() + 5
        while not executor._stopping:
            assert time.monotonic() < deadline, "the cancelled command never began to stop its process group"
            await asyncio.sleep(.005)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        assert not running(pid)
        assert not executor._running and not executor._stopping and not executor._left_running
        last = executor.journal.events()[-1]
        assert last["type"] == "action_interrupted" and "processes_remaining" not in last["data"]
    finally:
        kill(pid)


@pytest.mark.parametrize("operation", ["timeout", "cancel"])
async def test_an_unexpected_failure_in_the_stop_is_reported_not_raised(executor, monkeypatch, operation):
    def broken(pgid):
        raise RuntimeError("scan failed")

    monkeypatch.setattr(agent_tools, "_group_running", broken)
    executor.command_timeout_seconds = .3 if operation == "timeout" else 30
    task, pid = await started_child(executor)
    try:
        if operation == "timeout":
            result = await asyncio.wait_for(task, 10)
            assert result["timed_out"] and result["processes_remaining"]
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10)
            assert executor.journal.events()[-1]["data"]["processes_remaining"]
        assert executor.journal.pending_actions()[0]["last_state"] == "action_interrupted"
        # It still sent SIGKILL as a best effort.
        deadline = time.monotonic() + 5
        while running(pid):
            assert time.monotonic() < deadline
            await asyncio.sleep(.01)
    finally:
        kill(pid)


async def until_gone(pid, seconds=5):
    deadline = time.monotonic() + seconds
    while running(pid):
        assert time.monotonic() < deadline, "the process outlived the stop"
        await asyncio.sleep(.01)


async def test_a_stop_that_is_itself_cancelled_still_sends_sigkill(executor):
    # The event loop ending cancels every task, the stop in its SIGTERM grace too.
    task, pid = await started_child(executor)
    try:
        task.cancel()
        # The stop begins when the cancelled task next runs; on a slow runner that took longer than a fixed
        # tenth of a second. It then waits half a second for SIGTERM, which this child ignores.
        deadline = time.monotonic() + 5
        while not executor._stopping:
            assert time.monotonic() < deadline, "the cancelled command never began to stop its process group"
            await asyncio.sleep(.005)
        [(_, stop)] = executor._stopping.values()
        assert not stop.done()
        stop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        assert stop.cancelled()
        last = executor.journal.events()[-1]
        assert last["type"] == "action_interrupted" and last["data"]["processes_remaining"]
        await until_gone(pid, 2)
    finally:
        kill(pid)


async def test_a_cancellation_during_a_spawn_that_then_fails_is_not_lost(executor, monkeypatch):
    gate = asyncio.Event()

    async def failing_spawn(*args, **kwargs):
        await gate.wait()
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(agent_tools.asyncio, "create_subprocess_exec", failing_spawn)
    task = asyncio.create_task(executor.execute("run_command", {"command": "true"}))
    deadline = time.monotonic() + 5
    while not executor._starting:
        assert time.monotonic() < deadline
        await asyncio.sleep(.01)
    task.cancel()
    await asyncio.sleep(0)
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert not executor._starting and not executor._running and not executor._left_running


async def test_a_group_whose_leader_was_reaped_unrecorded_is_still_signalled():
    # The leader has been reaped but asyncio has not yet set its returncode,
    # while a member of the group that ignores SIGTERM still runs.
    leader = subprocess.Popen(["/bin/bash", "-c", "(trap '' TERM; exec sleep 30) & echo $! >&2"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
    pid = int(leader.stderr.readline())
    leader.stderr.close()
    leader.wait()
    try:
        assert running(pid) and os.getpgid(pid) == leader.pid
        assert agent_tools.process_start(leader.pid) is None

        class Unrecorded:
            returncode = None

        assert await ToolExecutor._signal_group(leader.pid, Unrecorded(), 0)
        await until_gone(pid, 2)
    finally:
        kill(pid)


async def test_the_group_scan_runs_off_the_event_loop(monkeypatch):
    calls = []

    def slow(pgid):
        calls.append(pgid)
        time.sleep(.3)
        return False

    monkeypatch.setattr(agent_tools, "_group_running", slow)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(.01)

    ticking = asyncio.create_task(ticker())
    try:
        assert await agent_tools._group_ended(123456, 1)
    finally:
        ticking.cancel()
    assert calls == [123456] and ticks >= 5
