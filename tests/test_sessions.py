"""Real ArcadeDB/Git control-plane checks; no mocked model claims comprehension."""
import hashlib
import json
from contextlib import contextmanager
import subprocess
import threading
import time

import pytest

from cairnmind.ingest import import_history
from cairnmind.plans import PlanManager
from cairnmind.sessions import MAX_PACKET_BYTES, SessionManager, format_packet
from cairnmind.store import Store
from cairnmind.sql import json_text


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def sessions(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "user.name", "Fixture")
    (root / "app.py").write_text("VALUE = 1\n")
    git(root, "add", "app.py")
    git(root, "commit", "-m", "initial")
    store = Store(tmp_path / "memory")
    project = store.add_project("session-fixture", root)
    return store, project, root, SessionManager(store, store.settings)


def readback(packet):
    return {key: ({"id": packet[field]["id"], "version": packet[field]["version"]}
                  if packet.get(field) else None)
            for key, field in (("plan", "active_plan"), ("task", "active_task"))}


def acknowledge(manager, result):
    packet = result["packet"]
    return manager.ack(result["session_id"], packet["id"], packet["hash"], readback(packet))


def event(result, root, *, name="SessionStart", native="native-codex", reason="startup", history=None, eid="first"):
    payload = {"devbrain_session_id": result["session_id"], "session_id": native,
               "hook_event_name": name, "resume_reason": reason, "cwd": str(root), "event_id": eid}
    if history is not None:
        stat = history.stat()
        head = history.read_bytes()[:4096]
        payload.update(transcript_path=str(history), transcript_watermark={
            "size": stat.st_size, "device": stat.st_dev, "inode": stat.st_ino, "mtime_ns": stat.st_mtime_ns,
            "head_sha256": hashlib.sha256(head).hexdigest(), "head_bytes": len(head)})
    return payload


def write_history(path, root, text="A source-backed instruction", *, append=False, native="native-codex"):
    data = {"type": "user", "cwd": str(root), "sessionId": native,
            "message": {"role": "user", "content": text}}
    with path.open("a" if append else "w") as handle:
        handle.write(json.dumps(data) + "\n")


def active_plan(store, project, root):
    return PlanManager(store).save(project["id"],
        {"title": "Shared implementation", "goal": "Implement and verify safely", "constraints": ["Keep native permissions"]},
        [{"key": "build", "content": "Implement the feature"},
         {"key": "verify", "content": "Verify the result", "depends_on": ["build"]}],
        "initial-plan", worktree=str(root), activate=True)


def test_empty_ready_packet_is_immutable_and_delivery_never_acknowledges(sessions):
    store, project, _, manager = sessions
    result = manager.prepare(project["id"], "codex", timeout=0)
    packet = result["packet"]
    assert result["status"] == "ready" and packet["no_history"] is True
    assert packet["active_plan"] is None and packet["active_task"] is None
    assert packet["archive"]["status"] == "complete"
    assert manager.resume_context(result["session_id"])["packet"] == packet
    assert not store.query("SELECT FROM resume_acks")
    assert not manager.status(result["session_id"])["acknowledged"]
    with pytest.raises(ValueError, match="resume_required"):
        manager.require_ack(result["session_id"])
    with pytest.raises(ValueError, match="readback"):
        manager.ack(result["session_id"], packet["id"], packet["hash"], {})
    acknowledgment = acknowledge(manager, result)
    assert "not proof of understanding" in acknowledgment["meaning"]
    assert acknowledge(manager, result)["id"] == acknowledgment["id"]
    manager.require_ack(result["session_id"], packet["id"])
    assert manager.status(result["session_id"])["acknowledged"]
    assert manager.resume_context(result["session_id"])["packet"] == packet
    body = {key: value for key, value in packet.items() if key != "hash"}
    assert packet["hash"] == hashlib.sha256(json_text(body).encode()).hexdigest()
    assert packet["id"] in format_packet(packet)
    assert len(format_packet(packet)) <= 6500


