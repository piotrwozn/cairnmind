"""A restore writes in bounded transactions and keeps one outcome with a mark of an unfinished restore (#362).

The store holds the backup, or what it held before; between those two, while a process runs or after it was
killed, it carries a mark, and nothing uses it until a restore finishes.
"""
import gzip
import json
import os
import signal
import subprocess
import sys
import time

import pytest
from typer.testing import CliRunner

from conftest import counted_waits
from cairnmind.agents import api_runtime
from cairnmind.storage import arcade, restore_staged, store_writer
from cairnmind import cli
from cairnmind.pal import file_locks
from cairnmind.memory import integrations
from cairnmind.storage.journal import Journal, JournalTransaction
from cairnmind.memory.memory import MemoryManager
from cairnmind.memory.memory_published import PUBLICATION_SWITCH, Publisher
from cairnmind.storage.restore_staged import RestoreFailed, RestoreUnfinished
from cairnmind.store import GRAPH_ROWS_PER_REQUEST, Store
from cairnmind.storage.store_readonly import StoreNotReady
from cairnmind.storage.sql import utc_now

from cairnmind.storage.restore_claims import VECTORS_REBUILT, VECTORS_UNDECIDED, VECTORS_WAIT
from test_backup_restore_stream import comparable
from test_backup_without_vectors import embedded  # noqa: F401 - a fixture
from test_restore_groups import _large
from test_store_writer import second_installation

runner = CliRunner()


def _mark(store, **changes):
    mark = {"backup": "/somewhere/the-backup.cairnmind.json.gz", "before": "/somewhere/before-restore-x.cairnmind.json.gz",
            "before_sha256": "0" * 64, "installation_id": store.installation.id, "before_installation": store.installation.id,
            "started_at": utc_now(), "undoing": False, **changes}
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
    assert text.count("cairnmind restore") == 3 and "putting the store back did not finish" in text
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
    sessions, commits, unbounded = {}, [], []
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
        elif command.startswith("DELETE FROM") and " WHERE " not in command and "journal_" not in command:
            unbounded.append(command)  # every row of a type in one statement: what 0.12.0 did
        if rows and self.session_id:
            sessions[self.session_id] = sessions.get(self.session_id, 0) + rows
        if path.startswith("/api/v1/commit/") and self.session_id in sessions:
            commits.append(sessions[self.session_id])
        return response

    monkeypatch.setattr(arcade.ArcadeClient, "_request", watched)
    return commits, unbounded


def test_a_restore_commits_no_more_than_the_bound_per_transaction(tmp_path, monkeypatch):
    path = _large(Store(tmp_path / "memory"), tmp_path)
    source = Store(tmp_path / "memory")
    target = Store(tmp_path / "target")
    monkeypatch.setattr(restore_staged, "ROWS_PER_TRANSACTION", 100)
    commits, unbounded = _watching(monkeypatch)
    target.restore(path)
    monkeypatch.undo()
    # The bound, and one group of the request limit that a commit waits for: never one transaction of 2,000 rows.
    assert len(commits) >= 8 and max(commits) < 100 + GRAPH_ROWS_PER_REQUEST, commits
    assert not unbounded, unbounded  # and every delete names its limit
    assert comparable(target.export()) == comparable(source.export())


def _failing_commits(monkeypatch, *numbers, error=OSError("the engine went away")):
    """Make the ``n``-th commit of the write phase (counted over every restore of the test) fail, as a lost engine does."""
    seen = [0]

    def failing(self):
        seen[0] += 1
        if seen[0] in numbers:
            raise error  # the transaction stays open: the restore rolls it back
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


