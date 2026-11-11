import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cairnmind.ingest import (
    current_source, import_history, index_pending_sources, index_project, poll_imports, register_default_histories,
)
from cairnmind.store import Store


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    store = Store(tmp_path / "memory")
    project = store.add_project("demo", root)
    return store, project, root


def rows(store, sql, params=None):
    return store.query(sql, params)


def test_incremental_snapshots_graph_and_deleted_code(workspace):
    store, project, root = workspace
    auth = root / "auth.py"
    auth.write_text("def login():\n    return True\n")
    (root / "test_auth.py").write_text("from auth import login\n\ndef test_login():\n    assert login()\n")
    first = index_project(store, project["id"])
    assert first["changed"] == 2
    edge_kinds = {row["kind"] for row in rows(store, "SELECT * FROM edges")}
    assert {"imports", "defines", "calls", "test_imports"} <= edge_kinds
    assert index_project(store, project["id"])["unchanged"] == 2
    original = rows(store, 'SELECT * FROM sources WHERE uri=:p0', {"p0": str(auth)})[0]
    auth.write_text("def login():\n    return False\n")
    changed = current_source(store, original["id"])
    assert changed["id"] != original["id"]
    assert "False" in changed["content"]
    assert store.get_source(original["id"])["content"].endswith("True\n")
    auth.unlink()
    result = index_project(store, project["id"])
    assert result["deleted"] == 1
    assert not rows(store, 'SELECT * FROM sources WHERE uri=:p0 AND is_current=true', {"p0": str(auth)})
    assert not rows(store, "SELECT * FROM edges WHERE kind='calls'")


def test_gitignore_secrets_binary_and_symlink_exclusion(workspace, tmp_path):
    store, project, root = workspace
    (root / ".gitignore").write_text("ignored/\n*.log\n!keep.log\n")
    (root / "ignored").mkdir()
    (root / "ignored" / "hidden.py").write_text("secret=1")
    (root / "x.log").write_text("ignored")
    (root / "keep.log").write_text("retained")
    (root / ".env").write_text("TOKEN=secret")
    (root / "id_rsa").write_text("private")
    (root / "thing.bin").write_bytes(b"a\x00b")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "lib.js").write_text("dependency")
    outside = tmp_path / "outside.py"
    outside.write_text("private")
    (root / "link.py").symlink_to(outside)
    (root / "nested").mkdir()
    (root / "nested" / ".gitignore").write_text("skip.txt\n")
    (root / "nested" / "skip.txt").write_text("no")
    index_project(store, project["id"])
    names = {Path(row["uri"]).name for row in rows(store, "SELECT * FROM sources")}
    assert names == {".gitignore", "keep.log"}


def test_history_partial_tail_is_retried_without_duplicates(workspace, tmp_path):
    store, project, root = workspace
    history = tmp_path / "claude.jsonl"
    first = {"type": "user", "cwd": str(root), "sessionId": "s", "message": {"role": "user", "content": "Use OAuth"}}
    second = {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "I will implement it"}]}}
    raw_first = json.dumps(first) + "\n"
    raw_second = json.dumps(second)
    history.write_text(raw_first + raw_second[:20])
    result = import_history(store, history, project["id"], "claude")
    assert result["archived"] == 1
    assert poll_imports(store)["archived"] == 0
    with history.open("a") as handle:
        handle.write(raw_second[20:] + "\n")
    assert poll_imports(store)["archived"] == 1
    assert poll_imports(store)["archived"] == 0
    sources = rows(store, "SELECT * FROM sources")
    assert len(sources) == 2
    assert sources[0]["content"] == raw_first
    assert {row["metadata"]["role"] for row in sources} == {"user", "assistant"}
    assert len(rows(store, "SELECT * FROM jobs WHERE kind='extract'")) == 2


def test_history_unknown_and_invalid_records_are_preserved(workspace, tmp_path):
    store, project, _ = workspace
    history = tmp_path / "new-schema.jsonl"
    history.write_text('{"newSchema": true}\ninvalid-json\n')
    result = import_history(store, history, project["id"])
    assert result["archived"] == 2
    assert result["unknown"] == 2
    assert len(result["errors"]) == 2
    assert len(rows(store, "SELECT * FROM sources WHERE kind='transcript_unknown'")) == 2
    assert not rows(store, "SELECT * FROM jobs WHERE kind='extract'")


