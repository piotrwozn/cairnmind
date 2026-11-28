"""Verify that the product website stays synced with canonical repository docs and excluded from Python packages (#251)."""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEBSITE = ROOT / "website"


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def test_website_excluded_from_python_package():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    hatch_build = data.get("tool", {}).get("hatch", {}).get("build", {})
    excludes = hatch_build.get("exclude", [])
    assert "website" in excludes, "website must be excluded from [tool.hatch.build] (sdist & wheel)"

    wheel_packages = hatch_build.get("targets", {}).get("wheel", {}).get("packages", [])
    assert wheel_packages, "wheel packages list must be explicit"
    assert not any("website" in pkg for pkg in wheel_packages)


def test_website_install_commands_match_repo_docs():
    install_data = json.loads((WEBSITE / "src" / "data" / "install.json").read_text(encoding="utf-8"))
    readme_text = (ROOT / "README.md").read_text(encoding="utf-8")
    install_doc = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
    combined_docs = readme_text + "\n" + install_doc

    commands: list[str] = []
    commands.extend(install_data["quickTry"]["commands"])
    commands.extend(install_data["firstProjectFlow"])
    for platform in install_data["platforms"]:
        commands.extend(platform["prereqCommands"])
        commands.extend(platform["installCommands"])
        commands.extend(platform["manualCommands"])

    assert len(commands) >= 15
    missing = [cmd for cmd in commands if cmd not in combined_docs]
    assert missing == [], f"Website install commands drifted from README.md / docs/INSTALL.md: {missing}"


def test_website_benchmarks_trace_to_evaluation_docs():
    bench = json.loads((WEBSITE / "src" / "data" / "benchmarks.json").read_text(encoding="utf-8"))
    eval_doc = (ROOT / "docs" / "EVALUATION.md").read_text(encoding="utf-8")
    results_doc = (ROOT / "docs" / "evaluation" / "RESULTS.md").read_text(encoding="utf-8")
    combined = eval_doc + "\n" + results_doc

    for token in (
        "qwen3.5:9b",
        "qwen3-embedding:0.6b",
        "ArcadeDB 26.11.1",
        "RTX 4080 Super",
        "29%",
        "20 of 21",
        "24",
    ):
        assert token in combined, f"Expected evaluation token {token!r} in docs/EVALUATION.md or RESULTS.md"

    for row in bench["releaseGateTable"]:
        base_cmd = " ".join(row["command"].split()[:3])  # e.g. 'cairnmind eval run'
        assert base_cmd in eval_doc, f"Benchmark command {base_cmd!r} not documented in docs/EVALUATION.md"

    assert _normalize_ws(bench["environment"]["chatModel"]).startswith("qwen3.5:9b")


def test_website_contract_fixtures_exist():
    contracts_ts = (WEBSITE / "src" / "lib" / "contracts.ts").read_text(encoding="utf-8")
    referenced = re.findall(r"readFixture\(['\"]([^'\"]+)['\"]\)", contracts_ts)
    assert len(referenced) >= 5
    fixtures_root = ROOT / "tests" / "fixtures" / "contracts"
    for rel in referenced:
        target = fixtures_root / rel
        assert target.is_file(), f"Missing frozen contract fixture referenced by website: {rel}"
        parsed = json.loads(target.read_text(encoding="utf-8"))
        assert isinstance(parsed, dict)


def test_website_seo_and_ai_crawler_files_present():
    robots = (WEBSITE / "public" / "robots.txt").read_text(encoding="utf-8")
    for token in (
        "GPTBot",
        "OAI-SearchBot",
        "ClaudeBot",
        "PerplexityBot",
        "Google-Extended",
        "sitemap-index.xml",
    ):
        assert token in robots, f"robots.txt missing {token}"

    llms = (WEBSITE / "public" / "llms.txt").read_text(encoding="utf-8")
    assert "https://github.com/piotrwozn/cairnmind" in llms
    assert "https://cairnmind.dev/docs/" in llms

    llms_full_src = (WEBSITE / "src" / "pages" / "llms-full.txt.ts").read_text(encoding="utf-8")
    assert "uvx cairnmind try" in llms_full_src
    assert "REPO_META" in llms_full_src


def _normalize_try_line(line: str) -> str:
    line = re.sub(r"fingerprint: [0-9a-f]{12}\.\.\.", "fingerprint: <sha256>...", line)
    line = re.sub(r"pinned check_run=[0-9a-f]{8}", "pinned check_run=<run-id>", line)
    return line


def test_website_try_output_matches_live_cli():
    from cairnmind.cli_cmds.try_demo import run_try_sandbox

    website_data = json.loads((WEBSITE / "src" / "data" / "try-output.json").read_text(encoding="utf-8"))
    live_lines: list[str] = []
    result = run_try_sandbox(interactive=False, echo=live_lines.append)
    assert result["completed"] is True

    while live_lines and not live_lines[0].strip():
        live_lines.pop(0)
    while live_lines and not live_lines[-1].strip():
        live_lines.pop()

    expected = [_normalize_try_line(line) for line in website_data["lines"]]
    actual = [_normalize_try_line(line) for line in live_lines]
    assert actual == expected, "website/src/data/try-output.json drifted from live `cairnmind try` output"
