"""The snapshot text view: pure rendering, with no store, engine or service."""
from __future__ import annotations

import pytest

from devbrain.memory_view import render_snapshot


def _record(identifier, kind, content, *, status="confirmed", sources=("s1",), updated="2026-11-01T10:00:00+00:00",
            **metadata):
    return {"id": identifier, "project_id": "p", "kind": kind, "status": status, "key": f"key-{identifier}",
            "content": content, "version": 2, "updated_at": updated, "source_ids": list(sources), "metadata": metadata}


@pytest.fixture
def snapshot():
    payload = {
        "sources": [{"id": "s1", "uri": "chat://session/1", "kind": "conversation"},
                    {"id": "s2", "uri": "file://src/login.py", "kind": "code"}],
        "records": [
            _record("d1", "decision", "Login errors are handled in auth/errors.py.", sources=("s1", "s2")),
            _record("f1", "fact", "The release identifier is zephyr_214.", status="proposed"),
            _record("t1", "task", "Add the retry limit\nwith a test.", state="in_progress", test_passed=True),
            _record("t2", "task", "Document the retry limit.", depends_on=["t1"]),
        ],
        "plans": [{"id": "plan1", "title": "Harden login", "goal": "No endless retries.", "version": 3,
                   "constraints": ["Keep the public API."], "step_ids": ["t1", "t2"], "source_ids": ["s1"]}],
        "check_runs": [{"id": "run1", "task_id": "t1", "profile_id": "unit", "passed": True, "outcome": "completed",
                        "returncode": 0, "ended_at": "2026-11-01T11:00:00+00:00", "origin": "local_runner"},
                       {"id": "run0", "task_id": "t1", "profile_id": "unit", "passed": False, "outcome": "completed",
                        "returncode": 1, "ended_at": "2026-11-01T09:00:00+00:00", "origin": "local_runner"}],
    }
    manifest = {"project": {"id": "p", "name": "shop"}, "snapshot_id": "a" * 64, "created_at": "2026-11-02T08:00:00+00:00",
                "origin_root": "/home/ana/src/shop", "active_plan_id": "plan1",
                "counts": {"records": 4, "plans": 1, "sources": 2, "check_runs": 2},
                "completeness": {"pending_analysis": 1}}
    return manifest, payload


def test_header_names_the_snapshot_and_says_the_view_is_read_only(snapshot):
    text = render_snapshot(*snapshot)
    assert text.splitlines()[0] == "Portable memory snapshot (read-only view; nothing is imported or written)"
    assert "project:   shop (p)" in text
    assert f"snapshot:  {'a' * 64}" in text
    assert "created:   2026-11-02T08:00:00+00:00" in text
    assert "origin:    /home/ana/src/shop" in text
    assert "contains:  4 records, 1 plan, 2 sources, 2 historical check runs" in text
    assert "note:      1 source still waited for analysis" in text
    assert "filters:" not in text


def test_records_show_kind_status_version_time_and_source_uris(snapshot):
    text = render_snapshot(*snapshot)
    assert "Records (4 shown of 4)" in text
    assert "[decision] confirmed, version 2, updated 2026-11-01T10:00:00+00:00" in text
    assert "  | Login errors are handled in auth/errors.py." in text
    assert "  id: d1  key: key-d1" in text
    assert "  sources: chat://session/1, file://src/login.py" in text
    assert text.index("[decision]") < text.index("[task]") < text.index("[fact]")


def test_kind_status_and_grep_filters_combine(snapshot):
    manifest, payload = snapshot
    text = render_snapshot(manifest, payload, kind="decision", grep="LOGIN")
    assert "Records (1 shown of 4)" in text and "auth/errors.py" in text
    assert "zephyr_214" not in text and "Plans" not in text
    assert "filters:   kind=decision grep=LOGIN" in text
    assert "zephyr_214" in render_snapshot(manifest, payload, status="proposed")
    assert "auth/errors.py" not in render_snapshot(manifest, payload, status="proposed")
    # A source URI is searchable, because "which records came from this file" is a real question.
    assert "Records (1 shown of 4)" in render_snapshot(manifest, payload, grep="src/login.py")
    assert "Nothing in this snapshot matches the filters." in render_snapshot(manifest, payload, grep="absent-word")


def test_plan_lists_ordered_steps_with_their_states(snapshot):
    manifest, payload = snapshot
    text = render_snapshot(manifest, payload, kind="plan")
    assert "Records" not in text
    assert "[plan] Harden login, version 3, active in the origin worktree" in text
    assert "  | No endless retries." in text and "  constraint: Keep the public API." in text
    assert "  1. [in_progress] Add the retry limit (t1)" in text
    assert "  2. [todo] Document the retry limit. (t2)" in text
    assert "Plans (1 shown of 1)" in render_snapshot(manifest, payload, grep="document the retry")
    assert "Plans (0 shown of 1)" in render_snapshot(manifest, payload, grep="zephyr")


def test_check_runs_are_labelled_historical_and_never_fresh(snapshot):
    text = render_snapshot(*snapshot, kind="task")
    assert "historical check runs (recorded at the snapshot's origin; not fresh evidence for your checkout):" in text
    newest, older = text.index("run run1"), text.index("run run0")
    assert newest < older
    assert "unit: passed, ended 2026-11-01T11:00:00+00:00, recorded by local_runner, run run1" in text
    assert "unit: not passed (completed, exit 1)" in text
    assert "fresh" not in text.replace("not fresh evidence", "")


def test_task_claims_are_marked_as_reported_and_dependencies_are_listed(snapshot):
    text = render_snapshot(*snapshot, kind="task")
    assert "  state: in_progress" in text
    assert "  reported by an agent, not verified by this view: test passed" in text
    assert "  depends on: t1" in text


def test_terminal_control_and_bidi_characters_never_reach_the_output(snapshot):
    manifest, payload = snapshot
    payload["records"][0]["content"] = "safe\x1b[2J\x1b]0;title\x07 ‮gnp.exe\r\nnext"
    payload["sources"][0]["uri"] = "chat://x\x1b[31m\nforged: line"
    manifest["project"]["name"] = "shop\x1b[0m\nproject:   forged"
    text = render_snapshot(manifest, payload)
    assert not any(ch in text for ch in "\x1b\x07\r‮")
    assert "  | safe�[2J" in text and "  | next" in text
    assert "\nforged: line" not in text and "\nproject:   forged" not in text


@pytest.mark.parametrize("options, message", [
    ({"kind": "note"}, "Unknown kind"), ({"status": "fresh"}, "Unknown status"),
    ({"kind": "plan", "status": "confirmed"}, "Plans have no status")])
def test_unknown_filters_are_rejected(snapshot, options, message):
    with pytest.raises(ValueError, match=message):
        render_snapshot(*snapshot, **options)


def test_empty_snapshot_says_so():
    manifest = {"project": {"id": "p", "name": "empty"}, "snapshot_id": "b" * 64, "origin_root": "/x",
                "counts": {"records": 0, "plans": 0, "sources": 0, "check_runs": 0}}
    payload = {"sources": [], "records": [], "plans": [], "check_runs": []}
    text = render_snapshot(manifest, payload)
    assert "created:   unknown" in text and text.endswith("This snapshot is empty.")
