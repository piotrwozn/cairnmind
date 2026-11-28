"""Cross-platform exclusive and shared file lock tests (flock on POSIX, LockFileEx on Windows)."""
from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time

import pytest

from cairnmind.pal import file_locks


def test_exclusive_lock_blocks_nonblocking_contender(tmp_path):
    target = tmp_path / "resource.lock"
    with target.open("a", encoding="utf-8") as first, target.open("a", encoding="utf-8") as second:
        file_locks.lock_exclusive(first, blocking=False)
        try:
            with pytest.raises(BlockingIOError):
                file_locks.lock_exclusive(second, blocking=False)
        finally:
            file_locks.unlock(first)
        file_locks.lock_exclusive(second, blocking=False)
        file_locks.unlock(second)


def test_shared_locks_allow_concurrent_readers_and_block_writer(tmp_path):
    target = tmp_path / "slot.lock"
    with (target.open("a", encoding="utf-8") as r1,
          target.open("a", encoding="utf-8") as r2,
          target.open("a", encoding="utf-8") as writer):
        file_locks.lock_shared(r1, blocking=False)
        file_locks.lock_shared(r2, blocking=False)
        try:
            with pytest.raises(BlockingIOError):
                file_locks.lock_exclusive(writer, blocking=False)
        finally:
            file_locks.unlock(r1)
            file_locks.unlock(r2)
        file_locks.lock_exclusive(writer, blocking=False)
        file_locks.unlock(writer)


def test_relock_and_content_io_while_locked(tmp_path):
    """Offset 2**62 locking allows reading/writing file contents while locked and safe re-locking (#435)."""
    target = tmp_path / "stamp.lock"
    with target.open("a+", encoding="utf-8") as handle:
        file_locks.lock_exclusive(handle, blocking=False)
        # Idempotent re-lock on same handle must not fail or deadlock
        file_locks.lock_exclusive(handle, blocking=False)
        handle.write("pid=1234\n")
        handle.flush()
        handle.seek(0)
        assert handle.read() == "pid=1234\n"
        # Another reader can read bytes at offset 0 even while offset 2**62 is exclusively locked
        assert target.read_text(encoding="utf-8") == "pid=1234\n"
        # Downgrade to shared and upgrade back to exclusive on same handle
        file_locks.lock_shared(handle, blocking=False)
        file_locks.lock_exclusive(handle, blocking=False)
        file_locks.unlock(handle)


def test_locked_path_releases_on_normal_and_exception_exit(tmp_path):
    target = tmp_path / "ctx.lock"
    with file_locks.locked_path(target, blocking=False) as handle:
        assert not os.get_inheritable(handle.fileno())
        with pytest.raises(BlockingIOError), file_locks.locked_path(target, blocking=False):
            pytest.fail("nested nonblocking lock must fail")
    with pytest.raises(RuntimeError), file_locks.locked_path(target, blocking=False):
        raise RuntimeError("boom")
    with file_locks.locked_path(target, blocking=False):
        pass