def test_codex_to_claude_handoff_reads_actual_plan_task_versions(sessions):
    store, project, root, manager = sessions
    saved = active_plan(store, project, root)
    codex = manager.prepare(project["id"], "codex", timeout=0)
    acknowledge(manager, codex)
    task = saved["tasks"][0]
    source = store.snapshot(project["id"], "manual://test-state", "Implementation needs testing")
    updated = store.remember(project["id"], {
        "id": task["id"], "expected_version": task["version"], "expected_plan_version": saved["plan"]["version"],
        "metadata": {**task["metadata"], "state": "needs_test"}}, [source["id"]])
    with pytest.raises(ValueError, match="resume_required"):
        manager.require_ack(codex["session_id"])
    claude = manager.prepare(project["id"], "claude", timeout=0)
    packet = claude["packet"]
    assert packet["active_plan"]["id"] == saved["plan"]["id"]
    assert packet["active_plan"]["version"] == saved["plan"]["version"] + 1
    assert packet["active_task"]["id"] == task["id"]
    assert packet["active_task"]["version"] == updated["version"]
    assert packet["active_task"]["state"] == "needs_test"
    assert packet["next_action"] == "needs_test"
    bad = readback(packet)
    bad["task"]["version"] -= 1
    with pytest.raises(ValueError, match="readback"):
        manager.ack(claude["session_id"], packet["id"], packet["hash"], bad)
    acknowledge(manager, claude)
    manager.require_ack(claude["session_id"])
    current = PlanManager(store).get(project["id"], saved["plan"]["id"])
    assert current["tasks"][0]["metadata"]["state"] == "needs_test"
    assert current["tasks"][1]["metadata"]["state"] == "todo"


def test_archive_barrier_is_actual_eof_not_just_current_cursor(sessions, tmp_path):
    store, project, root, manager = sessions
    history = tmp_path / "history.jsonl"
    write_history(history, root)
    import_history(store, history, project["id"], "claude")
    write_history(history, root, "Last important instruction", append=True)
    partial = manager.prepare(project["id"], "claude", timeout=0)
    assert partial["status"] == "partial"
    assert partial["packet"]["archive_watermarks"][0]["size"] == history.stat().st_size
    assert partial["packet"]["archive_watermarks"][0]["archived_offset"] < history.stat().st_size
    with pytest.raises(ValueError, match="ready"):
        acknowledge(manager, partial)
    import_history(store, history, project["id"], "claude")
    resumed = manager.prepare(project["id"], "claude", session_id=partial["session_id"], timeout=0)
    assert resumed["epoch"] == partial["epoch"]
    assert resumed["status"] == "ready"
    assert resumed["packet"]["extraction"]["status"] == "pending"
    assert resumed["packet"]["indexing"]["status"] == "pending"
    assert resumed["packet"]["archive_watermarks"][0]["sha256"] == hashlib.sha256(history.read_bytes()).hexdigest()
    acknowledge(manager, resumed)


@pytest.mark.parametrize("damage", ["partial_tail", "unknown_schema", "missing_source", "deleted_unproved"])
def test_incomplete_unparsed_or_missing_ranges_are_never_ready(sessions, tmp_path, damage):
    store, project, root, manager = sessions
    history = tmp_path / "damaged.jsonl"
    write_history(history, root)
    import_history(store, history, project["id"], "claude")
    if damage == "partial_tail":
        with history.open("a") as handle:
            handle.write('{"type":"user","message":')
        import_history(store, history, project["id"], "claude")
    elif damage == "unknown_schema":
        with history.open("a") as handle:
            handle.write('{"new_schema":true}\n')
        import_history(store, history, project["id"], "claude")
    elif damage == "missing_source":
        store.command("DELETE FROM sources WHERE metadata.import_path=:path", {"path": str(history)})
    else:
        history.unlink()
    result = manager.prepare(project["id"], "claude", timeout=0)
    assert result["status"] == "partial"
    assert result["packet"]["archive"]["status"] == "partial"
    assert result["unknowns"]


def test_pending_hook_horizon_and_rotation_cannot_disappear_from_scope(sessions, tmp_path):
    store, project, root, manager = sessions
    result = manager.prepare(project["id"], "codex", timeout=0)
    history = tmp_path / "new-history.jsonl"
    write_history(history, root)
    payload = event(result, root, history=history)
    spool = store.data_dir / "hook-events"
    spool.mkdir()
    (spool / "pending.json").write_text(json.dumps({**payload, "client": "codex"}))
    blocked = manager.prepare(project["id"], "claude", timeout=0)
    assert blocked["status"] == "partial"
    manager.note_event(payload, "codex")
    (spool / "pending.json").unlink()
    old = tmp_path / "rotated.jsonl"
    history.rename(old)
    write_history(history, root, "Replacement generation")
    import_history(store, history, project["id"], "claude")
    rotated = manager.prepare(project["id"], "claude", session_id=blocked["session_id"], timeout=0)
    assert rotated["status"] == "partial"
    assert any("rotated" in text or "prefix changed" in text for text in rotated["unknowns"])


