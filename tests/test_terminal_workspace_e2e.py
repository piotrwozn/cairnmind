"""Independent terminal QA: real ArcadeDB, HTTP, WebSocket, tmux and PTYs.

Native executables are controlled fixtures. These tests never call a coding
model, request paid inference, or open a browser.
"""
from __future__ import annotations

import base64
import asyncio
from dataclasses import dataclass
import fcntl
import inspect
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
from uuid import uuid4

import httpx
import pytest
import uvicorn
from websockets.sync.client import connect

from cairnmind import client_auth
from cairnmind.client_runtime import NativeRuntime
from cairnmind.cockpit import _birth, create_cockpit_app, instance_id
from cairnmind.cockpit_state import _atomic
from cairnmind.config import Settings
from cairnmind.service import create_app
from cairnmind.store import Store


AUTH_CANARY = "QA060_PRIVATE_DEVICE_CODE_MUST_NOT_BE_ARCHIVED"
FAKE_NATIVE = r'''
import fcntl, json, os, signal, struct, sys, termios, tty
from pathlib import Path
client = Path(sys.argv[0]).name
args = sys.argv[1:]
def trace(event, **extra):
    path = os.environ.get('FIXTURE_QA_TRACE')
    if path:
        row = {'event':event,'client':client,'pid':os.getpid(), **extra}
        with open(path, 'a', encoding='utf8') as out:
            out.write(json.dumps(row, ensure_ascii=False) + '\n')
trace('command', args=args)
if args == ['--version']:
    print('codex-cli 0.166.0' if client == 'codex' else '2.1.319 (Claude Code)')
    raise SystemExit
if args == ['--help'] or args == ['resume', '--help']:
    print('[PROMPT] mcp --no-daemon --resume SESSION_ID --model MODEL')
    raise SystemExit
if args == ['features', 'list']:
    print('hooks stable true')
    raise SystemExit
if args == ['login', 'status']:
    print('Logged in using ChatGPT')
    raise SystemExit
if args == ['auth', 'status', '--json']:
    print(json.dumps({'loggedIn':True,'authMethod':'claude.ai'}))
    raise SystemExit
if args and args[0] in {'login', 'auth'}:
    trace('auth', session_id=os.environ.get('DEVBRAIN_SESSION_ID'),
          packet_id=os.environ.get('DEVBRAIN_PACKET_ID'), cwd=os.getcwd())
    print('QA060_PRIVATE_DEVICE_CODE_MUST_NOT_BE_ARCHIVED', flush=True)
    sys.stdin.readline()
    raise SystemExit
sid = os.environ.get('DEVBRAIN_SESSION_ID')
trace('coding', session_id=sid, packet_id=os.environ.get('DEVBRAIN_PACKET_ID'), cwd=os.getcwd())
signal.signal(signal.SIGINT, lambda *a: print('INTERRUPTED '+client, flush=True))
signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
tty.setcbreak(0)
print('NATIVE_READY '+client+' '+str(os.getpid())+' '+str(sid), flush=True)
for line in sys.stdin:
    line = line.rstrip('\n')
    trace('input', text=line)
    if line == 'quit':
        break
    if line == 'size':
        rows, cols, _, _ = struct.unpack('HHHH', fcntl.ioctl(0, termios.TIOCGWINSZ, b'\0'*8))
        print('SIZE '+str(cols)+'x'+str(rows), flush=True)
    else:
        print('ECHO '+json.dumps(line, ensure_ascii=False), flush=True)
'''


def available_tmux():
    binary = os.environ.get("DEVBRAIN_TEST_TMUX") or shutil.which("tmux")
    if not binary and Path("/tmp/devbrain-tmux-3.4/tmux").is_file():
        binary = "/tmp/devbrain-tmux-3.4/tmux"
    if not binary:
        pytest.skip("Real terminal workspace QA requires tmux >= 3.4")
    return str(Path(binary).resolve())


_RESERVED = {}


def free_port():
    """Reserve a port and keep its socket open until a LiveServer serves on it.

    Closing the socket and binding the port again later lets any other process take
    the port in between, which made the server fail with "address already in use".
    """
    sock = socket.socket()
    # As Uvicorn sets it on its own sockets: without it a restart on the same port fails while
    # connections of the first server are still closing. Listening at once keeps other
    # processes off the port in spite of the option.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    _RESERVED[port] = sock
    return port


@dataclass
class LiveServer:
    app: object
    port: int

    def start(self):
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port,
            log_level="warning", access_log=False, proxy_headers=False, timeout_graceful_shutdown=5))
        reserved = _RESERVED.pop(self.port, None)
        # The reserved socket is handed to the server, so the port is never free in between.
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [reserved]} if reserved else {},
                                       daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.02)
        assert self.server.started, "Real Uvicorn server did not start"
        return self

    def stop(self):
        self.server.should_exit = True
        self.thread.join(10)
        assert not self.thread.is_alive(), "Owned Uvicorn server did not stop"


