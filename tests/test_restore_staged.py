"""A restore writes in bounded transactions and keeps one outcome with a mark of an unfinished restore (#362).

The store holds the backup, or what it held before; between those two, while a process runs or after it was
killed, it carries a mark, and nothing uses it until a restore finishes.
"""
import json
import os
import signal
import subprocess
import sys
import time

import pytest
from typer.testing import CliRunner

from cairnmind import api_runtime, arcade, cli, integrations, restore_staged, store_writer
from cairnmind.journal import Journal
from cairnmind.memory import MemoryManager
from cairnmind.memory_published import PUBLICATION_SWITCH, Publisher
from cairnmind.restore_staged import RestoreFailed, RestoreUnfinished
from cairnmind.store import GRAPH_ROWS_PER_REQUEST, Store
from cairnmind.store_readonly import StoreNotReady
from cairnmind.sql import utc_now

from test_backup_restore_stream import comparable
from test_restore_groups import _large
from test_store_writer import second_installation

runner = CliRunner()


def _mark(store, **changes):
    mark = {"backup": "/somewhere/the-backup.cairnmind.json.gz", "before": "/somewhere/before-restore-x.cairnmind.json.gz",
            "before_sha256": "0" * 64, "installation_id": store.installation.id, "started_at": utc_now(),
            "undoing": False, **changes}
    restore_staged.write_mark(store, mark)
    return mark


def _doctor(monkeypatch, store):
    """`cairnmind doctor --json` over the real store; the report of the rest is left empty and nothing is probed."""
    monkeypatch.setattr(cli, "service_alive", lambda settings: False)
    monkeypatch.setattr(integrations, "doctor", lambda settings: {})
    monkeypatch.setattr(store_writer, "inspect", lambda settings: {"role": "writer"})
    monkeypatch.setattr(api_runtime, "coding_profiles_status", lambda settings: [])
    return runner.invoke(cli.app, ["--data-dir", str(store.data_dir), "doctor", "--json"])


def test_a_store_with_the_mark_is_refused_by_every_opening_but_a_restore(tmp_path, monkeypatch):
    store = Store(tmp_path / "memory")
    assert restore_staged.unfinished(store.client) is None
    mark = _mark(store, undoing=True)
    with pytest.raises(RestoreUnfinished) as refused:
        Store(store.data_dir)
    text = str(refused.value)
    assert refused.value.mark == mark and mark["backup"] in text and mark["before"] in text
    assert text.count("cairnmind restore") == 2 and "putting the store back did not finish" in text
    with pytest.raises(RestoreUnfinished, match="did not finish"):
        Store.open_readonly(store.settings)
    # A restore opens it, and nothing else runs over it.
    again = Store(store.data_dir, for_restore=True)
    assert not again.read_only and again.confirmations_before_rule is None
    done = _doctor(monkeypatch, store)
    assert done.exit_code == 1, done.output
    report = json.loads(done.output)
    assert report["restore_unfinished"] == mark and any("did not finish" in note for note in report["environment_blockers"])
    restore_staged.clear_mark(store)
    assert json.loads(_doctor(monkeypatch, store).output)["restore_unfinished"] is None
    assert not Store(store.data_dir).read_only


def test_a_reader_is_refused_while_a_restore_is_unfinished(tmp_path):
    first = Store(tmp_path / "first")
    second_dir = second_installation(tmp_path, first)
    Store(second_dir)  # a reader of the same database, as before the restore began
    _mark(first)
    with pytest.raises(StoreNotReady, match="did not finish"):
        Store(second_dir)
    taking = Store(second_dir, for_takeover=True)  # a person can still take the writer role, and then restore
    assert taking.read_only and "did not finish" in taking.not_ready
    restore_staged.clear_mark(first)
    assert Store(second_dir).read_only


def test_an_unreadable_mark_refuses_every_opening(tmp_path, monkeypatch):
    store = Store(tmp_path / "memory")
    store.client.command("INSERT INTO devbrain_meta CONTENT :row", {"row": {"name": restore_staged.MARK, "backup": 7}})
    assert restore_staged.unfinished(store.client) == {}
    with pytest.raises(RestoreUnfinished, match="cannot be read") as refused:
        Store(store.data_dir)
    assert refused.value.mark == {}
    with pytest.raises(RestoreUnfinished, match="cannot be read"):
        Store.open_readonly(store.settings)
    done = _doctor(monkeypatch, store)
    assert done.exit_code == 1 and json.loads(done.output)["restore_unfinished"] == {"unreadable": True}


