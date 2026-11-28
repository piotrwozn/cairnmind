"""Edge-case tests for inspector.py and process_control.py (pure fast-tier)."""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from cairnmind.pal import process_control as pc
from cairnmind.terminal import inspector as insp


class _MiniInspectorStore:
    def __init__(self, root):
        self.root = str(root)
        self.rows = []

    def resolve_project(self, project):
        return {"id": "p1", "name": "P1", "root": self.root}

    def query(self, sql, params=None):
        if "project_roots" in sql:
            return [{"path": self.root}]
        if "FROM records WHERE" in sql and "id=:" in sql:
            return list(self.rows)
        if "FROM plans WHERE" in sql and "id=:" in sql:
            return [{"id": "pl1", "project_id": "p1", "version": 1, "metadata": {}}]
        if "FROM ingestion_inbox WHERE id=:" in sql:
            return []
        if "count(*)" in sql:
            return [{"n": 0}]
        return []

    @contextmanager
    def transaction(self):
        yield self


def test_inspector_cursors_validation_and_not_found_branches(tmp_path, monkeypatch):
    monkeypatch.setattr(insp, "_cursor", lambda reader, store: {"epoch": "e1", "seq": 1})
    # _decode_cursor error paths
    with pytest.raises(ValueError, match="cursor"):
        insp._decode_cursor(123)
    with pytest.raises(ValueError, match="cursor"):
        insp._decode_cursor("!!!not-base64!!!")
    with pytest.raises(ValueError, match="cursor"):
        insp._decode_cursor(insp._encode_cursor({"missing_after": 1}))

    store = _MiniInspectorStore(tmp_path)
    ins = insp.Inspector(store)

    # page: unknown collection, status filter, state filter on non-task
    with pytest.raises(ValueError, match="Unknown inspector collection"):
        ins.page("bogus", "p1")
    with pytest.raises(ValueError, match="Task state filter"):
        ins.page("records", "p1", kind="fact", state="todo")
    res = ins.page("jobs", "p1", status="pending")
    assert res["items"] == []

    # entity: unknown collection & missing entity
    with pytest.raises(ValueError, match="Unknown inspector entity collection"):
        ins.entity("bogus", "p1", "id1")
    with pytest.raises(insp.InspectorNotFound):
        ins.entity("jobs", "p1", "missing")

    # plan_revisions / revisions / inbox invalid cursor
    bad_cursor = insp._encode_cursor({"after": "bad", "query": "other"})
    with pytest.raises(ValueError, match="another plan"):
        ins.plan_revisions("p1", "pl1", cursor=bad_cursor)
    with pytest.raises(ValueError, match="another collection"):
        ins.inbox(cursor=bad_cursor)

    # source / inbox_source invalid view & negative offset & not found
    with pytest.raises(ValueError, match="Source view"):
        ins.source("p1", "s1", view="xml")
    with pytest.raises(ValueError, match="nonnegative"):
        ins.source("p1", "s1", offset=-1)
    with pytest.raises(ValueError, match="Inbox view"):
        ins.inbox_source("i1", view="normalized")
    with pytest.raises(ValueError, match="nonnegative"):
        ins.inbox_source("i1", offset=-5)
    with pytest.raises(insp.InspectorNotFound):
        ins.inbox_source("missing")

    # review validation errors and missing record
    with pytest.raises(ValueError, match="confirm or dispute"):
        ins.review("p1", "r1", "approve", 1)
    with pytest.raises(ValueError, match="exact record version"):
        ins.review("p1", "r1", "confirm", 0)
    with pytest.raises(ValueError, match="4000"):
        ins.review("p1", "r1", "confirm", 1, reason="x" * 4001)
    with pytest.raises(insp.InspectorNotFound):
        ins.review("p1", "missing", "confirm", 1)


