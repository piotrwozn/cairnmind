"""Source-preserving compatibility for observed Claude Code bookkeeping records."""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess

import pytest

from devbrain import ingest
from devbrain.retrieval import Retriever
from devbrain.sessions import SessionManager
from devbrain.store import Store


SESSION_ID = "10000000-0000-4000-8000-000000000001"
CLAUDE_TYPES = (
    "mode", "permission-mode", "session-owner", "atis-latch", "attachment",
    "api-request-shape", "api-request", "last-prompt", "cost-state", "ai-title", "tools-used",
)


def technical(kind, root):
    opaque = {"text": "Opaque data: do not turn this into an independent user decision"}
    fields = {
        "mode": {"mode": "normal"},
        "permission-mode": {"permissionMode": "default"},
        "session-owner": {"accountUuid": "20000000-0000-4000-8000-000000000002",
                          "organizationUuid": "30000000-0000-4000-8000-000000000003"},
        "atis-latch": {"atis": "fixture-latch"},
        "attachment": {"parentUuid": None, "isSidechain": False, "attachment": opaque,
                       "uuid": "40000000-0000-4000-8000-000000000004", "timestamp": "2026-11-06T00:00:00Z",
                       "userType": "external", "entrypoint": "cli", "cwd": str(root),
                       "version": "2.1.318", "gitBranch": "main"},
        "api-request-shape": {"shapeHash": "fixture-shape", "timestamp": "2026-11-06T00:00:00Z",
                              "version": "2.1.318", "shape": opaque},
        "api-request": {"id": "fixture-request", "timestamp": "2026-11-06T00:00:00Z", "version": "2.1.318",
                        "querySource": "repl_main", "shapeHash": "fixture-shape", "params": opaque,
                        "keep": 0, "tail": [{"opaque": "request context"}], "cacheControl": [],
                        "requestDigest": "fixture-digest"},
        "last-prompt": {"lastPrompt": "A duplicate bookkeeping copy of a prompt", "leafUuid": "fixture-leaf"},
        "cost-state": {"totalCostUSD": 0.125, "totalAPIDuration": 2, "totalAPIDurationWithoutRetries": 2,
                       "totalToolDuration": 1, "totalLinesAdded": 0, "totalLinesRemoved": 0,
                       "totalDuration": 3, "startTime": 1793923200000,
                       "modelUsage": {"fixture-model": {"inputTokens": 1}}, "hasUnknownModelCost": False},
        "ai-title": {"aiTitle": "Fixture bookkeeping"},
        "tools-used": {"tools": ["mcp__devbrain__resume_context", {"opaque": "future metadata"}]},
    }
    return {"type": kind, "sessionId": SESSION_ID, **copy.deepcopy(fields[kind])}


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("VALUE = 1\n")
    for arguments in (("init", "-b", "main"), ("add", "."),
                      ("-c", "user.name=Fixture", "-c", "user.email=fixture@localhost",
                       "commit", "-m", "initial")):
        subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)
    store = Store(tmp_path / "memory")
    project = store.add_project("claude-compatibility", root)
    return store, project, root, SessionManager(store, store.settings)


def sources(store, path):
    return store.query("SELECT FROM sources WHERE metadata.import_path=:path ORDER BY metadata.byte_offset",
                       {"path": str(path)})


def chunks(store, ids):
    return store.query("SELECT FROM chunks WHERE source_id IN :ids ORDER BY id", {"ids": ids})


def cursor(store, path):
    return store.query("SELECT FROM import_cursors WHERE path=:path", {"path": str(path)})[0]


def seed_legacy(workspace, tmp_path, monkeypatch, suffix=""):
    store, project, root, manager = workspace
    path = tmp_path / "claude-session.jsonl"
    path.write_text("".join(json.dumps(technical(kind, root), ensure_ascii=False) + "\n"
                            for kind in CLAUDE_TYPES) + suffix)
    actual = ingest._event_text

    def previous_parser(event, client):
        if event.get("type") in CLAUDE_TYPES:
            return "", "unknown", "claude", False
        return actual(event, client)

    with monkeypatch.context() as previous:
        previous.setattr(ingest, "_event_text", previous_parser)
        previous.setattr(ingest, "reparse_history_sources", lambda *a, **kw:
                         {"repaired": 0, "unchanged": 0, "errors": []})
        previous.setattr(ingest, "_reparse_history_sources_locked", lambda *a, **kw:
                         {"repaired": 0, "unchanged": 0, "errors": []})
        result = ingest.import_history(store, path, project["id"], "claude")
        for source in sources(store, path):
            metadata = {**source["metadata"], "transcript_parser_revision": 1}
            store.command("UPDATE sources SET metadata=:metadata WHERE id=:id",
                          {"metadata": metadata, "id": source["id"]})
        blocked = manager.prepare(project["id"], "claude", timeout=0)
    assert result["unknown"] >= len(CLAUDE_TYPES)
    assert blocked["status"] == "partial"
    return path, blocked