def test_unassigned_history_inbox_and_explicit_assignment(workspace, tmp_path):
    store, project, _ = workspace
    history = tmp_path / "unassigned.jsonl"
    history.write_text(json.dumps({"role": "user", "content": "important fact"}) + "\n")
    assert import_history(store, history, None)["inbox"] == 1
    assert not rows(store, "SELECT * FROM sources")
    assert len(rows(store, "SELECT * FROM ingestion_inbox")) == 1
    assert import_history(store, history, project["id"])["archived"] == 1
    assert len(rows(store, "SELECT * FROM sources")) == 1
    assert not rows(store, "SELECT * FROM ingestion_inbox")
    store.forget_project(project["id"])
    assert not rows(store, "SELECT * FROM sources")
    assert not rows(store, "SELECT * FROM ingestion_inbox")
    # The user's original CLI history is outside Cairnmind's deletion boundary.
    assert history.exists()


def test_history_cwd_inference_and_wrong_project_are_isolated(workspace, tmp_path):
    store, project, root = workspace
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = store.add_project("other", other_root)
    history = tmp_path / "session.jsonl"
    history.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": str(other_root), "id": "other"}}) + "\n"
                       + json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "other decision"}}) + "\n")
    result = import_history(store, history, project["id"], "codex")
    assert result["inbox"] == 2
    assert not rows(store, "SELECT * FROM sources")
    # Reassigning explicitly to the declared project safely replays the file.
    assert import_history(store, history, other["id"], "codex")["archived"] == 2
    assert {row["project_id"] for row in rows(store, "SELECT * FROM sources")} == {other["id"]}


def test_in_place_rotation_replays_new_content(workspace, tmp_path):
    store, project, _ = workspace
    path = tmp_path / "rotate.jsonl"
    path.write_text(json.dumps({"role": "user", "content": "old"}) + "\n")
    import_history(store, path, project["id"])
    # Same inode and a longer new file: size-only detection would skip its prefix.
    path.write_text(json.dumps({"role": "user", "content": "new version with more words"}) + "\n")
    assert poll_imports(store)["archived"] == 1
    sources = rows(store, "SELECT * FROM sources")
    assert len(sources) == 2
    # Historical transcript evidence remains valid despite reused file offsets.
    assert all(source["is_current"] for source in sources)


def test_discovery_registers_only_matching_project(workspace, tmp_path, monkeypatch):
    store, project, root = workspace
    codex = tmp_path / "codex"
    sessions = codex / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(codex))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-claude"))
    for name, cwd in [("yes", root), ("no", tmp_path / "unregistered")]:
        (sessions / f"{name}.jsonl").write_text(json.dumps({"type": "session_meta", "payload": {"cwd": str(cwd), "id": name}}) + "\n")
    result = register_default_histories(store, project["id"], root)
    assert result["discovered"] == 2
    assert result["registered"] == 1
    assert len(rows(store, "SELECT * FROM import_cursors")) == 1


def test_javascript_dependency_graph_has_observed_import_and_call(workspace):
    store, project, root = workspace
    (root / "auth.ts").write_text("export function login() { return true; }\n")
    (root / "api.ts").write_text("import { login as authenticate } from './auth';\nexport const endpoint = () => authenticate();\n")
    index_project(store, project["id"])
    assert rows(store, "SELECT * FROM edges WHERE kind='imports'")
    assert rows(store, "SELECT * FROM edges WHERE kind='calls'")
    assert {row["status"] for row in rows(store, "SELECT * FROM edges")} == {"observed"}


def test_current_source_rejects_registered_path_escape(workspace, tmp_path):
    store, project, _ = workspace
    outside = tmp_path / "outside.py"
    outside.write_text("secret")
    source = store.snapshot(project["id"], str(outside), "old", kind="code", metadata={"path": str(outside)})
    with pytest.raises(ValueError, match="outside"):
        current_source(store, source["id"])


def test_snapshot_preserves_bom_and_crlf(workspace):
    store, project, root = workspace
    file = root / "windows.py"
    original = b"\xef\xbb\xbfdef example():\r\n    return True\r\n"
    file.write_bytes(original)
    index_project(store, project["id"])
    source = rows(store, 'SELECT * FROM sources WHERE uri=:p0', {"p0": str(file)})[0]
    assert source["content"].encode() == original
    assert current_source(store, source["id"])["id"] == source["id"]


def test_claude_tool_result_is_not_user_authored(workspace, tmp_path):
    store, project, _ = workspace
    file = tmp_path / "tool-result.jsonl"
    file.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "ask-1", "content": "A previous generated answer"}
    ]}}) + "\n")
    import_history(store, file, project["id"], "claude")
    source = rows(store, "SELECT * FROM sources")[0]
    assert source["metadata"]["role"] == "tool"
    assert not rows(store, "SELECT * FROM jobs WHERE kind='extract'")


