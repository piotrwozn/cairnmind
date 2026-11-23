"""The database requests of one search (issue #406).

A search read one source per hit, listed every current source of the project and asked twice for the state of the
vectors. It now takes a number of requests that does not grow with the hits or the sources, and finds what it found.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from test_knowledge import FakeOllama, workspace  # noqa: F401  (workspace is a fixture)

from cairnmind import arcade
from cairnmind.ingest import index_project
from cairnmind.retrieval import Retriever


@pytest.fixture
def requests_made(monkeypatch):
    """The SQL of every database request made while the test runs."""
    seen, request = [], arcade.ArcadeClient._request

    def counted(client, method, path, **kwargs):
        seen.append(str((kwargs.get("json") or {}).get("command", path)))
        return request(client, method, path, **kwargs)
    monkeypatch.setattr(arcade.ArcadeClient, "_request", counted)
    return seen


def _files(root: Path, count: int) -> None:
    (root / "pkg").mkdir(exist_ok=True)
    for index in range(count):
        (root / "pkg" / f"limit_{index:02d}.py").write_text(f"def limit_{index}():\n    return 'retry budget {index}'\n")


def test_a_search_takes_the_same_requests_for_a_few_hits_and_for_many(workspace, requests_made):  # noqa: F811
    store, project, settings = workspace
    root = Path(project["root"])
    _files(root, 2)
    index_project(store, project["id"])
    retriever = Retriever(store, settings, FakeOllama())
    retriever.search(project["id"], "retry budget", 8)  # the first search may set things up
    requests_made.clear()
    few = retriever.search(project["id"], "retry budget", 8)
    in_a_search = len(requests_made)
    _files(root, 20)
    index_project(store, project["id"])
    requests_made.clear()
    many = retriever.search(project["id"], "retry budget", 8)
    assert len(few) == 2 and len(many) == 8
    assert len(requests_made) == in_a_search <= 12, requests_made  # 9 today; it was 36 to 41 in a real project
    assert not any("FROM sources WHERE id=:id" in sql or ("FROM sources" in sql and "metadata.root AS root" in sql)
                   for sql in requests_made), requests_made  # no source read per hit, no list of every source
    print(f"search: {in_a_search} requests")


def test_a_search_with_vectors_asks_for_the_state_of_the_vectors_once(workspace, requests_made):  # noqa: F811
    store, project, settings = workspace
    _files(Path(project["root"]), 3)
    index_project(store, project["id"])
    retriever = Retriever(store, settings, FakeOllama())
    store.command("INSERT INTO vector_indexes CONTENT :doc", {"doc": {
        "id": "registry", "project_id": project["id"], "model": settings.embedding_model, "namespace": "ns",
        "dimension": 3, "type_name": "absent", "index_name": "absent[vector]"}})
    requests_made.clear()
    found = retriever.search(project["id"], "retry budget", 8, [0.0, 1.0, 0.0], "ns")
    assert found  # the vector query fails on the absent type; the text ranking still answers, and says so
    assert sum("FROM jobs" in sql for sql in requests_made) == 1, requests_made
    assert any(warning.startswith("Native vector search unavailable") for warning in retriever.warnings)


def test_a_small_worktree_costs_one_filtered_read_and_never_a_wider_ranking(workspace, tmp_path, requests_made):  # noqa: F811
    store, project, settings = workspace
    root = Path(project["root"])
    _files(root, 1)
    (root / "pkg" / "limit_00.py").write_text("def limit_00():\n    return 'budget'\n")
    second = tmp_path / "a-second-worktree"
    (second / "pkg").mkdir(parents=True)
    for index in range(40):  # they rank first: much more of the query's words
        (second / "pkg" / f"budget_{index:02d}.py").write_text("budget budget budget retry retry budget\n")
    store.add_project_root(project["id"], second)
    index_project(store, project["id"])
    retriever = Retriever(store, settings, FakeOllama())
    requests_made.clear()
    ids = [hit["source_id"] for hit in retriever.search(project["id"], "retry budget", 8, worktree=str(root))]
    ranked = [sql for sql in requests_made if "FROM chunks" in sql and "SEARCH_INDEX" in sql]
    assert [("source_id IN :eligible" in sql) for sql in ranked] == [False, True], ranked  # one filtered read after the first
    assert all(sql.endswith("LIMIT 24") for sql in ranked), ranked  # a ranking is never read wider than the first
    uris = {store.get_source(source_id)["uri"] for source_id in ids}
    assert uris == {str(root / "pkg" / "limit_00.py")}
    assert len(retriever.search(project["id"], "retry budget", 8, worktree=str(second))) == 8


def test_the_rule_for_one_source_is_the_rule_of_the_list_of_every_current_source(workspace, tmp_path):  # noqa: F811
    store, project, settings = workspace
    root = Path(project["root"])
    _files(root, 3)
    second = tmp_path / "other-worktree"
    (second / "pkg").mkdir(parents=True)
    (second / "pkg" / "other.py").write_text("OTHER = 1\n")
    store.add_project_root(project["id"], second)
    index_project(store, project["id"])
    (root / "pkg" / "limit_00.py").write_text("def limit_00():\n    return 'changed'\n")  # a replaced version stays
    index_project(store, project["id"])
    retriever = Retriever(store, settings, FakeOllama())
    sources = store.get_sources([row["id"] for row in store.query(
        "SELECT id FROM sources WHERE project_id=:project", {"project": project["id"]})])
    assert any(not row["is_current"] for row in sources.values())
    for worktree in (root, second):
        pid, scope_root, roots = retriever._scope(project["id"], str(worktree))
        listed = retriever._source_ids(pid, scope_root, roots)
        decided = {identifier for identifier, row in sources.items()
                   if retriever._in_scope(row, pid, scope_root, roots, [])}
        assert decided == listed and decided
        # the chunks of the query that the list of sources used to filter, in its order, are the hits of the search
        old = store.query(
            "SELECT id FROM chunks WHERE SEARCH_INDEX('chunks[text]', :query)=true AND project_id=:project "
            "AND is_current=true AND source_id IN :eligible ORDER BY $score DESC LIMIT 24",
            {"query": "limit OR changed OR other OR retry", "project": pid, "eligible": sorted(listed)})
        found = retriever.search(pid, "limit changed other retry", 8, worktree=str(worktree))
        assert [row["id"] for row in old][:8] == [hit["id"] for hit in found] and found


def test_sources_read_together_are_the_rows_read_one_by_one(workspace):  # noqa: F811
    store, project, _settings = workspace
    _files(Path(project["root"]), 5)
    index_project(store, project["id"])
    ids = [row["id"] for row in store.query("SELECT id FROM sources WHERE project_id=:project", {"project": project["id"]})]
    together = store.get_sources([*ids, ids[0], "missing"])
    assert together == {identifier: store.get_source(identifier) for identifier in ids}
    assert store.get_sources([]) == {}