def test_proved_archive_survives_original_deletion_but_not_source_loss(sessions, tmp_path):
    store, project, root, manager = sessions
    history = tmp_path / "archived.jsonl"
    write_history(history, root)
    import_history(store, history, project["id"], "claude")
    initial = manager.prepare(project["id"], "claude", timeout=0)
    assert initial["status"] == "ready"
    history.unlink()
    later = manager.prepare(project["id"], "codex", timeout=0)
    assert later["status"] == "ready"
    assert later["packet"]["archive_watermarks"][0]["original_unavailable"]
    store.command("DELETE FROM sources WHERE metadata.import_path=:path", {"path": str(history)})
    damaged = manager.prepare(project["id"], "codex", timeout=0)
    assert damaged["status"] == "partial"


def test_actual_ack_stales_on_code_and_decisions_then_refreshes_epoch(sessions):
    store, project, root, manager = sessions
    initial = manager.prepare(project["id"], "codex", timeout=0)
    acknowledge(manager, initial)
    (root / "app.py").write_text("VALUE = 2\n")
    with pytest.raises(ValueError, match="code changed"):
        manager.require_ack(initial["session_id"])
    refreshed = manager.resume_context(initial["session_id"])
    assert refreshed["epoch"] == initial["epoch"] + 1
    assert refreshed["packet"]["hash"] != initial["packet"]["hash"]
    with pytest.raises(ValueError, match="resume_required"):
        acknowledge(manager, initial)
    acknowledge(manager, refreshed)
    source = store.snapshot(project["id"], "manual://decision", "Use an explicit protocol")
    store.remember(project["id"], {"kind": "decision", "key": "protocol", "content": "Use protocol v2"}, [source["id"]])
    with pytest.raises(ValueError, match="resume_required"):
        manager.require_ack(initial["session_id"])


def test_technical_tail_preserves_ack_but_compaction_invalidates_exactly_once(sessions, tmp_path):
    store, project, root, manager = sessions
    initial = manager.prepare(project["id"], "codex", timeout=0)
    manager.note_event(event(initial, root), "codex")
    acknowledge(manager, initial)
    history = tmp_path / "technical.jsonl"
    write_history(history, root, "Tool: ack_resume receipt")
    manager.note_event(event(initial, root, name="Stop", history=history, eid="stop"), "codex")
    import_history(store, history, project["id"], "claude")
    manager.require_ack(initial["session_id"])
    assert manager.resume_context(initial["session_id"])["packet"]["id"] == initial["packet"]["id"]
    compact = event(initial, root, name="PreCompact", reason="compact", eid="compact-start")
    manager.note_event(compact, "codex")
    manager.note_event(compact, "codex")
    state = manager.status(initial["session_id"])
    assert state["epoch"] == initial["epoch"] + 1 and not state["acknowledged"]
    manager.note_event(event(initial, root, reason="compact", eid="compact-finished"), "codex")
    restored = manager.resume_context(initial["session_id"])
    assert restored["epoch"] == state["epoch"] and restored["status"] == "ready"
    assert len(store.query("SELECT FROM session_events WHERE session_id=:id", {"id": initial["session_id"]})) == 4
    acknowledge(manager, restored)
    manager.require_ack(initial["session_id"])


def test_native_binding_scope_and_worktree_plan_selection_are_isolated(sessions, tmp_path):
    store, project, root, manager = sessions
    first_plan = active_plan(store, project, root)
    other = tmp_path / "feature"
    git(root, "worktree", "add", "-b", "feature", str(other))
    store.add_project_root(project["id"], other)
    second_plan = PlanManager(store).save(project["id"], {"title": "Second branch", "goal": "Other work"},
        [{"key": "other", "content": "Only for the second worktree"}], "second-plan", worktree=str(other), activate=True)
    main = manager.prepare(project["id"], "codex", worktree=str(root), timeout=0)
    feature = manager.prepare(project["id"], "claude", worktree=str(other), timeout=0)
    assert main["packet"]["active_plan"]["id"] == first_plan["plan"]["id"]
    assert feature["packet"]["active_plan"]["id"] == second_plan["plan"]["id"]
    with pytest.raises(ValueError, match="cannot change"):
        manager.prepare(project["id"], "codex", worktree=str(other), session_id=main["session_id"], timeout=0)
    manager.note_event(event(main, root), "codex")
    with pytest.raises(ValueError, match="different native"):
        manager.note_event(event(main, root, native="different", eid="wrong-native"), "codex")
    with pytest.raises(ValueError, match="worktree"):
        manager.note_event(event(main, other, eid="wrong-worktree"), "codex")


