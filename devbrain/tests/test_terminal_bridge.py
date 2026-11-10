"""Raw bytes, terminal restoration and explicit leases without an ANSI emulator."""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
import fcntl
import json
import os
from pathlib import Path
import signal
import struct
import termios
from types import SimpleNamespace

import pytest

from devbrain.config import Settings
from devbrain import native_worker
from devbrain.terminal_bridge import PaneStatus, RawTerminalBridge, raw_terminal, terminal_dimensions


class Pane:
    def __init__(self):
        self.nonce = ""
        self.states = []
        self.auth_closed = False

    def poll_nonce(self):
        return self.nonce

    def state(self, value):
        self.states.append(value)

    def auth_finished(self):
        self.auth_closed = True


class Socket:
    def __init__(self, controller):
        self.messages = asyncio.Queue()
        self.messages.put_nowait(json.dumps({"type": "control", "controller": controller}))
        self.messages.put_nowait(json.dumps({"type": "output", "encoding": "base64",
            "data": base64.b64encode(b"\x1b[31mNATIVE_SCREEN\x1b[0m").decode()}))
        self.sent = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        value = await self.messages.get()
        if value is None:
            raise StopAsyncIteration
        return value

    async def send(self, value):
        frame = json.loads(value)
        self.sent.append(frame)
        if frame["type"] == "takeover":
            self.messages.put_nowait(json.dumps({"type": "control", "controller": True}))


class Client:
    def __init__(self, controller=True):
        self.controller = controller
        self.requests = []
        self.sockets = []
        self.connections = []
        self.lifecycle = "running"
        self.generation = 7
        self.purpose = "coding"

    def reconnect(self):
        pass

    def request(self, method, path, **kwargs):
        assert method == "GET" and path == "/api/client-runs/run-fixture"
        self.requests.append((method, path))
        return {"id": "run-fixture", "generation": self.generation,
                "lifecycle": self.lifecycle, "purpose": self.purpose}

    @asynccontextmanager
    async def terminal(self, run_id, generation):
        self.connections.append((run_id, generation))
        socket = Socket(self.controller)
        self.sockets.append(socket)
        yield socket


@pytest.fixture
def pty_pair():
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 31, 101, 0, 0))
    os.set_blocking(master, False)
    try:
        yield master, slave
    finally:
        os.close(master)
        os.close(slave)


async def wait_for(predicate, timeout=4):
    end = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= end:
            raise TimeoutError("Controlled terminal condition was not reached")
        await asyncio.sleep(.01)


async def read_terminal(master, *expected, timeout=4):
    """Everything the bridge wrote, read from the pty master.

    The kernel moves bytes from the slave side to the master side asynchronously, so one
    os.read() right after the writer finished can return only the first part of the
    output. Read until every expected text arrived, then until the output stays quiet.
    """
    loop = asyncio.get_running_loop()
    needles = [item.encode("utf-8") if isinstance(item, str) else item for item in expected]
    data, end, quiet_since = b"", loop.time() + timeout, None
    blocking = os.get_blocking(master)
    os.set_blocking(master, False)
    try:
        while True:
            try:
                chunk = os.read(master, 65536)
            except BlockingIOError:
                chunk = b""
            now = loop.time()
            if chunk:
                data, quiet_since = data + chunk, None
                continue
            if all(needle in data for needle in needles):
                quiet_since = quiet_since or now
                if now - quiet_since >= .2:
                    return data
            if now >= end:
                return data  # the caller's assertions report what is missing
            await asyncio.sleep(.01)
    finally:
        os.set_blocking(master, blocking)


def input_bytes(socket):
    return b"".join(base64.b64decode(frame["data"]) for frame in socket.sent if frame["type"] == "input")


def test_raw_context_restores_exact_terminal_state_after_exception(pty_pair):
    _, slave = pty_pair
    attributes = termios.tcgetattr(slave)
    flags = fcntl.fcntl(slave, fcntl.F_GETFL)
    with pytest.raises(RuntimeError):
        with raw_terminal(slave, slave):
            assert not termios.tcgetattr(slave)[3] & termios.ISIG
            assert not os.get_blocking(slave)
            raise RuntimeError("controlled interruption")
    assert termios.tcgetattr(slave) == attributes
    assert fcntl.fcntl(slave, fcntl.F_GETFL) == flags


@pytest.mark.parametrize(("columns", "rows", "expected"), [(10, 2, (20, 5)), (600, 250, (500, 200)), (99, 31, (99, 31))])
def test_resize_frames_always_fit_broker_bounds(pty_pair, columns, rows, expected):
    _, slave = pty_pair
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
    assert terminal_dimensions(slave) == expected