def test_the_commits_of_the_undo_wait_as_long_as_those_of_the_restore(tmp_path, monkeypatch):
    store, path, before, known = _world(tmp_path, monkeypatch, rows=50)
    _failing_commits(monkeypatch, 3)
    commits, request = [], arcade.ArcadeClient._request

    def recording(self, method, request_path, **kwargs):
        if request_path.startswith("/api/v1/commit/"):
            commits.append(kwargs.get("timeout", arcade.REQUEST_TIMEOUT))
        return request(self, method, request_path, **kwargs)

    monkeypatch.setattr(arcade.ArcadeClient, "_request", recording)
    with pytest.raises(RestoreFailed, match="put back as it was before"):
        store.restore(path)
    monkeypatch.undo()
    assert comparable(store.export()) == before
    # The commits at either end are not the restore's own (see test_restore_network); between them are its mark, its
    # deletes and writes up to the failing one, and the undo's.
    assert len(commits) > 8 and set(commits[1:-1]) == {arcade.RESTORE_COMMIT_TIMEOUT}


def test_an_engine_that_runs_out_of_memory_is_named_in_the_error_of_the_restore(tmp_path, monkeypatch):
    store, path, before, known = _world(tmp_path, monkeypatch)
    _failing_commits(monkeypatch, 4, error=arcade.ArcadeError("ArcadeDB HTTP 500: Java heap space", status_code=500))
    with pytest.raises(RestoreFailed, match=r"Java heap space\. The database engine ran out of memory.*put back as it was"):
        store.restore(path)
    monkeypatch.undo()
    assert comparable(store.export()) == before


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


def test_a_failed_restore_over_an_unreadable_mark_does_not_put_back_a_store_of_unknown_content(tmp_path, monkeypatch):
    store, path, before, known = _world(tmp_path, monkeypatch)
    store.client.command("INSERT INTO devbrain_meta CONTENT :row", {"row": {"name": restore_staged.MARK, "backup": 7}})
    _failing_commits(monkeypatch, 3)
    with pytest.raises(RestoreFailed, match="not known and it was not put back"):
        Store(store.data_dir, for_restore=True).restore(path)
    monkeypatch.undo()
    assert _copies(store, known) == []  # a copy of a store that may be half restored would be no way back
    assert restore_staged.unfinished(store.client) == {}
    with pytest.raises(RestoreUnfinished, match="cannot be read"):
        Store(store.data_dir)
    Store(store.data_dir, for_restore=True).restore(path)  # a restore that finishes is the way out
    assert restore_staged.unfinished(store.client) is None and not Store(store.data_dir).read_only


def _failing_before_the_first_delete(monkeypatch, store):
    """The engine goes away after the mark is written and before the first row is deleted; nothing is put back."""
    armed, query, mark = [], store.query, restore_staged.write_mark

    def marked(target, row):
        mark(target, row)
        armed.append(True)

    def failing(sql, params=None):
        if armed and "vector_indexes" in sql:
            raise OSError("the engine went away")
        return query(sql, params)

    def never(*arguments):
        raise AssertionError("an intact store must not be rewritten from the copy")

    monkeypatch.setattr(restore_staged, "write_mark", marked)
    monkeypatch.setattr(restore_staged, "_undo", never)
    monkeypatch.setattr(store, "query", failing)


def test_a_failure_before_the_first_delete_leaves_the_store_untouched_and_is_not_undone(tmp_path, monkeypatch):
    store, path, before, known = _world(tmp_path, monkeypatch)
    _failing_before_the_first_delete(monkeypatch, store)
    with pytest.raises(RestoreFailed, match="before it changed any row: the store is as it was"):
        store.restore(path)
    monkeypatch.undo()
    assert comparable(store.export()) == before and restore_staged.unfinished(store.client) is None
    assert len(_copies(store, known)) == 1 and not Store(store.data_dir).read_only


