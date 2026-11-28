"""Freed memory goes back to the system after a heavy pass, and the service keeps two malloc arenas (#410)."""
from __future__ import annotations

import os
import subprocess
import sys
import types

import pytest

from cairnmind.pal import memory_release

GLIBC = sys.platform.startswith("linux") and os.confstr("CS_GNU_LIBC_VERSION").startswith("glibc") if hasattr(os, "confstr") else False


@pytest.mark.skipif(not GLIBC, reason="glibc only")
def test_glibc_accepts_the_arena_limit_and_trims():
    assert memory_release.limit_arenas(2) is True
    assert memory_release.release_freed() is True


SCATTERED = """
import ctypes, threading
from cairnmind.pal import memory_release

def rss():
    return int([l for l in open("/proc/self/status") if l.startswith("VmRSS")][0].split()[1]) / 1024

libc = ctypes.CDLL(None)
libc.malloc.restype = ctypes.c_void_p
libc.free.argtypes = [ctypes.c_void_p]
kept = []

def work():  # what a worker does: build a lot, free nearly all of it, keep a few scattered chunks
    chunks = []
    for _ in range(40_000):
        pointer = libc.malloc(1000)
        ctypes.memset(pointer, 1, 1000)
        chunks.append(pointer)
    for number, pointer in enumerate(chunks):
        kept.append(pointer) if number % 50 == 0 else libc.free(pointer)

assert memory_release.limit_arenas(2)
start = rss()
threads = [threading.Thread(target=work) for _ in range(4)]
[t.start() for t in threads]
[t.join() for t in threads]
held = rss() - start
assert memory_release.release_freed()
print(held, rss() - start)
"""


@pytest.mark.skipif(not GLIBC, reason="glibc only")
def test_release_freed_gives_back_what_freed_worker_threads_still_hold():
    done = subprocess.run([sys.executable, "-c", SCATTERED], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    held, left = (float(number) for number in done.stdout.split())
    if held < 30:
        pytest.skip("this glibc already returned the memory by itself")
    assert left < held / 2


def test_without_glibc_both_say_false_and_do_nothing(monkeypatch):
    monkeypatch.setattr(memory_release, "_libc", lambda: None)
    assert memory_release.limit_arenas() is False and memory_release.release_freed() is False


def test_a_library_without_the_functions_is_not_called_as_if_it_had_them(monkeypatch):
    monkeypatch.setattr(memory_release, "_libc", lambda: object())  # a libc of another kind: no mallopt, no malloc_trim
    assert memory_release.limit_arenas() is False and memory_release.release_freed() is False


@pytest.mark.parametrize("platform", ["darwin", "win32", "freebsd14"])
def test_off_linux_no_library_is_looked_up_and_both_calls_do_nothing(monkeypatch, platform):
    monkeypatch.setattr(memory_release, "sys", types.SimpleNamespace(platform=platform))  # not the real sys
    memory_release._libc.cache_clear()
    try:
        assert memory_release._libc() is None
        assert memory_release.limit_arenas() is False and memory_release.release_freed() is False
    finally:
        monkeypatch.undo()
        memory_release._libc.cache_clear()


def test_a_libc_that_refuses_the_setting_or_the_trim_is_reported_as_doing_nothing(monkeypatch):
    class Refusing:  # what a library without the glibc behaviour may answer: "not done"
        mallopt = staticmethod(lambda option, value: 0)
        malloc_trim = staticmethod(lambda pad: 2)

    monkeypatch.setattr(memory_release, "_libc", lambda: Refusing())
    assert memory_release.limit_arenas() is False and memory_release.release_freed() is False


def test_serve_limits_the_arenas_before_the_service_is_made(monkeypatch, tmp_path):
    from typer.testing import CliRunner
    from cairnmind import cli, service
    from cairnmind.config import Settings
    order = []

    def create_app(*args, **kwargs):
        order.append("app")
        raise RuntimeError("the service is not started by this test")

    monkeypatch.setattr(memory_release, "limit_arenas", lambda *args: order.append("limit"))
    monkeypatch.setattr(service, "create_app", create_app)
    settings = Settings.load(tmp_path / "memory", create=True)
    CliRunner().invoke(cli.app, ["--data-dir", str(settings.data_dir), "serve"])
    assert order == ["limit", "app"]
