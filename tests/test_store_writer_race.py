"""One writer per store (#70) when two installations act at the same moment, in chosen orders.

A claim or a takeover is one transaction: it reads the journal head, reads the writer row, writes the row
and names itself in the head. The tests stop one installation at a point inside that transaction, let the
other one finish, and let the first go on. Whatever the order, the row and the head name one installation.
"""
from __future__ import annotations

import json
import threading

import pytest
from typer.testing import CliRunner

from cairnmind import store_writer
from cairnmind.arcade import ArcadeClient, ArcadeError
from cairnmind.cli import app
from cairnmind.store import Store
from cairnmind.store_writer import StoreNotWriter, StoreWriterLost, take_over

from test_store_writer import head, project, second_installation, writer_row

runner = CliRunner()
CONFLICT = "ConcurrentModificationException"


class Paused:
    """Run ``work`` in a thread that stops once at ``point`` of its claim or takeover transaction.

    ``after_head``: the head is read, the writer row is not yet. ``before_stamp``: both are read and the
    row is written (when there is one to write), the head is not yet named.
    """

    def __init__(self, monkeypatch, point: str, work):
        self.reached, self.go, self.result, self.error = threading.Event(), threading.Event(), None, None
        self.paused = False
        name = {"after_head": "read_writer", "before_stamp": "stamp"}[point]
        original = getattr(store_writer, name)

        def stopping(client, *args):
            if threading.current_thread() is self.thread and client.session_id and not self.reached.is_set():
                self.paused = True
                self.reached.set()
                assert self.go.wait(60), "the paused transaction was never released"
            return original(client, *args)

        def run():
            try:
                self.result = work()
            except Exception as exc:  # noqa: BLE001 - looked at by the test
                self.error = exc
            finally:
                self.reached.set()  # so that a transaction that ended before the point fails the test at once

        monkeypatch.setattr(store_writer, name, stopping)
        self.thread = threading.Thread(target=run)
        self.thread.start()
        assert self.reached.wait(60), "the transaction did not reach the point"
        # Without this a transaction that never gets to the point would turn the test into a sequential run.
        assert self.paused and self.thread.is_alive(), (
            f"the transaction ended without stopping at {point}", self.result, self.error)

    def resume(self):
        self.go.set()
        self.thread.join(60)
        assert not self.thread.is_alive(), "the paused transaction did not end"
        return self


def agreed_writer(store: Store) -> str:
    """The one installation that the writer row, its last change and the journal head all name."""
    reader = ArcadeClient(store.settings)  # nothing else runs when a test calls this
    rows = reader.query("SELECT FROM devbrain_meta WHERE name='writer'")
    named = reader.query("SELECT writer FROM journal_state WHERE id='current'")[0]["writer"]
    assert len(rows) == 1, [row.get("installation_id") for row in rows]
    assert rows[0]["installation_id"] == named == rows[0]["history"][-1]["installation_id"]
    return named


def make_mixed(store: Store, row: Store, in_head: Store) -> None:
    """The state a takeover race of a build before the fix left: the row names one, the head the other."""
    client = ArcadeClient(store.settings)
    client.command("UPDATE devbrain_meta SET installation_id=:id WHERE name='writer'", {"id": row.installation.id})
    client.command("UPDATE journal_state SET writer=:id WHERE id='current'", {"id": in_head.installation.id})


# No ("before_stamp", "paused"): a takeover by the installation that is the writer already returns before the
# stamp, so nothing can be stopped there (the same installation stopped after the head read is the CI order).
@pytest.mark.parametrize("point,writer_before", [
    ("after_head", "first"), ("after_head", "paused"), ("after_head", "other"),
    ("before_stamp", "first"), ("before_stamp", "other")])
def test_a_takeover_that_overlaps_another_never_leaves_the_row_and_the_head_apart(
        tmp_path, monkeypatch, point, writer_before):
    first = Store(tmp_path / "first")
    paused, other = (Store(second_installation(tmp_path, first, name)) for name in ("one", "two"))
    if writer_before != "first":
        take_over({"paused": paused, "other": other}[writer_before])
    stopped = Paused(monkeypatch, point, lambda: take_over(paused, "paused"))
    took = take_over(other, "other")
    stopped.resume()
    if stopped.error is not None:
        # The engine refused the second commit whole; the person runs the command again.
        assert isinstance(stopped.error, ArcadeError) and CONFLICT in stopped.error.exception_name, stopped.error
        assert took["taken_over"] is True and agreed_writer(first) == other.installation.id
        assert take_over(paused)["taken_over"] is True
        assert agreed_writer(first) == paused.installation.id
    else:
        # It committed on top of the other one's takeover, with both records its own.
        assert stopped.result["taken_over"] is True, stopped.result
        assert agreed_writer(first) == paused.installation.id


