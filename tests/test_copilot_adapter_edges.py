"""Fast-tier edge-case coverage tests for cairnmind.copilot_adapter."""
from __future__ import annotations

import asyncio
import importlib.metadata
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from cairnmind import copilot_adapter as ca
from cairnmind.config import Settings
from cairnmind.copilot_adapter import CopilotAdapter, CopilotError


def test_runtime_status_prepare_and_worker_edge_cases(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data", create=True)

    # 1. SDK missing
    def raise_not_found(_name):
        raise importlib.metadata.PackageNotFoundError("github-copilot-sdk")

    monkeypatch.setattr(importlib.metadata, "version", raise_not_found)
    st = ca.runtime_status(settings)
    assert st["available"] is False and st["code"] == "copilot_sdk_missing"

    # 2. SDK unsupported version
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "0.0.1")
    st2 = ca.runtime_status(settings)
    assert st2["available"] is False and st2["code"] == "copilot_sdk_unsupported"

    # 3. _prepare_runtime_locked failure paths (non-zero rc, OSError)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="err"),
    )
    with pytest.raises(CopilotError, match="copilot_prepare_failed"):
        ca._prepare_runtime_locked(settings)

    def raise_oserror(*_a, **_k):
        raise OSError("spawn failed")

    monkeypatch.setattr(subprocess, "run", raise_oserror)
    with pytest.raises(CopilotError, match="copilot_prepare_failed"):
        ca._prepare_runtime_locked(settings)

    # 4. _prepare_worker happy path + replace existing bundle + version mismatch
    fake_dl = SimpleNamespace(download_cli=None)
    fake_ver = SimpleNamespace(CLI_VERSION=ca.RUNTIME_VERSION)
    monkeypatch.setitem(sys.modules, "copilot._cli_download", fake_dl)
    monkeypatch.setitem(sys.modules, "copilot._cli_version", fake_ver)
    monkeypatch.setattr(ca, "_sdk", lambda: None)

    def fake_download(version, force=True):
        extract_dir = Path(ca.os.environ["COPILOT_CLI_EXTRACT_DIR"])
        ep = extract_dir / "copilot-bin"
        ep.write_bytes(b"#!/bin/sh\necho ok\n")
        return str(ep)

    fake_dl.download_cli = fake_download
    ca._prepare_worker(settings)
    assert (ca._runtime_root(settings) / "receipt.json").is_file()
    # Second call replaces existing bundle via previous backup path
    ca._prepare_worker(settings)
    assert (ca._runtime_root(settings) / ca.RUNTIME_VERSION / "copilot-bin").is_file()

    # Mismatched CLI_VERSION refused
    fake_ver.CLI_VERSION = "0.0.0-bad"
    with pytest.raises(CopilotError, match="copilot_sdk_unsupported"):
        ca._prepare_worker(settings)

    # Invalid session ID refused
    with pytest.raises(ValueError, match="Invalid Copilot session ID"):
        CopilotAdapter({"client": "copilot"}, session_id="bad id!")