def test_missing_embedding_enqueue_is_repaired_on_rescan(workspace):
    store, project, root = workspace
    (root / "app.py").write_text("def app():\n    pass\n")
    index_project(store, project["id"])
    store.command("DELETE FROM jobs WHERE kind='embed'")
    assert not rows(store, "SELECT * FROM jobs")
    index_project(store, project["id"])
    assert len(rows(store, "SELECT * FROM jobs WHERE kind='embed'")) == 1


def test_concurrent_index_and_import_are_idempotent(workspace, tmp_path):
    store, project, root = workspace
    (root / "app.py").write_text("def login():\n    return True\n")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: index_project(store, project["id"]), range(2)))
    assert sum(result["changed"] for result in results) == 1
    assert len(rows(store, "SELECT * FROM sources WHERE is_current=true")) == 1
    assert len(rows(store, "SELECT * FROM chunks")) == 1
    assert len(rows(store, "SELECT * FROM edges WHERE kind='defines'")) == 1
    history = tmp_path / "race.jsonl"
    history.write_text(json.dumps({"role": "user", "content": "Keep OAuth"}) + "\n")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: import_history(store, history, project["id"]), range(2)))
    assert sum(result["archived"] for result in results) == 1
    assert len(rows(store, "SELECT * FROM sources WHERE kind='transcript'")) == 1
    assert len(rows(store, "SELECT * FROM chunks")) == 2


