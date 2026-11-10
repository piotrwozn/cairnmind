from concurrent.futures import ThreadPoolExecutor
import gzip
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from devbrain.store import Store
from devbrain.verification import run_check, verification_status


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "memory")


@pytest.fixture
def project(store, tmp_path):
    return store.add_project("example", tmp_path / "repo")


def source(store, project, text="User chose token authentication"):
    return store.snapshot(project["id"], "conversation://one", text, "transcript")


def configure_real_check(project):
    """A named local check with a real Git HEAD and no external tool dependency."""
    root = Path(project["root"])
    root.mkdir(parents=True)
    for args in (("init", "-q"), ("config", "user.name", "Storage fixture"),
                 ("config", "user.email", "storage@example.invalid")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (root / "app.py").write_text("value = 1\n")
    script = root / "check_unit.py"
    script.write_text("print('actual named check passed')\n")
    config = root / ".devbrain" / "checks.toml"
    config.parent.mkdir()
    config.write_text(f"[checks.unit]\nargv={json.dumps([sys.executable, script.name])}\ncwd=\".\"\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "check fixture"], check=True, capture_output=True)
    return root


def test_project_roots_resolve_longest_and_aliases(store, project, tmp_path):
    nested = store.add_project("nested", tmp_path / "repo" / "child")
    assert store.resolve_project(tmp_path / "repo" / "child" / "file.py")["id"] == nested["id"]
    alias = tmp_path / "worktree"
    store.add_project_root(project["id"], alias)
    assert store.resolve_project(alias / "src")["id"] == project["id"]
    assert store.add_project("example", tmp_path / "repo")["id"] == project["id"]
    with pytest.raises(ValueError):
        store.add_project_root(nested["id"], alias)


def test_snapshot_dedup_preserves_historical_content(store, project):
    first = source(store, project)
    assert source(store, project)["id"] == first["id"]
    second = source(store, project, "User now chose session authentication")
    assert second["id"] != first["id"]
    assert store.get_source(first["id"])["is_current"] == 0
    assert store.get_source(first["id"])["content"] == first["content"]
    assert store.get_source(second["id"])["is_current"] == 1


def test_native_fulltext_update_delete_and_transaction_rollback(store, project):
    s = source(store, project)
    store.command("INSERT INTO chunks CONTENT :data", {"data": {
        "id": "chunk-one", "project_id": project["id"], "source_id": s["id"],
        "text": "authentication refresh", "is_current": True,
    }})
    assert store.query("SELECT id FROM chunks WHERE text CONTAINSTEXT :q", {"q": "refresh"})[0]["id"] == "chunk-one"
    store.command("UPDATE chunks SET text='login session' WHERE id='chunk-one'")
    assert not store.query("SELECT id FROM chunks WHERE text CONTAINSTEXT :q", {"q": "refresh"})
    assert len(store.query("SELECT id FROM chunks WHERE text CONTAINSTEXT :q", {"q": "session"})) == 1
    with pytest.raises(RuntimeError):
        with store.transaction() as tx:
            tx.command("DELETE FROM chunks WHERE id='chunk-one'")
            raise RuntimeError("rollback")
    assert store.query("SELECT count(*) AS total FROM chunks")[0]["total"] == 1
    store.forget_project(project["id"])
    assert not store.query("SELECT id FROM chunks WHERE text CONTAINSTEXT :q", {"q": "session"})


def test_record_version_conflict_and_supersede_history(store, project):
    s = source(store, project)
    first = store.remember(project["id"], {"kind": "decision", "key": "auth", "content": "Use tokens", "status": "confirmed"}, [s["id"]])
    changed = store.remember(project["id"], {"id": first["id"], "expected_version": 1, "content": "Use signed tokens"}, [s["id"]])
    assert changed["version"] == 2
    with pytest.raises(ValueError, match="version conflict"):
        store.remember(project["id"], {"id": first["id"], "expected_version": 1, "content": "stale update"}, [s["id"]])
    replacement = store.remember(project["id"], {"kind": "decision", "key": "auth", "content": "Use sessions", "supersedes": first["id"], "expected_version": 2}, [s["id"]])
    assert [r["id"] for r in store.list_records(project["id"])] == [replacement["id"]]
    history = store.record_history(first["id"])
    assert [v["version"] for v in history] == [1, 2, 3]
    assert history[-1]["status"] == "superseded"
    assert history[0]["content"] == "Use tokens"


def test_cross_project_sources_and_empty_evidence_rejected(store, project, tmp_path):
    s = source(store, project)
    other = store.add_project("other", tmp_path / "other")
    for evidence in ([s["id"]], [], ["missing"]):
        with pytest.raises(ValueError):
            store.remember(other["id"], {"kind": "fact", "content": "claim"}, evidence)


def test_two_models_cannot_overwrite_same_version(store, project):
    s = source(store, project)
    record = store.remember(project["id"], {"kind": "task", "content": "Implement authentication"}, [s["id"]])

    def update(client):
        try:
            store.remember(project["id"], {"id": record["id"], "expected_version": 1, "metadata": {"state": "in_progress", "client": client}}, [s["id"]])
            return "updated"
        except ValueError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update, ["codex", "claude"])) == ["conflict", "updated"]
    current = store.list_records(project["id"])[0]
    assert current["metadata"]["state"] == "in_progress"
    assert current["version"] == 2


