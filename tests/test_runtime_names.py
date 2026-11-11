"""Names of running processes and of whole-database backups after the rename (issue #40).

What a process or a file of an earlier version carries is still understood: the identity a
service announces, the session key of a spooled hook event, and a backup's suffix and format.
"""
from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

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


def installer_functions():
    script = (Path(__file__).resolve().parents[1] / "scripts" / "install-wsl.sh").read_text()
    start = script.index("# --- what an installed version left running")
    return script[start:script.index("# --- end ---", start)]


@pytest.fixture
def stand_ins(tmp_path):
    """A temporary directory for sockets, and stand-ins for tmux and for the installed command that log their calls."""
    tools, log = tmp_path / "bin", tmp_path / "calls.log"
    tools.mkdir()
    # Kept short: the path of a socket has a hard limit of about a hundred bytes.
    sockets = Path(tempfile.mkdtemp(prefix="cm-", dir="/tmp"))
    (tools / "tmux").write_text(
        "#!/bin/sh\necho \"tmux $3\" >> " + str(log) + "\n"
        "case \"$3\" in list-sessions) [ -e \"$2.alive\" ] ;; kill-server) rm -f \"$2.alive\" ;; esac\n")
    (tools / "cairnmind").write_text(
        "#!/bin/sh\necho \"$*\" >> " + str(log) + "\n"
        "if [ \"$1 $2\" = 'workspace status' ]; then [ \"$STATUS\" = fail ] && exit 3; "
        "printf '{\\n  \"available\": %s\\n}\\n' \"$STATUS\"; fi\n")
    for tool in tools.iterdir():
        tool.chmod(0o755)

    def terminal(name):
        """A listening socket where a terminal server of that name would have one, marked alive."""
        import socket
        folder = sockets / f"{name}-{os.getuid()}-0123456789abcdef"
        folder.mkdir()
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(folder / "tmux.sock"))
        (folder / "tmux.sock.alive").write_text("")
        return server, folder / "tmux.sock"

    def run(status="false", **extra):
        log.write_text("")
        environment = {key: value for key, value in os.environ.items() if key != "CAIRNMIND_INSTALL_STOP_WORKSPACE"}
        environment.update(PATH=f"{tools}:{os.environ['PATH']}", TMPDIR=str(sockets), STATUS=status, **extra)
        result = subprocess.run(
            ["bash", "-c", "set -euo pipefail\n" + installer_functions()
             + f'\nstop_installed "{tools}/cairnmind" cairnmind\necho continued'],
            env=environment, capture_output=True, text=True, timeout=30)
        return result, [line for line in log.read_text().splitlines() if not line.startswith("tmux list-sessions")]

    yield run, terminal
    shutil.rmtree(sockets, ignore_errors=True)


def test_with_nothing_running_the_installer_stops_only_the_background_processes(stand_ins):
    run, terminal = stand_ins
    result, calls = run()
    assert result.returncode == 0 and "continued" in result.stdout
    assert calls == ["workspace status", "cockpit stop", "stop"]


@pytest.mark.parametrize("name", ["cairnmind-ui", "devbrain-ui", "cairnmind-native", "devbrain-native"])
def test_the_installer_refuses_while_a_workspace_or_a_coding_session_runs(stand_ins, name):
    run, terminal = stand_ins
    server, socket_path = terminal(name)
    # The installed version itself reports no workspace: the terminal belongs to another data directory.
    result, calls = run(status="false")
    assert result.returncode == 1 and "continued" not in result.stdout
    assert str(socket_path) in result.stderr and "cairnmind workspace stop --clients" in result.stderr
    assert calls == ["workspace status"]  # nothing was stopped behind the user's back
    server.close()


def test_the_installer_refuses_on_a_workspace_that_only_the_installed_version_reports(stand_ins):
    run, terminal = stand_ins
    result, calls = run(status="true")
    assert result.returncode == 1 and "workspace reported by cairnmind workspace status" in result.stderr
    assert calls == ["workspace status"]


def test_told_to_the_installer_closes_everything_including_terminals_of_other_data_directories(stand_ins):
    run, terminal = stand_ins
    server, socket_path = terminal("devbrain-native")
    result, calls = run(status="true", CAIRNMIND_INSTALL_STOP_WORKSPACE="1")
    assert result.returncode == 0 and "continued" in result.stdout
    assert calls == ["workspace status", "workspace stop --clients", "cockpit stop --clients", "tmux kill-server",
                     "cockpit stop", "stop"]
    assert not Path(str(socket_path) + ".alive").exists()
    server.close()


def test_a_status_that_cannot_be_read_is_said_and_not_taken_for_no_workspace(stand_ins):
    run, terminal = stand_ins
    result, calls = run(status="fail")
    assert result.returncode == 0 and "could not be ruled out" in result.stderr


def test_scheduled_backups_of_both_suffixes_are_ordered_by_the_time_in_their_name(tmp_path):
    from cairnmind.service import daily_backups

    folder = tmp_path / "backups"
    assert daily_backups(folder) == []
    folder.mkdir()
    names = [f"{1794000000000000000 + index}{BACKUP_SUFFIXES[index % 2]}" for index in range(10)]
    for index, name in enumerate(names):
        (folder / name).write_bytes(b"x")
        # A copied directory: every file has the same, unrelated modification time.
        os.utime(folder / name, (1000, 1000))
    for other in ("before-restore-abc.cairnmind.json.gz", "before-schema-9-to-10-abc.devbrain.json.gz",
                  "cairnmind-20261111T000000-abcd1234.cairnmind.json.gz", "2026-notes.cairnmind.json.gz"):
        (folder / other).write_bytes(b"x")  # safety copies and user backups are not scheduled ones
    assert [path.name for path in daily_backups(folder)] == names
    assert cockpit.BROKER_NAMES == BROKER_NAMES
