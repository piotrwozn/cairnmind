"""Findings of the 0.11.0 release trial in the store and its upkeep (#238)."""
from __future__ import annotations

import pytest

from cairnmind.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "memory")


@pytest.fixture
def project(store, tmp_path):
    return store.add_project("example", tmp_path / "repo")


def source(store, project, text="We decided to keep the queue in the database."):
    return store.snapshot(project["id"], "conversation://one", text, "transcript")


@pytest.mark.parametrize("kind", ["decision", "task"])
def test_a_null_depends_on_is_stored_as_no_dependencies(store, project, kind):
    # A model's metadata may carry depends_on: null; the whole extraction of a message failed on it.
    cited = source(store, project)
    record = store.remember(project["id"], {"kind": kind, "content": "Keep the queue in the database",
                                            "metadata": {"depends_on": None}}, [cited["id"]])
    assert "depends_on" not in record["metadata"] or record["metadata"]["depends_on"] == []
    edges = store.query("SELECT kind FROM edges WHERE from_id=:id", {"id": f"record:{record['id']}"})
    assert [row["kind"] for row in edges] == ["supported_by"]


def test_a_key_held_by_a_person_confirmed_record_gets_the_kept_refusal(store, project):
    from conftest import as_person
    from cairnmind.confirmation import VerdictKept
    cited = source(store, project)
    held = as_person(store, project["id"], {"kind": "decision", "key": "queue", "content": "Queue in the database"},
                     [cited["id"]])
    # A session that writes under that key is told what it can do, not to update by id (a second refusal).
    with pytest.raises(VerdictKept) as refused:
        store.remember(project["id"], {"kind": "decision", "key": "queue", "content": "Queue in Redis"}, [cited["id"]])
    message = str(refused.value)
    assert held["id"] in message and "possible_conflicts" in message and "another key" in message
    assert "expected_version" not in message


def test_a_key_held_by_another_record_names_that_record(store, project):
    cited = source(store, project)
    held = store.remember(project["id"], {"kind": "decision", "key": "queue", "content": "Queue in the database"},
                          [cited["id"]])
    with pytest.raises(ValueError, match="^Active record key already exists") as refused:
        store.remember(project["id"], {"kind": "decision", "key": "queue", "content": "Queue in Redis"}, [cited["id"]])
    assert held["id"] in str(refused.value)


def test_the_command_line_key_clash_names_what_a_person_can_do(tmp_path, monkeypatch):
    from conftest import plain_words
    from cairnmind import confirmation, ingest
    from test_confirmed_by_person import cli
    monkeypatch.setattr(ingest, "register_default_histories", lambda *a: {})
    monkeypatch.setattr(confirmation, "person_at_terminal", lambda *a: True)
    run, _ = cli(tmp_path)
    first = run("remember", "demo", "Queue in the database", "--kind", "decision", "--key", "queue", "--confirmed")
    assert first.exit_code == 0, first.output
    for extra in ([], ["--confirmed"]):
        again = run("remember", "demo", "Queue in Redis", "--kind", "decision", "--key", "queue", *extra)
        words = plain_words(again.output)
        assert again.exit_code != 0
        assert "expected_version" not in words and "--id" not in words
        assert "another key" in words and "inspector" in words
    import json
    assert [row["content"] for row in json.loads(run("records", "demo").output)] == ["Queue in the database"]
