"""Phase 4b / PR #7 tests: SQLiteBackend (WAL, FTS5, CTE cycles, float32 vectors, 4-process concurrency),
schema upgrade backup, packaging extras, and zero-setup ``uvx cairnmind try`` sandbox.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tomllib
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.cli_cmds import try_demo
from cairnmind.config import Settings, resolved_backend, sqlite_db_path
from cairnmind.scip import ingest
from cairnmind.storage.sqlite_backend import (
    SQLiteBackend,
    cosine_distance,
    pack_vector,
    quote_fts5_query,
    unpack_vector,
)
from cairnmind.storage.storage_backend import ALL_COLLECTIONS, StorageBackend
from cairnmind.store import Store


def test_sqlite_fts5_safe_quoting_punctuation_and_operators(tmp_path: Path):
    data_dir = tmp_path / "data"
    store = Store(data_dir, backend="sqlite")
    try:
        assert isinstance(store.backend, StorageBackend)
        assert store.backend.backend_kind == "sqlite"
        repo = tmp_path / "repo"
        repo.mkdir()
        proj = store.add_project("fts5-edge", repo)
        src = store.snapshot(
            proj["id"],
            f"file://{repo / 'cfg.py'}",
            "API_BASE_URL = 'https://api.example.com:8443/v2'\nFLAG = '--strict-mode'\n",
            "code",
            {"root": str(repo), "relative_path": "cfg.py", "path": str(repo / "cfg.py")},
        )
        cid = str(uuid4())
        with store.transaction() as tx:
            store.backend.insert_document(
                tx,
                "chunks",
                {
                    "id": cid,
                    "project_id": proj["id"],
                    "source_id": src["id"],
                    "text": "API_BASE_URL = 'https://api.example.com:8443/v2' --strict-mode NOT OR AND",
                    "line_start": 1,
                    "line_end": 2,
                    "is_current": True,
                    "metadata": {},
                },
            )
        # Adversarial FTS5 operator words & punctuation must never raise sqlite3.OperationalError.
        assert quote_fts5_query(["--strict-mode", "NOT", 'foo"bar']) == '"--strict-mode" OR "NOT" OR "foo""bar"'
        hits = store.backend.search_chunks_fts(proj["id"], ["--strict-mode", "NOT", "OR", "API_BASE_URL"], [src["id"]], 5)
        assert any(h["id"] == cid for h in hits)
    finally:
        store.close()


def test_sqlite_vector_float32_blob_roundtrip_and_cosine():
    vec = [0.25, -0.5, 0.75, 1.0]
    blob = pack_vector(vec)
    assert len(blob) == 16
    restored = unpack_vector(blob)
    assert pytest.approx(list(restored), rel=1e-6) == vec
    assert cosine_distance([1.0, 0.0], [1.0, 0.0]) == pytest.approx(0.0, abs=1e-6)
    assert cosine_distance([1.0, 0.0], [0.0, 1.0]) == pytest.approx(1.0, abs=1e-6)


def test_sqlite_schema_upgrade_writes_pre_v_backup(tmp_path: Path):
    db_file = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute("CREATE TABLE dummy (id TEXT PRIMARY KEY)")
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
    finally:
        conn.close()

    backend = SQLiteBackend(db_file)
    try:
        backend.init_schema()
        backup = tmp_path / "state.db.pre-v3.bak"
        assert backup.is_file()
        assert int(backend._conn().execute("PRAGMA user_version").fetchone()[0]) == 4
        for name in ALL_COLLECTIONS:
            assert name in backend.list_collections()
    finally:
        backend.close()


def test_sqlite_four_process_concurrent_writes(tmp_path: Path):
    """Four OS processes concurrently writing to the same SQLite WAL store lose zero writes."""
    data_dir = tmp_path / "shared_data"
    repo = tmp_path / "repo"
    repo.mkdir()
    store = Store(data_dir, backend="sqlite")
    project = store.add_project("concurrent", repo)
    project_id = project["id"]
    store.close()

    worker_code = """
import sys
from pathlib import Path
from cairnmind.store import Store

data_dir = Path(sys.argv[1])
project_id = sys.argv[2]
worker_id = int(sys.argv[3])
store = Store(data_dir, backend="sqlite")
try:
    for i in range(6):
        src = store.snapshot(project_id, f"note://w{worker_id}-{i}", f"worker {worker_id} item {i}", "note")
        store.remember(
            project_id,
            {"kind": "fact", "key": f"w{worker_id}-k{i}", "content": f"Finding from worker {worker_id} #{i}"},
            [src["id"]],
        )
finally:
    store.close()
