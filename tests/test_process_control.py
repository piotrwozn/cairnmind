"""Cross-platform process control, command formatting, and Job Object / process-group kill tests."""
from __future__ import annotations

import subprocess
import sys
import time

from cairnmind.pal import process_control, process_identity


def test_popen_kwargs_and_format_command():
    detached = process_control.detached_popen_kwargs()
    grouped = process_control.group_popen_kwargs()
    if sys.platform == "win32":
        assert "creationflags" in detached and "start_new_session" not in detached
        assert "creationflags" in grouped and "start_new_session" not in grouped
        assert process_control.format_command(["cairnmind", "--data-dir", r"C:\My Data\cm", "stop"]) == (
            r'cairnmind --data-dir "C:\My Data\cm" stop'
        )
    else:
        assert detached == {"start_new_session": True}
        assert grouped == {"start_new_session": True}
        assert process_control.format_command(["cairnmind", "--data-dir", "/tmp/my data", "stop"]) == (
            "cairnmind --data-dir '/tmp/my data' stop"
        )


def test_process_job_terminates_parent_and_grandchild(tmp_path):
    pid_file = tmp_path / "grandchild.pid"
    parent_code = (
        "import subprocess, sys, time; from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8')\n"
        "time.sleep(60)\n"
    )
    job = process_control.ProcessJob()
    proc = subprocess.Popen(
        [sys.executable, "-c", parent_code, str(pid_file)],
        **process_control.group_popen_kwargs(),
    )
    try:
        assert job.attach(proc.pid)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (pid_file.exists() and pid_file.read_text().strip().isdigit()):
            time.sleep(0.05)
        assert pid_file.exists(), "grandchild pid was not recorded"
        grandchild_pid = int(pid_file.read_text().strip())
        assert process_identity.alive(grandchild_pid)
        job.terminate()
        proc.wait(timeout=10)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and process_identity.alive(grandchild_pid):
            time.sleep(0.05)
        assert not process_identity.alive(grandchild_pid)
    finally:
        job.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_terminate_pid_stops_child():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        process_control.terminate_pid(proc.pid, force=True)
        proc.wait(timeout=10)
        assert not process_identity.alive(proc.pid)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
