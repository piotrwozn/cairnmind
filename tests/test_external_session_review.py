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


def test_an_external_plan_and_task_reach_a_native_session_marked(fixture):
    from cairnmind.mcp_server import _next_task_view, _plan_header
    from cairnmind.plans import PlanManager
    from cairnmind.retrieval import Retriever
    from cairnmind.sessions import format_packet

    native = fixture.manager.prepare(fixture.project["id"], "codex", timeout=0)["packet"]
    assert format_packet(native).startswith("Cairnmind verified resume bootstrap")
    save_plan(fixture, fixture.session("external"), "editor-plan", [{"key": "one", "content": "Editor task"}],
              plan={"title": "Editor plan", "goal": "Written in Cursor"}, activate=True)
    packet = fixture.manager.prepare(fixture.project["id"], "codex", timeout=0)["packet"]
    for view in (packet["active_plan"], packet["active_task"]):
        assert (view["origin_client"], view["origin_evidence"]) == ("external", "degraded")
    text = format_packet(packet)
    first, lines = text.splitlines()[0], text.splitlines()
    assert "verified resume" not in first and "external client's session and is not verified" in first
    note = "[written by an external client's session; not verified]"
    assert any(line.startswith("Active plan v") and note in line for line in lines)
    assert any(line.startswith("Active task ") and note in line for line in lines)
    following = PlanManager(fixture.store).next_task(fixture.project["id"], worktree=str(fixture.root))
    assert _plan_header(following["plan"])["origin_evidence"] == "degraded"
    assert _next_task_view(following)["task"]["metadata"]["origin_evidence"] == "degraded"
    workflow = Retriever(fixture.store, fixture.settings, object()).workflow(fixture.project["id"], str(fixture.root))
    assert workflow["plan"]["origin_evidence"] == "degraded"
    assert workflow["next_task"]["metadata"]["origin_evidence"] == "degraded"


def test_a_search_hit_from_an_external_source_carries_the_mark(fixture):
    from cairnmind.agent import KnowledgeAgent
    from cairnmind.ingest import index_pending_sources
    from cairnmind.retrieval import Retriever

    checkpoint(fixture, "external", {"summary": "zephyrquill migration was decided in the editor"})
    fixture.store.snapshot(fixture.project["id"], "manual://native-zephyrquill", "zephyrquill notes by a person",
                           "manual")
    index_pending_sources(fixture.store)
    retriever = Retriever(fixture.store, fixture.settings, object())
    hits = retriever.search(fixture.project["id"], "zephyrquill")
    by_uri = {hit["uri"]: hit for hit in hits}
    external = next(hit for uri, hit in by_uri.items() if uri.startswith("checkpoint://"))
    assert external["metadata"]["origin_evidence"] == "degraded"
    assert "origin_evidence" not in (by_uri["manual://native-zephyrquill"].get("metadata") or {})
    # The chunk itself stays unmarked: the mark is read from the source when the hit is made.
    chunk = fixture.store.query("SELECT FROM chunks WHERE id=:id", {"id": external["id"]})[0]
    assert "origin_evidence" not in (chunk.get("metadata") or {})
    agent = KnowledgeAgent(fixture.store, fixture.settings, object(), retriever)
    shown = {hit["source_id"]: hit for hit in agent._hits(hits, fixture.project["id"], {})}
    assert shown[external["source_id"]]["metadata"]["origin_evidence"] == "degraded"
