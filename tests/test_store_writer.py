"""One declared writer per store (#70): the installation that writes a database, and everyone else reading it."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import threading

import pytest
from typer.testing import CliRunner

from cairnmind import store_writer
from cairnmind.arcade import ArcadeClient, ArcadeError
from cairnmind.cli import app
from cairnmind.store import Store
from cairnmind.store_writer import StoreNotWriter, StoreWriterLost, take_over

runner = CliRunner()


def second_installation(tmp_path: Path, first: Store, name: str = "second") -> Path:
    """A data directory of another installation whose configuration names the first one's database."""
    data_dir = tmp_path / name
    data_dir.mkdir(mode=0o700)
    (data_dir / "config.json").write_text(json.dumps({"arcade_database": first.settings.arcade_database}))
    return data_dir


def counts(store: Store) -> dict:
    return {table: store.query(f"SELECT count(*) AS n FROM {table}")[0]["n"]
            for table in ("projects", "sources", "chunks", "nodes", "records")}


def project(tmp_path: Path, store: Store) -> str:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("def handler():\n    return 1\n")
    return store.add_project("demo", root)["id"]


def writer_row(store: Store) -> dict:
    return store_writer.read_writer(ArcadeClient(store.settings))


def head(store: Store) -> dict:
    return ArcadeClient(store.settings).query("SELECT FROM journal_state WHERE id='current'")[0]


def test_a_second_installation_on_the_same_database_cannot_write(tmp_path):
    first = Store(tmp_path / "first")
    pid = project(tmp_path, first)
    from cairnmind.ingest import index_project
    index_project(first, pid)
    before = counts(first)
    second = Store(second_installation(tmp_path, first))
    # The second installation reads the store as it is, and says why it does not write it.
    assert second.read_only and "only reads this store" in second.not_writer
    assert first.installation.id[:8] in second.not_writer and "cairnmind store takeover --yes" in second.not_writer
    assert [row["name"] for row in second.list_projects()] == ["demo"]
    with pytest.raises(StoreNotWriter, match="writes it"):
        second.add_project("other", tmp_path)
    # Two installations indexing one project: the second is refused, naming the writer; nothing is duplicated.
    (tmp_path / "repo" / "app.py").write_text("def handler():\n    return 2\n")
    with pytest.raises(StoreNotWriter, match=first.installation.id[:8]):
        index_project(second, pid)
    with pytest.raises(StoreNotWriter):
        second.command("CREATE DOCUMENT TYPE vector_never IF NOT EXISTS")
    assert counts(first) == before
    index_project(first, pid)
    current = first.query("SELECT uri FROM sources WHERE project_id=:project AND is_current=true", {"project": pid})
    assert len(current) == len({row["uri"] for row in current})
    assert second.writer_state()["role"] == "reader" and first.writer_state()["role"] == "writer"


def test_the_first_installation_becomes_the_writer_and_reopening_changes_nothing(tmp_path):
    first = Store(tmp_path / "first")
    identity = (first.data_dir / "installation.json").read_bytes()
    row = writer_row(first)
    assert row["installation_id"] == first.installation.id == head(first)["writer"]
    assert [entry["how"] for entry in row["history"]] == ["first"]
    again = Store(first.data_dir)
    assert not again.read_only and again.installation == first.installation
    assert (first.data_dir / "installation.json").read_bytes() == identity
    assert writer_row(again) == row
    assert (first.data_dir / "installation.json").stat().st_mode & 0o777 == 0o600


def test_a_store_without_a_writer_row_is_claimed_by_the_first_installation_that_opens_it(tmp_path):
    """A store written by a release before #70 has no writer: whoever opens it first writes it, as before."""
    first = Store(tmp_path / "first")
    client = ArcadeClient(first.settings)
    client.command("DELETE FROM devbrain_meta WHERE name='writer'")
    client.command("UPDATE journal_state REMOVE writer WHERE id='current'")
    second = Store(second_installation(tmp_path, first))
    assert not second.read_only and writer_row(second)["installation_id"] == second.installation.id
    assert head(second)["writer"] == second.installation.id
    assert Store(first.data_dir).read_only


