"""Where an external session's mark must follow, and where it must not linger.

Real HTTP service, ArcadeDB and Git; no model is asked.
"""
from cairnmind.store import ORIGIN_METADATA
from test_external_session_writes import fixture as fixture, post


def checkpoint(fixture, client, state):
    return post(fixture, "/checkpoint", {"project": fixture.project["id"], **fixture.session(client), "state": state})


def test_a_native_checkpoint_after_an_external_one_is_not_marked(fixture):
    from cairnmind.brief import build_brief
    from cairnmind.evidence import evidence_states

    external = checkpoint(fixture, "external", {"summary": "From an external editor", "blockers": ["external"]})
    assert fixture.record(external["id"])["metadata"]["origin_evidence"] == "degraded"
    native = checkpoint(fixture, "codex", {"summary": "From a native client"})
    assert native["id"] == external["id"] and native["version"] == external["version"] + 1
    record = fixture.record(native["id"])
    assert not set(ORIGIN_METADATA) & set(record["metadata"])
    # Known merge behaviour of the one checkpoint record: a state key the later checkpoint does not name
    # stays in the metadata, while the content is exactly the latest checkpoint.
    assert record["metadata"]["blockers"] == ["external"] and "blockers" not in record["content"]
    assert evidence_states(fixture.store, fixture.project["id"], [record])[record["id"]]["evidence_state"] == "anchored"
    assert not any("external client" in line["text"] for line in build_brief(fixture.store, fixture.project["id"])["lines"])
    packet = fixture.manager.prepare(fixture.project["id"], "claude", timeout=0)["packet"]
    shown = next(row for row in packet["records"] if row["id"] == native["id"])
    assert "evidence_state" not in shown
    # And an external checkpoint after a native one is marked again.
    again = checkpoint(fixture, "external", {"summary": "External again"})
    assert fixture.record(again["id"])["metadata"]["origin_client"] == "external"


def save_plan(fixture, bound, key, steps, plan=None, **extra):
    body = {"project": fixture.project["id"], "idempotency_key": key, "steps": steps,
            "plan": plan or {"title": "Shared plan", "goal": "Ship it"}, **(bound or {}), **extra}
    return post(fixture, "/plans/save", body)


def without_evidence_time(metadata):
    # Every save cites its new plan snapshot, so the evidence time of each step moves, native or not.
    return {key: value for key, value in metadata.items() if not key.startswith("evidence_")}


def test_an_external_plan_save_marks_only_what_it_changes(fixture):
    native = save_plan(fixture, None, "native", [{"key": "a", "content": "Step A"}, {"key": "b", "content": "Step B"},
                                                  {"key": "c", "content": "Step C"}])
    tasks = {task["metadata"]["step_key"]: task for task in native["tasks"]}
    plan = native["plan"]
    unchanged = save_plan(fixture, fixture.session("external"), "same",
                          [{"key": "a", "content": "Step A"}, {"key": "b", "content": "Step B"},
                           {"key": "c", "content": "Step C"}],
                          plan={"id": plan["id"]}, expected_version=plan["version"],
                          task_versions={task["id"]: task["version"] for task in tasks.values()})
    stored = fixture.store.query("SELECT FROM plans WHERE id=:id", {"id": plan["id"]})[0]
    assert not set(ORIGIN_METADATA) & set(stored)
    assert all(not set(ORIGIN_METADATA) & set(fixture.record(task["id"])["metadata"]) for task in unchanged["tasks"])

    current = {task["metadata"]["step_key"]: task for task in unchanged["tasks"]}
    before_b = fixture.record(current["b"]["id"])
    changed = save_plan(fixture, fixture.session("external"), "edit",
                        [{"key": "a", "content": "Step A, rewritten in the editor"}, {"key": "b", "content": "Step B"},
                         {"key": "d", "content": "Step D, new"}],
                        plan={"id": plan["id"]}, expected_version=unchanged["plan"]["version"],
                        task_versions={task["id"]: task["version"] for task in current.values()})
    after = {task["metadata"]["step_key"]: task for task in changed["tasks"]}
    assert fixture.record(after["a"]["id"])["metadata"]["origin_evidence"] == "degraded"
    assert fixture.record(after["d"]["id"])["metadata"]["origin_evidence"] == "degraded"
    after_b = fixture.record(after["b"]["id"])
    assert without_evidence_time(after_b["metadata"]) == without_evidence_time(before_b["metadata"])
    removed = fixture.record(current["c"]["id"])
    assert removed["status"] == "superseded" and not set(ORIGIN_METADATA) & set(removed["metadata"])
    stored = fixture.store.query("SELECT FROM plans WHERE id=:id", {"id": plan["id"]})[0]
    assert stored["origin_evidence"] == "degraded"
