"""A restore of a store the size of this repository's own passes on the engine's default heap of 1 GB (#362).

The trial that found it: a backup of 550 files, 2,942 passages, 81,488 graph vertices and 101,428 edges ran the engine
out of memory (java.lang.OutOfMemoryError: Java heap space) three times in three, because the whole restore was one
transaction and the engine holds a transaction's changed pages in its heap until it commits. The store here is
synthetic and has those counts; its text compresses better than the trial's, which changes the file's size and
nothing the engine holds. The engine is a private one, started with the product's own command line (-Xmx1024m), so
the heap is the one a person's engine has.
"""
import hashlib
import json
import os
from pathlib import Path
import random
import time

import pytest

from cairnmind import process_identity
from cairnmind.config import env_names
from cairnmind.engine import stop_engine
from cairnmind.store import Store

from conftest import prepare_engine

SOURCES, CHUNKS, NODES, EDGES = 550, 2942, 81488, 101428
# #377: at about 160,000 vertices the unique index lookup on nodes.id misses vertices on this engine, and no store of
# any restore can be written. This test stays far below that size; do not raise the counts toward it.
assert NODES < 100_000
WORDS = "def class return import self value store graph node edge restore backup engine heap memory".split()


def _text(rng, length):
    return " ".join(rng.choice(WORDS) for _ in range(length // 6))


def _build(store, root):
    """The trial's counts, written as the product writes them: documents in requests of a few hundred rows, the graph
    in transactions of 5,000."""
    rng = random.Random(362)
    folder = root / "project-folder"
    folder.mkdir()
    project = store.add_project("probe", folder)["id"]
    now = "2026-11-22T00:00:00+00:00"
    sources = []
    for offset in range(0, SOURCES, 100):
        rows = []
        for i in range(offset, min(offset + 100, SOURCES)):
            content = _text(rng, 12000)
            sources.append(f"source-{i}")
            rows.append({"id": f"source-{i}", "project_id": project, "uri": f"file://{folder}/f{i}.py", "kind": "file",
                         "content": content, "sha256": hashlib.sha256(content.encode()).hexdigest(),
                         "is_current": True, "created_at": now})
        store.client.command("INSERT INTO sources CONTENT :rows", {"rows": rows})
    for offset in range(0, CHUNKS, 200):
        store.client.command("INSERT INTO chunks CONTENT :rows", {"rows": [
            {"id": f"chunk-{n}", "project_id": project, "source_id": sources[n % SOURCES], "text": _text(rng, 2500),
             "is_current": True, "metadata": {"start": n, "end": n + 60}} for n in range(offset, min(offset + 200, CHUNKS))]})
    for offset in range(0, CHUNKS, 500):
        store.client.command("INSERT INTO jobs CONTENT :rows", {"rows": [
            {"id": f"job-{n}", "project_id": project, "kind": "embed", "state": "completed",
             "payload": json.dumps({"chunk_id": f"chunk-{n}"}), "created_at": now}
            for n in range(offset, min(offset + 500, CHUNKS))]})
    nodes = [{"id": f"{project}:node:{i}", "project_id": project, "kind": rng.choice(["function", "class", "module", "file"]),
              "label": f"src/cairnmind/module_{i % 400}.py::function_{i}", "source_id": sources[i % SOURCES],
              "metadata": {"path": f"src/cairnmind/module_{i % 400}.py", "line": i % 2000, "end_line": i % 2000 + 30,
                           "language": "python", "qualified": f"cairnmind.module_{i % 400}.function_{i}"}}
             for i in range(NODES)]
    edges = [{"id": f"{project}:edge:{i}", "project_id": project, "from_id": f"{project}:node:{i % NODES}",
              "to_id": f"{project}:node:{(i % NODES + 1 + (i * 7919 + 13) % (NODES - 1)) % NODES}",
              "kind": rng.choice(["calls", "imports", "defines"]), "source_id": sources[i % SOURCES],
              "metadata": {"line": i % 2000}} for i in range(EDGES)]
    for kind, items in (("nodes", nodes), ("edges", edges)):
        for offset in range(0, len(items), 5000):
            part = items[offset:offset + 5000]
            with store.transaction() as tx:
                tx.touch(project, "graph", op="reset")
                for group in range(0, len(part), 1000):
                    chosen = part[group:group + 1000]
                    one, other = (chosen, []) if kind == "nodes" else ([], chosen)
                    store.prefetch_graph(tx, one, other)
                    store.write_graph(tx, project, one, other)
                    tx.known = None


def _counts(store):
    return {table: store.query(f"SELECT count(*) AS n FROM {table}")[0]["n"]
            for table in ("sources", "chunks", "nodes", "edges")}


def _restore_through_a_slow_link(store, backup, engine, monkeypatch):
    """The same restore again, as a connect-only installation through a relay that delays each request by 20 ms (the
    trial's network: 18.5 ms). The trial's clients gave up with a read timeout on ONE request that the server was still
    working on; here no request of the restore comes near the client's wait, a commit included."""
    from cairnmind import arcade
    from database_relay import Relay
    monkeypatch.delenv("CAIRNMIND_ARCADE_URL")
    monkeypatch.setenv("CAIRNMIND_ARCADE_DATABASE", store.settings.arcade_database)
    with Relay(int(engine.arcade_url.rsplit(":", 1)[1]), delay=0.02) as relay:
        monkeypatch.setenv("CAIRNMIND_ARCADE_CONNECT_URL", relay.url)
        far = Store(store.data_dir)
        waits, request = [], arcade.ArcadeClient._request

        def noting(self, method, path, **kwargs):
            waits.append((path, kwargs.get("timeout", arcade.REQUEST_TIMEOUT)))
            return request(self, method, path, **kwargs)

        monkeypatch.setattr(arcade.ArcadeClient, "_request", noting)
        begun, before = time.monotonic(), len(relay.durations)
        far.restore(backup)
        took = time.monotonic() - begun
        monkeypatch.setattr(arcade.ArcadeClient, "_request", request)
        measured = relay.durations[before:]
        assert _counts(far) == {"sources": SOURCES, "chunks": CHUNKS, "nodes": NODES, "edges": EDGES}  # through the relay
    longest = max(measured, key=lambda item: item[1])
    print(f"\nrestore over a 20 ms relay: {len(measured)} requests, {took:.1f} s; longest request {longest[1]:.2f} s: "
          f"{longest[0][:110]!r}")  # shown by `pytest -s`; the figure the documents quote
    assert longest[1] < arcade.REQUEST_TIMEOUT / 2, longest  # the trial's clients gave up after 30 s on one request
    commits = [timeout for path, timeout in waits if path.startswith("/api/v1/commit/")]
    # The restore's own commits are waited for longer; the read of the safety copy first and the rule step last are not.
    assert len(commits) >= 6 and set(commits[1:-1]) == {arcade.RESTORE_COMMIT_TIMEOUT}
    assert commits[0] == commits[-1] == arcade.REQUEST_TIMEOUT
    assert {timeout for path, timeout in waits if not path.startswith("/api/v1/commit/")} == {arcade.REQUEST_TIMEOUT}


def _say(engine_pid, seconds):
    """The restore's seconds and the engine's largest resident memory (VmHWM, Linux; the Java heap itself is held to
    1 GB by -Xmx1024m) go to the job summary of CI, and are printed for a run by hand."""
    try:
        peak = next(line.split(":", 1)[1].strip() for line in Path(f"/proc/{engine_pid}/status").read_text().splitlines()
                    if line.startswith("VmHWM"))
    except (OSError, StopIteration):
        peak = "not readable here"
    text = f"restore of {SOURCES} files, {NODES} vertices, {EDGES} edges: {seconds:.1f} s; engine's largest resident memory since it started (building the store included): {peak}"
    print("\n" + text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write(text + "\n")


# Not in a plain run: `python -m pytest -m slow_size tests/test_restore_size.py` runs it (the `restore-size` CI job
# does, on main and by hand). The ordinary tier keeps the bound of rows in one transaction: tests/test_restore_staged.py.
@pytest.mark.slow_size
@pytest.mark.time_limit(1500)
def test_a_store_the_size_of_this_repository_restores_on_the_default_heap(tmp_path, monkeypatch):
    for name in (key for base in ("ARCADE_URL", "ARCADE_USER", "ARCADE_PASSWORD", "ARCADE_DATABASE", "ARCADE_DB_PREFIX",
                                  "ARCADE_CONNECT_URL") for key in env_names(base)):
        monkeypatch.delenv(name, raising=False)
    engine = prepare_engine(tmp_path / "engine", os.getpid())
    pid = json.loads((engine.data_dir / "arcade" / "process.json").read_text())["pid"]
    try:
        for name, value in (("CAIRNMIND_ARCADE_URL", engine.arcade_url), ("CAIRNMIND_ARCADE_USER", engine.arcade_user),
                            ("CAIRNMIND_ARCADE_PASSWORD", engine.arcade_password),
                            ("CAIRNMIND_ARCADE_DB_PREFIX", "restoresize")):
            monkeypatch.setenv(name, value)
        store = Store(tmp_path / "memory")
        _build(store, tmp_path)
        assert _counts(store) == {"sources": SOURCES, "chunks": CHUNKS, "nodes": NODES, "edges": EDGES}
        backup = store.backup(tmp_path / "trial.cairnmind.json.gz")
        begun = time.monotonic()
        store.restore(backup)  # before the change: ArcadeDB HTTP 500: Java heap space, after 69 to 104 s
        _say(pid, time.monotonic() - begun)
        assert _counts(store) == {"sources": SOURCES, "chunks": CHUNKS, "nodes": NODES, "edges": EDGES}
        assert store.embed_jobs_requeued == CHUNKS  # a backup holds no vectors: every completed embed job is pending
        assert len(list((store.data_dir / "backups").glob("before-restore-*"))) == 1
        assert not store.query("SELECT FROM devbrain_meta WHERE name='restore_unfinished'")
        log = Path(engine.data_dir / "arcade" / "engine.log").read_text(errors="replace")
        assert "OutOfMemoryError" not in log and "critically low" not in log
        _restore_through_a_slow_link(store, backup, engine, monkeypatch)
    finally:
        stop_engine(engine)
        for _ in range(200):  # the engine ends after the stop is asked for: nothing may outlive the test
            if not process_identity.alive(pid):
                break
            time.sleep(0.1)
