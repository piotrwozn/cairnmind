"""Real archive compatibility and in-place recovery for Codex technical events."""
from __future__ import annotations

import hashlib
import json
import subprocess

import pytest

from cairnmind import ingest
from cairnmind.sessions import SessionManager
from cairnmind.store import Store


TECHNICAL_TYPES = {"world_state", "token_usage_record"}


def usage():
    return {"input_tokens": 17, "cached_input_tokens": 3, "cache_write_input_tokens": 2,
            "output_tokens": 4, "reasoning_output_tokens": 1, "total_tokens": 21}


def technical(kind, root, number=1):
    if kind == "world_state":
        payload = {"full": True, "state": {"cwd": str(root), "opaque": {"text": "technical snapshot"}}}
    else:
        payload = {"thread_id": "00000000-0000-4000-8000-000000000001",
                   "turn_id": f"turn-{number}",
                   "session_id": "00000000-0000-4000-8000-000000000002",
                   "root_turn_id": "root-turn", "response_id": f"resp-{number}",
                   "usage": usage(), "turn_token_usage": usage(), "thread_token_usage": usage()}
    return {"type": kind, "timestamp": "2026-11-05T00:00:00Z", "payload": payload}


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("VALUE = 1\n")
    for args in (("init", "-b", "main"), ("add", "."),
                 ("-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-m", "initial")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    store = Store(tmp_path / "memory")
    project = store.add_project("transcript-compatibility", root)
    return store, project, root, SessionManager(store, store.settings)


def source_rows(store, path):
    return store.query("SELECT FROM sources WHERE metadata.import_path=:path ORDER BY metadata.byte_offset",
                       {"path": str(path)})


def chunk_rows(store, source_ids):
    return store.query("SELECT FROM chunks WHERE source_id IN :ids ORDER BY id", {"ids": source_ids})


def cursor(store, path):
    return store.query("SELECT FROM import_cursors WHERE path=:path", {"path": str(path)})[0]


def seed_legacy(workspace, tmp_path, monkeypatch, extra_line=None):
    """Create exactly the durable records/cursor produced by the previous parser."""
    store, project, root, manager = workspace
    path = tmp_path / "rollout.jsonl"
    events = [{"type": "session_meta", "payload": {"id": "native-session", "cwd": str(root)}},
              technical("world_state", root),
              *[technical("token_usage_record", root, number) for number in range(1, 7)]]
    raw = "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)
    if extra_line is not None:
        raw += extra_line
    path.write_text(raw)
    current_parser = ingest._event_text

    def legacy_parser(event, client):
        if event.get("type") in TECHNICAL_TYPES:
            return "", "unknown", "codex", False
        return current_parser(event, client)

    with monkeypatch.context() as legacy:
        legacy.setattr(ingest, "_event_text", legacy_parser)
        # The old release had no reparsing entrypoint. Disable only that entrypoint
        # while establishing its persisted partial packet, never during recovery.
        if hasattr(ingest, "reparse_history_sources"):
            legacy.setattr(ingest, "reparse_history_sources", lambda *a, **k:
                           {"repaired": 0, "unchanged": 0, "errors": []})
        if hasattr(ingest, "_reparse_history_sources_locked"):
            legacy.setattr(ingest, "_reparse_history_sources_locked", lambda *a, **k:
                           {"repaired": 0, "unchanged": 0, "errors": []})
        imported = ingest.import_history(store, path, project["id"], "codex")
        for source in source_rows(store, path):
            metadata = dict(source["metadata"])
            metadata.pop("transcript_parser_revision", None)
            store.command("UPDATE sources SET metadata=:metadata WHERE id=:id",
                          {"metadata": metadata, "id": source["id"]})
        pending = manager.prepare(project["id"], "codex", timeout=0)
    assert imported["unknown"] >= 7
    assert cursor(store, path)["offset"] == path.stat().st_size
    assert pending["status"] == "partial"
    assert pending["packet"]["archive"]["status"] == "partial"
    return path, pending


@pytest.mark.parametrize("kind", sorted(TECHNICAL_TYPES))
def test_known_technical_records_are_metadata_not_user_instructions(tmp_path, kind):
    event = technical(kind, tmp_path)
    assert ingest._event_text(event, "auto") == ("", "metadata", "codex", True)
    assert ingest._event_text(event, "codex") == ("", "metadata", "codex", True)


@pytest.mark.parametrize("damage", ["payload_missing", "full_integer", "state_array", "world_required_missing",
                                    "record_required_missing", "null_thread_id", "invalid_session_uuid",
                                    "turn_id_number", "usage_missing_counter", "boolean_counter",
                                    "float_counter", "overflow_counter", "usage_array", "invalid_budget_units"])
def test_malformed_technical_records_are_not_promoted_to_metadata(tmp_path, damage):
    kind = "world_state" if damage in {"full_integer", "state_array", "world_required_missing"} else "token_usage_record"
    event = technical(kind, tmp_path)
    payload = event["payload"]
    if damage == "payload_missing":
        event.pop("payload")
    elif damage == "full_integer":
        payload["full"] = 1
    elif damage == "state_array":
        payload["state"] = []
    elif damage == "world_required_missing":
        payload.pop("state")
    elif damage == "record_required_missing":
        payload.pop("thread_token_usage")
    elif damage == "null_thread_id":
        payload["thread_id"] = None
    elif damage == "invalid_session_uuid":
        payload["session_id"] = "not-a-session-uuid"
    elif damage == "turn_id_number":
        payload["turn_id"] = 1
    elif damage == "usage_missing_counter":
        payload["usage"].pop("total_tokens")
    elif damage == "boolean_counter":
        payload["usage"]["input_tokens"] = True
    elif damage == "float_counter":
        payload["usage"]["input_tokens"] = 1.0
    elif damage == "overflow_counter":
        payload["usage"]["input_tokens"] = 2 ** 63
    elif damage == "usage_array":
        payload["usage"] = []
    else:
        payload["usage"]["codex_rollout_budget_units"] = True
    # A malformed technical envelope must not fall through to generic role/content.
    event.update(role="user", content="Do not turn malformed technical metadata into a user instruction")
    assert ingest._event_text(event, "codex")[3] is False


def test_official_signed_counters_optional_fields_and_empty_turn_ids_remain_supported(tmp_path):
    event = technical("token_usage_record", tmp_path)
    payload = event["payload"]
    for field in ("turn_id", "root_turn_id", "response_id"):
        payload[field] = ""
    for field in ("usage", "turn_token_usage", "thread_token_usage"):
        payload[field].pop("cache_write_input_tokens")
        payload[field]["input_tokens"] = -(2 ** 63)
        payload[field]["total_tokens"] = 2 ** 63 - 1
        payload[field]["codex_rollout_budget_units"] = None
    assert ingest._event_text(event, "codex") == ("", "metadata", "codex", True)
    payload["usage"]["codex_rollout_budget_units"] = 2.5
    assert ingest._event_text(event, "codex")[3] is True


def test_eof_import_reclassifies_legacy_unknowns_without_changing_archive_or_chunks(workspace, tmp_path, monkeypatch):
    store, project, _, _ = workspace
    path, _ = seed_legacy(workspace, tmp_path, monkeypatch)
    before_sources = source_rows(store, path)
    before_chunks = chunk_rows(store, [source["id"] for source in before_sources])
    before_cursor = cursor(store, path)
    original = path.read_bytes()
    assert sum(source["kind"] == "transcript_unknown" for source in before_sources) == 7
    assert all(row["metadata"]["role"] == "unknown" for row in before_chunks
               if row["source_id"] in {source["id"] for source in before_sources if source["kind"] == "transcript_unknown"})

    result = ingest.import_history(store, path, project["id"], "codex")
    assert result["archived"] == 0  # Cursor stays at EOF; old raw events are not duplicated.
    after_sources = source_rows(store, path)
    def immutable(row):
        return {key: row[key] for key in ("id", "project_id", "uri", "sha256", "content", "created_at")}
    assert [immutable(row) for row in after_sources] == [immutable(row) for row in before_sources]
    assert all(row["kind"] == "transcript" and not row["metadata"].get("schema_warning")
               and row["metadata"]["role"] == "metadata" for row in after_sources)
    for row in after_sources:
        if row["metadata"]["byte_offset"] == 0:
            continue
        assert row["metadata"]["transcript_parser_revision"] >= 1
        assert row["metadata"]["transcript_reparse"] == {
            "previous_kind": "transcript_unknown",
            "previous_schema_warning": "Unknown transcript schema; raw record retained",
            "previous_parser_revision": None}
    after_chunks = chunk_rows(store, [source["id"] for source in after_sources])
    assert [{key: value for key, value in row.items() if key != "metadata"} for row in after_chunks] == [
        {key: value for key, value in row.items() if key != "metadata"} for row in before_chunks]
    assert all(row["metadata"]["role"] == "metadata" for row in after_chunks)
    assert cursor(store, path)["offset"] == before_cursor["offset"] == len(original)
    assert path.read_bytes() == original
    assert not store.query("SELECT id FROM jobs WHERE kind='extract'")
    assert not store.query("SELECT id FROM records")
    again = ingest.import_history(store, path, project["id"], "codex")
    assert again["archived"] == 0
    assert ingest.reparse_history_sources(store, path, project["id"], "codex")["repaired"] == 0
    assert source_rows(store, path) == after_sources
    assert chunk_rows(store, [source["id"] for source in after_sources]) == after_chunks


def test_prepare_repairs_the_real_seven_record_horizon_without_creating_ack(workspace, tmp_path, monkeypatch):
    store, project, _, manager = workspace
    path, pending = seed_legacy(workspace, tmp_path, monkeypatch)
    before = source_rows(store, path)
    ready = manager.prepare(project["id"], "codex", session_id=pending["session_id"], timeout=0)
    assert ready["status"] == "ready", ready["unknowns"]
    assert ready["packet"]["archive"]["status"] == "complete"
    horizon = ready["packet"]["archive_watermarks"][0]
    assert horizon["archived_offset"] == horizon["size"] == path.stat().st_size
    assert horizon["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert horizon["source_count"] == len(before) == 8
    assert {row["id"] for row in source_rows(store, path)} == {row["id"] for row in before}
    assert not store.query("SELECT id FROM resume_acks")
    assert not manager.status(ready["session_id"])["acknowledged"]
    with pytest.raises(ValueError, match="resume_required"):
        manager.require_ack(ready["session_id"])


@pytest.mark.parametrize("line", [
    '{"new_schema":true,"content":"Unrecognized bytes remain archived"}\n',
    '{"type":"world_state","payload":{"full":"yes","state":{}}}\n',
    '{"type":"world_state",broken}\n',
    '[1,2,3]\n',
])
def test_other_unknown_or_invalid_records_still_block_ready(workspace, tmp_path, monkeypatch, line):
    store, project, _, manager = workspace
    path, pending = seed_legacy(workspace, tmp_path, monkeypatch, line)
    result = manager.prepare(project["id"], "codex", session_id=pending["session_id"], timeout=0)
    assert result["status"] == "partial"
    assert result["packet"]["archive"]["status"] == "partial"
    remaining = [row for row in source_rows(store, path) if row["kind"] == "transcript_unknown"]
    assert len(remaining) == 1 and remaining[0]["content"] == line
    assert remaining[0]["metadata"]["schema_warning"]
    assert not store.query("SELECT id FROM resume_acks")


@pytest.mark.parametrize("damage", ["stale_hash", "content_changed", "cwd_wrong_project", "other_warning", "uri_changed"])
def test_reparse_never_clears_hash_scope_or_provenance_conflicts(workspace, tmp_path, monkeypatch, damage):
    store, project, _, manager = workspace
    path, pending = seed_legacy(workspace, tmp_path, monkeypatch)
    source = next(row for row in source_rows(store, path) if row["kind"] == "transcript_unknown")
    if damage == "stale_hash":
        store.command("UPDATE sources SET sha256=:value WHERE id=:id", {"value": "f" * 64, "id": source["id"]})
    elif damage == "content_changed":
        store.command("UPDATE sources SET content=:value WHERE id=:id", {"value": source["content"] + " ", "id": source["id"]})
    elif damage == "cwd_wrong_project":
        other = tmp_path / "other"
        other.mkdir()
        store.add_project("other", other)
        metadata = {**source["metadata"], "cwd": str(other)}
        store.command("UPDATE sources SET metadata=:value WHERE id=:id", {"value": metadata, "id": source["id"]})
    elif damage == "other_warning":
        metadata = {**source["metadata"], "schema_warning": "Declared working directory belongs to another project"}
        store.command("UPDATE sources SET metadata=:value WHERE id=:id", {"value": metadata, "id": source["id"]})
    else:
        store.command("UPDATE sources SET uri=:value WHERE id=:id", {"value": "transcript://untrusted/offset", "id": source["id"]})
    result = manager.prepare(project["id"], "codex", session_id=pending["session_id"], timeout=0)
    assert result["status"] == "partial", result
    assert store.get_source(source["id"])["kind"] == "transcript_unknown"
    assert store.get_source(source["id"])["metadata"].get("schema_warning")
    assert not store.query("SELECT id FROM resume_acks")


def test_reparse_cannot_reassign_sources_to_a_different_project(workspace, tmp_path, monkeypatch):
    store, project, _, _ = workspace
    path, _ = seed_legacy(workspace, tmp_path, monkeypatch)
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = store.add_project("other", other_root)
    before = source_rows(store, path)
    result = ingest.reparse_history_sources(store, path, other["id"], "codex")
    assert result["repaired"] == 0
    assert source_rows(store, path) == before
    assert not store.query("SELECT id FROM sources WHERE project_id=:project", {"project": other["id"]})


def test_reparse_preserves_durable_records_when_original_is_missing_but_does_not_invent_archive_proof(
        workspace, tmp_path, monkeypatch):
    store, project, _, manager = workspace
    path, pending = seed_legacy(workspace, tmp_path, monkeypatch)
    before = source_rows(store, path)
    path.unlink()
    repaired = ingest.reparse_history_sources(store, path, project["id"], "codex")
    assert repaired["repaired"] == 7
    assert [(row["id"], row["content"], row["sha256"], row["uri"]) for row in source_rows(store, path)] == [
        (row["id"], row["content"], row["sha256"], row["uri"]) for row in before]
    state = manager.prepare(project["id"], "codex", session_id=pending["session_id"], timeout=0)
    assert state["status"] == "partial"
    assert state["packet"]["archive"]["status"] == "partial"
    assert not store.query("SELECT id FROM resume_acks")