def test_task_completion_requires_declared_evidence_and_dependency(store, project):
    configure_real_check(project)
    s = source(store, project)
    dependency = store.remember(project["id"], {"kind": "task", "content": "Design API"}, [s["id"]])
    task = store.remember(project["id"], {"kind": "task", "content": "Implement API", "metadata": {
        "depends_on": [dependency["id"]], "acceptance_criteria": ["Login works"], "requires_test": True, "requires_review": True,
        "required_profiles": ["unit"],
    }}, [s["id"]])
    with pytest.raises(ValueError, match="dependencies"):
        store.remember(project["id"], {"id": task["id"], "expected_version": 1, "metadata": {"state": "done"}}, [s["id"]])
    store.remember(project["id"], {"id": dependency["id"], "expected_version": 1, "metadata": {"state": "done"}}, [s["id"]])
    with pytest.raises(ValueError, match="acceptance_passed"):
        store.remember(project["id"], {"id": task["id"], "expected_version": 1, "metadata": {"state": "done"}}, [s["id"]])
    evidence = store.snapshot(project["id"], "review://run-1", "Acceptance and review passed", "review_result")
    completion = {
        "state": "done", "acceptance_passed": True, "acceptance_source_ids": [evidence["id"]],
        "test_passed": True, "test_source_ids": [evidence["id"]], "review_passed": True, "review_source_ids": [evidence["id"]],
    }
    with pytest.raises(ValueError, match="fresh machine check"):
        store.remember(project["id"], {"id": task["id"], "expected_version": 1, "metadata": completion},
                       [s["id"], evidence["id"]])
    run = run_check(store, store.settings, project["id"], "unit", task["id"])
    assert run["passed"] is True
    done = store.remember(project["id"], {"id": task["id"], "expected_version": 1, "metadata": {
        **completion, "check_run_ids": [run["id"]],
    }}, [s["id"], evidence["id"]])
    assert done["metadata"]["state"] == "done"
    assert done["metadata"]["requires_test"] is True


def test_task_dependency_cycle_rejected(store, project):
    s = source(store, project)
    a = store.remember(project["id"], {"kind": "task", "content": "A"}, [s["id"]])
    b = store.remember(project["id"], {"kind": "task", "content": "B", "metadata": {"depends_on": [a["id"]]}}, [s["id"]])
    with pytest.raises(ValueError, match="cycle"):
        store.remember(project["id"], {"id": a["id"], "expected_version": 1, "metadata": {"depends_on": [b["id"]]}}, [s["id"]])