def test_exclusive_lock_enforced_across_processes(tmp_path):
    target = tmp_path / "cross-proc.lock"
    code = (
        "import sys; from pathlib import Path; from cairnmind.pal import file_locks\n"
        "with file_locks.locked_path(Path(sys.argv[1]), blocking=False):\n"
        "    print('LOCKED', flush=True)\n"
        "    sys.stdin.readline()\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(target)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert child.stdout.readline().strip() == "LOCKED"
        with pytest.raises(BlockingIOError), file_locks.locked_path(target, blocking=False):
            pytest.fail("acquired lock held by another process")
    finally:
        child.stdin.close()
        child.wait(timeout=15)
    with file_locks.locked_path(target, blocking=False):
        pass


def test_shared_locks_enforced_across_two_processes(tmp_path):
    """Two processes hold shared locks concurrently while an exclusive contender is blocked (#435)."""
    target = tmp_path / "shared-proc.lock"
    code = (
        "import sys; from pathlib import Path; from cairnmind.pal import file_locks\n"
        "with Path(sys.argv[1]).open('a', encoding='utf-8') as f:\n"
        "    file_locks.lock_shared(f, blocking=False)\n"
        "    print('SHARED', flush=True)\n"
        "    sys.stdin.readline()\n"
    )
    p1 = subprocess.Popen([sys.executable, "-c", code, str(target)],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    p2 = subprocess.Popen([sys.executable, "-c", code, str(target)],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert p1.stdout.readline().strip() == "SHARED"
        assert p2.stdout.readline().strip() == "SHARED"
        with pytest.raises(BlockingIOError), file_locks.locked_path(target, blocking=False):
            pytest.fail("exclusive lock succeeded while two processes held shared locks")
    finally:
        p1.stdin.close()
        p2.stdin.close()
        p1.wait(timeout=15)
        p2.wait(timeout=15)
    with file_locks.locked_path(target, blocking=False):
        pass


def test_killing_lock_holder_releases_lock_for_next_process(tmp_path):
    """Hard-killing the lock holder releases the OS lock immediately (#435)."""
    from cairnmind.pal import process_control
    target = tmp_path / "killed-holder.lock"
    code = (
        "import os, sys, time; from pathlib import Path; from cairnmind.pal import file_locks\n"
        "with file_locks.locked_path(Path(sys.argv[1]), blocking=False):\n"
        "    print(f'LOCKED {os.getpid()}', flush=True)\n"
        "    time.sleep(60)\n"
    )
    holder = subprocess.Popen([sys.executable, "-c", code, str(target)],
                              stdout=subprocess.PIPE, text=True)
    real_pid = None
    try:
        line = holder.stdout.readline().strip()
        assert line.startswith("LOCKED ")
        real_pid = int(line.split()[1])
        with pytest.raises(BlockingIOError), file_locks.locked_path(target, blocking=False):
            pass
        process_control.terminate_pid(real_pid, force=True)
        holder.kill()
        holder.wait(timeout=15)
        # On Windows, holder.pid is uv's .venv\Scripts\python.exe shim while real_pid is the child
        # cpython process; TerminateProcess is asynchronous in the kernel, so wait until real_pid exits.
        from cairnmind.pal import process_identity
        deadline = time.monotonic() + 5.0
        while process_identity.alive(real_pid) and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        if real_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                process_control.terminate_pid(real_pid, force=True)
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=15)
    with file_locks.locked_path(target, blocking=False):
        pass


def test_child_process_does_not_inherit_lock_handle(tmp_path):
    """A child spawned while parent holds locked_path must not inherit the lock and keep it stuck (#435)."""
    target = tmp_path / "inherit.lock"
    grandchild_code = "import sys, time; print('CHILD_UP', flush=True); sys.stdin.readline()"
    parent_code = (
        "import subprocess, sys; from pathlib import Path; from cairnmind.pal import file_locks\n"
        "with file_locks.locked_path(Path(sys.argv[1]), blocking=False):\n"
        "    gc = subprocess.Popen([sys.executable, '-c', sys.argv[2]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)\n"
        "    assert gc.stdout.readline().strip() == 'CHILD_UP'\n"
        "print(f'GC_PID={gc.pid}', flush=True)\n"
        "sys.stdin.readline()\n"
        "gc.stdin.close(); gc.wait(timeout=10)\n"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code, str(target), grandchild_code],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        line = parent.stdout.readline().strip()
        assert line.startswith("GC_PID=")
        # Parent exited locked_path while grandchild is still alive: lock MUST be free now.
        with file_locks.locked_path(target, blocking=False):
            pass
    finally:
        parent.stdin.close()
        parent.wait(timeout=15)


def test_product_locks_work_on_this_platform(tmp_path):
    """Exercise real product lock callers: config.load(create=True), setup_mark.held, CockpitState (#435)."""
    from cairnmind.config import Settings
    from cairnmind.terminal import setup_mark
    from cairnmind.terminal.cockpit_state import CockpitState

    settings = Settings.load(tmp_path / "data", create=True)
    assert (settings.data_dir / "config.lock").exists()

    with setup_mark.held(settings.data_dir):
        assert setup_mark.mark_path(settings.data_dir).exists()
        with pytest.raises(setup_mark.ChangeBusy), setup_mark.held(settings.data_dir):
            pass
    assert not setup_mark.mark_path(settings.data_dir).exists()

    state = CockpitState(settings)
    state.update({"language": "pl"})
    assert state.load()["language"] == "pl"