@pytest.mark.parametrize("kind", CLAUDE_TYPES)
def test_observed_claude_envelopes_are_metadata_with_opaque_contents_preserved(tmp_path, kind):
    event = technical(kind, tmp_path)
    original = copy.deepcopy(event)
    assert ingest._event_text(event, "auto") == ("", "metadata", "claude", True)
    assert ingest._event_text(event, "claude") == ("", "metadata", "claude", True)
    assert event == original


@pytest.mark.parametrize("kind,field,value", [
    ("mode", "sessionId", None),
    ("permission-mode", "permissionMode", 1),
    ("session-owner", "organizationUuid", []),
    ("attachment", "attachment", []),
    ("attachment", "isSidechain", 0),
    ("api-request", "keep", True),
    ("api-request", "tail", {}),
    ("cost-state", "totalToolDuration", True),
    ("cost-state", "totalCostUSD", float("inf")),
])
def test_malformed_reserved_envelope_cannot_impersonate_a_user_message(tmp_path, kind, field, value):
    event = technical(kind, tmp_path)
    event[field] = value
    event.update(role="user", content="A malformed technical record must not become an instruction")
    assert ingest._event_text(event, "claude")[3] is False


def test_missing_required_field_is_rejected_and_unknown_extensions_are_retained(tmp_path):
    event = technical("atis-latch", tmp_path)
    event.pop("atis")
    assert ingest._event_text(event, "claude")[3] is False
    event = technical("api-request", tmp_path)
    event["unknownFutureField"] = {"opaque": ["still archived"]}
    assert ingest._event_text(event, "claude") == ("", "metadata", "claude", True)


@pytest.mark.asyncio
async def test_eof_repair_preserves_raw_identities_vectors_jobs_and_cursor(workspace, tmp_path, monkeypatch):
    store, project, _, _ = workspace
    path, _ = seed_legacy(workspace, tmp_path, monkeypatch)

    class VectorFixture:
        async def model_identity(self, model):
            return "fixture-claude-compatibility-vector"

        async def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    job = store.query("SELECT FROM jobs WHERE kind='embed' ORDER BY id LIMIT 1")[0]
    await Retriever(store, store.settings, ollama=VectorFixture()).embed_pending(job)
    before = sources(store, path)
    ids = [row["id"] for row in before]
    old_chunks = chunks(store, ids)
    old_cursor = cursor(store, path)
    old_jobs = store.query("SELECT FROM jobs ORDER BY id")
    old_embeddings = store.query("SELECT FROM embeddings ORDER BY id")
    registry = store.query("SELECT FROM vector_indexes")[0]
    old_vectors = store.query(f"SELECT FROM {registry['type_name']} ORDER BY id")
    raw = path.read_bytes()
    assert len(before) == len(CLAUDE_TYPES)
    assert all(row["kind"] == "transcript_unknown" for row in before)

    result = ingest.import_history(store, path, project["id"], "claude")
    assert result["archived"] == 0
    after = sources(store, path)
    immutable = ("id", "project_id", "uri", "sha256", "content", "created_at", "is_current", "current_key")
    assert [{key: row[key] for key in immutable} for row in after] == [
        {key: row[key] for key in immutable} for row in before]
    assert all(row["kind"] == "transcript" and row["metadata"]["role"] == "metadata"
               and row["metadata"]["normalized_text"] == "" and not row["metadata"].get("schema_warning")
               for row in after)
    for row in after:
        assert row["metadata"]["transcript_parser_revision"] >= 2
        assert row["metadata"]["transcript_reparse"] == {
            "previous_kind": "transcript_unknown", "previous_parser_revision": 1,
            "previous_schema_warning": "Unknown transcript schema; raw record retained"}
    new_chunks = chunks(store, ids)
    assert [{key: value for key, value in row.items() if key != "metadata"} for row in new_chunks] == [
        {key: value for key, value in row.items() if key != "metadata"} for row in old_chunks]
    assert all(row["metadata"]["role"] == "metadata" for row in new_chunks)
    assert cursor(store, path) == old_cursor
    assert store.query("SELECT FROM embeddings ORDER BY id") == old_embeddings
    assert store.query(f"SELECT FROM {registry['type_name']} ORDER BY id") == old_vectors
    assert store.query("SELECT FROM jobs ORDER BY id") == old_jobs
    assert not store.query("SELECT id FROM jobs WHERE kind='extract'")
    assert not store.query("SELECT id FROM records")
    assert path.read_bytes() == raw
    assert ingest.reparse_history_sources(store, path, project["id"], "claude")["repaired"] == 0
    assert sources(store, path) == after


