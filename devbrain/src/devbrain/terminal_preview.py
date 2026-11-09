"""Developer-only real terminal preview, backed solely by synthetic HTTP data.

No Store, engine, native runtime, provider, or production configuration is used.
Operational writes are rejected at the server, including requests made by the
unmodified panels. Only the private preview's UI state is writable.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import shlex
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
import fcntl

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

from . import __version__
from .cockpit import BrowserSessions, _birth, cockpit_status, instance_id
from .cockpit_state import _atomic
from .config import Settings
from .terminal_client import BrokerClient, BrokerError
from .workspace import WorkspaceManager

SCENARIOS = ("normal", "empty", "offline", "slow", "unicode-many")
MARKER = "terminal-preview.json"
NOTICE = "Podgląd syntetyczny: operacja zablokowana; modele i prywatna baza są wyłączone."


def source_hashes():
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path(__file__).parent.glob("*.py"))}


def preview_settings(root: Path, *, create=False, scenario="normal"):
    root = root.expanduser().absolute()
    if root.is_symlink() or root.resolve() != root:
        raise ValueError("Preview root must be a real directory, without symlink components.")
    marker = root / MARKER
    if create:
        if root.exists() and any(root.iterdir()):
            raise ValueError("A new empty preview directory is required; existing data is never adopted.")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        settings = Settings(root, token=secrets.token_urlsafe(32), arcade_url="http://127.0.0.1:1",
                            ollama_url="http://127.0.0.1:1", arcade_database="preview_never_opened")
        settings.save()
        _atomic(marker, {"kind": "devbrain-synthetic-terminal-preview", "scenario": scenario,
                         "created": time.time(), "version": __version__, "source_hashes": source_hashes()})
    else:
        if marker.is_symlink() or not marker.is_file():
            raise ValueError("Not an owned synthetic preview directory.")
        if json.loads(marker.read_text()).get("kind") != "devbrain-synthetic-terminal-preview":
            raise ValueError("Invalid preview ownership marker.")
        settings = Settings.load(root)
    return settings


class PreviewBroker(BrokerClient):
    """Real authenticated HTTP/SSE client, with all daemon startup disabled."""

    def ensure(self):
        with self._lock:
            status = cockpit_status(self.settings)
            if not status.get("available"):
                raise BrokerError(503, "Syntetyczny serwer podglądu nie odpowiada.")
            self._connect(status)
            return status

    def reconnect(self):
        with self._lock:
            self._csrf = None
            return self.ensure()


class FixtureData:
    def __init__(self, settings, scenario):
        self.settings, self.scenario = settings, scenario
        self.revision = 0
        self.epoch = "00000000-0000-0000-0000-000000000080"
        count = 45 if scenario == "unicode-many" else 2
        names = ["Atlas · podgląd pamięci", "Borealis · drugi projekt"]
        self.projects = [] if scenario == "empty" else [
            {"id": f"preview-{i}", "name": names[i % 2] + (f" {i} · Zażółć 日本語 🧠" if i > 1 else ""),
             "root": str(settings.data_dir / "projects" / f"project-{i}")} for i in range(count)]
        self.records, self.sources = {}, {}
        for project in self.projects:
            pid = project["id"]
            size = 330 if scenario == "unicode-many" else 8
            self.sources[pid] = [{"id": f"{pid}-source-{i}", "project_id": pid, "kind": "manual",
                                  "uri": f"preview://{pid}/{i}", "sha256": hashlib.sha256(str(i).encode()).hexdigest()}
                                 for i in range(size)]
            self.records[pid] = [{"id": f"{pid}-record-{i}", "project_id": pid,
                "kind": "decision" if i % 2 == 0 else "task", "key": f"preview-{i}",
                "content": f"{i + 1}. Zachowaj dowody i sprawdź wybrany zakres · Zażółć 日本語 🧠",
                "status": "confirmed" if i % 3 else "proposed", "version": 1 + i % 4,
                "source_ids": [self.sources[pid][i]["id"]], "metadata": {"state": "needs_review"}}
                for i in range(size)]

    def page(self, values, params):
        try:
            offset = max(0, int(params.get("cursor") or params.get("page_cursor") or 0))
            limit = min(300, max(1, int(params.get("limit") or 100)))
        except (TypeError, ValueError):
            raise HTTPException(422, "Invalid preview cursor") from None
        end = offset + limit
        return {"items": values[offset:end], "total": len(values), "next_cursor": str(end) if end < len(values) else None}

    def read(self, path, params):
        project = params.get("project")
        if path == "/api/state":
            return {"projects": self.projects, "profiles": [
                {"id": "preview-codex", "client": "codex", "label": "Codex · wyłączony"},
                {"id": "preview-claude", "client": "claude", "label": "Claude · wyłączony"}],
                "runs": [], "settings": {"default_slots": [
                    {"id": "first", "profile_id": "preview-codex"},
                    {"id": "second", "profile_id": "preview-claude"}], "project_slots": {}, "scan_roots": []},
                "preview": True}
        if path == "/api/auth" or path.endswith("/connection"):
            return {"available": False, "status": "preview_disabled", "message": NOTICE}
        if path == "/api/memory/status":
            return {"path": str(self.settings.data_dir / "synthetic-memory"), "latest": None,
                    "auto": {"enabled": False, "interval_minutes": 15}, "preview": True}
        if path == "/api/client-runs":
            return {"runs": []}
        if path == "/api/inspector/graph/groups":
            return {"items": [], "next_cursor": None}
        if path == "/api/inspector/graph":
            if project:
                records = self.records.get(project, [])
                page = self.page(records, params)
                nodes = [{"id": "record:" + row["id"], "record_id": row["id"], "entity_type": "record",
                          "project_id": project, "kind": row["kind"], "label": row["content"]} for row in page["items"]]
            else:
                page = self.page(self.projects, params)
                nodes = [{"id": "project:" + row["id"], "project_id": row["id"],
                          "kind": "project", "label": row["name"]} for row in page["items"]]
            return {"nodes": nodes, "edges": [{"from_id": a["id"], "to_id": b["id"]}
                    for a, b in zip(nodes, nodes[1:])], "groups": [], "next_cursor": page["next_cursor"],
                    "cursor": {"epoch": self.epoch, "seq": 1}}
        prefix = "/api/inspector/"
        if path.startswith(prefix):
            parts = path.removeprefix(prefix).split("/")
            collection = parts[0]
            rows = self.records.get(project, []) if collection == "records" else self.sources.get(project, []) if collection == "sources" else []
            if params.get("kind"):
                rows = [row for row in rows if row.get("kind") == params["kind"]]
            if len(parts) == 1:
                return self.page(rows, params)
            row = next((row for row in rows if row["id"] == parts[1]), None)
            if row is None:
                raise HTTPException(404, "Synthetic record not found")
            if len(parts) == 3 and parts[2] == "revisions":
                return {"items": [{"id": row["id"] + ":1", "version": 1, "snapshot": row}], "next_cursor": None}
            if collection == "sources":
                return {"source": row, "text": "Syntetyczny materiał źródłowy.\nZażółć gęślą jaźń · 日本語 🧠\n"
                        "[bold]tekst literalny[/bold]\n\x1b]52;c;PREVIEW_ONLY\x07", "next_offset": None}
            return {"record": row, "verification": {"status": "historical"}}
        raise HTTPException(404, "Unknown synthetic read route")


def create_preview_app(settings, scenario="normal", *, fixture=None):
    fixture = fixture if fixture is not None else FixtureData(settings, scenario)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    sessions = BrowserSessions()
    app.state.fixture = fixture
    app.state.audit = []
    app.state.operational_writes_accepted = 0

    def session(request):
        value = sessions.get(request.cookies.get("devbrain-preview"))
        if value is None:
            raise HTTPException(401, "Preview session required")
        return value

    @app.middleware("http")
    async def guard(request, call_next):
        started = time.monotonic()
        path = request.url.path
        if request.method not in {"GET", "HEAD"} and path not in {"/_control/ticket", "/auth/exchange"}:
            response = JSONResponse({"detail": NOTICE}, status_code=403)
        elif path.startswith("/api/") and fixture.scenario == "offline":
            response = JSONResponse({"detail": "Syntetyczna awaria połączenia."}, status_code=503)
        else:
            if path.startswith("/api/") and fixture.scenario == "slow":
                await asyncio.sleep(1.5)
            response = await call_next(request)
        entry = {"method": request.method, "path": path, "status": response.status_code,
                 "duration_ms": round((time.monotonic() - started) * 1000, 2)}
        app.state.audit.append(entry)
        if request.method not in {"GET", "HEAD"} and path.startswith("/api/") and response.status_code < 400:
            app.state.operational_writes_accepted += 1
        if len(app.state.audit) > 1000:
            del app.state.audit[:500]
        with (settings.data_dir / "requests.jsonl").open("a") as stream:
            stream.write(json.dumps(entry) + "\n")
        return response

    @app.get("/health")
    async def health():
        return {"service": "devbrain-cockpit", "version": __version__, "instance": instance_id(settings),
                "pid": os.getpid(), "birth": _birth(os.getpid()), "synthetic_preview": True}

    @app.post("/_control/ticket")
    async def ticket(request: Request):
        if not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + settings.token):
            raise HTTPException(401, "Invalid preview token")
        return {"ticket": sessions.ticket()}

    @app.post("/auth/exchange")
    async def exchange(request: Request):
        token, value = sessions.exchange((await request.json()).get("ticket"))
        response = JSONResponse({"csrf": value["csrf"]})
        response.set_cookie("devbrain-preview", token, httponly=True, samesite="strict")
        return response

    @app.get("/auth/session")
    async def auth(request: Request):
        return {"csrf": session(request)["csrf"]}

    @app.get("/api/events")
    async def events(request: Request):
        session(request)
        async def stream():
            revision = fixture.revision
            yield ": synthetic preview\n\n"
            while not await request.is_disconnected():
                await asyncio.sleep(2)
                if revision != fixture.revision:
                    revision = fixture.revision
                    yield "event: reset\ndata: {\"entity_type\":\"reset\"}\n\n"
                else:
                    yield ": heartbeat\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/api/{path:path}")
    async def read(request: Request, path: str):
        session(request)
        return fixture.read("/api/" + path, dict(request.query_params))

    @app.websocket("/{path:path}")
    async def no_native_socket(websocket: WebSocket, path: str):
        await websocket.close(code=1008, reason="Synthetic preview has no native terminals")

    return app


class PreviewServer:
    def __init__(self, settings, scenario, *, fixture=None):
        self.settings = settings
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        self.app = create_preview_app(settings, scenario, fixture=fixture)
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level="error", access_log=False,
                                                   lifespan="off", timeout_graceful_shutdown=2))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True)

    def start(self):
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.02)
        if not self.server.started:
            raise RuntimeError("Synthetic server did not start")
        _atomic(self.settings.data_dir / "cockpit/server.json", {"url": f"http://127.0.0.1:{self.port}",
                "pid": os.getpid(), "birth": _birth(os.getpid()), "instance": instance_id(self.settings),
                "version": __version__, "synthetic_preview": True})
        return self

    def close(self):
        self.server.should_exit = True
        self.thread.join(4)
        self.socket.close()
        if self.thread.is_alive():
            raise RuntimeError("Synthetic server did not stop")


class PreviewWorkspace(WorkspaceManager):
    def _panel(self, role):
        if role.startswith("client"):
            return super()._panel(role)
        return shlex.join([sys.executable, "-I", "-m", "devbrain.terminal_preview", "--panel", role,
                           "--data-dir", str(self.settings.data_dir)])


class PreviewTerminal:
    """A real bounded PTY attachment; passive by default so a user can take over."""

    def __init__(self, manager, size=(180, 55), *, readonly=True):
        self.master, self.slave = os.openpty()
        self.stopped = threading.Event()
        self.output = bytearray()
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", size[1], size[0], 0, 0))
        os.set_blocking(self.master, False)
        command = [manager.binary, "-S", str(manager.socket), "attach-session"]
        if readonly:
            command += ["-r", "-f", "ignore-size"]
        command += ["-t", "devbrain-ui"]
        script = "import fcntl,os,sys,termios; fcntl.ioctl(0,termios.TIOCSCTTY,0); os.execv(sys.argv[1],sys.argv[1:])"
        self.process = subprocess.Popen([sys.executable, "-I", "-c", script, *command],
            stdin=self.slave, stdout=self.slave, stderr=self.slave, start_new_session=True,
            env={**os.environ, "TERM": "xterm-256color"})
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def _drain(self):
        while not self.stopped.is_set():
            try:
                self.output += os.read(self.master, 65536)
                if len(self.output) > 512 * 1024:
                    del self.output[:-256 * 1024]
            except BlockingIOError:
                self.stopped.wait(.01)
            except OSError:
                return

    def send(self, value):
        os.write(self.master, value if isinstance(value, bytes) else value.encode())

    def resize(self, cols, rows):
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        os.kill(self.process.pid, signal.SIGWINCH)

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.stopped.set()
        self.thread.join(1)
        os.close(self.master)
        os.close(self.slave)


def capture_workspace(manager, target, label):
    from rich.console import Console
    from rich.text import Text
    target.mkdir(parents=True, exist_ok=True)
    frames = {}
    for role, pane in manager.panes().items():
        raw = manager.tmux("capture-pane", "-p", "-e", "-t", pane).stdout
        size = manager.tmux("display-message", "-p", "-t", pane, "#{pane_width} #{pane_height} #{pane_dead}").stdout.split()
        frames[role] = {"pane": pane, "width": int(size[0]), "height": int(size[1]), "dead": size[2] == "1",
                        "sha256": hashlib.sha256(raw.encode()).hexdigest()}
        (target / f"{label}-{role}.ansi").write_text(raw)
        console = Console(record=True, width=int(size[0]), file=io.StringIO(), force_terminal=True)
        console.print(Text.from_ansi(raw), end="")
        console.save_svg(str(target / f"{label}-{role}.svg"), title=f"DevBrain preview · {role}")
    (target / f"{label}.json").write_text(json.dumps(frames, indent=2))
    return frames


def panel_main(settings, kind):
    from .terminal_graph import GraphApp
    from .terminal_inspector import InspectorApp
    from .terminal_panels import SidebarApp
    app_type = {"sidebar": SidebarApp, "inspector": InspectorApp, "graph": GraphApp}[kind]
    broker = PreviewBroker(settings)
    try:
        app_type(settings, broker=broker).run()
    finally:
        broker.close()


def parse_size(value):
    try:
        cols, rows = map(int, value.lower().split("x"))
    except ValueError:
        raise argparse.ArgumentTypeError("Size must be COLSxROWS") from None
    if not 40 <= cols <= 300 or not 12 <= rows <= 100:
        raise argparse.ArgumentTypeError("Supported size: 40..300 columns, 12..100 rows")
    return cols, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--scenario", choices=SCENARIOS, default="normal")
    parser.add_argument("--view", choices=("focus", "cockpit"))
    parser.add_argument("--language", choices=("en", "pl"))
    parser.add_argument("--size", type=parse_size, default=(180, 55))
    parser.add_argument("--duration", type=float, default=120)
    parser.add_argument("--screenshots", type=Path)
    parser.add_argument("--tmux-bin")
    parser.add_argument("--attach", action="store_true")
    parser.add_argument("--attach-only", action="store_true")
    parser.add_argument("--panel", choices=("sidebar", "inspector", "graph"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.panel or args.attach_only:
        settings = preview_settings(args.data_dir)
        if args.panel:
            panel_main(settings, args.panel)
            return
        raise SystemExit(PreviewWorkspace(settings, tmux_binary=args.tmux_bin).attach())
    if args.duration <= 0:
        parser.error("--duration must be positive")
    settings = preview_settings(args.data_dir, create=True, scenario=args.scenario)
    if args.view or args.language:
        from .cockpit_state import CockpitState
        CockpitState(settings).update({key: value for key, value in
                                      (("view", args.view), ("language", args.language)) if value is not None})
    stopped = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopped.set())
    server = PreviewServer(settings, args.scenario)
    broker = PreviewBroker(settings)
    manager = PreviewWorkspace(settings, broker=broker, tmux_binary=args.tmux_bin)
    terminal = None
    supervisor = None
    report = {"version": __version__, "scenario": args.scenario, "source_hashes": source_hashes(),
              "size": args.size, "pid": os.getpid(), "birth": _birth(os.getpid()), "started": time.time(),
              "synthetic_only": True, "socket": str(manager.socket), "status": "starting"}
    try:
        server.start()
        broker.ensure()
        manager.create(start_supervisor=False)
        projects = server.app.state.fixture.projects
        if projects:
            manager.state.select_project(projects[0], intent="inspect")
        manager.tmux("resize-window", "-t", "devbrain-ui:workspace", "-x", args.size[0], "-y", args.size[1])
        manager.refresh()
        if not args.attach:
            terminal = PreviewTerminal(manager, args.size)
        report.update(status="running", url=f"http://127.0.0.1:{server.port}")
        _atomic(settings.data_dir / "preview-status.json", report)
        print(json.dumps({"status": "running", "data_dir": str(settings.data_dir), "socket": str(manager.socket)}), flush=True)
        def supervise():
            try:
                while not stopped.wait(.5):
                    manager.refresh()
                    for role, pane in manager.panes().items():
                        dead = manager.tmux("display-message", "-p", "-t", pane, "#{pane_dead}").stdout.strip()
                        if dead == "1":
                            raise RuntimeError(f"Preview pane exited: {role}")
            except Exception as exc:
                report.update(status="failed", error=type(exc).__name__ + ": " + str(exc))
                stopped.set()
        supervisor = threading.Thread(target=supervise, daemon=True)
        supervisor.start()
        deadline = time.monotonic() + args.duration
        if args.attach:
            # Duration is a hard safety bound even for an attached terminal.
            def expire():
                stopped.wait(args.duration)
                with suppress(Exception):
                    manager.tmux("detach-client", "-s", "devbrain-ui", check=False)
                stopped.set()
            threading.Thread(target=expire, daemon=True).start()
            manager.attach()
        else:
            while time.monotonic() < deadline and not stopped.wait(.2):
                pass
        stopped.set()
        supervisor.join(4)
        if args.screenshots:
            report["frames"] = capture_workspace(manager, args.screenshots, "final")
        if report["status"] != "failed":
            report["status"] = "complete"
    except BaseException as exc:
        report.update(status="failed", error=type(exc).__name__ + ": " + str(exc))
        raise
    finally:
        stopped.set()
        if supervisor is not None:
            supervisor.join(5)
        if terminal is not None:
            terminal.close()
        with suppress(Exception):
            manager.tmux("kill-server", check=False)
        manager.close()
        try:
            server.close()
        except Exception as exc:
            report.update(status="failed", cleanup_error=type(exc).__name__)
        report["ended"] = time.time()
        report["operational_writes_accepted"] = server.app.state.operational_writes_accepted
        _atomic(settings.data_dir / "preview-status.json", report)


if __name__ == "__main__":
    main()
