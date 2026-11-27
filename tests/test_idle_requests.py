"""What an idle knowledge service sends to the store does not grow with the rows of the store (#363)."""
from __future__ import annotations

import json
import time

import pytest

from cairnmind.storage import embed_jobs
from cairnmind.scip import ingest
from cairnmind.service import schedule_embeddings
from cairnmind.store import Store
from cairnmind.storage.store_writer import StoreNotWriter

from test_index_batching import counted, tree
from test_store_writer import second_installation

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


def test_a_reader_installation_refuses_the_repair_even_when_every_job_exists(tmp_path):
    first = Store(tmp_path / "first")
    pid = first.add_project("p", tmp_path)["id"]
    chunk_rows(first, pid, 3)
    assert schedule_embeddings(first, NAMESPACE) == 3  # every job exists and is pending
    reader = Store(second_installation(tmp_path, first))
    with pytest.raises(StoreNotWriter):
        schedule_embeddings(reader, NAMESPACE)


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


def per_row_repair(store, rows, namespace):
    """The repair as it was before #363: a transaction or more for every chunk."""
    for row in rows:
        job = store.enqueue("embed", row["project_id"], {"chunk_id": row["id"]}, f"embed:{row['id']}")
        if store.query("SELECT state FROM jobs WHERE id=:id", {"id": job})[0]["state"] == "failed":
            job = store.enqueue("embed", row["project_id"], {"chunk_id": row["id"]},
                                f"embedding-namespace:{namespace}:{row['id']}")
        with store.transaction() as tx:
            if not tx.query("SELECT id FROM embeddings WHERE chunk_id=:c AND namespace=:n", {"c": row["id"], "n": namespace}):
                tx.command("UPDATE jobs SET state='pending',error=null,lease_until=null,updated_at=:now "
                           "WHERE id=:id AND state='completed'", {"now": "t", "id": job})


def per_row_queue(store, pid, chunk_ids):
    for chunk in chunk_ids:  # what ingest did for the chunks of a file
        store.enqueue("embed", pid, {"chunk_id": chunk}, f"embed:{chunk}")


def stored_jobs(store, pid):
    """What is stored for the jobs, without what differs by nature (ids, times, the project's id)."""
    jobs = store.query("SELECT * FROM jobs WHERE project_id=:p", {"p": pid})
    names = {job["id"]: job["dedupe_key"] for job in jobs}
    skip = {"id", "project_id", "created_at", "updated_at", "job_key", "last_error_at"}
    rows = sorted(({key: value for key, value in job.items() if key not in skip and not key.startswith('@')} for job in jobs),
                  key=lambda job: job["dedupe_key"])
    for row in rows:
        row["attempt_history"] = [{k: v for k, v in item.items() if k != "at"} for item in row.get("attempt_history") or []]
    keys = sorted(json.loads(job["job_key"])[2] for job in jobs)
    events = sorted((event["entity_type"], event["op"], names.get(event["entity_id"], event["entity_id"]))
                    for event in store.query("SELECT * FROM journal_events WHERE project_id=:p AND entity_type='job'", {"p": pid}))
    return rows, keys, events


def test_jobs_written_in_groups_are_what_the_per_row_path_wrote(tmp_path):
    found = []
    for name in ("rows", "groups"):
        store = Store(tmp_path / name)
        pid = store.add_project("p", tmp_path)["id"]
        rows = chunk_rows(store, pid, 6)
        store.enqueue("embed", pid, {"chunk_id": "c1"}, "embed:c1")
        store.finish_job(store.enqueue("embed", pid, {"chunk_id": "c2"}, "embed:c2"))
        store.finish_job(store.enqueue("embed", pid, {"chunk_id": "c3"}, "embed:c3"), "model down")
        if name == "rows":
            per_row_repair(store, rows, NAMESPACE)
            per_row_queue(store, pid, ["c5", "c6"])
        else:
            schedule_embeddings(store, NAMESPACE)
            embed_jobs.queue_embed_jobs(store, pid, ["c5", "c6"])
        found.append(stored_jobs(store, pid))
    for left, right in zip(*found):
        assert left == right, (left, right)
    assert len(found[0][0]) == 8 and ("job", "upsert", "embed:c6") in found[0][2]

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


