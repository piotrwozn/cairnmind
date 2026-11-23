"""Restore reads a backup in pieces (#196): bounded memory, the same checks and messages, nothing half done."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import tracemalloc

import pytest

from cairnmind import backup_reader
from cairnmind.backup_reader import opened_backup
from cairnmind.restore_staged import RestoreFailed
from cairnmind.sql import json_text
from cairnmind.store import EXPORT_TYPES, RESTORE_TYPES, Store

from conftest import as_person

FIXTURE_0_10_0 = Path(__file__).parent / "fixtures" / "backup-0.10.0.cairnmind.json.gz"
MEMORY_LIMIT = 12 * 2**20
ROW_TEXT = 30_000


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "memory")


def comparable(payload):
    return {collection: sorted(json_text(row) for row in payload[collection])
            for collection in EXPORT_TYPES if collection not in {"journal_state", "journal_events"}} | {
        "vector_data": {name: sorted(json_text(row) for row in rows) for name, rows in payload["vector_data"].items()}}


def test_restore_peak_memory_stays_under_a_fixed_limit(store, tmp_path):
    project = store.add_project("large", tmp_path / "large")
    rows = 4 * MEMORY_LIMIT // ROW_TEXT + 200
    for first in range(0, rows, 100):
        store.client.command("INSERT INTO chunks CONTENT :rows", {"rows": [
            {"id": f"bulk-{n}", "project_id": project["id"], "source_id": "bulk", "text": f"{n} " + "x" * ROW_TEXT,
             "is_current": True} for n in range(first, min(rows, first + 100))]})
    path = store.backup(tmp_path / "large.cairnmind.json.gz")
    with gzip.open(path, "rb") as handle:
        payload_bytes = sum(len(piece) for piece in iter(lambda: handle.read(1 << 20), b""))
    assert payload_bytes > 4 * MEMORY_LIMIT
    target = Store(tmp_path / "target")
    tracemalloc.start()
    try:
        target.restore(path)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < MEMORY_LIMIT, f"peak {peak / 2**20:.1f} MiB for a backup of {payload_bytes / 2**20:.1f} MiB"
    assert comparable(target.export()) == comparable(store.export())


def _backup_payload(store, tmp_path):
    """A varied payload: the 0.10.0 fixture plus a second project, a task, a plan and a machine check run."""
    store.restore(FIXTURE_0_10_0)
    project = store.add_project("second", tmp_path / "second")
    source = store.snapshot(project["id"], "conversation://two", "Linia dwa \"cytat\" \\ \U0001F600", "transcript")
    as_person(store, project["id"], {"kind": "fact", "key": "quote", "content": "Zażółć \t tab </script>"},
              [source["id"]])
    payload = json.loads(gzip.decompress(store.backup(tmp_path / "base.cairnmind.json.gz").read_bytes()))["payload"]
    # Backups of this version carry no vectors (#187); the reader still has to take them from an older file.
    older = json.loads(gzip.decompress(FIXTURE_0_10_0.read_bytes()))["payload"]
    payload.update({name: older[name] for name in ("embeddings", "vector_indexes", "vector_data")})
    payload["records"].append({"id": "task-1", "kind": "task", "project_id": project["id"], "source_ids": [source["id"]]})
    payload["plans"].append({"id": "plan-1", "project_id": project["id"], "step_ids": ["task-1"]})
    payload["check_runs"].append({"id": "run-1", "project_id": project["id"], "task_id": "task-1",
                                  "source_ids": {"log": source["id"]}, "profile_source_id": source["id"]})
    return payload, project["id"], source["id"]


def _sealed(payload, **envelope):
    return {"format": "cairnmind-arcadedb-backup", "payload": payload,
            "sha256": hashlib.sha256(json_text(payload).encode()).hexdigest(), "version": 1, **envelope}


def _first(payload, collection, test=lambda row: True):
    return next(row for row in payload[collection] if test(row))


def _variants(payload, project_id, source_id):
    """Name -> (file bytes before compression, whether the stream reader hands it to the in-memory one)."""
    cases = {}

    def sealed(name, change=None, **envelope):
        copy = json.loads(json.dumps(payload))
        if change:
            change(copy)
        cases[name] = (json_text(_sealed(copy, **envelope)).encode(), False)

    def setter(collection, key, value, test=lambda row: True):
        return lambda p: _first(p, collection, test).__setitem__(key, value)

    good = _sealed(payload)
    cases["good"] = (json_text(good).encode(), False)
    cases["pretty"] = (json.dumps(good, indent=2, sort_keys=True, ensure_ascii=False).encode(), False)
    cases["spaced"] = (json.dumps(good, sort_keys=True).encode(), False)
    cases["crlf"] = (json.dumps(good, indent=1, sort_keys=True).replace("\n", "\r\n").encode(), False)
    cases["unsorted"] = (json.dumps({**good, "payload": dict(reversed(list(payload.items())))}).encode(), True)
    cases["repeated-key"] = (json_text(good).encode().replace(b'"version":1}', b'"version":1,"version":1}'), True)
    cases["array"] = (b"[1, 2]", True)
    cases["extra-data"] = (json_text(good).encode() + b" {}", False)
    cases["truncated"] = (json_text(good).encode()[:-40], False)
    cases["truncated-number"] = (json_text(good).encode()[:-1], False)
    cases["empty"] = (b"", False)
    cases["no-payload"] = (json_text({"format": "cairnmind-arcadedb-backup", "version": 1}).encode(), False)
    sealed("foreign-format", format="foreign")
    sealed("version-2", version=2)
    tampered = json.loads(json.dumps(good))
    _first(tampered["payload"], "sources")["content"] = "changed without updating the checksum"
    cases["checksum"] = (json_text(tampered).encode(), False)
    cases["payload-list"] = (json_text(_sealed([1, 2])).encode(), False)
    sealed("schema-6", lambda p: p.__setitem__("format_version", 6))
    sealed("schema-list", lambda p: p.__setitem__("format_version", [5]))
    sealed("backend", lambda p: p.__setitem__("backend", "sqlite"))
    sealed("missing-plans", lambda p: p.pop("plans"))
    sealed("plans-object", lambda p: p.__setitem__("plans", {}))
    sealed("row-not-object", lambda p: p["chunks"].append([1]))
    sealed("project-without-id", lambda p: _first(p, "projects").pop("id"))
    sealed("source-id-list", setter("sources", "id", ["x"]))
    sealed("dangling-project", setter("chunks", "project_id", "nowhere"))
    sealed("journal-dangling-project", lambda p: p["journal_events"].append({"id": "j", "project_id": "nowhere"}))
    sealed("corrupt-source", setter("sources", "content", "not what was hashed"))
    sealed("source-content-list", setter("sources", "content", ["x"]))
    sealed("source-without-hash", lambda p: _first(p, "sources").pop("sha256"))
    sealed("dangling-evidence", setter("records", "source_ids", ["nowhere"]))
    sealed("evidence-not-list", setter("records", "source_ids", 7))
    sealed("dangling-edge", setter("edges", "to_id", "nowhere"))
    sealed("node-without-project", lambda p: [row.pop("project_id") for row in p["nodes"]])
    sealed("dangling-plan", setter("plans", "step_ids", ["nowhere"], lambda row: row["id"] == "plan-1"))
    sealed("plan-steps-text", setter("plans", "step_ids", "task-1", lambda row: row["id"] == "plan-1"))
    sealed("dangling-run", setter("check_runs", "task_id", "nowhere", lambda row: row["id"] == "run-1"))
    sealed("run-evidence-list", setter("check_runs", "source_ids", [source_id], lambda row: row["id"] == "run-1"))
    sealed("run-dangling-source", setter("check_runs", "profile_source_id", "nowhere", lambda row: row["id"] == "run-1"))
    sealed("vector-data-list", lambda p: p.__setitem__("vector_data", []))
    sealed("several-faults", lambda p: (_first(p, "edges").__setitem__("to_id", "nowhere"),
                                        _first(p, "sources").__setitem__("content", "x"),
                                        _first(p, "chunks").__setitem__("project_id", "nowhere")))
    sealed("schema-4-ignores-journal", lambda p: (p.__setitem__("format_version", 4),
                                                  p["journal_events"].append({"id": "kept-out", "project_id": "x"})))
    sealed("schema-2", lambda p: (p.__setitem__("format_version", 2), [p.pop(name) for name in (
        "plans", "plan_revisions", "plan_requests", "worktree_states", "sessions", "resume_packets", "resume_acks",
        "session_events", "check_runs", "memory_settings", "memory_snapshots", "memory_imports", "memory_bindings",
        "journal_state", "journal_events")]))
    sealed("unknown-key", lambda p: p.__setitem__("zzz_later", [{"id": 1}]))
    cases["not-a-number"] = (json_text(good).encode().replace(
        b'"format_version":5', b'"format_version":5,"format_version_x":NaN'), False)
    cases["broken-utf8"] = (json_text(good).encode().replace("Zażółć".encode(), b"Za\xff"), False)
    # A lone surrogate is valid JSON but cannot be encoded for the checksum. The key sorts after "zzz...",
    # so the file keeps the order every writer produces and takes the streamed path.
    surrogate = b'"\\ud800"'
    for name, envelope in (("surrogate-key-foreign-format", {"format": "foreign"}), ("surrogate-key", {})):
        copy = json.loads(json.dumps(payload))
        copy["zzz_surrogate"] = [{"id": 1}]
        cases[name] = (json_text(_sealed(copy, **envelope)).encode().replace(b'"zzz_surrogate"', surrogate), False)
    copy = json.loads(json.dumps(payload))
    copy["vector_data"]["zzz_surrogate"] = []
    cases["surrogate-vector-type"] = (json_text(_sealed(copy)).encode().replace(b'"zzz_surrogate"', surrogate), False)
    return cases


def _outcome(read):
    try:
        return "ok", read()
    except UnicodeError as exc:
        return type(exc).__name__, ""  # its message names a position inside the piece being coded
    except Exception as exc:  # noqa: BLE001 - the comparison is of the kind and the message of any error
        return type(exc).__name__, str(exc)


def _loaded(path):
    payload = Store._validated_backup(path)
    return ({name: payload[name] for name in EXPORT_TYPES}, payload.get("vector_data", {}), payload["vector_indexes"])


def _streamed(path, fallback):
    def whole(source):
        fallback.append(source)
        return Store._validated_backup(source)

    with opened_backup(path, whole, RESTORE_TYPES) as source:
        rows, vectors = {name: [] for name in EXPORT_TYPES}, {}
        for kind, name, row in source.rows(EXPORT_TYPES, vectors=True):
            if kind == "row":
                rows[name].append(row)
            elif row is None:
                vectors.setdefault(name, [])
            else:
                vectors[name].append(row)
        return rows, vectors, source.vector_indexes


@pytest.mark.parametrize("window", [7, 65536])
def test_streamed_validation_matches_the_in_memory_reader(store, tmp_path, monkeypatch, window):
    payload, project_id, source_id = _backup_payload(store, tmp_path)
    monkeypatch.setattr(backup_reader, "READ_CHARS", window)
    for name, (text, unusual) in _variants(payload, project_id, source_id).items():
        path = tmp_path / f"{name}.cairnmind.json.gz"
        path.write_bytes(gzip.compress(text))
        fallback = []
        expected, got = _outcome(lambda: _loaded(path)), _outcome(lambda: _streamed(path, fallback))
        assert got == expected, name
        assert bool(fallback) == unusual, name
    # The two files every version writes take the streamed path and give the payload the fixture holds.
    for path in (FIXTURE_0_10_0, store.backup(tmp_path / "new.cairnmind.json.gz")):
        fallback = []
        assert _streamed(path, fallback) == _loaded(path) and not fallback


def test_cases_that_must_be_refused_are_refused(store, tmp_path):
    payload, project_id, source_id = _backup_payload(store, tmp_path)
    expected = {"truncated": "Invalid or corrupt", "checksum": "checksum mismatch", "dangling-edge": "graph endpoint",
                "corrupt-source": "corrupt source snapshot", "several-faults": "invalid project reference",
                "dangling-evidence": "evidence reference", "foreign-format": "not a supported"}
    cases = _variants(payload, project_id, source_id)
    for name, message in expected.items():
        path = tmp_path / f"{name}.cairnmind.json.gz"
        path.write_bytes(gzip.compress(cases[name][0]))
        with pytest.raises(ValueError, match=message):
            _streamed(path, [])


@pytest.mark.parametrize("replacement", ["another backup", "not gzip"])
def test_restore_refuses_a_file_changed_after_validation_and_changes_nothing(store, tmp_path, monkeypatch, replacement):
    payload, _, _ = _backup_payload(store, tmp_path)
    path = store.backup(tmp_path / "first.cairnmind.json.gz")
    store.add_project("later", tmp_path / "later")
    other = store.backup(tmp_path / "second.cairnmind.json.gz").read_bytes()
    if replacement == "not gzip":
        other = b"not a backup any more"
    before = comparable(store.export())
    backup = Store.backup

    def swap_then_backup(self, destination):
        # The safety copy runs after validation and before the inserts read the file again.
        result = backup(self, destination)
        if "before-restore" in str(destination):
            path.write_bytes(other)
        return result

    monkeypatch.setattr(Store, "backup", swap_then_backup)
    # The file was valid when checked: it changed, it is not called corrupt or a checksum mismatch (#238).
    with pytest.raises(ValueError, match="^The backup file changed while it was being read"):
        store.restore(path)
    assert comparable(store.export()) == before


def test_a_file_changed_between_the_two_checking_passes_says_it_changed(store, tmp_path, monkeypatch):
    _backup_payload(store, tmp_path)
    path = store.backup(tmp_path / "first.cairnmind.json.gz")
    store.add_project("later", tmp_path / "later")
    other = store.backup(tmp_path / "second.cairnmind.json.gz").read_bytes()
    check_index = backup_reader._check_index

    def swap_after_first_pass(index, canonical):
        path.write_bytes(other)
        return check_index(index, canonical)

    monkeypatch.setattr(backup_reader, "_check_index", swap_after_first_pass)
    with pytest.raises(ValueError, match="^The backup file changed while it was being read"):
        with opened_backup(path, lambda p: pytest.fail("a written backup is read as a stream"), RESTORE_TYPES):
            pass


def test_restore_that_fails_midway_changes_nothing(store, tmp_path, monkeypatch):
    _backup_payload(store, tmp_path)
    path = store.backup(tmp_path / "full.cairnmind.json.gz")
    store.add_project("after the backup", tmp_path / "after")
    before = comparable(store.export())
    edges = []

    write_graph = Store.write_graph

    def failing(self, tx, project_id, nodes, edges_of_group):
        # Edges are written after every document and vertex (in groups since #219).
        if edges_of_group and not edges:  # once: the undo writes its edges, as an engine that recovered would
            edges.extend(edges_of_group)
            raise OSError("the engine went away")
        return write_graph(self, tx, project_id, nodes, edges_of_group)

    monkeypatch.setattr(Store, "write_graph", failing)
    # The store is put back from the safety copy, and the error says so (#362).
    with pytest.raises(RestoreFailed, match="went away.*put back as it was before"):
        store.restore(path)
    assert edges and comparable(store.export()) == before


def test_a_large_row_is_read_whole_across_many_windows(store, tmp_path, monkeypatch):
    project = store.add_project("big row", tmp_path / "big")
    text = "ż" + os.urandom(150_000).hex()
    store.snapshot(project["id"], "manual://big", text)
    path = store.backup(tmp_path / "big.cairnmind.json.gz")
    monkeypatch.setattr(backup_reader, "READ_CHARS", 64)
    target = Store(tmp_path / "target")
    target.restore(path)
    assert any(row["content"] == text for row in target.export()["sources"])