def test_jobs_deduplicate_and_claim_once_across_process_like_connections(store, project):
    job_id = store.enqueue("extract", project["id"], {"source_id": "example"}, "one")
    assert store.enqueue("extract", project["id"], {}, "one") == job_id
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = list(pool.map(lambda worker: store.claim_job(str(worker)), range(4)))
    claimed = [job for job in jobs if job]
    assert len(claimed) == 1
    assert claimed[0]["payload"] == {"source_id": "example"}
    store.command("UPDATE jobs SET lease_until=:lease WHERE id=:id", {"lease": time.time() - 1, "id": job_id})
    reclaimed = store.claim_job("replacement")
    assert reclaimed["id"] == job_id
    assert reclaimed["attempts"] == 2
    with pytest.raises(ValueError, match="stale"):
        store.finish_job(job_id, worker="wrong-worker")
    store.finish_job(job_id, worker="replacement")
    assert store.status(project["id"])["jobs"]["completed"] == 1


def test_backup_restore_sources_queue_cursors_and_revisions(store, project, tmp_path):
    s = source(store, project)
    store.checkpoint(project["id"], {"goal": "Finish login", "next_step": "test"})
    store.enqueue("extract", project["id"], {"source_id": s["id"]}, "persist")
    store.command("INSERT INTO import_cursors CONTENT :cursor", {"cursor": {
        "id": "cursor-one", "path": "/history.jsonl", "project_id": project["id"], "client": "codex",
        "offset": 125, "fingerprint": "fingerprint", "updated_at": "now",
    }})
    backup = store.backup(tmp_path / "backup.devbrain.json.gz")
    before = store.export(project["id"])
    store.forget_project(project["id"])
    assert store.status()["projects"] == 0
    store.restore(backup)
    restored = store.export(project["id"])
    for table in ("projects", "sources", "records", "record_revisions", "jobs", "import_cursors"):
        assert restored[table] == before[table]
    assert store.get_source(s["id"])["content"] == s["content"]
    assert store.claim_job("after-restart")["payload"] == {"source_id": s["id"]}
    checkpoint = next(record for record in restored["records"] if record["kind"] == "checkpoint")
    neighbors = store.query("SELECT expand(out('edges')) FROM nodes WHERE id=:id", {"id": f"record:{checkpoint['id']}"})
    assert {row["id"] for row in neighbors} == {f"source:{checkpoint['source_ids'][0]}"}
    assert "config" not in restored


def test_definition_change_preserves_done_history_but_invalidates_verification(store, project):
    configure_real_check(project)
    s = source(store, project)
    task = store.remember(project["id"], {"kind": "task", "content": "Original task", "metadata": {
        "state": "needs_test", "requires_test": True, "required_profiles": ["unit"],
    }}, [s["id"]])
    run = run_check(store, store.settings, project["id"], "unit", task["id"])
    assert run["passed"]
    done = store.remember(project["id"], {"id": task["id"], "expected_version": 1, "metadata": {
        "state": "done", "test_passed": True, "test_source_ids": [run["source_ids"]["stdout"]],
        "check_run_ids": [run["id"]],
    }}, [s["id"], run["source_ids"]["stdout"]])
    assert verification_status(store, done)["status"] == "fresh"
    changed = store.remember(project["id"], {"id": task["id"], "expected_version": 2,
        "content": "Changed task"}, [s["id"]])
    assert changed["metadata"]["state"] == "done"
    assert "test_passed" not in changed["metadata"]
    assert "test_source_ids" not in changed["metadata"]
    assert verification_status(store, changed)["status"] == "stale"
    assert store.record_history(task["id"])[1]["metadata"]["test_passed"] is True
    with pytest.raises(ValueError, match="fresh machine check"):
        store.remember(project["id"], {"id": task["id"], "expected_version": 3,
            "metadata": {"state": "done"}}, [s["id"]])
    rerun = run_check(store, store.settings, project["id"], "unit", task["id"])
    assert rerun["passed"]
    renewed = store.remember(project["id"], {"id": task["id"], "expected_version": 3,
        "metadata": {"state": "done", "check_run_ids": [rerun["id"]]}}, [s["id"]])
    assert verification_status(store, renewed)["status"] == "fresh"


