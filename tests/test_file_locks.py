"""Cross-platform exclusive and shared file lock tests (flock on POSIX, LockFileEx on Windows)."""
from __future__ import annotations

import subprocess
import sys

import pytest

from cairnmind import file_locks


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


def test_locked_path_releases_on_normal_and_exception_exit(tmp_path):
    target = tmp_path / "ctx.lock"
    with file_locks.locked_path(target, blocking=False):
        with pytest.raises(BlockingIOError):
            with file_locks.locked_path(target, blocking=False):
                pytest.fail("nested nonblocking lock must fail")
    with pytest.raises(RuntimeError):
        with file_locks.locked_path(target, blocking=False):
            raise RuntimeError("boom")
    with file_locks.locked_path(target, blocking=False):
        pass


def test_exclusive_lock_enforced_across_processes(tmp_path):
    target = tmp_path / "cross-proc.lock"
    code = (
        "import sys; from pathlib import Path; from cairnmind import file_locks\n"
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
        with pytest.raises(BlockingIOError):
            with file_locks.locked_path(target, blocking=False):
                pytest.fail("acquired lock held by another process")
    finally:
        child.stdin.close()
        child.wait(timeout=15)
    with file_locks.locked_path(target, blocking=False):
        pass
