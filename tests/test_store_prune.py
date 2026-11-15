"""Old job history, safety backups and engine.log are bounded without losing what their readers read (#190)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import subprocess
import time

import pytest
from typer.testing import CliRunner

from cairnmind import engine
from cairnmind.arcade import ArcadeError
from cairnmind.cli import app
from cairnmind.config import Settings
from cairnmind.ingest import import_history
from cairnmind.integrations import _transcript_watermark
from cairnmind.memory import MemoryManager
from cairnmind.memory_egress import REMOTE_NOT_ALLOWED, release_held_extractions
from cairnmind.service import compact_job_history, daily_backups
from cairnmind.sessions import SessionManager
from cairnmind.store import Store
from cairnmind.store_prune import compact_old_jobs, delete_backups, prune_plan, safety_backups
from cairnmind.store_size import measure
from cairnmind.sql import json_text

OMITTED = {"stage": "extract", "code": "completed_with_omissions", "repair_attempted": False,
           "repair_outcome": "not_needed", "validation_codes": ["proposal_rejected"],
           "counts": {"candidate_records": 6, "accepted_records": 0, "written_records": 0, "skipped_records": 6}}
LATER = timedelta(days=31)


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("VALUE = 1\n")
    for args in (("init", "-q"), ("config", "user.name", "Prune fixture"),
                 ("config", "user.email", "prune@example.invalid"), ("add", "."), ("commit", "-qm", "initial")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    store = Store(tmp_path / "memory")
    project = store.add_project("prune", root)
    return store, project, root


def claim(store, kind, jid):
    """Claim the job ``jid``; jobs queued before it (an import queues its own) are run to completion first."""
    while True:
        job = store.claim_job("worker", kinds=[kind])
        if job["id"] == jid:
            return job
        store.finish_job(job["id"], worker="worker", expected_attempts=job["attempts"])


def run_job(store, project, kind, payload, key, *, diagnostics=None, fail_first=False, error=None):
    """A job run through the queue as the workers run it; with ``fail_first`` it failed once and was retried."""
    jid = store.enqueue(kind, project["id"], payload, key)
    if fail_first:
        job = claim(store, kind, jid)
        store.finish_job(jid, "first attempt failed", worker="worker", expected_attempts=job["attempts"])
        store.retry_job(project["id"], jid, job["attempts"])
    job = claim(store, kind, jid)
    store.set_job_diagnostics(jid, diagnostics or {"stage": kind, "code": "ok"}, worker="worker",
                              expected_attempts=job["attempts"])
    store.finish_job(jid, error, worker="worker", expected_attempts=job["attempts"])
    return jid


def captured_import(store, project, root, tmp_path):
    """A completed import job whose coverage is proven by a published resume packet (captured_complete)."""
    history = tmp_path / "transcript.jsonl"
    history.write_text(json_text({"type": "user", "cwd": str(root), "sessionId": "native",
                                  "message": {"role": "user", "content": "Remember this instruction"}}) + "\n")
    manager = SessionManager(store, store.settings)
    initial = manager.prepare(project["id"], "claude", timeout=0)
    capture = _transcript_watermark(history)
    event = {"event_id": "capture-one", "devbrain_session_id": initial["session_id"], "session_id": "native",
             "hook_event_name": "SessionStart", "cwd": str(root), "transcript_path": str(history),
             "transcript_watermark": capture}
    manager.note_event(event, "claude")
    import_history(store, history, project["id"], "claude")
    assert manager.prepare(project["id"], "claude", session_id=initial["session_id"], timeout=0)["status"] == "ready"
    return run_job(store, project, "import", {"path": str(history), "client": "claude",
                                              "origin_event_id": event["event_id"], "requested_watermark": capture},
                   "import-one", fail_first=True)


def readers(store, project, root, jobs):
    """Everything that reads completed jobs, except the attempt history itself."""
    def public(job):
        return {key: value for key, value in job.items() if key not in ("attempt_history", "attempt_history_omitted")}
    pid = project["id"]
    memory = MemoryManager(store)
    return {"status": store.status(), "project_status": store.status(pid), "summary": store.job_summary(pid),
            "list": [public(job) for job in store.list_jobs(pid, limit=100)],
            "show": {jid: public(store.job_status(pid, jid)) for jid in jobs.values()},
            "failed": store.recent_job_errors(pid),
            "packet_counts": SessionManager(store, store.settings)._pending_status(pid, "extract"),
            "snapshot_processing": memory.status(pid, root)["processing"],
            "proofs": store._job_proofs(pid)}


def jobs_bytes(store):
    rows = store.query("SELECT sum(@this.toJSON().length()) AS b FROM jobs WHERE state='completed'")
    return int(rows[0].get("b") or 0)


def test_old_completed_jobs_lose_only_their_history_and_every_reader_reads_the_same(workspace, tmp_path):
    store, project, root = workspace
    pid = project["id"]
    source = store.snapshot(pid, "conversation://source", "Use explicit evidence.", "transcript")
    jobs = {"import": captured_import(store, project, root, tmp_path),
            "extract": run_job(store, project, "extract", {"source_id": source["id"]}, f"extract:{source['id']}",
                               fail_first=True),
            "omitted": run_job(store, project, "extract", {"source_id": source["id"]}, "omitted", diagnostics=OMITTED),
            "held": run_job(store, project, "extract", {"source_id": source["id"]}, "held",
                            diagnostics={"stage": "extract", "code": REMOTE_NOT_ALLOWED}),
            "embed": run_job(store, project, "embed", {"chunk_id": "chunk-one"}, "embed:chunk-one"),
            "failed": run_job(store, project, "extract", {"source_id": source["id"]}, "failed", error="model failed")}
    pending = store.enqueue("embed", pid, {"chunk_id": "chunk-two"}, "embed:chunk-two")
    jobs["pending"] = pending
    assert store.job_status(pid, jobs["import"])["coverage"]["status"] == "captured_complete"
    before, size = readers(store, project, root, jobs), jobs_bytes(store)
    rows = {row["id"]: row for row in store.query("SELECT FROM jobs")}
    assert store.cache_stamp(pid)

    report = compact_old_jobs(store, 30, now=datetime.now(timezone.utc) + LATER)

    completed = [jid for jid, row in rows.items() if row["state"] == "completed"]
    assert set(completed) >= {jid for name, jid in jobs.items() if name not in ("failed", "pending")}
    assert report["compacted"] == len(completed)
    assert report["events"] == sum(len(rows[jid]["attempt_history"]) for jid in completed)
    after = {row["id"]: row for row in store.query("SELECT FROM jobs")}
    for jid in completed:
        old, new = rows[jid], after[jid]
        assert new["attempt_history"] == [] and new["attempt_history_pruned"] == len(old["attempt_history"])
        # Every other field stays: the key, the payload, the diagnostics, the state and the time it last changed.
        assert {k: v for k, v in new.items() if k not in ("attempt_history", "attempt_history_pruned", "compacted_at")} \
            == {k: v for k, v in old.items() if k != "attempt_history"}
    for jid in set(rows) - set(completed):
        assert after[jid] == rows[jid]
    assert {jobs["failed"], pending} <= set(rows) - set(completed)
    assert readers(store, project, root, jobs) == before
    shown = store.job_status(pid, jobs["extract"])
    assert shown["attempt_history"] == [] and shown["attempt_history_count"] == len(rows[jobs["extract"]]["attempt_history"])
    print(f"completed jobs: {size} bytes before, {jobs_bytes(store)} after")
    assert jobs_bytes(store) < size

    # The deduplication key is read by every later enqueue: the same work is not queued again.
    assert store.enqueue("extract", pid, {"source_id": source["id"]}, f"extract:{source['id']}") == jobs["extract"]
    assert store.enqueue("embed", pid, {"chunk_id": "chunk-one"}, "embed:chunk-one") == jobs["embed"]
    assert store.status(pid)["jobs"]["pending"] == 1
    # An extraction whose records were all rejected can still be retried, and held work still be released.
    assert store.retry_job(pid, jobs["omitted"], 1, include_omissions=True)["state"] == "pending"
    assert release_held_extractions(store, store.settings) == 1
    assert store.job_status(pid, jobs["held"])["state"] == "pending"


def test_a_recent_job_is_kept_whole_and_a_job_queued_again_is_compacted_again(workspace):
    store, project, _ = workspace
    pid = project["id"]
    jid = run_job(store, project, "extract", {"source_id": "s"}, "one", diagnostics=OMITTED)
    assert compact_old_jobs(store, 30)["compacted"] == 0  # finished just now
    assert compact_old_jobs(store, 30, now=datetime.now(timezone.utc) + LATER)["compacted"] == 1
    assert compact_old_jobs(store, 30, now=datetime.now(timezone.utc) + LATER)["compacted"] == 0  # once only
    store.retry_job(pid, jid, 1, include_omissions=True)
    job = store.claim_job("worker", kinds=["extract"])
    store.finish_job(jid, worker="worker", expected_attempts=job["attempts"])
    history = store.query("SELECT attempt_history FROM jobs WHERE id=:id", {"id": jid})[0]["attempt_history"]
    assert [event["event"] for event in history] == ["retry_requested", "finished"]
    assert store.job_status(pid, jid)["attempt_history_count"] == 1 + 2
    assert compact_old_jobs(store, 30, now=datetime.now(timezone.utc) + LATER)["compacted"] == 1
    assert store.query("SELECT attempt_history_pruned FROM jobs WHERE id=:id", {"id": jid})[0][
        "attempt_history_pruned"] == 3


@pytest.mark.parametrize("days", [0, -1, 3651, "30", 1.5, True, None])
def test_a_setting_that_is_not_a_number_of_days_compacts_nothing(workspace, days):
    store, project, _ = workspace
    run_job(store, project, "embed", {"chunk_id": "c"}, "c")
    with pytest.raises(ValueError):
        compact_old_jobs(store, days)
    assert compact_job_history(store, Settings(store.data_dir, job_history_days=days)) is None
    assert store.query("SELECT attempt_history FROM jobs")[0]["attempt_history"]


def test_compaction_pages_through_many_jobs_in_short_transactions(workspace):
    store, project, _ = workspace
    for number in range(7):
        run_job(store, project, "embed", {"chunk_id": f"c{number}"}, f"c{number}")
    report = compact_old_jobs(store, 30, now=datetime.now(timezone.utc) + LATER, batch=3)
    assert report["compacted"] == 7 and report["transactions"] == 3
    assert all(row["attempt_history"] == [] for row in store.query("SELECT attempt_history FROM jobs"))


# Safety backups

def backups_dir(tmp_path):
    directory = tmp_path / "data" / "backups"
    directory.mkdir(parents=True)
    now = time.time()
    names = {"before-schema-3-to-4-a.cairnmind.json.gz": 1, "before-schema-4-to-5-b.cairnmind.json.gz": 2,
             "before-schema-5-to-6-c.devbrain.json.gz": 3, "before-restore-a.cairnmind.json.gz": 4,
             "before-restore-b.cairnmind.json.gz": 5, "1700000000000000000.cairnmind.json.gz": 6,
             "mine.cairnmind.json.gz": 7, "before-restore-notes.txt": 8}
    for name, age in names.items():
        (directory / name).write_bytes(b"x" * (100 * age))
        os.utime(directory / name, (now - age * 3600, now - age * 3600))
    (directory / "before-restore-dir.cairnmind.json.gz").mkdir()
    (directory / "before-restore-link.cairnmind.json.gz").symlink_to(directory / "mine.cairnmind.json.gz")
    return directory


def test_safety_backups_are_listed_newest_first_and_a_plan_keeps_the_newest_of_each_kind(tmp_path):
    directory = backups_dir(tmp_path)
    listed = safety_backups(directory)
    assert [entry["name"] for entry in listed] == [
        "before-schema-3-to-4-a.cairnmind.json.gz", "before-schema-4-to-5-b.cairnmind.json.gz",
        "before-schema-5-to-6-c.devbrain.json.gz", "before-restore-a.cairnmind.json.gz",
        "before-restore-b.cairnmind.json.gz"]
    plan = prune_plan(directory, 1)
    assert [entry["name"] for entry in plan["kept"]] == ["before-schema-3-to-4-a.cairnmind.json.gz",
                                                          "before-restore-a.cairnmind.json.gz"]
    assert plan["delete_bytes"] == 200 + 300 + 500
    assert [entry["name"] for entry in prune_plan(directory, 0)["delete"]] == [e["name"] for e in listed]
    with pytest.raises(ValueError):
        prune_plan(directory, -1)
    # A file written again after the plan was made is not the one the plan showed: it stays.
    os.utime(directory / "before-restore-b.cairnmind.json.gz", None)
    result = delete_backups(directory, plan)
    assert result["skipped"] == ["before-restore-b.cairnmind.json.gz"]
    assert sorted(result["deleted"]) == ["before-schema-4-to-5-b.cairnmind.json.gz",
                                         "before-schema-5-to-6-c.devbrain.json.gz"]
    for name in ("1700000000000000000.cairnmind.json.gz", "mine.cairnmind.json.gz", "before-restore-notes.txt",
                 "before-restore-b.cairnmind.json.gz"):
        assert (directory / name).exists()
    assert (directory / "before-restore-dir.cairnmind.json.gz").is_dir()
    assert (directory / "before-restore-link.cairnmind.json.gz").is_symlink()


def test_the_daily_backup_rotation_never_counts_a_safety_backup(tmp_path):
    directory = backups_dir(tmp_path)
    assert [path.name for path in daily_backups(directory)] == ["1700000000000000000.cairnmind.json.gz"]


def test_store_size_lists_each_safety_backup(workspace):
    store, _, _ = workspace
    directory = store.data_dir / "backups"
    directory.mkdir(exist_ok=True)
    (directory / "before-restore-abc.cairnmind.json.gz").write_bytes(b"r" * 300)
    (directory / "before-schema-3-to-4-\x1b[31mabc.cairnmind.json.gz").write_bytes(b"s" * 200)
    report = measure(Store.open_readonly(store.settings))
    safety = report["disk"]["backups"]["safety"]
    assert sorted((entry["kind"], entry["bytes"]) for entry in safety) == [("before_restore", 300),
                                                                           ("before_schema", 200)]
    assert "before-restore-abc.cairnmind.json.gz" in report["text"] and "prune-backups" in report["text"]
    assert "\x1b" not in report["text"]


def invoke(tmp_path, *arguments, **kwargs):
    return CliRunner().invoke(app, ["--data-dir", str(tmp_path / "data"), "store", "prune-backups", *arguments],
                              **kwargs)


def test_only_a_person_prunes_safety_backups(tmp_path, monkeypatch):
    directory = backups_dir(tmp_path)
    before = sorted(path.name for path in directory.iterdir())
    for name in ("cairnmind.engine.start_engine", "cairnmind.engine.ensure_engine", "cairnmind.cli.ensure_service"):
        monkeypatch.setattr(name, lambda *a, **k: pytest.fail("pruning backups started the engine or the service"))
    result = invoke(tmp_path, "--keep", "1", "--yes")
    assert result.exit_code == 2 and "Only a person" in result.output
    assert sorted(path.name for path in directory.iterdir()) == before


def test_a_person_is_asked_before_safety_backups_are_deleted(tmp_path, person_terminal):
    directory = backups_dir(tmp_path)
    before = sorted(path.name for path in directory.iterdir())
    declined = invoke(tmp_path, "--keep", "1", input="n\n")
    assert declined.exit_code == 1 and "Nothing was deleted" in declined.output
    assert "before-schema-4-to-5-b.cairnmind.json.gz" in declined.output
    assert sorted(path.name for path in directory.iterdir()) == before
    accepted = invoke(tmp_path, "--keep", "1", input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert [entry["name"] for entry in safety_backups(directory)] == ["before-schema-3-to-4-a.cairnmind.json.gz",
                                                                      "before-restore-a.cairnmind.json.gz"]
    assert (directory / "1700000000000000000.cairnmind.json.gz").exists() and (directory / "mine.cairnmind.json.gz").exists()
    again = invoke(tmp_path, "--keep", "1")
    assert again.exit_code == 0 and json.loads(again.output)["deleted"] == []


# engine.log

def test_a_log_at_the_limit_is_moved_aside_and_only_the_set_number_of_copies_stays(tmp_path):
    log = tmp_path / "engine.log"
    assert not engine.rotate_log(log, 100, 2)
    for run in range(5):
        log.write_bytes(str(run).encode() * 150)
        assert engine.rotate_log(log, 100, 2)
        assert not log.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["engine.log.1", "engine.log.2"]
    assert (tmp_path / "engine.log.1").read_bytes()[:1] == b"4" and (tmp_path / "engine.log.2").read_bytes()[:1] == b"3"
    log.write_bytes(b"small")
    assert not engine.rotate_log(log, 100, 2) and log.read_bytes() == b"small"
    log.write_bytes(b"z" * 200)
    assert engine.rotate_log(log, 100, 0)
    assert sorted(path.name for path in tmp_path.iterdir()) == []


@pytest.mark.parametrize("limit,copies", [(0, 3), (-5, 3), ("1000", 3), (None, 3), (100, -1), (100, 21), (100, "2")])
def test_a_mistyped_log_setting_falls_back_to_the_default(tmp_path, limit, copies):
    log = tmp_path / "engine.log"
    log.write_bytes(b"x" * 150)
    moved = engine.rotate_log(log, limit, copies)
    # A bad limit is the default of 10 MiB, which 150 bytes do not reach; a bad count keeps the default 3.
    assert moved == (type(limit) is int and limit > 0)


def test_engine_log_stays_under_the_limit_after_repeated_starts(tmp_path, monkeypatch):
    """Each start of a (fake) engine writes 600 bytes. With a limit of 1000 the engine always starts on a log
    under the limit, and the log never holds more than the limit plus what one run writes."""
    at_launch = tmp_path / "sizes"
    java = tmp_path / "fake-jdk" / "bin" / "java"
    java.parent.mkdir(parents=True)
    java.write_text("#!/bin/sh\ncase \"$1\" in -version) echo 'openjdk version \"21.0.13\"' >&2; exit 0;; esac\n"
                    f"stat -c %s engine.log >> '{at_launch}'\n"
                    "head -c 600 /dev/zero | tr '\\0' x\nexit 1\n")
    java.chmod(0o700)
    settings = Settings(tmp_path / "data", java_executable=str(java), arcade_database="prune_test",
                        engine_log_bytes=1000, engine_log_copies=2)
    (engine.runtime_dir(settings) / "lib").mkdir(parents=True)
    monkeypatch.setattr(engine, "engine_status", lambda settings: {"available": False})
    log = settings.data_dir / "arcade" / "engine.log"
    sizes = []
    for _ in range(6):
        with pytest.raises(ArcadeError, match="exited during startup"):
            engine.start_engine(settings)
        sizes.append(log.stat().st_size)
    assert sizes == [600, 1200, 600, 1200, 600, 1200]
    assert [int(line) for line in at_launch.read_text().split()] == [0, 600, 0, 600, 0, 600]
    assert sorted(path.name for path in log.parent.glob("engine.log*")) == ["engine.log", "engine.log.1",
                                                                          "engine.log.2"]


def fake_engine(tmp_path, monkeypatch, **settings):
    java = tmp_path / "fake-jdk" / "bin" / "java"
    java.parent.mkdir(parents=True)
    java.write_text("#!/bin/sh\ncase \"$1\" in -version) echo 'openjdk version \"21.0.13\"' >&2; exit 0;; esac\n"
                    "head -c 600 /dev/zero | tr '\\0' x\nexit 1\n")
    java.chmod(0o700)
    settings = Settings(tmp_path / "data", java_executable=str(java), arcade_database="prune_test", **settings)
    (engine.runtime_dir(settings) / "lib").mkdir(parents=True)
    monkeypatch.setattr(engine, "engine_status", lambda settings: {"available": False})
    return settings


def test_a_rotation_the_filesystem_refuses_does_not_stop_the_engine_from_starting(tmp_path, monkeypatch):
    settings = fake_engine(tmp_path, monkeypatch, engine_log_bytes=1000, engine_log_copies=2)
    state = settings.data_dir / "arcade"
    state.mkdir(parents=True)
    (state / "engine.log").write_bytes(b"o" * 1500)
    (state / "engine.log.5").mkdir()  # a directory where a copy beyond the count would be deleted
    with pytest.raises(ArcadeError, match="exited during startup"):
        engine.start_engine(settings)
    text = (state / "engine.log").read_bytes()
    assert text.startswith(b"o" * 1500) and b"was not rotated (IsADirectoryError" in text
    assert text.endswith(b"x" * 600)


def test_store_size_counts_the_old_engine_logs(workspace):
    store, _, _ = workspace
    state = store.data_dir / "arcade"
    state.mkdir(exist_ok=True)
    (state / "engine.log.1").write_bytes(b"a" * 300)
    (state / "engine.log.2").write_bytes(b"b" * 200)
    (state / "engine.log.old").write_bytes(b"c" * 50)  # not a numbered copy
    (state / "engine.log.3").symlink_to(state / "engine.log.1")  # a link is not counted
    old = measure(Store.open_readonly(store.settings))["disk"]["old_engine_logs"]
    assert (old["files"], old["bytes"]) == (2, 500)
    assert "old engine.log" in measure(Store.open_readonly(store.settings))["text"]
