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


# The same attach, held at its first claiming tmux call until the test lets it go.
HELD_SCRIPT = SCRIPT.replace("raise SystemExit", """import time
import cairnmind.workspace as w
real, here = w.WorkspaceManager.tmux, Path(sys.argv[3])
def tmux(self, *args, **kwargs):
    if args[:1] == ("switch-client",):
        (here / "held").write_text("1")
        end = time.monotonic() + 8
        while not (here / "go").exists() and time.monotonic() < end:
            time.sleep(.02)
        if (here / "fail").exists():  # tmux refuses although the server is alive
            raise w.subprocess.CalledProcessError(1, args)
    return real(self, *args, **kwargs)
w.WorkspaceManager.tmux = tmux
raise SystemExit""")


def _attached(workspace, script=SCRIPT):
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 55, 180, 0, 0))
    process = subprocess.Popen([sys.executable, "-I", "-c", script, str(workspace.settings.data_dir), workspace.binary,
                                str(workspace.directory)],
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


def test_a_stop_that_comes_while_the_attach_is_claiming_still_ends_it_with_zero(manager):
    workspace, _, _ = manager
    workspace.create(start_supervisor=False)
    process, master, slave = _attached(workspace, HELD_SCRIPT)
    try:
        deadline = time.monotonic() + 8
        while not (workspace.directory / "held").exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert (workspace.directory / "held").exists()  # the attach is inside its claim, the client already there
        module.stop_workspace(workspace.settings)
        (workspace.directory / "go").write_text("1")  # the claim's tmux call now meets a server that is gone
        assert process.wait(timeout=8) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        os.close(master)
        os.close(slave)

@pytest.mark.parametrize("stop_under_way", [True, False])
def test_a_claim_refused_by_a_living_server_is_a_stop_only_when_the_stop_marker_is_there(manager, stop_under_way):
    # The stop writes its marker before it tells the server to exit, so the claim can fail while the server still answers.
    workspace, _, _ = manager
    workspace.create(start_supervisor=False)
    process, master, slave = _attached(workspace, HELD_SCRIPT)
    try:
        deadline = time.monotonic() + 8
        while not (workspace.directory / "held").exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert (workspace.directory / "held").exists()
        if stop_under_way:
            (workspace.directory / module.STOP_ASKED).write_text("1")
        (workspace.directory / "fail").write_text("1")
        (workspace.directory / "go").write_text("1")
        code = process.wait(timeout=8)
        assert (code == 0) is stop_under_way, code
        deadline = time.monotonic() + 8
        while workspace.clients() and time.monotonic() < deadline:
            time.sleep(.05)
        assert not workspace.clients()  # no unclaimed client stays attached either way
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        os.close(master)
        os.close(slave)
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
        # #169: a stop the person asked for is said in one sentence after tmux's own "server exited".
        # The slave stays open, so the reader never sees EOF: wait for the sentence, or for the output to stop growing.
        def said_now():
            return " ".join(bytes(process.captured).decode(errors="replace").split())
        deadline, size, still = time.monotonic() + 5, -1, 0
        while time.monotonic() < deadline:
            if by_person and module.STOPPED_ON_PURPOSE in said_now():
                break
            still = still + 1 if len(process.captured) == size else 0
            if not by_person and still >= 6:
                break
            size = len(process.captured)
            time.sleep(.05)
        assert (module.STOPPED_ON_PURPOSE in said_now()) is by_person
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        os.close(master)
        os.close(slave)
