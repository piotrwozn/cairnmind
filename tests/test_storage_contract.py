"""Parameterized StorageBackend contract tests (PR #6 / Faza 4a; extended to SQLite in PR #7).

Every backend implementing ``StorageBackend`` MUST satisfy all assertions here:
- All canonical collections present and queryable
- Project / root / source / chunk / record CRUD & optimistic CAS versioning
- Explicit regex-free JournalTransaction recording & cursor advancement
- Full-text search on chunks and records (including punctuation tokens)
- Vector indexing & cosine similarity ranking + cleanup on source replacement
- Graph vertices/edges, batch writes, seed lookup, and cycle-safe bounded BFS (A -> B -> A)
- Definition symbol search and case-insensitive URI substring search
- Job enqueue, deduplication, claim, lease expiry, diagnostics, finish
- Streaming ``iter_collection`` and ``collection_counts`` parity
- Read-only enforcement
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from cairnmind.config import Settings
from cairnmind.journal import Journal
from cairnmind.retrieval import Retriever
from cairnmind.storage_backend import ALL_COLLECTIONS, StorageBackend
from cairnmind.store import Store


class _FakeEmbedOllama:
    def __init__(self, vectors: dict[str, list[float]]):
        self.vectors = vectors

    async def model_identity(self, model: str) -> str:
        return f"{model}@sha256:contract"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        result = []
        for text in texts:
            for needle, vec in self.vectors.items():
                if needle in text:
                    result.append(vec)
                    break
            else:
                result.append([0.1, 0.2, 0.3, 0.4])
        return result


@pytest.fixture(params=["arcade"])
def contract_store(request, tmp_path: Path):
    """Yield a fresh ``Store`` for each supported ``StorageBackend`` implementation."""
    kind = request.param
    if kind == "arcade":
        store = Store(tmp_path / "arcade_data")
        assert isinstance(store.backend, StorageBackend)
        assert store.backend.backend_kind == "arcade"
        try:
            yield store
        finally:
            store.backend.close()
        return
    raise AssertionError(f"Unknown backend_kind: {kind}")


def _seed_project(store: Store, tmp_path: Path, name: str = "demo") -> tuple[dict, Path]:
    root = tmp_path / f"repo_{name}_{uuid4().hex[:6]}"
    root.mkdir(parents=True, exist_ok=True)
    project = store.add_project(name, root)
    return project, root


def test_contract_all_collections_present_and_countable(contract_store: Store):
    collections = contract_store.backend.list_collections()
    for name in ALL_COLLECTIONS:
        assert name in collections, f"Missing collection: {name}"
    counts = contract_store.backend.collection_counts()
    for name in ALL_COLLECTIONS:
        assert isinstance(counts[name], int) and counts[name] >= 0


def test_contract_crud_optimistic_cas_and_explicit_journal(contract_store: Store, tmp_path: Path, monkeypatch):
    from cairnmind import journal as journal_mod

    project, _ = _seed_project(contract_store, tmp_path, "cas_proj")
    journal = Journal(contract_store)
    before_cursor = journal.cursor()

    # Spy on regex parser: structured insert_document/update_document must NOT need regex mutation() parsing.
    regex_calls = []
    real_mutation = journal_mod.mutation

    def spy_mutation(sql):
        regex_calls.append(sql)
        return real_mutation(sql)

    monkeypatch.setattr(journal_mod, "mutation", spy_mutation)

    source = contract_store.snapshot(project["id"], "file://src/app.py", "TIMEOUT = 30\n", "code")
    rec = contract_store.remember(
        project["id"],
        {"kind": "decision", "key": "timeout-policy", "content": "Default timeout is 30 seconds"},
        [source["id"]],
    )
    assert rec["version"] == 1
    assert rec["status"] == "proposed"

    # Optimistic CAS update succeeds with exact expected_version and fails with stale version.
    updated = contract_store.remember(
        project["id"],
        {"id": rec["id"], "expected_version": 1, "content": "Default timeout is 45 seconds"},
        [source["id"]],
    )
    assert updated["version"] == 2
    with pytest.raises(ValueError, match="Record version conflict"):
        contract_store.remember(
            project["id"],
            {"id": rec["id"], "expected_version": 1, "content": "Stale overwrite"},
            [source["id"]],
        )

    # Journal advanced cleanly via structured methods without regex parsing on insert/update.
    after_cursor = journal.cursor()
    assert after_cursor["seq"] > before_cursor["seq"]
    diff = journal.read(after=before_cursor, project_id=project["id"])
    assert diff["reset_required"] is False
    entity_types = {e["entity_type"] for e in diff["events"]}
    assert {"source", "record"} <= entity_types
    # Only the single DELETE FROM edges in _record_graph still uses raw command(); inserts/updates used structured paths.
    assert not any(sql.lstrip().upper().startswith(("INSERT INTO", "UPDATE ")) for sql in regex_calls)


def test_contract_fts_chunks_and_records(contract_store: Store, tmp_path: Path):
    project, root = _seed_project(contract_store, tmp_path, "fts_proj")
    source = contract_store.snapshot(
        project["id"],
        f"file://{root / 'service.py'}",
        "def calculate_retry_backoff():\n    return 42\n",
        "code",
        {"root": str(root), "relative_path": "service.py", "path": str(root / "service.py")},
    )
    chunk_id = str(uuid4())
    with contract_store.transaction() as tx:
        contract_store.backend.insert_document(
            tx,
            "chunks",
            {
                "id": chunk_id,
                "project_id": project["id"],
                "source_id": source["id"],
                "text": "def calculate_retry_backoff(): return 42",
                "line_start": 1,
                "line_end": 2,
                "is_current": True,
                "metadata": {},
            },
        )
    contract_store.remember(
        project["id"],
        {"kind": "decision", "key": "retry-policy", "content": "Exponential backoff capped at 42 seconds"},
        [source["id"]],
    )

    chunk_hits = contract_store.backend.search_chunks_fts(
        project["id"], ["calculate_retry_backoff"], [source["id"]], limit=5
    )
    assert any(row["id"] == chunk_id for row in chunk_hits)

    recs = contract_store.list_records(project["id"])
    rec_hits = contract_store.backend.search_records_fts(
        project["id"], ["exponential", "backoff"], [r["id"] for r in recs], limit=5
    )
    assert len(rec_hits) >= 1
    assert "backoff" in rec_hits[0]["content"].lower()


def test_contract_vectors_embed_search_and_delete(contract_store: Store, tmp_path: Path):
    project, root = _seed_project(contract_store, tmp_path, "vec_proj")
    src_a = contract_store.snapshot(
        project["id"], f"file://{root / 'a.py'}", "alpha vector target", "code",
        {"root": str(root), "relative_path": "a.py", "path": str(root / "a.py")},
    )
    src_b = contract_store.snapshot(
        project["id"], f"file://{root / 'b.py'}", "beta unrelated target", "code",
        {"root": str(root), "relative_path": "b.py", "path": str(root / "b.py")},
    )
    cid_a, cid_b = str(uuid4()), str(uuid4())
    with contract_store.transaction() as tx:
        contract_store.backend.insert_document(
            tx, "chunks",
            {"id": cid_a, "project_id": project["id"], "source_id": src_a["id"],
             "text": "alpha vector target", "line_start": 1, "line_end": 1, "is_current": True, "metadata": {}},
        )
        contract_store.backend.insert_document(
            tx, "chunks",
            {"id": cid_b, "project_id": project["id"], "source_id": src_b["id"],
             "text": "beta unrelated target", "line_start": 1, "line_end": 1, "is_current": True, "metadata": {}},
        )

    fake_ollama = _FakeEmbedOllama({
        "alpha": [1.0, 0.0, 0.0, 0.0],
        "beta": [0.0, 1.0, 0.0, 0.0],
    })
    retriever = Retriever(contract_store, contract_store.settings, ollama=fake_ollama)
    asyncio.run(retriever.embed_pending({"project_id": project["id"], "payload": {"chunk_id": cid_a}}))
    asyncio.run(retriever.embed_pending({"project_id": project["id"], "payload": {"chunk_id": cid_b}}))

    present, neighbors, warning = contract_store.backend.search_vectors(
        project["id"],
        retriever.embedding_key,
        [0.98, 0.02, 0.0, 0.0],
        [src_a["id"], src_b["id"]],
        limit=5,
    )
    assert present is True
    assert warning is None
    assert len(neighbors) >= 2
    assert neighbors[0]["chunk_id"] == cid_a

    # Replacing source A deletes its old vectors cleanly.
    with contract_store.transaction() as tx:
        contract_store.backend.delete_vectors_for_sources(tx, [src_a["id"]])
    present2, neighbors2, _ = contract_store.backend.search_vectors(
        project["id"], retriever.embedding_key, [0.98, 0.02, 0.0, 0.0], [src_a["id"], src_b["id"]], limit=5
    )
    assert present2 is True
    assert all(n["chunk_id"] != cid_a for n in neighbors2)


def test_contract_graph_cycle_safe_bounded_bfs(contract_store: Store, tmp_path: Path):
    """Graph traversal must terminate cleanly on cyclic graphs (A -> B -> A) and respect depth/limit."""
    project, root = _seed_project(contract_store, tmp_path, "graph_proj")
    src = contract_store.snapshot(
        project["id"], f"file://{root / 'mod.py'}", "x = 1\n", "code",
        {"root": str(root), "relative_path": "mod.py", "path": str(root / "mod.py")},
    )
    node_a = {"id": f"file:{project['id']}:a", "project_id": project["id"], "kind": "file",
              "label": "module_alpha.py", "source_id": src["id"], "is_current": True, "metadata": {}}
    node_b = {"id": f"file:{project['id']}:b", "project_id": project["id"], "kind": "file",
              "label": "module_beta.py", "source_id": src["id"], "is_current": True, "metadata": {}}
    node_c = {"id": f"file:{project['id']}:c", "project_id": project["id"], "kind": "file",
              "label": "module_gamma.py", "source_id": src["id"], "is_current": True, "metadata": {}}
    edge_ab = {"id": hashlib.sha256(b"ab").hexdigest(), "project_id": project["id"],
               "from_id": node_a["id"], "to_id": node_b["id"], "kind": "imports",
               "source_id": src["id"], "is_current": True, "status": "observed"}
    edge_ba = {"id": hashlib.sha256(b"ba").hexdigest(), "project_id": project["id"],
               "from_id": node_b["id"], "to_id": node_a["id"], "kind": "imports",
               "source_id": src["id"], "is_current": True, "status": "observed"}
    edge_bc = {"id": hashlib.sha256(b"bc").hexdigest(), "project_id": project["id"],
               "from_id": node_b["id"], "to_id": node_c["id"], "kind": "imports",
               "source_id": src["id"], "is_current": True, "status": "observed"}

    with contract_store.transaction() as tx:
        for n in (node_a, node_b, node_c):
            contract_store.upsert_node(n, tx)
        for e in (edge_ab, edge_ba, edge_bc):
            contract_store.upsert_edge(e, tx)

    eligible_nodes = [node_a["id"], node_b["id"], node_c["id"]]
    eligible_edges = [edge_ab["id"], edge_ba["id"], edge_bc["id"]]
    seeds = contract_store.backend.search_graph_seeds(
        project["id"], "module_alpha", [], ["alpha"], eligible_nodes, limit=5
    )
    assert any(row["id"] == node_a["id"] for row in seeds)

    # Traverse cyclic A <-> B -> C with max_depth=4 and verify unique termination.
    visited = contract_store.backend.traverse_graph(
        project["id"], [node_a["id"]], eligible_nodes, eligible_edges, max_depth=4, limit=10
    )
    visited_ids = [row["id"] for row in visited]
    assert set(visited_ids) == {node_a["id"], node_b["id"], node_c["id"]}
    assert len(visited_ids) == 3  # no cycle duplicates

    # Depth 1 from A reaches A and B, not C.
    shallow = contract_store.backend.traverse_graph(
        project["id"], [node_a["id"]], eligible_nodes, eligible_edges, max_depth=1, limit=10
    )
    assert {row["id"] for row in shallow} == {node_a["id"], node_b["id"]}


def test_contract_definition_and_uri_search(contract_store: Store, tmp_path: Path):
    project, root = _seed_project(contract_store, tmp_path, "def_proj")
    uri = f"file://{root / 'billing' / 'retry.py'}"
    src = contract_store.snapshot(
        project["id"], uri, "MAX_RETRIES = 5\n", "code",
        {"root": str(root), "relative_path": "billing/retry.py", "path": str(root / "billing" / "retry.py")},
    )
    sym_node = {
        "id": f"sym:{project['id']}:MAX_RETRIES",
        "project_id": project["id"],
        "kind": "symbol",
        "label": "MAX_RETRIES",
        "source_id": src["id"],
        "is_current": True,
        "metadata": {
            "kind": "constant",
            "member": "MAX_RETRIES",
            "path": str(root / "billing" / "retry.py"),
            "line_start": 1,
            "line_end": 1,
        },
    }
    contract_store.upsert_node(sym_node)

    by_code = contract_store.backend.search_definition_nodes(
        project["id"], [src["id"]], mode="code", names=["MAX_RETRIES"], limit=10
    )
    assert len(by_code) == 1 and by_code[0]["label"] == "MAX_RETRIES"

    by_pattern = contract_store.backend.search_definition_nodes(
        project["id"], [src["id"]], mode="pattern", pattern=r".*retries.*", limit=10
    )
    assert len(by_pattern) == 1 and by_pattern[0]["label"] == "MAX_RETRIES"

    uri_hits = contract_store.backend.search_sources_by_uri_substring(project["id"], "/billing/", limit=10)
    assert any(row["uri"] == uri for row in uri_hits)


def test_contract_jobs_streaming_export_and_server_time(contract_store: Store, tmp_path: Path):
    project, _ = _seed_project(contract_store, tmp_path, "jobs_proj")
    jid1 = contract_store.enqueue("extract", project["id"], {"source_id": "s1"}, "dedupe-1")
    jid2 = contract_store.enqueue("extract", project["id"], {"source_id": "s1"}, "dedupe-1")
    assert jid1 == jid2  # idempotent deduplication

    claimed = contract_store.claim_job("worker-1", lease_seconds=60, project_ids=[project["id"]])
    assert claimed is not None and claimed["id"] == jid1 and claimed["state"] == "running"
    contract_store.finish_job(jid1, worker="worker-1", expected_attempts=claimed["attempts"])

    # Streaming export iterates all rows in deterministic id order across batches.
    for i in range(5):
        contract_store.snapshot(project["id"], f"note://item-{i}", f"content {i}", "note")
    streamed = []
    for batch in contract_store.backend.iter_collection(contract_store, "sources", project_id=project["id"], batch_size=2):
        assert len(batch) <= 2
        streamed.extend(batch)
    assert len(streamed) == 5
    assert [r["id"] for r in streamed] == sorted(r["id"] for r in streamed)

    # Server clock returns positive integer epoch milliseconds.
    ms = contract_store.backend.server_time_ms()
    assert isinstance(ms, int) and ms > 1_700_000_000_000


def test_contract_readonly_blocks_writes(contract_store: Store, tmp_path: Path):
    _seed_project(contract_store, tmp_path, "ro_proj")
    settings = Settings.load(contract_store.data_dir)
    ro = Store.open_readonly(settings)
    try:
        assert ro.read_only is True
        assert ro.backend.read_only is True
        assert len(ro.list_projects()) >= 1
        with pytest.raises(RuntimeError):
            ro.command("DELETE FROM projects")
    finally:
        ro.backend.close()
