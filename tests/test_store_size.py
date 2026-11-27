"""`cairnmind store size` (#184): rows and approximate bytes of a store, read without changing anything."""
from __future__ import annotations

import asyncio
import json
import re
import time

import pytest
from typer.testing import CliRunner

from cairnmind.storage.arcade import ArcadeClient
from cairnmind.cli import app
from cairnmind.scip.ingest import _write_chunks
from cairnmind.agents.retrieval import Retriever
from cairnmind.store import EXPORT_TYPES, Store

SECRET = "SECRET-PASSAGE-184"


class Vectors:
    async def embed(self, texts):
        return [[0.25, 0.5, 0.75, 1.0] for _ in texts]

    async def model_identity(self, name):
        return "embedder@digest"


def filled(tmp_path, *, notes=0):
    """A store with a replaced file, a note, a record, vectors, another project, backups and journals."""
    store = Store(tmp_path / "memory")
    (tmp_path / "repo").mkdir()
    (tmp_path / "other").mkdir()
    project = store.add_project("demo", tmp_path / "repo")
    other = store.add_project("other", tmp_path / "other")
    pid = project["id"]
    old = store.snapshot(pid, "file://app.py", SECRET + "\n" + "line\n" * 200, "document")
    _write_chunks(store, pid, old["id"], old["content"])
    new = store.snapshot(pid, "file://app.py", SECRET + "\n" + "changed\n" * 120, "document")
    _write_chunks(store, pid, new["id"], new["content"])
    # Replacing deletes the old chunks (#186); a store written by an earlier release still holds such a chunk.
    store.command("INSERT INTO chunks CONTENT :row", {"row": {
        "id": "chunk-kept-by-an-earlier-release", "project_id": pid, "source_id": old["id"],
        "text": SECRET + "\n" + "line\n" * 80, "line_start": 1, "line_end": 81, "metadata": {}, "is_current": False}})
    note = store.snapshot(pid, "manual://note", SECRET + " note", "manual")
    _write_chunks(store, pid, note["id"], note["content"])
    store.remember(pid, {"kind": "fact", "content": SECRET + " fact"}, [note["id"]])
    for number in range(notes):
        store.snapshot(pid, f"manual://n{number:04d}", "x" * 4000, "manual")
    elsewhere = store.snapshot(other["id"], "manual://elsewhere", SECRET, "manual")
    _write_chunks(store, other["id"], elsewhere["id"], elsewhere["content"])
    retriever = Retriever(store, store.settings, Vectors())
    for row in store.query("SELECT id FROM chunks WHERE project_id=:p AND is_current=true", {"p": pid}):
        asyncio.run(retriever.embed_pending({"project_id": pid, "payload": {"chunk_id": row["id"]}}))
    data = store.data_dir
    store.backup(data / "backups" / f"{time.time_ns()}.cairnmind.json.gz")
    (data / "backups" / "before-restore-abc.cairnmind.json.gz").write_bytes(b"r" * 300)
    (data / "backups" / "before-schema-3-to-4-abc.cairnmind.json.gz").write_bytes(b"s" * 200)
    for session, size in (("s1", 1000), ("s2", 500)):
        (data / "runtime" / "agents" / session).mkdir(parents=True)
        (data / "runtime" / "agents" / session / "events.jsonl").write_bytes(b"j" * size)
    (data / "arcade" / "databases" / store.settings.arcade_database).mkdir(parents=True)
    (data / "arcade" / "databases" / store.settings.arcade_database / "pages").write_bytes(b"d" * 700)
    (data / "arcade" / "engine.log").write_bytes(b"e" * 300)
    return store, project, other


def direct(store, table, where="", params=None):
    rows = store.query(f"SELECT count(*) AS n, sum(@this.toJSON().length()) AS b FROM `{table}`{where}", params or {})
    return (int(rows[0]["n"]), int(rows[0].get("b") or 0)) if rows else (0, 0)


def vector_types(store):
    return sorted(row["name"] for row in store.query("SELECT name FROM schema:types")
                  if str(row["name"]).startswith("vector_") and row["name"] != "vector_indexes")


