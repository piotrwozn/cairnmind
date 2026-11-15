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
