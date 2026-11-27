"""Phase 5 / PR #5 tests: Native Windows foundation (locks, FS, verification, identity, gating — ADR-05)."""
from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys
import time
import types

import httpx
import pytest

from cairnmind import (
    cockpit_state,
    config,
    integrations,
    path_spelling,
    person_proof,
    platform_fs,
    platform_hints,
    platform_locks,
    process_identity,
    store_writer,
    verification,
)
from cairnmind.client_runtime import NativeRuntime
from cairnmind.config import Settings


def test_platform_locks_exclusive_nonblocking_and_release(tmp_path):
    lock_path = tmp_path / "sample.lock"
    with lock_path.open("a+") as h1, lock_path.open("a+") as h2:
        platform_locks.lock_file(h1, exclusive=True, blocking=True)
        with pytest.raises(BlockingIOError):
            platform_locks.lock_file(h2, exclusive=True, blocking=False)
        platform_locks.unlock_file(h1)
        platform_locks.lock_file(h2, exclusive=True, blocking=False)
        platform_locks.unlock_file(h2)


def test_platform_locks_shared_and_context_manager(tmp_path):
    lock_path = tmp_path / "shared.lock"
    with platform_locks.file_lock(lock_path, exclusive=False) as h1:
        with lock_path.open("a+") as h2:
            # Two shared locks coexist, but an exclusive non-blocking lock is refused.
            platform_locks.lock_file(h2, exclusive=False, blocking=False)
            with lock_path.open("a+") as h3:
                with pytest.raises(BlockingIOError):
                    platform_locks.lock_file(h3, exclusive=True, blocking=False)
            platform_locks.unlock_file(h2)
        assert h1 is not None
    # After exiting the context manager, exclusive lock succeeds immediately.
    with platform_locks.file_lock(lock_path, exclusive=True, blocking=False):
        pass


def test_platform_fs_pread_preserves_offset_and_fallback(tmp_path, monkeypatch):
    sample = tmp_path / "data.bin"
    sample.write_bytes(b"0123456789abcdef")
    fd = os.open(sample, os.O_RDONLY)
    try:
        os.lseek(fd, 4, os.SEEK_SET)
        assert platform_fs.pread_bytes(fd, 5, 2) == b"23456"
        assert os.lseek(fd, 0, os.SEEK_CUR) == 4

        # Force the Windows fallback path even on POSIX.
        monkeypatch.delattr(os, "pread", raising=False)
        assert platform_fs.pread_bytes(fd, 4, 10) == b"abcd"
        assert os.lseek(fd, 0, os.SEEK_CUR) == 4
        assert platform_fs.pread_bytes(fd, 0, 2) == b""
    finally:
        os.close(fd)


def test_platform_fs_default_user_data_base():
    win_base = platform_fs.default_user_data_base(
        {"LOCALAPPDATA": r"C:\Users\alice\AppData\Local"}, platform_name="win32"
    )
    assert str(win_base).endswith("Local")
    win_fallback = platform_fs.default_user_data_base(
        {"APPDATA": r"C:\Users\alice\AppData\Roaming"}, platform_name="win32"
    )
    assert str(win_fallback).endswith("Roaming")
    posix_base = platform_fs.default_user_data_base({}, platform_name="linux")
    assert posix_base == Path("~/.local/share").expanduser()


def test_platform_fs_sync_directory_safe_on_windows_and_posix(tmp_path, monkeypatch):
    platform_fs.sync_directory(tmp_path)
    monkeypatch.setattr(platform_fs, "IS_WINDOWS", True)
    platform_fs.sync_directory(tmp_path)  # no-op, never raises


