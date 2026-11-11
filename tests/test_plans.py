from concurrent.futures import ThreadPoolExecutor
import json
from uuid import UUID

import pytest

from cairnmind.plans import PlanManager
from cairnmind.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "memory")


@pytest.fixture
def project(store, tmp_path):
    return store.add_project("example", tmp_path / "repo")


@pytest.fixture
def manager(store):
    return PlanManager(store)


def definitions():
    # Deliberately put the dependent first: input order is the plan order, not
    # a constraint on native transactional persistence.
    return [{"key": "ship", "content": "Ship API", "depends_on": ["design"]},
            {"key": "design", "content": "Design API"}]


def save(manager, project, **kwargs):
    return manager.save(project["id"], {"title": "API", "goal": "A usable API",
                                         "constraints": ["Keep public compatibility"]},
                        definitions(), "initial", **kwargs)


def versions(result):
    return {task["id"]: task["version"] for task in result["tasks"]}


def by_key(result):
    return {task["metadata"]["step_key"]: task for task in result["tasks"]}


def progress(store, project, result, key, state):
    task = by_key(result)[key]
    return store.remember(project["id"], {"id": task["id"], "expected_version": task["version"],
        "expected_plan_version": result["plan"]["version"], "metadata": {"state": state}}, task["source_ids"])


def test_atomic_plan_remaps_keys_preserves_input_order_and_native_graph(manager, store, project):
    result = save(manager, project, activate=True)
    tasks = by_key(result)
    assert [task["metadata"]["step_key"] for task in result["tasks"]] == ["ship", "design"]
    assert tasks["ship"]["metadata"]["depends_on"] == [tasks["design"]["id"]]
    assert all(task["metadata"]["state"] == "todo" and UUID(task["id"]) for task in result["tasks"])
    assert result["plan"]["version"] == 1
    assert manager.get(project["id"], result["plan"]["id"])["tasks"] == result["tasks"]
    assert manager.list(project["id"]) == [result["plan"]]
    source = store.get_source(result["plan"]["source_ids"][0])
    assert json.loads(source["content"])["steps"] == definitions()
    assert source["kind"] == "plan" and all(source["id"] in task["source_ids"] for task in result["tasks"])
    assert len(store.query("SELECT id FROM edges WHERE kind='has_step'")) == 2
    edge = store.query("SELECT from_id,to_id FROM edges WHERE kind='depends_on'")[0]
    assert edge["from_id"] == f"record:{tasks['ship']['id']}"
    assert edge["to_id"] == f"record:{tasks['design']['id']}"
    assert manager.next_task(project["id"])["task"]["id"] == tasks["design"]["id"]


def test_idempotency_replays_original_even_after_progress_and_rejects_changed_payload(manager, store, project):
    original = save(manager, project)
    progress(store, project, original, "design", "in_progress")
    replay = save(manager, project)
    assert replay == {**original, "replayed": True}
    assert len(store.query("SELECT id FROM plans")) == 1
    assert len(store.query("SELECT id FROM sources")) == 1
    with pytest.raises(ValueError, match="different payload"):
        manager.save(project["id"], {"title": "Changed"}, definitions(), "initial")


@pytest.mark.parametrize("steps,error", [
    ([{"key": "a", "content": "A"}, {"key": "a", "content": "B"}], "unique"),
    ([{"key": "a", "content": "A", "depends_on": ["missing"]}], "final plan"),
    ([{"key": "a", "content": "A", "depends_on": ["b"]},
      {"key": "b", "content": "B", "depends_on": ["a"]}], "cycle"),
    ([{"key": "a", "content": "A", "metadata": {"state": "done"}}], "todo"),
    ([{"key": "a", "content": "A", "metadata": {"test_passed": True}}], "evidence"),
    ([{"key": "a", "content": "A", "metadata": {"check_run_ids": ["fake"]}}], "evidence"),
    ([{"key": "a", "content": "A", "metadata": {"worktree": "/another"}}], "Managed"),
])
def test_invalid_final_plan_leaves_no_partial_state(manager, store, project, steps, error):
    with pytest.raises(ValueError, match=error):
        manager.save(project["id"], {"title": "Bad"}, steps, "bad")
    for collection in ("plans", "records", "sources", "plan_revisions", "plan_requests", "nodes", "edges"):
        assert not store.query(f"SELECT id FROM {collection}")