def test_a_failure_before_the_first_delete_that_cannot_remove_the_mark_says_both_and_keeps_the_store_refused(
        tmp_path, monkeypatch):
    store, path, before, known = _world(tmp_path, monkeypatch)
    _failing_before_the_first_delete(monkeypatch, store)

    def gone(target):
        raise OSError("the engine is still gone")

    monkeypatch.setattr(restore_staged, "clear_mark", gone)
    with pytest.raises(RestoreFailed) as failed:  # not the bare OSError: the person needs the first error too
        store.restore(path)
    monkeypatch.undo()
    text = " ".join(str(failed.value).split())
    assert "the engine went away" in text and "mark could not be removed (the engine is still gone)" in text
    assert f"cairnmind restore {path}" in text
    # What was said is what holds: the store carries the mark and is refused, and a restore that finishes clears it.
    with pytest.raises(RestoreUnfinished):
        Store(store.data_dir)
    Store(store.data_dir, for_restore=True).restore(path)
    assert restore_staged.unfinished(store.client) is None and not Store(store.data_dir).read_only


def test_a_group_of_edges_that_came_back_short_is_written_again_and_loses_no_edge(tmp_path, monkeypatch):
    """#377, #396: the unique index lookup on nodes.id may miss vertices that exist, so a group of edges comes back
    short. That is the failure injected here, once: the group is committed, waited for and written again, and the
    edges the first try wrote are kept, not written twice. (A group that keeps missing fails by name: the tests of
    `_hiding_the_index`.)"""
    store, path, before, known = _world(tmp_path, monkeypatch)
    store.restore(path)
    restored = comparable(store.export())
    script, missed = JournalTransaction.script, []

    def missing(self, statements, params, project_id):
        found = script(self, statements, params, project_id)
        if not missed and any(str(sql).startswith("CREATE EDGE edges") for sql in statements):
            missed.append(True)
            return [{"n": found[0]["n"] - 1}]
        return found

    monkeypatch.setattr(JournalTransaction, "script", missing)
    store.restore(path)
    monkeypatch.undo()
    assert missed and comparable(store.export()) == restored
    assert restore_staged.unfinished(store.client) is None and not Store(store.data_dir).read_only


def test_an_engine_error_inside_a_group_of_edges_is_named_as_it_is_and_not_retried(tmp_path, monkeypatch):
    """The engine rolled its transaction back: a commit and a retry would only replace its error with a 404."""
    store, path, before, known = _world(tmp_path, monkeypatch)
    script, tried = JournalTransaction.script, []

    def heap(self, statements, params, project_id):
        if not tried and any(str(sql).startswith("CREATE EDGE edges") for sql in statements):  # the undo writes edges too
            tried.append(True)
            raise arcade.ArcadeError("ArcadeDB HTTP 500: Java heap space", status_code=500)
        return script(self, statements, params, project_id)

    monkeypatch.setattr(JournalTransaction, "script", heap)
    with pytest.raises(RestoreFailed, match=r"Java heap space\. The database engine ran out of memory.*put back as it was"):
        store.restore(path)
    monkeypatch.undo()
    assert len(tried) == 1 and comparable(store.export()) == before