def test_every_collection_matches_direct_queries_exactly_on_a_small_store(tmp_path):
    from cairnmind.storage.store_size import measure
    store, project, _ = filled(tmp_path)
    reader = Store.open_readonly(store.settings)
    report = measure(reader)
    collections = {row["name"]: row for row in report["collections"]}
    assert vector_types(store), "the fixture made no vector type"
    assert set(collections) == set(EXPORT_TYPES) | set(vector_types(store))
    for name, row in collections.items():
        assert (row["rows"], row["bytes"], row["sampled"]) == (*direct(store, name), False), name
    # Sources: current against replaced, per kind.
    expected = {}
    for row in store.query("SELECT kind, is_current, count(*) AS n, sum(@this.toJSON().length()) AS b FROM sources "
                           "GROUP BY kind, is_current"):
        expected[(row["kind"], "current" if row["is_current"] else "replaced")] = (row["n"], row["b"])
    shown = {(row["kind"], state): (row[state]["rows"], row[state]["bytes"])
             for row in report["sources"]["kinds"] for state in ("current", "replaced") if row[state]["rows"]}
    assert shown == expected
    assert shown[("document", "replaced")][0] == 1 and shown[("document", "current")][0] == 1
    # Chunks: grouped by the kind of their source.
    kinds = {row["id"]: row["kind"] for row in store.query("SELECT id, kind FROM sources")}
    chunks = {}
    for row in store.query("SELECT source_id, is_current, @this.toJSON().length() AS b FROM chunks"):
        key = (kinds[row["source_id"]], "current" if row["is_current"] else "replaced")
        rows, size = chunks.get(key, (0, 0))
        chunks[key] = (rows + 1, size + row["b"])
    shown = {(row["kind"], state): (row[state]["rows"], row[state]["bytes"])
             for row in report["chunks"]["kinds"] for state in ("current", "replaced") if row[state]["rows"]}
    assert shown == chunks and ("document", "replaced") in shown
    # Vectors per namespace, and jobs per state.
    namespaces = {row["namespace"]: row for row in report["vectors"]["namespaces"]}
    assert set(namespaces) == {"embedder@digest"}
    assert namespaces["embedder@digest"]["rows"] == sum(direct(store, name)[0] for name in vector_types(store)) > 0
    jobs = {row["state"]: row["n"] for row in store.query("SELECT state, count(*) AS n FROM jobs GROUP BY state")}
    assert report["jobs"] == jobs
    # Disk use of the data directory.
    disk = report["disk"]
    assert disk["database"]["bytes"] == 700 and disk["engine_log"]["bytes"] == 300
    assert disk["agent_journals"] == {"path": str(store.data_dir / "runtime" / "agents"), "exists": True,
                                      "sessions": 2, "files": 2, "bytes": 1500}
    backups = disk["backups"]
    assert backups["scheduled"]["count"] == 1 and backups["scheduled"]["bytes"] > 0
    assert backups["before_restore"] == {"count": 1, "bytes": 300}
    assert backups["before_schema"] == {"count": 1, "bytes": 200}
    assert disk["data_dir"]["bytes"] >= 700 + 300 + 1500 + 500 + backups["scheduled"]["bytes"]


def test_one_project_is_measured_alone(tmp_path):
    from cairnmind.storage.store_size import measure
    store, project, other = filled(tmp_path)
    report = measure(Store.open_readonly(store.settings), project["id"])
    collections = {row["name"]: row for row in report["collections"]}
    params = {"p": project["id"]}
    for name in ("sources", "chunks", "records", "jobs", "embeddings"):
        assert (collections[name]["rows"], collections[name]["bytes"]) == direct(
            store, name, " WHERE project_id=:p", params), name
    assert collections["projects"]["rows"] == 1
    assert report["project"] == {"id": project["id"], "name": "demo"}
    assert all(row["project_id"] == project["id"] for row in report["vectors"]["types"])
    # Rows that belong to no project are not shown as a measured zero.
    for name in ("journal_state", "ingestion_inbox"):
        assert collections[name] == {"name": name, "rows": None, "bytes": None, "sampled": False,
                                     "store_wide": True}, name
    assert not collections["sources"]["store_wide"]
    assert re.search(r"journal_state\s+store-wide, not per project", report["text"])
    whole = {row["name"]: row for row in measure(Store.open_readonly(store.settings))["collections"]}
    assert whole["journal_state"]["rows"] == 1 and not whole["journal_state"]["store_wide"]


