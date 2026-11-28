"""Native sqlite-vec (vec0) ANN vector indexing, parity, lifecycle, and fallback tests."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest

from cairnmind.config import Settings, resolved_backend
from cairnmind.storage.sqlite_backend import SQLiteBackend
from cairnmind.storage.sqlite_vectors import vec0_table_exists, vec0_table_name, vector_type_name
from cairnmind.storage.storage_backend import ALL_COLLECTIONS
from cairnmind.storage.storage_query import is_in
from cairnmind.store import Store

HAS_SQLITE_VEC = importlib.util.find_spec("sqlite_vec") is not None


def _make_sqlite_store(tmp_path: Path, name: str = "vec_data") -> Store:
    data_dir = tmp_path / name
    settings = Settings.load(data_dir, create=True)
    settings.storage_backend = "sqlite"
    settings.save()
    return Store(data_dir, settings=settings, backend="sqlite")


def _seed_chunks_and_vectors(
    store: Store,
    tmp_path: Path,
    vectors: list[tuple[str, list[float]]],
    *,
    namespace: str = "test-embed@v1",
) -> tuple[dict, list[tuple[str, str, str]]]:
    repo = tmp_path / f"repo_{uuid4().hex[:6]}"
    repo.mkdir(parents=True, exist_ok=True)
    proj = store.add_project(f"proj_{uuid4().hex[:6]}", repo)
    dim = len(vectors[0][1])
    idx = store.backend.ensure_vector_index(proj["id"], namespace, dim, "test-embed")
    assert idx is not None

    entries: list[tuple[str, str, str]] = []
    with store.transaction() as tx:
        for label, vec in vectors:
            src = store.snapshot(
                proj["id"],
                f"file://{repo / f'{label}.py'}",
                f"# {label}\n",
                "code",
                {"root": str(repo), "relative_path": f"{label}.py", "path": str(repo / f"{label}.py")},
            )
            cid = f"chk-{label}"
            eid = f"emb-{label}"
            store.backend.insert_document(
                tx,
                "chunks",
                {
                    "id": cid,
                    "project_id": proj["id"],
                    "source_id": src["id"],
                    "text": f"content {label}",
                    "line_start": 1,
                    "line_end": 1,
                    "is_current": True,
                    "metadata": {},
                },
            )
            store.backend.upsert_embedding(
                tx,
                {
                    "id": eid,
                    "project_id": proj["id"],
                    "source_id": src["id"],
                    "chunk_id": cid,
                    "namespace": namespace,
                    "dimension": dim,
                    "vector": vec,
                    "is_current": True,
                },
                idx,
            )
            entries.append((src["id"], cid, eid))
    return proj, entries


def test_storage_backend_env_var_selection(monkeypatch, tmp_path: Path):
    data_dir = tmp_path / "env_sqlite"
    monkeypatch.setenv("CAIRNMIND_STORAGE_BACKEND", "sqlite")
    settings = Settings.load(data_dir, create=True)
    assert settings.storage_backend == "sqlite"
    assert resolved_backend(settings) == "sqlite"

    # CAIRNMIND_STORAGE_BACKEND takes precedence over CAIRNMIND_BACKEND
    monkeypatch.setenv("CAIRNMIND_BACKEND", "arcade")
    settings2 = Settings.load(data_dir, create=False)
    assert settings2.storage_backend == "sqlite"


def test_sqlite_vector_fallback_when_extension_disabled(tmp_path: Path):
    """Exact cosine math.sumprod fallback works deterministically when sqlite-vec is absent."""
    store = _make_sqlite_store(tmp_path, "fallback_only")
    try:
        backend = store.backend
        assert isinstance(backend, SQLiteBackend)
        backend._conn()
        backend._sqlite_vec_available = False

        proj, entries = _seed_chunks_and_vectors(
            store,
            tmp_path,
            [
                ("a", [1.0, 0.0, 0.0, 0.0]),
                ("b", [0.8, 0.6, 0.0, 0.0]),
                ("c", [0.0, 1.0, 0.0, 0.0]),
                ("zero", [0.0, 0.0, 0.0, 0.0]),
            ],
        )
        all_sources = [src_id for src_id, _, _ in entries]
        present, neighbors, warning = backend.search_vectors(
            proj["id"], "test-embed@v1", [1.0, 0.0, 0.0, 0.0], all_sources, limit=4
        )
        assert present is True
        assert warning is None
        assert [n["chunk_id"] for n in neighbors] == ["chk-a", "chk-b", "chk-c", "chk-zero"]
        assert neighbors[0]["distance"] == pytest.approx(0.0, abs=1e-6)
        assert neighbors[1]["distance"] == pytest.approx(0.2, abs=1e-5)
        assert neighbors[2]["distance"] == pytest.approx(1.0, abs=1e-6)
        assert neighbors[3]["distance"] == pytest.approx(1.0, abs=1e-6)
        snap = backend.metrics_snapshot()
        assert snap["vec_ann_queries"] == 0
        assert snap["vec_fallback_queries"] == 1
    finally:
        store.close()


@pytest.mark.skipif(not HAS_SQLITE_VEC, reason="optional cairnmind[vec] extra (sqlite-vec) not installed")
def test_sqlite_vec_ann_execution_parity_and_lifecycle(tmp_path: Path):
    """With cairnmind[vec], SQLiteBackend executes native vec0 KNN matching cosine fallback."""
    store = _make_sqlite_store(tmp_path, "vec0_live")
    try:
        backend = store.backend
        assert isinstance(backend, SQLiteBackend)
        conn = backend._conn()
        assert backend.sqlite_vec_loaded is True

        vectors = [
            ("a", [1.0, 0.0, 0.0, 0.0]),
            ("b", [0.9, 0.43589, 0.0, 0.0]),
            ("c", [0.5, 0.866025, 0.0, 0.0]),
            ("d", [0.0, 1.0, 0.0, 0.0]),
            ("zero", [0.0, 0.0, 0.0, 0.0]),
        ]
        proj, entries = _seed_chunks_and_vectors(store, tmp_path, vectors)
        vec_table = vec0_table_name(vector_type_name(proj["id"], "test-embed@v1", 4))
        assert vec0_table_exists(conn, vec_table) is True

        all_sources = [src_id for src_id, _, _ in entries]
        query = [0.98, 0.198997, 0.0, 0.0]

        # 1. Native vec0 KNN execution
        present_ann, ann_neighbors, warn_ann = backend.search_vectors(
            proj["id"], "test-embed@v1", query, all_sources, limit=5
        )
        assert present_ann is True
        assert warn_ann is None
        assert backend.metrics_snapshot()["vec_ann_queries"] == 1
        assert backend.metrics_snapshot()["vec_fallback_queries"] == 0

        # 2. Exact parity with fallback cosine scan
        backend._sqlite_vec_available = False
        present_fb, fb_neighbors, warn_fb = backend.search_vectors(
            proj["id"], "test-embed@v1", query, all_sources, limit=5
        )
        backend._sqlite_vec_available = True
        assert present_fb is True and warn_fb is None
        assert [n["chunk_id"] for n in ann_neighbors] == [n["chunk_id"] for n in fb_neighbors]
        for ann_item, fb_item in zip(ann_neighbors, fb_neighbors, strict=True):
            assert ann_item["distance"] == pytest.approx(fb_item["distance"], abs=1e-5)

        # 3. Large eligible_sources list (>500, triggers TEMP TABLE) works natively in vec0 KNN
        large_sources = [entries[2][0], *(f"missing-src-{i}" for i in range(1500))]
        present_big, big_neighbors, _ = backend.search_vectors(
            proj["id"], "test-embed@v1", query, large_sources, limit=3
        )
        assert present_big is True
        assert [n["chunk_id"] for n in big_neighbors] == ["chk-c"]
        assert backend.metrics_snapshot()["vec_ann_queries"] == 2

        # 4. Zero-norm query vector falls back safely without NaN
        present_z, z_neighbors, _ = backend.search_vectors(
            proj["id"], "test-embed@v1", [0.0, 0.0, 0.0, 0.0], all_sources, limit=3
        )
        assert present_z is True
        assert len(z_neighbors) == 3
        assert all(n["distance"] == pytest.approx(1.0) for n in z_neighbors)

        # 5. Upsert overwrite updates vec0 in place
        with store.transaction() as tx:
            backend.upsert_embedding(
                tx,
                {
                    "id": entries[3][2],
                    "project_id": proj["id"],
                    "source_id": entries[3][0],
                    "chunk_id": entries[3][1],
                    "namespace": "test-embed@v1",
                    "dimension": 4,
                    "vector": [1.0, 0.0, 0.0, 0.0],
                    "is_current": True,
                },
            )
        _, after_upd, _ = backend.search_vectors(proj["id"], "test-embed@v1", [1.0, 0.0, 0.0, 0.0], all_sources, limit=2)
        assert {n["chunk_id"] for n in after_upd} == {"chk-a", "chk-d"}

        # 6. Delete by source_id and direct delete_where("embeddings") stay synced with vec0
        with store.transaction() as tx:
            backend.delete_vectors_for_sources(tx, [entries[0][0]])
            backend.delete_where(tx, "embeddings", is_in("id", [entries[3][2]]))
        _, after_del, _ = backend.search_vectors(proj["id"], "test-embed@v1", [1.0, 0.0, 0.0, 0.0], all_sources, limit=5)
        remaining_ids = {n["chunk_id"] for n in after_del}
        assert "chk-a" not in remaining_ids and "chk-d" not in remaining_ids
        assert "chk-b" in remaining_ids

        # 7. Read-only Store executes native vec0 KNN directly
        ro = Store.open_readonly(Settings.load(store.data_dir))
        try:
            assert isinstance(ro.backend, SQLiteBackend)
            ro_present, ro_neighbors, _ = ro.backend.search_vectors(
                proj["id"], "test-embed@v1", [1.0, 0.0, 0.0, 0.0], all_sources, limit=3
            )
            assert ro_present is True
            assert ro_neighbors[0]["chunk_id"] == "chk-b"
            assert ro.backend.metrics_snapshot()["vec_ann_queries"] == 1
        finally:
            ro.close()

        # 8. Delete by project and wipe_all_collections drop/clean vec0 tables cleanly
        with store.transaction() as tx:
            backend.delete_vectors_for_project(tx, proj["id"])
        assert conn.execute(f'SELECT count(*) FROM "{vec_table}"').fetchone()[0] == 0

        backend.wipe_all_collections(ALL_COLLECTIONS)
        assert vec0_table_exists(conn, vec_table) is False
    finally:
        store.close()


@pytest.mark.skipif(not HAS_SQLITE_VEC, reason="optional cairnmind[vec] extra (sqlite-vec) not installed")
def test_sqlite_vec_backfills_legacy_db_and_ro_fallback(tmp_path: Path):
    """A DB populated without sqlite-vec works read-only (fallback) and auto-backfills when opened writable."""
    data_dir = tmp_path / "legacy_vec_data"
    settings = Settings.load(data_dir, create=True)
    settings.storage_backend = "sqlite"
    settings.save()

    # Step 1: Populate embeddings with sqlite-vec disabled (simulates DB created without cairnmind[vec])
    store1 = Store(data_dir, settings=settings, backend="sqlite")
    try:
        assert isinstance(store1.backend, SQLiteBackend)
        store1.backend._conn()
        store1.backend._sqlite_vec_available = False
        proj, entries = _seed_chunks_and_vectors(
            store1,
            tmp_path,
            [("alpha", [1.0, 0.0, 0.0, 0.0]), ("beta", [0.0, 1.0, 0.0, 0.0])],
        )
        all_sources = [src_id for src_id, _, _ in entries]
        vec_table = vec0_table_name(vector_type_name(proj["id"], "test-embed@v1", 4))
        assert vec0_table_exists(store1.backend._conn(), vec_table) is False
    finally:
        store1.close()

    # Step 2: Open read-only WITH sqlite-vec installed -> cannot create vec0 table, falls back cleanly
    ro = Store.open_readonly(Settings.load(data_dir))
    try:
        assert isinstance(ro.backend, SQLiteBackend)
        present, neighbors, _ = ro.backend.search_vectors(
            proj["id"], "test-embed@v1", [1.0, 0.0, 0.0, 0.0], all_sources, limit=2
        )
        assert present is True
        assert [n["chunk_id"] for n in neighbors] == ["chk-alpha", "chk-beta"]
        assert ro.backend.metrics_snapshot()["vec_fallback_queries"] == 1
        assert ro.backend.metrics_snapshot()["vec_ann_queries"] == 0
    finally:
        ro.close()

    # Step 3: Open writable WITH sqlite-vec installed -> auto-backfills vec0 on first search and uses ANN
    store2 = Store(data_dir, settings=Settings.load(data_dir), backend="sqlite")
    try:
        assert isinstance(store2.backend, SQLiteBackend)
        present2, neighbors2, _ = store2.backend.search_vectors(
            proj["id"], "test-embed@v1", [1.0, 0.0, 0.0, 0.0], all_sources, limit=2
        )
        assert present2 is True
        assert [n["chunk_id"] for n in neighbors2] == ["chk-alpha", "chk-beta"]
        assert vec0_table_exists(store2.backend._conn(), vec_table) is True
        assert store2.backend.metrics_snapshot()["vec_ann_queries"] == 1
    finally:
        store2.close()


def test_ci_workflow_has_sqlite_and_vec_job():
    ci_yml = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
    text = ci_yml.read_text(encoding="utf-8")
    assert "\n  sqlite-tests:\n" in text
    assert "CAIRNMIND_STORAGE_BACKEND: sqlite" in text
    assert "uv sync --locked --extra dev --extra vec" in text
    assert "tests/test_sqlite_vec_ann.py" in text