@pytest.mark.asyncio
async def test_raw_binary_keyboard_output_and_native_exit_restore_terminal(pty_pair):
    master, slave = pty_pair
    client, pane = Client(), Pane()
    original = termios.tcgetattr(slave)
    flags = fcntl.fcntl(slave, fcntl.F_GETFL)
    handler = signal.getsignal(signal.SIGINT)
    bridge = RawTerminalBridge(client, "run-fixture", 7, input_fd=slave, output_fd=slave, pane=pane)
    task = asyncio.create_task(bridge.run())
    try:
        await wait_for(lambda: bridge.controller and client.sockets)
        raw = "żółć".encode() + b"\x03\x1b[200~paste\x1b[201~\x00"
        os.write(master, raw)
        await wait_for(lambda: raw in input_bytes(client.sockets[0]))
        rendered = await read_terminal(master, b"\x1b[31mNATIVE_SCREEN\x1b[0m")
        assert b"\x1b[31mNATIVE_SCREEN\x1b[0m" in rendered
        await wait_for(lambda: any(f["type"] == "resize" for f in client.sockets[0].sent))
        assert {"type": "resize", "cols": 101, "rows": 31} in client.sockets[0].sent
        client.lifecycle = "exited"
        assert await asyncio.wait_for(task, 4) == 0
    finally:
        bridge.stopped.set()
        await asyncio.wait_for(task, 4)
    assert termios.tcgetattr(slave) == original
    assert fcntl.fcntl(slave, fcntl.F_GETFL) == flags
    assert signal.getsignal(signal.SIGINT) == handler
    assert pane.states[-1] == "exited"


@pytest.mark.asyncio
async def test_observer_discards_input_and_only_fresh_explicit_nonce_takes_control(pty_pair):
    master, slave = pty_pair
    client, pane = Client(False), Pane()
    bridge = RawTerminalBridge(client, "run-fixture", 7, input_fd=slave, output_fd=slave, pane=pane)
    task = asyncio.create_task(bridge.run())
    try:
        await wait_for(lambda: client.sockets and "readonly" in pane.states)
        os.write(master, b"not-authorized")
        await asyncio.sleep(.1)
        assert not client.sockets[0].sent
        pane.nonce = "explicit-user-request"
        await wait_for(lambda: bridge.controller)
        os.write(master, b"authorized")
        await wait_for(lambda: b"authorized" in input_bytes(client.sockets[0]))
        assert b"not-authorized" not in input_bytes(client.sockets[0])
        client.sockets[0].messages.put_nowait(None)
        await wait_for(lambda: len(client.sockets) == 2)
        await asyncio.sleep(.35)
        assert not bridge.controller
        assert not any(frame["type"] == "takeover" for frame in client.sockets[1].sent)
        assert client.connections == [("run-fixture", 7), ("run-fixture", 7)]
    finally:
        bridge.stopped.set()
        await asyncio.wait_for(task, 4)


@pytest.mark.asyncio
async def test_gap_reconnect_never_replays_offline_input_or_starts_a_model(pty_pair):
    master, slave = pty_pair
    client, pane = Client(), Pane()
    bridge = RawTerminalBridge(client, "run-fixture", 7, input_fd=slave, output_fd=slave, pane=pane)
    task = asyncio.create_task(bridge.run())
    try:
        await wait_for(lambda: bridge.controller)
        client.sockets[0].messages.put_nowait(json.dumps({"type": "gap"}))
        await wait_for(lambda: pane.states and pane.states[-1] == "offline")
        os.write(master, b"must-not-be-replayed")
        await wait_for(lambda: len(client.sockets) == 2 and bridge.controller)
        await asyncio.sleep(.1)
        assert not input_bytes(client.sockets[1])
        assert client.connections == [("run-fixture", 7), ("run-fixture", 7)]
        assert all(method == "GET" for method, _ in client.requests)
    finally:
        bridge.stopped.set()
        await asyncio.wait_for(task, 4)


@pytest.mark.asyncio
async def test_stale_generation_and_non_tty_do_not_attach(pty_pair):
    _, slave = pty_pair
    client = Client()
    client.generation = 8
    bridge = RawTerminalBridge(client, "run-fixture", 7, input_fd=slave, output_fd=slave)
    assert await bridge.run() == 1
    assert client.connections == []
    read, write = os.pipe()
    try:
        assert await RawTerminalBridge(client, "run-fixture", 7, input_fd=read, output_fd=write).run() == 2
    finally:
        os.close(read)
        os.close(write)


