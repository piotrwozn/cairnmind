"""Fast-tier edge-case tests for cli.py commands and error branches."""
from __future__ import annotations

import json
from types import SimpleNamespace

from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.scip.scip_manager import ScipError

runner = CliRunner()


def _init_data_dir(tmp_path):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "config.json").write_text(json.dumps({"data_dir": str(data)}), encoding="utf-8")
    return data


def test_cli_autostart_benchmark_forget_and_export_markdown(tmp_path, monkeypatch):
    data = _init_data_dir(tmp_path)

    # autostart status / install / remove (ok + ValueError refusal)
    monkeypatch.setattr("cairnmind.pal.service_autostart.status", lambda s: {"installed": False})
    res = runner.invoke(cli.app, ["--data-dir", str(data), "service", "autostart", "status"])
    assert res.exit_code == 0 and '"installed": false' in res.stdout

    monkeypatch.setattr("cairnmind.pal.service_autostart.install", lambda s: {"installed": True})
    assert runner.invoke(cli.app, ["--data-dir", str(data), "service", "autostart", "install"]).exit_code == 0
    monkeypatch.setattr("cairnmind.pal.service_autostart.install", lambda s: (_ for _ in ()).throw(ValueError("not windows")))
    assert runner.invoke(cli.app, ["--data-dir", str(data), "service", "autostart", "install"]).exit_code != 0

    monkeypatch.setattr("cairnmind.pal.service_autostart.remove", lambda s: {"removed": True})
    assert runner.invoke(cli.app, ["--data-dir", str(data), "service", "autostart", "remove"]).exit_code == 0
    monkeypatch.setattr("cairnmind.pal.service_autostart.remove", lambda s: (_ for _ in ()).throw(ValueError("not windows")))
    assert runner.invoke(cli.app, ["--data-dir", str(data), "service", "autostart", "remove"]).exit_code != 0

    # forget is explicitly refused on CLI
    res_forget = runner.invoke(cli.app, ["--data-dir", str(data), "forget", "proj-a", "--delete-project"])
    assert res_forget.exit_code != 0

    # benchmark with mocked request & nvidia-smi fallback
    monkeypatch.setattr(cli, "request", lambda *a, **k: {"status": "answered", "cached": False, "sources": ["s1"]})
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no nvidia-smi")))
    res_bench = runner.invoke(cli.app, ["--data-dir", str(data), "benchmark", "proj-a", "Why?"])
    assert res_bench.exit_code == 0 and "nvidia-smi unavailable" in res_bench.stdout

    # export --markdown with mocked store
    fake_store = SimpleNamespace(
        resolve_project=lambda p: {"id": "p1"},
        export=lambda pid: {
            "records": [{"id": "r1", "kind": "fact", "key": "k1", "content": "Fact text"}],
            "sources": [{"id": "s1", "uri": "file://a.py", "content": "print(1)"}],
        },
    )
    monkeypatch.setattr(cli, "store_for", lambda *a, **k: fake_store)
    md_out = tmp_path / "export.md"
    res_md = runner.invoke(cli.app, ["--data-dir", str(data), "export", str(md_out), "--project", "p1", "--markdown"])
    assert res_md.exit_code == 0
    assert "# Cairnmind — memory export" in md_out.read_text(encoding="utf-8")