@pytest.mark.asyncio
async def test_copilot_adapter_events_deltas_turn_errors_and_login(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data", create=True)
    adapter = CopilotAdapter({"client": "copilot"}, key="SECRET1234", settings=settings)

    # _notify with both sync and async callback
    seen = []
    await ca._notify(lambda v: seen.append(("sync", v)), 1)

    async def async_cb(v):
        seen.append(("async", v))

    await ca._notify(async_cb, 2)
    assert seen == [("sync", 1), ("async", 2)]

    # _event ignores subagent events, handles session.error, _enqueue when None / QueueFull
    adapter._enqueue(("delta", "ignored"))  # self._events is None
    adapter._events = asyncio.Queue(maxsize=1)
    adapter._event({"type": "assistant.message_delta", "agent_id": "sub-1", "data": {"delta_content": "skip"}})
    assert adapter._events.empty()
    adapter._event({"type": "session.error", "data": {"message": "rate limit exceeded"}})
    assert adapter._limit_code is not None
    adapter._limit_code = None
    adapter._enqueue(("delta", "a"))
    adapter._enqueue(("delta", "b"))  # triggers QueueFull -> _set_limit
    assert adapter._limit_code == "copilot_output_limit"
    if adapter._callbacks:
        await asyncio.gather(*adapter._callbacks, return_exceptions=True)

    # _emit_delta redacts split secret across chunks & enforces _MAX_TEXT
    emitted = []
    adapter._on_delta = emitted.append
    adapter._delta_buffer = ""
    adapter._limit_code = None
    await adapter._emit_delta("hi SECR")
    await adapter._emit_delta("ET1234 bye")
    assert "SECRET1234" not in "".join(emitted)
    monkeypatch.setattr(ca, "_MAX_TEXT", 5)
    await adapter._emit_delta("0123456789")
    assert adapter._limit_code == "copilot_output_limit"
    monkeypatch.setattr(ca, "_MAX_TEXT", 200_000)

    # run_turn argument validation & busy check
    adapter._active = True
    with pytest.raises(CopilotError, match="copilot_session_busy"):
        await adapter.run_turn("hi", [], lambda *a: None)
    adapter._active = False
    with pytest.raises(ValueError, match="Invalid Copilot turn"):
        await adapter.run_turn(123, [], lambda *a: None)
    with pytest.raises(ValueError, match="Invalid Copilot custom tool schema"):
        await adapter.run_turn("hi", [{"name": "bad name!", "parameters": {}, "description": "d"}], lambda *a: None)

    # _login flow covering device-code, open-url, input-required, needs-plaintext-consent, completed & error
    class FakeRpcReq:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    fake_rpc_mod = SimpleNamespace(
        AuthLoginAdvanceRequest=FakeRpcReq,
        AuthLoginBeginRequest=FakeRpcReq,
        AuthLoginCancelRequest=FakeRpcReq,
        AuthLoginMethod=SimpleNamespace(DEVICE_CODE="device"),
        LoginProviderKind=SimpleNamespace(GITHUB_DOT_COM="gh"),
    )
    monkeypatch.setitem(sys.modules, "copilot.generated.rpc", fake_rpc_mod)

    steps = iter([
        SimpleNamespace(kind="open-url", url="https://github.com/login"),
        SimpleNamespace(kind="input-required", prompt="OTP:"),
        SimpleNamespace(kind="completed", result=SimpleNamespace(status="needs-plaintext-consent")),
        SimpleNamespace(kind="completed", result=SimpleNamespace(status="completed")),
    ])

    class FakeLoginApi:
        async def begin(self, _req, timeout=30):
            return SimpleNamespace(flow_id="flow-1", step=SimpleNamespace(kind="device-code", verification_uri="https://gh/d", user_code="ABCD"))

        async def advance(self, _req, timeout=300):
            return next(steps)

        async def cancel(self, _req, timeout=5):
            pass

    class FakeSession:
        def __init__(self):
            self.rpc = SimpleNamespace(accounts=SimpleNamespace(login=FakeLoginApi()))

        async def abort(self):
            pass

        async def disconnect(self):
            pass

    async def fake_ensure_client(self):
        self._client = SimpleNamespace(stop=lambda: asyncio.sleep(0))

    async def fake_ensure_session(self, _tools, _sys):
        self._session = FakeSession()

    monkeypatch.setattr(CopilotAdapter, "_ensure_client", fake_ensure_client)
    monkeypatch.setattr(CopilotAdapter, "_ensure_session", fake_ensure_session)
    answers = iter(["123456", "yes"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    await ca._login(settings)

    # Error step in _login
    class ErrLoginApi(FakeLoginApi):
        async def begin(self, _req, timeout=30):
            return SimpleNamespace(flow_id="flow-2", step=SimpleNamespace(kind="error"))

    async def fake_ensure_err_session(self, _tools, _sys):
        sess = FakeSession()
        sess.rpc = SimpleNamespace(accounts=SimpleNamespace(login=ErrLoginApi()))
        self._session = sess

    monkeypatch.setattr(CopilotAdapter, "_ensure_session", fake_ensure_err_session)
    with pytest.raises(CopilotError, match="copilot_auth_rejected"):
        await ca._login(settings)

    # main() CLI entrypoints: prepare-worker, CopilotError, KeyboardInterrupt, generic Exception
    monkeypatch.setattr(sys, "argv", ["copilot_adapter", "--data-dir", str(settings.data_dir), "--prepare-worker"])
    called = []
    monkeypatch.setattr(ca, "_prepare_worker", lambda s: called.append(s.data_dir))
    ca.main()
    assert called == [settings.data_dir]

    monkeypatch.setattr(ca, "_prepare_worker", lambda s: (_ for _ in ()).throw(CopilotError("copilot_failed")))
    with pytest.raises(SystemExit) as exc1:
        ca.main()
    assert exc1.value.code == 1

    monkeypatch.setattr(ca, "_prepare_worker", lambda s: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(SystemExit) as exc2:
        ca.main()
    assert exc2.value.code == 1

    monkeypatch.setattr(ca, "_prepare_worker", lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(SystemExit) as exc3:
        ca.main()
    assert exc3.value.code == 1