class TerminalStack:
    """Own only the isolated databases, servers and tmux sessions created here."""

    def __init__(self, base, monkeypatch):
        self.base, self.patch = Path(base), monkeypatch
        self.tmux = available_tmux()
        self.settings = Settings.load(self.base / "data", create=True)
        from cairnmind.cockpit_state import CockpitState
        # These geometry/resize scenarios exercise the optional full cockpit.
        # Focus mode is verified separately and intentionally stays compact.
        CockpitState(self.settings).update({"view": "cockpit"})
        self.settings.port, self.settings.cockpit_port = free_port(), free_port()
        self.settings.ollama_url = "http://127.0.0.1:1"
        self.settings.save()
        self.trace_path = self.base / "native-trace.jsonl"
        self.home = self.base / "home"
        self.home.mkdir()
        bindir = self.base / "bin"
        bindir.mkdir()
        for name in ("codex", "claude"):
            path = bindir / name
            path.write_text("#!" + sys.executable + "\n" + FAKE_NATIVE)
            path.chmod(0o700)
        (bindir / "tmux").symlink_to(self.tmux)
        monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setenv("CODEX_HOME", str(self.home / ".codex"))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(self.home / ".claude"))
        monkeypatch.setenv("FIXTURE_QA_TRACE", str(self.trace_path))
        for key in ("DEVBRAIN_SESSION_ID", "DEVBRAIN_PACKET_ID", "DEVBRAIN_CLIENT", "DEVBRAIN_PROJECT"):
            monkeypatch.delenv(key, raising=False)
        client_auth.clear_probe_cache()
        self.store = Store(self.settings.data_dir)
        self.projects = []
        for name in ("terminal-alpha", "terminal-beta"):
            root = self.base / name
            root.mkdir()
            (root / "app.py").write_text("def healthy():\n    return True\n")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "add", "app.py"], check=True)
            subprocess.run(["git", "-C", str(root), "-c", "user.name=Terminal QA",
                "-c", "user.email=terminal-qa@example.invalid", "commit", "-q", "-m", "Controlled fixture"], check=True)
            project = self.store.add_project(name, root)
            source = self.store.snapshot(project["id"], "manual://" + name,
                "Source for " + name + " — literal <script> & exact UTF-8.", "manual")
            record = self.store.remember(project["id"], {"kind": "decision",
                "content": "Use explicit local memory for " + name}, [source["id"]])
            self.projects.append({**project, "source": source, "record": record})
        self.memory = LiveServer(create_app(self.settings, start_worker=False), self.settings.port).start()
        self.broker = None
        self.http = None
        self.start_broker()

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.settings.cockpit_port}"

    def start_broker(self):
        self.runtime = NativeRuntime(self.settings, tmux_binary=self.tmux)
        app = create_cockpit_app(self.settings, runtime=self.runtime, public_url=self.origin)
        self.broker = LiveServer(app, self.settings.cockpit_port).start()
        _atomic(self.settings.data_dir / "cockpit/server.json", {"url": self.origin,
            "pid": os.getpid(), "birth": _birth(os.getpid()), "instance": instance_id(self.settings),
            "version": __import__("cairnmind").__version__})
        self.login()

    def login(self):
        if self.http:
            self.http.close()
        self.http = httpx.Client(base_url=self.origin, timeout=40, trust_env=False)
        ticket = self.http.post("/_control/ticket", headers={"Authorization": "Bearer " + self.settings.token})
        ticket.raise_for_status()
        response = self.http.post("/auth/exchange", json={"ticket": ticket.json()["ticket"]},
                                  headers={"Origin": self.origin})
        response.raise_for_status()
        self.csrf = response.json()["csrf"]

    def request(self, method, path, **kwargs):
        headers = kwargs.pop("headers", {})
        if method != "GET":
            headers.update({"Origin": self.origin, "X-CSRF-Token": self.csrf})
        response = self.http.request(method, path, headers=headers, **kwargs)
        response.raise_for_status()
        return response.json()

    def start_clients(self, project=None):
        project = project or self.projects[0]
        result = self.request("POST", "/api/client-runs", json={"project": project["id"],
            "idempotency_key": "qa-" + uuid4().hex})
        assert result["errors"] == [], result
        return result["runs"]

    def terminal(self, run):
        cookies = "; ".join(f"{key}={value}" for key, value in self.http.cookies.items())
        url = self.origin.replace("http:", "ws:") + f"/api/client-runs/{run['id']}/terminal?generation={run['generation']}&csrf={self.csrf}"
        return connect(url, origin=self.origin, additional_headers={"Cookie": cookies},
                       proxy=None, open_timeout=5, close_timeout=3, max_size=128 * 1024)

    def trace(self, event=None):
        rows = [json.loads(line) for line in self.trace_path.read_text().splitlines()] if self.trace_path.exists() else []
        return [row for row in rows if event is None or row["event"] == event]

    def close(self):
        if self.http:
            self.http.close()
        if self.broker:
            self.broker.stop()
        for row in self.runtime.list_runs():
            if row["lifecycle"] in {"running", "starting", "stopping"}:
                self.runtime.stop(row["id"], row["generation"])
        self.runtime._tmux("kill-server", check=False)
        self.memory.stop()
        client_auth.clear_probe_cache()


