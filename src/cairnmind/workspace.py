"""Private outer terminal workspace. Native agents remain in the existing broker."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
from uuid import uuid4

from rich.cells import cell_len

from .cockpit_state import CockpitState, _atomic, _private_directory, _private_lock
from .config import SESSION_NAMES, Settings, drop_env
from .native_worker import process_start
from .runtime_selection import resolve_tmux
from . import workspace_activity as column
from .terminal_theme import TMUX
from .workspace_activity import ACTIVITY_WIDTH, COCKPIT_MIN_WIDTH  # noqa: F401 (re-exported)
from .workspace_state import UI_COMMANDS, WorkspaceState

SESSION = "cairnmind-ui"
MINIMUM = (140, 42)
DEFAULT_SIZE = (180, 55)
_ROLES = ("sidebar", "client1", "client2", "inspector", "graph")


def reusable_native_run(runs, project_id, worktree, client, slot_id):
    """The live identity used by NativeRuntime.start, independent of model edits."""
    return next((r for r in reversed(runs)
        if (r.get("project_id"), r.get("worktree"), r.get("client"), r.get("slot_id"), r.get("purpose"))
        == (project_id, worktree, client, slot_id, "coding")
        and r.get("lifecycle") in {"starting", "running", "stopping"}), None)


def _status_text(value, columns):
    """Literal, cell-bounded text: user data is never a tmux format/program."""
    text = "".join(c for c in str(value or "") if not unicodedata.category(c).startswith("C"))
    text = text.replace("#", "＃")
    if cell_len(text) <= columns:
        return text
    result = ""
    for character in text:
        if cell_len(result + character) > columns - 1:
            break
        result += character
    return result + "…"


# The keys, on the right of the active view's border (#161). The two status rows replace tmux's own row,
# so status-right is never drawn; the rows are full at 80 columns, the border of the active view is not.
HINT_KEYS = "C-b Space: menu · C-b ?: help · C-b d: detach"
HINT_SHORT = "C-b ?: help"
HINT_READ_ONLY = "Read only · C-b d: detach"
HINT_READ_ONLY_SHORT = "C-b d: detach"
BORDER_RESERVE = 40  # the left part at its widest: role, control word and memory badge, with their spaces


def _hint(text):
    """Catalog text as a literal inside a tmux conditional: no format, no branch separator."""
    return _status_text(text, 60).replace(",", " ").replace("}", ")")


def border_hint(language="en"):
    """The keys for the active pane, cut to what its width holds: all of them, one, or none."""
    from .terminal_i18n import tr
    full, short = (_hint(tr(text, language)) for text in (HINT_KEYS, HINT_SHORT))
    read_only, read_only_short = (_hint(tr(text, language)) for text in (HINT_READ_ONLY, HINT_READ_ONLY_SHORT))
    wide = max(cell_len(full), cell_len(read_only)) + BORDER_RESERVE
    narrow = max(cell_len(short), cell_len(read_only_short)) + BORDER_RESERVE
    pick = lambda writer, reader: "#{?client_readonly," + reader + "," + writer + "}"  # noqa: E731
    return ("#{?pane_active,#{?#{e|>=:#{pane_width}," + str(wide) + "}," + pick(full, read_only) + " ,"
            "#{?#{e|>=:#{pane_width}," + str(narrow) + "}," + pick(short, read_only_short) + " ,}},}")


def tmux_chrome(language="en"):
    """tmux options that only colour the chrome, from the palette the panels use (terminal_theme.TMUX).

    Every option is set quietly, so a tmux that does not know one (popup styles need 3.2 or 3.3)
    starts as before. The pane badge shows the raw memory word; only exactly `ack` is the good colour.
    The active view's border also names the keys, on its right, in the panels' language.
    """
    c = {name: f"colour{cell}" for name, cell in TMUX.items()}
    badge = ("#{?#{==:#{@cairnmind_memory},ack},#[fg=" + c["ok"] + "],#{?#{==:#{@cairnmind_memory},unavailable},#[fg="
             + c["danger"] + "],#[fg=" + c["warn"] + "]}}#{@cairnmind_memory}#[default]")
    border = ("#[align=right]#[fg=" + c["label"] + "]" + border_hint(language) + "#[default]#[align=left]"
              " #{?pane_active,#[fg=" + c["accent"] + "]#[bold],#[fg=" + c["label"] + "]}#{@cairnmind_role}#[default] "
              "#[fg=" + c["label"] + "]#{@cairnmind_control}#[default] " + badge + " ")
    return [
        ("status-style", f"bg={c['bg']},fg={c['fg']}"),
        ("window-status-style", f"fg={c['label']}"),
        ("window-status-current-style", f"fg={c['accent']},bold"),
        ("pane-border-lines", "single"),
        ("pane-border-style", f"fg={c['line']}"),
        ("pane-active-border-style", f"fg={c['accent']}"),
        ("pane-border-format", border),
        ("popup-border-lines", "rounded"),
        ("popup-border-style", "fg=colour242"),
        ("popup-style", "bg=colour233"),
        ("message-style", "bg=colour236,fg=colour253"),
        ("message-command-style", f"bg=colour236,fg={c['accent']}"),
        ("mode-style", f"bg={c['select']},fg=colour255"),
        ("copy-mode-match-style", f"bg={c['line']},fg=colour255"),
        ("copy-mode-current-match-style", f"bg={c['accent']},fg=colour233"),
        ("display-panes-active-colour", c["accent"]),
        ("display-panes-colour", "colour242"),
    ]


def workspace_status_lines(selected, cached, slot, control, language="en"):
    """Two persistent rows fit 80 columns, including Polish and wide names.

    Connection describes cached local readiness, never an inferred provider
    connection. Only allowlisted scalars enter the display, not raw diagnostics.
    """
    from .terminal_i18n import tr

    good, attention, problem, neutral = TMUX["ok"], TMUX["warn"], TMUX["danger"], TMUX["accent"]
    separator = f"#[fg=colour{TMUX['sep']}] | #[default]"  # the same three cells, quieter than the chips

    def chip(label, value, color):
        return f"#[fg=colour{color}]" + tr(label, language) + ":" + value + "#[default]"

    project = _status_text(selected.get("project_name") or tr("None", language), 20)
    run_id = slot.get("run_id")
    lifecycle = slot.get("lifecycle")
    states = {"running": "Running", "starting": "Starting", "stopping": "Stopping",
              "exited": "Ended", "failed": "Failed", "interrupted": "Interrupted"}
    session = tr(states.get(lifecycle, "Unknown") if run_id else "None", language)
    if run_id:
        session = " ".join((_status_text(slot.get("client"), 7), _status_text(run_id, 8), session))
    session_color = good if lifecycle == "running" else problem if lifecycle in {"failed", "interrupted"} else attention
    first = " " + chip("Project", project, neutral) + separator + chip("Session", session, session_color)

    memory = slot.get("memory") or {}
    if selected.get("broker_available") is False or cached.get("memory_available") is False:
        memory_text, memory_color = "Unavailable", problem
    elif memory.get("stale"):
        memory_text, memory_color = "Stale", attention
    elif memory.get("status") in {"partial", "unavailable"}:
        memory_text, memory_color = ("Partial", attention) if memory["status"] == "partial" else ("Unavailable", problem)
    elif memory.get("acknowledged") is True:
        memory_text, memory_color = "ACK ready", good
    else:
        memory_text, memory_color = ("ACK needed", attention) if run_id else ("Not loaded", attention)

    profile = next((p for p in cached.get("profiles", []) if p.get("id") == slot.get("profile_id")), {})
    auth = cached.get("auth", {}).get(slot.get("client") or profile.get("client"), {})
    connection, connection_color = ("Configured", attention) if profile else ("Unknown", attention)
    if selected.get("broker_available") is False:
        connection, connection_color = "Offline", problem
    elif auth.get("installation") in {"missing", "unsupported"}:
        connection, connection_color = "Unavailable", problem
    elif auth.get("connectivity") == "unavailable":
        connection, connection_color = "Offline", problem
    elif auth.get("auth") == "expired_or_rejected" or auth.get("login_required") is True:
        connection, connection_color = "Login needed", problem
    elif auth.get("usage") in {"quota_exhausted", "rate_limited"}:
        connection, connection_color = "Limited", problem
    elif auth.get("error_code"):
        connection, connection_color = "Unknown", attention
    elif auth.get("auth") == "reported_signed_in" and auth.get("ready_to_launch") is True:
        connection, connection_color = "Ready(local)", good

    ownership, ownership_color = {"controller": ("You", good), "readonly": ("Other", attention),
        "offline": ("Offline", problem)}.get(control, ("Unknown", attention) if run_id else ("UI", neutral))
    # Evaluated by tmux for EACH attached terminal, even when a pane is zoomed.
    ownership_format = "#{?client_readonly," + chip("Control", tr("Read only", language), attention) + "," + chip("Control", tr(ownership, language), ownership_color) + "}"
    second = " " + chip("Memory", tr(memory_text, language), memory_color) + separator + chip("Connection", tr(connection, language), connection_color) + separator + ownership_format
    return first, second


def _clean_env():
    result = dict(os.environ)
    drop_env(result, *SESSION_NAMES)
    for key in ("TMUX", "TMUX_PANE", "CLAUDECODE", "CODEX_THREAD_ID"):
        result.pop(key, None)
    return result


class WorkspaceManager:
    def __init__(self, settings, workspace_id="default", tmux_binary=None, broker=None):
        self.settings, self.workspace_id = settings, workspace_id
        self.state = WorkspaceState(settings, workspace_id)
        self.directory = self.state.directory
        namespace = hashlib.sha256((str(settings.data_dir.resolve()) + ":" + workspace_id).encode()).hexdigest()[:16]
        self.socket_directory = Path(tempfile.gettempdir()) / f"cairnmind-ui-{os.getuid()}-{namespace}"
        self.socket = self.socket_directory / "tmux.sock"
        self.config = self.directory / "tmux.conf"
        self.binary = resolve_tmux(settings, override=tmux_binary)
        self._broker_instance = broker
        self._last_fetch = 0
        self._broker_state = None
        self._last_layout = None
        self._last_focus = None
        self._activity_restarted = float("-inf")
        self._last_status = None
        self._border_language = None

    @property
    def broker(self):
        if self._broker_instance is None:
            from .terminal_client import BrokerClient
            self._broker_instance = BrokerClient(self.settings, ensure=False)
        return self._broker_instance

    def diagnostics(self):
        if not self.binary:
            return {"available": False, "code": "tmux_missing", "minimum_version": "3.4"}
        try:
            probe = subprocess.run([self.binary, "-V"], capture_output=True, text=True, timeout=3)
            version = re.search(r"tmux (\d+)\.(\d+)", probe.stdout)
            valid = probe.returncode == 0 and version and tuple(map(int, version.groups())) >= (3, 4)
            return {"available": bool(valid), "version": probe.stdout.strip()[:80],
                    "code": "ready" if valid else "tmux_unsupported", "minimum_version": "3.4"}
        except (OSError, subprocess.SubprocessError):
            return {"available": False, "code": "tmux_unavailable", "minimum_version": "3.4"}

    def _initialize(self):
        if not self.diagnostics()["available"]:
            raise ValueError("Install tmux >= 3.4 in the same WSL distribution as Cairnmind.")
        _private_directory(self.directory.parent)
        _private_directory(self.directory)
        _private_directory(self.socket_directory)
        if len(os.fsencode(self.socket)) > 100:
            raise ValueError("The workspace socket path is too long.")
        if self.config.is_symlink():
            raise ValueError("The workspace configuration must not be a link.")
        if not self.config.exists():
            fd = os.open(self.config, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write("set -g prefix C-b\nunbind-key -a\nset -g mouse off\n"
                             "set -g default-terminal tmux-256color\nset -g status on\n"
                             "set -g status-left ' Cairnmind '\nset -g status-right ' C-b p/1/2/i/g · z zoom · d detach '\n"
                             "set -g pane-border-status top\n"
                             + "".join(f"set -gq {option} '{value}'\n" for option, value in tmux_chrome())
                             + "set -g remain-on-exit on\nset -g exit-empty off\nset -s set-clipboard off\n"
                             "set -g allow-passthrough off\nset -g history-limit 5000\n")
        info = self.config.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("The workspace configuration must be a private file of the user.")

    def tmux(self, *args, check=True, timeout=8):
        if not self.binary:
            raise ValueError("tmux is unavailable.")
        # Machine-readable formats require literal tabs and Unicode even when
        # the user's terminal locale is C or unset.
        return subprocess.run([self.binary, "-u", "-S", str(self.socket), "-f", str(self.config), *map(str, args)],
                              capture_output=True, text=True, timeout=timeout, check=check, env=_clean_env())

    def exists(self):
        if not self.binary or not self.socket.exists():
            return False
        try:
            result = self.tmux("show-options", "-v", "-t", SESSION, "@cairnmind_workspace", check=False)
            return result.returncode == 0 and result.stdout.strip() == self.workspace_id
        except (OSError, subprocess.SubprocessError, ValueError):
            return False

    def panes(self, roles=_ROLES):
        if not self.exists():
            return {}
        result = self.tmux("list-panes", "-a", "-F", "#{pane_id}\t#{@cairnmind_workspace}\t#{@cairnmind_role}\t#{window_name}")
        found = {}
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 4 and parts[1] == self.workspace_id and parts[2] in roles:
                found[parts[2]] = parts[0]
        return found

    def activity_pane(self):
        """The activity column, which is not one of the five views (no focus stop, no key of its own)."""
        return self.panes(("activity",)).get("activity")

    def clients(self):
        """Inspect only clients of this positively identified private session."""
        if not self.exists():
            return []
        result = self.tmux("list-clients", "-t", SESSION, "-F",
                           "#{client_pid}\t#{client_tty}\t#{client_flags}\t#{client_readonly}")
        clients = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 4 or not parts[0].isdigit() or not parts[1].startswith("/dev/"):
                continue
            flags = parts[2].split(",") if parts[2] else []
            clients.append({"pid": int(parts[0]), "tty": parts[1], "flags": flags,
                            "readonly": "read-only" in flags or parts[3] == "1", "ignore_size": "ignore-size" in flags})
        return clients

    def _claim_attached(self, pid, terminal, *, takeover=False):
        clients = self.clients()
        own = next((row for row in clients if row["pid"] == pid and row["tty"] == terminal), None)
        if own is None:
            return None
        writers = [row for row in clients if row is not own and not row["readonly"]]
        if takeover:
            for client in writers:
                self.tmux("refresh-client", "-t", client["tty"], "-f", "read-only,ignore-size")
        if not writers or takeover:
            # tmux3.4 deliberately refuses to clear READONLY through
            # refresh-client flags. switch-client -r is its supported toggle
            # and clears ignore-size together with read-only.
            if own["readonly"]:
                self.tmux("switch-client", "-r", "-c", own["tty"], "-t", SESSION)
            self.tmux("refresh-client", "-t", own["tty"], "-f", "!ignore-size")
            return True
        if not own["readonly"]:
            self.tmux("refresh-client", "-t", own["tty"], "-f", "read-only,ignore-size")
        self.tmux("display-message", "-c", own["tty"], "-d", "8000",
                  "READ ONLY: C-b d, potem cairnmind workspace takeover", check=False)
        return False

    def attach(self, *, takeover=False):
        """Attach without a writable interval before obtaining the UI lease.

        Every new client is read-only and ignored for sizing until its exact
        process/TTY identity is observed under the private attach lock. The
        broker still independently enforces control between this UI and web.
        """
        if type(takeover) is not bool or not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ValueError("Attaching needs an interactive terminal.")
        terminal = os.ttyname(sys.stdin.fileno())
        process = None
        try:
            with _private_lock(self.directory / "attach.lock"):
                if not self.exists():
                    raise ValueError("The terminal workspace is not running.")
                process = subprocess.Popen([self.binary, "-S", str(self.socket), "attach-session", "-r",
                                            "-f", "ignore-size", "-t", SESSION], env=_clean_env())
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline and process.poll() is None:
                    claimed = self._claim_attached(process.pid, terminal, takeover=takeover)
                    if claimed is not None:
                        break
                    time.sleep(.025)
                else:
                    if process.poll() is None:
                        raise ValueError("The identity of the attaching terminal was not confirmed.")
            # No lock is held while the interactive client is attached.
            return process.wait()
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)

    def _command(self, *arguments):
        return shlex.join([sys.executable, "-I", "-m", "cairnmind.workspace", "--data-dir", str(self.settings.data_dir),
                           "--workspace-id", self.workspace_id, *map(str, arguments)])

    def _panel(self, role):
        if role.startswith("client"):
            return self._command("--placeholder", role)
        return shlex.join([sys.executable, "-I", "-m", "cairnmind.terminal_panels", "--kind", role,
                           "--workspace-id", self.workspace_id, "--data-dir", str(self.settings.data_dir)])

    def _activity_command(self, *popup):
        return shlex.join([sys.executable, "-I", "-m", "cairnmind.terminal_panels", "--kind", "activity",
                           "--workspace-id", self.workspace_id, "--data-dir", str(self.settings.data_dir), *popup])

    def _tag(self, pane, role, run_id=None):
        self.tmux("set-option", "-p", "-t", pane, "@cairnmind_workspace", self.workspace_id)
        self.tmux("set-option", "-p", "-t", pane, "@cairnmind_role", role)
        self.tmux("set-option", "-p", "-t", pane, "@cairnmind_run_id", run_id or "")

    def _split(self, target, role, direction, percentage):
        pane = self.tmux("split-window", "-d", direction, "-t", target, "-l", str(percentage) + "%", "-P", "-F", "#{pane_id}", self._panel(role)).stdout.strip()
        self._tag(pane, role)
        return pane

    def create(self, *, start_supervisor=True):
        self._initialize()
        with _private_lock(self.directory / "ui.lock"):
            if not self.exists():
                sidebar = self.tmux("new-session", "-d", "-s", SESSION, "-n", "workspace", "-x", "180", "-y", "55",
                                    "-P", "-F", "#{pane_id}", self._panel("sidebar")).stdout.strip()
                self.tmux("set-option", "-t", SESSION, "@cairnmind_workspace", self.workspace_id)
                self._tag(sidebar, "sidebar")
                first = self._split(sidebar, "client1", "-h", 86)
                inspector = self._split(first, "inspector", "-v", 42)
                self._split(first, "client2", "-h", 50)
                self._split(inspector, "graph", "-h", 40)
                current = self.state.read()
                self.state.update({"pane_ids": self.panes(), "handled_work_request": current["work_request"],
                                   "intent": "inspect", "focus": "sidebar", "zoom": False})
                self._bindings()
                self._layout()
            self._recover_ui_panels()
            self._bindings()
            # Live as well as in the config: the config file is written once and is not rewritten.
            self._border_language = CockpitState(self.settings).load().get("language", "en")
            for option, value in tmux_chrome(self._border_language):
                self.tmux("set-option", "-gq", option, value)
            self.tmux("set-option", "-g", "status-right-length", "90")
            self.tmux("set-option", "-g", "status-right",
                      "#{?client_readonly, READ ONLY · C-b d: detach , C-b Space: commands · C-b ?: help · C-b Tab: next · C-b d: detach }")
            self.tmux("set-option", "-g", "status", "2")
            self.tmux("set-option", "-g", "status-interval", "1")
            self._update_status(self.state.read())
            if start_supervisor:
                self._start_supervisor()
        return self.status()

    def _recover_ui_panels(self, roles=("sidebar", "inspector", "graph", "activity"), *, run_request=False):
        # Only an explicit launch/create, or a key that asks for that view, retries crashed views.
        # Reconciliation never loops on a failing UI, and client/native panes are untouched.
        for role, pane in self.panes((*_ROLES, "activity")).items():
            if role not in roles:
                continue
            identity = self.tmux("display-message", "-p", "-t", pane,
                                 "#{@cairnmind_workspace}\t#{@cairnmind_role}\t#{pane_dead}").stdout.strip()
            if identity == f"{self.workspace_id}\t{role}\t1":
                command = self._activity_command() if role == "activity" else self._panel(role)
                # The menu request that asked for this view is already in the state: the new process runs it.
                self.tmux("respawn-pane", "-k", "-t", pane, command + (" --run-request" if run_request else ""))
                self._tag(pane, role)

    def _bindings(self):
        for key, role in (("p", "sidebar"), ("1", "client1"), ("2", "client2"), ("i", "inspector"), ("g", "graph")):
            arguments = ("--ui-command", "search") if role == "sidebar" else ("--focus", role)
            self.tmux("bind-key", key, "run-shell", self._command(*arguments))
        self.tmux("bind-key", "z", "run-shell", self._command("--toggle-zoom"))
        self.tmux("bind-key", "d", "detach-client")
        # Ctrl+Q in a Cairnmind view leaves like C-b d: only this terminal detaches and everything keeps
        # running. Native assistant panes get the key unchanged.
        self.tmux("bind-key", "-n", "C-q", "if-shell", "-F", "#{m/r:^(sidebar|inspector|graph|activity)$,#{@cairnmind_role}}",
                  "detach-client", "send-keys C-q")
        self.tmux("bind-key", "C-b", "send-prefix")
        client = ("--client-pid", "#{client_pid}", "--client-tty", "#{client_tty}")
        self.tmux("bind-key", "Space", "run-shell", self._command("--palette", *client))
        self.tmux("bind-key", "?", "run-shell", self._command("--help-popup", *client))
        # The form is a popup process of its own: typed text never enters this command line.
        self.tmux("bind-key", "n", "run-shell", self._command("--finding", *client))
        self.tmux("bind-key", "a", "run-shell", self._command("--activity", *client))
        self.tmux("bind-key", "Tab", "run-shell", self._command("--next-view"))
        self.tmux("bind-key", "[", "copy-mode")

    def _supervisor_info(self):
        path = self.directory / "supervisor.json"
        try:
            if path.is_symlink():
                return None
            info = json.loads(path.read_text())
            if (info.get("workspace_id") == self.workspace_id and info.get("birth")
                    and process_start(info["pid"]) == info["birth"]):
                return info
        except (OSError, ValueError, KeyError):
            pass
        return None

    def _start_supervisor(self):
        if self._supervisor_info():
            return
        current = self.state.read()
        if current["work_request"] != current["handled_work_request"]:
            # A durable UI selection is not durable authorization to start a
            # model after restarting the coordinator.
            self.state.update({"handled_work_request": current["work_request"], "intent": "inspect",
                               "last_error": "Start work again explicitly after the workspace resumes."})
        command = [sys.executable, "-I", "-m", "cairnmind.workspace", "--supervise", "--data-dir", str(self.settings.data_dir),
                   "--workspace-id", self.workspace_id, "--tmux-bin", self.binary]
        fd = os.open(self.directory / "supervisor.log", os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "ab") as output:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                       env=_clean_env(), start_new_session=True)
        _atomic(self.directory / "supervisor.json", {"pid": process.pid, "birth": process_start(process.pid),
                                                    "workspace_id": self.workspace_id})

    def _fetch(self, *, force=False):
        if self._broker_state is not None and not force and time.monotonic() - self._last_fetch < 2:
            return self._broker_state
        value = self.broker.request("GET", "/api/state")
        self._last_fetch, self._broker_state = time.monotonic(), value
        return value

    def _save_if_current(self, selected, changes):
        for _ in range(5):
            current = self.state.read()
            if (current["project_id"], current["worktree"]) != (selected["project_id"], selected["worktree"]):
                return current
            try:
                return self.state.update(changes, expected_revision=current["revision"])
            except ValueError as error:
                if "stale_workspace_revision" not in str(error):
                    raise
        return self.state.read()

    def _adopt(self, identifiers):
        return self.state.merge_ids("run_ids", identifiers)

    def refresh(self):
        if not self.exists():
            return self.status()
        with _private_lock(self.directory / "reconcile.lock"):
            selected = self.state.read()
            try:
                broker_state = self._fetch()
                projects = broker_state.get("projects", [])
                runs = broker_state.get("runs", [])
                profiles = {row["id"]: row for row in broker_state.get("profiles", [])}
                config = broker_state.get("settings", {})
                configured = config.get("project_slots", {}).get(selected["project_id"], config.get("default_slots", []))
                requested_now = []
                if (selected["intent"] == "work" and selected["work_request"] != selected["handled_work_request"]
                        and self.state.claim_work_request(selected)):
                    # Claim the user action before network work, so a daemon
                    # crash/retry cannot silently repeat a start request.
                    requested = selected.get("requested_slot_ids", [])
                    wanted = [slot for slot in configured if slot["id"] in requested]
                    if not requested or len(wanted) != len(requested):
                        raise ValueError("Choose a configured assistant before starting work")
                    result = self.broker.request("POST", "/api/client-runs", json={
                        "project": selected["project_id"], "worktree": selected["worktree"], "slots": wanted,
                        "idempotency_key": "workspace:" + hashlib.sha256((self.workspace_id + str(selected["work_request"])).encode()).hexdigest()})
                    runs = [*runs, *result.get("runs", [])]
                    self._adopt([run["id"] for run in result.get("runs", [])])
                    self._save_if_current(selected, {"launch_errors": result.get("errors", [])})
                    self._last_fetch = 0
                    requested_now = requested
                slots = []
                adopted = []
                for slot in configured:
                    profile = profiles.get(slot["profile_id"], {})
                    matching = [run for run in runs if run.get("project_id") == selected["project_id"]
                                and run.get("worktree") == selected["worktree"] and run.get("slot_id") == slot["id"]
                                and run.get("purpose") == "coding"
                                and run.get("client") == profile.get("client")
                                and run.get("profile_id", run.get("client")) == slot["profile_id"]]
                    # Profile/model edits apply only to a future launch. The
                    # already-running process remains the native slot's owner.
                    run = reusable_native_run(runs, selected["project_id"], selected["worktree"],
                                              profile.get("client"), slot["id"])
                    if run is None:
                        run = max(matching, key=lambda row: row.get("generation", 0), default=None)
                    item = dict(slot)
                    if run:
                        memory = run.get("memory") or {}
                        item.update(run_id=run["id"], generation=run["generation"], client=run.get("client"),
                                    lifecycle=run.get("lifecycle"),
                                    exit_code=run["exit_code"] if type(run.get("exit_code")) is int
                                    and 0 <= run["exit_code"] <= 255 else None,
                                    memory={key: memory.get(key) for key in ("status", "acknowledged", "stale")})
                        adopted.append(run["id"])
                    slots.append(item)
                self._adopt(adopted)
                visible = [identifier for identifier in selected["visible_slot_ids"] if any(slot["id"] == identifier for slot in slots)]
                if not visible:
                    visible = [slot["id"] for slot in slots[:2]]
                if requested_now:
                    visible = list(dict.fromkeys([*requested_now, *visible]))[:2]
                selected = self._save_if_current(selected, {"cached_projects": projects, "slots": slots,
                    "visible_slot_ids": visible, "broker_available": True, "last_error": None})
            except Exception:
                # Keep every existing bridge and native child alive when the
                # memory service/broker is unavailable. Never dump credentials.
                selected = self._save_if_current(selected, {"broker_available": False,
                    "last_error": "The client service is unavailable; running sessions are kept."})
                reconnect = getattr(self.broker, "reconnect", None)
                if reconnect is not None:
                    try:
                        reconnect()
                    except Exception:
                        pass
            with _private_lock(self.directory / "layout.lock"):
                self._reconcile_views(selected)
                self._arrange()
            self._update_status(self.state.read())
        return self.status()

    def _update_status(self, selected):
        panes = self.panes()
        previous = self.tmux("show-options", "-gv", "@cairnmind_status_role", check=False).stdout.strip()
        role = selected.get("focus") if selected.get("focus") in {"client1", "client2"} else previous
        role = role if role in panes and role in {"client1", "client2"} else "client1"
        if previous != role:
            self.tmux("set-option", "-g", "@cairnmind_status_role", role)
        visible = selected.get("visible_slot_ids", [])
        index = 1 if role == "client2" else 0
        identifier = visible[index] if index < len(visible) else None
        slot = next((s for s in selected.get("slots", []) if s["id"] == identifier), {})
        control = None
        if role in panes and slot.get("run_id"):
            fields = self.tmux("display-message", "-p", "-t", panes[role],
                "#{@cairnmind_binding}\t#{@cairnmind_control}").stdout.rstrip("\n").split("\t")
            if len(fields) == 2 and fields[0] == f"{slot['run_id']}:{slot.get('generation')}":
                control = fields[1]
        language = CockpitState(self.settings).load().get("language", "en")
        if language != self._border_language:
            # The key hint in the border follows a language switch like the status rows do (#161).
            self.tmux("set-option", "-gq", "pane-border-format", dict(tmux_chrome(language))["pane-border-format"])
            self._border_language = language
        formats = workspace_status_lines(selected, self._broker_state or {}, slot, control, language)
        if formats != self._last_status:
            for index, value in enumerate(formats):
                self.tmux("set-option", "-g", f"status-format[{index}]", value)
            self._last_status = formats

    def _bridge_command(self, run_id, generation):
        return shlex.join([sys.executable, "-I", "-m", "cairnmind.terminal_bridge", "--data-dir", str(self.settings.data_dir),
                           "--run-id", run_id, "--generation", str(generation), "--workspace-id", self.workspace_id])

    def _reconcile_views(self, selected):
        panes = self.panes()
        visible = [next(row for row in selected["slots"] if row["id"] == identifier) for identifier in selected["visible_slot_ids"]]
        wanted = max(1, len(visible))
        if wanted == 1 and "client2" in panes:
            self.tmux("kill-pane", "-t", panes["client2"])
            panes.pop("client2")
            self._last_layout = None
        elif wanted == 2 and "client2" not in panes and "client1" in panes:
            panes["client2"] = self._split(panes["client1"], "client2", "-h", 50)
            self._last_layout = None
        for index, role in enumerate(("client1", "client2")):
            if role not in panes:
                continue
            slot = visible[index] if index < len(visible) else {}
            run_id, generation = slot.get("run_id"), slot.get("generation")
            binding = (run_id or "") + ":" + str(generation or "")
            memory = slot.get("memory") or {}
            badge = ("stale" if memory.get("stale") else memory["status"] if memory.get("status") in {"partial", "unavailable"}
                     else "ack" if memory.get("acknowledged") is True else "ACK required" if run_id else "")
            if run_id and selected["broker_available"] is False:
                badge = "unavailable"
            self.tmux("set-option", "-p", "-t", panes[role], "@cairnmind_memory", badge)
            current = self.tmux("show-options", "-pv", "-t", panes[role], "@cairnmind_binding", check=False).stdout.strip()
            if current == binding:
                continue
            self.tmux("set-option", "-p", "-t", panes[role], "@cairnmind_control", "")
            command = self._bridge_command(run_id, generation) if run_id else self._panel(role)
            self.tmux("respawn-pane", "-k", "-t", panes[role], command)
            self._tag(panes[role], role, run_id)
            self.tmux("set-option", "-p", "-t", panes[role], "@cairnmind_binding", binding)
        self._save_if_current(selected, {"pane_ids": panes})

    def _layout(self):
        # Every process that arranges panes (the supervisor, each key) takes this lock: the
        # activity column is created and killed here, and two processes must never both do it.
        with _private_lock(self.directory / "layout.lock"):
            self._arrange()

    def _arrange(self):
        found = column.rows(self)  # one query; the activity column is not a view
        columns = [(pane, dead) for pane, role, dead in found if role == "activity"]
        for extra, _ in columns[:-1]:  # one column only, whatever left a second one behind
            self.tmux("kill-pane", "-t", extra, check=False)
        current, dead = columns[-1] if columns else (None, False)
        panes = {role: pane for pane, role, _ in found if role != "activity"}
        if "sidebar" not in panes:
            return
        selected = self.state.read()
        dimensions = self.tmux("display-message", "-p", "-t", panes["sidebar"], "#{window_width} #{window_height} #{window_zoomed_flag}").stdout.split()
        width, height, zoomed = map(int, dimensions)
        preference = CockpitState(self.settings).load().get("view", "focus")
        # window_height excludes the two persistent status rows; MINIMUM is
        # the user's complete terminal geometry, not just the pane area.
        compact = preference == "focus" or width < MINIMUM[0] or height + 2 < MINIMUM[1]
        activity = self._reconcile_activity(panes["sidebar"], current, dead, column.wanted(compact, width))
        if activity != current:  # adding or removing a pane unzooms the window
            zoomed = int(self.tmux("display-message", "-p", "-t", panes["sidebar"], "#{window_zoomed_flag}").stdout.strip() or 0)
        focus = panes.get(selected["focus"], panes.get("client1", panes["sidebar"]))
        layout = (width, height, len(panes), compact, activity)
        changed_focus = selected["focus"] != self._last_focus
        if compact:
            if changed_focus and zoomed:
                self.tmux("resize-pane", "-Z", "-t", focus)
                zoomed = False
            if changed_focus or not zoomed:
                self.tmux("select-pane", "-t", focus)
            if not zoomed:
                self.tmux("resize-pane", "-Z", "-t", focus)
        elif layout != self._last_layout:
            if zoomed:
                self.tmux("resize-pane", "-Z", "-t", focus)
            # Rebuild a deterministic layout in-place, moving only the UI
            # views within their own window; native panes are on another socket.
            self.tmux("select-layout", "-t", panes["sidebar"], "main-vertical")
            self.tmux("set-option", "-w", "-t", panes["sidebar"], "main-pane-width", "24")
            if column.order(self, panes["sidebar"], [panes[role] for role in column.ORDER if role in panes] + [activity] * bool(activity)):
                self._apply_layout(panes, width, height, activity)
            else:
                layout = None  # a pane this code does not know is in the window: try again later
            self.tmux("select-pane", "-t", focus)
            if selected["zoom"]:
                self.tmux("resize-pane", "-Z", "-t", focus)
        elif bool(zoomed) != selected["zoom"]:
            self.tmux("resize-pane", "-Z", "-t", focus)
        elif changed_focus:
            self.tmux("select-pane", "-t", focus)
        self._last_layout = layout
        self._last_focus = selected["focus"]
        if selected["layout"] != ("compact" if compact else "wide"):
            self._save_if_current(selected, {"layout": "compact" if compact else "wide"})

    def _reconcile_activity(self, window, pane, dead, wanted):
        """Create, restart or kill the activity column; a pane that is not wanted does not exist at all."""
        if wanted and pane is not None and dead:
            # Started again in place (never moved), and not in a tight loop if it ends at once.
            if time.monotonic() - self._activity_restarted >= column.RESPAWN_PAUSE:
                self._activity_restarted = time.monotonic()
                self.tmux("respawn-pane", "-k", "-t", pane, self._activity_command(), check=False)
                self._tag(pane, "activity")
        elif wanted and pane is None:
            pane = self.tmux("split-window", "-d", "-h", "-f", "-l", str(ACTIVITY_WIDTH), "-t", window,
                             "-P", "-F", "#{pane_id}", self._activity_command()).stdout.strip()
            self._tag(pane, "activity")
        elif not wanted and pane is not None:
            self.tmux("kill-pane", "-t", pane, check=False)  # already gone counts as removed
            pane = None
        return pane

    def _apply_layout(self, panes, width, height, activity=None):
        # tmux custom layout grammar: checksum, WxH,X,Y{horizontal children} or
        # [vertical children]. Pane IDs are server-assigned and never shell text.
        available = height
        left = 24
        right = width - left - 1 - (ACTIVITY_WIDTH + 1 if activity else 0)
        top = max(5, round((available - 1) * .58))
        bottom = available - top - 1
        first_width = (right - 1) // 2 if "client2" in panes else right
        inspector_width = round((right - 1) * .6)
        def leaf(role, w, h, x, y):
            return f"{w}x{h},{x},{y},{panes[role][1:]}"
        upper = leaf("client1", right, top, left + 1, 0)
        if "client2" in panes:
            upper = (f"{right}x{top},{left + 1},0{{" + leaf("client1", first_width, top, left + 1, 0) + "," +
                     leaf("client2", right - first_width - 1, top, left + first_width + 2, 0) + "}")
        lower = (f"{right}x{bottom},{left + 1},{top + 1}{{" + leaf("inspector", inspector_width, bottom, left + 1, top + 1) + "," +
                 leaf("graph", right - inspector_width - 1, bottom, left + inspector_width + 2, top + 1) + "}")
        edge = (f",{ACTIVITY_WIDTH}x{available},{left + right + 2},0,{activity[1:]}" if activity else "")
        description = (f"{width}x{available},0,0{{" + leaf("sidebar", left, available, 0, 0) + "," +
                       f"{right}x{available},{left + 1},0[" + upper + "," + lower + "]" + edge + "}")
        self.tmux("select-layout", "-t", panes["sidebar"], column.with_checksum(description))

    def focus(self, role):
        if role not in _ROLES:
            raise ValueError("Invalid workspace pane.")
        self._revive(role)
        panes = self.panes()
        target = panes.get(role, panes.get("client1"))
        if target:
            self.state.update({"focus": role})
            self._layout()
        return self.status()

    def toggle_zoom(self):
        preferences = CockpitState(self.settings)
        current_view = preferences.load().get("view", "focus")
        if current_view == "focus":
            preferences.update({"view": "cockpit"})
            self._last_layout = None
            self._layout()
            return self.status()
        current = self.state.read()
        self.state.update({"zoom": not current["zoom"]})
        self._last_layout = None
        self._layout()
        return self.status()

    def next_view(self):
        current = self.state.read()["focus"]
        roles = [role for role in _ROLES if role in self.panes()]
        if roles:
            return self.focus(roles[(roles.index(current) + 1) % len(roles)] if current in roles else roles[0])
        return self.status()

    def _revive(self, role, *, run_request=False):
        """A view whose process ended starts again when a key or the menu asks for it (Ctrl+B p/i/g, Tab).

        `run_request`: the command request for this view is written first, and the new process runs it;
        a panel otherwise treats the request it finds at start as already seen.
        """
        if role in {"sidebar", "inspector", "graph"} and self.exists():
            with _private_lock(self.directory / "ui.lock"):
                self._recover_ui_panels((role,), run_request=run_request)

    def request_command(self, action, target_role="sidebar"):
        self.state.update({"command_request": {"id": str(uuid4()), "action": action, "target_role": target_role},
                           "focus": target_role})
        self._revive(target_role, run_request=True)
        self._layout()

    def _owner(self, client_pid, client_tty):
        if not self.exists():
            raise ValueError("Open Cairnmind before using commands")
        owner = next((client for client in self.clients()
                      if client["pid"] == client_pid and client["tty"] == client_tty and not client["readonly"]), None)
        if owner is None:
            raise ValueError("The command menu requires the initiating writable terminal")
        return owner

    def open_palette(self, *, help_only=False, client_pid=None, client_tty=None):
        owner = self._owner(client_pid, client_tty)
        arguments = ["--palette-ui"] + (["--help-only"] if help_only else [])
        # An interactive popup ends when the user closes it, not after the
        # bounded timeout used for ordinary tmux queries.
        self.tmux("display-popup", "-c", owner["tty"], "-E", "-w", "90%", "-h", "85%",
                  self._command(*arguments), timeout=None)

    def open_activity(self, *, client_pid=None, client_tty=None):
        """Ctrl+B a: focus the activity column when it is on screen, otherwise open it as a popup."""
        owner = self._owner(client_pid, client_tty)
        pane = self.activity_pane()
        # A zoomed window hides the column, and a column whose process ended shows nothing: the popup then.
        if pane is not None and self.tmux("display-message", "-p", "-t", pane,
                                          "#{window_zoomed_flag} #{pane_dead}").stdout.strip() == "0 0":
            self.tmux("select-pane", "-t", pane)
            return
        self.tmux("display-popup", "-c", owner["tty"], "-E", "-w", "90%", "-h", "85%",
                  self._activity_command("--popup"), timeout=None)

    def open_finding(self, *, client_pid=None, client_tty=None):
        """The quick finding form, opened like the palette for the terminal that pressed the key."""
        owner = self._owner(client_pid, client_tty)
        self.tmux("display-popup", "-c", owner["tty"], "-E", "-w", "90%", "-h", "85%",
                  self._command("--finding-ui", "--client-tty", owner["tty"]), timeout=None)

    def take_control(self, run_id):
        for pane in self.panes().values():
            bound = self.tmux("show-options", "-pv", "-t", pane, "@cairnmind_run_id", check=False).stdout.strip()
            if bound == run_id:
                self.tmux("set-option", "-p", "-t", pane, "@cairnmind_takeover", str(uuid4()))
                return {"requested": True, "run_id": run_id}
        return {"requested": False, "run_id": run_id}

    def open_auth(self, client):
        if client not in {"codex", "claude", "copilot"} or not self.exists():
            raise ValueError("Choose a supported client in the open workspace.")
        run = (self.broker.request("POST", "/api/copilot/login", json={}) if client == "copilot"
               else self.broker.request("POST", "/api/auth/login", json={"client": client}))["run"]
        return self.show_auth_run(run)

    def show_auth_run(self, run):
        client = run.get("client")
        if client not in {"codex", "claude", "copilot"} or run.get("purpose") != "auth" or not self.exists():
            raise ValueError("Invalid sign-in terminal.")
        pane = self.tmux("new-window", "-t", SESSION, "-n", "login-" + client, "-P", "-F", "#{pane_id}",
                         self._bridge_command(run["id"], run["generation"])).stdout.strip()
        self._tag(pane, "auth", run["id"])
        self.tmux("set-option", "-w", "-t", pane, "history-limit", "0")
        self.state.merge_ids("auth_runs", [run["id"]])
        return {"run": run, "pane_id": pane}

    def auth_finished(self, run_id):
        if not self.exists():
            return {"closed": False}
        rows = self.tmux("list-panes", "-a", "-F", "#{pane_id}\t#{@cairnmind_workspace}\t#{@cairnmind_role}\t#{@cairnmind_run_id}").stdout.splitlines()
        # The callback can run inside the bridge being closed. Commit local
        # bookkeeping before kill-pane sends SIGHUP to that very process.
        self.state.merge_ids("auth_runs", remove=[run_id])
        closed = False
        own_pane = os.environ.get("TMUX_PANE")
        rows.sort(key=lambda row: row.split("\t")[0] == own_pane)
        for row in rows:
            parts = row.split("\t")
            if len(parts) == 4 and parts[1:] == [self.workspace_id, "auth", run_id]:
                self.tmux("clear-history", "-t", parts[0])
                self.tmux("kill-pane", "-t", parts[0])
                closed = True
        return {"closed": closed}

    def status(self):
        return {"available": self.exists(), "workspace_id": self.workspace_id, "socket": str(self.socket),
                "state": self.state.read()}

    def close(self):
        close = getattr(self._broker_instance, "close", None)
        if close is not None:
            close()

    def supervise(self):
        try:
            with _private_lock(self.directory / "supervisor.lock", blocking=False):
                while self.exists():
                    try:
                        self.refresh()
                    except (OSError, ValueError, subprocess.SubprocessError):
                        pass
                    time.sleep(.5)
        finally:
            self.close()


def workspace_status(settings):
    return WorkspaceManager(settings).status()


def launch_workspace(settings, takeover=False):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("Cairnmind needs an interactive terminal. Use `cairnmind workspace status` to diagnose.")
    manager = WorkspaceManager(settings)
    if not manager.diagnostics()["available"]:
        raise ValueError("Install tmux >= 3.4 in Ubuntu: sudo apt install tmux")
    manager.broker.ensure()
    manager.create()
    inherited = os.environ.get("TMUX", "").split(",", 1)[0]
    if inherited == str(manager.socket) and os.environ.get("TMUX_PANE") in manager.panes().values():
        if takeover:
            raise ValueError("Detach this view with C-b d and run `cairnmind workspace takeover` in another terminal.")
        return 0
    # A different user's tmux server/config is neither modified nor adopted.
    if not takeover and any(not client["readonly"] for client in manager.clients()):
        from .terminal_commands import JoinWorkspaceApp
        choice = JoinWorkspaceApp(settings).run()
        if choice is None:
            manager.close()
            return 0
        takeover = choice == "takeover"
    return manager.attach(takeover=takeover)


def refresh_workspace(settings, workspace_id="default"):
    manager = WorkspaceManager(settings, workspace_id)
    try:
        return manager.refresh()
    finally:
        manager.close()


def detach_workspace(settings):
    manager = WorkspaceManager(settings)
    if not manager.exists():
        return {"detached": False, "available": False}
    manager.tmux("detach-client", "-s", SESSION, check=False)
    return {"detached": True, "available": True}


_ENDED = {"exited", "failed", "interrupted"}


def _unknown_run(error):
    """The broker answered that it has no run with this id: 404, or the 400 of a broker before 0.11."""
    status, detail = getattr(error, "status_code", None), getattr(error, "detail", None)
    return status == 404 or (status == 400 and detail == "Unknown native run")


def _forget_finished(manager, finished):
    """Save what the stop did: the supervisor that would have refreshed the slots is stopped with it (#154)."""
    manager.state.merge_ids("run_ids", remove=list(finished))
    for _ in range(5):
        current = manager.state.read()
        slots = [{**slot, "lifecycle": finished[slot["run_id"]]} if slot.get("run_id") in finished else slot
                 for slot in current["slots"]]
        try:
            manager.state.update({"slots": slots}, expected_revision=current["revision"])
            return
        except ValueError as error:
            if "stale_workspace_revision" not in str(error):
                raise


def stop_workspace(settings, stop_clients=False):
    manager = WorkspaceManager(settings)
    current = manager.state.read()
    stopped, ended, errors, finished, unknown = [], [], [], {}, []
    if stop_clients:
        for run_id in current["run_ids"]:
            try:
                try:
                    run = manager.broker.request("GET", "/api/client-runs/" + run_id)
                except Exception as error:
                    if _unknown_run(error):
                        # The broker keeps no such run (its records were cleared): nothing runs to stop (#161).
                        unknown.append(run_id)
                        continue
                    raise
                if run.get("lifecycle") in _ENDED:
                    # Its process has already ended: there is nothing to stop, and that is not an error.
                    ended.append(run_id)
                    finished[run_id] = run["lifecycle"]
                    continue
                result = manager.broker.request("POST", "/api/client-runs/" + run_id + "/stop", json={"generation": run["generation"]})
                stopped.append(run_id)
                lifecycle = result.get("lifecycle") if isinstance(result, dict) else None
                finished[run_id] = lifecycle if lifecycle in _ENDED else "exited"
            except Exception:
                errors.append({"run_id": run_id, "code": "client_stop_unavailable"})
    running = manager.exists()
    if running:
        manager.tmux("kill-server")
    info = manager._supervisor_info()
    if info:
        try:
            os.kill(info["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
    if finished:
        _forget_finished(manager, finished)
    if unknown:
        manager.state.merge_ids("run_ids", remove=unknown)
    manager.close()
    return {"stopped": running, "clients_stopped": stopped, "clients_already_ended": ended,
            "clients_unknown": unknown, "errors": errors}


def show_auth(settings, client, workspace_id="default"):
    manager = WorkspaceManager(settings, workspace_id)
    try:
        return manager.open_auth(client)
    finally:
        manager.close()


def take_control(settings, run_id, workspace_id="default"):
    return WorkspaceManager(settings, workspace_id).take_control(run_id)


def select_run(settings, run_id, workspace_id="default"):
    manager = WorkspaceManager(settings, workspace_id)
    run = manager.broker.request("GET", "/api/client-runs/" + run_id)
    if run.get("purpose") == "auth":
        try:
            return manager.show_auth_run(run)
        finally:
            manager.close()
    state = manager._fetch(force=True)
    project = next(item for item in state["projects"] if item["id"] == run["project_id"])
    manager.state.select_project(project, run["worktree"], intent="inspect")
    return manager.refresh()


def resume_run(settings, run_id, workspace_id="default"):
    manager = WorkspaceManager(settings, workspace_id)
    run = manager.broker.request("GET", "/api/client-runs/" + run_id)
    result = manager.broker.request("POST", "/api/client-runs", json={"project": run["project_id"], "worktree": run["worktree"],
        "profile_id": run.get("profile_id", run["client"]), "slot_id": run["slot_id"], "resume": True, "idempotency_key": str(uuid4())})
    manager.refresh()
    return result


def stop_run(settings, run_id, generation, workspace_id="default"):
    manager = WorkspaceManager(settings, workspace_id)
    result = manager.broker.request("POST", "/api/client-runs/" + run_id + "/stop", json={"generation": generation})
    manager.refresh()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--workspace-id", default="default")
    parser.add_argument("--tmux-bin")
    parser.add_argument("--supervise", action="store_true")
    parser.add_argument("--focus", choices=_ROLES)
    parser.add_argument("--toggle-zoom", action="store_true")
    parser.add_argument("--placeholder")
    parser.add_argument("--palette", action="store_true")
    parser.add_argument("--help-popup", action="store_true")
    parser.add_argument("--palette-ui", action="store_true")
    parser.add_argument("--help-only", action="store_true")
    parser.add_argument("--finding", action="store_true")
    parser.add_argument("--finding-ui", action="store_true")
    parser.add_argument("--next-view", action="store_true")
    parser.add_argument("--activity", action="store_true")
    parser.add_argument("--ui-command", choices=UI_COMMANDS)
    parser.add_argument("--client-pid", type=int)
    parser.add_argument("--client-tty")
    args = parser.parse_args()
    settings = Settings.load(args.data_dir)
    manager = WorkspaceManager(settings, args.workspace_id, tmux_binary=args.tmux_bin)
    if args.palette_ui:
        from .terminal_commands import CommandPaletteApp, dispatch_command
        dispatch_command(manager, CommandPaletteApp(manager, help_only=args.help_only).run())
    elif args.palette or args.help_popup:
        manager.open_palette(help_only=args.help_popup, client_pid=args.client_pid, client_tty=args.client_tty)
    elif args.finding_ui:
        from .workspace_finding import run_finding_form
        run_finding_form(manager, args.client_tty)
    elif args.finding:
        manager.open_finding(client_pid=args.client_pid, client_tty=args.client_tty)
    elif args.activity:
        manager.open_activity(client_pid=args.client_pid, client_tty=args.client_tty)
    elif args.next_view:
        manager.next_view()
    elif args.ui_command:
        manager.request_command(args.ui_command)
    elif args.supervise:
        manager.supervise()
    elif args.focus:
        manager.focus(args.focus)
    elif args.toggle_zoom:
        manager.toggle_zoom()
    elif args.placeholder:
        title, quiet = f"\033[1;38;5;{TMUX['accent']}m", f"\033[38;5;{TMUX['label']}m"
        print(f"\033[2J\033[H{title}Cairnmind · Workspace\033[0m\n")
        print(f"{quiet}Open a project, choose your assistant, then select Start work.\n")
        print("Browsing memory never starts a model.\nC-b Space: commands · C-b p: projects · C-b ?: help · C-b d: detach\033[0m")
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    main()