def _hiding_the_index(monkeypatch, hidden, at="wait"):
    """The engine, compacting its unique index on nodes.id, finds no vertex for a while after a commit (#377). From the
    wait for the vertices (`at="wait"`: the index misses during the vertex phase) or from the first edge group
    (`at="edges"`: it misses again after the wait), the next `hidden` lookups of vertices by id answer nothing,
    and so does every later lookup of a transaction that has missed one, as under the engine's REPEATABLE_READ (measured
    on 26.11.1): the end points of its CREATE EDGE are not found either. The sleeps that follow a hidden answer are counted, not slept; others (a real lag) are slept, not counted (#417)."""
    from cairnmind.storage import graph_transactions, restore_groups
    query, script, write = JournalTransaction.query, JournalTransaction.script, restore_groups.GraphGroups._write
    state = {"hidden": hidden, "armed": False, "stale": []}

    def hides(tx):
        if tx in state["stale"]:
            return True
        if state["armed"] and state["hidden"]:
            state["hidden"] -= 1
            state["stale"].append(tx)
            state["waits_for"].missed = True
            return True
        return False

    def hiding_query(self, sql, params=None):
        found = query(self, sql, params)
        return [] if "FROM nodes WHERE id IN" in sql and hides(self) else found

    def hiding_script(self, statements, params, project_id):
        if any(str(sql).startswith("CREATE EDGE edges") for sql in statements) and hides(self):
            params = {key: f"hidden {value}" if key[0] in "ft" and key[1:].isdigit() else value for key, value in params.items()}
        return script(self, statements, params, project_id)

    def armed_write(self, project):
        state["armed"] = state["armed"] or (at == "edges" and self.kind == "edge")
        return write(self, project)

    def armed_wait(*args):
        state["armed"] = state["armed"] or at == "wait"
        state["waits"] += 1
        return wait(*args)
    wait, state["waits"] = restore_groups.await_vertices, 0
    monkeypatch.setattr(restore_groups, "await_vertices", armed_wait)
    monkeypatch.setattr(JournalTransaction, "query", hiding_query)
    monkeypatch.setattr(JournalTransaction, "script", hiding_script)
    monkeypatch.setattr(restore_groups.GraphGroups, "_write", armed_write)
    state["waits_for"] = counted_waits(monkeypatch, graph_transactions)
    return state


def test_a_restore_waits_for_the_index_to_find_the_vertices_it_committed_before_it_writes_their_edges(tmp_path, monkeypatch):
    """#377: the edges are written by looking their vertices up in the index of nodes.id, as the indexer's are."""
    store, path, before, known = _world(tmp_path, monkeypatch)
    store.restore(path)
    restored = comparable(store.export())
    state = _hiding_the_index(monkeypatch, 2)
    store.restore(path)
    assert state["hidden"] == 0 and len(state["waits_for"].slept) == 2  # it waited, and wrote every edge after
    assert state["waits"] == 1  # once for the restore, not before each group of edges (#396)
    monkeypatch.undo()
    assert comparable(store.export()) == restored and restore_staged.unfinished(store.client) is None


def test_a_group_of_edges_whose_vertices_the_index_misses_again_is_committed_waited_for_and_written_once_more(
        tmp_path, monkeypatch):
    """#396: the wait before the edges is one; a group that still misses a vertex is the retry, and loses no edge."""
    store, path, before, known = _world(tmp_path, monkeypatch)
    store.restore(path)
    restored = comparable(store.export())
    state = _hiding_the_index(monkeypatch, 1, at="edges")
    store.restore(path)
    assert state["hidden"] == 0 and state["waits"] == 2  # the wait before the edges, and the group's own
    monkeypatch.undo()
    assert comparable(store.export()) == restored and restore_staged.unfinished(store.client) is None


def test_a_group_that_misses_its_vertices_on_the_retry_too_fails_by_name_and_is_put_back(tmp_path, monkeypatch):
    from cairnmind.storage import graph_transactions
    store, path, before, known = _world(tmp_path, monkeypatch)
    state = _hiding_the_index(monkeypatch, 2, at="edges")  # the group, then the read-back of the retry's wait
    monkeypatch.setattr(graph_transactions, "VERTEX_WAIT", 0)
    with pytest.raises(RestoreFailed, match=r"did not find \d+ graph vertices it had committed.*put back as it was before"):
        store.restore(path)
    assert state["hidden"] == 0
    monkeypatch.undo()
    assert comparable(store.export()) == before
    assert restore_staged.unfinished(store.client) is None and not Store(store.data_dir).read_only


def test_a_restore_of_the_usual_shape_commits_once_for_its_vertices_and_once_at_the_end_not_per_group_of_edges(
        tmp_path, monkeypatch):
    """#396: before, every group of 250 edges committed, waited and began again (the second project's, too)."""
    path = _large(Store(tmp_path / "memory"), tmp_path)
    target = Store(tmp_path / "target")
    monkeypatch.setattr(restore_staged, "TIME_PER_TRANSACTION", 10 ** 9)  # a slow machine adds no commit of its own
    made, commit = [], restore_staged.Batches.commit
    monkeypatch.setattr(restore_staged.Batches, "commit",
                        lambda self: (made.append(self._stack is not None), commit(self))[1])
    target.restore(path)
    monkeypatch.undo()
    assert sum(made) == 2, made  # the vertices' commit before the one wait, and the last


