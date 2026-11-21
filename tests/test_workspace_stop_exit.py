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
                if not os.read(master, 65536):
                    return
            except OSError:
                return
    threading.Thread(target=drain, daemon=True).start()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and process.poll() is None and not workspace.clients():
        time.sleep(.05)
    assert process.poll() is None and workspace.clients()
    return process, master, slave


@pytest.mark.parametrize("by_person, expected", [(True, 0), (False, 1)])
def test_stop_asked_for_by_the_person_ends_the_attached_process_with_zero(manager, by_person, expected):
    workspace, _, _ = manager
    workspace.create(start_supervisor=False)
    process, master, slave = _attached(workspace)
    try:
        if by_person:
            module.stop_workspace(workspace.settings)
        else:
            workspace.tmux("kill-server")  # the server killed from outside
        code = process.wait(timeout=8)
        # Killed from outside, tmux reports that the server exited unexpectedly: a failure, not zero.
        assert (code == 0) is (expected == 0), code
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        os.close(master)
        os.close(slave)
