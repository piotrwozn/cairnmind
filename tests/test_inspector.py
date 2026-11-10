"""Inspector browsing, projections, local-user CAS and journal subscriptions."""
from __future__ import annotations

import json
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from devbrain.inspector import Inspector, InspectorNotFound
from devbrain.inspector_api import attach_inspector_routes
from devbrain.plans import PlanManager
from devbrain.store import Store


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "project"
    other_root = tmp_path / "other"
    root.mkdir()
    other_root.mkdir()
    store = Store(tmp_path / "database")
    project = store.add_project("inspector", root)
    private = store.add_project("other-project", other_root)
    source = store.snapshot(project["id"], "note://literal", "Use durable local memory.", "manual")
    record = store.remember(project["id"], {"kind": "decision", "content": "Use durable local memory."}, [source["id"]])
    private_source = store.snapshot(private["id"], "note://private", "OTHER_PROJECT_INSPECTOR_CANARY", "manual")
    private_record = store.remember(private["id"], {"kind": "fact", "content": "OTHER_PROJECT_INSPECTOR_CANARY"}, [private_source["id"]])
    app = FastAPI()
    attach_inspector_routes(app, store, store.settings)
    headers = {"Authorization": "Bearer " + store.settings.token}
    return store, project, root, private, private_record, source, record, TestClient(app), headers


def normalized_export(store):
    result = store.export()
    result.pop("exported_at", None)
    return result


def test_read_only_paging_details_and_literal_source_slices_do_not_change_memory(workspace, monkeypatch):
    store, project, root, private, private_record, source, record, client, headers = workspace
    from devbrain.agent import KnowledgeAgent
    import devbrain.ingest as ingest
    import devbrain.verification as verification

    def forbidden(*args, **kwargs):
        raise AssertionError("Inspector reads must not run models, indexers or recovery")

    monkeypatch.setattr(KnowledgeAgent, "ask", forbidden)
    monkeypatch.setattr(KnowledgeAgent, "extract", forbidden)
    monkeypatch.setattr(ingest, "index_project", forbidden)
    monkeypatch.setattr(verification, "recover_interrupted_runs", forbidden)
    for index in range(3):
        store.remember(project["id"], {"kind": "fact", "content": f"Known fact {index}"}, [source["id"]])
    raw = '{"message":"<script>literal</script>"}\r\n'
    transcript = store.snapshot(project["id"], "transcript://literal", raw, "transcript",
        {"normalized_text": "Normalized message\r\nSecond line", "role": "assistant", "client": "codex", "session_id": "native-session"})
    before = normalized_export(store)
    page = client.get("/inspector/records", params={"project": project["id"], "limit": 2}, headers=headers).json()
    second = client.get("/inspector/records", params={"project": project["id"], "limit": 2, "cursor": page["next_cursor"]}, headers=headers).json()
    assert page["total"] == second["total"] == 4
    assert not ({row["id"] for row in page["items"]} & {row["id"] for row in second["items"]})
    assert second["next_cursor"] is None
    detail = client.get(f"/inspector/records/{record['id']}", params={"project": project["id"]}, headers=headers)
    assert detail.status_code == 200 and detail.json()["record"]["content"] == record["content"]
    direct_history = Inspector(store).revisions(project["id"], record["id"])
    history = client.get(f"/inspector/records/{record['id']}/revisions", params={"project": project["id"]}, headers=headers).json()
    assert "total" in history, history
    assert history["total"] == 1 and history["items"][0]["snapshot"]["id"] == record["id"]
    assert direct_history["items"] == history["items"]
    first = client.get(f"/inspector/sources/{transcript['id']}", params={"project": project["id"], "limit": 10}, headers=headers).json()
    rest = client.get(f"/inspector/sources/{transcript['id']}", params={"project": project["id"], "offset": first["next_offset"]}, headers=headers).json()
    assert first["text"] + rest["text"] == raw
    normalized = client.get(f"/inspector/sources/{transcript['id']}", params={"project": project["id"], "view": "normalized"}, headers=headers).json()
    assert normalized["text"] == "Normalized message\nSecond line"
    sources = client.get("/inspector/sources", params={"project": project["id"]}, headers=headers).json()
    assert all("content" not in row and "normalized_text" not in json.dumps(row) for row in sources["items"])
    assert "OTHER_PROJECT_INSPECTOR_CANARY" not in json.dumps([page, second, detail.json(), history, sources])
    assert client.get(f"/inspector/records/{private_record['id']}", params={"project": project["id"]}, headers=headers).status_code == 404
    assert client.get("/inspector/records", params={"project": private["id"], "cursor": page["next_cursor"]}, headers=headers).status_code == 400
    assert normalized_export(store) == before


