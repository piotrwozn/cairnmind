"""Private tmux formats and exact terminal claims work with a C locale."""
from __future__ import annotations

import errno
import fcntl
import os
import select
import struct
import subprocess
import sys
import termios
import threading
import time

import pytest

from test_workspace import manager as manager


def _drain_pty(master, stopped, output, output_lock, errors):
    """Keep consuming terminal output until its owner has reaped the client."""
    try:
        while not stopped.is_set():
            if not select.select([master], [], [], .05)[0]:
                continue
            try:
                data = os.read(master, 65536)
            except BlockingIOError:
                continue
            except OSError as error:
                if error.errno == errno.EIO:
                    return  # The slave has closed after its terminal client exited.
                raise
            if not data:
                return
            with output_lock:
                output.extend(data)
                del output[:-131072]
    except Exception as error:
        errors.append(error)


@pytest.mark.parametrize("locale", ["C", "unset"])
def test_c_locale_formats_and_actual_attach_keep_exact_terminal_lease(manager, monkeypatch, locale):
    workspace, broker, _ = manager
    for key in list(os.environ):
        if key in {"LANG", "LANGUAGE"} or key.startswith("LC_"):
            monkeypatch.delenv(key, raising=False)
    if locale == "C":
        for key in ("LANG", "LC_ALL", "LC_CTYPE"):
            monkeypatch.setenv(key, "C")

    workspace.create(start_supervisor=False)
    assert set(workspace.panes()) == {"sidebar", "client1", "client2", "inspector", "graph"}
    marker = "zażółć λ"
    workspace.tmux("set-option", "-g", "@locale-check", marker)
    assert workspace.tmux("display-message", "-p", "#{@locale-check}\t#{@locale-check}").stdout == (
        marker + "\t" + marker + "\n")

    master, slave = os.openpty()
    terminal = os.ttyname(slave)
    os.set_blocking(master, False)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 55, 180, 0, 0))
    script = """import fcntl, os, sys, termios
from pathlib import Path
from cairnmind.config import Settings
from cairnmind.workspace import WorkspaceManager
# Python may coerce an unset locale at startup; restore the user's environment
# before creating either the query clients or the interactive tmux client.
for key in list(os.environ):
    if key in {"LANG", "LANGUAGE"} or key.startswith("LC_"):
        os.environ.pop(key)
if sys.argv[3] == "C":
    for key in ("LANG", "LC_ALL", "LC_CTYPE"):
        os.environ[key] = "C"
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
workspace = WorkspaceManager(Settings.load(Path(sys.argv[1])), tmux_binary=sys.argv[2])
raise SystemExit(workspace.attach())
"""
    process = None
    output = bytearray()
    output_lock = threading.Lock()
    stopped = threading.Event()
    drain_errors = []
    drainer = threading.Thread(target=_drain_pty,
        args=(master, stopped, output, output_lock, drain_errors), daemon=True)

    def output_tail():
        with output_lock:
            return bytes(output[-2000:])

    try:
        drainer.start()
        process = subprocess.Popen([sys.executable, "-I", "-c", script,
            str(workspace.settings.data_dir), workspace.binary, locale],
            stdin=slave, stdout=slave, stderr=slave,
            env={**os.environ, "TERM": "xterm-256color"}, start_new_session=True)
        deadline = time.monotonic() + 8
        clients = []
        while time.monotonic() < deadline and process.poll() is None:
            assert not drain_errors, drain_errors
            clients = workspace.clients()
            if (len(clients) == 1 and clients[0]["tty"] == terminal
                    and not clients[0]["readonly"] and not clients[0]["ignore_size"]):
                break
            time.sleep(.025)
        assert process.poll() is None, (process.returncode, output_tail())
        assert len(clients) == 1, (clients, output_tail())
        client = clients[0]
        assert client["pid"] > 0 and client["tty"] == terminal
        assert client["readonly"] is False and client["ignore_size"] is False
        assert workspace._claim_attached(-1, terminal) is None
        assert workspace._claim_attached(client["pid"], terminal + "-wrong") is None
        assert workspace.clients() == clients
        assert not broker.calls

        workspace.tmux("detach-client", "-t", terminal)
        assert process.wait(timeout=5) == 0
        assert workspace.exists()
        assert workspace.clients() == []
    finally:
        primary_error = sys.exception()
        cleanup_errors = []
        try:
            if process is not None and process.poll() is None:
                workspace.tmux("detach-client", "-t", terminal, check=False)
        except Exception as error:
            cleanup_errors.append(error)
        try:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        except Exception as error:
            cleanup_errors.append(error)
        stopped.set()
        if drainer.ident is not None:
            drainer.join(timeout=1)
            if drainer.is_alive():
                cleanup_errors.append(AssertionError("PTY drainer did not stop after the client cleanup"))
        for descriptor in (master, slave):
            try:
                os.close(descriptor)
            except OSError as error:
                cleanup_errors.append(error)
        cleanup_errors.extend(drain_errors)
        if cleanup_errors:
            if primary_error is not None:
                raise BaseExceptionGroup("Terminal trial and cleanup failed", [primary_error, *cleanup_errors]) from None
            raise ExceptionGroup("Terminal cleanup failed", cleanup_errors)
