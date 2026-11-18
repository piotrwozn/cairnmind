"""Under pytest-xdist, one worker's checks must not see or kill what another worker runs.

The fixture MCP server of another worker is its own copy of the script, under that worker's base
directory, so it is started here from such a copy, as another worker would have it running.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import pytest

import conftest
from conftest import mcp_fixture_processes


def start(script: Path, *modes: str) -> subprocess.Popen:
    # "deaf" ignores SIGTERM and keeps a child in its group, as the hostile fixture servers do.
    return subprocess.Popen([sys.executable, str(script), *modes], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def group_of(process: subprocess.Popen) -> set[int]:
    members = set()
    for pid in conftest.process_ids():
        try:
            if os.getpgid(pid) == process.pid:
                members.add(pid)
        except OSError:
            pass
    return members


def test_the_processes_of_the_machine_are_listed_without_procfs():
    # The ps(1) path of macOS, taken here on Linux too: it sees this process and a new one.
    with subprocess.Popen(["sleep", "30"]) as sleeper:
        try:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(conftest.sys, "platform", "darwin")
                listed = conftest.process_ids()
        finally:
            sleeper.kill()
    assert {os.getpid(), sleeper.pid} <= set(listed)


def test_the_fixture_servers_of_another_worker_are_not_this_workers(tmp_path):
    other_copy = tmp_path / "popen-gw9" / "mcp-fixture" / Path(conftest.MCP_FIXTURE).name
    other_copy.parent.mkdir(parents=True)
    shutil.copyfile(conftest.MCP_FIXTURE_SOURCE, other_copy)
    assert conftest.MCP_FIXTURE != str(conftest.MCP_FIXTURE_SOURCE)  # every pytest process has its own copy
    theirs, ours = start(other_copy, "deaf"), start(Path(conftest.MCP_FIXTURE), "deaf")
    try:
        deadline = time.monotonic() + 30
        while (len(group_of(theirs)) < 2 or len(group_of(ours)) < 2) and time.monotonic() < deadline:
            time.sleep(.05)
        assert len(group_of(theirs)) == 2 and len(group_of(ours)) == 2  # each server and its child run
        found = set(mcp_fixture_processes())
        assert group_of(ours) <= found  # this worker's server and child are seen, so a leak is caught
        assert not group_of(theirs) & found  # the other worker's are not: never killed, never blamed
    finally:
        for process in (theirs, ours):
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
