"""A restore writes in bounded transactions and keeps one outcome with a mark of an unfinished restore (#362).

The store holds the backup, or what it held before; between those two, while a process runs or after it was
killed, it carries a mark, and nothing uses it until a restore finishes.
"""
import json

import pytest
from typer.testing import CliRunner

from cairnmind import api_runtime, arcade, cli, integrations, restore_staged, store_writer
from cairnmind.restore_staged import RestoreUnfinished
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
