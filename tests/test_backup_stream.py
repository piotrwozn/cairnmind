"""The whole-database backup written as a stream (#196): bounded memory, same file, no partial file."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import tracemalloc

import pytest

from cairnmind import store_backup
from cairnmind.arcade import ArcadeClient
from cairnmind.config import BACKUP_FORMATS
from cairnmind.journal import JournalTransaction
from cairnmind.retrieval import Retriever
from cairnmind.service import daily_backups
from cairnmind.sql import json_text
from cairnmind.store import EXPORT_TYPES, Store
from cairnmind.store_size import _backups

FIXTURE_0_10_0 = Path(__file__).parent / "fixtures" / "backup-0.10.0.cairnmind.json.gz"
# A fixed limit for the Python memory a backup may use at its peak, and a store several times larger.
MEMORY_LIMIT = 12 * 2**20
ROW_TEXT = 1500


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "memory")


def fill(store, project_id, count, *, random=False):
    """Rows straight into the engine, a batch at a time, without building them all in the test."""
    for first in range(0, count, 500):
        rows = [{"id": f"bulk-{n}", "project_id": project_id, "source_id": "bulk",
                 "text": f"{n} " + (os.urandom(ROW_TEXT // 2).hex() if random else "x" * ROW_TEXT),
                 "is_current": True} for n in range(first, min(count, first + 500))]
        store.client.command("INSERT INTO chunks CONTENT :rows", {"rows": rows})


def varied(store, tmp_path):
    """A store with Unicode text, graph edges, a queued job and native vectors, from the 0.10.0 fixture."""
    store.restore(FIXTURE_0_10_0)
    project = store.add_project("second", tmp_path / "second")
    source = store.snapshot(project["id"], "conversation://two", "Linia dwa \"cytat\" \\ \U0001F600", "transcript")
    store.remember(project["id"], {"kind": "fact", "key": "quote", "content": "Zażółć \t tab </script>",
                                   "status": "confirmed"}, [source["id"]])
    return project


def comparable(payload):
    """Rows of every collection a restore brings back, in a stable order; the journal starts over on restore."""
    return {collection: sorted(json_text(row) for row in payload[collection])
            for collection in EXPORT_TYPES if collection not in {"journal_state", "journal_events"}} | {
        "vector_data": {name: sorted(json_text(row) for row in rows) for name, rows in payload["vector_data"].items()}}


def test_streamed_backup_has_the_bytes_of_the_in_memory_envelope(store, tmp_path, monkeypatch):
    varied(store, tmp_path)
    # Pages of a few rows, and a page budget so small that the page size shrinks and grows again.
    monkeypatch.setattr(store_backup, "PAGE_ROWS", 3)
    monkeypatch.setattr(store_backup, "PAGE_BYTES", 300)
    path = store.backup(tmp_path / "out")
    text = gzip.decompress(path.read_bytes())
    envelope = json.loads(text)
    # What the 0.10.0 restore checks before anything else, on the file as it reads it.
    assert envelope["format"] == BACKUP_FORMATS[0] and envelope["version"] == 1
    assert hashlib.sha256(json_text(envelope["payload"]).encode()).hexdigest() == envelope["sha256"]
    # The very layout the in-memory writer produced: the canonical text of the whole envelope.
    assert text == json_text(envelope).encode()
    exported = store.export()
    assert {key: value for key, value in envelope["payload"].items() if key != "exported_at"} == {
        key: value for key, value in exported.items() if key != "exported_at"}
    assert envelope["payload"]["vector_data"] and all(envelope["payload"]["vector_data"].values())
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_payload_chunks_join_to_the_canonical_text():
    payload = {"b": [{"z": 1, "a": " ż"}], "a": {"y": [[1, 2]], "x": []}, "c": None, "d": (1.5, "t")}
    assert "".join(store_backup.payload_chunks(payload)) == json_text(payload)
    assert store_backup.payload_digest(payload) == hashlib.sha256(json_text(payload).encode()).hexdigest()


def test_new_backup_restores_to_the_same_store(store, tmp_path, monkeypatch):
    varied(store, tmp_path)
    monkeypatch.setattr(store_backup, "PAGE_ROWS", 2)
    before = store.export()
    path = store.backup(tmp_path / "round-trip.cairnmind.json.gz")
    target = Store(tmp_path / "target")
    target.restore(path)
    assert comparable(target.export()) == comparable(before)
    # Vectors are searchable again after the restore, from the native index the backup recreated.
    namespace = "fixture-embedding@digest"
    project = next(row for row in target.list_projects() if row["name"] == "backup-fixture")
    found = Retriever(target, target.settings).search(project["id"], "unrelated", query_vector=[1, 0, 0],
                                                      embedding_key=namespace)
    assert found[0]["id"] == "fixture-chunk"


def test_backup_written_by_0_10_0_still_restores(store, tmp_path):
    envelope = json.loads(gzip.decompress(FIXTURE_0_10_0.read_bytes()))
    assert envelope["format"] == BACKUP_FORMATS[0] and envelope["payload"]["format_version"] == 5
    store.restore(FIXTURE_0_10_0)
    project = next(row for row in store.list_projects() if row["name"] == "backup-fixture")
    records = store.list_records(project["id"])
    assert {row["content"] for row in records if row["kind"] == "decision"} == {"Use signed tokens"}
    source = next(row for row in store.export()["sources"] if row["uri"] == "conversation://fixture-0.10.0")
    assert source["content"].startswith("Zażółć gęślą jaźń  ")
    # What a 0.10.0 file restored into is what the stream writes back out and restores again.
    restored = comparable(store.export())
    assert restored == comparable(envelope["payload"])
    again = Store(tmp_path / "again")
    again.restore(store.backup(tmp_path / "again.cairnmind.json.gz"))
    assert comparable(again.export()) == restored
    assert store.claim_job("after-restore")["payload"] == {"source_id": source["id"]}


def _stream_checksum(path):
    """Check a backup's checksum reading the file in pieces, as large files must be read."""
    head = b'{"format":' + json_text(BACKUP_FORMATS[0]).encode() + b',"payload":'
    digest, size, tail = hashlib.sha256(), 0, b""
    with gzip.open(path, "rb") as handle:
        assert handle.read(len(head)) == head
        while piece := handle.read(1 << 20):
            data = tail + piece
            # Keep back enough bytes for the closing ',"sha256":"<64 hex>","version":1}'.
            digest.update(data[:-200])
            size += len(data[:-200])
            tail = data[-200:]
    payload_end = tail.rindex(b',"sha256":')
    digest.update(tail[:payload_end])
    closing = json.loads(b"{" + tail[payload_end + 1:])
    assert closing == {"sha256": digest.hexdigest(), "version": 1}
    return size + payload_end