def test_machine_checks_round_trip_without_accepting_forged_links(store, project, tmp_path):
    configure_real_check(project)
    s = source(store, project)
    task = store.remember(project["id"], {"kind": "task", "content": "Verify persistence", "metadata": {
        "requires_test": True, "required_profiles": ["unit"], "state": "needs_test",
    }}, [s["id"]])
    other_task = store.remember(project["id"], {"kind": "task", "content": "Different task"}, [s["id"]])
    run = run_check(store, store.settings, project["id"], "unit", task["id"])
    assert run["passed"]
    for metadata in ({"check_run_ids": ["invented-run"]}, {"check_run_ids": [run["id"]]},
                     {"verification_status": "fresh"}, {"machine_verified": True}, {"verified": True}):
        with pytest.raises(ValueError):
            store.remember(project["id"], {"id": other_task["id"], "expected_version": 1,
                                           "metadata": metadata}, [s["id"]])
    done = store.remember(project["id"], {"id": task["id"], "expected_version": 1, "metadata": {
        "state": "done", "check_run_ids": [run["id"]],
    }}, [s["id"]])
    backup = store.backup(tmp_path / "machine-check.devbrain.json.gz")
    store.forget_project(project["id"])
    store.restore(backup)
    restored = next(row for row in store.list_records(project["id"]) if row["id"] == task["id"])
    assert restored == done
    assert verification_status(store, restored)["status"] == "fresh"
    restored_run = store.query("SELECT FROM check_runs WHERE id=:id", {"id": run["id"]})[0]
    assert {key: value for key, value in restored_run.items() if not key.startswith("@")} == run
    assert store.get_source(run["source_ids"]["stdout"])["content"].strip() == "actual named check passed"


def test_completion_worktree_is_transient_and_does_not_borrow_other_root_runs(store, project, tmp_path):
    root = configure_real_check(project)
    alternate = tmp_path / "alternate"
    subprocess.run(["git", "-C", str(root), "worktree", "add", "--detach", str(alternate), "HEAD"],
                   check=True, capture_output=True)
    store.add_project_root(project["id"], alternate)
    s = source(store, project)
    task = store.remember(project["id"], {"kind": "task", "content": "Scoped completion", "metadata": {
        "requires_test": True, "required_profiles": ["unit"], "state": "needs_test",
    }}, [s["id"]])
    run = run_check(store, store.settings, project["id"], "unit", task["id"], alternate)
    assert run["passed"]
    with pytest.raises(ValueError, match="fresh machine check"):
        store.remember(project["id"], {"id": task["id"], "expected_version": 1,
            "metadata": {"state": "done", "check_run_ids": [run["id"]]}}, [s["id"]])
    done = store.remember(project["id"], {"id": task["id"], "expected_version": 1, "worktree": str(alternate),
        "metadata": {"state": "done", "check_run_ids": [run["id"]]}}, [s["id"]])
    assert verification_status(store, done, alternate)["status"] == "fresh"
    assert verification_status(store, done)["status"] == "unknown"
    assert "worktree" not in done and "worktree" not in done["metadata"]
    assert "_completion_worktree" not in store.record_history(done["id"])[-1]


