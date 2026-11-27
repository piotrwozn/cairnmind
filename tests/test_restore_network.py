"""A restore over a network that drops or is slow, from one installation or another (#362).

The trial between two machines: a restore of a 26 MB backup ended with the client's read timeout, with the server
working and no error in its log. Now a restore is many short transactions, so no request is long; and a restore that
the network cuts in the middle leaves a mark that every installation of the store sees, which any installation can
finish by restoring a valid backup once it is the writer.
"""
from contextlib import closing
import shutil

import pytest

from cairnmind.storage import arcade, store_writer
from cairnmind.scip import ingest
from cairnmind.storage.arcade import ArcadeError
from cairnmind.config import Settings
from cairnmind.storage.restore_staged import RestoreFailed
from cairnmind.store import Store
from cairnmind.storage.store_readonly import StoreNotReady
from test_connect_only import data_directory, test_engine  # noqa: F401 - a fixture
from test_connect_only_network import MARK_ROW, contents, direct, relay, whole_transactions_only  # noqa: F401
from test_index_batching import tree
from test_restore_groups import _large
from test_store_writer import second_installation


@pytest.mark.parametrize("finish", ["the same installation restores again", "the other one restores another backup"])
def test_a_restore_cut_by_the_network_leaves_a_mark_that_both_installations_see(tmp_path, relay, finish):  # noqa: F811
    root = tmp_path / "repo"
    tree(root, files=6)
    data = data_directory(tmp_path)
    store = Store(data)
    pid = store.add_project("p", root)["id"]
    ingest.index_project(store, pid)
    backup = store.backup(tmp_path / "saved.cairnmind.json.gz")
    with closing(direct(relay, store)) as client:
        saved, saved_files = contents(client), len(whole_transactions_only(client, pid))
    (root / "extra.py").write_text("def extra():\n    return 1\n")
    ingest.index_project(store, pid)
    later_backup = store.backup(tmp_path / "later.cairnmind.json.gz")
    with closing(direct(relay, store)) as client:
        later = contents(client)
    assert later != saved

    # The network goes in the middle of the writes. The undo needs the connection that just failed, so it fails too.
    relay.cut_at(3, matching=b"INSERT INTO")
    with pytest.raises((ArcadeError, RestoreFailed), match="is unavailable"):
        store.restore(backup)
    relay.heal()
    second = second_installation(tmp_path, store)
    with closing(direct(relay, store)) as client:
        assert client.query(MARK_ROW)  # in the store, where every installation of it reads it
    # Nobody uses the store: readers and writers of both installations are refused, with the sentence.
    for opening in (lambda: Store(data), lambda: Store.open_readonly(store.settings), lambda: Store(second),
                    lambda: Store.open_readonly(Settings.load(second))):
        with pytest.raises(StoreNotReady, match="did not finish.*store takeover --yes.*any valid backup"):
            opening()

    if finish.startswith("the same"):
        Store(data, for_restore=True).restore(backup)
        expected = saved
    else:
        # Its machine is gone for good: no safety copy of it is needed, and the other installation takes the writer role
        # (a person's act that a marked store allows) and restores a backup of its own.
        shutil.rmtree(data / "backups")
        assert store_writer.take_over_at(second)["taken_over"] is True
        with closing(direct(relay, store)) as client:
            assert client.query(MARK_ROW)  # the takeover left the mark where it was
        Store(second, for_restore=True).restore(later_backup)
        expected = later
    with closing(direct(relay, store)) as client:
        assert contents(client) == expected and not client.query(MARK_ROW)
        assert len(whole_transactions_only(client, pid)) == saved_files + (0 if expected is saved else 1)
    Store(second if expected is later else data)  # the one that restored writes again
    Store.open_readonly(store.settings).client.close()  # and the other reads


def test_no_request_of_a_restore_waits_long_and_a_commit_is_waited_for_on_purpose(tmp_path, relay, monkeypatch):  # noqa: F811
    store = Store(data_directory(tmp_path))
    path = _large(store, tmp_path)
    relay.delay = 0.02  # the trial's network: 18.5 ms
    waits, request = [], arcade.ArcadeClient._request

    def noting(self, method, request_path, **kwargs):
        waits.append((request_path, kwargs.get("timeout", arcade.REQUEST_TIMEOUT)))
        return request(self, method, request_path, **kwargs)

    monkeypatch.setattr(arcade.ArcadeClient, "_request", noting)
    before = len(relay.durations)
    store.restore(path)
    monkeypatch.undo()
    measured = relay.durations[before:]
    longest = max(measured, key=lambda item: item[1])
    print(f"\n{len(measured)} requests over a 20 ms relay; the longest took {longest[1]:.2f} s: {longest[0][:100]!r}")
    assert longest[1] < arcade.REQUEST_TIMEOUT / 2, longest  # the client gives up after 30 s on one request
    commits = [timeout for request_path, timeout in waits if request_path.startswith("/api/v1/commit/")]
    # The mark, the deletes, the staged writes and the removal of the mark wait 120 s. The commits at either end are
    # not the restore's own: the read of the safety copy before it and the rule step of the person's confirmations after.
    assert len(commits) >= 6 and set(commits[1:-1]) == {arcade.RESTORE_COMMIT_TIMEOUT}
    assert commits[0] == commits[-1] == arcade.REQUEST_TIMEOUT
    assert {timeout for request_path, timeout in waits if not request_path.startswith("/api/v1/commit/")} == {
        arcade.REQUEST_TIMEOUT}
    waits.clear()
    monkeypatch.setattr(arcade.ArcadeClient, "_request", noting)
    with store.transaction() as tx:  # an ordinary write: its commit keeps the default
        tx.command("INSERT INTO devbrain_meta SET name='ordinary_write'")
    monkeypatch.undo()
    assert [timeout for request_path, timeout in waits if request_path.startswith("/api/v1/commit/")] == [
        arcade.REQUEST_TIMEOUT]