def test_mid_batch_error_rolls_back_sources_records_graph_and_activation(manager, store, project, monkeypatch):
    original = store._remember
    count = 0

    def fail_second(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("Injected mid-batch failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "_remember", fail_second)
    with pytest.raises(RuntimeError):
        save(manager, project, activate=True)
    for collection in ("plans", "records", "sources", "record_revisions", "plan_revisions", "plan_requests", "worktree_states", "nodes", "edges"):
        assert not store.query(f"SELECT id FROM {collection}")


def test_valid_dependency_reversal_and_key_swap_are_validated_as_final_state(manager, store, project):
    first = save(manager, project)
    old = by_key(first)
    reversed_steps = [{"key": "ship", "content": "Ship API"},
                      {"key": "design", "content": "Design API", "depends_on": ["ship"]}]
    second = manager.save(project["id"], {"id": first["plan"]["id"]}, reversed_steps, "reversal",
                          expected_version=1, task_versions=versions(first))
    assert {k: t["id"] for k, t in by_key(second).items()} == {k: t["id"] for k, t in old.items()}
    assert by_key(second)["design"]["metadata"]["depends_on"] == [old["ship"]["id"]]
    swapped = [{"id": old["ship"]["id"], "key": "design", "content": "Ship API"},
               {"id": old["design"]["id"], "key": "ship", "content": "Design API"}]
    third = manager.save(project["id"], {"id": first["plan"]["id"]}, swapped, "swap",
                         expected_version=2, task_versions=versions(second))
    assert by_key(third)["design"]["id"] == old["ship"]["id"]
    assert len(store.record_history(old["ship"]["id"])) == 3
    assert [row["version"] for row in store.query("SELECT version FROM plan_revisions ORDER BY version")] == [1, 2, 3]


def test_progress_plan_cas_is_atomic_and_definition_updates_preserve_done(manager, store, project):
    first = save(manager, project)
    done = progress(store, project, first, "design", "done")
    current = manager.get(project["id"], first["plan"]["id"])
    assert done["version"] == 2 and current["plan"]["version"] == 2
    ship = by_key(current)["ship"]
    with pytest.raises(ValueError, match="Plan version conflict"):
        store.remember(project["id"], {"id": ship["id"], "expected_version": 1,
            "expected_plan_version": 1, "metadata": {"state": "in_progress"}}, ship["source_ids"])
    assert manager.get(project["id"], first["plan"]["id"]) == current
    assert len(store.record_history(ship["id"])) == 1
    updated = manager.save(project["id"], {"id": first["plan"]["id"], "goal": "Changed goal"},
                           [{"key": "ship", "content": "Ship API", "depends_on": ["design"]},
                            {"key": "design", "content": "Design a revised API"}], "changed",
                           expected_version=2, task_versions=versions(current))
    assert by_key(updated)["design"]["metadata"]["state"] == "done"
    assert by_key(updated)["design"]["metadata"]["plan_context_hash"] != done["metadata"]["plan_context_hash"]
    history = store.query("SELECT snapshot FROM plan_revisions WHERE plan_id=:id ORDER BY version", {"id": first["plan"]["id"]})
    assert history[1]["snapshot"]["tasks"][1]["metadata"]["state"] == "done"


def test_concurrent_clients_cannot_overwrite_plan_or_other_task_progress(manager, store, project):
    first = save(manager, project)

    def update(client):
        try:
            return manager.save(project["id"], {"id": first["plan"]["id"], "goal": client},
                                definitions(), client, expected_version=1, task_versions=versions(first))["plan"]["version"]
        except ValueError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ["codex", "claude"]))
    assert results.count(2) == 1 and results.count("conflict") == 1
    assert len(store.query("SELECT id FROM plan_revisions")) == 2
    assert len(store.query("SELECT id FROM plan_requests")) == 2
    current = manager.get(project["id"], first["plan"]["id"])
    stale = versions(first)
    with pytest.raises(ValueError, match="Task version conflict"):
        manager.save(project["id"], {"id": first["plan"]["id"]}, definitions(), "stale-task",
                     expected_version=2, task_versions=stale)
    assert manager.get(project["id"], first["plan"]["id"]) == current


def test_removed_step_is_archived_with_history_and_stale_plan_cannot_erase_progress(manager, store, project):
    first = save(manager, project)
    removed = by_key(first)["ship"]
    second = manager.save(project["id"], {"id": first["plan"]["id"]},
                          [{"key": "design", "content": "Design API"}], "remove",
                          expected_version=1, task_versions=versions(first))
    assert second["plan"]["step_ids"] == [by_key(first)["design"]["id"]]
    history = store.record_history(removed["id"])
    assert len(history) == 2 and history[-1]["status"] == "superseded"
    assert history[-1]["metadata"]["plan_archived"] is True
    assert not store.query("SELECT id FROM edges WHERE kind='has_step' AND to_id=:id", {"id": f"record:{removed['id']}"})
    with pytest.raises(ValueError, match="Plan version conflict"):
        manager.save(project["id"], {"id": first["plan"]["id"]}, [], "stale",
                     expected_version=1, task_versions=versions(first))


