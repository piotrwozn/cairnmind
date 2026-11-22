"""A project of four times this repository's graph is indexed and backed up on the engine's default heap (#377).

This repository's graph has about 81,000 vertices and 101,000 edges. Written in ONE transaction, a graph of 120,000
vertices and 150,000 edges already ran the engine out of memory or made it stop answering (1 GB heap, the product's
own command line); the graph is now written in transactions of at most 20,000 rows, so the heap it needs does not
follow the size of the project. The engine here is a private one with the product's own heap. The graph is synthetic:
330 files of 1,000 symbols each, written by the indexer's own graph step. The restore of such a backup is #362's.
"""
import json
import os
from pathlib import Path
import time

import pytest

from cairnmind import ingest, process_identity
from cairnmind.config import env_names
from cairnmind.engine import stop_engine
from cairnmind.store import Store

from conftest import prepare_engine

FILES, SYMBOLS = 330, 1000
NODES = FILES * (SYMBOLS + 2)  # symbols, files and the nodes of their sources
EDGES = FILES * (1 + SYMBOLS + SYMBOLS // 4)  # snapshot_of, defines, and a call out of every fourth symbol


def _indexed(project, folder, now):
    indexed = {}
    for number in range(FILES):
        path = folder / f"module_{number}.py"
        symbols = [{"name": f"function_{i}", "kind": "function", "line_start": 2 * i + 1, "line_end": 2 * i + 2}
                   for i in range(SYMBOLS)]
        calls = [{"name": f"function_{(i * 7 + 1) % SYMBOLS}", "line": 2 * i + 1} for i in range(0, SYMBOLS, 4)]
        indexed[path] = {"source": {"id": f"source-{number}", "uri": f"file://{path}", "kind": "file"}, "root": folder,
                         "analysis": {"symbols": symbols, "imports": [], "calls": calls}}
    return indexed


@pytest.mark.time_limit(1800)
def test_a_graph_of_four_times_this_repository_is_indexed_and_backed_up_on_the_default_heap(tmp_path, monkeypatch):
    for name in (key for base in ("ARCADE_URL", "ARCADE_USER", "ARCADE_PASSWORD", "ARCADE_DATABASE", "ARCADE_DB_PREFIX",
                                  "ARCADE_CONNECT_URL") for key in env_names(base)):
        monkeypatch.delenv(name, raising=False)
    engine = prepare_engine(tmp_path / "engine", os.getpid())
    pid = json.loads((engine.data_dir / "arcade" / "process.json").read_text())["pid"]
    try:
        for name, value in (("CAIRNMIND_ARCADE_URL", engine.arcade_url), ("CAIRNMIND_ARCADE_USER", engine.arcade_user),
                            ("CAIRNMIND_ARCADE_PASSWORD", engine.arcade_password),
                            ("CAIRNMIND_ARCADE_DB_PREFIX", "graphsize")):
            monkeypatch.setenv(name, value)
        store = Store(tmp_path / "memory")
        folder = tmp_path / "project-folder"
        folder.mkdir()
        project = store.add_project("probe", folder)["id"]
        now = "2026-11-22T00:00:00+00:00"
        store.client.command("INSERT INTO sources CONTENT :rows", {"rows": [
            {"id": f"source-{n}", "project_id": project, "uri": f"file://{folder}/module_{n}.py", "kind": "file",
             "content": "x", "sha256": f"{n:064x}", "is_current": True, "created_at": now} for n in range(FILES)]})
        started = time.monotonic()
        result = ingest._build_graph(store, project, _indexed(project, folder, now))  # before: Java heap space
        built = time.monotonic() - started
        assert (result["nodes"], result["edges"]) == (NODES, EDGES)
        assert store.query("SELECT count(*) AS n FROM nodes")[0]["n"] == NODES
        assert store.query("SELECT count(*) AS n FROM edges")[0]["n"] == EDGES
        assert store.query("SELECT count(*) AS n FROM edges WHERE out IS NULL OR in IS NULL")[0]["n"] == 0
        row = store.query("SELECT graph_fingerprint,graph_nodes,graph_edges FROM projects WHERE id=:p", {"p": project})[0]
        assert row["graph_fingerprint"] and (row["graph_nodes"], row["graph_edges"]) == (NODES, EDGES)
        backup = store.backup(tmp_path / "graph.cairnmind.json.gz")
        assert Path(backup).stat().st_size > 1_000_000
        log = Path(engine.data_dir / "arcade" / "engine.log").read_text(errors="replace")
        assert "OutOfMemoryError" not in log and "critically low" not in log
        print(f"\n{NODES} vertices and {EDGES} edges written in {built:.0f} s, backup {Path(backup).stat().st_size} bytes")
    finally:
        stop_engine(engine)
        for _ in range(200):  # the engine ends after the stop is asked for: nothing may outlive the test
            if not process_identity.alive(pid):
                break
            time.sleep(0.1)
