"""The attached workspace process ends with code 0 when the person stops the workspace (#269)."""
from __future__ import annotations

import fcntl
import os
import struct
import subprocess
import sys
import termios
import threading
import time

import pytest

from test_workspace import manager as manager  # noqa: F401 (fixture)
import cairnmind.workspace as module

SCRIPT = """import fcntl, sys, termios
from pathlib import Path
from cairnmind.config import Settings
from cairnmind.workspace import WorkspaceManager
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
raise SystemExit(WorkspaceManager(Settings.load(Path(sys.argv[1])), tmux_binary=sys.argv[2]).attach())
"""


def _attached(workspace):
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 55, 180, 0, 0))
    process = subprocess.Popen([sys.executable, "-I", "-c", SCRIPT, str(workspace.settings.data_dir), workspace.binary],
                               stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
                               env={**os.environ, "TERM": "xterm-256color"})
    def drain():  # a terminal nobody reads blocks the tmux client
        while True:
            try:
                chunk = os.read(master, 65536)
                if not chunk:
                    return
                process.captured += chunk
            except OSError:
                return
    process.captured = bytearray()
    threading.Thread(target=drain, daemon=True).start()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and process.poll() is None and not workspace.clients():
        time.sleep(.05)
    assert process.poll() is None and workspace.clients()
    return process, master, slave


def test_a_failed_kill_server_leaves_no_stop_marker(manager, monkeypatch):
    workspace, _, _ = manager
    workspace.create(start_supervisor=False)
    real = module.WorkspaceManager.tmux

    def tmux(self, *args, **kwargs):
        if args[:1] == ("kill-server",):
            raise subprocess.TimeoutExpired("tmux", 8)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(module.WorkspaceManager, "tmux", tmux)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            module.stop_workspace(workspace.settings)
        assert not (workspace.directory / module.STOP_ASKED).exists()
    finally:
        monkeypatch.undo()
        workspace.tmux("kill-server")


@pytest.mark.parametrize("by_person, expected, stale", [(True, 0, False), (False, 1, False), (False, 1, True)])
def test_stop_asked_for_by_the_person_ends_the_attached_process_with_zero(manager, by_person, expected, stale):
    workspace, _, _ = manager
    workspace.create(start_supervisor=False)
    if stale:  # a marker left by an earlier stop must not turn this attach's real failure into 0
        (workspace.directory / module.STOP_ASKED).write_text("1")
    process, master, slave = _attached(workspace)
    try:
        if by_person:
            module.stop_workspace(workspace.settings)
        else:
            workspace.tmux("kill-server")  # the server killed from outside
        code = process.wait(timeout=8)
        # Killed from outside, tmux reports that the server exited unexpectedly: a failure, not zero.
        assert (code == 0) is (expected == 0), code
        time.sleep(.2)  # the reader thread has taken what the process wrote last
        # #169: a stop the person asked for is said in one sentence after tmux's own "server exited".
        said = " ".join(bytes(process.captured).decode(errors="replace").split())
        assert (module.STOPPED_ON_PURPOSE in said) is by_person
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        os.close(master)
        os.close(slave)