def test_a_restore_whose_index_misses_once_with_no_time_left_to_wait_fails_by_name_and_is_put_back(tmp_path, monkeypatch):
    from cairnmind.storage import graph_transactions
    store, path, before, known = _world(tmp_path, monkeypatch)
    state = _hiding_the_index(monkeypatch, 1)  # the first wait finds nothing, and with no time to wait it fails
    monkeypatch.setattr(graph_transactions, "VERTEX_WAIT", 0)
    with pytest.raises(RestoreFailed, match=r"did not find \d+ graph vertices it had committed.*put back as it was before"):
        store.restore(path)
    assert state["hidden"] == 0
    monkeypatch.undo()
    assert comparable(store.export()) == before
    assert restore_staged.unfinished(store.client) is None and not Store(store.data_dir).read_only


def test_a_restore_whose_index_keeps_missing_cannot_put_the_store_back_either_and_keeps_the_mark(tmp_path, monkeypatch):
    from cairnmind.storage import graph_transactions
    store, path, before, known = _world(tmp_path, monkeypatch)
    _hiding_the_index(monkeypatch, 10 ** 9)
    monkeypatch.setattr(graph_transactions, "VERTEX_WAIT", 0)
    with pytest.raises(RestoreFailed) as failed:
        store.restore(path)
    monkeypatch.undo()
    text = " ".join(str(failed.value).split())
    assert "did not find" in text and "putting the store back" in text and "failed too" in text
    assert restore_staged.unfinished(store.client) is not None
    Store(store.data_dir, for_restore=True).restore(path)  # the person restores the backup again, once the index answers
    assert restore_staged.unfinished(store.client) is None


def test_a_transaction_open_for_the_time_bound_is_committed_and_a_slow_delete_halves_the_next(tmp_path, monkeypatch):
    path = _large(Store(tmp_path / "memory"), tmp_path)
    target = Store(tmp_path / "target")
    target.restore(path)  # rows to delete
    commits, _ = _watching(monkeypatch)
    limits, inner = [], arcade.ArcadeClient._request

    def spying(self, method, request_path, **kwargs):
        command = str((kwargs.get("json") or {}).get("command", ""))
        if command.startswith("DELETE FROM") and " LIMIT " in command:
            limits.append(int(command.rsplit(" LIMIT ", 1)[1]))
        return inner(self, method, request_path, **kwargs)

    monkeypatch.setattr(arcade.ArcadeClient, "_request", spying)
    target.restore(path)
    quick, quick_limits = len(commits), set(limits)
    del commits[:], limits[:]
    monkeypatch.setattr(restore_staged, "TIME_PER_TRANSACTION", 0.0)  # every transaction has been open long enough
    target.restore(path)
    monkeypatch.undo()
    # In time, a delete asks for what is left of the 5,000 rows of its transaction; slow ones ask for less and less.
    assert max(quick_limits) == restore_staged.ROWS_PER_TRANSACTION
    assert len(commits) > quick + 5 and max(commits) <= restore_staged.ROWS_PER_TRANSACTION, (quick, commits)
    assert limits[0] == restore_staged.ROWS_PER_TRANSACTION and max(limits[1:]) <= restore_staged.ROWS_PER_TRANSACTION // 2
    assert limits[-1] <= restore_staged.MIN_ROWS_PER_DELETE, limits
    assert comparable(target.export()) == comparable(Store(tmp_path / "memory").export())