def test_record_graph_has_evidence_dependencies_and_replacements(store, project):
    s = source(store, project)
    old = store.remember(project["id"], {"kind": "decision", "key": "auth", "content": "Old choice"}, [s["id"]])
    new = store.remember(project["id"], {"kind": "decision", "key": "auth", "content": "New choice", "supersedes": old["id"]}, [s["id"]])
    dep = store.remember(project["id"], {"kind": "task", "content": "A"}, [s["id"]])
    task = store.remember(project["id"], {"kind": "task", "content": "B", "metadata": {"depends_on": [dep["id"]]}}, [s["id"]])
    edges = {(r["from_id"], r["to_id"], r["kind"]) for r in store.query("SELECT FROM edges")}
    assert (f"record:{new['id']}", f"record:{old['id']}", "supersedes") in edges
    assert (f"record:{task['id']}", f"record:{dep['id']}", "depends_on") in edges
    assert (f"record:{new['id']}", f"source:{s['id']}", "supported_by") in edges
    reachable = store.query("TRAVERSE both('edges') FROM (SELECT FROM nodes WHERE id=:id) "
                            "WHILE $depth<=2 LIMIT 20 STRATEGY BREADTH_FIRST", {"id": f"record:{task['id']}"})
    assert f"record:{dep['id']}" in {row.get("id") for row in reachable}


def test_corrupt_or_foreign_backups_rejected_without_destroying_store(store, project, tmp_path):
    corrupt = tmp_path / "bad.gz"
    corrupt.write_text("not an archive")
    with pytest.raises(ValueError):
        store.restore(corrupt)
    foreign = tmp_path / "foreign.gz"
    with gzip.open(foreign, "wt") as handle:
        json.dump({"format": "foreign"}, handle)
    with pytest.raises(ValueError):
        store.restore(foreign)
    assert store.resolve_project(project["id"])["name"] == "example"


def test_native_ann_survives_portable_backup_restore(store, project, tmp_path):
    from devbrain.retrieval import Retriever

    s = source(store, project)
    namespace = "qwen3-embedding:0.6b@test-digest"
    retriever = Retriever(store, store.settings)
    index = retriever._ensure_vector_index(project["id"], namespace, 3)
    chunk = {"id": "semantic-chunk", "project_id": project["id"], "source_id": s["id"],
             "text": "Authentication tokens", "line_start": 1, "line_end": 1,
             "metadata": {}, "is_current": True}
    vector = {"id": "semantic-vector", "chunk_id": chunk["id"], "source_id": s["id"],
              "project_id": project["id"], "model": store.settings.embedding_model,
              "namespace": namespace, "dimension": 3, "vector": [1.0, 0.0, 0.0]}
    store.command("INSERT INTO chunks CONTENT :row", {"row": chunk})
    store.command("INSERT INTO embeddings CONTENT :row", {"row": vector})
    store.command(f"INSERT INTO {index['type_name']} CONTENT :row", {"row": {**vector, "is_current": True}})
    assert retriever.search(project["id"], "unrelated", query_vector=[1, 0, 0], embedding_key=namespace)[0]["id"] == chunk["id"]
    backup = store.backup(tmp_path / "native-ann.devbrain.json.gz")
    store.forget_project(project["id"])
    store.restore(backup)
    restored = Retriever(store, store.settings)
    assert restored.search(project["id"], "unrelated", query_vector=[1, 0, 0], embedding_key=namespace)[0]["id"] == chunk["id"]
    assert not restored.warnings


def test_snapshot_invalidates_native_ann_and_lexical_current_flags(store, project):
    from devbrain.retrieval import Retriever

    s = source(store, project)
    retriever = Retriever(store, store.settings)
    namespace = "test-model@digest"
    index = retriever._ensure_vector_index(project["id"], namespace, 3)
    store.command("INSERT INTO chunks CONTENT :row", {"row": {"id": "old-chunk", "project_id": project["id"],
        "source_id": s["id"], "text": "old authentication", "is_current": True}})
    row = {"id": "old-vector", "chunk_id": "old-chunk", "project_id": project["id"], "source_id": s["id"],
           "model": "test-model", "namespace": namespace, "dimension": 3, "vector": [1.0, 0.0, 0.0], "is_current": True}
    store.command("INSERT INTO embeddings CONTENT :row", {"row": row})
    store.command(f"INSERT INTO {index['type_name']} CONTENT :row", {"row": row})
    source(store, project, "Replacement authentication design")
    assert store.query("SELECT is_current FROM chunks WHERE id='old-chunk'")[0]["is_current"] is False
    assert store.query("SELECT count(*) AS total FROM embeddings")[0]["total"] == 0
    assert store.query(f"SELECT count(*) AS total FROM {index['type_name']}")[0]["total"] == 0


