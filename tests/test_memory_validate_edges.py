"""Edge-case validation tests for memory.py (_validate, _canonical_graph, _read, _parse)."""
from __future__ import annotations

import hashlib
import json

import pytest

from cairnmind.memory import memory


def _valid_payload(project_id="p1"):
    src_text = "fact evidence"
    src_sha = hashlib.sha256(src_text.encode()).hexdigest()
    source = {
        "id": "s1", "project_id": project_id, "uri": "file://a.py", "kind": "file",
        "content": src_text, "sha256": src_sha, "metadata": {}, "is_current": True,
    }
    rec = {
        "id": "r1", "project_id": project_id, "kind": "task", "status": "proposed",
        "key": "task-1", "content": "Do thing", "version": 1, "source_ids": ["s1"],
        "metadata": {"state": "todo", "acceptance_criteria": ["c1"], "required_profiles": ["test"]},
    }
    plan = {
        "id": "pl1", "project_id": project_id, "version": 1, "title": "Plan 1",
        "goal": "Goal", "constraints": ["no break"], "source_ids": ["s1"], "step_ids": ["r1"],
    }
    return {
        "sources": [source],
        "records": [rec],
        "plans": [plan],
        "record_revisions": [{"id": "r1:1", "project_id": project_id, "record_id": "r1", "version": 1, "snapshot": dict(rec)}],
        "plan_revisions": [{"id": "pl1:1", "project_id": project_id, "plan_id": "pl1", "version": 1,
                            "snapshot": {"plan": dict(plan), "tasks": [dict(rec)]}}],
        "nodes": [
            {"id": "source:s1", "project_id": project_id, "kind": "file", "label": "a.py", "metadata": {}},
            {"id": "record:r1", "project_id": project_id, "kind": "task", "label": "Do thing", "source_id": "s1", "metadata": {}},
            {"id": "plan:pl1", "project_id": project_id, "kind": "plan", "label": "Plan 1", "source_id": "s1", "metadata": {}},
        ],
        "edges": [
            {"id": "e1", "project_id": project_id, "from_id": "record:r1", "kind": "supported_by", "to_id": "source:s1", "source_id": "s1"},
            {"id": "e2", "project_id": project_id, "from_id": "plan:pl1", "kind": "supported_by", "to_id": "source:s1", "source_id": "s1"},
            {"id": "e3", "project_id": project_id, "from_id": "plan:pl1", "kind": "has_step", "to_id": "record:r1", "source_id": "s1"},
        ],
        "check_runs": [],
    }


def test_memory_read_parse_and_validate_error_branches(tmp_path):
    # _unique_pairs rejects duplicate keys; _read rejects oversized file; _text/_version reject invalid values
    with pytest.raises(ValueError, match="Duplicate JSON property"):
        memory._parse(b'{"a": 1, "a": 2}')

    big = tmp_path / "big.json"
    big.write_bytes(b"x" * 20)
    with pytest.raises(ValueError, match="byte budget"):
        memory._read(big, 10)

    with pytest.raises(ValueError, match="Invalid memory label"):
        memory._text("   ", "label")
    with pytest.raises(ValueError, match="entity version"):
        memory._version(0)

    # Baseline valid payload succeeds
    assert memory._validate(_valid_payload(), "p1", active_plan_id="pl1")

    def mutate_and_expect(mutator, match, *, active_plan_id=None, graph=True):
        p = json.loads(json.dumps(_valid_payload()))
        mutator(p)
        with pytest.raises(ValueError, match=match):
            memory._validate(p, "p1", active_plan_id=active_plan_id, graph=graph)

    mutate_and_expect(lambda p: p.__setitem__("records", "not-a-list"), "Invalid memory collection")
    mutate_and_expect(lambda p: p["records"].append("not-a-dict"), "Invalid memory entity")
    mutate_and_expect(lambda p: p["records"][0].__setitem__("kind", "bad"), "Invalid memory record")
    mutate_and_expect(lambda p: p["records"][0].__setitem__("content", "   "), "record content")
    mutate_and_expect(lambda p: p["records"][0].__setitem__("source_ids", []), "require evidence")
    mutate_and_expect(lambda p: p["records"][0].__setitem__("metadata", "bad"), "record metadata")
    mutate_and_expect(lambda p: p["records"][0].__setitem__("supersedes", "missing"), "Missing superseded")
    mutate_and_expect(lambda p: p["records"][0]["metadata"].__setitem__("requires_test", "yes"), "task boolean")
    mutate_and_expect(lambda p: p["records"][0]["metadata"].__setitem__("acceptance_criteria", [""]), "acceptance criteria")
    mutate_and_expect(lambda p: p["records"][0]["metadata"].__setitem__("required_profiles", [""]), "check profiles")
    mutate_and_expect(lambda p: p["records"][0]["metadata"].__setitem__("criterion_checks", {"bad": "shape"}), "criterion checks")
    mutate_and_expect(lambda p: p["records"][0]["metadata"].__setitem__("step_index", -1), "step index")
    mutate_and_expect(lambda p: p["records"][0]["metadata"].__setitem__("depends_on", ["missing"]), "task dependency")
    mutate_and_expect(lambda p: p["records"][0]["metadata"].__setitem__("plan_ref", "missing"), "managed memory plan")
    mutate_and_expect(lambda p: p["records"][0]["metadata"].__setitem__("check_run_ids", ["missing"]), "check evidence")
    mutate_and_expect(lambda p: p["plans"][0].__setitem__("title", "  "), "plan definition")
    mutate_and_expect(lambda p: p["plans"][0].__setitem__("step_ids", ["missing"]), "plan step")
    mutate_and_expect(lambda p: p["sources"].append({**p["sources"][0], "id": "s2"}), "competing current snapshots")
    mutate_and_expect(lambda p: p["record_revisions"][0].__setitem__("id", "wrong:1"), "revision identity")
    mutate_and_expect(lambda p: p["plan_revisions"][0].__setitem__("snapshot", "bad"), "plan revision")
    mutate_and_expect(
        lambda p: p["plan_revisions"][0]["snapshot"]["tasks"][0].__setitem__("content", "tampered"),
        "immutable record revision",
    )
    mutate_and_expect(
        lambda p: p["plan_revisions"][0]["snapshot"]["plan"].__setitem__("step_ids", []),
        "step order",
    )
    mutate_and_expect(
        lambda p: p["record_revisions"][0]["snapshot"].__setitem__("id", "other"),
        "match its owner",
    )
    mutate_and_expect(lambda p: p["nodes"][0].__setitem__("metadata", "bad"), "graph node")
    mutate_and_expect(lambda p: p["nodes"][1].__setitem__("label", "wrong"), "record graph")
    mutate_and_expect(lambda p: p["nodes"][2].__setitem__("label", "wrong"), "plan graph")
    mutate_and_expect(lambda p: p["edges"].pop(), "Canonical memory relationships")
    mutate_and_expect(lambda p: p["check_runs"].append({"id": "cr1", "project_id": "p1", "task_id": "missing"}), "historical memory check")
    mutate_and_expect(lambda p: None, "Missing active memory plan", active_plan_id="missing")
