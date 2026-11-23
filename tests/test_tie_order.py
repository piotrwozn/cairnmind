"""Hits with an equal score come in an order that does not depend on the process (issue #424).

Their order came from whatever the engine or a set gave, which depends on the hash seed of the process: the same
question could cite its sources in another order after a restart. The rule now: after the score, by the id of the
chunk (of a definition, the id of its node), applied where the ranking is made, so that the cut to the limit is stable too.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from test_knowledge import FakeOllama, workspace  # noqa: F401  (workspace is a fixture)

from cairnmind.definitions import rank_definitions
from cairnmind.retrieval import Retriever

TEXT = "def retry_limit():\n    return 'retry budget'\n"
QUESTION = "retry budget"

# Run by a subprocess with its own hash seed: the same searches, the ids of their hits in order.
SCRIPT = '''
import json, sys
from pathlib import Path
from cairnmind.config import Settings
from cairnmind.retrieval import Retriever
from cairnmind.store import Store

store = Store(Path(sys.argv[1]))
settings, project = store.settings, sys.argv[2]
retriever = Retriever(store, settings, None)
text = [hit["id"] for hit in retriever.search(project, "retry budget", 5)]
ids = [row["id"] for row in store.query("SELECT id FROM chunks WHERE is_current=true")]
real = store.query


def query(sql, params=None, *args, **kwargs):
    if "vector.neighbors" in sql:  # a stand-in vector ranking: equal distances, in the order of a set (hash seed)
        return [{"chunk_id": chunk, "distance": 0.5} for chunk in set(ids)]
    return real(sql, params, *args, **kwargs)


store.query = query
fused = [hit["id"] for hit in retriever.search(project, "retry budget", 5, [1.0, 0.0, 0.0], "ns")]
print(json.dumps({"text": text, "fused": fused}))
'''


def _project(workspace):  # noqa: F811
    store, project, settings = workspace
    root = Path(project["root"])
    source = store.snapshot(project["id"], str(root / "same.py"), TEXT, "code", metadata={"path": str(root / "same.py")})
    ids = sorted(str(uuid4()) for _ in range(12))
    for chunk_id in reversed(ids):  # stored in the opposite order to the rule: the engine's own order is not by id
        store.command("INSERT INTO chunks CONTENT :doc", {"doc": {
            "id": chunk_id, "project_id": project["id"], "source_id": source["id"], "text": TEXT,
            "line_start": 1, "line_end": 2, "metadata": {}, "is_current": True}})
    store.command("INSERT INTO vector_indexes CONTENT :doc", {"doc": {
        "id": "registry", "project_id": project["id"], "model": settings.embedding_model, "namespace": "ns",
        "dimension": 3, "type_name": "absent", "index_name": "absent[vector]"}})
    return store, project, settings, ids


def test_chunks_with_an_equal_score_are_cut_and_ordered_by_id(workspace):  # noqa: F811
    store, project, settings, ids = _project(workspace)
    found = Retriever(store, settings, FakeOllama()).search(project["id"], QUESTION, 5)
    assert [hit["id"] for hit in found] == ids[:5]


def test_definitions_with_every_key_equal_are_ordered_by_node_id():
    def row(node_id):
        return {"node_id": node_id, "source_id": "s", "path": "pkg/a.py",
                "symbol": {"name": "RETRY_LIMIT", "kind": "constant", "line_start": 3}}
    first = rank_definitions("What is the retry limit?", [row("n2"), row("n3"), row("n1")])
    again = rank_definitions("What is the retry limit?", [row("n3"), row("n1"), row("n2")])
    assert [item["node_id"] for item in first] == [item["node_id"] for item in again] == ["n1", "n2", "n3"]


def test_the_same_search_in_two_processes_with_other_hash_seeds_gives_the_same_order(workspace):  # noqa: F811
    store, project, settings, ids = _project(workspace)
    runs = []
    for seed in ("1", "2"):
        done = subprocess.run([sys.executable, "-c", SCRIPT, str(settings.data_dir), project["id"]],
                              env={**os.environ, "PYTHONHASHSEED": seed}, capture_output=True, text=True, timeout=300)
        assert done.returncode == 0, done.stderr[-2000:]
        runs.append(json.loads(done.stdout.strip().splitlines()[-1]))
    assert runs[0] == runs[1]
    assert runs[0]["text"] == ids[:5] and len(runs[0]["fused"]) == 5