def reparse_stub(monkeypatch, answers):
    calls = []

    def stub(*args, **kwargs):
        calls.append(1)
        return answers[min(len(calls), len(answers)) - 1]
    monkeypatch.setattr(ingest, "_reparse_history_sources_locked", stub)
    return calls


def test_a_file_with_reparse_errors_or_repairs_is_imported_by_every_poll(tmp_path, monkeypatch):
    store, pid, root, history = followed(tmp_path, lambda root: transcript(root, "hello"))
    error = "Transcript source s: working directory conflicts with its project"
    calls = reparse_stub(monkeypatch, [{"repaired": 0, "unchanged": 1, "errors": [error]}] * 2
                         + [{"repaired": 1, "unchanged": 0, "errors": []}] * 2
                         + [{"repaired": 0, "unchanged": 0, "errors": []}])
    assert [ingest.poll_imports(store)["errors"] for _ in range(5)] == [[error], [error], [], [], []]
    assert len(calls) == 5  # an error that stops being true is not repeated; a repair continues while it repairs
    ingest.poll_imports(store)
    assert len(calls) == 5  # at last nothing to do: the file is skipped


def test_a_line_appended_during_the_look_is_reported_once(tmp_path, monkeypatch):
    store, pid, root, history = followed(tmp_path, lambda root: transcript(root, "hello"))
    real = ingest.import_history
    appended = []

    def race(*args, **kwargs):
        item = real(*args, **kwargs)  # the look read nothing; a line arrives right after it
        if not appended:
            appended.append(1)
            with history.open("a") as handle:
                handle.write('{"newSchema": true}\n')
        return item
    monkeypatch.setattr(ingest, "import_history", race)
    assert ingest.poll_imports(store)["errors"] == []  # remembered at its end
    assert len(ingest.poll_imports(store)["errors"]) == 1  # no longer at its end: looked at again
    assert ingest.poll_imports(store)["errors"] == []


def test_an_unreadable_secret_registry_is_still_reported_by_every_poll(tmp_path, monkeypatch):
    store, pid, root, history = followed(tmp_path, lambda root: transcript(root, "hello"))
    ingest.poll_imports(store)
    ingest.poll_imports(store)  # remembered
    def refuse(settings):
        raise ValueError("the saved connection profiles cannot be read")
    monkeypatch.setattr(ingest, "ingest_masker", refuse)
    for _ in range(2):
        errors = ingest.poll_imports(store)["errors"]
        assert len(errors) == 1 and "Transcript import refused" in errors[0] and str(history) in errors[0]


def test_a_secret_registry_that_cannot_be_opened_is_reported_per_file_not_raised(tmp_path, monkeypatch):
    store, pid, root, history = followed(tmp_path, lambda root: transcript(root, "hello"))
    ingest.poll_imports(store)
    ingest.poll_imports(store)  # remembered

    def refuse(settings):
        raise PermissionError("the credential directory cannot be traversed")
    monkeypatch.setattr(ingest, "ingest_masker", refuse)
    for _ in range(2):  # an OSError must not abort the scanner's whole round
        errors = ingest.poll_imports(store)["errors"]
        assert len(errors) == 1 and str(history) in errors[0]

