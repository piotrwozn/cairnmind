"""A backup carries no vectors (#187): restore sets the embed jobs back to pending, says so, and the vectors are rebuilt.

The owner's decision of 15 November: backups are smaller, vectors come back from the text, and until then search
uses the text only and says so.
"""
import asyncio
import json
import types

import pytest
from typer.testing import CliRunner

from cairnmind.agents.retrieval import (
    VECTORS_BUILDING,
    VECTORS_FAILED,
    VECTORS_NOT_READY,
    Retriever,
    last_warnings,
)
from cairnmind.cli import app
from cairnmind.scip.ingest import _write_chunks
from cairnmind.storage.restore_claims import VECTORS_REBUILT, VECTORS_UNDECIDED, VECTORS_WAIT, checked_restore
from cairnmind.store import Store
from cairnmind.terminal import person_proof
from cairnmind.terminal.terminal_i18n import tr

NAMESPACE = "embedder@digest"
TEXT = "signed tokens are rotated every night\n" * 30


class Vectors:
    async def embed(self, texts):
        return [[0.25, 0.5, 0.75, 1.0] for _ in texts]

    async def model_identity(self, name):
        return NAMESPACE


def rebuild(store):
    """What the service does with each embed job: the model embeds, the job completes. Returns how many ran."""
    retriever, done = Retriever(store, store.settings, Vectors()), 0
    while job := store.claim_job("test", kinds=["embed"]):
        asyncio.run(retriever.embed_pending(job))
        store.finish_job(job["id"], worker=job["worker"], expected_attempts=job["attempts"])
        done += 1
    return done


@pytest.fixture
def embedded(tmp_path):
    """A store whose chunks are all embedded and whose embed jobs are completed, and a backup of it."""
    store = Store(tmp_path / "data")
    (tmp_path / "repo").mkdir()
    project = store.add_project("demo", tmp_path / "repo")["id"]
    source = store.snapshot(project, "manual://note", TEXT, "manual")
    assert _write_chunks(store, project, source["id"], TEXT) > 0
    jobs = rebuild(store)
    assert jobs > 0 and store.query("SELECT count(*) AS n FROM embeddings")[0]["n"] == jobs
    return types.SimpleNamespace(store=store, project=project, jobs=jobs, tmp=tmp_path,
                                 backup=store.backup(tmp_path / "b.cairnmind.json.gz"))


def test_restore_sets_the_embed_jobs_back_to_pending_and_the_rebuild_restores_the_vectors(embedded):
    target = Store(embedded.tmp / "target")
    target.restore(embedded.backup)
    assert target.embed_jobs_requeued == embedded.jobs
    assert target.query("SELECT count(*) AS n FROM embeddings")[0]["n"] == 0
    assert target.job_summary()["indexing"]["pending"] == embedded.jobs
    assert target.job_summary()["indexing"]["completed"] == 0
    # Text search answers at once; a search that wanted vectors says they are not ready.
    retriever = Retriever(target, target.settings)
    assert retriever.search(embedded.project, "rotated tokens")
    retriever.search(embedded.project, "rotated tokens", query_vector=[0.25, 0.5, 0.75, 1.0], embedding_key=NAMESPACE)
    assert any("not ready" in warning and "text only" in warning for warning in retriever.warnings)
    # The jobs the restore set back are the ones that rebuild every vector.
    assert rebuild(target) == embedded.jobs
    assert target.missing_embeddings(NAMESPACE) == []
    fresh = Retriever(target, target.settings)
    found = fresh.search(embedded.project, "unrelated", query_vector=[0.25, 0.5, 0.75, 1.0], embedding_key=NAMESPACE)
    assert found and not fresh.warnings


def test_search_warns_for_the_whole_rebuild_not_only_before_the_first_vector(embedded):
    for number in range(2):  # the fixture has one chunk; the rebuild needs several jobs
        text = f"note {number} about nightly rotation\n" * 30
        source = embedded.store.snapshot(embedded.project, f"manual://more{number}", text, "manual")
        assert _write_chunks(embedded.store, embedded.project, source["id"], text) > 0
    total = embedded.jobs + rebuild(embedded.store)
    target = Store(embedded.tmp / "target")
    target.restore(embedded.store.backup(embedded.tmp / "more.cairnmind.json.gz"))
    assert total > 1
    retriever = Retriever(target, target.settings, Vectors())
    job = target.claim_job("test", kinds=["embed"])
    asyncio.run(retriever.embed_pending(job))
    target.finish_job(job["id"], worker=job["worker"], expected_attempts=job["attempts"])
    # The registry row exists now, yet most vectors are still missing: the search must say so.
    partial = Retriever(target, target.settings)
    partial.search(embedded.project, "rotated", query_vector=[0.25, 0.5, 0.75, 1.0], embedding_key=NAMESPACE)
    assert f"{VECTORS_BUILDING}{total - 1}" in partial.warnings