def test_memory_and_client_credentials_are_never_project_sources(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    store = Store(root / "memory")
    project = store.add_project("demo", root)
    (store.data_dir / "config.json").write_text('{"token": "private"}')
    for directory in (".codex", ".claude", ".ssh", ".aws", ".azure"):
        folder = root / directory
        folder.mkdir()
        (folder / "config.json").write_text('{"token": "private"}')
    (root / "public.py").write_text("value = 1\n")
    index_project(store, project["id"])
    assert {row["uri"] for row in rows(store, "SELECT * FROM sources")} == {str(root / "public.py")}
    manually_added = store.snapshot(project["id"], str(store.data_dir / "config.json"), "old", kind="document",
                                    metadata={"path": str(store.data_dir / "config.json")})
    with pytest.raises(ValueError, match="excluded"):
        current_source(store, manually_added["id"])


def test_session_changing_to_unregistered_cwd_cannot_leak_into_project(workspace, tmp_path):
    store, project, root = workspace
    history = tmp_path / "cwd-change.jsonl"
    events = [
        {"type": "session_meta", "payload": {"cwd": str(root), "id": "s"}},
        {"type": "turn_context", "payload": {"cwd": str(tmp_path / "unregistered")}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Another project's private decision"}},
    ]
    history.write_text("".join(json.dumps(event) + "\n" for event in events))
    result = import_history(store, history, project["id"], "codex")
    assert result["archived"] == 1
    assert result["inbox"] == 2
    assert len(rows(store, "SELECT * FROM sources")) == 1
    assert not rows(store, "SELECT * FROM jobs WHERE kind='extract'")
    assert all("outside" in row["reason"] for row in rows(store, "SELECT * FROM ingestion_inbox"))


def test_record_provenance_bridges_to_code_and_dependent_test(workspace):
    store, project, root = workspace
    auth = root / "auth.py"
    auth.write_text("def login():\n    return True\n")
    test_file = root / "test_auth.py"
    test_file.write_text("from auth import login\n\ndef test_login():\n    assert login()\n")
    index_project(store, project["id"])
    source = rows(store, 'SELECT * FROM sources WHERE uri=:p0 AND is_current=true', {"p0": str(auth)})[0]
    decision = store.remember(project["id"], {"kind": "decision", "key": "login", "content": "Keep login behavior"}, [source["id"]])
    # Rebuilding syntax graphs must preserve record edges and recreate the bridge.
    index_project(store, project["id"])
    graph_edges = rows(store, 'SELECT * FROM edges WHERE project_id=:p0', {"p0": project["id"]})
    traversed = store.query(
        "TRAVERSE both('edges') FROM (SELECT FROM nodes WHERE id=:root AND project_id=:project) "
        "MAXDEPTH 4 LIMIT 100 STRATEGY BREADTH_FIRST",
        {"root": f"record:{decision['id']}", "project": project["id"]},
    )
    reached = {row["id"] for row in traversed}
    test_node = rows(store, "SELECT * FROM nodes WHERE kind='file' AND label=:p0", {"p0": str(test_file)})[0]
    assert test_node["id"] in reached
    assert f"source:{source['id']}" in reached
    assert any(edge["kind"] == "snapshot_of" for edge in graph_edges)


def test_pending_manual_checkpoint_and_verification_sources_get_semantic_jobs(workspace, tmp_path):
    store, project, _ = workspace
    manual = store.snapshot(project["id"], "manual://fact", "Prefer source-grounded answers", "manual", {"role": "user"})
    # A verification path is provenance only; no live file read is needed.
    verification = store.snapshot(project["id"], "verification://test", "pytest: 12 passed", "verification",
                                  {"path": str(tmp_path / "missing-result.txt"), "role": "user"})
    checkpoint = store.checkpoint(project["id"], {"goal": "Finish local memory", "next_step": "Run tests"})
    assert not rows(store, "SELECT * FROM chunks")
    result = index_pending_sources(store)
    assert result == {"sources": 3, "chunks": 3, "errors": []}
    chunk_sources = {row["source_id"] for row in rows(store, "SELECT * FROM chunks")}
    assert chunk_sources == {manual["id"], verification["id"], checkpoint["source_ids"][0]}
    assert len(rows(store, "SELECT * FROM jobs WHERE kind='embed'")) == 3
    assert not rows(store, "SELECT * FROM jobs WHERE kind='extract'")
    assert index_pending_sources(store) == {"sources": 0, "chunks": 0, "errors": []}
    assert len(rows(store, "SELECT * FROM jobs")) == 3


def test_pending_sources_keep_project_scope_and_exclude_inbox_and_generated_answers(workspace, tmp_path):
    store, project, _ = workspace
    other_root = tmp_path / "other-pending"
    other_root.mkdir()
    other = store.add_project("other-pending", other_root)
    first = store.snapshot(project["id"], "manual://one", "Project one fact", "manual")
    second = store.snapshot(other["id"], "manual://two", "Project two fact", "manual")
    store.snapshot(None, "inbox://unassigned", "Unassigned secret", "manual")
    store.snapshot(project["id"], "answer://generated", "Generated belief", "agent_answer",
                   {"normalized_text": "Generated belief"})
    store.snapshot(project["id"], "transcript://not-imported", "Transcript raw", "transcript")
    store.snapshot(project["id"], "file://not-indexed", "File raw", "document", {"path": "/outside/file.txt"})
    result = index_pending_sources(store, limit=1)
    assert result["sources"] == 1
    assert index_pending_sources(store)["sources"] == 1
    chunks = rows(store, "SELECT * FROM chunks")
    assert {(row["project_id"], row["source_id"]) for row in chunks} == {
        (project["id"], first["id"]), (other["id"], second["id"]),
    }
    assert len(rows(store, "SELECT * FROM jobs WHERE kind='embed'")) == 2


def test_pending_custom_normalized_source_indexes_readable_text_without_extraction(workspace):
    store, project, _ = workspace
    source = store.snapshot(project["id"], "note://normalized", '{"payload":"encoded wrapper"}', "note",
                            {"normalized_text": "Readable archived note", "role": "user"})
    assert index_pending_sources(store)["sources"] == 1
    chunk = rows(store, "SELECT * FROM chunks")[0]
    assert chunk["source_id"] == source["id"]
    assert chunk["text"] == "Readable archived note"
    assert not rows(store, "SELECT * FROM jobs WHERE kind='extract'")


def test_concurrent_pending_source_indexing_creates_one_chunk_set(workspace, monkeypatch):
    from threading import Barrier

    from cairnmind import ingest

    store, project, _ = workspace
    source = store.snapshot(project["id"], "manual://concurrent", "Shared archived decision", "manual")
    original = ingest._write_chunks
    selected = Barrier(2)

    def write_after_both_selected(*args, **kwargs):
        # Both callers must observe the same pending source before either writes.
        selected.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(ingest, "_write_chunks", write_after_both_selected)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: index_pending_sources(store), range(2)))
    assert sum(result["chunks"] for result in results) == 1
    assert all(not result["errors"] for result in results)
    assert len(rows(store, 'SELECT * FROM chunks WHERE source_id=:p0', {"p0": source["id"]})) == 1
    assert len(rows(store, "SELECT * FROM jobs WHERE kind='embed'")) == 1


def test_native_graph_fingerprint_skips_unchanged_rebuild(workspace, monkeypatch):
    store, project, root = workspace
    (root / "app.py").write_text("def app():\n    return True\n")
    initial = index_project(store, project["id"])
    assert initial["graph_unchanged"] is False

    def unexpected_upsert(*args, **kwargs):
        raise AssertionError("Unchanged graph must not perform native vertex upserts")

    monkeypatch.setattr(store, "upsert_node", unexpected_upsert)
    result = index_project(store, project["id"])
    assert result["graph_unchanged"] is True
    assert result["nodes"] == initial["nodes"]
    assert result["edges"] == initial["edges"]


