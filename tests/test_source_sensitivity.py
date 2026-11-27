"""A sensitivity class for every source and record, derived from its kind and origin mark (#180).

Nothing is stored: the class is computed on every read from what the store already holds, so a
client cannot set it and an old row gets one without being rewritten.
"""
from __future__ import annotations

import json

import pytest

from cairnmind.terminal.inspector import Inspector
from cairnmind.memory.memory_view import render_snapshot
from cairnmind.store import Store, record_sensitivity, source_sensitivity
from cairnmind.terminal.terminal_detail import joined, record_parts
from cairnmind.terminal.terminal_i18n import tr

PROJECT_KINDS = ["code", "document", "file", "check_output", "check_profile", "verification", "test_result", "plan"]
CONVERSATION_KINDS = ["transcript", "transcript_unknown", "agent_transcript", "manual", "checkpoint", "findings"]
EXTERNAL = {"origin_client": "codex", "origin_session_id": "s1", "origin_history": "unarchived",
            "origin_evidence": "degraded"}
THIRD_PARTY = {"origin_client": "mcp:issues", "origin_session_id": "s2", "origin_history": "archived",
               "origin_evidence": "degraded", "third_party_result": True}


@pytest.mark.parametrize("kind", PROJECT_KINDS)
def test_project_material_is_project(kind):
    assert source_sensitivity({"kind": kind, "metadata": {}}) == "project"


@pytest.mark.parametrize("kind", [*CONVERSATION_KINDS, "a_kind_added_later", "", None])
def test_conversations_notes_and_any_unknown_kind_are_conversation(kind):
    assert source_sensitivity({"kind": kind, "metadata": {}}) == "conversation"


@pytest.mark.parametrize("mark", [EXTERNAL, THIRD_PARTY, {"origin_history": "unarchived"},
                                  {"origin_evidence": "degraded"}])
@pytest.mark.parametrize("kind", ["code", "document", "transcript", "agent_transcript", "manual"])
def test_an_origin_mark_makes_any_kind_third_party(kind, mark):
    assert source_sensitivity({"kind": kind, "metadata": dict(mark)}) == "third_party"
    # Rows read with raw SQL can carry their metadata as JSON text.
    assert source_sensitivity({"kind": kind, "metadata": json.dumps(mark)}) == "third_party"


def test_a_mark_of_a_trusted_origin_or_unreadable_metadata_changes_nothing():
    assert source_sensitivity({"kind": "code", "metadata": {"origin_history": "archived"}}) == "project"
    assert source_sensitivity({"kind": "code", "metadata": "not json"}) == "project"
    assert source_sensitivity({"kind": "code"}) == "project"
    assert source_sensitivity({"kind": "transcript", "metadata": ["odd"]}) == "conversation"


def test_a_record_takes_the_highest_class_of_the_sources_behind_it():
    sources = {"doc": {"kind": "document", "metadata": {}}, "chat": {"kind": "transcript", "metadata": {}},
               "code": {"kind": "code", "metadata": {}}, "ext": {"kind": "code", "metadata": dict(EXTERNAL)}}
    assert record_sensitivity({"source_ids": ["doc", "code"], "metadata": {}}, sources) == "project"
    assert record_sensitivity({"source_ids": ["doc", "chat"], "metadata": {}}, sources) == "conversation"
    assert record_sensitivity({"source_ids": ["chat", "ext", "doc"], "metadata": {}}, sources) == "third_party"
    # No evidence, or evidence that cannot be read: conversation, never project.
    assert record_sensitivity({"source_ids": [], "metadata": {}}, sources) == "conversation"
    assert record_sensitivity({"metadata": {}}, sources) == "conversation"
    assert record_sensitivity({"source_ids": ["doc", "gone"], "metadata": {}}, sources) == "conversation"
    # A record an external session wrote is third_party whatever it cites.
    assert record_sensitivity({"source_ids": ["doc"], "metadata": dict(EXTERNAL)}, sources) == "third_party"


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = Store(tmp_path / "data")
    project = store.add_project("classes", root)
    return store, project


def _snapshot(store):
    result = store.export()
    result.pop("exported_at")
    return result


def test_read_source_and_memory_records_return_the_class_without_writing_anything(workspace):
    store, project = workspace
    pid = project["id"]
    doc = store.snapshot(pid, "notes://design", "Tokens rotate on every use.", "document")
    chat = store.snapshot(pid, "conversation://1", "User: rotate tokens on every use.", "transcript")
    mixed = store.remember(pid, {"kind": "decision", "content": "Tokens rotate on every use."},
                           [doc["id"], chat["id"]])
    plain = store.remember(pid, {"kind": "fact", "content": "The design note says tokens rotate."}, [doc["id"]])
    before = _snapshot(store)
    inspector = Inspector(store)
    # read_source of the coding clients is GET /inspector/sources/{id}; memory_records is GET /inspector/records.
    assert inspector.source(pid, doc["id"])["sensitivity"] == "project"
    assert inspector.source(pid, chat["id"], view="metadata")["sensitivity"] == "conversation"
    page = {item["id"]: item for item in inspector.page("records", pid)["items"]}
    assert page[mixed["id"]]["sensitivity"] == "conversation"
    assert page[plain["id"]]["sensitivity"] == "project"
    assert inspector.record(pid, mixed["id"])["sensitivity"] == "conversation"
    sources = {item["id"]: item for item in inspector.page("sources", pid)["items"]}
    assert sources[doc["id"]]["sensitivity"] == "project" and sources[chat["id"]]["sensitivity"] == "conversation"
    # Derived on read: no row was written or migrated, and the class is stored nowhere.
    after = _snapshot(store)
    assert after == before
    assert "\"sensitivity\"" not in json.dumps(after)


