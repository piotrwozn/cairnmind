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
