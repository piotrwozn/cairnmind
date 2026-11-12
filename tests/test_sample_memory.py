"""The committed sample memory stays readable by the commands its walkthrough names (issue #36).

No engine, model or service is used: these tests are what a reader with only Python does.
"""
from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.memory import inspect_snapshot
from cairnmind.memory_view import show_snapshot

REPOSITORY = Path(__file__).resolve().parents[1]
SAMPLE = REPOSITORY / "examples" / "sample-project"
PACKAGE = SAMPLE / ".memory" / "cairnmind"
NEUTRAL = "/tmp/cairnmind-sample/trail-notes"


def test_the_sample_verifies_with_the_loader_the_commands_use():
    summary = inspect_snapshot(PACKAGE)
    assert summary["status"] == "valid" and summary["project"]["name"] == "trail-notes"
    assert summary["counts"]["plans"] == 1 and summary["counts"]["check_runs"] == 1 and summary["counts"]["records"] == 7


def test_the_sample_shows_what_the_walkthrough_says_it_shows():
    decisions = show_snapshot(PACKAGE, kind="decision")
    assert decisions.count("[decision] confirmed") == 2 and decisions.count("[decision] superseded") == 1
    assert "  | Every note gets a random UUID as its identifier." in decisions
    assert "  | Notes are numbered in the order they are written." in decisions
    assert f"  sources: note://trail-notes/2026-03-02-storage, {NEUTRAL}/trail_notes/store.py" in decisions
    everything = show_snapshot(PACKAGE)
    assert "[constraint] confirmed" in everything and "Plans (1 shown of 1)" in everything
    assert "[plan] Find notes by tag" in everything and "active in the origin worktree" in everything
    for line in ("  1. [done] Store a list of tags with each note", "  2. [in_progress] List the notes that carry a given tag",
                 "  3. [todo] Describe tags in the README"):
        assert line in everything
    assert "historical check runs" in everything and "      unit: passed, ended " in everything
    assert "reported by an agent, not verified by this view: acceptance passed" in everything


def test_the_sample_names_no_machine_and_holds_only_what_its_manifest_lists():
    manifest = json.loads((PACKAGE / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["origin_root"] == NEUTRAL
    files = sorted(path.relative_to(PACKAGE).as_posix() for path in PACKAGE.rglob("*") if path.is_file())
    assert "manifest.json" in files and all(re.fullmatch(r"manifest\.json|objects/[0-9a-f]{64}\.json|manifests/[0-9a-f]{64}\.json", name)
                                            for name in files), files
    text = "".join(path.read_text(encoding="utf-8") for path in PACKAGE.rglob("*.json"))
    for forbidden in ("/home/", "/mnt/", "/Users/", "\\\\Users\\\\", "OneDrive"):
        assert forbidden not in text, forbidden
    # What the sample is there to show is in the package, also where the read-only view does not print it.
    assert '"criterion_checks"' in text and '"supersedes"' in text and "The unit check passes." in text


def test_the_code_the_sample_points_at_is_the_code_in_the_repository():
    code = (SAMPLE / "trail_notes" / "store.py").read_text(encoding="utf-8")
    objects = [json.loads(path.read_text(encoding="utf-8")) for path in (PACKAGE / "objects").glob("*.json")]
    stored = [row for row in _rows(objects) if row.get("uri") == f"{NEUTRAL}/trail_notes/store.py"]
    assert stored and stored[0]["content"] == code, "run scripts/build_sample_memory.py after changing the sample's code"


def _rows(objects):
    for item in objects:
        if isinstance(item, dict):
            yield item
            for value in item.values():
                if isinstance(value, dict):
                    yield value


def _commands():
    text = (SAMPLE / "README.md").read_text(encoding="utf-8")
    return [line.strip() for block in re.findall(r"```bash\n(.*?)```", text, re.S) for line in block.splitlines()
            if line.strip().startswith("cairnmind ")]


def test_every_command_of_the_walkthrough_runs_with_nothing_installed_but_python(tmp_path, monkeypatch):
    commands = _commands()
    assert len(commands) >= 4 and all(" memory show " in line or " memory inspect " in line for line in commands), commands
    missing = tmp_path / "nothing" / "here"
    for name in ("CAIRNMIND_SESSION_ID", "DEVBRAIN_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: pytest.fail("the walkthrough made an HTTP call"))
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: pytest.fail("the walkthrough started a process"))
    monkeypatch.chdir(REPOSITORY)
    before = {path: path.read_bytes() for path in PACKAGE.rglob("*") if path.is_file()}
    for line in commands:
        result = CliRunner().invoke(cli.app, ["--data-dir", str(missing), *shlex.split(line)[1:]])
        assert result.exit_code == 0, (line, result.output or repr(result.exception))
        assert "trail-notes" in result.output
    assert not missing.parent.exists()
    assert before == {path: path.read_bytes() for path in PACKAGE.rglob("*") if path.is_file()}