def test_two_installations_opening_a_store_without_a_writer_at_once_get_one_writer(tmp_path):
    first = Store(tmp_path / "first")
    client = ArcadeClient(first.settings)
    directories = [second_installation(tmp_path, first, name) for name in ("one", "two")]
    for round_ in range(12):
        client.command("DELETE FROM devbrain_meta WHERE name='writer'")
        client.command("UPDATE journal_state REMOVE writer WHERE id='current'")
        opened, errors = {}, []

        def open_store(directory):
            try:
                opened[directory.name] = Store(directory)
            except Exception as exc:  # noqa: BLE001 - reported below with the round
                errors.append(exc)

        threads = [threading.Thread(target=open_store, args=(directory,)) for directory in directories]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert not errors, (round_, errors)
        assert sorted(store.read_only for store in opened.values()) == [False, True], round_
        writer = next(store for store in opened.values() if not store.read_only)
        assert writer_row(first)["installation_id"] == writer.installation.id == head(first)["writer"]


def test_a_takeover_moves_writing_and_the_previous_writer_stops_at_its_next_write(tmp_path):
    first = Store(tmp_path / "first")
    pid = project(tmp_path, first)
    second = Store(second_installation(tmp_path, first))
    result = take_over(second, "first laptop retired")
    assert result["taken_over"] is True and result["previous"] == first.installation.id
    row = writer_row(first)
    assert row["installation_id"] == second.installation.id == head(first)["writer"]
    assert row["history"][-1] == {"installation_id": second.installation.id, "previous": first.installation.id,
                                  "host": row["host"], "at": row["since"], "how": "takeover",
                                  "note": "first laptop retired"}
    before = counts(first)
    # The previous writer's store was opened as the writer: its next write is refused, and so is every later one.
    with pytest.raises(StoreWriterLost, match=second.installation.id[:8]):
        first.snapshot(pid, "note://late", "Written after the takeover.", "note")
    with pytest.raises(StoreWriterLost):
        first.add_project("other", tmp_path)
    assert counts(first) == before
    assert first.writer_state()["role"] == "lost"
    # Reopened, both installations know their roles.
    assert Store(first.data_dir).read_only
    writer = Store(second.data_dir)
    assert not writer.read_only
    writer.snapshot(pid, "note://new-writer", "Written by the new writer.", "note")
    assert take_over(writer)["taken_over"] is False


def test_taking_writing_back_lets_the_store_that_lost_it_write_again_without_reopening(tmp_path):
    """A running service keeps its Store: after a person takes writing back there, it writes at its next write."""
    first = Store(tmp_path / "first")
    pid = project(tmp_path, first)
    second_dir = second_installation(tmp_path, first)
    take_over(Store(second_dir))
    with pytest.raises(StoreWriterLost):
        first.add_project("other", tmp_path)
    assert first.writer_state()["role"] == "lost"
    assert take_over(Store(first.data_dir), "back to the first laptop")["taken_over"] is True
    # The same object, never reopened: the role reads as the writer and every kind of write goes through again.
    assert first.writer_state()["role"] == "writer" and "next" not in first.writer_state()
    first.snapshot(pid, "note://taken-back", "Written after taking writing back.", "note")
    first.command("CREATE DOCUMENT TYPE writer_probe_70 IF NOT EXISTS")  # a write outside the journal
    assert writer_row(first)["installation_id"] == first.installation.id == head(first)["writer"]
    assert Store(second_dir).read_only


