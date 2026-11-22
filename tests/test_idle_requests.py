"""What an idle knowledge service sends to the store does not grow with the rows of the store (#363)."""
from __future__ import annotations

import json

from cairnmind import embed_jobs, ingest
from cairnmind.service import schedule_embeddings
from cairnmind.store import Store

from test_index_batching import counted, tree

NAMESPACE = "embedding-model@idle"


def chunk_rows(store, pid, count, prefix="c"):
    rows = [{"id": f"{prefix}{n}", "project_id": pid} for n in range(count)]
    for row in rows:
        store.command("INSERT INTO chunks CONTENT :row", {"row": {**row, "source_id": "s", "text": "t", "is_current": True}})
    return rows


def journal_head(store):
    return store.query("SELECT seq FROM journal_state WHERE id='current'")[0]["seq"]


def job_state(store, pid, dedupe):
    rows = store.query("SELECT state,error FROM jobs WHERE project_id=:p AND dedupe_key=:d", {"p": pid, "d": dedupe})
    return rows[0] if rows else None


def test_the_repair_grows_by_batches_of_keys_not_by_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(embed_jobs, "READ_BATCH", 10)
    store = Store(tmp_path / "memory")
    pid = store.add_project("p", tmp_path)["id"]
    sizes = {}
    for count in (25, 100):
        rows = [{"id": f"x{count}-{n}", "project_id": pid} for n in range(count)]
        embed_jobs.repair_embed_jobs(store, rows, NAMESPACE)  # the jobs exist, all pending
        seen = counted(monkeypatch)
        embed_jobs.repair_embed_jobs(store, rows, NAMESPACE)
        sizes[count] = len(seen)
    assert sizes[25] == 3 and sizes[100] - sizes[25] == 7  # ceil(100/10) - ceil(25/10): a bounded batch term


def test_an_idle_repair_writes_nothing(tmp_path, monkeypatch):
    store = Store(tmp_path / "memory")
    pid = store.add_project("p", tmp_path)["id"]
    chunk_rows(store, pid, 30)
    assert schedule_embeddings(store, NAMESPACE) == 30  # every chunk lacks a vector; the jobs are written now
    head = journal_head(store)
    seen = counted(monkeypatch)
    assert schedule_embeddings(store, NAMESPACE) == 30
    assert "begin" not in seen and journal_head(store) == head


def test_the_repair_still_queues_resets_and_replaces_jobs_as_before(tmp_path):
    store = Store(tmp_path / "memory")
    pid = store.add_project("p", tmp_path)["id"]
    rows = chunk_rows(store, pid, 5)
    pending = store.enqueue("embed", pid, {"chunk_id": "c1"}, "embed:c1")
    completed = store.enqueue("embed", pid, {"chunk_id": "c2"}, "embed:c2")
    store.finish_job(completed)
    failed = store.enqueue("embed", pid, {"chunk_id": "c3"}, "embed:c3")
    store.finish_job(failed, "model down")
    raced = store.enqueue("embed", pid, {"chunk_id": "c4"}, "embed:c4")
    store.finish_job(raced)
    assert schedule_embeddings(store, NAMESPACE) == 5
    assert job_state(store, pid, "embed:c0")["state"] == "pending"  # had no job
    assert store.query("SELECT state FROM jobs WHERE id=:id", {"id": pending})[0]["state"] == "pending"
    reset = job_state(store, pid, "embed:c2")
    assert reset["state"] == "pending" and reset["error"] is None  # completed, still no vector
    assert job_state(store, pid, "embed:c3")["state"] == "failed"  # stays failed until retry-jobs
    assert job_state(store, pid, f"embedding-namespace:{NAMESPACE}:c3")["state"] == "pending"
    # A vector that arrives between the read of the rows and the reset keeps its completed job.
    store.finish_job(raced)
    store.command("INSERT INTO embeddings CONTENT :row", {"row": {
        "id": "v4", "chunk_id": "c4", "project_id": pid, "source_id": "s", "model": "embedding-model",
        "namespace": NAMESPACE, "dimension": 3, "vector": [1.0, 0.0, 0.0]}})
    embed_jobs.repair_embed_jobs(store, [rows[4]], NAMESPACE)
    assert store.query("SELECT state FROM jobs WHERE id=:id", {"id": raced})[0]["state"] == "completed"


