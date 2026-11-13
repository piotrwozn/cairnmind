"""Impact verdicts in storage: never searchable, carried by the project memory, fingerprinted from bytes."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from cairnmind.impact import build_impact
from cairnmind.ingest import _write_chunks, index_pending_sources
from cairnmind.inspector import Inspector
from cairnmind.inspector_api import attach_inspector_routes
from cairnmind.memory import MemoryManager
from cairnmind.retrieval import Retriever
from cairnmind.store import Store

from test_impact_verdicts import RENEWED, changed as changed, entry, give, verdicts, workspace as workspace


def chunks_of(store, source_id):
    return store.query("SELECT id FROM chunks WHERE source_id=:source", {"source": source_id})


def jobs(store):
    return len(store.query("SELECT id FROM jobs"))


def test_a_verdict_is_never_chunked_or_found_while_a_review_note_still_is(changed):
    store, pid, root, records = changed
    index_pending_sources(store, limit=1000)  # what was pending before the verdict
    before = jobs(store)
    saved = give(store, pid, records["rule"], "needs_update", note="Mention the zanzibar renewal window.")
    assert index_pending_sources(store, limit=1000) == {"sources": 0, "chunks": 0, "errors": []}
    assert _write_chunks(store, pid, saved["audit_source_id"], "zanzibar") == 0
    assert chunks_of(store, saved["audit_source_id"]) == [] and jobs(store) == before
    assert Retriever(store, store.settings).search(pid, "zanzibar") == []
    # A review note, the same address scheme and origin, is cited by its record and indexed as before.
    rule = records["rule"]
    review = Inspector(store).review(pid, rule["id"], "confirm", rule["version"], reason="Checked by quokka.")
    assert index_pending_sources(store, limit=1000)["sources"] == 1
    assert chunks_of(store, review["audit_source_id"]) and jobs(store) > before
    assert [hit["source_id"] for hit in Retriever(store, store.settings).search(pid, "quokka")] == [
        review["audit_source_id"]]


def test_a_verdict_travels_with_the_project_memory_and_is_not_chunked_there(changed, tmp_path):
    store, pid, root, records = changed
    manager = MemoryManager(store)
    first = manager.snapshot(pid, worktree=str(root))
    saved = give(store, pid, records["rule"], "not_related", note="Only the name matches.")
    published = manager.snapshot(pid, worktree=str(root))
    assert first["status"] == "created" and published["status"] == "created"  # the verdict is a local change
    assert manager.snapshot(pid, worktree=str(root))["status"] == "unchanged"
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(root), str(clone)], check=True, capture_output=True)
    (clone / "src" / "auth.py").write_text(RENEWED)
    target = Store(tmp_path / "target-database")
    imported = MemoryManager(target).import_snapshot(Path(published["manifest_path"]), clone)
    assert imported["status"] == "imported"
    source_id = saved["audit_source_id"]
    assert target.get_source(source_id)["content"] == store.get_source(source_id)["content"]
    assert chunks_of(target, source_id) == []
    assert index_pending_sources(target, limit=1000)["errors"] == [] and chunks_of(target, source_id) == []
    report = build_impact(target, pid, "HEAD")
    there = entry(report, records["rule"])
    assert there["verdict"]["verdict"] == "not_related" and there["verdict"]["note"] == "Only the name matches."
    assert "  checked: not related (" in report["text"]


def big(path, step):
    path.write_text("SESSION_MINUTES = 30\n" + "#" * (2 * 1024 * 1024 + 10) + "x" * step + "\n")


def with_nul(path, step):
    path.write_bytes(RENEWED.encode() + b"\0" * (step + 1))


def not_utf8(path, step):
    path.write_bytes(RENEWED.encode() + bytes([0xff - step]))


def symlink(path, step):
    path.unlink()
    path.symlink_to(["billing.py", "missing-target.py"][step])


@pytest.mark.parametrize("write", [big, with_nul, not_utf8, symlink])
def test_a_file_that_cannot_be_read_as_text_still_changes_the_fingerprint(changed, write):
    store, pid, root, records = changed
    auth = root / "src" / "auth.py"
    write(auth, 0)
    first = build_impact(store, pid, "HEAD")
    write(auth, 1)
    second = build_impact(store, pid, "HEAD")
    for name in ("rule", "task"):  # a definition link and a file link
        assert entry(first, records[name])["fingerprint"] != entry(second, records[name])["fingerprint"], name
    auth.unlink()
    gone = build_impact(store, pid, "HEAD")
    assert entry(gone, records["task"])["fingerprint"] != entry(second, records["task"])["fingerprint"]


def test_a_link_out_of_the_worktree_is_not_followed(changed, tmp_path):
    store, pid, root, records = changed
    outside = tmp_path / "outside.py"
    outside.write_text("def refresh_token(token):\n    return 1\n")
    auth = root / "src" / "auth.py"
    auth.unlink()
    auth.symlink_to(outside)
    first = entry(build_impact(store, pid, "HEAD"), records["task"])["fingerprint"]
    outside.write_text("def refresh_token(token):\n    return 2\n")
    assert entry(build_impact(store, pid, "HEAD"), records["task"])["fingerprint"] == first
    auth.unlink()
    auth.symlink_to(os.path.relpath(outside, auth.parent))
    assert entry(build_impact(store, pid, "HEAD"), records["task"])["fingerprint"] != first


def test_the_route_answers_409_only_for_a_stale_entry(changed):
    store, pid, root, records = changed
    service = FastAPI()
    attach_inspector_routes(service, store, store.settings)
    client = TestClient(service)
    shown = entry(build_impact(store, pid, "HEAD"), records["rule"])
    body = {"project": pid, "since": "HEAD", "record_id": shown["id"], "fingerprint": shown["fingerprint"],
            "verdict": "still_valid"}

    def post(payload):
        return client.post("/inspector/impact/verdict", json=payload,
                           headers={"Authorization": "Bearer " + store.settings.token})

    unknown = post({**body, "since": "conflict-branch-that-does-not-exist"})
    assert unknown.status_code == 400 and "conflict-branch" in unknown.json()["detail"]
    assert post({**body, "fingerprint": "0" * 64}).status_code == 409
    assert verdicts(store, pid) == []