@pytest.fixture
def terminal_stack(tmp_path, monkeypatch):
    stack = TerminalStack(tmp_path, monkeypatch)
    try:
        yield stack
    finally:
        stack.close()


def receive_until(ws, expected, timeout=6):
    if isinstance(expected, str):
        expected = expected.encode()
    result = b""
    deadline = time.monotonic() + timeout
    while expected not in result:
        event = json.loads(ws.recv(timeout=max(.01, deadline - time.monotonic())))
        if event.get("type") == "output":
            result += base64.b64decode(event["data"])
        assert event.get("type") != "gap", event
    return result


def receive_control(ws, expected, timeout=6):
    deadline = time.monotonic() + timeout
    while True:
        event = json.loads(ws.recv(timeout=max(.01, deadline - time.monotonic())))
        if event.get("type") == "control" and event.get("controller") is expected:
            return event


def eventually(assertion, timeout=5):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return assertion()
        except AssertionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.03)


def test_real_broker_inspection_never_starts_models_and_sources_are_exact(terminal_stack):
    stack = terminal_stack
    state = stack.request("GET", "/api/state")
    assert len(state["projects"]) == 2 and state["runs"] == []
    for project in stack.projects:
        graph = stack.request("GET", "/api/inspector/graph", params={"project": project["id"]})
        assert "source:" + project["source"]["id"] in {row["id"] for row in graph["nodes"]}
        source = stack.request("GET", "/api/inspector/sources/" + project["source"]["id"],
                               params={"project": project["id"]})
        assert source["text"] == project["source"]["content"]
    assert stack.trace("coding") == []
    assert stack.trace("auth") == []
    assert stack.runtime.list_runs() == []


def test_real_broker_paired_native_unicode_interrupt_resize_and_restart(terminal_stack):
    stack = terminal_stack
    first, second = stack.start_clients()
    assert first["pid"] != second["pid"] and first["session_id"] != second["session_id"]
    with stack.terminal(first) as one, stack.terminal(second) as two:
        receive_until(one, "NATIVE_READY codex")
        receive_until(two, "NATIVE_READY claude")
        for ws, text in ((one, "zażółć λ ☃"), (two, "Claude UTF-8 你好")):
            ws.send(json.dumps({"type": "input", "text": text + "\n"}))
            receive_until(ws, text)
        one.send(json.dumps({"type": "input", "data": base64.b64encode(b"\x03").decode()}))
        receive_until(one, "INTERRUPTED codex")
        two.send(json.dumps({"type": "resize", "cols": 103, "rows": 34}))
        time.sleep(.15)
        two.send(json.dumps({"type": "input", "text": "size\n"}))
        receive_until(two, "SIZE 103x34")
    before = {row["id"]: (row["pid"], row["session_id"]) for row in stack.runtime.list_runs()}
    stack.broker.stop()
    stack.start_broker()
    after = {row["id"]: (row["pid"], row["session_id"]) for row in stack.runtime.list_runs()}
    assert after == before
    with stack.terminal(first) as one:
        receive_until(one, "NATIVE_READY codex")
        one.send(json.dumps({"type": "input", "text": "reconnected-with-same-session\n"}))
        receive_until(one, "reconnected-with-same-session")
    assert len(stack.trace("coding")) == 2
    for run in (first, second):
        session = httpx.get(stack.settings.url + "/sessions/" + run["session_id"],
            headers={"Authorization": "Bearer " + stack.settings.token}, trust_env=False).json()
        assert session["acknowledged"] is False, "Transport attachment must never fabricate a model ACK"


