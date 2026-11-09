"""Updater process ownership checks use real harmless processes, never a model."""

from __future__ import annotations

import hashlib
import importlib.util
import itertools
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest


@pytest.fixture
def updater():
    spec = importlib.util.spec_from_file_location(
        "update072_guards", str(Path(__file__).resolve().parents[1] / "releases/devbrain-update-0.7.2.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def accelerate(monkeypatch, module):
    original_sleep = time.sleep
    ticks = itertools.count(0, 0.25)
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(module.time, "sleep", lambda _: original_sleep(0.001))


def test_dead_and_reused_processes_are_never_signalled(updater, monkeypatch):
    monkeypatch.setattr(updater.os, "pidfd_open", lambda *a: pytest.fail("Foreign process handle opened"))
    monkeypatch.setattr(updater, "process_identity", lambda pid: None)
    updater.wait_for_broker_exit({"pid": 1234, "birth": "old"})
    monkeypatch.setattr(updater, "process_identity", lambda pid: ("new", "S"))
    updater.wait_for_broker_exit({"pid": 1234, "birth": "old"})


def test_embedded_http_host_cannot_be_killed(updater, monkeypatch):
    birth, _ = updater.process_identity(os.getpid())
    monkeypatch.setattr(
        updater.os, "pidfd_open", lambda *a: pytest.fail("Own test host opened for signalling")
    )
    accelerate(monkeypatch, updater)
    with pytest.raises(ValueError, match="Poprzedni broker nadal"):
        updater.wait_for_broker_exit({"pid": os.getpid(), "birth": birth, "instance": "not-a-daemon"})


@pytest.mark.skipif(
    not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"), reason="Linux pidfd required"
)
def test_stalled_owned_daemon_is_stopped_without_killing_unrelated_process(updater, monkeypatch, tmp_path):
    package = tmp_path / "devbrain"
    package.mkdir()
    (package / "__init__.py").write_text("")
    ready = tmp_path / "ready.json"
    (package / "__main__.py").write_text(
        "import json,os,signal,time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGINT,signal.SIG_IGN)\n"
        f'Path({str(ready)!r}).write_text(json.dumps({{"pid":os.getpid()}}))\n'
        "while True:time.sleep(.1)\n"
    )
    data = tmp_path / "isolated-data"
    owned = subprocess.Popen(
        [sys.executable, "-m", "devbrain", "--data-dir", str(data), "cockpit", "serve"],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert json.loads(ready.read_text())["pid"] == owned.pid
        birth, _ = updater.process_identity(owned.pid)
        info = {
            "pid": owned.pid,
            "birth": birth,
            "instance": hashlib.sha256(str(data.resolve()).encode()).hexdigest()[:24],
        }
        with monkeypatch.context() as patch:
            accelerate(patch, updater)
            updater.wait_for_broker_exit(info)
        assert owned.wait(timeout=3) == -signal.SIGKILL
        assert unrelated.poll() is None
    finally:
        for child in (owned, unrelated):
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=3)


def test_successful_installed_receipt_is_required_for_noop(updater, tmp_path, monkeypatch):
    # Exercise apply against a minimal authentic payload rather than source
    # replacement details: a version string alone cannot prove installation.
    import base64
    import zlib

    root = tmp_path / "source"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts/install-wsl.sh").write_text("exit 0\n")
    (root / "pyproject.toml").write_text('[project]\nversion="0.6.0"\n')
    payload = {"version": "0.6.0", "supported_versions": ["0.4.0", "0.5.0"], "files": []}
    monkeypatch.setattr(
        updater, "PAYLOAD", base64.b64encode(zlib.compress(json.dumps(payload).encode())).decode()
    )
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setattr(updater.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(updater, "installed_version", lambda binary: "0.6.0")
    calls = []

    def run(args, **kwargs):
        calls.append(list(map(str, args)))
        if args[0] == "/bin/tmux":
            return subprocess.CompletedProcess(args, 0, stdout="tmux 3.4\n")
        if args[1:] == ["workspace", "status"] or args[1:] == ["cockpit", "status"]:
            return subprocess.CompletedProcess(args, 2, stdout="")
        return subprocess.CompletedProcess(args, 0, stdout="")

    monkeypatch.setattr(updater.subprocess, "run", run)
    assert updater.apply(root) == 0
    assert any(row[:2] == ["bash", str(root / "scripts/install-wsl.sh")] for row in calls)
    assert list((state / "devbrain-updates").glob("installed-*.json"))
    calls.clear()
    assert updater.apply(root) == 0
    assert calls == [["/bin/tmux", "-V"]]


def test_known_version_file_deletion_is_not_mistaken_for_an_older_baseline(updater, tmp_path, monkeypatch):
    import base64
    import zlib

    root = tmp_path / "source"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts/install-wsl.sh").write_text("exit 0\n")
    (root / "pyproject.toml").write_text('[project]\nversion="0.5.0"\n')
    old, new = b"known optional file from 0.5", b"updated file"
    row = {
        "path": "optional.js",
        "before": [None, updater.digest(old)],
        "before_by_version": {"0.4.0": None, "0.5.0": updater.digest(old)},
        "after": updater.digest(new),
        "encoding": "utf-8",
        "content": new.decode(),
    }
    package = {"version": "0.6.0", "supported_versions": ["0.4.0", "0.5.0"], "files": [row]}
    monkeypatch.setattr(
        updater, "PAYLOAD", base64.b64encode(zlib.compress(json.dumps(package).encode())).decode()
    )
    with pytest.raises(ValueError, match="zmieniony lokalnie"):
        updater.apply(root, check_only=True)
    (root / "optional.js").write_bytes(old)
    assert updater.apply(root, check_only=True) == 0
    assert (root / "optional.js").read_bytes() == old
