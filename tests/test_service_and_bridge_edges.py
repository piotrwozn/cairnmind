"""Edge-case tests for service.py helpers/workers and terminal_bridge.py."""
from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

from cairnmind import service
from cairnmind.config import Settings
from cairnmind.terminal import terminal_bridge as tb


@pytest.mark.asyncio
async def test_service_inference_queue_and_background_helpers(tmp_path, monkeypatch):
    # InferenceQueue: cancelled queued future, cancelled running task, and normal submit
    q = service.InferenceQueue()
    runner = asyncio.create_task(q.run())
    try:
        assert await q.submit(lambda: asyncio.sleep(0, result="first"), priority=0) == "first"

        # Pre-cancelled future is skipped cleanly (line 46)
        cancelled_fut = asyncio.get_running_loop().create_future()
        cancelled_fut.cancel()
        await q.queue.put((0, 999, lambda: asyncio.sleep(0), cancelled_fut))
        assert await q.submit(lambda: asyncio.sleep(0, result="second"), priority=1) == "second"

        # Operation raising CancelledError on its own propagates & cancels future
        async def self_cancel():
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await q.submit(self_cancel, priority=0)
    finally:
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner

    # finish_job_if_present swallows missing job ValueError
    finished = []
    fake_store = SimpleNamespace(finish_job=lambda *a, **k: finished.append((a, k)))
    service.finish_job_if_present(fake_store, "j1", None, worker="w", expected_attempts=1)
    assert len(finished) == 1

    def missing_job(*a, **k):
        raise ValueError("Job does not exist")

    service.finish_job_if_present(
        SimpleNamespace(finish_job=missing_job, query=lambda *a, **k: []),
        "j2", "err", worker="w", expected_attempts=1,
    )

    # stale_temporaries returns list cleanly on directory
    bdir = tmp_path / "backups"
    bdir.mkdir()
    assert isinstance(service.stale_temporaries(bdir, 60), list)


@pytest.mark.asyncio
async def test_terminal_bridge_pane_status_and_raw_protocol_edges(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data")

    # PaneStatus init swallows WorkspaceManager error
    monkeypatch.setattr("cairnmind.terminal.workspace.WorkspaceManager", lambda *a, **k: (_ for _ in ()).throw(ValueError("no tmux")))
    ps = tb.PaneStatus(settings, "run-1", "default")
    assert ps.manager is None and ps.poll_nonce() is None

    # PaneStatus with fake manager: poll_nonce error / valid -> state error / valid -> auth_finished error / valid
    auth_calls = []

    class _FakeMgr:
        def __init__(self):
            self.fail = False

        def tmux(self, *args, check=False):
            if self.fail:
                raise OSError("tmux dead")
            if args[0] == "display-message":
                return SimpleNamespace(returncode=0, stdout="default\trun-1\tnonce-1\n")
            return SimpleNamespace(returncode=0, stdout="")

        def auth_finished(self, rid):
            if self.fail:
                raise OSError("auth fail")
            auth_calls.append(rid)

    mgr = _FakeMgr()
    ps.manager = mgr
    ps.pane = "%1"
    assert ps.poll_nonce() == "nonce-1"
    ps.state("controller")
    assert ps.previous == "controller"
    ps.auth_finished()
    assert auth_calls == ["run-1"]
    mgr.fail = True
    assert ps.poll_nonce() is None
    ps.previous = None
    ps.state("readonly")
    ps.auth_finished()

    # RawTerminalBridge validation errors
    with pytest.raises(ValueError, match="run ID"):
        tb.RawTerminalBridge(None, "../bad", 1)
    with pytest.raises(ValueError, match="generation"):
        tb.RawTerminalBridge(None, "run-1", 0)

    # _state swallows pane error; _output handles written==0 & BlockingIOError
    bad_pane = SimpleNamespace(state=lambda v: (_ for _ in ()).throw(OSError("bad")), poll_nonce=lambda: None)
    bridge = tb.RawTerminalBridge(None, "run-1", 1, input_fd=0, output_fd=1, pane=bad_pane)
    await bridge._state("readonly")

    monkeypatch.setattr(tb.os, "write", lambda fd, view: 0)
    with pytest.raises(OSError, match="output closed"):
        await bridge._output(b"hi")

    # _connection receive protocol error & status branches
    class _FakeWS:
        def __init__(self, frames):
            self.frames = list(frames)
            self.sent = []

        async def send(self, data):
            self.sent.append(json.loads(data))

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.frames:
                raise StopAsyncIteration
            return self.frames.pop(0)

    monkeypatch.setattr(tb, "terminal_dimensions", lambda fd: (80, 24))

    async def _run_receive(frames):
        client = SimpleNamespace(request=lambda *a, **k: {"generation": 2, "lifecycle": "running"})
        b = tb.RawTerminalBridge(client, "run-1", 2, input_fd=0, output_fd=1, pane=None)
        monkeypatch.setattr(b, "_ready", lambda fd, write=False: asyncio.sleep(99))
        monkeypatch.setattr(b, "_flush_input", lambda: None)
        monkeypatch.setattr(b, "_output", lambda data: asyncio.sleep(0))
        await b._connection(_FakeWS(frames))
        return b

    # Stale generation ends cleanly
    b_stale = await _run_receive([json.dumps({"type": "status", "run": {"generation": 1, "lifecycle": "running"}})])
    assert b_stale.finished is True and b_stale.final_state == "stale"

    # Control + valid output + ended lifecycle
    b_ok = await _run_receive([
        json.dumps({"type": "control", "controller": True}),
        json.dumps({"type": "output", "encoding": "base64", "data": base64.b64encode(b"hello").decode()}),
        json.dumps({"type": "status", "run": {"generation": 2, "lifecycle": "exited", "exit_code": 0}}),
    ])
    assert b_ok.finished is True and b_ok.final_state == "exited"

    # Protocol rejections: non-str frame, non-dict JSON, invalid output encoding, oversized output, gap, error
    for bad_frame, exc_type in [
        (b"binary", ValueError),
        ("[]", ValueError),
        (json.dumps({"type": "output", "encoding": "plain", "data": "x"}), ValueError),
        (json.dumps({"type": "output", "encoding": "base64", "data": base64.b64encode(b"x" * 70000).decode()}), ValueError),
        (json.dumps({"type": "gap"}), OSError),
        (json.dumps({"type": "error"}), ValueError),
    ]:
        with pytest.raises(exc_type):
            await _run_receive([bad_frame])