def test_real_websocket_observer_takeover_never_sends_unauthorized_input(terminal_stack):
    stack = terminal_stack
    run = stack.start_clients()[0]
    with stack.terminal(run) as first, stack.terminal(run) as observer:
        receive_control(first, True)
        receive_control(observer, False)
        observer.send(json.dumps({"type": "input", "text": "FORBIDDEN_OBSERVER_INPUT\n"}))
        time.sleep(.1)
        assert not any(row.get("text") == "FORBIDDEN_OBSERVER_INPUT" for row in stack.trace("input"))
        observer.send(json.dumps({"type": "takeover"}))
        receive_control(observer, True)
        receive_control(first, False)
        first.send(json.dumps({"type": "input", "text": "FORBIDDEN_OLD_CONTROLLER\n"}))
        observer.send(json.dumps({"type": "input", "text": "AUTHORIZED_NEW_CONTROLLER\n"}))
        receive_until(observer, "AUTHORIZED_NEW_CONTROLLER")
        rows = stack.trace("input")
        assert any(row.get("text") == "AUTHORIZED_NEW_CONTROLLER" for row in rows)
        assert not any(row.get("text", "").startswith("FORBIDDEN_") for row in rows)
    assert len(stack.trace("coding")) == 2


def test_real_operational_login_has_no_project_identity_or_archived_output(terminal_stack):
    stack = terminal_stack
    before = {project["id"]: stack.store.query("SELECT count(*) AS total FROM sources WHERE project_id=:project",
        {"project": project["id"]})[0]["total"] for project in stack.projects}
    run = stack.request("POST", "/api/auth/login", json={"client": "codex"})["run"]
    assert run["purpose"] == "auth" and run["session_id"] is None and run["project_id"] is None
    with stack.terminal(run) as terminal:
        receive_until(terminal, AUTH_CANARY)
        terminal.send(json.dumps({"type": "input", "text": "\n"}))

    def finished():
        assert stack.runtime.get_run(run["id"])["lifecycle"] in {"exited", "failed"}
    eventually(finished)
    auth = stack.trace("auth")
    assert len(auth) == 1 and auth[0]["session_id"] is None and auth[0]["packet_id"] is None
    assert auth[0]["cwd"] == str(stack.home)
    for project in stack.projects:
        count = stack.store.query("SELECT count(*) AS total FROM sources WHERE project_id=:project",
            {"project": project["id"]})[0]["total"]
        assert count == before[project["id"]]
    assert AUTH_CANARY not in json.dumps(stack.store.query("SELECT content FROM sources"))
    assert not any(row["purpose"] == "coding" for row in stack.runtime.list_runs())


async def read_pty_until(fd, expected, timeout=8):
    expected = expected.encode() if isinstance(expected, str) else expected
    data, deadline = b"", asyncio.get_running_loop().time() + timeout
    while expected not in data:
        assert asyncio.get_running_loop().time() < deadline, data[-2000:]
        try:
            data += os.read(fd, 65536)
        except BlockingIOError:
            await asyncio.sleep(.02)
    return data


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["native_exit", "signal"])
async def test_raw_bridge_real_pty_input_paste_resize_and_terminal_restore(terminal_stack, ending):
    from cairnmind.terminal_bridge import RawTerminalBridge
    from cairnmind.terminal_client import BrokerClient

    stack = terminal_stack
    run = stack.start_clients()[0]
    master, slave = os.openpty()
    os.set_blocking(master, False)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 37, 112, 0, 0))
    before_attributes = termios.tcgetattr(slave)
    before_flags = fcntl.fcntl(slave, fcntl.F_GETFL)
    previous_signal = signal.getsignal(signal.SIGTERM)
    broker = BrokerClient(stack.settings)
    bridge = RawTerminalBridge(broker, run["id"], run["generation"], input_fd=slave, output_fd=slave)
    task = asyncio.create_task(bridge.run())
    try:
        await read_pty_until(master, "NATIVE_READY codex")
        os.write(master, "bridge zażółć λ\n".encode())
        await read_pty_until(master, 'ECHO "bridge zażółć λ"')
        os.write(master, "\x1b[200~paste 你好\x1b[201~\n".encode())
        await read_pty_until(master, "paste 你好")
        os.write(master, b"\x03")
        await read_pty_until(master, "INTERRUPTED codex")
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 31, 99, 0, 0))
        await asyncio.sleep(.4)
        os.write(master, b"size\n")
        await read_pty_until(master, "SIZE 99x31")
        if ending == "signal":
            os.kill(os.getpid(), signal.SIGTERM)
        else:
            os.write(master, b"quit\n")
        assert await asyncio.wait_for(task, 8) == 0
        assert termios.tcgetattr(slave) == before_attributes
        assert fcntl.fcntl(slave, fcntl.F_GETFL) == before_flags
        assert signal.getsignal(signal.SIGTERM) == previous_signal
        if ending == "signal":
            assert stack.runtime.get_run(run["id"])["pid"] == run["pid"]
            assert stack.runtime.get_run(run["id"])["lifecycle"] == "running"
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        broker.close()
        os.close(master)
        os.close(slave)


