"""Read a pseudo-terminal's output in a thread that is stopped before the descriptor is closed (#419).

Not collected (no test_ prefix). A drain thread left running after os.close(fd) can read a different file that the
process opened and that got the same descriptor number (state.json, a pipe), and take its content from its reader.
"""
from __future__ import annotations

import os
import select
import threading


def drain_pty(fd, output):
    """Append what `fd` prints to `output` in a thread; returns stop(), to call before closing `fd`."""
    stopped = threading.Event()

    def run():
        while not stopped.is_set():
            if not select.select([fd], [], [], .05)[0]:
                continue
            try:
                data = os.read(fd, 65536)
            except OSError:
                return
            if not data:
                return
            output.extend(data)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    def stop():
        stopped.set()
        thread.join(5)
        assert not thread.is_alive(), "the pty drain thread did not stop"
    return stop