def test_native_graph_marker_commits_only_with_complete_graph(workspace, monkeypatch):
    store, project, root = workspace
    code = root / "app.py"
    code.write_text("def app():\n    return True\n")
    index_project(store, project["id"])
    previous = store.query("SELECT graph_fingerprint FROM projects WHERE id=:id", {"id": project["id"]})[0]
    code.write_text("def app():\n    return False\n")
    original = store.upsert_edge

    def fail_graph_write(*args, **kwargs):
        raise RuntimeError("Simulated interruption during native edge writes")

    monkeypatch.setattr(store, "upsert_edge", fail_graph_write)
    with pytest.raises(RuntimeError, match="interruption"):
        index_project(store, project["id"])
    assert store.query("SELECT graph_fingerprint FROM projects WHERE id=:id", {"id": project["id"]})[0] == previous
    monkeypatch.setattr(store, "upsert_edge", original)
    repaired = index_project(store, project["id"])
    assert repaired["changed"] == 0  # Sources committed before the failed graph transaction.
    assert repaired["graph_unchanged"] is False
    assert store.query("SELECT graph_fingerprint FROM projects WHERE id=:id", {"id": project["id"]})[0] != previous


def test_restored_file_reactivates_new_snapshot_and_chunks(workspace):
    store, project, root = workspace
    code = root / "app.py"
    content = "def app():\n    return True\n"
    code.write_text(content)
    index_project(store, project["id"])
    first = rows(store, "SELECT FROM sources WHERE uri=:uri AND is_current=true", {"uri": str(code)})[0]
    code.unlink()
    assert current_source(store, first["id"])["deleted"] is True
    assert not rows(store, "SELECT FROM chunks WHERE source_id=:source AND is_current=true", {"source": first["id"]})
    code.write_text(content)
    index_project(store, project["id"])
    current = rows(store, "SELECT FROM sources WHERE uri=:uri AND is_current=true", {"uri": str(code)})[0]
    assert current["id"] != first["id"]
    assert rows(store, "SELECT FROM chunks WHERE source_id=:source AND is_current=true", {"source": current["id"]})


def test_parser_identity_change_rebuilds_derived_native_graph(workspace, monkeypatch):
    from cairnmind import ingest

    store, project, root = workspace
    code = root / "app.py"
    code.write_text("def app():\n    return True\n")
    index_project(store, project["id"])
    previous = store.query("SELECT graph_fingerprint FROM projects WHERE id=:id", {"id": project["id"]})[0]
    new_identity = ingest.parser_identity() + ":upgraded"
    monkeypatch.setattr(ingest, "parser_identity", lambda: new_identity)
    result = index_project(store, project["id"])
    assert result["unchanged"] == 1
    assert result["graph_unchanged"] is False
    source = rows(store, "SELECT FROM sources WHERE uri=:uri AND is_current=true", {"uri": str(code)})[0]
    assert source["metadata"]["analysis_version"] == new_identity
    assert store.query("SELECT graph_fingerprint FROM projects WHERE id=:id", {"id": project["id"]})[0] != previous


def test_project_deletion_during_index_cannot_recreate_orphan_vertices(workspace, monkeypatch):
    from cairnmind import ingest

    store, project, root = workspace
    (root / "app.py").write_text("def app():\n    return True\n")
    original = ingest._build_graph

    def forget_before_graph(store, project_id, indexed):
        store.forget_project(project_id)
        return original(store, project_id, indexed)

    monkeypatch.setattr(ingest, "_build_graph", forget_before_graph)
    with pytest.raises(ValueError, match="Project no longer exists"):
        index_project(store, project["id"])
    assert not rows(store, "SELECT FROM nodes")
    assert not rows(store, "SELECT FROM edges")
    assert not rows(store, "SELECT FROM chunks")


def test_project_deletion_during_import_cannot_recreate_orphan_cursor(workspace, tmp_path, monkeypatch):
    from cairnmind import ingest

    store, project, _ = workspace
    path = tmp_path / "forget-import.jsonl"
    path.write_text(json.dumps({"role": "user", "content": "Previously assigned note"}) + "\n")
    original = ingest._save_cursor

    def forget_before_cursor(store, path, project_id, client, offset):
        store.forget_project(project_id)
        return original(store, path, project_id, client, offset)

    monkeypatch.setattr(ingest, "_save_cursor", forget_before_cursor)
    with pytest.raises(ValueError, match="Project no longer exists"):
        import_history(store, path, project["id"])
    assert not rows(store, "SELECT FROM import_cursors")
    assert not rows(store, "SELECT FROM sources")