def test_preparation_can_wait_for_independent_import_without_owning_import_lock(sessions, tmp_path):
    store, project, root, manager = sessions
    history = tmp_path / "poll.jsonl"
    write_history(history, root)
    import_history(store, history, project["id"], "claude")
    write_history(history, root, "Late flushed user event", append=True)
    failure = []

    def importer():
        try:
            time.sleep(0.1)
            import_history(store, history, project["id"], "claude")
        except Exception as exc:
            failure.append(exc)

    worker = threading.Thread(target=importer)
    worker.start()
    try:
        result = manager.prepare(project["id"], "claude", timeout=5)
    finally:
        worker.join(timeout=5)
    assert not failure and not worker.is_alive()
    assert result["status"] == "ready"
    with store.transaction():
        acknowledge(manager, result)
        manager.require_ack(result["session_id"])


def test_oversized_essential_constraints_stay_bounded_and_unready(sessions):
    store, project, root, manager = sessions
    PlanManager(store).save(project["id"], {"title": "Large", "constraints": ["x" * (MAX_PACKET_BYTES * 2)]},
        [{"key": "first", "content": "Do work"}], "large-plan", worktree=str(root), activate=True)
    result = manager.prepare(project["id"], "codex", timeout=0)
    assert result["status"] == "partial"
    assert len(json_text(result["packet"]).encode("utf-8")) <= MAX_PACKET_BYTES
    assert any("budget" in text for text in result["unknowns"])


def test_unknown_code_state_is_unavailable_and_readback_versions_are_typed(sessions, tmp_path):
    store, project, root, manager = sessions
    saved = active_plan(store, project, root)
    result = manager.prepare(project["id"], "codex", timeout=0)
    malformed = readback(result["packet"])
    assert saved["plan"]["version"] == 1
    malformed["plan"]["version"] = True
    with pytest.raises(ValueError, match="readback"):
        manager.ack(result["session_id"], result["packet"]["id"], result["packet"]["hash"], malformed)
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "existing.py").write_text("Existing unversioned code\n")
    other = store.add_project("plain", plain)
    unavailable = manager.prepare(other["id"], "claude", timeout=0)
    assert unavailable["status"] == "unavailable"
    exact = unavailable["packet"]
    with pytest.raises(ValueError) as refused:
        manager.ack(unavailable["session_id"], exact["id"], exact["hash"], {"plan": None, "task": None})
    message = str(refused.value)
    assert message.startswith("resume_required: the current packet is unavailable, not ready")
    assert "partial" not in message and "does not match" not in message
    assert (exact["unknowns"][0][:200] if exact["unknowns"] else "code state cannot be checked") in message
    for invalid in (-1, float("nan"), float("inf"), True):
        with pytest.raises(ValueError, match="timeout"):
            manager.prepare(project["id"], "codex", timeout=invalid)


def test_first_empty_scope_may_start_without_claiming_verified_code(sessions, tmp_path):
    from cairnmind.integrations import _instructions

    store, _, _, manager = sessions
    plain = tmp_path / "brand-new"
    plain.mkdir()
    project = store.add_project("empty-baseline", plain)
    (plain / "AGENTS.md").write_text(_instructions(project, store.settings))
    store.snapshot(project["id"], str(plain / "AGENTS.md"), (plain / "AGENTS.md").read_text(), "document")
    result = manager.prepare(project["id"], "claude", timeout=0)
    assert result["status"] == "ready"
    assert result["packet"]["code"]["status"] == "unknown"
    assert result["packet"]["baseline_unverified"] is True
    assert "unverified" in format_packet(result["packet"])
    acknowledge(manager, result)
    (plain / "app.py").write_text("First actual code\n")
    with pytest.raises(ValueError, match="resume_required"):
        manager.require_ack(result["session_id"])
    assert manager.resume_context(result["session_id"])["status"] == "unavailable"