def test_store_writer_windows_machine_guid(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(store_writer, "windows_machine_guid", lambda: "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(store_writer.socket, "gethostname", lambda: "WIN-BOX")
    assert store_writer.machine_identity() == "machine-guid:11111111-2222-3333-4444-555555555555"

    monkeypatch.setattr(store_writer, "windows_machine_guid", lambda: None)
    assert store_writer.machine_identity() == "host:WIN-BOX"


def test_path_spelling_has_alias_windows_fallback(tmp_path, monkeypatch):
    sub = tmp_path / "dir~literal"
    sub.mkdir()
    target = sub / "note.txt"
    target.write_text("ok", encoding="utf-8")

    # Even with O_DIRECTORY disabled (Windows path), literal '~' files are NOT false-positive aliases.
    monkeypatch.setattr(path_spelling, "O_DIRECTORY", 0)
    assert path_spelling.has_alias(target, tmp_path) is False
    assert path_spelling._is_alias_path("missing~1", tmp_path) is False


def test_verification_threaded_collector_completed_limit_and_timeout(tmp_path):
    # 1. Completed run capturing both stdout and stderr.
    out_dir = tmp_path / "run_ok"
    out_dir.mkdir()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    logs = {name: (out_dir / name).open("wb", buffering=0) for name in buffers}
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdout.write('hello'); sys.stderr.write('warn')"],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        outcome = verification._collect_pipes_threaded(
            proc, buffers, logs, {"timeout_seconds": 10.0, "output_limit_bytes": 1024}, time.monotonic()
        )
        assert outcome == "completed"
        assert proc.returncode == 0
        assert bytes(buffers["stdout"]) == b"hello"
        assert bytes(buffers["stderr"]) == b"warn"
    finally:
        for s in (proc.stdout, proc.stderr):
            s.close()
        for h in logs.values():
            h.close()

    # 2. Output limit enforcement.
    out_dir2 = tmp_path / "run_limit"
    out_dir2.mkdir()
    buffers2 = {"stdout": bytearray(), "stderr": bytearray()}
    logs2 = {name: (out_dir2 / name).open("wb", buffering=0) for name in buffers2}
    proc2 = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 50000); sys.stdout.flush()"],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        outcome2 = verification._collect_pipes_threaded(
            proc2, buffers2, logs2, {"timeout_seconds": 10.0, "output_limit_bytes": 128}, time.monotonic()
        )
        assert outcome2 == "output_limit"
        assert sum(len(b) for b in buffers2.values()) == 128
    finally:
        for s in (proc2.stdout, proc2.stderr):
            s.close()
        for h in logs2.values():
            h.close()

    # 3. Timeout enforcement.
    out_dir3 = tmp_path / "run_timeout"
    out_dir3.mkdir()
    buffers3 = {"stdout": bytearray(), "stderr": bytearray()}
    logs3 = {name: (out_dir3 / name).open("wb", buffering=0) for name in buffers3}
    proc3 = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **({"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
           if sys.platform == "win32" else {"start_new_session": True}),
    )
    try:
        outcome3 = verification._collect_pipes_threaded(
            proc3, buffers3, logs3, {"timeout_seconds": 0.2, "output_limit_bytes": 1024}, time.monotonic()
        )
        assert outcome3 == "timeout"
        assert proc3.poll() is not None
    finally:
        for s in (proc3.stdout, proc3.stderr):
            s.close()
        for h in logs3.values():
            h.close()


def test_verification_execute_end_to_end(tmp_path):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    profile = {
        "id": "smoke",
        "argv": [sys.executable, "-c", "print('verified-ok')"],
        "cwd": ".",
        "timeout_seconds": 10.0,
        "output_limit_bytes": 4096,
    }
    outcome, code, error, buffers = verification._execute(profile, tmp_path, output_dir)
    assert outcome == "completed"
    assert code == 0
    assert error is None
    assert b"verified-ok" in bytes(buffers["stdout"])


def test_cockpit_state_normalize_path_skips_wslpath_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")

    def boom(*args, **kwargs):
        raise AssertionError("wslpath must never run on native Windows")

    monkeypatch.setattr(cockpit_state.subprocess, "run", boom)
    resolved = cockpit_state.normalize_path(str(tmp_path))
    assert resolved == tmp_path.resolve()


def test_windows_platform_hints_tmux_gating_and_doctor(tmp_path, monkeypatch):
    assert platform_hints.host_system("win32", "10") == "windows"
    settings = Settings.load(tmp_path / "data", create=True)
    monkeypatch.setattr(platform_hints, "host_system", lambda *a, **k: "windows")

    diag = NativeRuntime(settings).diagnostics()
    assert diag["available"] is False
    assert diag["code"] == "tmux_unsupported_platform"
    assert diag["platform_supported"] is False
    assert "Windows" in diag["action"]

    with pytest.raises(ValueError, match="tmux_unsupported_platform"):
        NativeRuntime(settings)._tmux("list-sessions")

    monkeypatch.setattr(sys, "platform", "win32")
    assert person_proof.workspace_socket(settings, str(tmp_path / "tmux.sock")) is None

    monkeypatch.setattr(integrations, "_probe_cli", lambda name, *a, **k: {"installed": False})
    monkeypatch.setattr(integrations.shutil, "which", lambda *a, **k: None)
    real_client = httpx.Client
    monkeypatch.setattr(
        integrations.httpx,
        "Client",
        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(lambda req: httpx.Response(503))),
    )
    report = integrations.doctor(settings)
    assert report["tmux"]["code"] == "tmux_unsupported_platform"
    assert report["gpu"]["available"] is False


def test_process_identity_alive_current_process():
    assert process_identity.alive(os.getpid()) is True
    assert process_identity.alive(0) is False
    assert process_identity.alive(-1) is False


def test_no_unguarded_top_level_fcntl_or_termios_imports():
    """Every module in src/cairnmind must be importable on native Windows without top-level fcntl/termios."""
    src_root = Path(config.__file__).resolve().parent
    offenders: list[str] = []
    for py_file in sorted(src_root.glob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in {"fcntl", "termios", "pty", "tty"}:
                        offenders.append(f"{py_file.name}:{node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module in {"fcntl", "termios", "pty", "tty"}:
                offenders.append(f"{py_file.name}:{node.lineno}: from {node.module} import ...")
    assert offenders == []


def test_config_default_data_dir_windows_env(tmp_path, monkeypatch):
    fake_local = tmp_path / "LocalAppData"
    fake_local.mkdir()
    monkeypatch.setattr(config, "default_user_data_base", lambda env=None: fake_local)
    assert config.default_data_dir(environ={}) == (fake_local / "cairnmind").resolve()
    assert isinstance(types.SimpleNamespace(path=fake_local).path, Path)