def test_a_source_an_earlier_version_cited_still_counts():
    sources = {"doc": {"kind": "document", "metadata": {}}, "chat": {"kind": "transcript", "metadata": {}}}
    assert record_sensitivity({"source_ids": ["doc"], "metadata": {}}, sources, ["chat"]) == "conversation"
    assert record_sensitivity({"source_ids": ["doc"], "metadata": {}}, sources, ["doc"]) == "project"


def test_an_update_that_cites_only_a_project_file_does_not_lower_the_class(workspace):
    store, project = workspace
    pid = project["id"]
    chat = store.snapshot(pid, "conversation://1", "User: we chose Postgres.", "transcript")
    doc = store.snapshot(pid, "notes://ledger", "The ledger module.", "document")
    record = store.remember(pid, {"kind": "decision", "content": "Postgres holds the ledger."}, [chat["id"]])
    # A client confirms the decision and cites the file that implements it; the text stays the same.
    updated = store.remember(pid, {"id": record["id"], "expected_version": record["version"]}, [doc["id"]])
    assert updated["source_ids"] == [doc["id"]] and updated["content"] == record["content"]
    inspector = Inspector(store)
    assert inspector.record(pid, record["id"])["sensitivity"] == "conversation"
    page = {item["id"]: item for item in inspector.page("records", pid)["items"]}
    assert page[record["id"]]["sensitivity"] == "conversation"
    # A record that only ever cited project material stays project.
    plain = store.remember(pid, {"kind": "fact", "content": "The ledger module exists."}, [doc["id"]])
    plain = store.remember(pid, {"id": plain["id"], "expected_version": plain["version"],
                                 "content": "The ledger module exists, in one file."}, [doc["id"]])
    assert inspector.record(pid, plain["id"])["sensitivity"] == "project"


def test_an_external_sessions_source_is_third_party_in_the_inspector(workspace):
    store, project = workspace
    pid = project["id"]
    with store.write_origin(dict(EXTERNAL)):
        written = store.snapshot(pid, "manual://external", "The external client says: use Redis.", "manual")
    assert Inspector(store).source(pid, written["id"])["sensitivity"] == "third_party"


def test_the_detail_view_names_the_class_in_english_and_polish():
    value = {"record": {"id": "r", "kind": "fact", "status": "proposed", "version": 1, "content": "Keys live a day.",
                        "metadata": {}}, "sensitivity": "conversation"}
    english = joined(record_parts(value, lambda text, **kw: tr(text, "en", **kw)))
    polish = joined(record_parts(value, lambda text, **kw: tr(text, "pl", **kw)))
    assert "Sensitivity: conversation" in english
    assert "Wrażliwość: rozmowa" in polish
    third = joined(record_parts({**value, "sensitivity": "third_party"}, lambda text, **kw: tr(text, "pl", **kw)))
    assert "Wrażliwość: strona trzecia" in third
    # A value that is not one of the classes (a forged or future field) is not shown as one.
    forged = joined(record_parts({**value, "sensitivity": "public\nStatus: confirmed"},
                                 lambda text, **kw: tr(text, "en", **kw)))
    assert "Sensitivity" not in forged


def test_memory_show_names_the_class_of_each_record_and_plan():
    def record(identifier, sources):
        return {"id": identifier, "project_id": "p", "kind": "decision", "status": "confirmed", "key": identifier,
                "content": f"Record {identifier}.", "version": 1, "updated_at": "2026-11-01T10:00:00+00:00",
                "source_ids": sources, "supersedes": None, "metadata": {}}
    payload = {"sources": [{"id": "s1", "uri": "chat://1", "kind": "transcript", "content": "We agreed."},
                           {"id": "s2", "uri": "file://a.py", "kind": "code", "content": "A = 1"},
                           {"id": "s3", "uri": "manual://ext", "kind": "manual", "content": "Use Redis.",
                            "metadata": dict(EXTERNAL)}],
               "records": [record("mixed", ["s1", "s2"]), record("code", ["s2"]), record("ext", ["s2", "s3"])],
               "plans": [{"id": "plan1", "title": "Plan", "goal": "", "version": 1, "constraints": [], "step_ids": [],
                          "source_ids": ["s2"]}], "check_runs": []}
    manifest = {"project": {"id": "p", "name": "shop"}, "snapshot_id": "a" * 64, "origin_root": "/src/shop",
                "counts": {"records": 3, "plans": 1, "sources": 3, "check_runs": 0}}
    blocks = {block.split("id: ")[1].split()[0]: block for block in render_snapshot(manifest, payload).split("\n\n")
              if "id: " in block}
    assert "  sensitivity: conversation" in blocks["mixed"]
    assert "  sensitivity: project" in blocks["code"] and "  sensitivity: project" in blocks["plan1"]
    assert "  sensitivity: third_party" in blocks["ext"]