def test_worktree_activation_is_explicit_project_scoped_and_independent(manager, store, project, tmp_path):
    first = save(manager, project)
    assert manager.next_task(project["id"])["reason"] == "no_active_plan"
    second = manager.save(project["id"], {"title": "Second"}, [{"key": "other", "content": "Other"}], "second")
    other_root = tmp_path / "worktree"
    store.add_project_root(project["id"], other_root)
    root_state = manager.activate(project["id"], first["plan"]["id"])
    manager.activate(project["id"], second["plan"]["id"], other_root)
    assert manager.next_task(project["id"])["plan"]["id"] == first["plan"]["id"]
    assert manager.next_task(project["id"], worktree=other_root)["plan"]["id"] == second["plan"]["id"]
    assert manager.activate(project["id"], first["plan"]["id"]) == root_state
    other = store.add_project("other", tmp_path / "other")
    with pytest.raises(ValueError):
        manager.activate(other["id"], first["plan"]["id"])
    with pytest.raises(ValueError, match="registered"):
        manager.next_task(project["id"], worktree=tmp_path / "unregistered")


def test_next_task_tracks_blockers_test_review_and_completed_without_reservations(manager, store, project):
    first = save(manager, project, activate=True)
    next_item = manager.next_task(project["id"])
    assert next_item["task"]["id"] == by_key(first)["design"]["id"]
    assert next_item["blocked"][0]["task_id"] == by_key(first)["ship"]["id"]
    progress(store, project, first, "design", "blocked")
    assert manager.next_task(project["id"])["reason"] == "blocked"
    current = manager.get(project["id"], first["plan"]["id"])
    progress(store, project, current, "design", "needs_test")
    assert manager.next_task(project["id"])["reason"] == "needs_test"
    current = manager.get(project["id"], first["plan"]["id"])
    progress(store, project, current, "design", "needs_review")
    assert manager.next_task(project["id"])["reason"] == "needs_review"
    for key in ("design", "ship"):
        current = manager.get(project["id"], first["plan"]["id"])
        progress(store, project, current, key, "done")
    assert manager.next_task(project["id"])["reason"] == "completed"


def test_legacy_groups_are_not_reinterpreted_until_explicit_adoption(manager, store, project):
    source = store.snapshot(project["id"], "legacy://plan", "Old accepted plan")
    task = store.remember(project["id"], {"kind": "task", "key": "legacy-a", "content": "Old task",
        "metadata": {"plan_id": "old-group", "state": "done", "test_passed": True,
                     "test_source_ids": [source["id"]], "worktree": "/legacy-scope"}}, [source["id"]])
    assert manager.list(project["id"]) == []
    result = manager.adopt_legacy(project["id"], "old-group", idempotency_key="adopt", activate=True)
    adopted = result["tasks"][0]
    assert adopted["id"] == task["id"] and adopted["metadata"]["state"] == "done"
    assert adopted["metadata"]["test_passed"] is True
    assert adopted["metadata"]["plan_id"] == "old-group"
    assert "worktree" not in adopted["metadata"]
    assert adopted["metadata"]["plan_ref"] == result["plan"]["id"]
    assert not store.query("SELECT id FROM check_runs")
    assert manager.adopt_legacy(project["id"], "old-group", idempotency_key="adopt", activate=True) == {**result, "replayed": True}


def test_cross_plan_task_injection_and_external_dangling_dependencies_are_rejected(manager, store, project):
    first = save(manager, project)
    design = by_key(first)["design"]
    with pytest.raises(ValueError, match="existing member"):
        manager.save(project["id"], {"title": "Another"}, [{"id": design["id"], "key": "x", "content": "X"}], "inject")
    source = store.snapshot(project["id"], "external://dependent", "Outside task")
    store.remember(project["id"], {"kind": "task", "content": "External", "metadata": {"depends_on": [design["id"]]}}, [source["id"]])
    with pytest.raises(ValueError, match="outside this plan"):
        manager.save(project["id"], {"id": first["plan"]["id"]}, [], "remove-all",
                     expected_version=1, task_versions=versions(first))
    assert manager.get(project["id"], first["plan"]["id"])["tasks"] == first["tasks"]