def _bulk_ids(path):
    """Every ``bulk-<n>`` row id in a backup, read in pieces; a list, so that a repeated row shows."""
    pattern, found, carry = re.compile(rb'"id":"bulk-(\d+)"'), [], b""
    with gzip.open(path, "rb") as handle:
        while piece := handle.read(1 << 20):
            data = carry + piece
            # A match that ends inside the carried bytes was already complete, and counted, in the last piece.
            found.extend(int(match[1]) for match in pattern.finditer(data) if match.end() > len(carry))
            carry = data[-64:]
    return found


def test_backup_peak_memory_stays_under_a_fixed_limit(store, tmp_path):
    project = store.add_project("large", tmp_path / "large")
    rows = 4 * MEMORY_LIMIT // ROW_TEXT + 1000
    fill(store, project["id"], rows)
    tracemalloc.start()
    try:
        path = store.backup(tmp_path / "large.cairnmind.json.gz")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    payload_bytes = _stream_checksum(path)
    assert payload_bytes > 4 * MEMORY_LIMIT, payload_bytes
    assert peak < MEMORY_LIMIT, f"peak {peak / 2**20:.1f} MiB for a payload of {payload_bytes / 2**20:.1f} MiB"
    # Every row is in the file exactly once: paging neither skipped nor repeated one.
    ids = _bulk_ids(path)
    assert len(ids) == rows and set(ids) == set(range(rows))


def test_pages_cover_every_row_once_across_buckets(store, monkeypatch):
    store.client.command("CREATE DOCUMENT TYPE paged_probe BUCKETS 4")
    store.client.command("INSERT INTO paged_probe CONTENT :rows", {"rows": [{"n": n} for n in range(600)]})
    # Holes and rows written after them, spread over the buckets again.
    store.client.command("DELETE FROM paged_probe WHERE n % 7 = 0")
    store.client.command("INSERT INTO paged_probe CONTENT :rows", {"rows": [{"n": n} for n in range(600, 700)]})
    monkeypatch.setattr(store_backup, "PAGE_ROWS", 7)
    with store.transaction() as tx:
        seen = [json.loads(text)["n"] for text in store_backup._pages(tx, "paged_probe")]
    assert sorted(seen) == [n for n in range(700) if n >= 600 or n % 7]


def test_backup_whose_transaction_fails_to_commit_leaves_no_file(store, tmp_path, monkeypatch):
    varied(store, tmp_path)

    def failing_flush(self):
        raise OSError("the transaction expired")

    monkeypatch.setattr(JournalTransaction, "flush", failing_flush)
    folder = tmp_path / "backups"
    with pytest.raises(OSError, match="expired"):
        store.backup(folder / f"{time.time_ns()}.cairnmind.json.gz")
    assert list(folder.iterdir()) == []


