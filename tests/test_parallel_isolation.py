"""Under pytest-xdist, one worker's checks must not see or kill what another worker runs.

The fixture MCP server of another worker is its own copy of the script, under that worker's base
directory, so it is started here from such a copy, as another worker would have it running.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import conftest
import pytest
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


@pytest.mark.skipif(sys.platform == "win32", reason="Tests POSIX ps(1) fallback")
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


def test_an_engine_owner_whose_command_line_cannot_be_read_counts_as_alive(monkeypatch):
    # The orphan reaper must not stop the engine of a live run it cannot identify.
    monkeypatch.setattr(conftest.process_identity, "command_line", lambda pid: None)
    cmd = [sys.executable, "-c", "import time; time.sleep(30)"] if sys.platform == "win32" else ["sleep", "30"]
    with subprocess.Popen(cmd) as sleeper:
        try:
            assert conftest._pytest_alive(sleeper.pid)
        finally:
            sleeper.kill()
    assert not conftest._pytest_alive(sleeper.pid)


def test_reap_orphan_engines_never_stops_the_current_runs_shared_engine(tmp_path, monkeypatch):
    stopped = []
    monkeypatch.setattr(conftest, "stop_engine", lambda settings: stopped.append(settings.data_dir))
    monkeypatch.setattr(conftest, "_pytest_alive", lambda pid: False)
    current = tmp_path / "pytest-10"
    engine_dir = current / "arcade-engine-shared"
    (engine_dir / "arcade").mkdir(parents=True)
    (engine_dir / "owner.json").write_text('{"pid": 999999}', encoding="utf-8")
    (engine_dir / "arcade" / "process.json").write_text('{"pid": 999999}', encoding="utf-8")
    stale = tmp_path / "pytest-9" / "arcade-engine-shared"
    (stale / "arcade").mkdir(parents=True)
    (stale / "owner.json").write_text('{"pid": 999999}', encoding="utf-8")
    (stale / "arcade" / "process.json").write_text('{"pid": 999999}', encoding="utf-8")
    conftest.reap_orphan_engines(tmp_path, current_run=current)
    assert stopped == [stale]


@pytest.mark.skipif(sys.platform == "win32", reason="Tests POSIX process-group isolation (os.getpgid/os.killpg)")
def test_the_fixture_servers_of_another_worker_are_not_this_workers(tmp_path):
    other_copy = tmp_path / "popen-gw9" / "mcp-fixture" / Path(conftest.MCP_FIXTURE).name
    other_copy.parent.mkdir(parents=True)
    shutil.copyfile(conftest.MCP_FIXTURE_SOURCE, other_copy)
    assert str(conftest.MCP_FIXTURE_SOURCE) != conftest.MCP_FIXTURE  # every pytest process has its own copy
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