def test_cli_scip_install_and_index_error_branches(tmp_path, monkeypatch):
    data = _init_data_dir(tmp_path)

    # Unknown SCIP target refused
    res_bad = runner.invoke(cli.app, ["--data-dir", str(data), "scip", "install", "brainfuck"])
    assert res_bad.exit_code != 0

    # Install all indexers with one ScipError -> exit 1
    def fake_ensure(settings, indexer_id, allow_download=True):
        if indexer_id == "scip-go":
            raise ScipError("download blocked")
        return SimpleNamespace(to_dict=lambda: {"indexer_id": indexer_id, "status": "installed"})

    monkeypatch.setattr("cairnmind.scip.scip_manager.ensure_indexer", fake_ensure)
    res_all = runner.invoke(cli.app, ["--data-dir", str(data), "scip", "install"])
    assert res_all.exit_code == 1 and "install_failed" in res_all.stdout

    # scip index with errors -> exit 1
    monkeypatch.setattr(cli, "store_for", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr("cairnmind.scip.scip_ingest.index_project_scip", lambda *a, **k: {"errors": ["boom"]})
    res_idx = runner.invoke(cli.app, ["--data-dir", str(data), "scip", "index", "proj-a", "--no-download"])
    assert res_idx.exit_code == 1


def test_cli_eval_answers_sessions_and_memory_edges(tmp_path, monkeypatch):
    data = _init_data_dir(tmp_path)
    monkeypatch.setattr(cli, "_not_run_without_models", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_managed_only", lambda *a, **k: None)
    monkeypatch.setattr(cli, "store_for_read", lambda *a, **k: SimpleNamespace(resolve_project=lambda p: {"id": "p1"}))

    # eval extraction, repository, injection, replay error & exit-code paths
    monkeypatch.setattr("cairnmind.agents.extraction_evaluation.run_extraction_evaluation", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad file")))
    assert runner.invoke(cli.app, ["--data-dir", str(data), "eval", "extraction"]).exit_code != 0
    monkeypatch.setattr("cairnmind.agents.extraction_evaluation.run_extraction_evaluation", lambda *a, **k: {"status": "failed", "runs": [{"run": 1, "summary": {}, "failures": []}]})
    assert runner.invoke(cli.app, ["--data-dir", str(data), "eval", "extraction"]).exit_code == 1

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    monkeypatch.setattr("cairnmind.agents.repository_evaluation.run_repository_evaluation", lambda *a, **k: {"status": "not_run"})
    assert runner.invoke(cli.app, ["--data-dir", str(data), "eval", "repository", str(repo_dir)]).exit_code == 3

    monkeypatch.setattr("cairnmind.agents.injection_evaluation.run_injection_evaluation", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad fixture")))
    assert runner.invoke(cli.app, ["--data-dir", str(data), "eval", "injection"]).exit_code != 0

    monkeypatch.setattr("cairnmind.agents.answer_log.replay", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad limit")))
    assert runner.invoke(cli.app, ["--data-dir", str(data), "eval", "replay", "p1"]).exit_code != 0

    # answers list ValueError
    monkeypatch.setattr("cairnmind.agents.answer_log.listing", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad limit")))
    assert runner.invoke(cli.app, ["--data-dir", str(data), "answers", "list", "p1"]).exit_code != 0

    # sessions prepare (not ready -> exit 2) and sessions ack (foreign session -> BadParameter)
    monkeypatch.setattr(cli, "request", lambda *a, **k: {"status": "partial"})
    assert runner.invoke(cli.app, ["--data-dir", str(data), "sessions", "prepare", "p1", "codex"]).exit_code == 2
    monkeypatch.setenv("CAIRNMIND_SESSION_ID", "other-sess")
    assert runner.invoke(cli.app, ["--data-dir", str(data), "sessions", "ack", "sess-1", "pkt", "hash", "{}"]).exit_code != 0

    # memory inspect inside session -> routes through request()
    monkeypatch.setattr(cli, "request", lambda *a, **k: {"snapshot_id": "snap-1"})
    snap = tmp_path / "snap"
    snap.mkdir()
    res_insp = runner.invoke(cli.app, ["--data-dir", str(data), "memory", "inspect", str(snap)])
    assert res_insp.exit_code == 0 and "snap-1" in res_insp.stdout

    # memory reextract inside session refused
    assert runner.invoke(cli.app, ["--data-dir", str(data), "memory", "reextract", "p1"]).exit_code != 0
    monkeypatch.delenv("CAIRNMIND_SESSION_ID", raising=False)

    # memory status with --worktree & memory import with --replace-confirmed --name
    monkeypatch.setattr("cairnmind.memory.memory.inspect_snapshot", lambda p: {"ok": True})
    assert runner.invoke(cli.app, ["--data-dir", str(data), "memory", "status", "p1", "--worktree", str(repo_dir)]).exit_code == 0
    assert runner.invoke(
        cli.app,
        ["--data-dir", str(data), "memory", "import", str(snap), "--root", str(repo_dir), "--name", "N", "--replace-confirmed"],
    ).exit_code == 0