def transcript(root, text):
    return json.dumps({"type": "user", "cwd": str(root), "sessionId": "s", "message": {"role": "user", "content": text}}) + "\n"


def followed(tmp_path, content):
    root = tmp_path / "repo"
    root.mkdir()
    store = Store(tmp_path / "memory")
    pid = store.add_project("p", root)["id"]
    history = tmp_path / "history.jsonl"
    history.write_text(content(root) if callable(content) else content)
    ingest.import_history(store, history, pid, "claude")
    return store, pid, root, history


def test_an_appended_transcript_line_is_archived_by_the_next_poll(tmp_path, monkeypatch):
    store, pid, root, history = followed(tmp_path, lambda root: transcript(root, "hello 1"))
    assert ingest.poll_imports(store)["archived"] == 0  # looked at in full once, then remembered
    seen = counted(monkeypatch)
    assert ingest.poll_imports(store) == {"files": 0, "archived": 0, "chunks": 0, "inbox": 0, "unknown": 0, "errors": []}
    assert len(seen) == 2  # the cursors and the removed projects: the file is not read again
    with history.open("a") as handle:
        handle.write(transcript(root, "hello 2"))
    assert ingest.poll_imports(store)["archived"] == 1
    assert ingest.poll_imports(store)["archived"] == 0
    # The file rewritten in place with the same size is the one the cursor no longer describes.
    history.write_text(transcript(root, "hello 3") + transcript(root, "hello 4"))
    assert ingest.poll_imports(store)["archived"] >= 1
    # A file that is gone is reported by every poll.
    history.unlink()
    assert [error for error in ingest.poll_imports(store)["errors"] if str(history) in error]


def test_a_skipped_transcript_reports_what_a_full_poll_reports(tmp_path, monkeypatch):
    store, pid, root, history = followed(tmp_path, lambda root: transcript(root, "hello"))
    monkeypatch.setattr(ingest, "_reparse_history_sources_locked", lambda *a, **k: {
        "repaired": 0, "unchanged": 1, "errors": ["Transcript source s: original provenance or raw hash does not match"]})
    ingest.poll_imports(store)
    skipped = ingest.poll_imports(store)["errors"]
    ingest._POLLED.clear()
    assert skipped and skipped == ingest.poll_imports(store)["errors"]
    monkeypatch.undo()
    # The errors of a line are reported in the round that reads it, and not again.
    other = tmp_path / "other"
    other.mkdir()
    store, pid, root, history = followed(other, "")
    history.write_text('{"newSchema": true}\ninvalid-json\n')
    assert len(ingest.poll_imports(store)["errors"]) == 2
    assert ingest.poll_imports(store)["errors"] == []
    assert ingest.poll_imports(store)["errors"] == []  # skipped
    ingest._POLLED.clear()
    assert ingest.poll_imports(store)["errors"] == []  # forced


def test_a_new_process_reindexing_nothing_reads_the_jobs_of_a_file_at_once(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    tree(root, files=40)
    store = Store(tmp_path / "memory")
    pid = store.add_project("p", root)["id"]
    ingest.index_project(store, pid)
    ingest._INDEXED.clear()
    ingest._REMEMBERED.clear()
    seen = counted(monkeypatch)
    result = ingest.index_project(store, pid)
    assert result["files"] >= 40 and not result["changed"]
    documents = 1  # the README queues an extraction job, which is looked up alone
    assert seen.count("SELECT id FROM jobs WHERE job_key=:key") <= documents  # no job read per passage
    assert seen.count("begin") <= result["files"] + documents, seen.count("begin")