def test_a_restore_into_the_same_store_rebuilds_into_its_emptied_vector_type(embedded):
    """The store keeps its emptied vector_* type after the restore; the rebuild registers it again and fills it."""
    store = embedded.store
    store.restore(embedded.backup)
    assert store.embed_jobs_requeued == embedded.jobs
    assert rebuild(store) == embedded.jobs
    retriever = Retriever(store, store.settings)
    found = retriever.search(embedded.project, "unrelated", query_vector=[0.25, 0.5, 0.75, 1.0], embedding_key=NAMESPACE)
    assert found and not retriever.warnings
    assert store.query("SELECT count(*) AS n FROM vector_indexes")[0]["n"] == 1


def test_the_workspace_shows_both_vector_warnings_in_polish():
    """The answer view of the workspace says the warnings of a search without its vectors in the person's language."""
    from cairnmind.terminal.terminal_commands import ask_summary
    answer = {"status": "complete", "answer": "x", "freshness": {"warnings": [VECTORS_NOT_READY, f"{VECTORS_BUILDING}7"]}}
    shown = ask_summary(answer, lambda text: tr(text, "pl"))
    assert "- " + tr(VECTORS_NOT_READY, "pl") in shown and tr(VECTORS_NOT_READY, "pl") != VECTORS_NOT_READY
    assert "pozostałe zadania osadzania: 7" in shown
    assert f"- {VECTORS_BUILDING}7" in ask_summary(answer, str)  # English stays as the search wrote it


def test_two_completed_jobs_of_one_chunk_are_requeued_and_counted_once(embedded):
    chunk = embedded.store.query("SELECT id FROM chunks WHERE is_current=true")[0]["id"]
    with embedded.store.transaction() as tx:
        extra = embedded.store.enqueue_new(tx, "embed", embedded.project, {"chunk_id": chunk}, "extra:" + chunk)
        tx.command("UPDATE jobs SET state='completed' WHERE id=:id", {"id": extra})
    target = Store(embedded.tmp / "target")
    target.restore(embedded.store.backup(embedded.tmp / "two.cairnmind.json.gz"))
    assert target.embed_jobs_requeued == embedded.jobs


def test_a_job_of_a_replaced_chunk_is_not_counted(embedded):
    # A completed job whose chunk is gone (replaced, #186) has nothing to rebuild.
    embedded.store.snapshot(embedded.project, "manual://note", "A replacement of the note", "manual")
    gone = embedded.store.backup(embedded.tmp / "gone.cairnmind.json.gz")
    target = Store(embedded.tmp / "target")
    target.restore(gone)
    assert target.embed_jobs_requeued == 0
    assert target.job_summary()["indexing"]["pending"] == 0


def test_the_output_of_restore_says_how_many_jobs_and_that_search_is_text_only(embedded, monkeypatch):
    monkeypatch.setattr("cairnmind.cli.service_alive", lambda settings: False)
    monkeypatch.setattr(person_proof, "restore_proof", lambda settings: "key")
    result = CliRunner().invoke(app, ["--data-dir", str(embedded.store.data_dir), "restore", str(embedded.backup)])
    assert result.exit_code == 0, (result.output, result.exception)
    assert json.loads(result.stdout)["embed_jobs_requeued"] == embedded.jobs
    assert f"{embedded.jobs} embedding job(s) are pending again" in result.stderr
    assert "search uses the text only" in result.stderr and "cairnmind status" in result.stderr