def test_process_control_windows_and_posix_edge_branches(monkeypatch):
    # POSIX terminate_pid & terminate_group & ProcessJob
    killed = []
    monkeypatch.setattr(pc, "IS_WINDOWS", False)
    monkeypatch.setattr(pc.os, "kill", lambda pid, sig: killed.append(("kill", pid, sig)))
    monkeypatch.setattr(pc.os, "killpg", lambda pgid, sig: killed.append(("killpg", pgid, sig)), raising=False)
    monkeypatch.setattr(pc.signal, "SIGKILL", 9, raising=False)
    pc.terminate_pid(10, force=True)
    pc.terminate_group(20, force=False)
    assert ("kill", 10, 9) in killed and ("killpg", 20, pc.signal.SIGTERM) in killed

    job = pc.ProcessJob()
    assert job.running() is False
    job.terminate()  # _pid is None -> no-op
    assert job.attach(99) is True
    monkeypatch.setattr(pc.os, "killpg", lambda pgid, sig: (_ for _ in ()).throw(ProcessLookupError()), raising=False)
    assert job.running() is False
    job.terminate()  # ProcessLookupError -> returns cleanly

    # POSIX running with OSError + group_running, and terminate with timeout -> SIGKILL
    monkeypatch.setattr(pc.os, "killpg", lambda pgid, sig: (_ for _ in ()).throw(PermissionError()), raising=False)
    monkeypatch.setattr("cairnmind.pal.process_identity.group_running", lambda pgid: True)
    assert job.running() is True

    calls = []
    monkeypatch.setattr(pc.os, "killpg", lambda pgid, sig: calls.append(sig), raising=False)
    job.terminate(grace_seconds=0.01)
    assert pc.signal.SIGTERM in calls and 9 in calls

    # Windows branches with mocked _kernel32 (Win32 APIs exist only on Windows)
    import sys

    if sys.platform != "win32":
        return
    monkeypatch.setattr(pc, "IS_WINDOWS", True)

    class _FakeK32:
        def __init__(self, *, open_h=1, term_ok=True, job_h=1, set_ok=True, query_ok=True, last_err=0):
            self.open_h = open_h
            self.term_ok = term_ok
            self.job_h = job_h
            self.set_ok = set_ok
            self.query_ok = query_ok
            self.last_err = last_err
            self.closed = 0

        def OpenProcess(self, *a):
            return self.open_h

        def TerminateProcess(self, *a):
            return self.term_ok

        def CreateJobObjectW(self, *a):
            return self.job_h

        def SetInformationJobObject(self, *a):
            return self.set_ok

        def AssignProcessToJobObject(self, *a):
            return True

        def QueryInformationJobObject(self, *a):
            return self.query_ok

        def TerminateJobObject(self, *a):
            return True

        def CloseHandle(self, h):
            self.closed += 1
            return True

    # OpenProcess failure: 87 -> ProcessLookupError, 5 -> OSError
    monkeypatch.setattr(pc, "_kernel32", _FakeK32(open_h=0))
    monkeypatch.setattr(pc.ctypes, "get_last_error", lambda: 87)
    with pytest.raises(ProcessLookupError):
        pc.terminate_pid(123)
    monkeypatch.setattr(pc.ctypes, "get_last_error", lambda: 5)
    with pytest.raises(OSError):
        pc.terminate_pid(123)

    # TerminateProcess fails with ACCESS_DENIED on already-exited PID -> ProcessLookupError
    monkeypatch.setattr(pc, "_kernel32", _FakeK32(open_h=5, term_ok=False))
    monkeypatch.setattr("cairnmind.pal.process_identity.alive", lambda pid: False)
    with pytest.raises(ProcessLookupError):
        pc.terminate_pid(123)

    # ProcessJob CreateJobObjectW fail & SetInformationJobObject fail
    monkeypatch.setattr(pc, "_kernel32", _FakeK32(job_h=0))
    with pytest.raises(OSError):
        pc.ProcessJob()
    monkeypatch.setattr(pc, "_kernel32", _FakeK32(job_h=5, set_ok=False))
    with pytest.raises(OSError):
        pc.ProcessJob()

    # ProcessJob running ERROR_MORE_DATA -> True, other error -> None, handle=None -> False + fallback terminate
    k32 = _FakeK32(job_h=5, query_ok=False)
    monkeypatch.setattr(pc, "_kernel32", k32)
    wjob = pc.ProcessJob()
    monkeypatch.setattr(pc.ctypes, "get_last_error", lambda: pc._ERROR_MORE_DATA)
    assert wjob.running() is True
    monkeypatch.setattr(pc.ctypes, "get_last_error", lambda: 6)
    assert wjob.running() is None
    wjob.close()
    assert wjob.attach(12) is False
    assert wjob.running() is False
    wjob._pid = 12
    monkeypatch.setattr(pc, "terminate_pid", lambda pid, force=False: (_ for _ in ()).throw(ProcessLookupError()))
    wjob.terminate()
