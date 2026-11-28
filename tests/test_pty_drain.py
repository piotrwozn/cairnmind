"""The pty drain thread is gone when stop() returns, so a closed descriptor number cannot be read by it (#419)."""
import os
import sys
import threading

import pytest
from pty_drain import drain_pty

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Tests POSIX os.openpty PTY draining")


def test_stop_ends_the_thread_before_the_descriptor_can_be_closed_and_reused():
    master, slave = os.openpty()
    output = bytearray()
    before = threading.active_count()
    stop = drain_pty(master, output)
    try:
        os.write(slave, b"one")
        for _ in range(200):
            if output:
                break
            threading.Event().wait(.01)
        assert bytes(output) == b"one"
        stop()
        assert threading.active_count() == before
        os.write(slave, b"two")  # nobody reads the master any more
        assert os.read(master, 16) == b"two" and bytes(output) == b"one"
    finally:
        os.close(master)
        os.close(slave)