def test_backup_checksum_rejected_before_any_database_mutation(store, project, tmp_path):
    original = source(store, project)
    backup = store.backup(tmp_path / "tampered.devbrain.json.gz")
    with gzip.open(backup, "rt") as handle:
        envelope = json.load(handle)
    envelope["payload"]["sources"][0]["content"] = "changed without updating checksum"
    with gzip.open(backup, "wt") as handle:
        json.dump(envelope, handle)
    with pytest.raises(ValueError, match="checksum"):
        store.restore(backup)
    assert store.get_source(original["id"])["content"] == original["content"]


def test_embedding_repair_uses_digest_namespace_not_configured_model_name(store, project):
    s = source(store, project)
    store.command("INSERT INTO chunks CONTENT :row", {"row": {
        "id": "digest-chunk", "project_id": project["id"], "source_id": s["id"],
        "text": "example", "is_current": True,
    }})
    store.command("INSERT INTO embeddings CONTENT :row", {"row": {
        "id": "old-digest-vector", "chunk_id": "digest-chunk", "project_id": project["id"],
        "source_id": s["id"], "model": "embedding-model", "namespace": "embedding-model@old",
        "dimension": 3, "vector": [1.0, 0.0, 0.0],
    }})
    assert store.missing_embeddings("embedding-model@old", project["id"]) == []
    assert store.missing_embeddings("embedding-model@new", project["id"]) == [
        {"id": "digest-chunk", "project_id": project["id"]}
    ]
    job_id = store.enqueue("embed", project["id"], {"chunk_id": "digest-chunk"}, "new-digest")
    store.finish_job(job_id)
    store.reset_missing_embedding_job(job_id, "digest-chunk", "embedding-model@new")
    assert store.query("SELECT state FROM jobs WHERE id=:id", {"id": job_id})[0]["state"] == "pending"


def _transcript(store, project, uri, text, stamp):
    return store.snapshot(project["id"], uri, text, "transcript", metadata={"source_timestamp": stamp})


def _evidence(record):
    metadata = record["metadata"]
    return metadata["evidence_earliest"], metadata["evidence_latest"], metadata["evidence_basis"]


def test_evidence_time_uses_transcript_time_not_write_time(store, project):
    march = _transcript(store, project, "transcript://march", "We chose tokens in March.", "2026-03-04T10:00:00Z")
    record = store.remember(project["id"], {"kind": "decision", "key": "auth", "content": "Use tokens"}, [march["id"]])
    assert _evidence(record) == ("2026-03-04T10:00:00+00:00", "2026-03-04T10:00:00+00:00", "stated")


def test_evidence_time_falls_back_to_observation_and_mixes_bases(store, project):
    from datetime import datetime
    note = store.snapshot(project["id"], "manual://note", "Tokens expire hourly.", "manual")
    march = _transcript(store, project, "transcript://march", "Tokens expire hourly, said in March.",
                        "2026-03-04T10:00:00+02:00")
    observed = store.remember(project["id"], {"kind": "fact", "key": "ttl", "content": "Hourly expiry"}, [note["id"]])
    assert _evidence(observed)[2] == "observed"
    assert datetime.fromisoformat(_evidence(observed)[1]) == datetime.fromisoformat(note["created_at"])
    mixed = store.remember(project["id"], {"id": observed["id"], "expected_version": 1, "content": "Hourly expiry"},
                           [note["id"], march["id"]])
    assert _evidence(mixed)[0] == "2026-03-04T08:00:00+00:00" and _evidence(mixed)[2] == "mixed"


