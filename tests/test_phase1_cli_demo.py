"""Phase 1 (v2): above-the-fold README, 60-second demo SVG, human CLI mapping, NO_COLOR and clean --json stdout."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET

import pytest
from typer.testing import CliRunner

from cairnmind import cli, cli_human, first_start
from cairnmind.terminal_commands import ask_summary
from cairnmind.terminal_i18n import tr

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
SVG = ROOT / "docs" / "assets" / "demo-60s.svg"
SAMPLE = ROOT / "examples" / "sample-project"


def test_readme_above_the_fold_has_two_promises_demo_and_no_heavy_deps():
    text = README.read_text(encoding="utf-8")
    above_fold = text[:text.index("## Quick start")]
    assert "unknown" in above_fold
    assert "`done`" in above_fold and "blocked" in above_fold.lower()
    assert "docs/assets/demo-60s.svg" in above_fold
    assert "docs/CONTRACTS.md#architecture-taxonomy" in above_fold
    assert "examples/sample-project/README.md" in above_fold
    assert "Java 21" not in above_fold and "Docker" not in above_fold
    assert "## Optional integrations" in text
    optional = text[text.index("## Optional integrations"):]
    assert "Ollama" in optional and "tmux" in optional
    contracts = (ROOT / "docs" / "CONTRACTS.md").read_text(encoding="utf-8")
    assert "## Architecture taxonomy" in contracts


def test_demo_svg_is_valid_xml_up_to_date_and_covers_all_three_acts():
    assert SVG.is_file()
    content = SVG.read_text(encoding="utf-8")
    root = ET.fromstring(content)
    assert root.tag.endswith("svg")
    for marker in ("WITHOUT CAIRNMIND", "WITH CAIRNMIND", "0:00–0:20", "0:20–0:45", "0:45–1:00",
                   "prefers-reduced-motion: reduce"):
        assert marker in content
    check = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_demo_svg.py"), "--check"],
                           capture_output=True, text=True, timeout=15)
    assert check.returncode == 0, check.stderr


def test_sample_project_check_profile_is_portable_and_passes():
    config = tomllib.loads((SAMPLE / ".cairnmind" / "checks.toml").read_text(encoding="utf-8"))
    argv = config["checks"]["unit"]["argv"]
    assert argv[0] == "uv" and "python3" not in argv
    assert not any(part.startswith("/") for part in argv)
    run = subprocess.run([sys.executable, "-B", "checks/notes_check.py"], cwd=SAMPLE,
                         capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr


def test_human_error_classification_and_bilingual_messages():
    assert cli_human.classify_error(
        "Task completion requires fresh machine check evidence: no passed run"
    )["error_code"] == cli_human.FINGERPRINT_MISMATCH
    assert cli_human.classify_error(
        "Plan version conflict; read the current plan and retry"
    )["error_code"] == cli_human.STALE_PLAN_VERSION
    assert cli_human.classify_error("origin_evidence: degraded")["error_code"] == cli_human.ORIGIN_EVIDENCE_DEGRADED
    assert cli_human.classify_error(
        "The quoted passage does not occur in the checked version of the source."
    )["error_code"] == cli_human.NO_VERBATIM_EVIDENCE

    en_fp = cli_human.format_human_error("Task completion requires fresh machine check evidence: stale", cmd="uv run pytest")
    pl_fp = cli_human.format_human_error("Task completion requires fresh machine check evidence: stale",
                                         language="pl", cmd="uv run pytest")
    assert "Blocked:" in en_fp and "uv run pytest" in en_fp
    assert "Zablokowano: kod zmienił się po ostatnim uruchomieniu testów." in pl_fp and "uv run pytest" in pl_fp

    pl_plan = cli_human.format_human_error("Plan version conflict; read the current plan and retry", language="pl")
    assert pl_plan == first_start.MESSAGES[first_start.STALE_PLAN_VERSION]

    payload = cli_human.json_error_payload("Plan version conflict; read the current plan and retry", exit_code=1)
    assert payload == {"error": "Plan version conflict; read the current plan and retry",
                       "exit_code": 1, "error_code": "stale_plan_version"}


def test_color_enabled_respects_no_color_dumb_term_and_tty():
    tty = type("TTY", (), {"isatty": lambda self: True})()
    pipe = type("Pipe", (), {"isatty": lambda self: False})()
    assert cli_human.color_enabled(tty, {}) is True
    assert cli_human.color_enabled(tty, {"NO_COLOR": "1"}) is False
    assert cli_human.color_enabled(tty, {"TERM": "dumb"}) is False
    assert cli_human.color_enabled(pipe, {}) is False
    assert cli_human.strip_ansi("\x1b[31mBlocked\x1b[0m") == "Blocked"


def test_cli_stderr_maps_errors_in_tty_and_keeps_exit_codes_consistent(monkeypatch, capsys):
    def boom():
        raise ValueError("Task completion requires fresh machine check evidence: stale")

    monkeypatch.setattr(cli, "app", boom)
    monkeypatch.setattr(cli, "_stderr_is_terminal", lambda: True)
    monkeypatch.setattr("sys.argv", ["cairnmind", "task", "update", "demo", "t1", "done"])
    with pytest.raises(SystemExit) as human_exit:
        cli.entrypoint()
    human_err = capsys.readouterr().err
    assert human_exit.value.code == 1
    assert "Blocked: code changed after the last check run." in human_err

    monkeypatch.setattr("sys.argv", ["cairnmind", "task", "update", "demo", "t1", "done", "--json"])
    with pytest.raises(SystemExit) as json_exit:
        cli.entrypoint()
    json_out, json_err = capsys.readouterr()
    assert json_exit.value.code == human_exit.value.code == 1
    assert json_out == ""
    assert "Task completion requires fresh machine check evidence" in json_err


def test_ask_json_stays_pure_stdout_with_degraded_warning_on_stderr(monkeypatch, tmp_path):
    degraded_no_quote = {
        "status": "partial",
        "answer": "The model's answer could not be verified in the sources.",
        "sources": [],
        "unknowns": ["The answer has no verified citations."],
        "metadata": {"origin_evidence": "degraded"},
        "diagnostics": {"stage": "ask", "code": "missing_evidence"},
    }
    monkeypatch.setattr(cli, "service_alive", lambda settings: True)
    monkeypatch.setattr(cli, "request", lambda settings, method, path, **kw: degraded_no_quote)
    monkeypatch.setattr(cli, "_stdout_is_terminal", lambda: True)
    runner = CliRunner()
    res = runner.invoke(cli.app, ["--data-dir", str(tmp_path / "memory"), "ask", "demo", "flag?", "--json"])
    assert res.exit_code == 0, res.output
    parsed = json.loads(res.stdout)
    assert parsed["error_code"] == "no_verbatim_evidence"
    assert parsed["status"] == "partial"
    assert first_start.DEGRADED_ORIGIN_NOTE in res.stderr
    assert first_start.DEGRADED_ORIGIN_NOTE not in res.stdout

    # Human terminal view renders the concise unknown line and the degraded-origin warning in EN and PL.
    human_en = ask_summary(parsed, str)
    assert first_start.NO_VERBATIM_EVIDENCE in human_en
    assert first_start.DEGRADED_ORIGIN_NOTE in human_en
    human_pl = ask_summary(parsed, lambda text: tr(text, "pl"))
    assert first_start.MESSAGES[first_start.NO_VERBATIM_EVIDENCE] in human_pl
    assert first_start.MESSAGES[first_start.DEGRADED_ORIGIN_NOTE] in human_pl