def test_a_rebuild_whose_jobs_failed_still_says_vectors_are_missing(embedded):
    for number in range(2):
        text = f"note {number} about nightly rotation\n" * 30
        source = embedded.store.snapshot(embedded.project, f"manual://more{number}", text, "manual")
        assert _write_chunks(embedded.store, embedded.project, source["id"], text) > 0
    rebuild(embedded.store)
    target = Store(embedded.tmp / "target")
    target.restore(embedded.store.backup(embedded.tmp / "more.cairnmind.json.gz"))
    retriever = Retriever(target, target.settings, Vectors())
    job = target.claim_job("test", kinds=["embed"])
    asyncio.run(retriever.embed_pending(job))
    target.finish_job(job["id"], worker=job["worker"], expected_attempts=job["attempts"])
    target.command("UPDATE jobs SET state='failed' WHERE kind='embed' AND state<>'completed'")
    missing = len(target.missing_embeddings(NAMESPACE))
    assert missing > 0 and target.job_summary()["indexing"]["pending"] == 0
    search = Retriever(target, target.settings)
    search.search(embedded.project, "rotated", query_vector=[0.25, 0.5, 0.75, 1.0], embedding_key=NAMESPACE)
    assert f"{VECTORS_FAILED}{missing}" in search.warnings
    from cairnmind.terminal.terminal_commands import ask_summary
    shown = ask_summary({"status": "complete", "answer": "x", "freshness": {"warnings": search.warnings}}, lambda text: tr(text, "pl"))
    assert f"fragmenty: {missing}" in shown and "retry-jobs" in shown and VECTORS_FAILED not in shown
    target.retry_jobs()  # the sentence's own advice: the jobs run again and the warning goes
    assert rebuild(target) == missing
    done = Retriever(target, target.settings)
    done.search(embedded.project, "rotated", query_vector=[0.25, 0.5, 0.75, 1.0], embedding_key=NAMESPACE)
    assert not done.warnings


def test_the_vector_warning_survives_the_cut_to_the_last_warnings():
    others = [f"Cannot verify current source: file{n}" for n in range(9)]
    kept = last_warnings([f"{VECTORS_BUILDING}4", *others], 8)
    assert len(kept) == 8 and kept[0] == f"{VECTORS_BUILDING}4" and kept[1:] == others[-7:]
    assert last_warnings([*others, "last"], 8) == [*others[2:], "last"]  # nothing about vectors: the plain tail


@pytest.mark.parametrize("agent, template", [("enabled", VECTORS_REBUILT), ("", VECTORS_UNDECIDED), ("disabled", VECTORS_WAIT)])
def test_checked_restore_names_the_count_and_a_deterministic_only_store_says_the_jobs_wait(embedded, monkeypatch, agent, template):
    monkeypatch.setattr(person_proof, "restore_proof", lambda settings: "key")
    outcome = checked_restore(types.SimpleNamespace(knowledge_agent=agent), embedded.backup,
                              lambda: embedded.store)
    assert outcome["embed_jobs_requeued"] == embedded.jobs
    assert outcome["vectors_notice"] == (template, {"jobs": embedded.jobs})
    assert "wektorów" in tr(template, "pl")  # every sentence is in Polish through the terminal catalog


def test_a_backup_that_carries_vectors_is_restored_with_them_and_nothing_is_requeued(embedded):
    """An older file: the writer of this version leaves vectors out, restore still loads those a file brings."""
    import gzip
    import hashlib

    from cairnmind.storage.sql import clean_row, json_text, quote_name
    envelope = json.loads(gzip.decompress(embedded.backup.read_bytes()))
    payload = envelope["payload"]
    assert payload["embeddings"] == [] and payload["vector_indexes"] == [] and payload["vector_data"] == {}
    store = embedded.store
    payload["embeddings"] = [clean_row(row) for row in store.query("SELECT FROM embeddings")]
    payload["vector_indexes"] = [clean_row(row) for row in store.query("SELECT FROM vector_indexes")]
    payload["vector_data"] = {row["type_name"]: [clean_row(v) for v in store.query(f"SELECT FROM {quote_name(row['type_name'])}")]
                              for row in payload["vector_indexes"]}
    envelope["sha256"] = hashlib.sha256(json_text(payload).encode()).hexdigest()
    older = embedded.tmp / "older.cairnmind.json.gz"
    older.write_bytes(gzip.compress(json_text(envelope).encode()))
    target = Store(embedded.tmp / "target")
    target.restore(older)
    assert target.embed_jobs_requeued is None
    assert target.query("SELECT count(*) AS n FROM embeddings")[0]["n"] == embedded.jobs
    assert target.job_summary()["indexing"]["completed"] == embedded.jobs
    found = Retriever(target, target.settings).search(embedded.project, "unrelated", query_vector=[0.25, 0.5, 0.75, 1.0],
                                                      embedding_key=NAMESPACE)
    assert found
