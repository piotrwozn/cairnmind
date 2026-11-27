"""Edge-case tests for engine.py, database_credentials.py, repository_evaluation.py, and person_pipe.py."""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
from dataclasses import replace
from types import SimpleNamespace

import pytest

from cairnmind.storage import database_credentials as dbcreds, engine
from cairnmind.pal import person_pipe
from cairnmind.agents import repository_evaluation as repeval
from cairnmind.storage.arcade import ArcadeError
from cairnmind.config import Settings


def test_engine_install_status_and_managed_java_edges(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data")
    state = settings.data_dir / "arcade"
    state.mkdir(parents=True)

    # managed_java_executable with pid <= 0
    (state / "process.json").write_text(json.dumps({"pid": 0}), encoding="utf-8")
    assert engine.managed_java_executable(settings) is None

    # install_engine: local distribution missing lib/
    bad_dist = tmp_path / "bad-dist"
    bad_dist.mkdir()
    with pytest.raises(ValueError, match="must contain lib"):
        engine.install_engine(settings, distribution=bad_dist)

    # install_engine: invalid arcade_version
    bad_ver = replace(settings, arcade_version="invalid-ver!")
    with pytest.raises(ValueError, match="Invalid ArcadeDB release version"):
        engine.install_engine(bad_ver)

    # install_engine: mocked download checksum mismatch -> missing lib/ -> valid tar.gz
    def make_tar(with_lib: bool) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            folder = f"arcadedb-{settings.arcade_version}"
            info = tarfile.TarInfo(f"{folder}/lib/server.jar" if with_lib else f"{folder}/README.txt")
            data = b"jar-bytes"
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        return buf.getvalue()

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    tar_no_lib = make_tar(False)
    monkeypatch.setattr(
        engine.urllib.request,
        "urlopen",
        lambda url, timeout=30: _Resp(b"0000badhash  file\n" if url.endswith(".sha256") else tar_no_lib),
    )
    with pytest.raises(ArcadeError, match="checksum mismatch"):
        engine.install_engine(settings)

    no_lib_hash = hashlib.sha256(tar_no_lib).hexdigest().encode()
    monkeypatch.setattr(
        engine.urllib.request,
        "urlopen",
        lambda url, timeout=30: _Resp(no_lib_hash + b"  file\n" if url.endswith(".sha256") else tar_no_lib),
    )
    with pytest.raises(ArcadeError, match="no server libraries"):
        engine.install_engine(settings)

    tar_ok = make_tar(True)
    ok_hash = hashlib.sha256(tar_ok).hexdigest().encode()
    monkeypatch.setattr(
        engine.urllib.request,
        "urlopen",
        lambda url, timeout=30: _Resp(ok_hash + b"  file\n" if url.endswith(".sha256") else tar_ok),
    )
    dest = engine.install_engine(settings)
    assert (dest / "lib" / "server.jar").exists()

    # engine_status: resolve_java raises ArcadeError, client raises 401
    monkeypatch.setattr(engine, "resolve_java", lambda s: (_ for _ in ()).throw(ArcadeError("no java")))

    class _AuthFailClient:
        def __init__(self, s):
            pass

        def server_info(self):
            raise ArcadeError("unauthorized", status_code=401)

        def close(self):
            pass

    monkeypatch.setattr(engine, "ArcadeClient", _AuthFailClient)
    st = engine.engine_status(settings)
    assert st["java_error"] == "no java" and st["sign_in_refused"] is True

    # engine_status: non-connect-only ValueError re-raised
    class _BadClient:
        def __init__(self, s):
            raise ValueError("broken setting")

    monkeypatch.setattr(engine, "ArcadeClient", _BadClient)
    with pytest.raises(ValueError, match="broken setting"):
        engine.engine_status(settings)


def test_database_credentials_validation_tls_doctor_and_create_user_edges(tmp_path, monkeypatch):
    assert dbcreds.is_loopback("http://[::1:bad") is False
    assert dbcreds.resolve_password({"arcade_password": 123}, environ={})[2] == "arcade_password in config.json must be text."

    s = Settings(data_dir=tmp_path / "data")
    assert "valid address" in dbcreds.problem(replace(s, arcade_url="http://[::1:bad"))
    assert "user name" in dbcreds.problem(replace(s, arcade_url="https://db.example.com", arcade_user="1bad", arcade_password="pw"))
    assert dbcreds.transport_label(replace(s, arcade_url="http://[::1:bad")) == "invalid"

    with pytest.raises(ValueError, match="absolute path"):
        dbcreds.tls_context(replace(s, arcade_ca_file="relative/ca.pem"))
    bad_ca = tmp_path / "bad.pem"
    bad_ca.write_text("not a pem", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot be loaded as PEM"):
        dbcreds.tls_context(replace(s, arcade_ca_file=str(bad_ca)))

    # doctor_block: ca_file recorded, query fails -> database_exists() -> missing_database
    conn_s = replace(
        s,
        arcade_connect_url="https://db.example.com",
        arcade_url="https://db.example.com",
        arcade_user="appuser",
        arcade_password="secret",
        arcade_ca_file=str(bad_ca),
    )

    class _FakeArcade:
        def __init__(self, st, administrator=False):
            self.st = st

        def _request(self, m, p, timeout=1.5):
            return SimpleNamespace(json=lambda: {"version": s.arcade_version, "serverName": "srv"})

        def server_info(self):
            return {"version": s.arcade_version}

        def query(self, sql):
            raise ArcadeError("no db", status_code=404)

        def database_exists(self):
            return False

        def server_command(self, cmd):
            if cmd.startswith("create user"):
                return {}
            if cmd.startswith("drop user"):
                return {}
            return {}

        def ensure_database(self):
            raise ArcadeError("cannot create db", status_code=500)

        def close(self):
            pass

    monkeypatch.setattr("cairnmind.storage.arcade.ArcadeClient", _FakeArcade)
    doc = dbcreds.doctor_block(conn_s)
    assert doc["ca_file"] == str(bad_ca) and doc["database_available"] is False and "error" in doc

    # _new_user refuses root
    with pytest.raises(ValueError, match="not root"):
        dbcreds._new_user(replace(s, arcade_user="root"))

    # plan_user version_problem raises
    conn_s._origin = {"password": "environment"}
    monkeypatch.setattr("cairnmind.storage.database_mode.version_problem", lambda st, info: "old server")
    with pytest.raises(ArcadeError, match="old server"):
        dbcreds.plan_user(conn_s, "adminpw")

    # create_user rolls back user when ensure_database fails
    monkeypatch.setattr("cairnmind.storage.database_mode.version_problem", lambda st, info: None)
    with pytest.raises(ArcadeError, match="was not created"):
        dbcreds.create_user(conn_s, "adminpw")


@pytest.mark.asyncio
async def test_repository_evaluation_ask_all_and_error_paths(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data")
    with pytest.raises(ValueError, match="not a directory"):
        repeval.run_repository_evaluation(settings, tmp_path / "missing", tmp_path / "q.json")

    # _ask_all when engine unavailable raises ArcadeError
    monkeypatch.setattr("cairnmind.storage.engine.engine_status", lambda s: {"available": False})
    with pytest.raises(ArcadeError, match="stopped during the evaluation"):
        await repeval._ask_all(settings, tmp_path, [], SimpleNamespace(calls=[]), {})

    # _ask_all happy path with lightweight fakes
    monkeypatch.setattr("cairnmind.storage.engine.engine_status", lambda s: {"available": True})

    class _FakeStore:
        def __init__(self, data_dir, settings=None):
            pass

        def add_project(self, name, root):
            return {"id": "p1"}

        def query(self, sql, params=None):
            return [{"id": "c1"}]

    class _FakeRetriever:
        def __init__(self, store, settings, ollama):
            self.embedded = []

        async def embed_pending(self, job):
            self.embedded.append(job)

    class _FakeAgent:
        def __init__(self, store, settings, ollama, retriever):
            pass

        async def ask(self, pid, question):
            return {"status": "answered", "answer": "yes", "sources": []}

    monkeypatch.setattr("cairnmind.store.Store", _FakeStore)
    monkeypatch.setattr("cairnmind.scip.ingest.index_project", lambda store, pid: {"files": 1, "errors": []})
    monkeypatch.setattr("cairnmind.agents.retrieval.Retriever", _FakeRetriever)
    monkeypatch.setattr("cairnmind.agents.agent.KnowledgeAgent", _FakeAgent)
    report = {"questions": []}
    case = {"id": "q1", "question": "Is it ok?", "file": "a.py", "accepted": ["yes"], "sources": []}
    await repeval._ask_all(settings, tmp_path, [case], SimpleNamespace(calls=[]), report)
    assert report["stage"] == "ask" and len(report["questions"]) == 1


def test_person_pipe_fail_closed_branches(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data")
    # Non-Windows fail-closed checks
    monkeypatch.setattr(person_pipe, "IS_WINDOWS", False)
    assert person_pipe._current_appcontainer_sid() is None
    assert person_pipe._process_owner_sid(123) is None
    with pytest.raises(OSError, match="only supported on Windows"):
        person_pipe.PersonPipeServer(tmp_path, "a" * 64)
    assert person_pipe.read_pipe_key(settings, person_pipe.pipe_name_for(tmp_path)) == ("unavailable", None)

    # Windows Win32 error branches via fake kernel32 (Win32 APIs exist only on Windows)
    import sys

    if sys.platform != "win32":
        return
    monkeypatch.setattr(person_pipe, "IS_WINDOWS", True)
    pname = person_pipe.pipe_name_for(settings.data_dir)

    class _FakeK32:
        def __init__(self, *, handle=10, busy=False, wait_ok=False, pid_ok=True, write_ok=True, read_ok=True, read_bytes=b"bad"):
            self.handle = handle
            self.busy = busy
            self.wait_ok = wait_ok
            self.pid_ok = pid_ok
            self.write_ok = write_ok
            self.read_ok = read_ok
            self.read_bytes = read_bytes

        def CreateFileW(self, *a):
            return person_pipe._INVALID_HANDLE_VALUE if self.busy else self.handle

        def WaitNamedPipeW(self, *a):
            return self.wait_ok

        def GetNamedPipeServerProcessId(self, h, ref):
            return self.pid_ok

        def WriteFile(self, *a):
            return self.write_ok

        def ReadFile(self, h, buf, n, read_ref, ov):
            if not self.read_ok:
                return False
            buf.value = self.read_bytes
            read_ref._obj.value = len(self.read_bytes)
            return True

        def CloseHandle(self, h):
            return True

    # Busy pipe + WaitNamedPipeW False
    monkeypatch.setattr(person_pipe, "_kernel32", _FakeK32(busy=True, wait_ok=False))
    monkeypatch.setattr(person_pipe.ctypes, "get_last_error", lambda: person_pipe._ERROR_PIPE_BUSY)
    assert person_pipe.read_pipe_key(settings, pname) == ("unavailable", None)

    # GetNamedPipeServerProcessId fails
    monkeypatch.setattr(person_pipe, "_kernel32", _FakeK32(pid_ok=False))
    assert person_pipe.read_pipe_key(settings, pname) == ("unavailable", None)

    # Mismatched owner SID
    monkeypatch.setattr(person_pipe, "_kernel32", _FakeK32())
    monkeypatch.setattr(person_pipe, "_process_owner_sid", lambda pid: "S-1-5-21-OTHER")
    monkeypatch.setattr(person_pipe, "current_user_sid", lambda: "S-1-5-21-ME")
    assert person_pipe.read_pipe_key(settings, pname) == ("unavailable", None)

    # WriteFile fails / ReadFile fails / invalid key payload
    monkeypatch.setattr(person_pipe, "_process_owner_sid", lambda pid: "S-1-5-21-ME")
    monkeypatch.setattr(person_pipe, "_kernel32", _FakeK32(write_ok=False))
    assert person_pipe.read_pipe_key(settings, pname) == ("unavailable", None)
    monkeypatch.setattr(person_pipe, "_kernel32", _FakeK32(read_ok=False))
    assert person_pipe.read_pipe_key(settings, pname) == ("unavailable", None)
    monkeypatch.setattr(person_pipe, "_kernel32", _FakeK32(read_bytes=b"not-64-hex\n"))
    assert person_pipe.read_pipe_key(settings, pname) == ("unavailable", None)
