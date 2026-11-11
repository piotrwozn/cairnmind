"""Raw terminal view of one existing native run through the global broker.

This process does not emulate ANSI, launch models, acquire control implicitly or
archive terminal text. The outer tmux pane and inner tmux renderer own the VT.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import contextmanager
import fcntl
import json
import os
import re
import signal
import subprocess
import termios
import tty

from websockets.exceptions import WebSocketException

from .config import Settings
from .terminal_client import BrokerClient, BrokerError
from .terminal_i18n import language_for, native_failure_notice


@contextmanager
def raw_terminal(input_fd, output_fd):
    if not os.isatty(input_fd) or not os.isatty(output_fd) or os.ttyname(input_fd) != os.ttyname(output_fd):
        raise ValueError("Terminal bridge requires one interactive terminal")
    attributes = termios.tcgetattr(input_fd)
    flags = {fd: fcntl.fcntl(fd, fcntl.F_GETFL) for fd in {input_fd, output_fd}}
    try:
        tty.setraw(input_fd, termios.TCSANOW)
        for fd in flags:
            os.set_blocking(fd, False)
        yield
    finally:
        for fd, value in flags.items():
            try:
                fcntl.fcntl(fd, fcntl.F_SETFL, value)
            except OSError:
                pass
        try:
            termios.tcsetattr(input_fd, termios.TCSANOW, attributes)
        except OSError:
            pass


def terminal_dimensions(fd):
    columns, rows = os.get_terminal_size(fd)
    return max(20, min(500, columns)), max(5, min(200, rows))


class PaneStatus:
    """Update only a positively identified outer workspace pane."""
    def __init__(self, settings, run_id, workspace_id):
        self.run_id, self.workspace_id = run_id, workspace_id
        self.pane = os.environ.get("TMUX_PANE", "")
        self.manager = None
        try:
            from .workspace import WorkspaceManager
            manager = WorkspaceManager(settings, workspace_id=workspace_id)
            socket = os.environ.get("TMUX", "").split(",", 1)[0]
            if socket == str(manager.socket) and re.fullmatch(r"%\d+", self.pane):
                self.manager = manager
        except (ImportError, OSError, ValueError):
            pass
        self.previous = None

    def poll_nonce(self):
        if self.manager is None:
            return None
        try:
            result = self.manager.tmux("display-message", "-p", "-t", self.pane,
                "#{@cairnmind_workspace}\t#{@cairnmind_run_id}\t#{@cairnmind_takeover}", check=False)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        fields = result.stdout.rstrip("\n").split("\t")
        if result.returncode or len(fields) != 3 or fields[:2] != [self.workspace_id, self.run_id]:
            return None
        return fields[2]

    def state(self, value):
        if value == self.previous:
            return
        if self.poll_nonce() is not None:
            try:
                result = self.manager.tmux("set-option", "-p", "-t", self.pane, "@cairnmind_control", value, check=False)
                if result.returncode == 0:
                    self.previous = value
            except (OSError, ValueError, subprocess.SubprocessError):
                pass

    def auth_finished(self):
        if self.poll_nonce() is not None:
            try:
                self.manager.auth_finished(self.run_id)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass


class RawTerminalBridge:
    def __init__(self, client, run_id, generation, *, input_fd=0, output_fd=1, pane=None, language="en"):
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}", run_id):
            raise ValueError("Invalid native run ID")
        if type(generation) is not int or generation < 1:
            raise ValueError("Invalid native generation")
        self.client, self.run_id, self.generation = client, run_id, generation
        self.input_fd, self.output_fd, self.pane = input_fd, output_fd, pane
        self.stopped = asyncio.Event()
        self.resize_pending = asyncio.Event()
        self.controller = False
        self.finished = False
        self.final_state = "offline"
        self.final_run = {}
        self.language = language if language in {"en", "pl"} else "en"
        self.auth = False
        self.nonce = None
        self._signals = {}

    def _finish(self, run, *, stale=False):
        self.finished = True
        self.final_state = "stale" if stale else run.get("lifecycle", run.get("state"))
        self.final_run = {"lifecycle": self.final_state, "client": run.get("client"),
                          "exit_code": run.get("exit_code"), "purpose": run.get("purpose")}

    async def _state(self, state):
        if self.pane is not None:
            try:
                await asyncio.to_thread(self.pane.state, state)
            except (OSError, ValueError):
                pass

    @staticmethod
    async def _ready(fd, write=False):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        def ready():
            if not future.done():
                future.set_result(None)
        (loop.add_writer if write else loop.add_reader)(fd, ready)
        try:
            await future
        finally:
            (loop.remove_writer if write else loop.remove_reader)(fd)

    async def _output(self, data):
        view = memoryview(data)
        while view:
            try:
                written = os.write(self.output_fd, view)
                if written == 0:
                    raise OSError("Terminal output closed")
                view = view[written:]
            except BlockingIOError:
                await self._ready(self.output_fd, True)

    async def _final_output(self, data, timeout=.25):
        """Bound final feedback even after the raw context restored blocking I/O."""
        try:
            flags = fcntl.fcntl(self.output_fd, fcntl.F_GETFL)
        except OSError:
            return False
        try:
            os.set_blocking(self.output_fd, False)
            deadline = asyncio.get_running_loop().time() + timeout
            view = memoryview(data)
            while view:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return False
                try:
                    written = os.write(self.output_fd, view)
                    if written == 0:
                        return False
                    view = view[written:]
                except BlockingIOError:
                    try:
                        await asyncio.wait_for(self._ready(self.output_fd, True), remaining)
                    except TimeoutError:
                        return False
            return True
        except OSError:
            return False
        finally:
            try:
                fcntl.fcntl(self.output_fd, fcntl.F_SETFL, flags)
            except OSError:
                pass

    def _flush_input(self):
        try:
            termios.tcflush(self.input_fd, termios.TCIFLUSH)
        except OSError:
            pass

    async def _connection(self, websocket):
        send_lock = asyncio.Lock()

        async def send(value):
            async with send_lock:
                await websocket.send(json.dumps(value, separators=(",", ":")))

        async def keyboard():
            while True:
                await self._ready(self.input_fd)
                try:
                    data = os.read(self.input_fd, 4096)
                except BlockingIOError:
                    continue
                if not data:
                    self.stopped.set()
                    return
                if self.controller:
                    await send({"type": "input", "data": base64.b64encode(data).decode()})

        async def receive():
            async for message in websocket:
                if not isinstance(message, str) or len(message) > 256 * 1024:
                    raise ValueError("Invalid terminal frame")
                value = json.loads(message)
                if not isinstance(value, dict):
                    raise ValueError("Invalid terminal frame")
                kind = value.get("type")
                if kind == "output":
                    if value.get("encoding") != "base64" or not isinstance(value.get("data"), str):
                        raise ValueError("Invalid native output")
                    data = base64.b64decode(value["data"], validate=True)
                    if len(data) > 65536:
                        raise ValueError("Native output frame exceeds limit")
                    await self._output(data)
                elif kind == "control":
                    controller = value.get("controller") is True
                    if controller and not self.controller:
                        self._flush_input()
                    self.controller = controller
                    if controller:
                        self.resize_pending.set()
                    await self._state("controller" if controller else "readonly")
                elif kind == "gap":
                    raise OSError("Terminal output gap requires a complete redraw")
                elif kind == "status":
                    run = value.get("run") or {}
                    if run.get("generation", self.generation) != self.generation:
                        self._finish(run, stale=True)
                        return
                    if run.get("lifecycle") in {"exited", "failed", "interrupted"}:
                        self._finish(run)
                        return
                elif kind == "error":
                    raise ValueError("Terminal protocol rejected the stream")

        async def controls():
            last_size, next_status = None, 0.0
            while True:
                size = terminal_dimensions(self.input_fd)
                if self.controller and (size != last_size or self.resize_pending.is_set()):
                    self.resize_pending.clear()
                    await send({"type": "resize", "cols": size[0], "rows": size[1]})
                    last_size = size
                if self.pane is not None:
                    nonce = await asyncio.to_thread(self.pane.poll_nonce)
                    if nonce is not None and nonce != self.nonce:
                        self.nonce = nonce
                        if nonce:
                            # Only a fresh explicit workspace action requests
                            # takeover; reconnect never replays an old request.
                            await send({"type": "takeover"})
                    await self._state("controller" if self.controller else "readonly")
                now = asyncio.get_running_loop().time()
                if now >= next_status:
                    run = await asyncio.to_thread(self.client.request, "GET", "/api/client-runs/" + self.run_id, timeout=5)
                    if run.get("generation") != self.generation:
                        self._finish(run, stale=True)
                        return
                    self.auth = run.get("purpose") == "auth"
                    if run.get("lifecycle", run.get("state")) in {"exited", "failed", "interrupted"}:
                        self._finish(run)
                        return
                    next_status = now + 1
                await asyncio.sleep(.25)

        tasks = [asyncio.create_task(keyboard()), asyncio.create_task(receive()),
                 asyncio.create_task(controls()), asyncio.create_task(self.stopped.wait())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _install_signals(self):
        loop = asyncio.get_running_loop()
        for number, callback in ((signal.SIGWINCH, self.resize_pending.set), (signal.SIGTERM, self.stopped.set),
                                  (signal.SIGHUP, self.stopped.set), (signal.SIGINT, self.stopped.set)):
            try:
                previous = signal.getsignal(number)
                loop.add_signal_handler(number, callback)
                self._signals[number] = previous
            except (RuntimeError, ValueError):
                pass

    def _restore_signals(self):
        loop = asyncio.get_running_loop()
        for number, previous in self._signals.items():
            loop.remove_signal_handler(number)
            signal.signal(number, previous)

    async def run(self):
        result = 0
        try:
            with raw_terminal(self.input_fd, self.output_fd):
                self._install_signals()
                if self.pane is not None:
                    self.nonce = await asyncio.to_thread(self.pane.poll_nonce)
                while not self.stopped.is_set() and not self.finished:
                    self.controller = False
                    self._flush_input()
                    await self._state("offline")
                    try:
                        await asyncio.to_thread(self.client.reconnect)
                        run = await asyncio.to_thread(self.client.request, "GET", "/api/client-runs/" + self.run_id, timeout=5)
                        if run.get("generation") != self.generation:
                            result = 1
                            self._finish(run, stale=True)
                            break
                        self.auth = run.get("purpose") == "auth"
                        if run.get("lifecycle", run.get("state")) in {"exited", "failed", "interrupted"}:
                            self._finish(run)
                            break
                        async with self.client.terminal(self.run_id, self.generation) as websocket:
                            # Cancel a possibly incomplete ANSI sequence in this
                            # view before the broker's authoritative tmux redraw.
                            await self._output(b"\x18\x1b[0m\x1b[H\x1b[2J")
                            await self._connection(websocket)
                    except asyncio.CancelledError:
                        raise
                    except BrokerError as exc:
                        if exc.status_code in {404, 409}:
                            result = 1
                            self._finish({"purpose": "auth" if self.auth else None}, stale=True)
                            break
                        await self._state("offline")
                        try:
                            await asyncio.wait_for(self.stopped.wait(), 1)
                        except TimeoutError:
                            pass
                    except (OSError, ValueError, WebSocketException):
                        await self._state("offline")
                        try:
                            await asyncio.wait_for(self.stopped.wait(), 1)
                        except TimeoutError:
                            pass
        except (OSError, ValueError):
            result = 2
        finally:
            self._restore_signals()
            await self._state(self.final_state if self.finished or result else "offline")
            # The raw context and receiver have ended. This fixed final message
            # cannot be cancelled by a status/output race or archive native text.
            try:
                message = native_failure_notice(self.final_run, self.language)
                if message:
                    await self._final_output(b"\r\n\x18\x1b[0m" + message.encode("utf-8") + b"\r\n")
            finally:
                # Even cancellation of optional feedback must retain the
                # existing post-restoration removal of an account-login pane.
                if self.auth and self.finished and self.pane is not None:
                    try:
                        await asyncio.to_thread(self.pane.auth_finished)
                    except (OSError, ValueError):
                        pass
        return result


def run_bridge(settings, run_id, generation, *, workspace_id="default"):
    client = BrokerClient(settings)
    try:
        bridge = RawTerminalBridge(client, run_id, generation,
                                   pane=PaneStatus(settings, run_id, workspace_id), language=language_for(settings))
        return asyncio.run(bridge.run())
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description="Private raw native terminal bridge")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--workspace-id", default="default")
    args = parser.parse_args()
    from pathlib import Path
    settings = Settings.load(Path(args.data_dir), create=False)
    raise SystemExit(run_bridge(settings, args.run_id, args.generation, workspace_id=args.workspace_id))


if __name__ == "__main__":
    main()