@pytest.fixture
def outer_workspace(terminal_stack):
    from cairnmind.workspace import WorkspaceManager

    manager = WorkspaceManager(terminal_stack.settings, tmux_binary=terminal_stack.tmux)
    manager.create(start_supervisor=False)
    try:
        yield manager
    finally:
        manager.tmux("kill-server", check=False)
        manager.broker.close()


def pane_capture(manager, pane):
    return manager.tmux("capture-pane", "-p", "-S", "-", "-t", pane).stdout


def wait_pane(manager, pane, text, timeout=10):
    def visible():
        captured = pane_capture(manager, pane)
        assert text in captured, captured[-2500:]
        return captured
    return eventually(visible, timeout=timeout)


def test_real_outer_workspace_project_switch_and_broker_restart_preserve_native_identity(terminal_stack, outer_workspace):
    stack, manager = terminal_stack, outer_workspace
    assert set(manager.panes()) == {"sidebar", "client1", "client2", "inspector", "graph"}
    assert manager.socket != stack.runtime.socket
    assert stack.trace("coding") == []
    manager.refresh()
    assert stack.trace("coding") == [], "Restoring and inspecting the workspace must not start native models"
    manager.state.select_project(stack.projects[0], intent="work", requested_slot_ids=["codex", "claude"])
    manager.refresh()
    panes = manager.panes()
    wait_pane(manager, panes["client1"], "NATIVE_READY codex")
    wait_pane(manager, panes["client2"], "NATIVE_READY claude")
    before = {row["id"]: (row["pid"], row["session_id"]) for row in stack.runtime.list_runs()}
    assert len(before) == 2
    manager.state.select_project(stack.projects[1], intent="inspect")
    manager.refresh()
    assert len(stack.trace("coding")) == 2
    manager.state.select_project(stack.projects[0], intent="inspect")
    manager.refresh()
    wait_pane(manager, manager.panes()["client1"], "NATIVE_READY codex")
    assert before == {row["id"]: (row["pid"], row["session_id"]) for row in stack.runtime.list_runs()}
    stack.broker.stop()
    stack.start_broker()
    manager.broker.reconnect()
    manager.refresh()
    first = manager.panes()["client1"]
    wait_pane(manager, first, "NATIVE_READY codex")
    def controller_restored():
        value = manager.tmux("show-options", "-pv", "-t", first, "@devbrain_control", check=False).stdout.strip()
        assert value == "controller", value
    eventually(controller_restored, timeout=10)
    manager.tmux("send-keys", "-t", first, "-l", "OUTER_RECONNECTED_zażółć")
    manager.tmux("send-keys", "-t", first, "Enter")
    wait_pane(manager, first, 'ECHO "OUTER_RECONNECTED_zażółć"')
    assert before == {row["id"]: (row["pid"], row["session_id"]) for row in stack.runtime.list_runs()}
    assert len(stack.trace("coding")) == 2


def test_real_outer_operational_login_destroys_only_its_secret_history(terminal_stack, outer_workspace):
    stack, manager = terminal_stack, outer_workspace
    opened = manager.open_auth("codex")
    run, pane = opened["run"], opened["pane_id"]
    wait_pane(manager, pane, AUTH_CANARY)
    assert run["project_id"] is None and run["session_id"] is None
    manager.tmux("send-keys", "-t", pane, "Enter")

    def removed():
        rows = manager.tmux("list-panes", "-a", "-F", "#{pane_id}").stdout.splitlines()
        assert pane not in rows
    eventually(removed, timeout=10)
    for other in manager.panes().values():
        assert AUTH_CANARY not in pane_capture(manager, other)
    assert run["id"] not in manager.state.read()["auth_runs"]
    assert AUTH_CANARY not in json.dumps(stack.store.query("SELECT content FROM sources"))
    assert len(manager.panes()) == 5


async def wait_async(predicate, timeout=10):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "Actual terminal projection did not refresh"
        await asyncio.sleep(.04)