def test_a_lines_errors_are_reported_once(tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    # The errors of a line are reported in the round that reads it, and not again.
    store, pid, root, history = followed(other, "")
    history.write_text('{"newSchema": true}\ninvalid-json\n')
    assert len(ingest.poll_imports(store)["errors"]) == 2
    assert ingest.poll_imports(store)["errors"] == []
    assert ingest.poll_imports(store)["errors"] == []  # skipped
    ingest._POLLED.clear()
    assert ingest.poll_imports(store)["errors"] == []  # forced


def settled_store(tmp_path, name, files, histories):
    """An indexed tree with followed transcripts, written now; ``warm`` finishes it once its files are old enough."""
    root = tmp_path / name
    tree(root, files=files)
    store = Store(tmp_path / f"{name}-memory")
    pid = store.add_project("p", root)["id"]
    for number in range(histories):
        path = tmp_path / f"{name}-history{number}.jsonl"
        path.write_text(transcript(root, f"hello {number}"))
        ingest.import_history(store, path, pid, "claude")
    return store, pid, root


def warm(store, pid):
    """Index with every file past the racy window, so that the signature is recorded, and poll once."""
    ingest.index_project_if_changed(store, pid)
    ingest.poll_imports(store)


def idle_round(store, pid):
    """What the service does in an idle round: the repair, the claims, the transcripts, the signature check."""
    schedule_embeddings(store, NAMESPACE)
    store.claim_job("archive", 120, ["import"])
    ingest.poll_imports(store)
    ingest.index_project_if_changed(store, pid)
    ingest.index_pending_sources(store)
    store.claim_job("inference", 60, ["none"])


def test_an_idle_round_costs_the_same_on_a_store_four_times_larger(tmp_path, monkeypatch):
    small = settled_store(tmp_path, "small", 30, 10)
    large = settled_store(tmp_path, "large", 120, 40)
    time.sleep(2.1)  # past RACY_WINDOW_NS, so the signature of the tree can be recorded
    costs = []
    for store, pid, _ in (small, large):
        warm(store, pid)
        warm(store, pid)
        seen = counted(monkeypatch)
        idle_round(store, pid)
        costs.append(len(seen))
        monkeypatch.undo()
    assert costs[0] == costs[1] and costs[1] < 40, costs


@pytest.mark.parametrize("state", ["missing", "elsewhere", "elsewhere-parser"])
def test_an_idle_round_of_a_project_whose_folder_is_unavailable_costs_no_work_per_row(tmp_path, monkeypatch, state):
    monkeypatch.setattr("cairnmind.storage.store_writer.machine_identity", lambda: "machine-id:fixture")
    small = settled_store(tmp_path, "small", 30, 10)
    large = settled_store(tmp_path, "large", 120, 40)
    time.sleep(2.1)
    costs = []
    for store, pid, root in (small, large):
        warm(store, pid)  # indexed here, with its folder
        if state == "missing":
            root.rename(root.with_name(root.name + ".away"))
        else:
            store.command("UPDATE project_roots SET machine='the-first-machine',host='mac' WHERE project_id=:p", {"p": pid})
        if state == "elsewhere-parser":  # the stored analyses are of another parser version: the graph is kept, and said so
            monkeypatch.setattr(ingest, "parser_identity", lambda: "another-release")
        warm(store, pid)  # the round that names the folder; the next ones only repeat it
        warm(store, pid)
        seen = counted(monkeypatch)
        idle_round(store, pid)
        costs.append(len(seen))
        again = ingest.index_project_if_changed(store, pid)
        assert again["skipped"] is True and [item["state"] for item in again["unavailable"]] == [state.split("-")[0]]
        assert len(again["errors"]) == (2 if state == "elsewhere-parser" else 1)
        monkeypatch.undo()
        monkeypatch.setattr("cairnmind.storage.store_writer.machine_identity", lambda: "machine-id:fixture")
    assert costs[0] == costs[1] and costs[1] < 40, costs


def test_a_changed_file_is_indexed_in_the_next_idle_round(tmp_path):
    store, pid, root = settled_store(tmp_path, "tree", 20, 3)
    time.sleep(2.1)
    warm(store, pid)
    assert ingest.index_project_if_changed(store, pid).get("skipped")
    (root / "pkg1" / "module_001.py").write_text("VALUE_1 = 100\n")
    result = ingest.index_project_if_changed(store, pid)
    assert not result.get("skipped") and result["changed"] == 1


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