"""
    env = {**os.environ, "PYTHONPATH": str(Path(cli.__file__).resolve().parents[1])}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", worker_code, str(data_dir), project_id, str(wid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for wid in range(4)
    ]
    failures = []
    for p in procs:
        out, err = p.communicate(timeout=45)
        if p.returncode != 0:
            failures.append((p.returncode, out, err))
    assert failures == []

    verify = Store(data_dir, backend="sqlite")
    try:
        records = verify.list_records(project_id)
        assert len(records) == 24
        keys = {r["key"] for r in records}
        assert len(keys) == 24
    finally:
        verify.close()


def test_sqlite_graph_cycle_termination_in_memory(tmp_path: Path):
    store = Store.open_memory(tmp_path / "mem_data")
    try:
        repo = tmp_path / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        proj = store.add_project("cyc", repo)
        src = store.snapshot(proj["id"], f"file://{repo / 'a.py'}", "x=1\n", "code")
        a = {"id": "n:a", "project_id": proj["id"], "kind": "file", "label": "a.py", "source_id": src["id"], "is_current": True, "metadata": {}}
        b = {"id": "n:b", "project_id": proj["id"], "kind": "file", "label": "b.py", "source_id": src["id"], "is_current": True, "metadata": {}}
        e1 = {"id": "e:ab", "project_id": proj["id"], "from_id": "n:a", "to_id": "n:b", "kind": "imports", "source_id": src["id"], "is_current": True, "status": "observed"}
        e2 = {"id": "e:ba", "project_id": proj["id"], "from_id": "n:b", "to_id": "n:a", "kind": "imports", "source_id": src["id"], "is_current": True, "status": "observed"}
        with store.transaction() as tx:
            store.upsert_node(a, tx)
            store.upsert_node(b, tx)
            store.upsert_edge(e1, tx)
            store.upsert_edge(e2, tx)
        rows = store.backend.traverse_graph(proj["id"], ["n:a"], ["n:a", "n:b"], ["e:ab", "e:ba"], max_depth=4, limit=10)
        assert [r["id"] for r in rows] == ["n:a", "n:b"]
        assert [r["distance"] for r in rows] == [0, 1]
    finally:
        store.close()


def test_sqlite_config_and_cli_init_without_java(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / "cm-sqlite"
    repo = tmp_path / "my-repo"
    repo.mkdir()
    (repo / "app.py").write_text("DEFAULT_PORT = 8080\n", encoding="utf-8")

    # Prove ArcadeDB/Java is never invoked when --backend sqlite is selected.
    from cairnmind.storage import engine
    monkeypatch.setattr(engine, "ensure_engine", lambda *a, **k: pytest.fail("ensure_engine must not run for SQLite"))
    monkeypatch.setattr("cairnmind.agents.knowledge_mode.choose", lambda *a, **k: None)

    runner = CliRunner()
    res = runner.invoke(cli.app, ["--data-dir", str(data_dir), "init", "--backend", "sqlite", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["storage_backend"] == "sqlite"
    assert (data_dir / "state.db").is_file()

    loaded = Settings.load(data_dir)
    assert resolved_backend(loaded) == "sqlite"
    assert sqlite_db_path(loaded) == (data_dir / "state.db").resolve()

    # Open directly via SQLite Store, index, remember, and read back in read-only mode.
    store = Store(data_dir)
    try:
        proj = store.add_project("my-repo", repo)
        ingest.index_project(store, proj["id"])
        src = store.snapshot(proj["id"], "note://decision", "Port defaults to 8080", "note")
        store.remember(proj["id"], {"kind": "decision", "key": "port", "content": "Default port is 8080"}, [src["id"]])
    finally:
        store.close()

    ro = Store.open_readonly(loaded)
    try:
        assert ro.read_only is True
        assert ro.backend.backend_kind == "sqlite"
        assert len(ro.list_projects()) == 1
        with pytest.raises(RuntimeError):
            ro.command("DELETE FROM projects")
    finally:
        ro.close()


def test_try_sandbox_runs_in_readonly_cwd_and_cleans_up(tmp_path: Path, monkeypatch):
    readonly_cwd = tmp_path / "ro_cwd"
    readonly_cwd.mkdir()
    if sys.platform != "win32":
        readonly_cwd.chmod(0o555)
    created_dirs: list[Path] = []
    monkeypatch.chdir(readonly_cwd)
    try:
        result = try_demo.run_try_sandbox(
            interactive=False,
            on_tempdir_created=lambda p: created_dirs.append(p),
        )
        assert result["completed"] is True
        assert result["backend"] == "sqlite"
        assert result["ephemeral"] is True
        assert len(result["steps"]) == 3
        # Step 1: unknown on missing flag + verbatim quote on real feature
        assert result["steps"][0]["absent_result"] == "unknown"
        assert result["steps"][0]["grounded_quote"] == 'note.setdefault("tags", [])'
        # Step 2: stale check blocks done
        assert result["steps"][1]["initial_check_passed"] is True
        assert result["steps"][1]["done_blocked"] is True
        assert result["steps"][1]["error_code"] == "fingerprint_mismatch"
        # Step 3: fresh check unlocks done
        assert result["steps"][2]["fresh_check_passed"] is True
        assert result["steps"][2]["task_state"] == "done"

        # Zero files written in cwd, and temp sandbox was deleted cleanly.
        assert list(readonly_cwd.iterdir()) == []
        assert len(created_dirs) == 1 and not created_dirs[0].exists()
    finally:
        if sys.platform != "win32":
            readonly_cwd.chmod(0o755)


def test_try_cli_command_json_output(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    res = runner.invoke(cli.app, ["try", "--non-interactive", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["completed"] is True
    assert data["backend"] == "sqlite"
    assert len(data["steps"]) == 3
    assert list(tmp_path.iterdir()) == []


def test_pyproject_optional_extras_and_sample_project_bundling():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    doc = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    base_deps = doc["project"]["dependencies"]
    extras = doc["project"]["optional-dependencies"]
    assert not any(dep.startswith("github-copilot-sdk") for dep in base_deps)
    assert not any(dep.startswith("onnxruntime") for dep in base_deps)
    assert any(dep.startswith("textual") for dep in base_deps)
    assert "copilot" in extras and "nli" in extras and "all" in extras
    force_include = doc["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert force_include.get("examples/sample-project") == "cairnmind/sample_project"