def test_sampling_covers_the_whole_collection_whatever_its_ids_look_like(tmp_path, monkeypatch):
    import cairnmind.storage.store_size as store_size
    store, project, _ = filled(tmp_path, notes=400)
    # Rows of uneven length, so that a sample from one end of a collection would be far off.
    for number in range(300):
        store.snapshot(project["id"], f"manual://u{number:04d}", "y" * (100, 9000, 2000, 500, 6000)[number % 5],
                       "manual")
    monkeypatch.setattr(store_size, "EXACT_ROWS", 50)
    reader = Store.open_readonly(store.settings)
    # Journal ids are "<epoch>:<seq>" with one epoch: not spread over any key space.
    rows = direct(store, "journal_events")[0]
    sample = store_size._Collection(reader, "journal_events").sample(rows, "seq, ")
    assert len(sample) == len({row["rid"] for row in sample}) == store_size.SAMPLE_POINTS * store_size.SAMPLE_WINDOW
    first = min(row["seq"] for row in store.query("SELECT seq FROM journal_events"))
    seqs = sorted(row["seq"] - first for row in sample)
    assert seqs[0] < rows / 8 and seqs[-1] > rows * 7 / 8, (rows, seqs[0], seqs[-1])
    report = store_size.measure(reader)
    collections = {row["name"]: row for row in report["collections"]}
    for name in ("journal_events", "sources"):
        count, size = direct(store, name)
        assert collections[name]["sampled"] is True and collections[name]["rows"] == count, name
        print(f"{name}: estimate {collections[name]['bytes']} against {size}")
        assert abs(collections[name]["bytes"] - size) / size < 0.03, name


def test_chunks_of_a_source_that_is_not_found_are_shown_as_such(tmp_path, monkeypatch):
    import cairnmind.storage.store_size as store_size
    store, project, _ = filled(tmp_path)
    note = store.query("SELECT id FROM sources WHERE uri='manual://note'")[0]["id"]
    chunks = direct(store, "chunks", " WHERE source_id=:s", {"s": note})
    original = store_size._source_kinds

    def without_note(*args):  # as if the note had been removed between the two queries
        kinds = original(*args)
        kinds.pop(note)
        return kinds

    monkeypatch.setattr(store_size, "_source_kinds", without_note)
    report = store_size.measure(Store.open_readonly(store.settings))
    missing = [row for row in report["chunks"]["kinds"] if not row["source_found"]]
    assert missing == [{"kind": None, "source_found": False, "current": {"rows": chunks[0], "bytes": chunks[1]},
                        "replaced": {"rows": 0, "bytes": 0}}]
    assert report["chunks"]["kinds"][-1] is missing[0]
    assert "(source not found)" in report["text"]


def test_a_vector_type_name_that_is_not_an_identifier_is_not_queried():
    from cairnmind.storage.store_size import _vector_types

    class Schema:
        def query(self, sql, params=None):
            if "schema:types" in sql:
                return [{"name": "vector_ab12"}, {"name": "vector_x` FROM sources --"}, {"name": "vector_indexes"}]
            return [{"type_name": "vector_ab12", "namespace": "n", "dimension": 4, "project_id": "p"}]

    assert [row["name"] for row in _vector_types(Schema(), None)] == ["vector_ab12"]


def test_above_the_exact_limit_bytes_are_sampled_within_one_percent(tmp_path, monkeypatch):
    import cairnmind.storage.store_size as store_size
    store, project, _ = filled(tmp_path, notes=400)
    monkeypatch.setattr(store_size, "EXACT_ROWS", 50)
    reader = Store.open_readonly(store.settings)
    report = store_size.measure(reader)
    sources = next(row for row in report["collections"] if row["name"] == "sources")
    rows, size = direct(store, "sources")
    assert sources["sampled"] is True and sources["rows"] == rows
    assert abs(sources["bytes"] - size) / size < 0.01
    manual = next(row for row in report["sources"]["kinds"] if row["kind"] == "manual")
    expected = direct(store, "sources", " WHERE kind='manual' AND is_current=true")
    assert manual["current"]["rows"] == expected[0] and report["sources"]["sampled"] is True
    assert abs(manual["current"]["bytes"] - expected[1]) / expected[1] < 0.01
    # One project's rows are summed exactly even above the limit: through the project index a sample
    # would cost more than the sum.
    alone = store_size.measure(reader, project["id"])
    sources = next(row for row in alone["collections"] if row["name"] == "sources")
    assert (sources["rows"], sources["bytes"], sources["sampled"]) == (
        *direct(store, "sources", " WHERE project_id=:p", {"p": project["id"]}), False)
    assert alone["sources"]["sampled"] is False