@pytest.mark.asyncio
async def test_actual_textual_graph_live_source_selection_opens_exact_inspector_source(terminal_stack):
    from cairnmind.terminal_graph import GraphApp
    from cairnmind.terminal_inspector import InspectorApp
    from cairnmind.workspace_state import WorkspaceState

    stack = terminal_stack
    project = stack.projects[0]
    state = WorkspaceState(stack.settings)
    state.select_project(project, intent="inspect")
    graph = GraphApp(stack.settings, workspace=state, motion=False)
    async with graph.run_test(size=(100, 35)):
        try:
            await wait_async(lambda: any(node.get("source_id") == project["source"]["id"] for node in graph.model.nodes))
        except AssertionError as error:
            raise AssertionError({"scope": graph.model.scope, "workspace": graph.workspace_data,
                "nodes": graph.model.nodes, "visible": graph._visible(),
                "notice": str(graph.query_one("#panel-notice").render())}) from error
        created = await asyncio.to_thread(stack.store.snapshot, project["id"], "manual://live-node",
            "Live graph source: zażółć & <script>literal</script>.", "manual")
        await wait_async(lambda: any(node.get("source_id") == created["id"] for node in graph.model.nodes))
        node = next(node for node in graph.model.nodes if node.get("source_id") == created["id"])
        await graph.open_node(node).wait()
        selection = state.read()["selected_entity"]
        assert selection == {"project_id": project["id"], "entity_type": "source", "id": created["id"]}
        assert state.read()["focus"] == "inspector"
    inspector = InspectorApp(stack.settings, workspace=state)
    async with inspector.run_test(size=(105, 40)):
        await wait_async(lambda: inspector.source_content == created["content"])
        assert inspector.entity_kind == "sources"
        assert inspector.detail_data["source"]["id"] == created["id"]
        assert inspector.source_content == created["content"]
    assert stack.trace("coding") == []
    assert stack.trace("auth") == []


@pytest.mark.asyncio
async def test_actual_sidebar_clone_snapshot_preflight_requires_confirmation_and_preserves_scope(terminal_stack, tmp_path):
    from cairnmind.terminal_panels import SidebarApp

    source = terminal_stack
    project = source.projects[0]
    saved = source.request("POST", "/api/memory/snapshot", json={"project": project["id"]})
    with pytest.MonkeyPatch.context() as patch:
        target = TerminalStack(tmp_path / "other-computer", patch)
        try:
            clone = target.base / "clone"
            clone.mkdir()
            destination = clone / ".memory/devbrain"
            shutil.copytree(Path(saved["path"]).parent, destination)
            unrelated = {p["id"]: target.store.list_records(p["id"]) for p in target.projects}
            sidebar = SidebarApp(target.settings)
            approve = [False]
            confirmations = []

            async def choose(screen, **kwargs):
                if type(screen).__name__ == "ChoiceModal":
                    return "import"
                if type(screen).__name__ == "FormModal":
                    fields = {field.name: field.value for field in screen.fields}
                    assert fields["root"] == str(clone)
                    assert fields["path"] == str(destination)
                    values = {"path": str(destination), "root": str(clone), "name": "cloned-alpha"}
                    if screen.on_submit is not None:
                        result = screen.on_submit(values)
                        if inspect.isawaitable(result):
                            await result
                    return values
                if type(screen).__name__ == "ConfirmModal":
                    confirmations.append(screen.value)
                    assert screen.value["project"]["id"] == project["id"]
                    assert screen.value["project"]["root"] == str(clone)
                    assert screen.value["status"] == "dry_run"
                    return approve[0]
                raise AssertionError("Unexpected confirmation screen: " + type(screen).__name__)

            sidebar.push_screen_wait = choose
            async with sidebar.run_test(size=(90, 40)):
                await wait_async(lambda: len(sidebar.projects) == 2)
                await sidebar.register_project("cloned-alpha", str(clone))
                assert len(target.store.list_projects()) == 2, "Preflight/cancel must not register a new project"
                assert not target.store.query("SELECT id FROM memory_imports")
                approve[0] = True
                await sidebar.register_project("cloned-alpha", str(clone))
                assert target.store.resolve_project(project["id"])["root"] == str(clone)
                assert sidebar.workspace.read()["intent"] == "inspect"
                assert sidebar.workspace.read()["project_id"] == project["id"]
            assert len(confirmations) == 2
            assert len(target.store.list_projects()) == 3
            assert target.store.list_records(project["id"])[0]["id"] == project["record"]["id"]
            imported = target.store.query("SELECT content FROM sources WHERE id=:id", {"id": project["source"]["id"]})
            assert imported[0]["content"] == project["source"]["content"]
            assert {p["id"]: target.store.list_records(p["id"]) for p in target.projects} == unrelated
            assert target.trace("coding") == [] and source.trace("coding") == []
        finally:
            target.close()


