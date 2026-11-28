"""Unit tests for the #248 pull-request contract declaration check (scripts/check_contract_pr.py)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "check_contract_pr.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_contract_pr", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_non_contract_changes_pass_without_label_or_section():
    mod = _load_module()
    passed, matched, errors = mod.evaluate_pr(
        ["README.md", "src/cairnmind/cli.py", "tests/test_workspace_cli.py"],
        pr_body="",
        labels=[],
    )
    assert passed is True
    assert matched == []
    assert errors == []


def test_contract_fixture_and_doc_paths_require_both_section_and_label():
    mod = _load_module()
    template_only_body = (
        "## What and why\nSome change.\n\n"
        "## Contracts\n\n"
        "<!-- Does this change the stable contract in docs/STABLE_CONTRACT.md? -->\n\n"
        "## Not verified\nNothing.\n"
    )

    for changed in (
        ["docs/STABLE_CONTRACT.md"],
        ["tests/fixtures/contracts/cli_json/status.json"],
        [r"tests\fixtures\contracts\mcp_tools\ask.json"],
        ["./docs/STABLE_CONTRACT.md"],
    ):
        # Missing both section text and label
        passed, matched, errors = mod.evaluate_pr(changed, template_only_body, [])
        assert passed is False
        assert len(matched) == 1
        assert len(errors) == 2

        # Label present, but section still empty / comment-only
        passed_lbl, _, errors_lbl = mod.evaluate_pr(changed, template_only_body, ["contract-change"])
        assert passed_lbl is False
        assert len(errors_lbl) == 1
        assert "## Contracts" in errors_lbl[0]

        # Section filled, but label missing
        filled_body = (
            "## What and why\nAdd field.\n\n"
            "## Contracts\n"
            "<!-- comment -->\n"
            "Additive field `foo` documented in `docs/STABLE_CONTRACT.md`.\n\n"
            "## Not verified\nNone.\n"
        )
        passed_sec, _, errors_sec = mod.evaluate_pr(changed, filled_body, ["enhancement"])
        assert passed_sec is False
        assert len(errors_sec) == 1
        assert "contract-change" in errors_sec[0]

        # Both present -> pass (including CRLF and case-insensitive label/heading)
        crlf_body = filled_body.replace("\n", "\r\n").replace("## Contracts", "## contracts")
        passed_ok, matched_ok, errors_ok = mod.evaluate_pr(changed, crlf_body, ["Contract-Change"])
        assert passed_ok is True
        assert len(matched_ok) == 1
        assert errors_ok == []


def test_cli_reads_github_event_json_and_changed_files(tmp_path):
    mod = _load_module()
    changed = tmp_path / "changed.txt"
    changed.write_text("docs/STABLE_CONTRACT.md\nREADME.md\n", encoding="utf-8")
    event = tmp_path / "event.json"

    event.write_text(
        json.dumps({
            "pull_request": {
                "body": "## Contracts\n<!-- template -->\n",
                "labels": [],
            }
        }),
        encoding="utf-8",
    )
    assert mod.main(["--changed-files-file", str(changed), "--event-path", str(event)]) == 1

    event.write_text(
        json.dumps({
            "pull_request": {
                "body": "## Contracts\nPublished initial 1.0.0 stable contract document (#248).\n",
                "labels": [{"name": "contract-change"}],
            }
        }),
        encoding="utf-8",
    )
    assert mod.main(["--changed-files-file", str(changed), "--event-path", str(event)]) == 0