def test_the_order_that_failed_in_ci_the_writer_takes_over_again_while_another_takes_over(tmp_path, monkeypatch):
    """The writer read the head naming itself, the other took over, the writer then read the row and took
    over "back": its stamp set the value its view of the head already had, which the engine drops."""
    first = Store(tmp_path / "first")
    one, two = (Store(second_installation(tmp_path, first, name)) for name in ("one", "two"))
    take_over(one)
    stopped = Paused(monkeypatch, "after_head", lambda: take_over(one))
    assert take_over(two)["taken_over"] is True
    stopped.resume()
    assert isinstance(stopped.error, ArcadeError) and CONFLICT in stopped.error.exception_name, (
        stopped.error, stopped.result, writer_row(first)["installation_id"], head(first)["writer"])
    assert agreed_writer(first) == two.installation.id
    # The command says what happened instead of showing the engine's error, and works when it is run again.
    stopped = Paused(monkeypatch, "after_head", lambda: store_writer.take_over_at(one.data_dir))
    assert take_over(two, "again")["taken_over"] is False
    assert take_over(Store(first.data_dir))["taken_over"] is True
    stopped.resume()
    assert isinstance(stopped.error, StoreNotWriter) and str(stopped.error) == store_writer.REFUSED_TAKEOVER
    assert "another installation claimed it or took it over" in str(stopped.error)
    assert agreed_writer(first) == first.installation.id
    assert store_writer.take_over_at(one.data_dir)["taken_over"] is True
    assert agreed_writer(first) == one.installation.id


@pytest.mark.parametrize("point", ["after_head", "before_stamp"])
def test_a_takeover_refused_because_the_writer_wrote_meanwhile_says_so_and_changes_nothing(
        tmp_path, monkeypatch, point):
    """Every journaled write of the writer changes the head, so a takeover that overlaps one is refused too.
    Nobody else claimed anything then, and the text must not send the person looking for another installation."""
    first = Store(tmp_path / "first")
    pid = project(tmp_path, first)
    directory = second_installation(tmp_path, first, "two")
    for attempt in range(2):  # a writer that keeps writing refuses it again
        stopped = Paused(monkeypatch, point, lambda: store_writer.take_over_at(directory))
        first.snapshot(pid, f"note://meanwhile/{point}/{attempt}", "Written during the takeover.", "note")
        stopped.resume()
        assert isinstance(stopped.error, StoreNotWriter) and not isinstance(stopped.error, StoreWriterLost)
        assert str(stopped.error) == store_writer.REFUSED_TAKEOVER, stopped.error
        assert "its writer wrote to it" in str(stopped.error) and "more than once" in str(stopped.error)
        assert "Nothing was changed here" in str(stopped.error) and "cairnmind store takeover --yes" in str(stopped.error)
        assert agreed_writer(first) == first.installation.id
        assert len(writer_row(first)["history"]) == 1
    first.snapshot(pid, f"note://after/{point}", "The writer still writes.", "note")
    assert store_writer.take_over_at(directory)["taken_over"] is True
    assert agreed_writer(first) == store_writer.installation(directory).id


def test_a_takeover_between_the_reads_of_the_row_and_the_head_is_not_reported_as_a_broken_store(tmp_path, monkeypatch):
    first = Store(tmp_path / "first")
    two = Store(second_installation(tmp_path, first, "two"))
    original, took = store_writer.head_writer, []

    def after_a_takeover(reader):
        if not took:
            took.append(take_over(two))
        return original(reader)

    monkeypatch.setattr(store_writer, "head_writer", after_a_takeover)
    shown = first.writer_state()
    assert took and took[0]["taken_over"] is True
    assert "problem" not in shown and "head_writer" not in shown, shown
    assert shown["role"] == "reader" and shown["writer"]["installation"] == two.installation.id
    assert shown == first.writer_state()


@pytest.mark.parametrize("point", ["after_head", "before_stamp"])
@pytest.mark.parametrize("head_names", [None, "paused"])
def test_two_installations_claiming_a_store_without_a_writer_in_any_order_get_one_writer_row(
        tmp_path, monkeypatch, point, head_names):
    first = Store(tmp_path / "first")
    client = ArcadeClient(first.settings)
    directories = [second_installation(tmp_path, first, name) for name in ("one", "two")]
    client.command("DELETE FROM devbrain_meta WHERE name='writer'")
    client.command("UPDATE journal_state REMOVE writer WHERE id='current'")
    if head_names:
        # A head that already names the claiming installation, without a row (a row deleted by hand).
        client.command("UPDATE journal_state SET writer=:id WHERE id='current'",
                       {"id": store_writer.installation(directories[0]).id})
    stopped = Paused(monkeypatch, point, lambda: Store(directories[0]))
    other = Store(directories[1])
    stopped.resume()
    assert stopped.error is None, stopped.error
    assert not other.read_only and stopped.result.read_only
    assert agreed_writer(first) == other.installation.id
    assert other.installation.id[:8] in stopped.result.not_writer