@pytest.mark.asyncio
async def test_attached_outer_terminal_prefix_unicode_paste_compact_resize_and_detach(terminal_stack, outer_workspace):
    stack, manager = terminal_stack, outer_workspace
    master, slave = os.openpty()
    os.set_blocking(master, False)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 55, 180, 0, 0))
    attributes = termios.tcgetattr(slave)
    environment = {**os.environ, "TERM": "xterm-256color"}
    process = subprocess.Popen([sys.executable, "-I", "-m", "cairnmind.native_worker", "--attach",
        "--tmux-bin", stack.tmux, "--socket", str(manager.socket), "--session", "devbrain-ui"],
        stdin=slave, stdout=slave, stderr=slave, env=environment, start_new_session=True)
    drain = None
    try:
        await read_pty_until(master, "Cairnmind")

        async def discard_screen_output():
            while True:
                try:
                    os.read(master, 65536)
                except BlockingIOError:
                    await asyncio.sleep(.02)
                except OSError:
                    return
        drain = asyncio.create_task(discard_screen_output())
        manager.refresh()
        wait_pane(manager, manager.panes()["sidebar"], "terminal-alpha")
        assert stack.trace("coding") == []
        manager.state.select_project(stack.projects[0], intent="work", requested_slot_ids=["codex", "claude"])
        manager.refresh()
        panes = manager.panes()
        for role, client in (("client1", "codex"), ("client2", "claude")):
            wait_pane(manager, panes[role], "NATIVE_READY " + client)

        async def select(key, role):
            os.write(master, b"\x02" + key.encode())
            await wait_async(lambda: manager.tmux("display-message", "-p", "-t", panes[role], "#{pane_active}").stdout.strip() == "1")

        await select("1", "client1")
        os.write(master, "OUTER_UTF8_zażółć λ\r".encode())
        wait_pane(manager, panes["client1"], 'ECHO "OUTER_UTF8_zażółć λ"')
        os.write(master, b"\x02\x02\r")
        await wait_async(lambda: any(row.get("text") == "\x02" for row in stack.trace("input")))
        await select("2", "client2")
        os.write(master, "\x1b[200~OUTER_PASTE_你好\x1b[201~\r".encode())
        await wait_async(lambda: any("OUTER_PASTE_你好" in row.get("text", "") for row in stack.trace("input")))
        os.write(master, b"\x03")
        wait_pane(manager, panes["client2"], "INTERRUPTED claude")
        before = {row["id"]: (row["pid"], row["session_id"]) for row in stack.runtime.list_runs()}
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 35, 115, 0, 0))
        os.kill(process.pid, signal.SIGWINCH)
        await asyncio.sleep(.3)
        manager.refresh()
        assert manager.state.read()["layout"] == "compact"
        assert manager.tmux("display-message", "-p", "-t", panes["client2"], "#{window_zoomed_flag}").stdout.strip() == "1"
        await asyncio.sleep(.4)
        cols, rows = manager.tmux("display-message", "-p", "-t", panes["client2"], "#{pane_width} #{pane_height}").stdout.split()
        os.write(master, b"size\r")
        wait_pane(manager, panes["client2"], f"SIZE {cols}x{rows}")
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 55, 180, 0, 0))
        os.kill(process.pid, signal.SIGWINCH)
        await asyncio.sleep(.3)
        manager.refresh()
        assert manager.state.read()["layout"] == "wide"
        os.write(master, b"\x02d")
        assert await asyncio.to_thread(process.wait, timeout=8) == 0
        assert manager.exists()
        assert termios.tcgetattr(slave) == attributes
        assert before == {row["id"]: (row["pid"], row["session_id"]) for row in stack.runtime.list_runs()}
        assert len(stack.trace("coding")) == 2
    finally:
        if drain is not None:
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)
        if process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait, timeout=5)
        os.close(master)
        os.close(slave)


class OuterTerminalClient:
    """One real user terminal entering the production attachment policy."""

    def __init__(self, manager, *, takeover=False, columns=180, rows=55):
        self.manager = manager
        self.master, self.slave = os.openpty()
        self.tty = os.ttyname(self.slave)
        os.set_blocking(self.master, False)
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        script = """import fcntl, sys, termios
from pathlib import Path
from cairnmind.config import Settings
from cairnmind.workspace import WorkspaceManager
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
manager = WorkspaceManager(Settings.load(Path(sys.argv[1])), tmux_binary=sys.argv[2])
raise SystemExit(manager.attach(takeover=sys.argv[3] == 'yes'))
"""
        self.process = subprocess.Popen([sys.executable, "-I", "-c", script,
            str(manager.settings.data_dir), manager.binary, "yes" if takeover else "no"],
            stdin=self.slave, stdout=self.slave, stderr=self.slave,
            env={**os.environ, "TERM": "xterm-256color"}, start_new_session=True)
        self.stopped = threading.Event()
        self.output = bytearray()
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def _drain(self):
        while not self.stopped.is_set():
            try:
                data = os.read(self.master, 65536)
                self.output += data
                if len(self.output) > 512 * 1024:
                    del self.output[:-256 * 1024]
            except BlockingIOError:
                self.stopped.wait(.01)
            except OSError:
                return

    def metadata(self):
        rows = [row for row in self.manager.clients() if row["tty"] == self.tty]
        assert len(rows) == 1, {"clients": self.manager.clients(), "terminal": self.tty,
                               "output": bytes(self.output[-1500:])}
        return rows[0]

    def resize(self, columns, rows):
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        os.killpg(self.process.pid, signal.SIGWINCH)

    def close(self):
        self.manager.tmux("detach-client", "-t", self.tty, check=False)
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
        self.stopped.set()
        self.thread.join(1)
        os.close(self.master)
        os.close(self.slave)