@pytest.mark.asyncio
async def test_operational_auth_exit_notifies_workspace_after_restoration(pty_pair):
    _, slave = pty_pair
    original = termios.tcgetattr(slave)
    client, pane = Client(), Pane()
    client.purpose, client.lifecycle = "auth", "exited"
    pane.auth_finished = lambda: setattr(pane, "auth_closed", termios.tcgetattr(slave) == original)
    bridge = RawTerminalBridge(client, "run-fixture", 7, input_fd=slave, output_fd=slave, pane=pane)
    assert await bridge.run() == 0
    assert pane.auth_closed


def test_outer_pane_status_never_targets_unverified_workspace(tmp_path, monkeypatch):
    settings = Settings(tmp_path / "data")
    manager = SimpleNamespace(socket=Path("/tmp/owned-ui.sock"), tmux=None)
    calls = []
    tag = {"value": "foreign\trun-fixture\trequest"}
    def tmux(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=tag["value"] + "\n")
    manager.tmux = tmux
    manager.auth_finished = lambda *args: pytest.fail("Unverified auth pane was closed")
    monkeypatch.setattr("devbrain.workspace.WorkspaceManager", lambda *args, **kwargs: manager)
    monkeypatch.setenv("TMUX", "/tmp/not-our-socket,123,0")
    monkeypatch.setenv("TMUX_PANE", "%7")
    pane = PaneStatus(settings, "run-fixture", "default")
    pane.state("controller")
    assert calls == []
    monkeypatch.setenv("TMUX", "/tmp/owned-ui.sock,123,0")
    pane = PaneStatus(settings, "run-fixture", "default")
    pane.state("controller")
    pane.auth_finished()
    assert all(command[0] == "display-message" for command in calls)
    tag["value"] = "default\trun-fixture\tfresh-nonce"
    assert pane.poll_nonce() == "fresh-nonce"
    pane.state("readonly")
    assert calls[-1] == ("set-option", "-p", "-t", "%7", "@devbrain_control", "readonly")


@pytest.mark.parametrize("inner_term", ["tmux-256color", None])
def test_native_worker_rebinds_actual_inner_tmux_identity(tmp_path, monkeypatch, inner_term):
    spec = tmp_path / "fixture.launch.json"
    spec.write_text(json.dumps({"run_id": "run-fixture", "generation": 1, "cwd": str(tmp_path),
        "command": ["/fixture/native"], "environment": {"TMUX": "/outer/socket,1,0", "TMUX_PANE": "%8",
                                                       "TERM": "xterm-256color", "CODEX_HOME": "/fixture/config"}}))
    spec.chmod(0o600)
    monkeypatch.setenv("TMUX", "/inner/socket,2,0")
    monkeypatch.setenv("TMUX_PANE", "%3")
    if inner_term is None:
        monkeypatch.delenv("TERM", raising=False)
    else:
        monkeypatch.setenv("TERM", inner_term)
    monkeypatch.setattr("sys.argv", ["worker", "--spec", str(spec)])
    monkeypatch.setattr(native_worker.os, "chdir", lambda path: None)
    seen = []
    def executed(binary, argv, environment):
        seen.append(environment)
        raise SystemExit(0)
    monkeypatch.setattr(native_worker.os, "execve", executed)
    with pytest.raises(SystemExit):
        native_worker.main()
    expected = {"TMUX": "/inner/socket,2,0", "TMUX_PANE": "%3", "CODEX_HOME": "/fixture/config"}
    if inner_term is not None:
        expected["TERM"] = inner_term
    assert seen[0] == expected
    assert not spec.exists()