def test_the_writer_lock_is_held_between_the_transactions_of_a_restore(tmp_path, monkeypatch):
    store, path, before, known = _world(tmp_path, monkeypatch)
    lock, free = store.data_dir / "database-writer.lock", []
    commit = restore_staged.Batches.commit

    def watching(self):
        commit(self)  # the transaction is over: another command of this installation could commit now, if it could lock
        with lock.open("a") as handle:
            try:
                file_locks.lock_exclusive(handle, blocking=False)
            except BlockingIOError:
                free.append(False)
            else:
                free.append(True)
                file_locks.unlock(handle)

    monkeypatch.setattr(restore_staged.Batches, "commit", watching)
    store.restore(path)
    monkeypatch.undo()
    assert len(free) > 3 and not any(free), free
    with file_locks.locked_path(lock, blocking=False):  # and it is released at the end
        pass
    assert not Store(store.data_dir).read_only


def test_a_marked_store_gives_no_backup_and_no_export_and_a_takeover_does_not_hide_the_mark(tmp_path, monkeypatch):
    first = Store(tmp_path / "first")
    second_dir = second_installation(tmp_path, first)
    mark = _mark(first)
    marked = Store(first.data_dir, for_restore=True)  # the opening a restore uses: it may write, so it must not copy
    for call in (lambda: marked.backup(tmp_path / "half.cairnmind.json.gz"), marked.export):
        with pytest.raises(RestoreUnfinished, match="did not finish"):
            call()
    assert not (tmp_path / "half.cairnmind.json.gz").exists()
    # Every other command opens the store through Store or Store.open_readonly: `store size` is one of them.
    shown = runner.invoke(cli.app, ["--data-dir", str(first.data_dir), "store", "size"])
    # (the entry point prints a ValueError's message and exits 1; the test runner hands it over as the exception)
    assert shown.exit_code != 0 and isinstance(shown.exception, RestoreUnfinished), (shown.output, shown.exception)
    # The other installation sees the sentence too, with the way out that needs nothing of the first one's machine.
    with pytest.raises(StoreNotReady) as refused:
        Store(second_dir)
    assert "store takeover --yes" in str(refused.value) and "any valid backup" in str(refused.value)
    # Taking the writer role is a person's act on a marked store, and it leaves the mark where it is.
    taken = store_writer.take_over_at(second_dir)
    assert taken["taken_over"] is True and "did not finish" in taken["next"]
    assert "cannot prepare" not in taken["next"] and "refused until a restore finishes" in taken["next"]
    assert restore_staged.unfinished(first.client) == mark
    for opening in (lambda: Store(second_dir), lambda: Store(first.data_dir)):
        with pytest.raises(StoreNotReady, match="did not finish"):
            opening()
    # A restore of any backup from that installation clears it: the first installation's copy is not needed.
    other = tmp_path / "other.cairnmind.json.gz"
    Store(tmp_path / "elsewhere").backup(other)
    Store(second_dir, for_restore=True).restore(other)
    assert restore_staged.unfinished(first.client) is None and not Store(second_dir).read_only
    assert Store(first.data_dir).read_only  # the first installation is now a reader of a consistent store
    assert b"restore_unfinished" not in gzip.decompress(Store(second_dir).backup(tmp_path / "whole.cairnmind.json.gz").read_bytes())


@pytest.mark.parametrize("agent", ["disabled", "enabled", ""])
def test_the_undo_says_what_becomes_of_the_vectors_as_the_setting_does(embedded, monkeypatch, agent):  # noqa: F811
    store = embedded.store
    monkeypatch.setattr(store.settings, "knowledge_agent", agent)
    monkeypatch.setattr(restore_staged, "ROWS_PER_TRANSACTION", 1)
    _failing_commits(monkeypatch, 2)
    with pytest.raises(RestoreFailed, match="put back as it was") as failed:
        store.restore(embedded.backup)
    expected = {"disabled": VECTORS_WAIT, "enabled": VECTORS_REBUILT, "": VECTORS_UNDECIDED}[agent]
    assert str(failed.value).endswith(expected.format(jobs=embedded.jobs))