def test_missing_current_memory_provenance_blocks_resume_and_stales_ack(sessions):
    store, project, _, manager = sessions
    source = store.snapshot(project["id"], "manual://critical", "Authoritative project constraint")
    store.remember(project["id"], {"kind": "constraint", "key": "critical", "content": "Retain compatibility"},
                   [source["id"]])
    initial = manager.prepare(project["id"], "codex", timeout=0)
    acknowledge(manager, initial)
    store.command("DELETE FROM sources WHERE id=:id", {"id": source["id"]})
    with pytest.raises(ValueError, match="resume_required"):
        manager.require_ack(initial["session_id"])
    refreshed = manager.resume_context(initial["session_id"])
    assert refreshed["status"] == "partial"
    assert any("no scoped durable source" in text for text in refreshed["unknowns"])


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_real_install_into_empty_project_then_prepare_handles_index_race(sessions, tmp_path, monkeypatch, client):
    from cairnmind import integrations
    from cairnmind.ingest import index_project

    store, _, _, manager = sessions
    plain = tmp_path / f"empty-{client}"
    plain.mkdir()
    project = store.add_project(f"fresh-{client}", plain)
    monkeypatch.setattr(integrations, "_probe_cli", lambda *args, **kwargs: {
        "installed": True, "mcp": True, "hooks": True, "verified_resume": True, "notes": []})
    installed = integrations.install_integrations(store.settings, project, clients=(client,))
    assert installed["clients"][client]["mcp_configured"]
    assert installed["clients"][client]["hooks_configured"]
    # The background scanner can archive these generated files before preparation.
    index_project(store, project["id"])
    result = manager.prepare(project["id"], client, timeout=0)
    assert result["status"] == "ready", result["unknowns"]
    assert result["packet"]["baseline_unverified"]
    assert result["packet"]["code"]["status"] == "unknown"
    acknowledge(manager, result)
    if client == "claude":
        assert (plain / ".mcp.json").is_file()
        assert (plain / ".claude/settings.local.json").is_file()
    assert not (plain / ".git").exists()


@pytest.mark.parametrize("alteration", ["other_mcp", "permissions", "hook_command", "instructions", "codex_model"])
def test_empty_exception_rejects_other_native_config_and_substring_impersonation(sessions, tmp_path, monkeypatch, alteration):
    from cairnmind import integrations

    store, _, _, manager = sessions
    plain = tmp_path / "config-is-state"
    plain.mkdir()
    project = store.add_project("config-is-state", plain)
    monkeypatch.setattr(integrations, "_probe_cli", lambda *args, **kwargs: {
        "installed": True, "mcp": True, "hooks": True, "verified_resume": True, "notes": []})
    integrations.install_integrations(store.settings, project)
    if alteration == "other_mcp":
        path = plain / ".mcp.json"
        payload = json.loads(path.read_text())
        payload["mcpServers"]["other"] = {"command": "user-native-server"}
        path.write_text(json.dumps(payload))
    elif alteration in {"permissions", "hook_command"}:
        path = plain / ".claude/settings.local.json"
        payload = json.loads(path.read_text())
        if alteration == "permissions":
            payload["permissions"] = {"allow": ["Read"]}
        else:
            payload["hooks"]["Stop"][0]["hooks"][0]["command"] += " && arbitrary-user-command"
        path.write_text(json.dumps(payload))
    elif alteration == "instructions":
        path = plain / "CLAUDE.md"
        path.write_text("Keep existing user instructions.\n" + path.read_text())
    else:
        path = plain / ".codex/config.toml"
        path.write_text('model = "user-chosen-model"\n' + path.read_text())
    before = path.read_bytes()
    result = manager.prepare(project["id"], "claude", timeout=0)
    assert result["status"] != "ready"
    assert path.read_bytes() == before
    assert not result["packet"].get("baseline_unverified")


@pytest.mark.parametrize("operation", ["prepare", "event"])
def test_forget_between_scope_resolution_and_writer_leaves_no_orphans(sessions, tmp_path, monkeypatch, operation):
    store, project, root, manager = sessions
    original_transaction = store.transaction
    forgotten = False

    @contextmanager
    def forget_before_first_writer():
        nonlocal forgotten
        if not forgotten:
            forgotten = True
            store.forget_project(project["id"])
        with original_transaction() as tx:
            yield tx

    monkeypatch.setattr(store, "transaction", forget_before_first_writer)
    with pytest.raises(ValueError, match="Unknown project"):
        if operation == "prepare":
            manager.prepare(project["id"], "claude", timeout=0)
        else:
            manager.note_event({"cwd": str(root), "session_id": "native-race", "hook_event_name": "SessionStart",
                                "event_id": "forget-race", "resume_reason": "startup"}, "claude")
    assert forgotten
    for collection in ("sessions", "session_events", "resume_packets", "resume_acks"):
        assert not store.query(f"SELECT FROM {collection}")
    backup = store.backup(tmp_path / "after-forget.devbrain.json.gz")
    restored = Store._validated_backup(backup)
    assert not restored["projects"] and not restored["sessions"] and not restored["session_events"]