def test_two_real_outer_terminals_enforce_single_writer_takeover_and_resize_authority(terminal_stack, outer_workspace):
    stack, manager = terminal_stack, outer_workspace
    manager.state.select_project(stack.projects[0], intent="work", requested_slot_ids=["codex", "claude"])
    manager.refresh()
    pane = manager.panes()["client1"]
    wait_pane(manager, pane, "NATIVE_READY codex")
    manager.focus("client1")
    identities = {row["id"]: (row["pid"], row["session_id"]) for row in stack.runtime.list_runs()}
    first = OuterTerminalClient(manager)
    observer = replacement = None
    try:
        def first_writer():
            assert first.metadata()["readonly"] is False, {
                "client": first.metadata(), "process": first.process.poll(),
                "output": bytes(first.output[-3000:])}
            assert first.metadata()["ignore_size"] is False
        eventually(first_writer)
        dimensions = manager.tmux("display-message", "-p", "-t", pane, "#{window_width} #{window_height}").stdout
        observer = OuterTerminalClient(manager, columns=115, rows=35)
        def read_only():
            assert observer.metadata()["readonly"] is True
            assert observer.metadata()["ignore_size"] is True
        eventually(read_only)
        assert sum(not row["readonly"] for row in manager.clients()) == 1
        assert manager.tmux("display-message", "-p", "-t", pane, "#{window_width} #{window_height}").stdout == dimensions
        os.write(observer.master, b"FORBIDDEN_OUTER_OBSERVER\r\x03")
        observer.resize(101, 30)
        time.sleep(.35)
        assert manager.tmux("display-message", "-p", "-t", pane, "#{window_width} #{window_height}").stdout == dimensions
        assert "INTERRUPTED codex" not in pane_capture(manager, pane)
        assert not any(row.get("text") == "FORBIDDEN_OUTER_OBSERVER" for row in stack.trace("input"))
        os.write(first.master, "OUTER_WRITER_zażółć\r".encode())
        wait_pane(manager, pane, 'ECHO "OUTER_WRITER_zażółć"')
        observer.close()
        observer = None
        replacement = OuterTerminalClient(manager, takeover=True, columns=165, rows=50)
        def takeover_complete():
            assert replacement.metadata()["readonly"] is False
            assert replacement.metadata()["ignore_size"] is False
            assert first.metadata()["readonly"] is True
            assert first.metadata()["ignore_size"] is True
        eventually(takeover_complete)
        assert sum(not row["readonly"] for row in manager.clients()) == 1
        os.write(first.master, b"FORBIDDEN_OLD_OUTER_WRITER\r")
        os.write(replacement.master, "NEW_OUTER_WRITER_你好\r".encode())
        wait_pane(manager, pane, "NEW_OUTER_WRITER_你好")
        assert not any(row.get("text", "").startswith("FORBIDDEN_") for row in stack.trace("input"))
        before_resize = manager.tmux("display-message", "-p", "-t", pane, "#{window_width} #{window_height}").stdout
        first.resize(100, 29)
        time.sleep(.25)
        assert manager.tmux("display-message", "-p", "-t", pane, "#{window_width} #{window_height}").stdout == before_resize
        replacement.resize(170, 52)
        def new_writer_resize():
            assert manager.tmux("display-message", "-p", "-t", pane, "#{window_width}").stdout.strip() == "170"
        eventually(new_writer_resize)
        assert identities == {row["id"]: (row["pid"], row["session_id"]) for row in stack.runtime.list_runs()}
        assert len(stack.trace("coding")) == 2
    finally:
        if replacement is not None:
            replacement.close()
        if observer is not None:
            observer.close()
        first.close()


def test_concurrent_real_outer_attach_claims_exactly_one_writer_without_model_start(terminal_stack, outer_workspace):
    manager = outer_workspace
    first = OuterTerminalClient(manager)
    second = OuterTerminalClient(manager)
    try:
        def one_writer():
            clients = manager.clients()
            assert len(clients) == 2
            assert sum(not row["readonly"] for row in clients) == 1, clients
            assert all(row["readonly"] == row["ignore_size"] for row in clients)
        eventually(one_writer, timeout=10)
        assert terminal_stack.trace("coding") == []
        assert terminal_stack.runtime.list_runs() == []
    finally:
        second.close()
        first.close()
