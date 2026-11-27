"""Phase 5 E2E tests: lossless SQLite <-> ArcadeDB migration, chaos resume/abort & ADR-04 (PR #8).

Uses the real pinned ArcadeDB engine and embedded SQLite backend.
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from cairnmind import confirmation, migration, record_kinds, sqlite_telemetry
from cairnmind.config import Settings, resolved_backend
from cairnmind.service import create_app
from cairnmind.store import Store
from cairnmind.verdict_cache import VerdictCache


def _sqlite_settings_with_arcade_env(tmp_path: Path, arcade_test_engine, *, name: str) -> Settings:
    """Create an isolated data_dir initialized on SQLite that targets the test ArcadeDB engine."""
    _engine_settings, prefix, _template = arcade_test_engine
    data_dir = tmp_path / name
    settings = Settings.load(data_dir, create=True)
    settings.storage_backend = "sqlite"
    settings.knowledge_agent = "disabled"
    settings.arcade_database = f"{prefix}_{uuid4().hex[:10]}"
    settings.save()
    return Settings.load(data_dir, create=False)


def _seed_rich_store(store: Store, repo: Path) -> dict:
    """Populate a realistic multi-collection dataset (projects, sources, chunks, confirmed records, graph, vectors)."""
    repo.mkdir(parents=True, exist_ok=True)
    code_path = repo / "engine.py"
    code_text = "DEFAULT_TIMEOUT = 30\n\ndef compute_total(items):\n    return sum(items)\n"
    code_path.write_text(code_text, encoding="utf-8")

    proj = store.add_project("mig_project", repo)
    src = store.snapshot(
        proj["id"],
        f"file://{code_path}",
        code_text,
        "code",
        {"root": str(repo), "path": str(code_path), "relative_path": "engine.py"},
    )
    chunk_id = f"chk-{uuid4().hex[:8]}"
    with store.transaction() as tx:
        tx.insert_document(
            "chunks",
            {
                "id": chunk_id,
                "project_id": proj["id"],
                "source_id": src["id"],
                "text": code_text,
                "line_start": 1,
                "line_end": 4,
                "is_current": True,
                "metadata": {"source_kind": "code"},
            },
        )

    # Record confirmed by a person (tests ADR-04 preservation vs reader downgrade!)
    person_src = confirmation.person_source(store, proj["id"], "Confirmed timeout policy", route="inspector_review")
    with store.person_act("inspector_review", person_src["id"]):
        rec = store.remember(
            proj["id"],
            {
                "kind": "decision",
                "key": "timeout-policy",
                "content": "DEFAULT_TIMEOUT stays at 30 seconds for deterministic retries.",
                "status": "confirmed",
            },
            [src["id"], person_src["id"]],
        )

    # Accept a custom transcript record kind on project (tests ADR-04 withdraw_all for reader!)
    record_kinds.act_as_person(
        store, proj["id"], "claude", "runbook_note", note="team decision", route="inspector_review"
    )

    # Graph vertices & edges
    n1 = store.upsert_node(
        {"id": f"node-1-{uuid4().hex[:6]}", "project_id": proj["id"], "kind": "symbol", "label": "compute_total", "source_id": src["id"]}
    )
    n2 = store.upsert_node(
        {"id": f"node-2-{uuid4().hex[:6]}", "project_id": proj["id"], "kind": "symbol", "label": "DEFAULT_TIMEOUT", "source_id": src["id"]}
    )
    edge = store.upsert_edge(
        {
            "id": f"edge-1-{uuid4().hex[:6]}",
            "project_id": proj["id"],
            "from_id": n1["id"],
            "to_id": n2["id"],
            "kind": "uses",
            "source_id": src["id"],
        }
    )

    # Vector index + embedding
    idx = store.backend.ensure_vector_index(proj["id"], "test-embed", 4, "test-model")
    with store.transaction() as tx:
        store.backend.upsert_embedding(
            tx,
            {
                "id": f"emb-{chunk_id}",
                "project_id": proj["id"],
                "source_id": src["id"],
                "chunk_id": chunk_id,
                "namespace": "test-embed",
                "dimension": 4,
                "vector": [0.25, 0.5, 0.75, 0.25],
                "is_current": True,
            },
            idx,
        )

    # Populate verdict cache in data_dir
    vc = VerdictCache(store.data_dir)
    vc.put("k-1", "level0", {"supported": True, "score": 1.0})

    return {
        "project_id": proj["id"],
        "source_id": src["id"],
        "chunk_id": chunk_id,
        "record_id": rec["id"],
        "edge_id": edge["id"],
    }


def test_e2e_sqlite_to_server_parity_archive_and_continued_service_work(tmp_path: Path, arcade_test_engine):
    settings = _sqlite_settings_with_arcade_env(tmp_path, arcade_test_engine, name="e2e-up")
    repo = tmp_path / "repo-up"

    src_store = Store(settings.data_dir, settings=settings)
    seeded = _seed_rich_store(src_store, repo)
    pre_counts, pre_digests, pre_sha = migration.compute_store_parity(src_store)
    src_store.close()

    # Run full lossless migration: SQLite -> ArcadeDB server!
    report = migration.migrate(settings, to="server", include_cache=True, batch_size=2)
    assert report["migrated"] is True
    assert report["direction"] == "sqlite_to_arcade"
    assert report["parity_verified"] is True
    assert report["sha256"] == pre_sha
    assert report["counts"] == pre_counts
    assert report["source_role"] == "writer"
    assert report["person_confirmations"] == "kept"
    assert report["verdict_cache"]["present"] is True
    assert report["verdict_cache"]["rows"] == 1

    # Local state.db archived to state.db.migrated-<timestamp>.bak
    archived = Path(report["archived_sqlite"])
    assert archived.is_file()
    assert ".migrated-" in archived.name and archived.name.endswith(".bak")
    assert not (settings.data_dir / "state.db").exists()

    # config.json atomically switched to arcade
    reloaded = Settings.load(settings.data_dir, create=False)
    assert resolved_backend(reloaded) == "arcade"

    # Re-open Store & HTTP service (same data_dir, unchanged MCP/client config) -> reads & writes work!
    migrated_store = Store(reloaded.data_dir, settings=reloaded)
    try:
        assert migrated_store.backend.backend_kind == "arcade"
        post_counts, post_digests, post_sha = migration.compute_store_parity(migrated_store)
        assert post_counts == pre_counts
        assert post_digests == pre_digests
        assert post_sha == pre_sha

        # Continued HTTP/MCP work on migrated ArcadeDB store
        app = create_app(reloaded, start_worker=False, background_inference=False, store=migrated_store)
        headers = {"Authorization": f"Bearer {reloaded.token}"}
        with TestClient(app) as client:
            assert client.get("/health").json()["backend"] == "arcadedb"
            projs = client.get("/projects", headers=headers)
            assert projs.status_code == 200
            assert projs.json()[0]["id"] == seeded["project_id"]

            # Write a new record post-cutover via HTTP API (same endpoint MCP calls!)
            rem = client.post(
                "/remember",
                headers=headers,
                json={
                    "project": seeded["project_id"],
                    "record": {
                        "kind": "fact",
                        "key": "post-migration-fact",
                        "content": "Writes succeed cleanly after SQLite -> ArcadeDB cutover.",
                    },
                    "sources": [seeded["source_id"]],
                },
            )
            assert rem.status_code == 200, rem.text
            assert rem.json()["key"] == "post-migration-fact"
    finally:
        migrated_store.close()


def test_e2e_reverse_migrate_server_to_sqlite_threshold_gate_and_force(
    tmp_path: Path, arcade_test_engine, monkeypatch
):
    settings = _sqlite_settings_with_arcade_env(tmp_path, arcade_test_engine, name="e2e-roundtrip")
    repo = tmp_path / "repo-rt"

    src_store = Store(settings.data_dir, settings=settings)
    seeded = _seed_rich_store(src_store, repo)
    src_store.close()

    # First migrate up to ArcadeDB server
    up = migration.migrate(settings, to="server")
    assert up["parity_verified"] is True

    arcade_settings = Settings.load(settings.data_dir, create=False)
    assert resolved_backend(arcade_settings) == "arcade"

    # Reverse migration refused when above SQLite scale limits unless --force
    monkeypatch.setitem(sqlite_telemetry.DEFAULT_THRESHOLDS, "max_files_count", 0)
    with pytest.raises(migration.MigrationRefused) as exc_lim:
        migration.migrate(arcade_settings, to="sqlite")
    assert exc_lim.value.code == "sqlite_limits_exceeded"
    assert resolved_backend(Settings.load(settings.data_dir, create=False)) == "arcade"

    # With --force -> succeeds with 100% SHA-256 parity!
    down = migration.migrate(arcade_settings, to="sqlite", force=True, batch_size=2)
    assert down["migrated"] is True
    assert down["direction"] == "arcade_to_sqlite"
    assert down["parity_verified"] is True
    assert down["sha256"] == up["sha256"]

    back_settings = Settings.load(settings.data_dir, create=False)
    assert resolved_backend(back_settings) == "sqlite"
    assert (settings.data_dir / "state.db").is_file()

    # Verify FTS5 & records work seamlessly back on SQLite!
    back_store = Store(back_settings.data_dir, settings=back_settings)
    try:
        hits = back_store.backend.search_chunks_fts(
            seeded["project_id"], ["DEFAULT_TIMEOUT"], [seeded["source_id"]], 5
        )
        assert hits and hits[0]["id"] == seeded["chunk_id"]
    finally:
        back_store.close()


def test_chaos_crash_at_50_percent_resume_and_abort(tmp_path: Path, arcade_test_engine):
    # --- Part A: Crash at ~50% -> --resume achieves 100% SHA-256 parity ---
    s_resume = _sqlite_settings_with_arcade_env(tmp_path, arcade_test_engine, name="chaos-resume")
    src1 = Store(s_resume.data_dir, settings=s_resume)
    _seed_rich_store(src1, tmp_path / "repo-chaos-1")
    pre_counts, _, pre_sha = migration.compute_store_parity(src1)
    src1.close()

    written_batches = {"count": 0}

    def crash_midway(collection: str, copied: int) -> None:
        written_batches["count"] += 1
        if written_batches["count"] >= 4:
            raise RuntimeError("Simulated power loss at 50% of migration!")

    with pytest.raises(RuntimeError, match="Simulated power loss"):
        migration.migrate(s_resume, to="server", batch_size=1, on_batch_written=crash_midway)

    # Config MUST still be sqlite; checkpoint & marker exist
    assert resolved_backend(Settings.load(s_resume.data_dir, create=False)) == "sqlite"
    assert migration.load_migration_state(s_resume.data_dir) is not None
    assert migration.is_migration_in_progress(s_resume.data_dir) is True

    # Resume completes with 100% exact SHA-256 parity and zero duplicate rows!
    resumed = migration.migrate(s_resume, resume=True, batch_size=2)
    assert resumed["migrated"] is True
    assert resumed["parity_verified"] is True
    assert resumed["sha256"] == pre_sha
    assert resumed["counts"] == pre_counts
    assert migration.load_migration_state(s_resume.data_dir) is None
    assert migration.is_migration_in_progress(s_resume.data_dir) is False

    # --- Part B: Crash at ~50% -> --abort cleans partial target & leaves SQLite untouched ---
    s_abort = _sqlite_settings_with_arcade_env(tmp_path, arcade_test_engine, name="chaos-abort")
    src2 = Store(s_abort.data_dir, settings=s_abort)
    _seed_rich_store(src2, tmp_path / "repo-chaos-2")
    abort_counts, _, abort_sha = migration.compute_store_parity(src2)
    src2.close()

    batches2 = {"count": 0}

    def crash_for_abort(collection: str, copied: int) -> None:
        batches2["count"] += 1
        if batches2["count"] >= 3:
            raise RuntimeError("Simulated crash before abort")

    with pytest.raises(RuntimeError, match="Simulated crash before abort"):
        migration.migrate(s_abort, to="server", batch_size=1, on_batch_written=crash_for_abort)

    aborted = migration.abort_migration(s_abort)
    assert aborted["aborted"] is True
    assert aborted["config_unchanged"] is True
    assert migration.load_migration_state(s_abort.data_dir) is None
    assert migration.is_migration_in_progress(s_abort.data_dir) is False
    assert resolved_backend(Settings.load(s_abort.data_dir, create=False)) == "sqlite"

    # Original SQLite store is 100% untouched!
    still_sqlite = Store(s_abort.data_dir, settings=Settings.load(s_abort.data_dir, create=False))
    try:
        after_counts, _, after_sha = migration.compute_store_parity(still_sqlite)
        assert after_counts == abort_counts
        assert after_sha == abort_sha
    finally:
        still_sqlite.close()


def test_adr04_writer_preserves_person_confirmations_reader_downgrades(tmp_path: Path, arcade_test_engine):
    # --- Role = writer: preserves confirmation.by == "person" and accepted_record_kinds without tmux ---
    s_writer = _sqlite_settings_with_arcade_env(tmp_path, arcade_test_engine, name="adr04-writer")
    st_w = Store(s_writer.data_dir, settings=s_writer)
    seeded_w = _seed_rich_store(st_w, tmp_path / "repo-writer")
    st_w.close()

    rep_w = migration.migrate(s_writer, to="server")
    assert rep_w["source_role"] == "writer"
    assert rep_w["person_confirmations"] == "kept"

    arc_w = Store(s_writer.data_dir, settings=Settings.load(s_writer.data_dir, create=False))
    try:
        rec = arc_w.query("SELECT FROM records WHERE id=:id", {"id": seeded_w["record_id"]})[0]
        assert confirmation.confirmation_of(rec)["by"] == "person"
        proj = arc_w.query("SELECT FROM projects WHERE id=:id", {"id": seeded_w["project_id"]})[0]
        accepted = proj.get(record_kinds.FIELD) or []
        assert any(entry.get("kind") == "runbook_note" for entry in accepted)
    finally:
        arc_w.close()

    # --- Role = reader (foreign writer installation): downgrades confirmations to import & withdraws record kinds ---
    s_reader = _sqlite_settings_with_arcade_env(tmp_path, arcade_test_engine, name="adr04-reader")
    st_r = Store(s_reader.data_dir, settings=s_reader)
    seeded_r = _seed_rich_store(st_r, tmp_path / "repo-reader")
    # Stamp a foreign installation_id into devbrain_meta writer row so this installation is a reader!
    foreign_id = uuid4().hex
    with st_r.backend.raw_transaction() as tx:
        st_r.backend._upsert_row(
            tx._conn,
            "devbrain_meta",
            {
                "name": "writer",
                "installation_id": foreign_id,
                "host": "other-workstation",
                "since": "2026-01-01T00:00:00+00:00",
                "history": [],
            },
            key_field="name",
        )
    st_r.close()

    rep_r = migration.migrate(s_reader, to="server")
    assert rep_r["source_role"] == "reader"
    assert rep_r["person_confirmations"] == "claims_of_the_migration"
    assert rep_r["confirmations_turned_into_claims"] >= 1
    assert rep_r["record_kinds_withdrawn"] >= 1
    assert rep_r["parity_verified"] is True

    arc_r = Store(s_reader.data_dir, settings=Settings.load(s_reader.data_dir, create=False))
    try:
        rec = arc_r.query("SELECT FROM records WHERE id=:id", {"id": seeded_r["record_id"]})[0]
        mark = confirmation.confirmation_of(rec)
        assert mark["by"] == "import"
        assert mark["package"] == "backup"
        proj = arc_r.query("SELECT FROM projects WHERE id=:id", {"id": seeded_r["project_id"]})[0]
        assert not (proj.get(record_kinds.FIELD) or [])
        withdrawn = proj.get(record_kinds.WITHDRAWN_FIELD) or []
        assert any(entry.get("kind") == "runbook_note" for entry in withdrawn)
    finally:
        arc_r.close()


def test_preflight_refuses_non_empty_target_and_accepts_target_namespace(tmp_path: Path, arcade_test_engine):
    _engine_settings, prefix, _template = arcade_test_engine
    settings = _sqlite_settings_with_arcade_env(tmp_path, arcade_test_engine, name="nonempty-target")
    st = Store(settings.data_dir, settings=settings)
    _seed_rich_store(st, tmp_path / "repo-ne")
    st.close()

    # Pre-populate the default target ArcadeDB database so it is non-empty!
    existing_target, _ = migration._open_store_for_backend(settings, backend="arcade")
    dummy_repo = tmp_path / "dummy-existing"
    dummy_repo.mkdir()
    existing_target.add_project("already_there", dummy_repo)
    existing_target.close()

    # Without --overwrite or --target-namespace -> refused!
    with pytest.raises(migration.MigrationRefused) as exc_ne:
        migration.migrate(settings, to="server")
    assert exc_ne.value.code == "target_not_empty"
    assert resolved_backend(Settings.load(settings.data_dir, create=False)) == "sqlite"

    # With --target-namespace -> succeeds cleanly into the new database!
    fresh_ns = f"{prefix}_ns_{uuid4().hex[:8]}"
    rep = migration.migrate(settings, to="server", target_namespace=fresh_ns)
    assert rep["migrated"] is True
    assert rep["target_namespace"] == fresh_ns
    assert rep["parity_verified"] is True
    assert Settings.load(settings.data_dir, create=False).arcade_database == fresh_ns
