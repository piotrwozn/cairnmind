"""ArcadeDB integration tests for SCIP ingestion, code graph, preservation, CLI, and background worker."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from cairnmind.cli import app as cli_app
from cairnmind.config import Settings
from cairnmind.scip.ingest import index_project
from cairnmind.scip.scip_ingest import apply_scip_index, enqueue_project_scip
from cairnmind.scip.scip_proto import (
    ROLE_DEFINITION,
    ROLE_IMPORT,
    ROLE_READ_ACCESS,
    ScipDocument,
    ScipIndex,
    ScipMetadata,
    ScipOccurrence,
    ScipRelationship,
    ScipSymbolInfo,
    ScipToolInfo,
    encode_scip_index,
)
from cairnmind.service import create_app
from cairnmind.store import Store


def _make_sample_go_project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "go.mod").write_text("module example.com/billing\n\ngo 1.22\n", encoding="utf-8")
    (root / "pkg").mkdir(exist_ok=True)
    (root / "pkg" / "engine.go").write_text(
        "package pkg\n\n"
        "type Calculator interface {\n"
        "    Compute(x int) int\n"
        "}\n\n"
        "type BillingEngine struct {\n"
        "    TaxRate int\n"
        "}\n\n"
        "func (b *BillingEngine) Compute(x int) int {\n"
        "    return x + b.TaxRate\n"
        "}\n",
        encoding="utf-8",
    )
    (root / "main.go").write_text(
        "package main\n\n"
        'import "fmt"\n'
        'import "example.com/billing/pkg"\n\n'
        "func main() {\n"
        "    eng := &pkg.BillingEngine{TaxRate: 5}\n"
        "    fmt.Println(eng.Compute(10))\n"
        "}\n",
        encoding="utf-8",
    )
    (root / "main_test.go").write_text(
        "package main\n\n"
        'import "testing"\n'
        'import "example.com/billing/pkg"\n\n'
        "func TestCompute(t *testing.T) {\n"
        "    eng := &pkg.BillingEngine{TaxRate: 2}\n"
        "    _ = eng.Compute(3)\n"
        "}\n",
        encoding="utf-8",
    )
    return root


def _build_go_scip_bytes() -> bytes:
    sym_calc = "scip-go gomod example.com/billing v0.1.0 pkg/Calculator#"
    sym_engine = "scip-go gomod example.com/billing v0.1.0 pkg/BillingEngine#"
    sym_tax = "scip-go gomod example.com/billing v0.1.0 pkg/BillingEngine#TaxRate."
    sym_compute = "scip-go gomod example.com/billing v0.1.0 pkg/BillingEngine#Compute()."
    sym_main = "scip-go gomod example.com/billing v0.1.0 main()."
    sym_test = "scip-go gomod example.com/billing v0.1.0 TestCompute()."
    sym_fmt = "scip-go gomod std 1.22 fmt/"

    index = ScipIndex(
        metadata=ScipMetadata(
            tool_info=ScipToolInfo(name="scip-go", version="v0.2.10"),
        ),
        documents=[
            ScipDocument(
                relative_path="pkg/engine.go",
                language="go",
                occurrences=[
                    ScipOccurrence(2, 5, 2, 15, sym_calc, ROLE_DEFINITION, 2, 4),
                    ScipOccurrence(6, 5, 6, 18, sym_engine, ROLE_DEFINITION, 6, 8),
                    ScipOccurrence(7, 4, 7, 11, sym_tax, ROLE_DEFINITION, 7, 7),
                    ScipOccurrence(10, 24, 10, 31, sym_compute, ROLE_DEFINITION, 10, 12),
                ],
                symbols=[
                    ScipSymbolInfo(symbol=sym_calc, kind=21, display_name="Calculator"),
                    ScipSymbolInfo(
                        symbol=sym_engine,
                        kind=49,
                        display_name="BillingEngine",
                        relationships=[ScipRelationship(symbol=sym_calc, is_implementation=True)],
                    ),
                    ScipSymbolInfo(symbol=sym_tax, kind=15, display_name="TaxRate"),
                    ScipSymbolInfo(symbol=sym_compute, kind=26, display_name="Compute"),
                ],
            ),
            ScipDocument(
                relative_path="main.go",
                language="go",
                occurrences=[
                    ScipOccurrence(2, 7, 2, 12, sym_fmt, ROLE_IMPORT),
                    ScipOccurrence(5, 5, 5, 9, sym_main, ROLE_DEFINITION, 5, 8),
                    ScipOccurrence(6, 16, 6, 29, sym_engine, ROLE_READ_ACCESS),
                    ScipOccurrence(7, 20, 7, 27, sym_compute, ROLE_READ_ACCESS),
                ],
                symbols=[ScipSymbolInfo(symbol=sym_main, kind=17, display_name="main")],
            ),
            ScipDocument(
                relative_path="main_test.go",
                language="go",
                occurrences=[
                    ScipOccurrence(5, 5, 5, 16, sym_test, ROLE_DEFINITION, 5, 8),
                    ScipOccurrence(6, 16, 6, 29, sym_engine, ROLE_READ_ACCESS),
                    ScipOccurrence(7, 12, 7, 19, sym_compute, ROLE_READ_ACCESS),
                ],
                symbols=[ScipSymbolInfo(symbol=sym_test, kind=17, display_name="TestCompute")],
            ),
        ],
    )
    return encode_scip_index(index)


def test_scip_import_builds_full_arcadedb_graph_and_survives_reindexing(tmp_path):
    store = Store(tmp_path / "data")
    repo = _make_sample_go_project(tmp_path / "go_app")
    project = store.add_project("billing", repo)
    pid = project["id"]

    # Fast initial index treats .go files as text without SCIP symbols
    first = index_project(store, pid)
    assert first["files"] >= 3
    before_symbols = store.query("SELECT id,label FROM nodes WHERE project_id=:p AND kind='symbol'", {"p": pid})
    assert before_symbols == []

    # Apply binary SCIP index
    scip_file = tmp_path / "billing.scip"
    scip_file.write_bytes(_build_go_scip_bytes())
    applied = apply_scip_index(store, pid, repo, scip_file)
    assert applied["files_updated"] == 3
    assert applied["symbols"] == 6
    assert applied["calls"] >= 2
    assert applied["references"] >= 3

    nodes = store.query("SELECT id,kind,label,metadata FROM nodes WHERE project_id=:p AND is_current=true", {"p": pid})
    edges = store.query("SELECT from_id,to_id,kind FROM edges WHERE project_id=:p AND is_current=true", {"p": pid})

    sym_labels = {n["label"]: n for n in nodes if n["kind"] == "symbol"}
    assert "BillingEngine" in sym_labels
    assert "BillingEngine.Compute" in sym_labels
    assert "BillingEngine.TaxRate" in sym_labels
    assert "Calculator" in sym_labels
    assert "main" in sym_labels
    assert "TestCompute" in sym_labels
    assert sym_labels["Calculator"]["metadata"]["kind"] == "interface"
    assert sym_labels["BillingEngine.Compute"]["metadata"]["owner"] == "BillingEngine"

    edge_kinds = {e["kind"] for e in edges}
    assert {"defines", "imports", "test_imports", "calls", "references"}.issubset(edge_kinds)
    ext_nodes = [n for n in nodes if n["kind"] == "external_module"]
    assert any("std" in n["label"] or "fmt" in n["label"] for n in ext_nodes)

    # Verify cross-file call edge: main -> BillingEngine.Compute
    main_id = sym_labels["main"]["id"]
    compute_id = sym_labels["BillingEngine.Compute"]["id"]
    assert any(e["kind"] == "calls" and e["from_id"] == main_id and e["to_id"] == compute_id for e in edges)

    # Verify implementation reference edge: BillingEngine -> Calculator
    engine_id = sym_labels["BillingEngine"]["id"]
    calc_id = sym_labels["Calculator"]["id"]
    assert any(e["kind"] == "references" and e["from_id"] == engine_id and e["to_id"] == calc_id for e in edges)

    # Re-running fast index_project on unchanged files MUST preserve SCIP symbols & edges!
    second = index_project(store, pid)
    assert second["changed"] == 0
    nodes_after = store.query("SELECT id,label FROM nodes WHERE project_id=:p AND kind='symbol' AND is_current=true", {"p": pid})
    assert len(nodes_after) == len(sym_labels)

    # Editing main.go invalidates stale SCIP analysis for main.go while preserving pkg/engine.go!
    (repo / "main.go").write_text(
        (repo / "main.go").read_text(encoding="utf-8") + "\n// edited\n",
        encoding="utf-8",
    )
    third = index_project(store, pid)
    assert third["changed"] == 1
    remaining_syms = {
        n["label"]
        for n in store.query("SELECT label FROM nodes WHERE project_id=:p AND kind='symbol' AND is_current=true", {"p": pid})
    }
    assert "BillingEngine.Compute" in remaining_syms
    assert "main" not in remaining_syms  # stale line numbers cleared on edit


def test_cli_project_add_scip_auto_vs_no_scip_and_scip_import(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    repo = _make_sample_go_project(tmp_path / "go_cli")
    scip_file = tmp_path / "index.scip"
    scip_file.write_bytes(_build_go_scip_bytes())

    monkeypatch.setenv("CAIRNMIND_SCIP_MODE", "auto")
    runner = CliRunner()

    # 1. project add --no-scip does not queue a scip_index job
    res_skip = runner.invoke(
        cli_app,
        ["--data-dir", str(data_dir), "project", "add", "go-skip", str(repo), "--no-scip", "--json"],
    )
    assert res_skip.exit_code == 0, res_skip.output
    body_skip = json.loads(res_skip.output)
    assert "scip" not in body_skip

    # 2. project add (auto mode) detects Go and enqueues a scip_index job without blocking
    repo2 = _make_sample_go_project(tmp_path / "go_cli_auto")
    res_auto = runner.invoke(
        cli_app,
        ["--data-dir", str(data_dir), "project", "add", "go-auto", str(repo2), "--json"],
    )
    assert res_auto.exit_code == 0, res_auto.output
    body_auto = json.loads(res_auto.output)
    assert body_auto["scip"]["queued"] is True
    assert "go" in body_auto["scip"]["detected_languages"]
    assert body_auto["scip"]["job_id"]

    # 3. cairnmind scip import ingests external index.scip into ArcadeDB
    res_imp = runner.invoke(
        cli_app,
        ["--data-dir", str(data_dir), "scip", "import", "go-skip", str(scip_file)],
    )
    assert res_imp.exit_code == 0, res_imp.output
    imp_body = json.loads(res_imp.output)
    assert imp_body["files_updated"] == 3
    assert imp_body["symbols"] == 6

    # 4. cairnmind scip status is read-only and reports Go detected
    res_status = runner.invoke(
        cli_app,
        ["--data-dir", str(data_dir), "scip", "status", "go-skip"],
    )
    assert res_status.exit_code == 0, res_status.output
    st_body = json.loads(res_status.output)
    assert any(item["language"] == "go" for item in st_body["detected_languages"])
    assert "scip-go" in st_body["indexers"]


def test_service_scip_worker_processes_queued_job_in_background(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRNMIND_SCIP_MODE", "auto")
    settings = Settings.load(tmp_path / "data", create=True)
    settings.poll_seconds = 0.2
    settings.save()
    store = Store(settings.data_dir)
    repo = _make_sample_go_project(tmp_path / "go_svc")
    project = store.add_project("go-svc", repo)
    pid = project["id"]
    index_project(store, pid)

    # Mock index_project_scip so the background worker applies our binary SCIP fixture cleanly
    scip_file = tmp_path / "svc.scip"
    scip_file.write_bytes(_build_go_scip_bytes())

    def fake_index_project_scip(st, cfg, project_id, *, languages=None, worktree=None, allow_download=True):
        applied = apply_scip_index(st, project_id, Path(worktree or repo), scip_file)
        return {"project_id": project_id, "errors": [], "indexers": [applied]}

    monkeypatch.setattr("cairnmind.scip.scip_ingest.index_project_scip", fake_index_project_scip)

    queued = enqueue_project_scip(store, settings, pid, repo)
    assert queued["queued"] is True
    job_id = queued["job_id"]

    app = create_app(settings, start_worker=True, background_inference=False, store=store)
    with TestClient(app):
        # Wait up to 8s for scip_worker to claim & complete the job
        end = time.monotonic() + 8.0
        state = "pending"
        while time.monotonic() < end:
            row = store.query("SELECT state FROM jobs WHERE id=:id", {"id": job_id})
            state = row[0]["state"] if row else "missing"
            if state == "completed":
                break
            time.sleep(0.15)
        assert state == "completed"

    sym_labels = {
        n["label"]
        for n in store.query("SELECT label FROM nodes WHERE project_id=:p AND kind='symbol' AND is_current=true", {"p": pid})
    }
    assert "BillingEngine.Compute" in sym_labels


def test_index_project_scip_paths_cli_install_index_and_edge_cases(tmp_path, monkeypatch):
    from cairnmind.scip import scip_ingest as si
    from cairnmind.scip.scip_manager import InstalledIndexer, ScipRunError

    settings = Settings.load(tmp_path / "data", create=True)
    store = Store(settings.data_dir)
    repo = _make_sample_go_project(tmp_path / "go_full")
    proj = store.add_project("go-full", repo)
    pid = proj["id"]
    index_project(store, pid)

    scip_file = tmp_path / "real.scip"
    scip_file.write_bytes(_build_go_scip_bytes())

    # 1. Helper unit paths: is_scip_analysis_version, _normalize_language aliases, scip_to_file_analyses edge cases
    assert si.is_scip_analysis_version("scip-v1:test") is True
    assert si.is_scip_analysis_version("tree-sitter") is False
    assert si._normalize_language("golang", Path("a.go")) == "go"
    assert si._normalize_language("c++", Path("a.cpp")) == "cpp"
    assert si._normalize_language("c#", Path("a.cs")) == "csharp"
    assert si._normalize_language("", Path("a.rb")) == "ruby"

    # Forward definition + external_symbols + escaping root path skipped
    fwd_sym = "scip-clang . . . core/Foo()."
    ext_sym = "scip-clang . . . std/println()."
    idx = ScipIndex(
        external_symbols=[ScipSymbolInfo(symbol=ext_sym, kind=17, display_name="println")],
        documents=[
            ScipDocument(
                relative_path="pkg/engine.go",
                language="golang",
                occurrences=[
                    ScipOccurrence(0, 0, 0, 3, fwd_sym, 0x40),  # ROLE_FORWARD_DEFINITION
                    ScipOccurrence(0, 0, 0, 3, fwd_sym, ROLE_READ_ACCESS),  # self-edge on decl line skipped
                    ScipOccurrence(1, 0, 1, 7, ext_sym, ROLE_IMPORT),
                ],
            )
        ],
    )
    analyses = si.scip_to_file_analyses(idx, repo)
    assert len(analyses) == 1
    only = next(iter(analyses.values()))
    assert only["language"] == "go"
    assert only["symbols"][0]["name"] == "Foo"
    assert only["external_imports"][0]["module"] == "std"

    # 2. index_project_scip: success, missing_toolchain, install_failed, ScipRunError, scip_languages filter
    fake_exe = tmp_path / "scip-go"
    fake_exe.write_bytes(b"bin")
    monkeypatch.setattr(si, "ensure_indexer", lambda cfg, iid, allow_download=True: InstalledIndexer(iid, "v0.2.10", "ready", "native", fake_exe))

    def fake_run_indexer(cfg, iid, root, *, installed=None, timeout=None):
        copy = tmp_path / "tmp_run" / "index.scip"
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_bytes(scip_file.read_bytes())
        return copy

    monkeypatch.setattr(si, "run_indexer", fake_run_indexer)
    res_ok = si.index_project_scip(store, settings, pid)
    assert res_ok["errors"] == []
    assert res_ok["indexers"][0]["status"] == "indexed"
    assert res_ok["indexers"][0]["files_updated"] == 3
    # Second run on unchanged SCIP reports files_unchanged == 3
    res_again = si.index_project_scip(store, settings, pid, languages=["go"])
    assert res_again["indexers"][0]["files_unchanged"] == 3

    # Settings.scip_languages filtering excludes 'go' when restricted to ['rust']
    settings.scip_languages = ["rust"]
    assert si.index_project_scip(store, settings, pid)["indexers"] == []
    assert si.enqueue_project_scip(store, settings, pid, repo)["queued"] is False
    settings.scip_languages = []

    # missing_toolchain path (not treated as fatal error)
    monkeypatch.setattr(si, "ensure_indexer", lambda cfg, iid, allow_download=True: InstalledIndexer(iid, "v0.2.10", "missing_toolchain", "native", missing_tools=["go"], detail="no go"))
    res_miss = si.index_project_scip(store, settings, pid)
    assert res_miss["errors"] == [] and res_miss["indexers"][0]["status"] == "missing_toolchain"

    # install_failed path
    monkeypatch.setattr(si, "ensure_indexer", lambda cfg, iid, allow_download=True: (_ for _ in ()).throw(RuntimeError("net down")))
    res_inst_err = si.index_project_scip(store, settings, pid)
    assert res_inst_err["indexers"][0]["status"] == "install_failed" and res_inst_err["errors"]

    # run_indexer failure path
    monkeypatch.setattr(si, "ensure_indexer", lambda cfg, iid, allow_download=True: InstalledIndexer(iid, "v0.2.10", "ready", "native", fake_exe))
    monkeypatch.setattr(si, "run_indexer", lambda *a, **k: (_ for _ in ()).throw(ScipRunError("compile failed")))
    res_run_err = si.index_project_scip(store, settings, pid)
    assert res_run_err["indexers"][0]["status"] == "failed" and "compile failed" in res_run_err["errors"][0]

    # Unregistered root refused
    with pytest.raises(ValueError, match="not registered"):
        si.index_project_scip(store, settings, pid, worktree=tmp_path)

    # 3. CLI scip install & scip index commands
    runner = CliRunner()
    monkeypatch.setattr("cairnmind.scip.scip_manager.ensure_indexer", lambda cfg, iid, allow_download=True: InstalledIndexer(iid, "1", "ready", "native", fake_exe))
    cli_inst = runner.invoke(cli_app, ["--data-dir", str(settings.data_dir), "scip", "install", "go", "rust"])
    assert cli_inst.exit_code == 0, cli_inst.output
    assert set(json.loads(cli_inst.output)["indexers"].keys()) == {"scip-go", "rust-analyzer"}

    cli_bad = runner.invoke(cli_app, ["--data-dir", str(settings.data_dir), "scip", "install", "brainfuck"])
    assert cli_bad.exit_code != 0

    monkeypatch.setattr(si, "run_indexer", fake_run_indexer)
    cli_idx = runner.invoke(cli_app, ["--data-dir", str(settings.data_dir), "scip", "index", "go-full", "--lang", "go"])
    assert cli_idx.exit_code == 0, cli_idx.output
    assert json.loads(cli_idx.output)["indexers"][0]["status"] == "indexed"