KILLED_RESTORE = """
import sys, time
from pathlib import Path
from cairnmind.storage import restore_staged
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
            if sys.platform == "win32":
                from cairnmind.pal import process_control
                process_control.terminate_pid(child.pid, force=True)
            else:
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


def test_a_restore_finished_from_another_installation_keeps_where_the_copy_is(tmp_path, monkeypatch):
    store, path, before, known = _world(tmp_path, monkeypatch)
    _mark(store, before="/gone/before-restore-x.cairnmind.json.gz", installation_id="first-installation",
          before_installation="first-installation")
    _failing_commits(monkeypatch, 3)
    with pytest.raises(RestoreFailed, match="is not on this machine"):  # the copy is the first installation's
        Store(store.data_dir, for_restore=True).restore(path)
    monkeypatch.undo()
    mark = restore_staged.unfinished(store.client)
    assert mark["installation_id"] == store.installation.id and mark["before_installation"] == "first-installation"
    with pytest.raises(RestoreUnfinished, match="backups of installation first-installation"):
        Store(store.data_dir)


def test_a_mark_that_cannot_be_removed_after_every_row_was_written_says_so(tmp_path, monkeypatch):
    store, path, before, known = _world(tmp_path, monkeypatch)

    def gone(target):
        raise OSError("the engine is gone")

    monkeypatch.setattr(restore_staged, "clear_mark", gone)
    with pytest.raises(RestoreFailed) as failed:
        store.restore(path)
    monkeypatch.undo()
    text = " ".join(str(failed.value).split())
    assert "Every row of the backup was written" in text and "the engine is gone" in text
    assert f"cairnmind restore {path}" in text
    with pytest.raises(RestoreUnfinished):
        Store(store.data_dir)
    Store(store.data_dir, for_restore=True).restore(path)
    assert restore_staged.unfinished(store.client) is None


def test_a_transaction_of_the_write_phase_is_committed_when_it_has_been_open_for_the_time_bound(tmp_path, monkeypatch):
    store, commits = Store(tmp_path / "memory"), []
    commit = restore_staged.Batches.commit
    monkeypatch.setattr(restore_staged.Batches, "commit", lambda self: (commits.append(self.rows), commit(self))[1])
    monkeypatch.setattr(restore_staged, "TIME_PER_TRANSACTION", 0.0)
    batches = restore_staged.Batches(store)
    for _ in range(3):
        batches.wrote(1)  # one row of a bound of 5,000: only the time commits it
    batches.commit()
    assert commits == [1, 1, 1, 0]
    monkeypatch.setattr(restore_staged, "TIME_PER_TRANSACTION", 3600.0)
    del commits[:]
    batches = restore_staged.Batches(store)
    batches.wrote(1)
    batches.commit()
    assert commits == [1]


def test_setup_of_a_marked_store_is_refused_with_the_whole_sentence_and_the_pasted_commands_name_the_data_dir(
        tmp_path, monkeypatch):
    store = Store(tmp_path / "memory")
    _mark(store)
    found = store_writer.inspect(store.settings)
    assert found["restore_unfinished"] and found["reason"].endswith("removes the mark.")  # not cut to 300 characters
    from cairnmind.terminal import setup_profiles
    monkeypatch.setattr(store_writer, "inspect", lambda settings: found)
    with pytest.raises(setup_profiles.SetupRefused):
        setup_profiles._inspected(store.settings, "http://db")
    monkeypatch.setitem(cli._INVOCATION, "data_dir", tmp_path / "memory")
    pasted = cli._pasteable(found["reason"])
    assert f"`cairnmind --data-dir {tmp_path / 'memory'} restore " in pasted and "store takeover --yes" in pasted
    assert f"--data-dir {tmp_path / 'memory'} store takeover" in pasted
    assert len(cli.last_log_line.__doc__) and "?" not in pasted[:0]