"""ArcadeDB integration tests for SCIP ingestion, code graph, preservation, CLI, and background worker."""
from __future__ import annotations

import json
from pathlib import Path
import time

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from cairnmind.cli import app as cli_app
from cairnmind.config import Settings
from cairnmind.ingest import index_project
from cairnmind.scip_ingest import apply_scip_index, enqueue_project_scip
from cairnmind.scip_proto import (
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

    monkeypatch.setattr("cairnmind.scip_ingest.index_project_scip", fake_index_project_scip)

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
