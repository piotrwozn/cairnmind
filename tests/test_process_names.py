"""A process name is at most 15 bytes, so a name with multi-byte characters can be cut in the middle of one.
/proc/<pid>/stat then holds bytes that are not valid UTF-8; reading it must not fail."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import mcp_fixture_processes
from cairnmind import cockpit, native_worker, verification


@pytest.fixture
def cut_name_process(tmp_path):
    program = tmp_path / "xzażółćgęśląjaźń"  # the 15-byte limit falls inside the two bytes of "ś"
    shutil.copy(shutil.which("sleep"), program)
    process = subprocess.Popen([str(program), "30"])
    try:
        raw = Path(f"/proc/{process.pid}/stat").read_bytes()
        with pytest.raises(UnicodeDecodeError):
            raw.decode("utf-8")
        yield process.pid
    finally:
        process.kill()
        process.wait()


def test_a_process_with_a_cut_name_can_still_be_identified(cut_name_process):
    pid = cut_name_process
    start = native_worker.process_start(pid)
    assert start and start.isdigit()
    assert cockpit._birth(pid) == start
    assert verification._process_identity(pid) == start


def test_scanning_processes_survives_a_cut_name(cut_name_process):
    assert cut_name_process not in mcp_fixture_processes()