def sql_sent(monkeypatch):
    sent = []
    original = ArcadeClient._request

    def request(self, method, path, **kwargs):
        sent.append((method, path, (kwargs.get("json") or {}).get("command")))
        return original(self, method, path, **kwargs)

    monkeypatch.setattr(ArcadeClient, "_request", request)
    return sent


def test_the_cost_is_bounded_no_text_is_fetched_and_queries_do_not_grow_with_rows(tmp_path, monkeypatch):
    import cairnmind.storage.store_size as store_size
    monkeypatch.setattr(store_size, "EXACT_ROWS", 10)
    counts = []
    for size, notes in (("small", 20), ("large", 90)):
        store, project, _ = filled(tmp_path / size, notes=notes)
        reader = Store.open_readonly(store.settings)
        sent = sql_sent(monkeypatch)
        started = time.perf_counter()
        store_size.measure(reader)
        print(f"store size of the {size} fixture: {time.perf_counter() - started:.3f} s")
        queries = [command for _, path, command in sent if path.startswith("/api/v1/query/")]
        for command in queries:
            # A row is measured in the engine; no query returns text, vectors or whole rows.
            assert not re.search(r"SELECT\s+(\*\s+)?FROM", command, re.I), command
            assert not re.search(r"\b(content|text|vector|payload|metadata)\b", command), command
        assert all(method == "GET" or path.startswith(("/api/v1/query/", "/api/v1/begin/", "/api/v1/rollback/"))
                   for method, path, _ in sent)
        counts.append(len(queries))
        monkeypatch.undo()
        monkeypatch.setattr(store_size, "EXACT_ROWS", 10)
    assert counts[0] == counts[1]


def files(root):
    return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("arguments", [["store", "size"], ["store", "size", "demo"]])
def test_the_command_prints_sizes_never_content_and_changes_nothing(tmp_path, monkeypatch, arguments):
    store, project, _ = filled(tmp_path)
    before = files(store.data_dir)
    for name in ("cairnmind.storage.engine.start_engine", "cairnmind.storage.engine.ensure_engine", "cairnmind.cli.ensure_service"):
        monkeypatch.setattr(name, lambda *a, **k: pytest.fail("store size started the engine or the service"))
    monkeypatch.setattr(Store, "__init__", lambda *a, **k: pytest.fail("store size prepared the store"))
    runner = CliRunner()
    text = runner.invoke(app, ["--data-dir", str(store.data_dir), *arguments])
    shown = runner.invoke(app, ["--data-dir", str(store.data_dir), *arguments, "--json"])
    assert text.exit_code == 0 and shown.exit_code == 0, (text.output, shown.output, repr(shown.exception))
    report = json.loads(shown.output)
    assert SECRET not in text.output and SECRET not in shown.output
    for heading in ("Collections", "Sources by kind", "Chunks by source kind", "Vectors by namespace",
                    "Jobs by state", "Disk"):
        assert heading in text.output, heading
    assert "sources" in text.output and "embedder@digest" in text.output
    def steady(value):  # the time it took differs between the two runs
        return [line for line in value.strip().splitlines() if not line.startswith("Measured in ")]
    assert steady(report["text"]) == steady(text.output)
    assert files(store.data_dir) == before


def test_a_kind_or_namespace_from_the_store_cannot_control_the_terminal(tmp_path):
    from cairnmind.storage.store_size import measure
    store, project, _ = filled(tmp_path)
    store.snapshot(project["id"], "manual://odd", "x", "odd\x1b[31mkind\n")
    text = measure(Store.open_readonly(store.settings))["text"]
    assert "\x1b" not in text and "odd" in text