def test_graph_contains_unlinked_messages_technical_events_and_revisions_without_duplicate_sources(workspace):
    store, project, root, private, private_record, source, record, client, headers = workspace
    messages = []
    for role in ("user", "assistant", "tool", "metadata"):
        messages.append(store.snapshot(project["id"], "transcript://" + role, json.dumps({"role": role}), "transcript",
            {"role": role, "client": "codex", "session_id": "test-native-session", "normalized_text": role}))
    store.remember(project["id"], {"id": record["id"], "expected_version": 1, "content": "Updated decision."}, [source["id"]])
    inspector = Inspector(store)
    graph = inspector.graph(project["id"])
    ids = [row["id"] for row in graph["nodes"]]
    assert len(ids) == len(set(ids))
    assert ids.count("source:" + source["id"]) == 1
    by_id = {row["id"]: row for row in graph["nodes"]}
    assert all("source:" + row["id"] in by_id for row in messages)
    assert by_id["source:" + messages[-1]["id"]]["kind"] == "technical"
    assert by_id["source:" + messages[0]["id"]]["kind"] == "message"
    assert f"revision:record:{record['id']}:1" in by_id
    assert f"revision:record:{record['id']}:2" in by_id
    assert graph["counts"]["sources"] == 5
    assert {edge["from_id"] for edge in graph["edges"]} <= set(ids)
    assert {edge["to_id"] for edge in graph["edges"]} <= set(ids)
    adjacency = inspector.neighbors(project["id"], "source:" + messages[0]["id"])
    assert adjacency["total"] == 1 and adjacency["edges"][0]["kind"] == "contains"
    global_view = inspector.graph()
    assert {row["project_id"] for row in global_view["nodes"]} == {project["id"], private["id"]}
    assert global_view["edges"] == []


def test_bounded_graph_group_pages_address_every_message(workspace):
    store, project, root, private, private_record, source, record, client, headers = workspace
    expected = set()
    for index in range(9):
        row = store.snapshot(project["id"], f"transcript://part/{index}", f"Message {index}", "transcript",
                             {"role": "assistant", "client": "claude", "session_id": "large-session"})
        expected.add("source:" + row["id"])
    inspector = Inspector(store)
    found, cursor = set(), None
    while True:
        graph = inspector.graph(project["id"], group="session:claude:large-session", limit=3, page_cursor=cursor)
        assert len(graph["nodes"]) <= 3 and len(graph["edges"]) <= 5000
        found.update(row["id"] for row in graph["nodes"])
        cursor = graph["next_cursor"]
        if not cursor:
            break
    assert found == expected
    groups = inspector.groups(project["id"], limit=1)
    assert groups["total"] >= 2 and groups["next_cursor"]


def test_worktree_filters_and_imported_bindings_preserve_raw_provenance(workspace, tmp_path):
    store, project, root, private, private_record, source, record, client, headers = workspace
    alternate = tmp_path / "alternate"
    alternate.mkdir()
    store.add_project_root(project["id"], alternate)
    private_file = store.snapshot(project["id"], str(alternate / "private.py"), "private_other_worktree", "code",
                                  {"path": str(alternate / "private.py"), "root": str(alternate)})
    store.upsert_node({"id": "private-node", "project_id": project["id"], "kind": "file", "label": "private_other_worktree",
                       "source_id": private_file["id"], "metadata": {"path": str(alternate / "private.py")}})
    foreign = tmp_path / "foreign-machine"
    imported_source = store.snapshot(project["id"], "conversation://foreign", "Portable visible fact", "transcript",
                                      {"root": str(foreign), "worktree": str(foreign), "role": "user"})
    imported_record = store.remember(project["id"], {"kind": "fact", "content": "Portable visible fact",
        "metadata": {"worktree": str(foreign)}}, [imported_source["id"]])
    store.command("INSERT INTO memory_bindings CONTENT :row", {"row": {
        "id": str(uuid4()), "project_id": project["id"], "origin_root": str(foreign), "local_root": str(root)}})
    inspector = Inspector(store)
    graph = inspector.graph(project["id"], worktree=root)
    assert "private_other_worktree" not in json.dumps(graph)
    assert "source:" + private_file["id"] not in {row["id"] for row in graph["nodes"]}
    assert "record:" + imported_record["id"] in {row["id"] for row in graph["nodes"]}
    assert inspector.record(project["id"], imported_record["id"], root)["record"]["metadata"]["worktree"] == str(foreign)
    with pytest.raises(InspectorNotFound):
        inspector.source(project["id"], private_file["id"], worktree=root)