def test_native_attach_helper_drops_outer_tmux_markers(tmp_path, monkeypatch):
    tmux = tmp_path / "tmux"
    tmux.write_text("#!/bin/sh\nexit 99\n")
    tmux.chmod(0o700)
    monkeypatch.setenv("TMUX", "/outer/socket,1,0")
    monkeypatch.setenv("TMUX_PANE", "%8")
    monkeypatch.setattr("sys.argv", ["worker", "--attach", "--tmux-bin", str(tmux),
        "--socket", "/inner/socket", "--session", "private"])
    monkeypatch.setattr(native_worker.fcntl, "ioctl", lambda *args: None)
    seen = []
    def executed(binary, argv, environment):
        seen.append(environment)
        raise SystemExit(0)
    monkeypatch.setattr(native_worker.os, "execve", executed)
    with pytest.raises(SystemExit):
        native_worker.main()
    assert "TMUX" not in seen[0] and "TMUX_PANE" not in seen[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
@pytest.mark.parametrize("state", ["failed", "interrupted", "stale"])
@pytest.mark.parametrize("path", ["initial_get", "websocket_status", "status_poll"])
async def test_terminal_failure_feedback_survives_each_exit_path_after_restore(pty_pair, language, state, path):
    from devbrain.terminal_i18n import native_failure_notice
    master, slave = pty_pair
    original = termios.tcgetattr(slave)
    client, pane = Client(), Pane()
    request = client.request
    def metadata(*args, **kwargs):
        return {**request(*args, **kwargs), "client": "codex", "exit_code": 1,
                "error": "PRIVATE_NATIVE_TEXT_MUST_NOT_BE_REFLECTED"}
    client.request = metadata
    if path == "initial_get":
        if state == "stale":
            client.generation = 8
        else:
            client.lifecycle = state
    bridge = RawTerminalBridge(client, "run-fixture", 7, input_fd=slave, output_fd=slave,
                               pane=pane, language=language)
    output = bridge._final_output
    final_writes = []
    async def observed_output(data):
        if data.startswith(b"\r\n\x18\x1b[0m"):
            final_writes.append((data, termios.tcgetattr(slave)))
        return await output(data)
    bridge._final_output = observed_output
    task = asyncio.create_task(bridge.run())
    try:
        if path != "initial_get":
            await wait_for(lambda: bridge.controller and client.sockets)
            if path == "websocket_status":
                client.sockets[0].messages.put_nowait(json.dumps({"type": "status", "run": {
                    "generation": 8 if state == "stale" else 7, "client": "codex",
                    "lifecycle": "running" if state == "stale" else state, "exit_code": 1,
                    "error": "PRIVATE_NATIVE_TEXT_MUST_NOT_BE_REFLECTED"}}))
            elif state == "stale":
                client.generation = 8
            else:
                client.lifecycle = state
        await asyncio.wait_for(task, 4)
        expected = native_failure_notice({"client": "codex", "lifecycle": state, "exit_code": 1}, language)
        rendered = (await read_terminal(master, expected)).decode("utf-8")
        assert expected and expected in rendered
        assert len(final_writes) == 1 and final_writes[0][1] == original
        assert "PRIVATE_NATIVE_TEXT_MUST_NOT_BE_REFLECTED" not in rendered
        assert pane.states[-1] == state
        assert len(client.connections) <= 1
        assert all(method == "GET" for method, _ in client.requests)
        assert termios.tcgetattr(slave) == original
    finally:
        bridge.stopped.set()
        await asyncio.wait_for(task, 4)


@pytest.mark.parametrize("code", [True, -1, 256, "1", {}, None])
def test_failure_feedback_does_not_reflect_unknown_client_or_unbounded_exit_code(code):
    from devbrain.terminal_i18n import native_failure_notice
    text = native_failure_notice({"client": "PRIVATE_CLIENT\x1b]52;value", "lifecycle": "failed",
                                  "exit_code": code, "error": "PRIVATE_ERROR"})
    assert text.startswith("Assistant failed.")
    assert "PRIVATE" not in text and "\x1b" not in text and "Exit code:" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
@pytest.mark.parametrize("path", ["initial_get", "websocket_status", "status_poll"])
async def test_auth_failure_keeps_connections_guidance_and_existing_post_restore_cleanup(pty_pair, language, path):
    from devbrain.terminal_i18n import tr
    master, slave = pty_pair
    original = termios.tcgetattr(slave)
    client, pane = Client(), Pane()
    client.purpose = "auth"
    if path == "initial_get":
        client.lifecycle = "failed"
    pane.auth_finished = lambda: setattr(pane, "auth_closed", termios.tcgetattr(slave) == original)
    bridge = RawTerminalBridge(client, "run-fixture", 7, input_fd=slave, output_fd=slave,
                               pane=pane, language=language)
    task = asyncio.create_task(bridge.run())
    try:
        if path != "initial_get":
            await wait_for(lambda: bridge.controller and client.sockets)
            if path == "websocket_status":
                client.sockets[0].messages.put_nowait(json.dumps({"type": "status", "run": {
                    "generation": 7, "purpose": "auth", "lifecycle": "failed",
                    "error": "PRIVATE_DEVICE_CODE_MUST_NOT_BE_REFLECTED"}}))
            else:
                client.lifecycle = "failed"
        await asyncio.wait_for(task, 4)
        guidance = tr("Open Connections from the menu (Ctrl+B Space) to review account sign-in.", language)
        rendered = (await read_terminal(master, guidance)).decode("utf-8")
        assert guidance in rendered
        assert tr("Start work", language) not in rendered
        assert "PRIVATE_DEVICE_CODE_MUST_NOT_BE_REFLECTED" not in rendered
        assert pane.auth_closed and pane.states[-1] == "failed"
        assert all(method == "GET" for method, _ in client.requests)
    finally:
        bridge.stopped.set()
        await asyncio.wait_for(task, 4)


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
@pytest.mark.parametrize("status", [404, 409])
async def test_stale_broker_rejection_shows_fixed_feedback_without_reattach(pty_pair, language, status):
    from devbrain.terminal_client import BrokerError
    from devbrain.terminal_i18n import tr
    master, slave = pty_pair
    original = termios.tcgetattr(slave)
    client = Client()
    def rejected():
        raise BrokerError(status, "PRIVATE_BROKER_DETAIL")
    client.reconnect = rejected
    bridge = RawTerminalBridge(client, "run-fixture", 7, input_fd=slave, output_fd=slave, language=language)
    assert await asyncio.wait_for(bridge.run(), 2) == 1
    rendered = (await read_terminal(
        master, tr("The session changed.", language),
        tr("Open the menu with Ctrl+B Space to review the current session.", language))).decode("utf-8")
    assert tr("The session changed.", language) in rendered
    assert tr("Open the menu with Ctrl+B Space to review the current session.", language) in rendered
    assert "PRIVATE_BROKER_DETAIL" not in rendered and not client.connections
    assert termios.tcgetattr(slave) == original


def test_full_unread_terminal_cannot_block_final_feedback_or_terminal_restoration():
    # A separate controlled child gives this regression a real external deadline;
    # asyncio.wait_for alone cannot interrupt a blocking os.write regression.
    import subprocess
    import sys
    import textwrap
    script = textwrap.dedent("""
        import asyncio, fcntl, json, os, termios, time
        from devbrain.terminal_bridge import RawTerminalBridge, raw_terminal
        async def main():
            master, slave = os.openpty()
            original = termios.tcgetattr(slave)
            flags = fcntl.fcntl(slave, fcntl.F_GETFL)
            try:
                with raw_terminal(slave, slave):
                    pass
                os.set_blocking(slave, False)
                blocked = False
                for _ in range(64):
                    try: os.write(slave, b'x' * 65536)
                    except BlockingIOError:
                        blocked = True
                        break
                assert blocked, 'The controlled PTY must really be full'
                fcntl.fcntl(slave, fcntl.F_SETFL, flags)
                ticks = []
                async def heartbeat():
                    while True:
                        ticks.append(time.monotonic())
                        await asyncio.sleep(.01)
                pulse = asyncio.create_task(heartbeat())
                started = time.monotonic()
                try:
                    bridge = RawTerminalBridge(object(), 'run-fixture', 7, input_fd=slave, output_fd=slave)
                    # More than a PTY can buffer keeps this test deterministic
                    # even if the driver briefly makes a few more bytes writable.
                    written = await bridge._final_output(b'FIXED_FINAL_NOTICE' * 65536)
                finally:
                    pulse.cancel()
                    await asyncio.gather(pulse, return_exceptions=True)
                elapsed = time.monotonic() - started
                assert written is False and elapsed < .8 and len(ticks) >= 2
                assert termios.tcgetattr(slave) == original
                assert fcntl.fcntl(slave, fcntl.F_GETFL) == flags
                print(json.dumps({'elapsed': elapsed, 'heartbeat_ticks': len(ticks), 'restored': True}))
            finally:
                os.close(master)
                os.close(slave)
        asyncio.run(main())
    """)
    child = subprocess.run([sys.executable, "-I", "-B", "-c", script],
                           capture_output=True, text=True, timeout=3)
    assert child.returncode == 0, child.stderr
    observed = json.loads(child.stdout)
    assert observed["restored"] and observed["heartbeat_ticks"] >= 2


@pytest.mark.asyncio
async def test_cancelled_final_feedback_still_closes_auth_pane_after_restoration(pty_pair):
    _, slave = pty_pair
    original = termios.tcgetattr(slave)
    client, pane = Client(), Pane()
    client.purpose, client.lifecycle = "auth", "failed"
    pane.auth_finished = lambda: setattr(pane, "auth_closed", termios.tcgetattr(slave) == original)
    bridge = RawTerminalBridge(client, "run-fixture", 7, input_fd=slave, output_fd=slave, pane=pane)
    async def cancelled(data):
        raise asyncio.CancelledError
    bridge._final_output = cancelled
    with pytest.raises(asyncio.CancelledError):
        await bridge.run()
    assert pane.auth_closed and termios.tcgetattr(slave) == original