def _watching(monkeypatch):
    """Rows written or deleted per server transaction (a session) that committed: counted from the requests and
    answers, as the engine sees them."""
    sessions, commits = {}, []
    request = arcade.ArcadeClient._request

    def watched(self, method, path, **kwargs):
        body = kwargs.get("json") or {}
        command, params = str(body.get("command", "")), body.get("params") or {}
        response = request(self, method, path, **kwargs)
        rows = 0
        if command.startswith(("INSERT INTO", "CREATE VERTEX")) and "rows" in params:
            rows = len(params["rows"])
        elif command.count("CREATE EDGE"):
            rows = command.count("CREATE EDGE")
        elif command.startswith("DELETE FROM") and " LIMIT " in command:
            rows = response.json()["result"][0]["count"]
        if rows and self.session_id:
            sessions[self.session_id] = sessions.get(self.session_id, 0) + rows
        if path.startswith("/api/v1/commit/") and self.session_id in sessions:
            commits.append(sessions[self.session_id])
        return response

    monkeypatch.setattr(arcade.ArcadeClient, "_request", watched)
    return commits


def test_a_restore_commits_no_more_than_the_bound_per_transaction(tmp_path, monkeypatch):
    path = _large(Store(tmp_path / "memory"), tmp_path)
    source = Store(tmp_path / "memory")
    target = Store(tmp_path / "target")
    monkeypatch.setattr(restore_staged, "ROWS_PER_TRANSACTION", 100)
    commits = _watching(monkeypatch)
    target.restore(path)
    monkeypatch.undo()
    # The bound, and one group of the request limit that a commit waits for: never one transaction of 2,000 rows.
    assert len(commits) >= 8 and max(commits) < 100 + GRAPH_ROWS_PER_REQUEST, commits
    assert comparable(target.export()) == comparable(source.export())


def _failing_commits(monkeypatch, *numbers):
    """Make the ``n``-th commit of the write phase (counted over every restore of the test) fail, as a lost engine does."""
    seen = [0]

    def failing(self):
        seen[0] += 1
        if seen[0] in numbers:
            raise OSError("the engine went away")  # the transaction stays open: the restore rolls it back
        return commit(self)

    commit = restore_staged.Batches.commit
    monkeypatch.setattr(restore_staged.Batches, "commit", failing)
    return seen


def _copies(store, known=()):
    """The safety copies in the store's backups, without those that were there before."""
    return sorted(set((store.data_dir / "backups").glob("before-restore-*")) - set(known))


def _world(tmp_path, monkeypatch, rows=100):
    """A store with a backup of a large one and, since then, one more project: what a failed restore must keep."""
    store = Store(tmp_path / "memory")
    path = _large(store, tmp_path)
    store.add_project("after the backup", tmp_path / "after")
    monkeypatch.setattr(restore_staged, "ROWS_PER_TRANSACTION", rows)
    return store, path, comparable(store.export()), _copies(store)


def test_a_failure_after_several_commits_puts_the_store_back_and_keeps_the_copy(tmp_path, monkeypatch):
    store, path, before, known = _world(tmp_path, monkeypatch)
    seen = _failing_commits(monkeypatch, 4)
    with pytest.raises(RestoreFailed, match="went away.*put back as it was before") as failed:
        store.restore(path)
    assert seen[0] > 4  # three commits of the write phase had landed: the undo was really needed
    monkeypatch.undo()
    assert comparable(store.export()) == before
    assert len(_copies(store, known)) == 1 and str(_copies(store, known)[0]) in str(failed.value)
    assert restore_staged.unfinished(store.client) is None and not Store(store.data_dir).read_only


def test_a_failure_while_putting_back_leaves_the_store_refused_until_a_restore_finishes(tmp_path, monkeypatch):
    store, path, before, known = _world(tmp_path, monkeypatch)
    _failing_commits(monkeypatch, 4, 6)  # the write phase, and again while the undo writes
    with pytest.raises(RestoreFailed, match="failed too") as failed:
        store.restore(path)
    monkeypatch.undo()
    (copy,) = _copies(store, known)
    assert str(copy) in str(failed.value) and str(path) in str(failed.value)
    for opening in (lambda: Store(store.data_dir), lambda: Store.open_readonly(store.settings)):
        with pytest.raises(RestoreUnfinished) as refused:
            opening()
        assert str(copy) in str(refused.value) and str(path) in str(refused.value) and refused.value.mark["undoing"]
    # The person goes back to the store as it was, with the copy: the same command, the other file.
    Store(store.data_dir, for_restore=True).restore(copy)
    assert restore_staged.unfinished(store.client) is None and comparable(store.export()) == before
    assert _copies(store, known) == [copy]  # finishing a restore writes no second copy: the first one is the way back