def test_a_writer_coming_back_while_the_new_writer_writes_fences_that_write(tmp_path, monkeypatch):
    """The previous writer takes writing back while the new writer has a transaction open: it does not commit."""
    first = Store(tmp_path / "first")
    pid = project(tmp_path, first)
    second = Store(second_installation(tmp_path, first))
    take_over(second)
    second = Store(second.data_dir)
    back = Store(first.data_dir)
    with pytest.raises((StoreWriterLost, ArcadeError)):
        with second.transaction() as tx:
            tx.query("SELECT FROM journal_state WHERE id='current'")
            assert take_over(back)["taken_over"] is True
            tx.command("UPDATE projects SET name='renamed' WHERE id=:project", {"project": pid})
    assert agreed_writer(first) == first.installation.id
    assert [row["name"] for row in Store(first.data_dir).list_projects()] == ["demo"]
    with pytest.raises(StoreWriterLost, match=first.installation.id[:8]):
        second.add_project("other", tmp_path)


def test_a_store_left_with_row_and_head_apart_is_reported_and_the_next_takeover_repairs_it(tmp_path, monkeypatch):
    for name in ("CAIRNMIND_SESSION_ID", "DEVBRAIN_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    first = Store(tmp_path / "first")
    pid = project(tmp_path, first)
    one, two = (Store(second_installation(tmp_path, first, name)) for name in ("one", "two"))
    take_over(one)
    one = Store(one.data_dir)  # a running process of the installation the row will name
    take_over(two)
    two = Store(two.data_dir)  # a running process of the installation the head will name
    make_mixed(first, row=one, in_head=two)
    # What each installation is told: by `store writer`, by `doctor`, and by a store that is open.
    for directory, role in ((one.data_dir, "writer"), (two.data_dir, "reader"), (first.data_dir, "reader")):
        shown = json.loads(runner.invoke(app, ["--data-dir", str(directory), "store", "writer"]).output)
        assert shown["role"] == role and shown["head_writer"] == two.installation.id, shown
        assert one.installation.id[:8] in shown["problem"] and two.installation.id[:8] in shown["problem"]
        assert shown["next"] == "cairnmind store takeover --yes"
        doctor = json.loads(runner.invoke(app, ["--data-dir", str(directory), "doctor"]).output)
        assert doctor["store_writer"]["problem"] == shown["problem"]
    assert "problem" in one.writer_state() and "problem" in two.writer_state()
    # Opening the store on the installation the row names does not move the fence without a person, and runs
    # no schema statement on a store another installation is committing to.
    refusal = "journal head names installation " + two.installation.id[:8]
    with monkeypatch.context() as patch:
        prepared = []
        patch.setattr(Store, "_prepare_schema", lambda self: prepared.append(self))
        opened = Store(one.data_dir)
    assert not prepared and not opened.read_only and head(first)["writer"] == two.installation.id
    assert opened.list_projects()[0]["id"] == pid
    for store in (opened, one):  # a new process and one that was running
        with pytest.raises(StoreWriterLost, match=refusal):
            store.command("CREATE DOCUMENT TYPE writer_race_probe IF NOT EXISTS")
        with pytest.raises(StoreWriterLost, match=refusal):
            store.add_project("refused", tmp_path)
    assert "writer_race_probe" not in {row["name"] for row in first.client.query("SELECT name FROM schema:types")}
    # The head is the fence, so at most one of them commits: the one the head names, from a process that was
    # already running. The one the row names is refused there, and told what is wrong.
    two.snapshot(pid, "note://head", "Written by the installation the head names.", "note")
    with pytest.raises(StoreWriterLost, match="journal head names installation " + two.installation.id[:8]):
        one.snapshot(pid, "note://row", "Refused: the head names another installation.", "note")
    # The next takeover on the installation the row names puts the head right, and says so.
    done = runner.invoke(app, ["--data-dir", str(one.data_dir), "store", "takeover", "--yes"])
    assert done.exit_code == 0, done.output
    result = json.loads(done.output)
    assert result["taken_over"] is True and result["previous"] == two.installation.id and "repaired" in result
    assert agreed_writer(first) == one.installation.id
    assert writer_row(first)["history"][-1]["how"] == "repair"
    assert "problem" not in one.writer_state() and one.writer_state()["role"] == "writer"
    one.snapshot(pid, "note://row", "Written after the repair.", "note")
    with pytest.raises(StoreWriterLost):
        two.snapshot(pid, "note://late", "Refused after the repair.", "note")
    # The other way: a takeover on the installation the head names moves the row to it as well.
    make_mixed(first, row=one, in_head=two)
    assert take_over(Store(two.data_dir, for_takeover=True))["taken_over"] is True
    assert agreed_writer(first) == two.installation.id
    assert "problem" not in json.loads(runner.invoke(app, ["--data-dir", str(first.data_dir), "store", "writer"]).output)
