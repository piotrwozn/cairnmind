"""Native lifecycle/state-refresh regressions on real ArcadeDB and Git.

No coding client or model is invoked, and packet delivery never supplies an ACK.
"""
from __future__ import annotations

import hashlib
import json
import subprocess

import pytest

from cairnmind.ingest import import_history
from cairnmind.sessions import SessionManager
from cairnmind.store import Store
from cairnmind.sql import json_text


NATIVE_ID = "11111111-1111-4111-8111-111111111111"


def _line(event):
    return (json.dumps(event) + "\n").encode()


def _message(text):
    return _line({"type": "event_msg", "payload": {"type": "user_message", "message": text}})


def _history(path, root):
    raw = _line({"type": "session_meta", "payload": {"id": NATIVE_ID, "cwd": str(root)}})
    raw += _message("Keep every historical instruction")
    path.write_bytes(raw)
    return raw


def _append(path, raw):
    with path.open("ab") as handle:
        handle.write(raw)


@pytest.fixture
def lifecycle(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    for args in (("init", "-b", "main"), ("config", "user.email", "fixture@example.invalid"),
                 ("config", "user.name", "Fixture")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (root / "app.py").write_text("VALUE = 1\n")
    for args in (("add", "app.py"), ("commit", "-m", "initial")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    store = Store(tmp_path / "memory")
    project = store.add_project("lifecycle", root)
    return store, project, root, SessionManager(store, store.settings)


def _event(result, root, *, reason="startup", name="SessionStart", history=None, eid="start"):
    payload = {"devbrain_session_id": result["session_id"], "session_id": NATIVE_ID,
               "hook_event_name": name, "resume_reason": reason, "cwd": str(root), "event_id": eid}
    if history is not None:
        stat = history.stat()
        head = history.read_bytes()[:4096]
        payload.update(transcript_path=str(history), transcript_watermark={
            "size": stat.st_size, "device": stat.st_dev, "inode": stat.st_ino,
            "mtime_ns": stat.st_mtime_ns, "head_sha256": hashlib.sha256(head).hexdigest(),
            "head_bytes": len(head)})
    return payload


def _ack(manager, result):
    packet = result["packet"]
    readback = {key: ({"id": packet[field]["id"], "version": packet[field]["version"]}
                      if packet.get(field) else None)
                for key, field in (("plan", "active_plan"), ("task", "active_task"))}
    return manager.ack(result["session_id"], packet["id"], packet["hash"], readback)


def _control_state(store, session_id):
    return {"sessions": store.query("SELECT FROM sessions WHERE id=:id", {"id": session_id}),
            "packets": store.query("SELECT FROM resume_packets WHERE session_id=:id ORDER BY id", {"id": session_id}),
            "acks": store.query("SELECT FROM resume_acks WHERE session_id=:id ORDER BY id", {"id": session_id})}


def test_native_startup_live_rollout_does_not_reblock_code_state_refresh(lifecycle, tmp_path):
    store, project, root, manager = lifecycle
    initial = manager.prepare(project["id"], "codex", timeout=0)
    assert initial["status"] == "ready" and initial["packet"]["no_history"]
    history = tmp_path / "live.jsonl"
    _history(history, root)
    manager.note_event(_event(initial, root, history=history), "codex")
    _ack(manager, initial)
    import_history(store, history, project["id"], "codex")
    _append(history, _message("Current turn arrived after launcher preparation"))
    _append(history, b'{"type":"event_msg","payload":')
    cursor = store.query("SELECT FROM import_cursors WHERE path=:path", {"path": str(history)})[0]
    assert cursor["offset"] < history.stat().st_size
    (root / "app.py").write_text("VALUE = 2\n")
    with pytest.raises(ValueError, match="resume_required"):
        manager.require_ack(initial["session_id"])
    refreshed = manager.resume_context(initial["session_id"])
    assert refreshed["status"] == "ready", refreshed["unknowns"]
    assert refreshed["epoch"] == initial["epoch"] + 1
    assert refreshed["packet"]["code"]["fingerprint"] != initial["packet"]["code"]["fingerprint"]
    assert refreshed["packet"]["no_history"]
    assert not manager.status(initial["session_id"])["acknowledged"]
    with pytest.raises(ValueError, match="resume_required"):
        _ack(manager, initial)
    with pytest.raises(ValueError, match="resume_required"):
        manager.require_ack(initial["session_id"])
    for _ in range(2):
        _append(history, b" ")
        assert manager.resume_context(initial["session_id"])["packet"] == refreshed["packet"]
        assert manager.exact_context(initial["session_id"], refreshed["packet"]["id"])["packet"] == refreshed["packet"]
    assert len(store.query("SELECT FROM resume_acks")) == 1
    _ack(manager, refreshed)
    manager.require_ack(initial["session_id"], refreshed["packet"]["id"])


@pytest.mark.parametrize("failure", [None, "wrong_packet", "stale_code"])
def test_exact_context_is_read_only_even_on_mismatch_or_changed_code(lifecycle, failure):
    store, project, root, manager = lifecycle
    result = manager.prepare(project["id"], "codex", timeout=0)
    manager.note_event(_event(result, root), "codex")
    packet_id = result["packet"]["id"]
    if failure == "wrong_packet":
        packet_id = "not-the-current-packet"
    elif failure == "stale_code":
        (root / "app.py").write_text("VALUE = 2\n")
    before = _control_state(store, result["session_id"])
    if failure:
        with pytest.raises(ValueError, match="resume_required"):
            manager.exact_context(result["session_id"], packet_id)
    else:
        assert manager.exact_context(result["session_id"], packet_id) == result
    assert _control_state(store, result["session_id"]) == before
    assert not store.query("SELECT FROM resume_acks")


@pytest.mark.parametrize("reason", ["clear", "fork", "resume"])
def test_real_native_lifecycle_invalidates_ack_after_state_refresh(lifecycle, reason):
    store, project, root, manager = lifecycle
    initial = manager.prepare(project["id"], "codex", timeout=0)
    manager.note_event(_event(initial, root), "codex")
    _ack(manager, initial)
    (root / "app.py").write_text("VALUE = 2\n")
    refreshed = manager.resume_context(initial["session_id"])
    assert refreshed["epoch"] == initial["epoch"] + 1
    _ack(manager, refreshed)
    payload = _event(refreshed, root, reason=reason, eid=f"real-{reason}")
    manager.note_event(payload, "codex")
    manager.note_event(payload, "codex")
    invalidated = manager.status(initial["session_id"])
    assert invalidated["epoch"] == refreshed["epoch"] + 1
    assert not invalidated["acknowledged"]
    with pytest.raises(ValueError, match="resume_required"):
        manager.require_ack(initial["session_id"])
    renewed = manager.resume_context(initial["session_id"])
    assert renewed["status"] == "ready"
    assert renewed["epoch"] == invalidated["epoch"]
    assert renewed["packet"]["id"] != refreshed["packet"]["id"]
    assert len(store.query("SELECT FROM resume_acks")) == 2
    _ack(manager, renewed)
    manager.require_ack(initial["session_id"])


def test_compaction_after_refresh_invalidates_exactly_once(lifecycle):
    store, project, root, manager = lifecycle
    initial = manager.prepare(project["id"], "codex", timeout=0)
    manager.note_event(_event(initial, root), "codex")
    _ack(manager, initial)
    (root / "app.py").write_text("VALUE = 2\n")
    refreshed = manager.resume_context(initial["session_id"])
    _ack(manager, refreshed)
    compact = _event(refreshed, root, name="PreCompact", reason="compact", eid="compact")
    manager.note_event(compact, "codex")
    manager.note_event(compact, "codex")
    manager.note_event(_event(refreshed, root, reason="compact", eid="compact-finished"), "codex")
    result = manager.resume_context(initial["session_id"])
    assert result["epoch"] == refreshed["epoch"] + 1
    assert result["status"] == "ready"
    assert not manager.status(initial["session_id"])["acknowledged"]
    assert len(store.query("SELECT FROM resume_acks")) == 2


def test_captured_partial_record_must_finish_but_later_appends_are_not_chased(lifecycle, tmp_path):
    store, project, root, manager = lifecycle
    history = tmp_path / "historical.jsonl"
    first = _history(history, root)
    import_history(store, history, project["id"], "codex")
    second = _message("This originally observed partial instruction must be archived")
    split = len(second) // 2
    _append(history, second[:split])
    initial = manager.prepare(project["id"], "codex", timeout=0)
    assert initial["status"] == "partial"
    before = _control_state(store, initial["session_id"])
    with pytest.raises(ValueError, match="resume_required"):
        manager.exact_context(initial["session_id"], initial["packet"]["id"])
    assert _control_state(store, initial["session_id"]) == before
    _append(history, second[split:])
    unknown_later = _line({"type": "future_unknown_schema", "payload": {"retained": True}})
    _append(history, unknown_later)
    _append(history, b'{"future_incomplete":')
    lagged = manager.resume_context(initial["session_id"])
    assert lagged["status"] == "partial"
    assert lagged["epoch"] == initial["epoch"]
    import_history(store, history, project["id"], "codex")
    result = manager.resume_context(initial["session_id"])
    assert result["status"] == "ready", result["unknowns"]
    assert result["epoch"] == initial["epoch"]
    horizons = result["packet"]["archive_watermarks"]
    assert len(horizons) == 1
    assert horizons[0]["size"] == len(first + second)
    assert horizons[0]["sha256"] == hashlib.sha256(first + second).hexdigest()
    unknowns = store.query("SELECT FROM sources WHERE kind='transcript_unknown'")
    assert len(unknowns) == 1 and unknowns[0]["content"] == unknown_later.decode()
    assert not store.query("SELECT FROM resume_acks")
    _ack(manager, result)
    _append(history, b" ")
    assert manager.resume_context(initial["session_id"])["packet"] == result["packet"]
    manager.require_ack(initial["session_id"])


def test_known_historical_unknown_is_never_dropped_from_retry(lifecycle, tmp_path):
    store, project, root, manager = lifecycle
    history = tmp_path / "unknown-history.jsonl"
    _history(history, root)
    _append(history, _line({"unrecognized_history": True}))
    import_history(store, history, project["id"], "codex")
    initial = manager.prepare(project["id"], "codex", timeout=0)
    assert initial["status"] == "partial"
    sources = store.query("SELECT FROM sources WHERE kind='transcript_unknown'")
    assert len(sources) == 1
    _append(history, _message("A later valid message cannot erase unknown history"))
    import_history(store, history, project["id"], "codex")
    retried = manager.resume_context(initial["session_id"])
    assert retried["status"] == "partial"
    assert any("Unparsed or conflicting" in text for text in retried["unknowns"])
    assert store.get_source(sources[0]["id"])["kind"] == "transcript_unknown"
    assert not store.query("SELECT FROM resume_acks")


def test_stop_horizon_is_deferred_until_next_real_resume(lifecycle, tmp_path):
    store, project, root, manager = lifecycle
    initial = manager.prepare(project["id"], "codex", timeout=0)
    history = tmp_path / "live-stop.jsonl"
    _history(history, root)
    manager.note_event(_event(initial, root, history=history), "codex")
    _ack(manager, initial)
    _append(history, _line({"unknown_completed_turn": True}))
    import_history(store, history, project["id"], "codex")
    manager.note_event(_event(initial, root, name="Stop", history=history, eid="stop"), "codex")
    assert manager.resume_context(initial["session_id"])["packet"] == initial["packet"]
    manager.require_ack(initial["session_id"])
    manager.note_event(_event(initial, root, reason="resume", history=history, eid="real-resume"), "codex")
    result = manager.resume_context(initial["session_id"])
    assert result["epoch"] == initial["epoch"] + 1
    assert result["status"] == "partial"
    assert any("Unparsed or conflicting" in text for text in result["unknowns"])
    assert not manager.status(initial["session_id"])["acknowledged"]
    assert len(store.query("SELECT FROM resume_acks")) == 1


@pytest.mark.parametrize("damage", ["replace_generation", "rewrite_prefix"])
def test_unproved_captured_generation_changes_fail_closed(lifecycle, tmp_path, damage):
    store, project, root, manager = lifecycle
    history = tmp_path / "generation.jsonl"
    first = _history(history, root)
    import_history(store, history, project["id"], "codex")
    second = _message("The scanner has not archived this captured record")
    _append(history, second)
    initial = manager.prepare(project["id"], "codex", timeout=0)
    assert initial["status"] == "partial"
    if damage == "replace_generation":
        history.rename(tmp_path / "previous-generation.jsonl")
        history.write_bytes(first + second)
    else:
        history.write_bytes((first + second).replace(b"Keep every historical", b"Keep newer historical", 1))
    import_history(store, history, project["id"], "codex")
    retried = manager.resume_context(initial["session_id"])
    assert retried["status"] == "partial"
    assert any("rotated" in text or "prefix changed" in text for text in retried["unknowns"])
    assert not store.query("SELECT FROM resume_acks")


@pytest.mark.parametrize("boundary", [None, "clear", "compact", "omitted_original_horizons"])
def test_legacy_live_horizon_migration_requires_original_action_proof(lifecycle, tmp_path, boundary):
    """Simulate 0.3.4's latest partial packet, not a clean new-version launch."""
    store, project, root, manager = lifecycle
    original = manager.prepare(project["id"], "codex", timeout=0)
    history = tmp_path / "legacy-live.jsonl"
    _history(history, root)
    manager.note_event(_event(original, root, history=history), "codex")
    _ack(manager, original)
    import_history(store, history, project["id"], "codex")
    _append(history, _message("Live record beyond the scanner's durable cursor"))
    # Force the old release's recapture behavior once. The upgrade then sees
    # only durable legacy packets/events and no new resume_targets field.
    store.command("UPDATE sessions SET resume_targets=null WHERE id=:id", {"id": original["session_id"]})
    (root / "app.py").write_text("VALUE = 2\n")
    legacy = manager.resume_context(original["session_id"])
    assert legacy["status"] == "partial" and legacy["epoch"] == original["epoch"] + 1
    assert any("Import cursor" in text for text in legacy["unknowns"])
    if boundary == "clear":
        manager.note_event(_event(legacy, root, reason="clear", history=history, eid="legacy-clear"), "codex")
        legacy = manager.resume_context(original["session_id"])
    elif boundary == "compact":
        manager.note_event(_event(legacy, root, reason="compact", name="PreCompact", eid="legacy-compact"), "codex")
        manager.note_event(_event(legacy, root, reason="compact", history=history, eid="legacy-after-compact"), "codex")
        legacy = manager.resume_context(original["session_id"])
    elif boundary == "omitted_original_horizons":
        # A truncated old proof is not evidence that an empty history scope
        # was intended, even if its legacy status says ready.
        packet = {**original["packet"], "omitted_archive_watermarks": 1}
        packet["hash"] = hashlib.sha256(json_text({key: value for key, value in packet.items() if key != "hash"}).encode()).hexdigest()
        store.command("UPDATE resume_packets SET payload=:payload,hash=:hash WHERE id=:id",
                      {"payload": packet, "hash": packet["hash"], "id": packet["id"]})
    store.command("UPDATE sessions REMOVE resume_targets WHERE id=:id", {"id": original["session_id"]})
    assert "resume_targets" not in store.query("SELECT FROM sessions WHERE id=:id", {"id": original["session_id"]})[0]
    recovered = manager.resume_context(original["session_id"])
    assert recovered["epoch"] == legacy["epoch"]
    assert recovered["packet"]["code"]["fingerprint"] == legacy["packet"]["code"]["fingerprint"]
    if boundary is None:
        assert recovered["status"] == "ready", recovered["unknowns"]
        assert recovered["packet"]["no_history"]
    else:
        assert recovered["status"] == "partial"
        assert recovered["packet"]["archive_watermarks"]
        assert any("Import cursor" in text for text in recovered["unknowns"])
    assert not manager.status(original["session_id"])["acknowledged"]
    assert len(store.query("SELECT FROM resume_acks")) == 1


def test_archive_timeout_preserves_required_unknown_history_for_retry(lifecycle, tmp_path, monkeypatch):
    store, project, root, manager = lifecycle
    history = tmp_path / "archive-timeout.jsonl"
    _history(history, root)
    _append(history, _line({"unknown_required_record": True}))
    import_history(store, history, project["id"], "codex")
    calls = []

    def interrupted_archive(session, target, deadline):
        calls.append(target["path"])
        raise TimeoutError("Controlled archive deadline interruption")

    with monkeypatch.context() as patch:
        patch.setattr(manager, "_archive", interrupted_archive)
        interrupted = manager.prepare(project["id"], "codex", timeout=0)
    assert calls == [str(history)]
    assert interrupted["status"] == "partial"
    session = store.query("SELECT FROM sessions WHERE id=:id", {"id": interrupted["session_id"]})[0]
    assert [target["path"] for target in session["resume_targets"]] == [str(history)]
    retried = manager.resume_context(interrupted["session_id"])
    assert retried["status"] == "partial"
    assert retried["packet"]["no_history"] is False
    assert any("Unparsed or conflicting" in text for text in retried["unknowns"])
    assert [target["path"] for target in retried["packet"]["archive_watermarks"]] == [str(history)]
    assert not store.query("SELECT FROM resume_acks")


@pytest.mark.parametrize("legacy_without_scope", [False, True])
def test_capture_timeout_does_not_freeze_an_empty_history_scope(lifecycle, tmp_path, monkeypatch, legacy_without_scope):
    store, project, root, manager = lifecycle
    history = tmp_path / "capture-timeout.jsonl"
    _history(history, root)
    _append(history, _line({"unknown_required_record": True}))
    import_history(store, history, project["id"], "codex")
    calls = []

    def interrupted_capture(session, deadline):
        calls.append(session["id"])
        raise TimeoutError("Controlled capture deadline interruption")

    with monkeypatch.context() as patch:
        patch.setattr(manager, "_targets", interrupted_capture)
        interrupted = manager.prepare(project["id"], "codex", timeout=0)
    assert calls == [interrupted["session_id"]]
    assert interrupted["status"] == "partial"
    session = store.query("SELECT FROM sessions WHERE id=:id", {"id": interrupted["session_id"]})[0]
    assert session.get("resume_targets") is None
    if legacy_without_scope:
        # 0.3.4 had no resume_targets field. Its empty partial packet may
        # represent interrupted capture rather than a proven empty scope.
        store.command("UPDATE sessions REMOVE resume_targets WHERE id=:id", {"id": interrupted["session_id"]})
    retried = manager.resume_context(interrupted["session_id"])
    assert retried["status"] == "partial"
    assert retried["packet"]["no_history"] is False
    assert any("Unparsed or conflicting" in text for text in retried["unknowns"])
    assert [target["path"] for target in retried["packet"]["archive_watermarks"]] == [str(history)]
    assert not store.query("SELECT FROM resume_acks")


def test_clear_consumes_old_precompact_flag_before_a_later_compact(lifecycle):
    store, project, root, manager = lifecycle
    initial = manager.prepare(project["id"], "codex", timeout=0)
    manager.note_event(_event(initial, root), "codex")
    _ack(manager, initial)
    manager.note_event(_event(initial, root, name="PreCompact", reason="compact", eid="precompact-before-clear"), "codex")
    manager.note_event(_event(initial, root, reason="clear", eid="clear-after-precompact"), "codex")
    cleared = manager.resume_context(initial["session_id"])
    assert cleared["status"] == "ready"
    _ack(manager, cleared)
    manager.require_ack(initial["session_id"])
    manager.note_event(_event(cleared, root, reason="compact", eid="later-compact-without-precompact"), "codex")
    invalidated = manager.status(initial["session_id"])
    assert invalidated["epoch"] == cleared["epoch"] + 1
    assert not invalidated["acknowledged"]
    with pytest.raises(ValueError, match="resume_required"):
        manager.require_ack(initial["session_id"])
    renewed = manager.resume_context(initial["session_id"])
    assert renewed["status"] == "ready" and renewed["epoch"] == invalidated["epoch"]
    assert len(store.query("SELECT FROM resume_acks")) == 2
    with pytest.raises(ValueError, match="resume_required"):
        manager.require_ack(initial["session_id"])


def test_new_action_cannot_merge_away_an_overwritten_captured_partial_tail(lifecycle, tmp_path):
    store, project, root, manager = lifecycle
    history = tmp_path / "changed-partial-tail.jsonl"
    first = _history(history, root)
    import_history(store, history, project["id"], "codex")
    second = _message("Keep ORIGINAL pending instruction until it is archived")
    split = second.index(b"ORIGINAL") + len(b"ORIGINAL")
    original_tail = second[:split]
    _append(history, original_tail)
    initial = manager.prepare(project["id"], "codex", timeout=0)
    assert initial["status"] == "partial"
    manager.note_event(_event(initial, root, history=history), "codex")
    original_inode = history.stat().st_ino
    replacement_tail = original_tail.replace(b"ORIGINAL", b"REPLACED")
    assert len(replacement_tail) == len(original_tail) and replacement_tail != original_tail
    history.write_bytes(first + replacement_tail + second[split:] + _message("Later complete known record"))
    assert history.stat().st_ino == original_inode
    import_history(store, history, project["id"], "codex")
    assert not store.query("SELECT FROM sources WHERE kind='transcript_unknown'")
    manager.note_event(_event(initial, root, reason="clear", history=history, eid="clear-after-tail-rewrite"), "codex")
    renewed = manager.resume_context(initial["session_id"])
    assert renewed["epoch"] == initial["epoch"] + 1
    assert renewed["status"] == "partial"
    assert any("tail changed" in text for text in renewed["unknowns"])
    assert not manager.status(initial["session_id"])["acknowledged"]
    assert not store.query("SELECT FROM resume_acks")


def test_shorter_rewritten_generation_keeps_its_own_unproved_horizon(lifecycle, tmp_path):
    store, project, root, manager = lifecycle
    history = tmp_path / "same-prefix-truncated.jsonl"
    first = _line({"type": "event_msg", "cwd": str(root),
                   "payload": {"type": "user_message", "message": "Preserved prefix " + "p" * 6000}})
    old = first + _message("Original long trailing record " + "o" * 3000)
    assert len(first) > 4096
    history.write_bytes(old)
    import_history(store, history, project["id"], "codex")
    proved = manager.prepare(project["id"], "codex", timeout=0)
    assert proved["status"] == "ready", proved["unknowns"]
    assert proved["packet"]["archive_watermarks"][0]["size"] == len(old)
    old_inode = history.stat().st_ino
    rewritten = first + _message("A different short trailing record not imported")
    history.write_bytes(rewritten)
    assert history.stat().st_ino == old_inode
    assert rewritten[:4096] == old[:4096] and len(rewritten) < len(old)
    cursor = store.query("SELECT FROM import_cursors WHERE path=:path", {"path": str(history)})[0]
    assert cursor["offset"] == len(old) > len(rewritten)
    # A different logical action must account for the newly observed shorter
    # generation even though the old larger generation has a durable proof.
    result = manager.prepare(project["id"], "codex", timeout=0)
    assert result["session_id"] != proved["session_id"]
    assert result["status"] == "partial"
    horizons = result["packet"]["archive_watermarks"]
    assert {target["size"] for target in horizons} == {len(old), len(rewritten)}
    assert any(target["size"] == len(old) and target["status"] == "complete" for target in horizons)
    assert any(target["size"] == len(rewritten) and target["status"] == "partial" for target in horizons)
    assert any("Missing durable source" in text for text in result["unknowns"])
    assert not store.query("SELECT FROM resume_acks")