def test_a_changed_copy_is_not_used_to_put_the_store_back(tmp_path, monkeypatch):
    store, path, before, known = _world(tmp_path, monkeypatch)
    other = store.backup(tmp_path / "other.cairnmind.json.gz").read_bytes()
    digest = restore_staged._file_sha256

    def changed_after_its_digest(copy):
        result = digest(copy)
        copy.write_bytes(other)  # a valid backup, but not the file this restore wrote
        return result

    monkeypatch.setattr(restore_staged, "_file_sha256", changed_after_its_digest)
    _failing_commits(monkeypatch, 4)
    with pytest.raises(RestoreFailed, match="changed after the restore wrote it"):
        store.restore(path)
    monkeypatch.undo()
    mark = restore_staged.unfinished(store.client)
    assert mark["undoing"] is True and mark["before"] == str(_copies(store, known)[0])
    with pytest.raises(RestoreUnfinished):
        Store(store.data_dir)


KILLED_RESTORE = """
import sys, time
from pathlib import Path
from cairnmind import restore_staged
from cairnmind.store import Store
restore_staged.ROWS_PER_TRANSACTION = 100
commit, seen = restore_staged.Batches.commit, [0]
def paused(self):
    commit(self)
    seen[0] += 1
    if seen[0] == 3:
        Path(sys.argv[3]).write_text("ready")
        time.sleep(120)
restore_staged.Batches.commit = paused
Store(Path(sys.argv[1]), for_restore=True).restore(Path(sys.argv[2]))
"""


def test_a_restore_killed_midway_is_refused_at_the_next_start_and_finishes_when_run_again(tmp_path, monkeypatch):
    store = Store(tmp_path / "memory")
    path = _large(store, tmp_path)
    saved = comparable(store.export())
    known = _copies(store)
    store.add_project("after the backup", tmp_path / "after")
    ready = tmp_path / "ready"
    child = subprocess.Popen([sys.executable, "-c", KILLED_RESTORE, str(store.data_dir), str(path), str(ready)],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        deadline = time.monotonic() + 120
        while not ready.exists():
            assert child.poll() is None, child.stdout.read().decode(errors="replace")
            assert time.monotonic() < deadline, "the restore never reached its third commit"
            time.sleep(0.05)
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # it already ended; the assertion above says how
        child.wait(timeout=30)
        child.stdout.close()
    (copy,) = _copies(store, known)
    with pytest.raises(RestoreUnfinished) as refused:
        Store(store.data_dir)
    assert not refused.value.mark["undoing"] and refused.value.mark["backup"] == str(path)
    assert str(copy) in str(refused.value)
    done = _doctor(monkeypatch, store)
    assert done.exit_code == 1 and json.loads(done.output)["restore_unfinished"]["backup"] == str(path)
    Store(store.data_dir, for_restore=True).restore(path)
    assert comparable(store.export()) == saved and restore_staged.unfinished(store.client) is None
    assert _copies(store, known) == [copy]  # no second copy: the one from before the killed restore stays the way back
    assert not Store(store.data_dir).read_only


def test_after_a_staged_restore_the_store_is_used_as_before(tmp_path, monkeypatch):
    monkeypatch.setattr(restore_staged, "ROWS_PER_TRANSACTION", 50)
    path = _large(Store(tmp_path / "origin"), tmp_path)
    target = Store(tmp_path / "target")
    target.restore(path)
    again = Store(tmp_path / "again")
    again.restore(target.backup(tmp_path / "second.cairnmind.json.gz"))
    assert comparable(again.export()) == comparable(target.export())
    # The journal names this installation and the last commit's epoch; writing goes on.
    head = target.query("SELECT FROM journal_state WHERE id='current'")[0]
    assert head["writer"] == target.installation.id and Journal(target).cursor()["epoch"] == head["epoch"]
    root = tmp_path / "repository"
    root.mkdir()
    for arguments in (("init", "-q", "-b", "main"), ("config", "user.name", "Fixture"),
                      ("config", "user.email", "fixture@example.invalid")):
        subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)
    (root / "app.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "initial"], check=True, capture_output=True)
    project = target.add_project("after the restore", root)["id"]
    source = target.snapshot(project, "note://after", "Written after a restore.", "note")
    target.remember(project, {"kind": "fact", "key": "after", "content": "Written after a restore."}, [source["id"]])
    assert MemoryManager(target).preview(project, worktree=str(root))["status"] == "preview"
    monkeypatch.setenv(PUBLICATION_SWITCH, "1")
    assert Publisher(target).publish(project, str(root), preview=True)
