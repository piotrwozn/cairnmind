"""Private headless tmux ownership and bounded browser PTY attachments.

Runtime metadata is deliberately outside project memory and independent of
ArcadeDB availability. Native transcripts, hooks and model ACKs remain the
authoritative memory protocol; terminal delivery never manufactures an ACK.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import termios
import time
from uuid import uuid4

import httpx

from .config import Settings
from .native_worker import process_start
from .runtime_selection import resolve_tmux

LIVE = {"starting", "running", "stopping"}
_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,199}$")
_CONFIG = """set -g status off
set -g prefix None
unbind-key -a
set -g mouse off
set -g remain-on-exit on
set -g exit-empty off
set -g history-limit 5000
set -g default-terminal tmux-256color
set -g extended-keys on
set -s set-clipboard off
set -g allow-passthrough off
"""


def _atomic(path: Path, data):
    fd, name = tempfile.mkstemp(prefix=".native-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class NativeRuntime:
    def __init__(self, settings: Settings, tmux_binary: str | None = None):
        self.settings = settings
        self.directory = settings.data_dir / "runtime"
        namespace = hashlib.sha256(str(settings.data_dir.resolve()).encode()).hexdigest()[:16]
        self.socket_directory = Path(tempfile.gettempdir()) / f"devbrain-native-{os.getuid()}-{namespace}"
        self.socket = self.socket_directory / "tmux.sock"
        self.registry = self.directory / "runs.json"
        self.config = self.directory / "tmux.conf"
        self._binary_error = None
        try:
            self.binary = resolve_tmux(settings, override=tmux_binary)
        except ValueError as exc:
            self.binary = None
            self._binary_error = str(exc)
        self._channels: dict[str, _Channel] = {}

    def diagnostics(self):
        result = {"available": False, "minimum_version": "3.4", "backend": "tmux", "version": None,
                  "executable": self.binary}
        if self._binary_error:
            return {**result, "code": "tmux_invalid_executable", "error": self._binary_error}
        if not self.binary:
            return {**result, "code": "tmux_missing", "action": "Install tmux >= 3.4 in the same WSL distribution"}
        try:
            probe = subprocess.run([self.binary, "-V"], capture_output=True, text=True, timeout=4)
            match = re.search(r"tmux (\d+)\.(\d+)", probe.stdout)
            supported = probe.returncode == 0 and match and tuple(map(int, match.groups())) >= (3, 4)
            return {**result, "available": bool(supported), "version": probe.stdout.strip()[:80],
                    "code": "ready" if supported else "tmux_unsupported"}
        except (OSError, subprocess.SubprocessError):
            return {**result, "code": "tmux_unavailable"}

    capabilities = diagnostics

    def _initialize(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.directory.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("Native runtime directory must be owner-only")
        self.socket_directory.mkdir(exist_ok=True, mode=0o700)
        info = self.socket_directory.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("Native terminal socket directory must be owner-only")
        if len(os.fsencode(self.socket)) > 100:
            raise ValueError("Native runtime Unix socket path is too long")
        if self.config.is_symlink():
            raise ValueError("Native runtime configuration cannot be a symlink")
        if not self.config.exists():
            self.config.write_text(_CONFIG)
            self.config.chmod(0o600)
        elif self.config.stat().st_uid != os.getuid() or stat.S_IMODE(self.config.stat().st_mode) != 0o600:
            raise ValueError("Native runtime configuration must be owner-only")

    @contextmanager
    def _locked(self):
        self._initialize()
        fd = os.open(self.directory / "registry.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def _read(self):
        if not self.registry.exists():
            return []
        fd = os.open(self.registry, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as handle:
            info = os.fstat(handle.fileno())
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("Native runtime registry must be owner-only")
            data = json.load(handle)
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("runs"), list):
            raise ValueError("Invalid native runtime registry")
        return data["runs"]

    def _save(self, rows):
        _atomic(self.registry, {"version": 1, "runs": rows})

    def _tmux(self, *args, check=True):
        if self._binary_error:
            raise ValueError("tmux_invalid_executable: " + self._binary_error)
        if not self.binary:
            raise ValueError("tmux_missing: install tmux >= 3.4 in this WSL distribution")
        result = subprocess.run([self.binary, "-S", str(self.socket), "-f", str(self.config), *args],
                                capture_output=True, text=True, timeout=5)
        if check and result.returncode:
            # Never expose command-line/bootstrap or terminal output in errors.
            raise ValueError("Native terminal supervisor rejected the operation")
        return result

    def _pane(self, row):
        if not self.binary or not self.socket.exists():
            return None
        info = self.socket.lstat()
        if info.st_uid != os.getuid() or not stat.S_ISSOCK(info.st_mode):
            raise ValueError("Native terminal socket is not owned by this user")
        result = self._tmux("display-message", "-p", "-t", row["tmux_session"],
            "#{session_name}\t#{@devbrain_run_id}\t#{@devbrain_generation}\t#{pane_id}\t#{pane_pid}\t#{pane_dead}\t#{pane_dead_status}", check=False)
        if result.returncode:
            return None
        # tmux 3.8 clears pane_pid after exit. Preserve empty fields, including
        # the trailing exit status, so absence cannot shift identity columns.
        fields = result.stdout.rstrip("\r\n").split("\t")
        if len(fields) != 7 or fields[:3] != [row["tmux_session"], row["id"], str(row["generation"])]:
            raise ValueError("Native terminal identity does not match its registry")
        if not re.fullmatch(r"%[0-9]+", fields[3]) or fields[5] not in {"0", "1"}:
            raise ValueError("Native terminal pane metadata is invalid")
        dead = fields[5] == "1"
        if dead and not fields[4]:
            pid = None
        elif re.fullmatch(r"[0-9]+", fields[4]) and 0 < int(fields[4]) <= 2**31 - 1:
            pid = int(fields[4])
        else:
            raise ValueError("Native terminal process identity is invalid")
        return {"pane": fields[3], "pid": pid, "dead": dead,
                "exit_code": int(fields[6]) if re.fullmatch(r"[0-9]+", fields[6]) else None}

    def _refresh(self, row):
        if row["lifecycle"] not in LIVE:
            return row
        pane = self._pane(row)
        if pane is None:
            row.update(lifecycle="interrupted", state="interrupted", interrupted=True, ended_at=time.time())
            (self.directory / f"{row['id']}.launch.json").unlink(missing_ok=True)
        elif pane["dead"]:
            lifecycle = "exited" if pane["exit_code"] == 0 else "failed"
            row.update(lifecycle=lifecycle, state=lifecycle, exit_code=pane["exit_code"], ended_at=time.time())
            if row.get("purpose") == "auth":
                # Device codes and OAuth URLs stay only in the operational
                # terminal's volatile memory, which is discarded at exit.
                self._tmux("kill-session", "-t", row["tmux_session"], check=False)
        else:
            if not row.get("process_start"):
                # The durable starting intent precedes spawn. If the browser
                # host died before recording exec, recover the same owned pane.
                row.update(process_start=process_start(pane["pid"]), pgid=os.getpgid(pane["pid"]))
            lifecycle = "stopping" if row["lifecycle"] == "stopping" else "running"
            row.update(lifecycle=lifecycle, state=lifecycle, pid=pane["pid"], pane_id=pane["pane"])
        return row

    def list_runs(self, project_id=None):
        if not self.registry.exists():
            return []
        with self._locked():
            rows = self._read()
            before = json.dumps(rows, sort_keys=True)
            for row in rows:
                self._refresh(row)
                if row["id"] in self._channels:
                    self._channels[row["id"]].row = dict(row)
            if json.dumps(rows, sort_keys=True) != before:
                self._save(rows)
            return [dict(r) for r in rows if project_id is None or r.get("project_id") == project_id]

    def get_run(self, run_id):
        if not isinstance(run_id, str) or not _ID.fullmatch(run_id):
            raise ValueError("Invalid native run ID")
        for row in self.list_runs():
            if row["id"] == run_id:
                return row
        raise ValueError("Unknown native run")

    history = list_runs

    def _guard(self, run_id, generation=None, *, live=True):
        row = self.get_run(run_id)
        if generation is not None and (type(generation) is not int or row["generation"] != generation):
            raise ValueError("Native run generation changed")
        if live and row["lifecycle"] not in LIVE:
            raise ValueError("Native run is no longer running")
        return row

    def _memory_identity(self, row):
        if not row.get("session_id"):
            return {}
        try:
            result = httpx.get(self.settings.url + "/sessions/" + row["session_id"],
                headers={"Authorization": f"Bearer {self.settings.token}"}, timeout=2, trust_env=False)
            result.raise_for_status()
            status = result.json()
            if status.get("session_id") == row["session_id"]:
                return {key: status.get(key) for key in ("native_session_id", "acknowledged", "stale", "status")}
        except (httpx.HTTPError, ValueError):
            pass
        return {}

    def memory_status(self, run_id):
        row = self.get_run(run_id)
        result = self._memory_identity(row)
        if result.get("native_session_id"):
            with self._locked():
                rows = self._read()
                next(r for r in rows if r["id"] == run_id)["native_session_id"] = result["native_session_id"]
                self._save(rows)
        return result or {"status": "unavailable", "acknowledged": False, "stale": False}

    def start_project(self, project, clients=("codex", "claude"), worktree=None, *, slots=None, resume=False):
        from .integrations import install_integrations
        if not self.diagnostics()["available"]:
            raise ValueError("tmux_missing_or_unsupported: install tmux >= 3.4")
        selected = slots or [{"id": name, "client": name} for name in clients]
        if not selected or len(selected) > 8 or any(not isinstance(s, dict) or s.get("client") not in {"codex", "claude"}
                                                    or not _ID.fullmatch(str(s.get("id", ""))) for s in selected):
            raise ValueError("Select one to eight supported native client slots")
        if len({s["id"] for s in selected}) != len(selected):
            raise ValueError("Native slot IDs must be unique")
        project = {**project, "root": str(Path(worktree or project["root"]).expanduser().resolve())}
        installation = install_integrations(self.settings, project, clients=tuple(dict.fromkeys(s["client"] for s in selected)))
        runs, errors = [], []
        for slot in selected:
            try:
                runs.append(self.start(project, slot["client"], slot_id=slot["id"],
                                       resume=resume, installation=installation, model=slot.get("model"),
                                       profile_id=slot.get("profile_id")))
            except (ValueError, OSError) as exc:
                # Classifications are fixed; raw native/auth output is never retained.
                code = str(exc).partition(":")[0]
                allowed = {"login_required", "client_missing", "client_unsupported", "native_resume_unavailable",
                           "native_resume_unsupported", "tmux_missing_or_unsupported"}
                errors.append({"slot_id": slot["id"], "client": slot["client"],
                               "code": code if code in allowed else "native_launch_failed"})
        return {"runs": runs, "errors": errors}

    def start(self, project, client, worktree=None, slot_id=None, resume=False, *, installation=None, model=None,
              profile_id=None):
        from .integrations import prepare_client_launch
        if not self.diagnostics()["available"]:
            raise ValueError("tmux_missing_or_unsupported: install tmux >= 3.4")
        if client not in {"codex", "claude"}:
            raise ValueError("Unsupported native client")
        slot_id = slot_id or client
        profile_id = profile_id or client
        if not _ID.fullmatch(slot_id) or not _ID.fullmatch(profile_id):
            raise ValueError("Invalid native slot ID")
        project = {**project, "root": str(Path(worktree or project["root"]).expanduser().resolve())}
        # Capability preflight contains no inference and is kept separate from
        # login state. Unknown budget never masquerades as a signed-out account.
        from .client_auth import probe_client
        auth = probe_client(client)
        if auth.get("installation") == "missing":
            raise ValueError("client_missing: install the native CLI in this WSL distribution")
        if auth.get("installation") == "unsupported":
            raise ValueError("client_unsupported: update the native CLI")
        if (auth.get("auth") == "expired_or_rejected" or auth.get("auth") == "signed_out"
                and auth.get("credential_configuration") != "environment_present"):
            raise ValueError("login_required: log in using the native client")
        if model is not None and (not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}", model)):
            raise ValueError("Invalid native model identifier")
        args = None
        # Serialize slot allocation and preparation across browser requests.
        with self._locked():
            rows = self._read()
            matches = [r for r in rows if (r.get("project_id"), r.get("worktree"), r.get("client"), r.get("slot_id"), r.get("purpose"))
                       == (project["id"], project["root"], client, slot_id, "coding")]
            for old in reversed(matches):
                self._refresh(old)
                if old["lifecycle"] in LIVE:
                    self._save(rows)
                    return dict(old)
            if resume:
                old = matches[-1] if matches else None
                native = (old or {}).get("native_session_id") or (self._memory_identity(old).get("native_session_id") if old else None)
                if not isinstance(native, str) or not _ID.fullmatch(native):
                    raise ValueError("native_resume_unavailable: start a fresh verified session")
                binary = shutil.which(client)
                check = subprocess.run([binary, "resume", "--help"] if client == "codex" else [binary, "--help"],
                                       capture_output=True, text=True, timeout=4)
                if check.returncode or (client == "claude" and "--resume" not in check.stdout):
                    raise ValueError("native_resume_unsupported: start a fresh verified session")
                args = ["resume", native] if client == "codex" else ["--resume", native]
            if model:
                args = [*(args or []), "--model", model]
            launch = prepare_client_launch(self.settings, project, client, args, installation=installation,
                                           bootstrap_when_args=True)
            launch["model"] = model
            launch["profile_id"] = profile_id
            return self._spawn(rows, project, client, slot_id, launch,
                               max((r["generation"] for r in matches), default=0) + 1, "coding")

    def start_auth(self, client):
        from .client_auth import auth_command
        from .integrations import native_environment
        if not self.diagnostics()["available"]:
            raise ValueError("tmux_missing_or_unsupported: install tmux >= 3.4")
        command = auth_command(client)
        environment = {k: v for k, v in native_environment().items() if not k.startswith("DEVBRAIN_")}
        with self._locked():
            rows = self._read()
            for row in rows:
                if row.get("purpose") == "auth" and row["client"] == client and self._refresh(row)["lifecycle"] in LIVE:
                    return dict(row)
            return self._spawn(rows, {"id": None, "root": str(Path.home())}, client, "auth-" + client,
                {"command": command, "cwd": str(Path.home()), "env": environment,
                 "session_id": None, "packet_id": None, "context_path": None}, 1, "auth")

    def _spawn(self, rows, project, client, slot_id, launch, generation, purpose, *, run_id=None):
        if not self.diagnostics()["available"]:
            raise ValueError("tmux_missing_or_unsupported: install tmux >= 3.4")
        run_id = run_id or uuid4().hex
        if not isinstance(run_id, str) or not _ID.fullmatch(run_id):
            raise ValueError("Invalid terminal run ID")
        session = "devbrain_" + run_id
        spec = self.directory / f"{run_id}.launch.json"
        row = {"id": run_id, "run_id": run_id, "project_id": project["id"], "worktree": launch["cwd"],
               "client": client, "profile_id": launch.get("profile_id", client), "slot_id": slot_id, "generation": generation,
               "model": launch.get("model"),
               "purpose": purpose, "session_id": launch.get("session_id"), "packet_id": launch.get("packet_id"),
               "native_session_id": None, "tmux_session": session, "lifecycle": "starting", "state": "starting",
               "created_at": time.time(), "exit_code": None, "interrupted": False}
        rows.append(row)
        self._save(rows)
        try:
            _atomic(spec, {"run_id": run_id, "generation": generation, "command": launch["command"],
                           "environment": launch["env"], "cwd": launch["cwd"]})
            self._tmux("new-session", "-d", "-x", "120", "-y", "40", "-s", session, "-c", launch["cwd"],
                       "--", sys.executable, "-I", "-m", "cairnmind.native_worker", "--spec", str(spec),
                       ";", "set-option", "-t", session, "@devbrain_run_id", run_id,
                       ";", "set-option", "-t", session, "@devbrain_generation", str(generation))
            self.socket.chmod(0o600)
            marker = spec.with_suffix(".started.json")
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            if marker.exists():
                identity = json.loads(marker.read_text())
                row.update(pid=identity["pid"], process_start=identity["start"], pgid=identity["pgid"])
                marker.unlink()
            self._refresh(row)
            self._save(rows)
            return dict(row)
        except BaseException:
            self._tmux("kill-session", "-t", session, check=False)
            row.update(lifecycle="failed", state="failed", error_code="native_spawn_failed", ended_at=time.time())
            self._save(rows)
            raise
        finally:
            spec.unlink(missing_ok=True)

    def interrupt(self, run_id, generation=None):
        row = self._guard(run_id, generation)
        self._tmux("send-keys", "-t", row["tmux_session"], "C-c")
        return self.get_run(run_id)

    def stop(self, run_id, generation=None):
        row = self._guard(run_id, generation, live=False)
        pane = self._pane(row)
        if pane and not pane["dead"]:
            pid = pane["pid"]
            # Pane ownership plus Linux process start identity prevents PID reuse
            # from turning an old run's stop button into an unrelated signal.
            if row.get("pid") != pid or not row.get("process_start") or process_start(pid) != row["process_start"]:
                raise ValueError("Native process identity changed; stop refused")
            group = os.getpgid(pid)
            if group != row.get("pgid") or group != pid:
                raise ValueError("Native process group is not owned by this run")
            with self._locked():
                rows = self._read()
                current = next(r for r in rows if r["id"] == run_id)
                current.update(lifecycle="stopping", state="stopping")
                self._save(rows)
            try:
                os.killpg(group, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 3
            while process_start(pid) == row["process_start"] and time.monotonic() < deadline:
                time.sleep(.03)
            if process_start(pid) == row["process_start"]:
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if pane:
            self._tmux("kill-session", "-t", row["tmux_session"], check=False)
        with self._locked():
            rows = self._read()
            current = next(r for r in rows if r["id"] == run_id)
            current.update(lifecycle="exited", state="exited", ended_at=time.time(), stopped=True)
            self._save(rows)
        channel = self._channels.get(run_id)
        if channel:
            channel.row = dict(current)
            if not channel.closed and not channel.loop.is_closed():
                channel.loop.call_soon_threadsafe(channel.publish, {"type": "status", "run": dict(current)})
        return dict(current)

    async def subscribe(self, run_id, generation, connection_id):
        row = await asyncio.to_thread(self._guard, run_id, generation, live=False)
        if row["lifecycle"] not in LIVE and await asyncio.to_thread(self._pane, row) is None:
            raise ValueError("Native terminal is unavailable; start or explicitly resume a session")
        if not isinstance(connection_id, str) or not _ID.fullmatch(connection_id):
            raise ValueError("Invalid terminal connection ID")
        channel = self._channels.get(run_id)
        if channel is None or channel.closed:
            channel = _Channel(self, row)
            self._channels[run_id] = channel
        if connection_id in channel.attachments:
            raise ValueError("Terminal connection is already attached")
        attachment = TerminalAttachment(channel, connection_id)
        channel.attachments[connection_id] = attachment
        if channel.controller is None:
            channel.controller = connection_id
        attachment.queue.put_nowait({"type": "status", "run": row})
        # tmux owns the VT screen. It redraws after reconnect rather than
        # replaying an incomplete byte ring as if it were a complete screen.
        if channel.process.poll() is None:
            await asyncio.to_thread(self._tmux, "refresh-client", "-t", channel.tty, check=False)
        return attachment

    async def aclose(self):
        # A slow terminal detachment must not add another timeout for every
        # open project. Native panes stay alive independently in tmux.
        await asyncio.gather(*(channel.close() for channel in list(self._channels.values())))


class _Channel:
    def __init__(self, runtime, row):
        self.runtime, self.row = runtime, row
        self.attachments, self.controller, self.closed = {}, None, False
        self.loop = asyncio.get_running_loop()
        master, slave = os.openpty()
        self.fd, self.tty = master, os.ttyname(slave)
        os.set_blocking(master, False)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        env = dict(os.environ)
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)
        env.update(TERM="xterm-256color")
        try:
            self.process = subprocess.Popen([sys.executable, "-I", "-m", "cairnmind.native_worker", "--attach",
                "--tmux-bin", runtime.binary, "--socket", str(runtime.socket), "--session", row["tmux_session"]],
                stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
        finally:
            os.close(slave)
        self.loop.add_reader(master, self._read)

    def publish(self, event):
        for attachment in list(self.attachments.values()):
            if attachment.overflowed:
                continue
            try:
                attachment.queue.put_nowait(event)
            except asyncio.QueueFull:
                while not attachment.queue.empty():
                    attachment.queue.get_nowait()
                attachment.overflowed = True
                attachment.queue.put_nowait({"type": "gap", "reason": "slow_consumer"})

    def _read(self):
        try:
            data = os.read(self.fd, 32768)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if data:
            self.publish({"type": "output", "data": data})
        else:
            self.loop.remove_reader(self.fd)
            self.publish({"type": "status", "run": {**self.row, "attachment_closed": True}})
            self.loop.create_task(self.close())

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.loop.remove_reader(self.fd)
        # Detach only this cockpit terminal client, never its native pane.
        await asyncio.to_thread(self.runtime._tmux, "detach-client", "-t", self.tty, check=False)
        os.close(self.fd)
        if self.process.poll() is None:
            self.process.terminate()
        try:
            await asyncio.to_thread(self.process.wait, timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            await asyncio.to_thread(self.process.wait, timeout=3)


class TerminalAttachment:
    def __init__(self, channel, connection_id):
        self.channel, self.connection_id = channel, connection_id
        self.queue = asyncio.Queue(maxsize=128)
        self.overflowed = False

    @property
    def controller(self):
        return (not self.channel.closed and self.channel.row["lifecycle"] == "running"
                and self.channel.controller == self.connection_id and not self.overflowed)

    def _writable(self):
        if self.channel.closed or not self.controller:
            raise ValueError("Terminal controller lease is required")

    def take_control(self):
        if self.channel.closed or self.overflowed or self.channel.row["lifecycle"] not in LIVE:
            raise ValueError("Terminal attachment is closed")
        self.channel.controller = self.connection_id
        self.channel.publish({"type": "control", "controller": self.connection_id})

    async def write(self, data):
        self._writable()
        if not isinstance(data, bytes) or len(data) > 65536:
            raise ValueError("Terminal input must be at most 65536 bytes")
        remaining = memoryview(data)
        while remaining:
            self._writable()
            try:
                count = os.write(self.channel.fd, remaining)
                remaining = remaining[count:]
            except BlockingIOError:
                await asyncio.sleep(.005)

    def resize(self, cols, rows):
        self._writable()
        if any(type(v) is not int for v in (cols, rows)) or not 20 <= cols <= 500 or not 5 <= rows <= 200:
            raise ValueError("Terminal dimensions are outside supported bounds")
        fcntl.ioctl(self.channel.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    async def close(self):
        channel = self.channel
        channel.attachments.pop(self.connection_id, None)
        if channel.controller == self.connection_id:
            channel.controller = None
            channel.publish({"type": "control", "controller": None})
        if not channel.attachments:
            await channel.close()