def test_payload_keys_that_are_not_text_are_refused():
    with pytest.raises(TypeError, match="keys must be strings"):
        "".join(store_backup.payload_chunks({1: []}))
    with pytest.raises(TypeError, match="keys must be strings"):
        "".join(store_backup.payload_chunks({"vector_data": {2: []}}))


def test_temporary_files_of_killed_backups_are_counted_and_old_ones_found(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    old, new = folder / ".cairnmind-backup-old", folder / ".cairnmind-backup-new"
    old.write_bytes(b"x" * 10)
    new.write_bytes(b"y" * 5)
    (folder / ".hidden").write_bytes(b"z" * 7)
    day_ago = time.time() - 2 * 86400
    os.utime(old, (day_ago, day_ago))
    assert store_backup.stale_temporaries(folder, 86400) == [old]
    assert store_backup.stale_temporaries(tmp_path / "missing", 86400) == []
    assert _backups(folder)["other"] == {"count": 2, "bytes": 15}


def test_backup_that_fails_midway_leaves_no_file(store, tmp_path, monkeypatch):
    varied(store, tmp_path)
    monkeypatch.setattr(store_backup, "PAGE_ROWS", 1)
    monkeypatch.setattr(store_backup, "WRITE_BYTES", 1)
    query, pages = ArcadeClient.query, []

    def failing(client, sql, params=None):
        if "@rid > :rid" in sql:
            pages.append(sql)
            if len(pages) == 5:
                raise OSError("disk went away")
        return query(client, sql, params)

    monkeypatch.setattr(ArcadeClient, "query", failing)
    folder = tmp_path / "backups"
    destination = folder / f"{time.time_ns()}.cairnmind.json.gz"
    with pytest.raises(OSError, match="disk went away"):
        store.backup(destination)
    assert list(folder.iterdir()) == []


@pytest.mark.parametrize("cut", ["gzip-half", "before-checksum", "without-last-brace", "mid-payload"])
def test_truncated_backup_is_refused_before_any_change(store, tmp_path, cut):
    project = varied(store, tmp_path)
    good = store.backup(tmp_path / "good.cairnmind.json.gz").read_bytes()
    text = gzip.decompress(good)
    broken = {"gzip-half": good[:len(good) // 2],
              "before-checksum": gzip.compress(text[:text.rindex(b',"sha256":')]),
              "without-last-brace": gzip.compress(text[:-1]),
              "mid-payload": gzip.compress(text[:len(text) // 2])}[cut]
    path = tmp_path / "broken.cairnmind.json.gz"
    path.write_bytes(broken)
    before = comparable(store.export())
    with pytest.raises(ValueError):
        store.restore(path)
    assert comparable(store.export()) == before
    assert project["id"] in {row["id"] for row in store.list_projects()}


KILLED_BACKUP = """
import os, sys, time
from pathlib import Path
from cairnmind import store_backup
from cairnmind.store import Store
store_backup.PAGE_ROWS, store_backup.WRITE_BYTES = 10, 1
pages = store_backup._pages
def paused(tx, name):
    for index, text in enumerate(pages(tx, name)):
        yield text
        if name == "chunks" and index == 150:
            Path(sys.argv[3]).write_text("ready")
            time.sleep(120)
store_backup._pages = paused
Store(Path(sys.argv[1])).backup(Path(sys.argv[2]))
"""


def test_backup_killed_midway_leaves_nothing_restore_accepts(store, tmp_path):
    project = store.add_project("killed", tmp_path / "killed")
    # Rows that do not compress, so that the part written before the pause reaches the disk.
    fill(store, project["id"], 300, random=True)
    folder = store.data_dir / "backups"
    folder.mkdir()
    destination = folder / f"{time.time_ns()}.cairnmind.json.gz"
    ready = tmp_path / "ready"
    child = subprocess.Popen([sys.executable, "-c", KILLED_BACKUP, str(store.data_dir), str(destination), str(ready)],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        deadline = time.monotonic() + 60
        while not ready.exists():
            assert child.poll() is None, child.stdout.read().decode(errors="replace")
            assert time.monotonic() < deadline, "the backup never reached the middle"
            time.sleep(0.02)
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # it already ended; the assertion above says how
        child.wait(timeout=30)
        child.stdout.close()
    assert not destination.exists()
    assert daily_backups(folder) == []
    partial = [path for path in folder.iterdir() if path.name.startswith(".cairnmind-backup-")]
    assert len(partial) == 1 and partial[0].stat().st_size > 0
    before = comparable(store.export())
    with pytest.raises(ValueError, match="Invalid or corrupt"):
        store.restore(partial[0])
    assert comparable(store.export()) == before
