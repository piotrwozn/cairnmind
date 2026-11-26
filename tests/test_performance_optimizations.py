"""Unit tests for snapshot, store, verification and hook CLI performance optimizations."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from cairnmind import arcade, cli_entry, memory, sql, verification
from cairnmind.config import Settings
from cairnmind.store import Store


def test_memory_encode_parse_logical_and_state_hash_equivalence():
    pid = str(uuid4())
    sid = str(uuid4())
    rid = str(uuid4())
    src = {
        "id": sid, "project_id": pid, "uri": "note://a", "kind": "note",
        "content": "hello", "sha256": hashlib.sha256(b"hello").hexdigest(),
        "metadata": {}, "is_current": True, "current_key": '["x"]',
    }
    rec = {
        "id": rid, "project_id": pid, "kind": "fact", "key": "k1", "content": "fact 1",
        "status": "proposed", "source_ids": [sid], "version": 1, "metadata": {"extra_extraction": sid},
        "active_key": '["x"]',
    }
    rev = {"id": f"{rid}:1", "project_id": pid, "record_id": rid, "version": 1, "snapshot": dict(rec)}
    payload_copy = {t: [] for t in memory.TABLES}
    payload_copy["sources"] = [memory._logical("sources", src, copy=True)]
    payload_copy["records"] = [memory._logical("records", rec, copy=True)]
    payload_copy["record_revisions"] = [memory._logical("record_revisions", rev, copy=True)]

    payload_nocopy = {t: [] for t in memory.TABLES}
    payload_nocopy["sources"] = [memory._logical("sources", src, copy=False)]
    payload_nocopy["records"] = [memory._logical("records", rec, copy=False)]
    payload_nocopy["record_revisions"] = [memory._logical("record_revisions", rev, copy=False)]

    assert payload_nocopy == payload_copy
    assert memory._state_hash(payload_nocopy, logical=True) == memory._state_hash(payload_copy, logical=False)

    objects, encoded = memory._encode_objects(payload_nocopy)
    assert objects == memory._descriptors(payload_nocopy)
    for group in objects.values():
        for desc in group:
            assert hashlib.sha256(encoded[desc["sha256"]]).hexdigest() == desc["sha256"]
            assert len(encoded[desc["sha256"]]) == desc["bytes"]

    assert memory._parse(b'{"ok":"warto\\u015b\\u0107"}') == {"ok": "wartość"}
    with pytest.raises(ValueError, match="Invalid memory package JSON"):
        memory._parse(b'{"bad":"\\ud800"}')


def test_memory_write_objects_batch_paths_and_error_cleanup(tmp_path, monkeypatch):
    objects_dir = tmp_path / "objects"
    # Empty list is a no-op
    memory._write_objects_batch(objects_dir, [])
    assert not objects_dir.exists()

    # <= 4 objects delegates to _write_whole
    small = [(objects_dir / f"{i}.json", f'{{"n":{i}}}'.encode()) for i in range(2)]
    memory._write_objects_batch(objects_dir, small)
    assert (objects_dir / "0.json").read_bytes() == b'{"n":0}'

    # Symlink objects dir rejected
    if hasattr(os, "symlink"):
        sym_dir = tmp_path / "sym_objects"
        try:
            sym_dir.symlink_to(objects_dir, target_is_directory=True)
        except OSError:
            sym_dir = None
        if sym_dir is not None:
            many = [(sym_dir / f"a{i}.json", b"{}") for i in range(6)]
            with pytest.raises(ValueError, match="symbolic links"):
                memory._write_objects_batch(sym_dir, many)

    # Linux syncfs path & fallback path tested deterministically
    many_dir = tmp_path / "many"
    items = [(many_dir / f"f{i}.json", f'{{"i":{i}}}'.encode()) for i in range(6)]
    monkeypatch.setattr(memory.sys, "platform", "linux")
    monkeypatch.setattr(memory, "_try_syncfs", lambda d: True)
    monkeypatch.setattr(memory.os, "fchmod", lambda fd, mode: None, raising=False)
    monkeypatch.setattr(memory, "atomic_replace", lambda src, dst: Path(src).replace(dst))
    memory._write_objects_batch(many_dir, items)
    assert (many_dir / "f5.json").read_bytes() == b'{"i":5}'

    # Fallback when _try_syncfs returns False
    items2 = [(many_dir / f"g{i}.json", f'{{"g":{i}}}'.encode()) for i in range(6)]
    monkeypatch.setattr(memory, "_try_syncfs", lambda d: False)
    fsync_calls = []
    real_open = os.open
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    monkeypatch.setattr(memory.os, "O_NOFOLLOW", no_follow, raising=False)

    def compat_open(p, flags, *a):
        if flags == (os.O_RDONLY | no_follow) and os.name == "nt":
            return real_open(p, os.O_RDWR)
        return real_open(p, flags, *a)

    monkeypatch.setattr(memory.os, "open", compat_open)
    with patch.object(memory.os, "fsync", side_effect=lambda fd: fsync_calls.append(fd)):
        monkeypatch.setattr(memory, "_REAL_OS_FSYNC", memory.os.fsync)
        memory._write_objects_batch(many_dir, items2)
    assert len(fsync_calls) == 6
    assert (many_dir / "g4.json").read_bytes() == b'{"g":4}'

    # Non-linux multi-threaded fsync path + error cleanup
    win_dir = tmp_path / "win"
    items3 = [(win_dir / f"w{i}.json", f'{{"w":{i}}}'.encode()) for i in range(6)]
    monkeypatch.setattr(memory.sys, "platform", "win32")
    monkeypatch.setattr(memory, "make_private", lambda p: None)
    monkeypatch.setattr(memory, "_REAL_OS_FSYNC", memory.os.fsync)
    memory._write_objects_batch(win_dir, items3)
    assert (win_dir / "w0.json").read_bytes() == b'{"w":0}'

    def fail_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(memory, "atomic_replace", fail_replace)
    items4 = [(win_dir / f"z{i}.json", b"{}") for i in range(6)]
    with pytest.raises(OSError, match="disk full"):
        memory._write_objects_batch(win_dir, items4)
    assert list(win_dir.glob(".cairnmind-memory-*")) == []


def test_memory_unchanged_since_receipt_fast_path(tmp_path, monkeypatch):
    pid = str(uuid4())
    root = tmp_path / "repo"
    root.mkdir()
    pkg_dir = root / memory.MEMORY_RELATIVE
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "objects").mkdir()

    empty = {t: [] for t in memory.TABLES}
    manifest = {
        "format": memory.FORMAT, "format_version": memory.VERSION,
        "project": {"id": pid, "name": "demo"},
        "origin_root": str(root.resolve()),
        "root_aliases": [str(root.resolve())],
        "ancestors": [], "parent_id": None, "active_plan_id": None,
        "objects": {t: [] for t in memory.TABLES}, "counts": {t: 0 for t in memory.TABLES},
        "state_hash": memory._state_hash(empty), "created_at": "2026-11-26T00:00:00Z",
    }
    manifest["snapshot_id"] = memory._manifest_id(manifest)
    raw = memory._bytes(manifest)
    mpath = pkg_dir / "manifest.json"
    mpath.write_bytes(raw)
    msha = hashlib.sha256(raw).hexdigest()

    state = {"epoch": str(uuid4()), "seq": 10, "first_seq": 1}
    receipt = {
        "id": "rcpt-1", "project_id": pid, "snapshot_id": manifest["snapshot_id"],
        "path": str(mpath), "manifest_sha": msha, "journal_cursor": {"epoch": state["epoch"], "seq": 10},
    }
    events_flag: list[dict] = []
    aliases_rows: list[dict] = []

    class FakeStore:
        def query(self, query_text, params=None):
            if "FROM projects WHERE" in query_text:
                return [{"id": pid, "name": "demo", "root": str(root.resolve())}]
            if "FROM project_roots WHERE" in query_text:
                return [{"id": "pr1"}]
            if "FROM memory_snapshots WHERE" in query_text:
                return [receipt]
            if "FROM journal_state WHERE" in query_text:
                return [state]
            if "FROM journal_events WHERE" in query_text:
                return list(events_flag)
            if "FROM memory_bindings WHERE" in query_text:
                return list(aliases_rows)
            return []

    monkeypatch.setattr("cairnmind.project_folders.require_here", lambda store, p, r: None)
    mgr = memory.MemoryManager(FakeStore())

    # Exact seq match -> unchanged
    res = mgr._unchanged_since_receipt(pid, root)
    assert res is not None and res["status"] == "unchanged" and res["snapshot_id"] == manifest["snapshot_id"]

    # seq advanced only by non-snapshot events and unchanged root_aliases -> still unchanged
    state["seq"] = 12
    assert mgr._unchanged_since_receipt(pid, root)["status"] == "unchanged"

    # Snapshot-relevant journal event -> falls back to None
    events_flag.append({"entity_type": "record"})
    assert mgr._unchanged_since_receipt(pid, root) is None
    events_flag.clear()

    # Aliases changed -> falls back to None
    aliases_rows.append({"origin_root": "/other/root"})
    assert mgr._unchanged_since_receipt(pid, root) is None
    aliases_rows.clear()

    # Manifest edited externally -> falls back to None
    receipt["manifest_sha"] = "0" * 64
    assert mgr._unchanged_since_receipt(pid, root) is None


def test_evidence_time_source_rows_cache_matches_query_path():
    rows = {
        "s1": {"id": "s1", "created_at": "2026-01-02T00:00:00+00:00", "metadata": {}},
        "s2": {"id": "s2", "created_at": "2026-01-03T00:00:00+00:00",
               "metadata": {"source_timestamp": "2026-01-01T12:00:00+00:00"}},
        "s3": {"id": "s3", "created_at": "2026-01-04T00:00:00+00:00", "metadata": {"origin": "local_user_review"}},
    }

    class QueryTx:
        def __init__(self):
            self.calls = 0

        def query(self, statement, params=None):
            self.calls += 1
            src = rows.get(params["id"])
            if not src:
                return []
            meta = src["metadata"]
            return [{"created_at": src["created_at"], "stated": meta.get("source_timestamp"), "origin": meta.get("origin")}]

    tx_uncached = QueryTx()
    expected = sql.evidence_time(tx_uncached, ["s1", "s2", "s3"])
    assert tx_uncached.calls == 3

    tx_cached = QueryTx()
    actual = sql.evidence_time(tx_cached, ["s1", "s2", "s3"], source_rows=rows)
    assert actual == expected
    assert actual["basis"] == "mixed"
    assert tx_cached.calls == 0


def test_store_load_checked_sources_and_record_graph_unit():
    pid = str(uuid4())
    sid = str(uuid4())
    src_row = {"id": sid, "project_id": pid, "uri": "note://1", "kind": "note", "is_current": True}
    existing_src_node = {
        "id": f"source:{sid}", "project_id": pid, "kind": "source", "label": "note://1",
        "source_id": sid, "is_current": True, "metadata": {"source_id": sid, "source_kind": "note"},
    }

    class FakeTx:
        def __init__(self):
            self.known = None
            self.keeps_known = False
            self.queries = []
            self.commands = []

        def query(self, statement, params=None):
            self.queries.append((statement, params))
            if "FROM sources WHERE id IN :ids" in statement:
                return [src_row] if sid in params["ids"] else []
            if "FROM nodes WHERE id IN :ids" in statement:
                return [existing_src_node] if f"source:{sid}" in params["ids"] else []
            return []

        def command(self, statement, params=None):
            self.commands.append((statement, params))
            return []

    store = Store.__new__(Store)
    tx = FakeTx()
    with pytest.raises(ValueError, match="at least one source ID"):
        Store._load_checked_sources(tx, pid, [])
    with pytest.raises(ValueError, match="same project"):
        Store._load_checked_sources(tx, pid, ["missing-source"])

    loaded = Store._load_checked_sources(tx, pid, [sid])
    assert loaded[sid]["uri"] == "note://1"

    record = {
        "id": str(uuid4()), "project_id": pid, "kind": "fact", "content": "content",
        "status": "proposed", "source_ids": [sid], "metadata": {"depends_on": []},
    }
    store._record_graph(tx, record, source_rows=loaded)
    # `tx.known` is cleanly restored to None and source node update was skipped cleanly
    assert tx.known is None
    assert not any("UPDATE nodes" in stmt for stmt, _ in tx.commands)
    assert any(stmt.startswith("CREATE VERTEX nodes") for stmt, _ in tx.commands)
    assert any(stmt.startswith("CREATE EDGE edges") for stmt, _ in tx.commands)


def test_arcade_client_preparsed_url_safety(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, arcade_url="http://127.0.0.1:2480", arcade_user="root",
                        arcade_password="secret", arcade_database="cairnmind")
    sent_paths = []

    class DummyResponse:
        status_code = 200
        is_success = True
        content = b'{"result":[]}'
        stream = None

        def json(self):
            return json.loads(self.content)

    class DummyPool:
        def client(self):
            return self

        def request(self, method, path, **kwargs):
            sent_paths.append(path)
            return DummyResponse()

    client = arcade.ArcadeClient(settings, _connections=DummyPool())
    assert client.query("SELECT 1") == []
    assert sent_paths == ["/api/v1/query/cairnmind"]

    with pytest.raises(ValueError, match="configured HTTP origin"):
        client._request("GET", "/api/v1/server#fragment")


def test_verification_manifest_parallel_matches_sequential(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "sub").mkdir(parents=True)
    files = [f"sub/file_{i:03d}.py" for i in range(70)]
    for idx, rel in enumerate(files):
        (root / rel).write_text(f"VAL = {idx}\n", encoding="utf-8")

    stage_bytes = b"\0".join(f"100644 deadbeef 0\t{rel}".encode() for rel in files) + b"\0"

    def fake_git(r, *args):
        if "--stage" in args:
            return stage_bytes
        if "--others" in args:
            return b""
        raise AssertionError(f"Unexpected git args: {args}")

    monkeypatch.setattr(verification, "_git", fake_git)

    parallel_entries, parallel_unknowns = verification._manifest(root, [])
    monkeypatch.setattr(verification, "PARALLEL_HASH_THRESHOLD", 10_000)
    seq_entries, seq_unknowns = verification._manifest(root, [])

    assert parallel_entries == seq_entries
    assert parallel_unknowns == seq_unknowns == []
    assert len(parallel_entries) == 70


def test_cli_entry_hook_fast_path_and_fallback(tmp_path, monkeypatch, capsys):
    assert cli_entry._parse_hook_argv(["hook", "--client", "claude"]) == (None, "claude")
    assert cli_entry._parse_hook_argv(["--data-dir", "/tmp/d", "hook", "--client=codex"]) == ("/tmp/d", "codex")
    assert cli_entry._parse_hook_argv(["--data-dir=/tmp/d", "hook", "--client", "codex"]) == ("/tmp/d", "codex")
    assert cli_entry._parse_hook_argv(["hook", "--help"]) is None
    assert cli_entry._parse_hook_argv(["--help"]) is None
    assert cli_entry._parse_hook_argv(["--data-dir", "-bad", "hook", "--client", "claude"]) is None
    assert cli_entry._parse_hook_argv(["status"]) is None

    # Non-hook falls back to cli.entrypoint
    called = []
    monkeypatch.setattr("cairnmind.cli.entrypoint", lambda: called.append(True))
    cli_entry.entrypoint(["status"])
    assert called == [True]

    # Fast-path hook runs without touching cli module when argv matches
    calls = []
    monkeypatch.setattr("cairnmind.integrations.hook", lambda s, p, c: calls.append((s.data_dir, p, c)) or {"ok": True})
    monkeypatch.setattr("cairnmind.cli_entry.sys.stdin", SimpleNamespace(read=lambda: '{"hook_event_name":"Stop"}'))
    data_dir = tmp_path / "data"
    cli_entry.entrypoint(["--data-dir", str(data_dir), "hook", "--client", "claude"])
    out = capsys.readouterr()
    assert json.loads(out.out.strip()) == {"ok": True}
    assert calls[0][1] == {"hook_event_name": "Stop"} and calls[0][2] == "claude"

    # Broken JSON stdin logs cleanly to stderr matching cli.hook
    monkeypatch.setattr("cairnmind.cli_entry.sys.stdin", SimpleNamespace(read=lambda: "{broken"))
    cli_entry.entrypoint(["--data-dir", str(data_dir), "hook", "--client", "claude"])
    assert "Cairnmind hook:" in capsys.readouterr().err

    # Subprocess check via `python -m cairnmind`: covers __main__.py + confirms no heavy CLI imports
    repo_root = Path(__file__).resolve().parent.parent
    probe = subprocess.run(
        [
            sys.executable, "-c",
            "import runpy, sys; sys.argv = ['cairnmind', '--data-dir', sys.argv[1], 'hook', '--client', 'claude']; "
            "runpy.run_module('cairnmind', run_name='__main__'); "
            "assert 'typer' not in sys.modules and 'fastapi' not in sys.modules and 'textual' not in sys.modules",
            str(data_dir),
        ],
        input='{"hook_event_name":"Ignore"}\n',
        text=True,
        capture_output=True,
        cwd=str(repo_root),
        check=True,
    )
    assert probe.returncode == 0


def test_memory_and_store_optimization_edge_branches(tmp_path, monkeypatch):
    # _load_snapshot_impl rejects symlink objects directory
    base = tmp_path / "pkg"
    base.mkdir()
    real_obj = tmp_path / "outside_obj"
    real_obj.mkdir()
    if hasattr(os, "symlink"):
        try:
            (base / "objects").symlink_to(real_obj, target_is_directory=True)
        except OSError:
            pass
        if (base / "objects").is_symlink():
            empty = {t: [] for t in memory.TABLES}
            m = {
                "format": memory.FORMAT, "format_version": memory.VERSION,
                "project": {"id": str(uuid4()), "name": "Demo"},
                "origin_root": "/abs/root", "root_aliases": ["/abs/root"],
                "ancestors": [], "parent_id": None, "active_plan_id": None,
                "objects": {t: [] for t in memory.TABLES}, "counts": {t: 0 for t in memory.TABLES},
                "state_hash": memory._state_hash(empty), "created_at": "2026-01-01T00:00:00Z",
            }
            m["snapshot_id"] = memory._manifest_id(m)
            mpath = base / "manifest.json"
            mpath.write_bytes(memory._bytes(m))
            with pytest.raises(ValueError, match="escapes its package"):
                memory._load_snapshot_impl(mpath)

    # _try_syncfs non-linux and missing libc symbol fallback
    monkeypatch.setattr(memory.sys, "platform", "win32")
    assert memory._try_syncfs(tmp_path) is False
    monkeypatch.setattr(memory.sys, "platform", "linux")
    monkeypatch.setattr(memory, "_LIBC_SYNCFS_LOADED", False)
    monkeypatch.setattr(memory, "_LIBC_SYNCFS", None)
    monkeypatch.setattr(memory.ctypes, "CDLL", lambda *a, **k: (_ for _ in ()).throw(OSError("no libc")))
    assert memory._try_syncfs(tmp_path) is False

    # _write_objects_batch per-target symlink guards and staging failure cleanup
    if hasattr(os, "symlink"):
        target_sym_dir = tmp_path / "symtargets"
        target_sym_dir.mkdir()
        sym_file = target_sym_dir / "link.json"
        real_f = tmp_path / "real.json"
        real_f.write_bytes(b"{}")
        try:
            sym_file.symlink_to(real_f)
        except OSError:
            sym_file = None
        if sym_file is not None:
            batch = [(sym_file, b"{}"), *[(target_sym_dir / f"x{i}.json", b"{}") for i in range(5)]]
            monkeypatch.setattr(memory.sys, "platform", "linux")
            monkeypatch.setattr(memory, "_REAL_OS_FSYNC", memory.os.fsync)
            with pytest.raises(ValueError, match="symbolic links"):
                memory._write_objects_batch(target_sym_dir, batch)
            monkeypatch.setattr(memory.sys, "platform", "win32")
            with pytest.raises(ValueError, match="symbolic links"):
                memory._write_objects_batch(target_sym_dir, batch)

    fail_stage_dir = tmp_path / "failstage"
    monkeypatch.setattr(memory.sys, "platform", "win32")
    monkeypatch.setattr(memory, "_REAL_OS_FSYNC", memory.os.fsync)
    monkeypatch.setattr(memory, "make_private", lambda p: (_ for _ in ()).throw(PermissionError("acl blocked")))
    with pytest.raises(PermissionError, match="acl blocked"):
        memory._write_objects_batch(fail_stage_dir, [(fail_stage_dir / f"f{i}.json", b"{}") for i in range(6)])
    assert list(fail_stage_dir.glob(".cairnmind-memory-*")) == []

    # _capture linked extra nodes, _receipt missing journal fallback, and _unchanged_since_receipt edge exits
    pid = str(uuid4())
    root = tmp_path / "repo2"
    root.mkdir()
    extra_node = {"id": "custom:1", "project_id": pid, "kind": "custom", "label": "Custom", "metadata": {}}
    edge_row = {"id": "e:1", "project_id": pid, "from_id": "custom:1", "to_id": "custom:1", "kind": "relates_to"}

    class FakeCaptureStore:
        def query(self, statement, params=None):
            if "FROM projects WHERE" in statement:
                return [{"id": pid, "name": "Demo", "root": str(root.resolve())}]
            if "FROM project_roots WHERE" in statement:
                return [{"id": "pr1"}]
            if "kind NOT IN :code_edges" in statement:
                return [edge_row]
            if "kind IN :kept_nodes" in statement:
                return []
            if "FROM nodes WHERE project_id=:project AND id IN :ids" in statement:
                return [extra_node]
            if "FROM journal_state" in statement:
                return []
            return []

        def command(self, statement, params=None):
            return []

        def _require_project(self, tx, project_id):
            return None

    monkeypatch.setattr("cairnmind.project_folders.require_here", lambda store, p, r: None)
    mgr = memory.MemoryManager(FakeCaptureStore())
    _, captured_payload = mgr._capture(pid, root, FakeCaptureStore())
    assert [n["id"] for n in captured_payload["nodes"]] == ["custom:1"]

    # _receipt when journal is unavailable stores journal_cursor=None
    inserted_receipts = []
    monkeypatch.setattr(memory, "insert_document", lambda tx, col, row: inserted_receipts.append((col, row)))
    mgr._receipt(FakeCaptureStore(), pid, root / memory.MEMORY_RELATIVE / "manifest.json", b"{}",
                 {"snapshot_id": "s1", "state_hash": "h1", "origin_root": str(root)}, "local")
    assert inserted_receipts[-1][1]["journal_cursor"] is None

    # _unchanged_since_receipt early-return branches
    assert mgr._unchanged_since_receipt(pid, root) is None
    (root / memory.EARLIER_MEMORY_RELATIVE).mkdir(parents=True)
    (root / memory.EARLIER_MEMORY_RELATIVE / "manifest.json").write_text("{}", encoding="utf-8")
    assert mgr._unchanged_since_receipt(pid, root) is None
    (root / memory.EARLIER_MEMORY_RELATIVE / "manifest.json").unlink()
    pkg = root / memory.MEMORY_RELATIVE
    pkg.mkdir(parents=True)
    mfile = pkg / "manifest.json"
    mfile.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(memory, "is_reparse_point", lambda p: p == pkg)
    assert mgr._unchanged_since_receipt(pid, root) is None
    monkeypatch.setattr(memory, "is_reparse_point", lambda p: False)

    # _unchanged_since_receipt epoch mismatch -> None
    valid_m = {
        "format": memory.FORMAT, "format_version": memory.VERSION,
        "project": {"id": pid, "name": "Demo"},
        "origin_root": str(root.resolve()), "root_aliases": [str(root.resolve())],
        "ancestors": [], "parent_id": None, "active_plan_id": None,
        "objects": {t: [] for t in memory.TABLES}, "counts": {t: 0 for t in memory.TABLES},
        "state_hash": memory._state_hash({t: [] for t in memory.TABLES}), "created_at": "2026-01-01T00:00:00Z",
    }
    valid_m["snapshot_id"] = memory._manifest_id(valid_m)
    mraw = memory._bytes(valid_m)
    mfile.write_bytes(mraw)
    rcpt = {
        "id": "r1", "snapshot_id": valid_m["snapshot_id"],
        "manifest_sha": hashlib.sha256(mraw).hexdigest(),
        "journal_cursor": {"epoch": "old-epoch", "seq": 1},
    }

    class EpochMismatchStore(FakeCaptureStore):
        def query(self, statement, params=None):
            if "FROM memory_snapshots WHERE" in statement:
                return [rcpt]
            if "FROM journal_state WHERE" in statement:
                return [{"epoch": "new-epoch", "seq": 1, "first_seq": 1}]
            return super().query(statement, params)

    assert memory.MemoryManager(EpochMismatchStore())._unchanged_since_receipt(pid, root) is None

    # Store._record_graph missing source error + Store._derive_symbol_anchor with & without source_rows
    class MissingSrcTx:
        def query(self, statement, params=None):
            return []

    st = Store.__new__(Store)
    with pytest.raises(ValueError, match="Source does not exist"):
        st._record_graph(MissingSrcTx(), {"id": "r1", "project_id": pid, "kind": "fact", "content": "x",
                                           "status": "proposed", "source_ids": ["absent"], "metadata": {}})

    code_src = {
        "id": "code-1", "uri": "app.py", "kind": "code",
        "content": "def helper():\n    return 1\n",
        "metadata": {"relative_path": "app.py"},
    }

    class CodeQueryTx:
        def query(self, statement, params=None):
            return [code_src]

    anchor_cached = st._derive_symbol_anchor(CodeQueryTx(), pid, ["code-1"], {"symbol": "helper"},
                                             source_rows={"code-1": code_src})
    anchor_queried = st._derive_symbol_anchor(CodeQueryTx(), pid, ["code-1"], {"symbol": "helper"})
    assert anchor_cached is not None and anchor_cached == anchor_queried