def test_a_reader_takes_over_a_store_that_this_release_cannot_read_as_a_reader(tmp_path, monkeypatch):
    from cairnmind.store_readonly import StoreNotReady
    for name in ("CAIRNMIND_SESSION_ID", "DEVBRAIN_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    first = Store(tmp_path / "first")
    pid = project(tmp_path, first)
    first.enqueue("extract", pid, {"source_id": "s"}, "job-70-ready")
    assert first.claim_job("inference", 60, ["extract"])
    second_dir = second_installation(tmp_path, first)
    Store(second_dir)
    # The writer's machine is gone and its store has no journal head: a reader cannot open it, and is told why.
    ArcadeClient(first.settings).command("DELETE FROM journal_state WHERE id='current'")
    with pytest.raises(StoreNotReady, match="cairnmind store takeover --yes"):
        Store(second_dir)
    done = runner.invoke(app, ["--data-dir", str(second_dir), "store", "takeover", "--yes"])
    assert done.exit_code == 0, done.output
    result = json.loads(done.output)
    assert result["taken_over"] is True and result["released_jobs"] == 1 and "next" not in result
    writer = Store(second_dir)
    assert not writer.read_only and writer_row(first)["installation_id"] == writer.installation.id
    assert head(first)["writer"] == writer.installation.id
    assert writer.query("SELECT state FROM jobs WHERE dedupe_key='job-70-ready'")[0]["state"] == "pending"
    # A store of a newer release: the role still moves, and the result says this release cannot prepare it.
    ArcadeClient(first.settings).command("UPDATE devbrain_meta SET value=99 WHERE name='schema_version'")
    moved = runner.invoke(app, ["--data-dir", str(first.data_dir), "store", "takeover", "--yes"])
    assert moved.exit_code == 0, moved.output
    result = json.loads(moved.output)
    assert result["taken_over"] is True and "cannot prepare it" in result["next"]
    assert writer_row(first)["installation_id"] == first.installation.id


def test_two_takeovers_at_once_leave_one_writer_that_the_row_and_the_head_agree_on(tmp_path):
    """The orders are chosen one by one in test_store_writer_race; here the threads race as they will.

    From the second round on one of the two is the writer already, which is the case that once left the
    row naming one installation and the head the other.
    """
    first = Store(tmp_path / "first")
    readers = [Store(second_installation(tmp_path, first, name)) for name in ("one", "two")]
    for round_ in range(6):
        results, errors, start = [], [], threading.Barrier(2)

        def take(store):
            try:
                start.wait(30)
                results.append((store.installation.id, take_over(store, f"round {round_}")))
            except Exception as exc:  # noqa: BLE001 - every outcome is checked below
                errors.append(exc)

        threads = [threading.Thread(target=take, args=(store,)) for store in readers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert not any(thread.is_alive() for thread in threads), round_  # nothing is read while a takeover runs
        # A takeover either returned or was refused whole by the engine because the other one committed first.
        assert len(results) + len(errors) == 2 and results, (round_, errors)
        assert all(isinstance(exc, ArcadeError) and "ConcurrentModification" in exc.exception_name
                   for exc in errors), (round_, errors)
        row, named = store_writer.read_fence(ArcadeClient(first.settings))  # both threads have ended
        assert row["installation_id"] == named == row["history"][-1]["installation_id"], round_
        # The writer is an installation whose takeover returned: one that took over in this round, or the one
        # that was the writer already and whose takeover therefore changed nothing.
        assert row["installation_id"] in {installation for installation, _ in results}, round_
        assert any(result["taken_over"] for _, result in results) or round_, results


def test_a_write_in_flight_during_a_takeover_does_not_commit(tmp_path):
    """The journal head is the fence: a transaction that read it before the takeover fails on commit."""
    first = Store(tmp_path / "first")
    pid = project(tmp_path, first)
    second = Store(second_installation(tmp_path, first))
    with pytest.raises((StoreWriterLost, ArcadeError)) as refused:
        with first.transaction() as tx:
            tx.query("SELECT FROM journal_state WHERE id='current'")
            take_over(second)
            tx.command("UPDATE projects SET name='renamed' WHERE id=:project", {"project": pid})
    # The head was read before the takeover, so the check passed on the old value: the engine refused the commit
    # (on 26.11.1: "Slot rebase not possible ... concurrent change to the same record").
    assert isinstance(refused.value, ArcadeError)
    assert [row["name"] for row in Store(second.data_dir).list_projects()] == ["demo"]


def test_only_the_writer_claims_jobs_and_a_takeover_returns_running_ones_to_the_queue(tmp_path):
    first = Store(tmp_path / "first")
    pid = project(tmp_path, first)
    first.enqueue("extract", pid, {"source_id": "s"}, "job-70")
    job = first.claim_job("inference", 60, ["extract"])
    assert job and job["state"] == "running"
    second = Store(second_installation(tmp_path, first))
    with pytest.raises(StoreNotWriter):
        second.claim_job("inference", 60, ["extract"])
    assert take_over(second)["released_jobs"] == 1
    stored = ArcadeClient(first.settings).query("SELECT FROM jobs WHERE id=:id", {"id": job["id"]})[0]
    assert stored["state"] == "pending" and stored["worker"] is None
    assert stored["attempt_history"][-1]["event"] == "writer_takeover"
    # The previous writer's worker can neither store its result (its lease is gone) nor claim the job again.
    with pytest.raises(ValueError, match="stale job lease"):
        first.finish_job(job["id"], worker=job["worker"], expected_attempts=job["attempts"])
    with pytest.raises(StoreWriterLost):
        first.claim_job("inference", 60, ["extract"])
    with pytest.raises(StoreWriterLost):
        first.set_job_diagnostics(job["id"], {"stage": "late"})
    again = Store(second.data_dir).claim_job("inference", 60, ["extract"])
    assert again["id"] == job["id"] and again["attempts"] == 2


def test_a_copied_data_directory_gets_its_own_identity_and_reads_only(tmp_path):
    first = Store(tmp_path / "first")
    project(tmp_path, first)
    copy = tmp_path / "copy"
    shutil.copytree(first.data_dir, copy)
    copied = Store(copy)
    assert copied.installation.id != first.installation.id
    assert copied.installation.copied_from == first.installation.id
    assert copied.read_only and "is a copy of that installation's" in copied.not_writer
    with pytest.raises(StoreNotWriter):
        copied.add_project("other", tmp_path)
    assert not Store(first.data_dir).read_only


def test_the_same_data_directory_seen_from_another_machine_is_a_copy(tmp_path, monkeypatch):
    first = Store(tmp_path / "first")
    original = first.installation
    monkeypatch.setattr(store_writer, "machine_identity", lambda: "machine-id:another-machine")
    moved = Store(first.data_dir)
    assert moved.installation.id != original.id and moved.installation.copied_from == original.id
    assert moved.read_only and "is a copy" in moved.not_writer
    # A person who means the copy to be the writer now says so.
    assert take_over(moved, "moved to the new machine")["taken_over"] is True
    assert not Store(first.data_dir).read_only


def test_restore_and_a_journal_reset_keep_the_writer(tmp_path):
    first = Store(tmp_path / "first")
    pid = project(tmp_path, first)
    second = Store(second_installation(tmp_path, first))
    backup = first.backup(tmp_path / "backup.cairnmind.json.gz")
    with pytest.raises(StoreNotWriter):
        second.restore(backup)
    first.restore(backup)
    assert head(first)["writer"] == first.installation.id
    first.forget_project(pid)  # resets the journal: the new head still names the writer
    assert head(first)["writer"] == first.installation.id
    project_root = tmp_path / "again"
    project_root.mkdir()
    first.add_project("again", project_root)
    assert Store(second.data_dir).read_only


def test_the_command_line_says_who_writes_and_takes_over_only_on_request(tmp_path, monkeypatch):
    for name in ("CAIRNMIND_SESSION_ID", "DEVBRAIN_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    first = Store(tmp_path / "first")
    second_dir = second_installation(tmp_path, first)
    Store(second_dir)
    shown = runner.invoke(app, ["--data-dir", str(second_dir), "store", "writer"])
    assert shown.exit_code == 0, shown.output
    role = json.loads(shown.output)
    assert role["role"] == "reader" and role["writer"]["installation"] == first.installation.id
    assert role["next"] == "cairnmind store takeover --yes"
    asked = runner.invoke(app, ["--data-dir", str(second_dir), "store", "takeover"])
    assert asked.exit_code == 1 and json.loads(asked.output)["taken_over"] is False
    assert writer_row(first)["installation_id"] == first.installation.id
    monkeypatch.setenv("CAIRNMIND_SESSION_ID", "session-of-an-agent")
    refused = runner.invoke(app, ["--data-dir", str(second_dir), "store", "takeover", "--yes"])
    assert refused.exit_code == 2 and "only a person can" in refused.output
    monkeypatch.delenv("CAIRNMIND_SESSION_ID")
    bad = runner.invoke(app, ["--data-dir", str(second_dir), "store", "takeover", "--yes", "--note", "two\nlines"])
    assert bad.exit_code == 2
    done = runner.invoke(app, ["--data-dir", str(second_dir), "store", "takeover", "--yes", "--note", "handover"])
    assert done.exit_code == 0, done.output
    assert json.loads(done.output)["taken_over"] is True
    shown = json.loads(runner.invoke(app, ["--data-dir", str(first.data_dir), "store", "writer"]).output)
    assert shown["role"] == "reader" and shown["writer"]["history"][-1]["how"] == "takeover"


def test_the_service_runs_only_for_the_writer(tmp_path):
    from cairnmind.service import create_app
    first = Store(tmp_path / "first")
    second = Store(second_installation(tmp_path, first))
    with pytest.raises(StoreNotWriter, match="only reads this store"):
        create_app(second.settings, start_worker=False)
    from fastapi.testclient import TestClient
    with TestClient(create_app(first.settings, start_worker=False)) as client:
        status = client.get("/status", headers={"Authorization": f"Bearer {first.settings.token}"}).json()
    assert status["writer"]["role"] == "writer" and status["writer"]["installation"] == first.installation.id


def test_a_host_label_from_the_database_is_printed_safely():
    assert store_writer.label("laptop\x1b[31m\nfake") == "laptop??31m?fake"
    assert store_writer.label(None) == "unknown"
    assert len(store_writer.label("x" * 500)) == 64
