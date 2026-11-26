"""Fast-tier edge-case tests for sessions.py target merging and memory_published_import.py."""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from cairnmind import memory_published_import as mpi
from cairnmind.memory import MemoryConflict
from cairnmind.sessions import SessionManager


def test_sessions_same_generation_and_merge_targets_edges(tmp_path):
    f = tmp_path / "log.jsonl"
    content = b"line1\nline2\n"
    f.write_bytes(content)
    st = f.stat()
    dev, ino = st.st_dev, st.st_ino

    base = {"path": str(f), "device": dev, "inode": ino, "size": 6, "observed_size": 6,
            "head_bytes": 6, "head_sha256": hashlib.sha256(content[:6]).hexdigest()}

    # Missing file on size difference -> False
    assert SessionManager._same_generation(
        {**base, "path": str(tmp_path / "missing.jsonl")},
        {**base, "path": str(tmp_path / "missing.jsonl"), "size": 12},
    ) is False

    # Pending tail checks: invalid bounds, wrong hash, OSError
    assert SessionManager._same_generation(
        {**base, "pending_tail_sha256": "a" * 64, "captured_tail_start": 10, "observed_size": 5},
        dict(base),
    ) is False
    assert SessionManager._same_generation(
        {**base, "pending_tail_sha256": "0" * 64, "captured_tail_start": 6, "observed_size": 12},
        {**base, "observed_size": 12},
    ) is False

    # Different head_bytes: left_dev is None -> False; matching prefix bytes on disk -> True; missing file -> False
    longer_head = {**base, "size": 12, "observed_size": 12, "head_bytes": 12,
                   "head_sha256": hashlib.sha256(content).hexdigest()}
    assert SessionManager._same_generation({**base, "device": None}, {**longer_head, "device": None}) is False
    assert SessionManager._same_generation(base, longer_head) is True
    assert SessionManager._same_generation(
        {**base, "path": str(tmp_path / "gone")},
        {**longer_head, "path": str(tmp_path / "gone"), "size": 6, "observed_size": 6},
    ) is False

    # _merge_targets for agent_journal: conflicting same-seq kept separate; valid append merged preserving known_prefix
    jfile = tmp_path / "journal.jsonl"
    jbytes = b"event1\nevent2\n"
    jfile.write_bytes(jbytes)
    t1 = {
        "kind": "agent_journal", "agent_session_id": "s1", "path": str(jfile), "client": "api",
        "sequence": 1, "size": 7, "sha256": hashlib.sha256(jbytes[:7]).hexdigest(), "chain_hash": "h1",
        "proof": {"status": "complete"},
    }
    t1_conflict = {**t1, "sha256": "0" * 64}
    assert len(SessionManager._merge_targets([t1, t1_conflict])) == 2

    t2 = {
        "kind": "agent_journal", "agent_session_id": "s1", "path": str(jfile), "client": "api",
        "sequence": 2, "size": len(jbytes), "sha256": hashlib.sha256(jbytes).hexdigest(), "chain_hash": "h2",
    }
    merged = SessionManager._merge_targets([t1, t2])
    assert len(merged) == 1 and merged[0]["sequence"] == 2 and merged[0]["known_prefix"]["sequence"] == 1

    # Invalid size skipped; OSError skipped
    t_bad_size = {**t1, "size": -1}
    assert len(SessionManager._merge_targets([t_bad_size, t2])) == 2
    t_missing = {**t1, "path": str(tmp_path / "nope")}
    assert len(SessionManager._merge_targets([t_missing, {**t2, "path": str(tmp_path / "nope")}])) == 2


def test_memory_published_import_stubs_store_and_plans_edges(monkeypatch):
    imp = mpi.PublishedImporter(SimpleNamespace())

    # _placement missing plan -> {}
    assert imp._placement({}, "missing", "t1") == {}

    # _published_here catches Held and returns []
    from cairnmind.memory_published import Held
    monkeypatch.setattr("cairnmind.memory_published.Publisher.destination", lambda self, pid: (None, "/root"))
    monkeypatch.setattr("cairnmind.memory_published.select_history", lambda *a, **k: (_ for _ in ()).throw(Held("held")))
    assert imp._published_here("p1", "fact", "r1", [], {}) == []

    # _stubs: conflict on mismatched published ref; quote growth updates document
    tree = {
        "records": {
            "r1": {"revisions": [{"quote": {"source": "s1", "lines": [1, 2], "text": "hello"}}]},
        },
        "sources": {
            "s1": {"ref": {"kind": "file", "path": "a.py"}, "passage": "hello"},
        },
    }
    monkeypatch.setattr(mpi, "stub_row", lambda pid, sid, src: {
        "id": sid, "project_id": pid, "kind": "published", "content": "hello",
        "metadata": {"published": src["ref"]},
    })
    monkeypatch.setattr(mpi, "one_row", lambda tx, sql, params, required=True: {
        "id": "s1", "kind": "published", "content": "different", "metadata": {"published": {"kind": "file", "path": "a.py"}},
    })
    with pytest.raises(MemoryConflict, match="published differently"):
        imp._stubs(SimpleNamespace(), tree, "p1", {"stubs": 0})

    updated_docs = []
    monkeypatch.setattr(mpi, "one_row", lambda tx, sql, params, required=True: {
        "id": "s1", "kind": "published", "content": "hello",
        "metadata": {"published": {"kind": "file", "path": "a.py"}, "published_quotes": []},
    })
    monkeypatch.setattr(mpi, "update_document", lambda tx, tbl, data, where, params: updated_docs.append((tbl, data)))
    imp._stubs(SimpleNamespace(), tree, "p1", {"stubs": 0})
    assert len(updated_docs) == 1 and updated_docs[0][0] == "sources"

    # _store active_key collision raises MemoryConflict
    monkeypatch.setattr(mpi, "one_row", lambda tx, sql, params, required=True: {"id": "other-rec"})
    with pytest.raises(MemoryConflict, match="belong to the local record"):
        imp._store(
            SimpleNamespace(),
            {"id": "r1", "project_id": "p1", "kind": "fact", "key": "k1", "status": "confirmed", "version": 1},
            "facts/k1.md",
        )

    # _plans diverged raises MemoryConflict; matching skips
    plan_model = {
        "created_at": "2026-01-01T00:00:00Z",
        "definitions": [{"title": "New", "goal": "G2", "constraints": [], "steps": [["t1", "s1"]], "sources": ["s1"], "at": "2026-01-02"}],
    }
    plan_tree = {"plans": {"pl1": plan_model}, "paths": {"pl1": "plans/pl1.md"}, "records": {}}
    local_plan = {"id": "pl1", "version": 1, "title": "Local", "goal": "G1", "constraints": [], "step_ids": ["t1"],
                  "source_ids": ["s1"], "created_at": "2026-01-01T00:00:00Z"}
    fake_tx = SimpleNamespace(
        query=lambda sql, params: [{
            "snapshot": {
                "plan": {"title": "Local", "goal": "G1", "constraints": []},
                "tasks": [{"id": "t1", "metadata": {"step_key": "s1"}}],
            }
        }],
    )
    monkeypatch.setattr(mpi, "one_row", lambda tx, sql, params, required=True: local_plan)
    with pytest.raises(MemoryConflict, match="diverged from the published memory"):
        imp._plans(fake_tx, "p1", plan_tree, {}, {}, {"plans": []}, "2026-01-03")
