"""Names of running processes and of whole-database backups after the rename (issue #40).

What a process or a file of an earlier version carries is still understood: the identity a
service announces, the session key of a spooled hook event, and a backup's suffix and format.
"""
from __future__ import annotations

import gzip
import json
import os
import subprocess
import time

import pytest

from cairnmind import cli, cockpit, sessions
from cairnmind.config import BACKUP_FORMATS, BACKUP_SUFFIXES, BROKER_NAMES, SERVICE_NAMES, Settings
from cairnmind.service import instance_id
from cairnmind.store import Store


class Reply:
    def __init__(self, payload):
        self.payload, self.is_success = payload, True

    def json(self):
        return self.payload


class Client:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, **kwargs):
        return Reply(self.payload)


@pytest.mark.parametrize("name, alive", [("cairnmind", True), ("devbrain", True), ("another-service", False)])
def test_a_service_of_this_or_an_earlier_version_is_recognised_as_ours(tmp_path, monkeypatch, name, alive):
    settings = Settings.load(tmp_path / "data", create=True)
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: Client({"service": name, "instance": instance_id(settings)}))
    assert cli.service_alive(settings) is alive
    assert SERVICE_NAMES[0] == "cairnmind" and BROKER_NAMES[0] == "cairnmind-cockpit"


@pytest.mark.parametrize("key", ["cairnmind_session_id", "devbrain_session_id"])
def test_a_spooled_event_of_this_or_an_earlier_hook_keeps_its_logical_session(tmp_path, key):
    store = Store(tmp_path / "memory")
    root = tmp_path / "repo"
    root.mkdir()
    project = store.add_project("demo", root)
    manager = sessions.SessionManager(store, Settings.load(store.data_dir))
    prepared = manager.prepare(project["id"], "codex", timeout=0)
    manager.note_event({"hook_event_name": "SessionStart", "session_id": "native-one", "cwd": str(root),
                        key: prepared["session_id"], "event_id": "event-one"}, "codex")
    bound = store.query("SELECT native_session_id FROM sessions WHERE id=:id", {"id": prepared["session_id"]})
    assert bound[0]["native_session_id"] == "native-one"


def test_a_backup_is_written_under_the_current_names_and_an_earlier_one_is_restored(tmp_path):
    store = Store(tmp_path / "memory")
    root = tmp_path / "repo"
    root.mkdir()
    project = store.add_project("demo", root)
    note = store.snapshot(project["id"], "note://one", "Kept across a restore.", "note")
    folder = tmp_path / "backups"
    folder.mkdir()
    written = store.backup(folder)
    assert written.name.startswith("cairnmind-") and written.name.endswith(BACKUP_SUFFIXES[0])
    with gzip.open(written, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    assert envelope["format"] == BACKUP_FORMATS[0] == "cairnmind-arcadedb-backup"
    # The same backup as 0.9.0 wrote it: the earlier format name under the earlier suffix.
    earlier = folder / "devbrain-earlier.devbrain.json.gz"
    with gzip.open(earlier, "wt", encoding="utf-8") as handle:
        json.dump({**envelope, "format": BACKUP_FORMATS[1]}, handle)
    store.remember(project["id"], {"kind": "fact", "key": "later", "content": "Written after the backup."}, [note["id"]])
    store.restore(earlier)
    assert store.list_records(project["id"]) == []
    foreign = folder / "foreign.cairnmind.json.gz"
    with gzip.open(foreign, "wt", encoding="utf-8") as handle:
        json.dump({**envelope, "format": "something-else"}, handle)
    with pytest.raises(ValueError, match="not a supported Cairnmind ArcadeDB backup"):
        store.restore(foreign)


def test_the_installer_refuses_while_a_workspace_runs_and_stops_the_background_processes(tmp_path):
    """The installer's first act on an installed version, with a stand-in for uv and for the installed command."""
    script = (__import__("pathlib").Path(__file__).resolve().parents[1] / "scripts" / "install-wsl.sh").read_text()
    start = script.index('installed_bin="$(uv tool dir --bin)/cairnmind"')
    block = script[start:script.index("\nfi\n", script.index('"$installed_bin" stop', start)) + 4]
    tools = tmp_path / "bin"
    tools.mkdir()
    log = tmp_path / "calls.log"
    (tools / "uv").write_text(f"#!/bin/sh\nif [ \"$1 $2 $3\" = 'tool dir --bin' ]; then echo {tools}; else echo 'cairnmind v0.9.0'; fi\n")
    (tools / "cairnmind").write_text(
        f"#!/bin/sh\necho \"$*\" >> {log}\n"
        "if [ \"$1 $2\" = 'workspace status' ]; then printf '{\\n  \"available\": %s\\n}\\n' \"$WORKSPACE\"; fi\n")
    for tool in tools.iterdir():
        tool.chmod(0o755)

    def run(**extra):
        log.write_text("")
        environment = {**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", **extra}
        environment.pop("CAIRNMIND_INSTALL_STOP_WORKSPACE", None) if "CAIRNMIND_INSTALL_STOP_WORKSPACE" not in extra else None
        result = subprocess.run(["bash", "-c", "set -euo pipefail\n" + block + "\necho continued"], env=environment,
                                capture_output=True, text=True, timeout=30)
        return result, log.read_text().splitlines()

    idle, calls = run(WORKSPACE="false")
    assert idle.returncode == 0 and "continued" in idle.stdout
    assert calls == ["workspace status", "cockpit stop", "stop"]
    busy, calls = run(WORKSPACE="true")
    assert busy.returncode == 1 and "continued" not in busy.stdout
    assert "A Cairnmind workspace is running" in busy.stderr and "cairnmind workspace stop" in busy.stderr
    assert calls == ["workspace status"]  # nothing was stopped behind the user's back
    told, calls = run(WORKSPACE="true", CAIRNMIND_INSTALL_STOP_WORKSPACE="1")
    assert told.returncode == 0 and calls == ["workspace status", "workspace stop", "cockpit stop", "stop"]


def test_daily_backups_of_both_suffixes_are_pruned_together_by_age(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    names = [f"{1000 + index}{BACKUP_SUFFIXES[index % 2]}" for index in range(10)]
    for index, name in enumerate(names):
        (folder / name).write_bytes(b"x")
        os.utime(folder / name, (time.time() - 1000 + index, time.time() - 1000 + index))
    (folder / "before-restore-abc.cairnmind.json.gz").write_bytes(b"x")  # not a daily backup: never pruned
    found = [path for suffix in BACKUP_SUFFIXES for path in folder.glob(f"[0-9]*{suffix}")]
    ordered = sorted(found, key=lambda path: (path.stat().st_mtime, path.name))
    assert [path.name for path in ordered] == names
    assert cockpit.BROKER_NAMES == BROKER_NAMES
