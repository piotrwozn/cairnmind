"""Recover only receipt-bound captures that never established a byte horizon."""
from __future__ import annotations

import copy
import json
import subprocess
import time
from uuid import uuid4

import pytest

from devbrain.ingest import import_history
from devbrain.integrations import _transcript_watermark
from devbrain.sessions import SessionManager
from devbrain.store import Store, _insert, _now, _set


NATIVE_ID = "70000000-0000-4000-8000-000000000007"


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("VALUE = 1\n")
    for args in (("init", "-b", "main"), ("add", "."),
                 ("-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-m", "initial")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    store = Store(tmp_path / "memory")
    project = store.add_project("capture-recovery", root)
    return store, project, root, SessionManager(store, store.settings)


def history_event(kind, root):
    value = {"mode": {"mode": "normal"}, "permission-mode": {"permissionMode": "default"},
             "ai-title": {"aiTitle": "Synthetic capture recovery"}}[kind]
    return {"type": kind, "sessionId": NATIVE_ID, "cwd": str(root), **value}


def seed_legacy(workspace, tmp_path, *, damage=None, measured=None, unknown=False, lag=False, legacy_marker=True):
    """Persist the two-target partial state produced by releases before recovery.

    The valid horizon is checked against real archived bytes. Only the missing
    startup reservation and its old partial packet are constructed explicitly;
    no receipt or successful ACK is invented by the recovery under test.
    """
    store, project, root, manager = workspace
    origin = manager.prepare(project["id"], "claude", timeout=0)
    assert origin["status"] == "ready"
    path = tmp_path / f"{NATIVE_ID}.jsonl"
    manager.note_event({"event_id": "initial-missing-start", "hook_event_name": "SessionStart",
                        "resume_reason": "startup", "cwd": str(root), "session_id": NATIVE_ID,
                        "devbrain_session_id": origin["session_id"], "transcript_path": str(path)}, "claude")
    stub = copy.deepcopy(manager._session(origin["session_id"])["transcript_targets"][0])
    assert "size" not in stub and stub.get("error") in {
        "Transcript EOF is unknown", "Transcript cannot be captured: FileNotFoundError"}
    # Explicitly retain the generic marker saved by 0.3.7, before the error
    # classification improvement in the current implementation.
    if legacy_marker:
        stub["error"] = "Transcript EOF is unknown"
    if measured:
        stub.update(measured)
    store.command("UPDATE sessions SET transcript_targets=:targets WHERE id=:id",
                  {"targets": [stub], "id": origin["session_id"]})

    first = json.dumps(history_event("mode", root)) + "\n"
    remaining = "".join(json.dumps(history_event(kind, root)) + "\n"
                         for kind in ("permission-mode", "ai-title"))
    if unknown:
        remaining += json.dumps({"type": "unrecognized-native-record", "sessionId": NATIVE_ID,
                                  "cwd": str(root)}) + "\n"
    path.write_text(first if lag else first + remaining)
    imported = import_history(store, path, project["id"], "claude")
    assert imported["unknown"] == (1 if unknown and not lag else 0)
    if lag:
        with path.open("a") as stream:
            stream.write(remaining)
    watermark = _transcript_watermark(path)
    later = {"event_id": "later-successful-stop", "hook_event_name": "SessionEnd", "cwd": str(root),
             "session_id": NATIVE_ID, "devbrain_session_id": origin["session_id"],
             "transcript_path": str(path), "transcript_watermark": watermark}
    manager.note_event(later, "claude")
    receipt = store.query("SELECT FROM session_events WHERE id=:id", {"id": later["event_id"]})[0]
    if damage == "no_original_binding":
        store.command("DELETE FROM session_events WHERE id=:id", {"id": "initial-missing-start"})
    elif damage == "no_later_capture":
        store.command("DELETE FROM session_events WHERE id=:id", {"id": later["event_id"]})
    elif damage == "wrong_native":
        store.command("UPDATE session_events SET native_session_id=:native WHERE id=:id",
                      {"native": str(uuid4()), "id": later["event_id"]})
    elif damage == "wrong_project":
        other = tmp_path / "other"
        other.mkdir()
        other_project = store.add_project("other", other)
        store.command("UPDATE session_events SET project_id=:project WHERE id=:id",
                      {"project": other_project["id"], "id": later["event_id"]})
    elif damage == "wrong_path":
        store.command("UPDATE session_events SET transcript_path=:path WHERE id=:id",
                      {"path": str(tmp_path / "unrelated.jsonl"), "id": later["event_id"]})
    elif damage == "wrong_client":
        store.command("UPDATE session_events SET client='codex' WHERE id=:id", {"id": later["event_id"]})
    elif damage == "invalid_capture":
        invalid = {**watermark, "size": -1}
        store.command("UPDATE session_events SET transcript_watermark=:value WHERE id=:id",
                      {"value": invalid, "id": later["event_id"]})

    sid, now = str(uuid4()), _now()
    session = {"id": sid, "project_id": project["id"], "client": "claude", "worktree": str(root),
               "native_session_id": None, "epoch": 1, "reason": "startup", "status": "partial",
               "packet_id": None, "ack_id": None, "pending_start": True,
               "transcript_targets": [], "resume_targets": [], "event_ids": [],
               "created_at": now, "updated_at": now}
    bounded = manager._target(str(path), "claude", watermark)
    archive = manager._archive(session, bounded, time.monotonic() + 30)
    if archive["status"] == "complete":
        bounded["proof"] = {key: archive[key] for key in
                            ("size", "status", "sha256", "archived_offset", "source_count", "source_ids_hash")}
    if damage == "missing_file":
        # This case deliberately lacks a previously established immutable proof.
        bounded.pop("proof", None)
        path.unlink()
    targets = [bounded, stub]
    session.update(transcript_targets=copy.deepcopy(targets), resume_targets=copy.deepcopy(targets))
    fault = {**stub, "status": "partial", "archived_offset": 0, "unknowns": [stub["error"]]}
    state = manager._state(session)
    packet = manager._build(session, state, [archive, fault], [f"{path}: {stub['error']}"])
    with store.transaction() as tx:
        _insert(tx, "sessions", session)
        _insert(tx, "resume_packets", {"id": packet["id"], "project_id": project["id"], "session_id": sid,
                                       "epoch": 1, "hash": packet["hash"], "payload": packet,
                                       "created_at": packet["created_at"]})
        _set(tx, "sessions", {"packet_id": packet["id"]}, "id=:id", {"id": sid})
    assert packet["status"] == "partial"
    return {"sid": sid, "origin_sid": origin["session_id"], "path": path, "project": project,
            "stub": stub, "bounded": bounded, "receipt": receipt, "packet": packet}


def assert_old_rows_unchanged(store, table, before):
    for row in before:
        assert store.query(f"SELECT FROM {table} WHERE id=:id", {"id": row["id"]}) == [row]


@pytest.mark.parametrize("legacy_marker", [True, False], ids=["legacy-marker", "classified-missing-file"])
def test_receipt_bound_uncaptured_reservation_recovers_existing_and_future_sessions(workspace, tmp_path, legacy_marker):
    store, project, _, manager = workspace
    old = seed_legacy(workspace, tmp_path, legacy_marker=legacy_marker)
    packets = store.query("SELECT FROM resume_packets ORDER BY id")
    events = store.query("SELECT FROM session_events ORDER BY id")
    sources = store.query("SELECT FROM sources ORDER BY id")
    cursor = store.query("SELECT FROM import_cursors WHERE path=:path", {"path": str(old["path"])})
    raw = old["path"].read_bytes()

    recovered = manager.resume_context(old["sid"])
    assert recovered["status"] == "ready", recovered["unknowns"]
    assert all(row["status"] == "complete" for row in recovered["packet"]["archive_watermarks"])
    audit = next(row["capture_recovery"] for row in recovered["packet"]["archive_watermarks"]
                 if row.get("capture_recovery"))
    assert audit["original_target"] == old["stub"]
    assert audit["origin_event"]["id"] == "initial-missing-start"
    assert audit["resolution_event"]["id"] == "later-successful-stop"
    assert audit["native_session_id"] == NATIVE_ID
    assert audit["project_id"] == project["id"]
    assert not store.query("SELECT id FROM resume_acks")
    with pytest.raises(ValueError, match="resume_required"):
        manager.require_ack(old["sid"])
    assert_old_rows_unchanged(store, "resume_packets", packets)
    assert_old_rows_unchanged(store, "session_events", events)
    assert_old_rows_unchanged(store, "sources", sources)
    assert store.query("SELECT FROM import_cursors WHERE path=:path", {"path": str(old["path"])}) == cursor
    assert old["path"].read_bytes() == raw

    # A Claude reservation must not poison the next consumer, including Codex.
    future = manager.prepare(project["id"], "codex", timeout=0)
    assert future["status"] == "ready", future["unknowns"]
    assert not store.query("SELECT id FROM resume_acks")
    assert all(row["status"] == "complete" for row in future["packet"]["archive_watermarks"])
    assert_old_rows_unchanged(store, "resume_packets", packets)
    assert_old_rows_unchanged(store, "session_events", events)


@pytest.mark.parametrize("damage", ["no_original_binding", "no_later_capture", "wrong_native", "wrong_project",
                                    "wrong_path", "wrong_client", "invalid_capture", "missing_file"])
def test_unproved_or_mismatched_capture_receipts_cannot_clear_an_unknown_horizon(workspace, tmp_path, damage):
    store, _, _, manager = workspace
    old = seed_legacy(workspace, tmp_path, damage=damage)
    result = manager.resume_context(old["sid"])
    assert result["status"] == "partial", result
    assert not store.query("SELECT id FROM resume_acks")


@pytest.mark.parametrize("measured", [
    {"size": 0},
    {"head_bytes": 0, "head_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"},
    {"pending_tail_bytes": 1, "pending_tail_sha256": "f" * 64},
    {"proof": {"size": 0, "status": "complete", "source_count": 0}},
])
def test_any_previously_observed_bounds_or_proof_are_never_treated_as_a_reservation(workspace, tmp_path, measured):
    store, _, _, manager = workspace
    old = seed_legacy(workspace, tmp_path, measured=measured)
    result = manager.resume_context(old["sid"])
    assert result["status"] == "partial", result
    assert not store.query("SELECT id FROM resume_acks")


def test_later_capture_cannot_hide_unknown_raw_records(workspace, tmp_path):
    store, _, _, manager = workspace
    old = seed_legacy(workspace, tmp_path, unknown=True)
    before = store.query("SELECT FROM sources WHERE kind='transcript_unknown'")
    assert len(before) == 1
    result = manager.resume_context(old["sid"])
    assert result["status"] == "partial"
    assert_old_rows_unchanged(store, "sources", before)
    assert not store.query("SELECT id FROM resume_acks")


def test_capture_resolution_waits_for_real_cursor_progress(workspace, tmp_path):
    store, project, _, manager = workspace
    old = seed_legacy(workspace, tmp_path, lag=True)
    cursor = store.query("SELECT FROM import_cursors WHERE path=:path", {"path": str(old["path"])})[0]
    assert cursor["offset"] < old["path"].stat().st_size
    blocked = manager.resume_context(old["sid"])
    assert blocked["status"] == "partial"
    assert not store.query("SELECT id FROM resume_acks")
    imported = import_history(store, old["path"], project["id"], "claude")
    assert imported["archived"] == 2 and imported["unknown"] == 0
    recovered = manager.resume_context(old["sid"])
    assert recovered["status"] == "ready", recovered["unknowns"]
    assert not store.query("SELECT id FROM resume_acks")


def test_raw_hash_mismatch_blocks_even_with_matching_native_receipts(workspace, tmp_path):
    store, _, _, manager = workspace
    old = seed_legacy(workspace, tmp_path)
    row = store.query("SELECT id FROM sources ORDER BY metadata.byte_offset LIMIT 1")[0]
    store.command("UPDATE sources SET sha256=:digest WHERE id=:id", {"digest": "f" * 64, "id": row["id"]})
    result = manager.resume_context(old["sid"])
    assert result["status"] == "partial"
    assert not store.query("SELECT id FROM resume_acks")


def test_a_known_permission_failure_is_not_treated_as_an_uncreated_transcript(workspace, tmp_path):
    store, _, _, manager = workspace
    old = seed_legacy(workspace, tmp_path)
    for sid in (old["origin_sid"], old["sid"]):
        session = manager._session(sid)
        for field in ("transcript_targets", "resume_targets"):
            targets = copy.deepcopy(session.get(field))
            if targets is None:
                continue
            for target in targets:
                if target.get("error"):
                    target["error"] = "Transcript cannot be captured: PermissionError"
            store.command(f"UPDATE sessions SET {field}=:targets WHERE id=:id", {"targets": targets, "id": sid})
    result = manager.resume_context(old["sid"])
    assert result["status"] == "partial"
    assert any("PermissionError" in reason for reason in result["unknowns"])
    assert not store.query("SELECT id FROM resume_acks")


@pytest.mark.parametrize("auxiliary,can_recover", [
    ({"status": "partial", "archived_offset": 0, "unknowns": ["Transcript EOF is unknown"]}, True),
    ({"status": "complete"}, False),
    ({"archived_offset": False}, False),
    ({"unknowns": ["Transcript EOF is unknown", "Unrelated archive warning"]}, False),
])
def test_reservation_auxiliary_fields_cannot_hide_another_archive_requirement(
        workspace, tmp_path, auxiliary, can_recover):
    store, _, _, manager = workspace
    old = seed_legacy(workspace, tmp_path, measured=auxiliary)
    result = manager.resume_context(old["sid"])
    assert result["status"] == ("ready" if can_recover else "partial"), result
    assert not store.query("SELECT id FROM resume_acks")