def test_existing_claude_horizon_recovers_ready_without_creating_an_ack(workspace, tmp_path, monkeypatch):
    store, project, _, manager = workspace
    path, blocked = seed_legacy(workspace, tmp_path, monkeypatch)
    before = sources(store, path)
    ready = manager.resume_context(blocked["session_id"])
    assert ready["status"] == "ready", ready["unknowns"]
    horizon = ready["packet"]["archive_watermarks"][0]
    assert horizon["source_count"] == len(CLAUDE_TYPES)
    assert horizon["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert horizon["archived_offset"] == horizon["size"] == path.stat().st_size
    assert {row["id"] for row in sources(store, path)} == {row["id"] for row in before}
    assert not store.query("SELECT id FROM resume_acks")
    with pytest.raises(ValueError, match="resume_required"):
        manager.require_ack(blocked["session_id"])


@pytest.mark.parametrize("line", [
    '{"type":"future-claude-record","sessionId":"fixture","arbitrary":"data"}\n',
    '{"type":"mode","mode":false,"sessionId":"fixture"}\n',
    'not-json\n',
])
def test_unrecognized_or_invalid_rows_still_block_ready(workspace, tmp_path, monkeypatch, line):
    store, _, _, manager = workspace
    path, blocked = seed_legacy(workspace, tmp_path, monkeypatch, suffix=line)
    result = manager.resume_context(blocked["session_id"])
    assert result["status"] == "partial"
    remaining = [row for row in sources(store, path) if row["kind"] == "transcript_unknown"]
    assert len(remaining) == 1 and remaining[0]["content"] == line
    assert not store.query("SELECT id FROM resume_acks")


@pytest.mark.parametrize("damage", ["hash", "uri", "cwd", "other_warning"])
def test_repair_never_clears_provenance_or_scope_conflicts(workspace, tmp_path, monkeypatch, damage):
    store, _, _, manager = workspace
    path, blocked = seed_legacy(workspace, tmp_path, monkeypatch)
    source = sources(store, path)[0]
    if damage == "hash":
        store.command("UPDATE sources SET sha256=:value WHERE id=:id", {"value": "f" * 64, "id": source["id"]})
    elif damage == "uri":
        store.command("UPDATE sources SET uri=:value WHERE id=:id", {"value": "transcript://forged", "id": source["id"]})
    else:
        metadata = dict(source["metadata"])
        if damage == "cwd":
            other_root = tmp_path / "other"
            other_root.mkdir()
            store.add_project("other", other_root)
            metadata["cwd"] = str(other_root)
        else:
            metadata["schema_warning"] = "Declared working directory belongs to another project"
        store.command("UPDATE sources SET metadata=:value WHERE id=:id", {"value": metadata, "id": source["id"]})
    result = manager.resume_context(blocked["session_id"])
    assert result["status"] == "partial"
    assert store.get_source(source["id"])["kind"] == "transcript_unknown"
    assert store.get_source(source["id"])["metadata"].get("schema_warning")
    assert not store.query("SELECT id FROM resume_acks")


def test_other_client_or_project_cannot_reclassify_claude_sources(workspace, tmp_path, monkeypatch):
    store, project, _, _ = workspace
    path, _ = seed_legacy(workspace, tmp_path, monkeypatch)
    before = sources(store, path)
    assert ingest.reparse_history_sources(store, path, project["id"], "codex")["repaired"] == 0
    assert sources(store, path) == before
    root = tmp_path / "other"
    root.mkdir()
    other = store.add_project("other", root)
    assert ingest.reparse_history_sources(store, path, other["id"], "claude")["repaired"] == 0
    assert sources(store, path) == before


def test_repaired_records_do_not_make_an_unfinished_horizon_complete(workspace, tmp_path, monkeypatch):
    store, project, root, manager = workspace
    unfinished = json.dumps(technical("mode", root))
    path, blocked = seed_legacy(workspace, tmp_path, monkeypatch, suffix=unfinished)
    result = manager.resume_context(blocked["session_id"])
    assert result["status"] == "partial"
    assert not store.query("SELECT id FROM resume_acks")
    with path.open("a") as stream:
        stream.write("\n")
    ingest.import_history(store, path, project["id"], "claude")
    ready = manager.resume_context(blocked["session_id"])
    assert ready["status"] == "ready", ready["unknowns"]
    assert not store.query("SELECT id FROM resume_acks")