def test_reviews_are_atomic_audited_cas_and_never_fabricate_completion(workspace):
    store, project, root, private, private_record, source, record, client, headers = workspace
    inspector = Inspector(store)
    result = inspector.review(project["id"], record["id"], "confirm", 1)
    assert result["record"]["version"] == 2 and result["record"]["status"] == "confirmed"
    audit = store.get_source(result["audit_source_id"])
    assert audit["uri"].startswith("review://") and audit["kind"] == "manual"
    assert json.loads(audit["content"])["actor"] == "local_user"
    assert json.loads(audit["content"])["previous_version"] == 1
    before = normalized_export(store)
    with pytest.raises(ValueError, match="conflict"):
        inspector.review(project["id"], record["id"], "dispute", 1, reason="A concurrent stale click")
    assert normalized_export(store) == before
    response = client.post(f"/inspector/records/{record['id']}/review", json={"project": project["id"],
        "action": "dispute", "expected_version": 2}, headers=headers)
    assert response.status_code == 400 and normalized_export(store) == before
    disputed = inspector.review(project["id"], record["id"], "dispute", 2, reason="Needs independent evidence")
    assert disputed["record"]["status"] == "disputed"
    assert "test_passed" not in disputed["record"]["metadata"]
    assert "state" not in disputed["record"]["metadata"]


def test_managed_review_requires_both_record_and_plan_versions(workspace):
    store, project, root, private, private_record, source, record, client, headers = workspace
    saved = PlanManager(store).save(project["id"], {"title": "Reviewed plan", "goal": "Keep progress intact"},
        [{"key": "task", "content": "Implement the real feature", "metadata": {"requires_test": True}}],
        "inspector-managed", worktree=root, activate=True)
    task = saved["tasks"][0]
    inspector = Inspector(store)
    before = normalized_export(store)
    with pytest.raises(ValueError, match="plan version conflict"):
        inspector.review(project["id"], task["id"], "confirm", task["version"])
    assert normalized_export(store) == before
    updated = inspector.review(project["id"], task["id"], "confirm", task["version"],
                               expected_plan_version=saved["plan"]["version"])["record"]
    assert updated["metadata"]["state"] == "todo"
    assert updated["metadata"]["requires_test"] is True
    assert not store.query("SELECT FROM check_runs")
    assert PlanManager(store).get(project["id"], saved["plan"]["id"])["plan"]["version"] == saved["plan"]["version"] + 1


def test_http_auth_input_validation_and_finite_committed_events(workspace):
    store, project, root, private, private_record, source, record, client, headers = workspace
    assert client.get("/inspector/summary").status_code == 401
    assert client.get("/events", params={"once": "true"}).status_code == 401
    response = client.get("/events", params={"once": "true", "project": project["id"]}, headers=headers)
    assert response.status_code == 200 and "event: reset" in response.text
    before = Inspector(store).graph(project["id"])["cursor"]
    after = f"{before['epoch']}:{before['seq']}"
    stored = store.snapshot(project["id"], "event://fresh", "COMMITTED_SOURCE_CONTENT_NOT_IN_EVENT", "manual")
    response = client.get("/events", params={"once": "true", "project": project["id"], "after": after}, headers=headers)
    assert "event: change" in response.text and stored["id"] in response.text
    assert "COMMITTED_SOURCE_CONTENT_NOT_IN_EVENT" not in response.text
    current = Inspector(store).graph(project["id"])["cursor"]
    reconnected = client.get("/events", params={"once": "true", "project": project["id"], "after": after},
        headers={**headers, "Last-Event-ID": f"{current['epoch']}:{current['seq']}"})
    assert "event: change" not in reconnected.text
    assert client.get("/inspector/records", params={"project": project["id"], "limit": 0}, headers=headers).status_code == 400
    assert client.post(f"/inspector/records/{record['id']}/review", json={"project": project["id"],
        "action": "confirm", "expected_version": True}, headers=headers).status_code == 422
    assert client.post(f"/inspector/records/{record['id']}/review", json={"project": project["id"],
        "action": "confirm", "expected_version": 1, "actor": "human_override"}, headers=headers).status_code == 422


