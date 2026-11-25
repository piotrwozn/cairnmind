"""64-bit unsigned st_dev/st_ino persistence in real ArcadeDB (#441) and engine lifecycle recovery (#438)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sys
import types

import pytest

from cairnmind import engine, private_files, process_control, process_identity
from cairnmind.config import Settings
from cairnmind.integrations import _transcript_watermark
from cairnmind.sessions import SessionManager
from cairnmind.store import Store


UNSIGNED_DEV = 10974387285115203278  # > 2**63 - 1 (real Windows NTFS st_dev from #431)
UNSIGNED_INO = (1 << 64) - 7         # > 2**63 - 1


def test_encode_decode_file_id_round_trip():
    assert private_files.encode_file_id(0) == 0
    assert private_files.encode_file_id((1 << 63) - 1) == (1 << 63) - 1
    assert private_files.encode_file_id(UNSIGNED_DEV) == str(UNSIGNED_DEV)
    assert private_files.decode_file_id(UNSIGNED_DEV) == UNSIGNED_DEV
    assert private_files.decode_file_id(str(UNSIGNED_DEV)) == UNSIGNED_DEV
    assert private_files.decode_file_id(-1) is None
    assert private_files.decode_file_id("abc") is None


def test_unsigned_64bit_watermark_persists_in_real_arcadedb(arcade_test_engine, tmp_path, monkeypatch):
    """st_dev/st_ino > 2^63-1 persist through session_events and resume_packets without ArcadeDB HTTP 400 (#441)."""
    settings = Settings.load(tmp_path / "data", create=True)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"role": "user", "text": "hello"}\n', encoding="utf-8")

    real_fstat = os.fstat
    real_stat = Path.stat

    def fake_stat_obj(st):
        return types.SimpleNamespace(
            st_mode=st.st_mode,
            st_ino=UNSIGNED_INO,
            st_dev=UNSIGNED_DEV,
            st_nlink=st.st_nlink,
            st_uid=st.st_uid,
            st_gid=st.st_gid,
            st_size=st.st_size,
            st_atime=st.st_atime,
            st_mtime=st.st_mtime,
            st_ctime=st.st_ctime,
            st_atime_ns=st.st_atime_ns,
            st_mtime_ns=st.st_mtime_ns,
            st_ctime_ns=st.st_ctime_ns,
        )

    def patched_fstat(fd):
        return fake_stat_obj(real_fstat(fd))

    transcript_abs = os.path.normcase(os.path.abspath(str(transcript.resolve())))

    def patched_path_stat(self, *, follow_symlinks=True):
        st = real_stat(self, follow_symlinks=follow_symlinks)
        return fake_stat_obj(st) if os.path.normcase(os.path.abspath(str(self))) == transcript_abs else st

    monkeypatch.setattr(os, "fstat", patched_fstat)
    monkeypatch.setattr(Path, "stat", patched_path_stat)

    watermark = _transcript_watermark(transcript)
    assert watermark["device"] == str(UNSIGNED_DEV)
    assert watermark["inode"] == str(UNSIGNED_INO)

    store = Store(settings.data_dir, settings=settings)
    store.init()
    repo = tmp_path / "repo"
    repo.mkdir()
    project = store.add_project("id64", repo)
    sessions = SessionManager(store, settings)
    started = sessions.prepare(project["id"], "codex", worktree=str(repo))

    # Store session_events row carrying unsigned 64-bit watermark in real ArcadeDB (failed HTTP 400 before #441)
    sessions.note_event({
        "cairnmind_session_id": started["session_id"],
        "session_id": "native-1",
        "hook_event_name": "SessionStart",
        "cwd": str(repo),
        "transcript_path": str(transcript.resolve()),
        "transcript_watermark": watermark,
    }, "codex")
    rows = store.query("SELECT FROM session_events WHERE session_id=:sid", {"sid": started["session_id"]})
    assert rows and rows[0]["transcript_watermark"]["device"] == str(UNSIGNED_DEV)
    assert rows[0]["transcript_watermark"]["inode"] == str(UNSIGNED_INO)

    # Verify SessionManager._same_generation matches both encoded str and legacy int forms
    left = SessionManager._target(str(transcript), "codex", watermark)
    right = SessionManager._target(str(transcript), "codex", {**watermark, "device": UNSIGNED_DEV, "inode": UNSIGNED_INO})
    assert SessionManager._same_generation(left, right) is True

    # Verify legacy small int watermark still compares cleanly without schema migration
    legacy = {**watermark, "device": 42, "inode": 99}
    assert SessionManager._same_generation(
        {"path": str(transcript.resolve()), "size": legacy["size"], "device": 42, "inode": 99,
         "head_bytes": legacy["head_bytes"], "head_sha256": legacy["head_sha256"]},
        {"path": str(transcript.resolve()), "size": legacy["size"], "device": "42", "inode": "99",
         "head_bytes": legacy["head_bytes"], "head_sha256": legacy["head_sha256"]},
    ) is True


def test_real_engine_pid_reuse_guard_and_kill_recovery(arcade_test_engine, tmp_path):
    """Real engine starts, refuses to kill a foreign PID on reuse, recovers after hard kill, and stops cleanly (#438)."""
    engine_settings = arcade_test_engine[0]
    runtime_src = engine.runtime_dir(engine_settings)
    if not (runtime_src / "lib").is_dir():
        pytest.skip("Managed local ArcadeDB runtime not installed (server mode)")

    data_dir = private_files.ensure_private_dir(tmp_path / "isolated-engine")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    iso = Settings.load(data_dir, create=True)
    iso.arcade_url = f"http://127.0.0.1:{port}"
    iso.save()

    # Reuse the already-installed ArcadeDB runtime directory via junction/symlink or copy?
    # engine.runtime_dir(iso) is iso.data_dir/arcade/runtime/arcadedb-<ver>.
    dst_runtime = engine.runtime_dir(iso)
    dst_runtime.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        import subprocess
        subprocess.run(["cmd", "/c", "mklink", "/J", str(dst_runtime), str(runtime_src)],
                       check=True, capture_output=True, timeout=10)
    else:
        dst_runtime.symlink_to(runtime_src)

    try:
        started = engine.start_engine(iso)
        assert started["available"] is True
        pid_file = iso.data_dir / "arcade" / "process.json"
        record = json.loads(pid_file.read_text(encoding="utf-8"))
        real_pid = int(record["pid"])
        assert process_identity.alive(real_pid)

        # PID-reuse guard: point process.json at our own pytest PID (not an ArcadeDB server on iso.data_dir)
        pid_file.write_text(json.dumps({**record, "pid": os.getpid()}), encoding="utf-8")
        with pytest.raises(engine.ArcadeError, match="no longer belongs"):
            engine.stop_engine(iso)
        # Our own process is still alive and the real engine was NOT killed by the mismatched record
        assert process_identity.alive(real_pid)

        # Restore real record, hard-kill the engine, and verify next start_engine recovers cleanly
        pid_file.write_text(json.dumps(record), encoding="utf-8")
        process_control.terminate_pid(real_pid, force=True)
        assert engine.start_engine(iso)["available"] is True
        engine.stop_engine(iso)
        assert engine.engine_status(iso)["available"] is False
    finally:
        try:
            engine.stop_engine(iso)
        except Exception:
            pass
        if dst_runtime.exists() or private_files.is_reparse_point(dst_runtime):
            if sys.platform == "win32":
                os.rmdir(dst_runtime)
            else:
                dst_runtime.unlink()