def test_evidence_time_ignores_naive_timestamps_and_cannot_be_forged(store, project):
    naive = _transcript(store, project, "transcript://naive", "No zone given.", "2026-03-04T10:00:00")
    forged = {"evidence_latest": "2020-01-01T00:00:00+00:00", "evidence_basis": "stated",
              "supersede_warning": "anachronistic_supersede"}
    record = store.remember(project["id"], {"kind": "fact", "key": "zone", "content": "No zone", "metadata": forged},
                            [naive["id"]])
    assert _evidence(record)[2] == "observed"
    assert "supersede_warning" not in record["metadata"]


def test_superseding_with_older_stated_evidence_warns_and_is_recomputed_on_update(store, project):
    june = _transcript(store, project, "transcript://june", "Sessions last one day.", "2026-06-01T09:00:00Z")
    march = _transcript(store, project, "transcript://march", "Sessions last one hour.", "2026-03-01T09:00:00Z")
    august = _transcript(store, project, "transcript://august", "Sessions last a week.", "2026-08-01T09:00:00Z")
    current = store.remember(project["id"], {"kind": "decision", "key": "ttl", "content": "One day"}, [june["id"]])
    older = store.remember(project["id"], {"kind": "decision", "key": "ttl-old", "content": "One hour",
                                           "supersedes": current["id"]}, [march["id"]])
    assert older["metadata"]["supersede_warning"] == "anachronistic_supersede"
    assert older["metadata"]["supersede_replaced_evidence_latest"] == "2026-06-01T09:00:00+00:00"
    # New, later evidence for the same replacement clears the stale warning.
    updated = store.remember(project["id"], {"id": older["id"], "expected_version": 1, "content": "One hour"},
                             [august["id"]])
    assert "supersede_warning" not in updated["metadata"]
    assert "supersede_replaced_evidence_latest" not in updated["metadata"]
    # Moving back to older evidence raises it again.
    again = store.remember(project["id"], {"id": older["id"], "expected_version": 2, "content": "One hour"},
                           [march["id"]])
    assert again["metadata"]["supersede_warning"] == "anachronistic_supersede"


def test_observed_replaced_evidence_never_triggers_the_warning(store, project):
    note = store.snapshot(project["id"], "manual://note", "Sessions last one day.", "manual")
    march = _transcript(store, project, "transcript://march", "Sessions last one hour.", "2026-03-01T09:00:00Z")
    current = store.remember(project["id"], {"kind": "decision", "key": "ttl", "content": "One day"}, [note["id"]])
    replacement = store.remember(project["id"], {"kind": "decision", "key": "ttl-old", "content": "One hour",
                                                 "supersedes": current["id"]}, [march["id"]])
    assert "supersede_warning" not in replacement["metadata"]


def test_public_record_shows_evidence_fields_without_truncation(store, project):
    from devbrain.retrieval import public_record
    june = _transcript(store, project, "transcript://june", "Sessions last one day.", "2026-06-01T09:00:00Z")
    march = _transcript(store, project, "transcript://march", "Sessions last one hour.", "2026-03-01T09:00:00Z")
    current = store.remember(project["id"], {"kind": "decision", "key": "ttl", "content": "One day",
                                             "metadata": {"summary": "kept"}}, [june["id"]])
    older = store.remember(project["id"], {"kind": "decision", "key": "ttl-old", "content": "One hour",
                                           "supersedes": current["id"]}, [march["id"]])
    shown = public_record(older)["metadata"]
    assert shown["evidence_basis"] == "stated" and shown["supersede_warning"] == "anachronistic_supersede"
    assert "truncated" not in shown and "omitted_fields" not in shown
    assert public_record(current)["metadata"]["summary"] == "kept"