def test_plan_task_check_and_job_read_routes_are_paged_and_side_effect_free(workspace):
    store, project, root, private, private_record, source, record, client, headers = workspace
    saved = PlanManager(store).save(project["id"], {"title": "Inspection plan", "goal": "Read all evidence"},
        [{"key": "a", "content": "Inspect a task"}], "read-routes", worktree=root)
    task = saved["tasks"][0]
    job_id = store.enqueue("extract", project["id"], {"source_id": source["id"]}, "inspect-job")
    check_id = str(uuid4())
    store.command("INSERT INTO check_runs CONTENT :row", {"row": {"id": check_id, "project_id": project["id"],
        "task_id": task["id"], "worktree": str(root), "origin": "reported_fixture", "outcome": "completed",
        "profile_id": "unit", "passed": True, "returncode": 0, "source_ids": {"stdout": source["id"]}}})
    before = normalized_export(store)
    for collection in ("plans", "tasks", "checks", "jobs"):
        result = client.get("/inspector/" + collection, params={"project": project["id"], "worktree": str(root)}, headers=headers)
        assert result.status_code == 200, result.text
        assert result.json()["total"] == 1
    for collection, identifier in (("plans", saved["plan"]["id"]), ("checks", check_id), ("jobs", job_id)):
        result = client.get(f"/inspector/{collection}/{identifier}", params={"project": project["id"]}, headers=headers)
        assert result.status_code == 200 and result.json()["item"]["id"] == identifier
    history = client.get(f"/inspector/plans/{saved['plan']['id']}/revisions", params={"project": project["id"]}, headers=headers)
    assert history.status_code == 200 and history.json()["total"] == 1
    filtered = client.get("/inspector/tasks", params={"project": project["id"], "state": "needs_review"}, headers=headers)
    assert filtered.status_code == 200 and filtered.json()["items"] == []
    assert normalized_export(store) == before


def test_unassigned_inbox_is_visible_only_in_global_views_without_assignment(workspace):
    store, project, root, private, private_record, source, record, client, headers = workspace
    identifier = str(uuid4())
    raw = '{"unknown_conversation":"UNASSIGNED_LITERAL"}\n'
    store.command("INSERT INTO ingestion_inbox CONTENT :row", {"row": {"id": identifier, "raw": raw,
        "path": "/foreign/transcript.jsonl", "source_offset": 0, "sha256": "fixture-hash",
        "client": "codex", "cwd": "/unregistered", "reason": "No registered project matches working directory"}})
    before = normalized_export(store)
    page = client.get("/inspector/inbox", headers=headers).json()
    assert page["total"] == 1 and page["items"][0]["project_id"] is None
    assert "raw" not in page["items"][0]
    detail = client.get(f"/inspector/inbox/{identifier}", headers=headers).json()
    assert detail["text"] == raw and detail["source"]["project_id"] is None
    global_graph = client.get("/inspector/graph", headers=headers).json()
    assert any(row.get("entity_type") == "inbox_group" and row["count"] == 1 for row in global_graph["nodes"])
    children = client.get("/inspector/graph", params={"group": "inbox"}, headers=headers).json()
    assert "inbox:" + identifier in {row["id"] for row in children["nodes"]}
    project_graph = client.get("/inspector/graph", params={"project": project["id"]}, headers=headers).json()
    assert "inbox:" + identifier not in {row["id"] for row in project_graph["nodes"]}
    assert Inspector(store).summary()["totals"]["inbox"] == 1
    assert "inbox" not in Inspector(store).summary(project["id"])["totals"]
    assert normalized_export(store) == before


def test_graph_watermark_is_read_in_the_same_database_transaction(workspace, monkeypatch):
    from devbrain.journal import Journal

    store, project, root, private, private_record, source, record, client, headers = workspace
    original = Journal.cursor
    observed = []

    def same_reader(journal, reader=None):
        assert reader is getattr(store._local, "transaction", None)
        assert reader is not None
        observed.append(reader)
        return original(journal, reader=reader)

    monkeypatch.setattr(Journal, "cursor", same_reader)
    graph = Inspector(store).graph(project["id"])
    assert observed and graph["cursor"]["seq"] >= 1
