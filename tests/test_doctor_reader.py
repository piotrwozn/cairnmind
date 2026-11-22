"""`cairnmind doctor` on an installation that only reads the store another one writes (#373).

A reader is how a second installation is meant to look (docs/DEPLOYMENT.md, One writer, any number of readers): it is
not a missing requirement. The exit code, the summary and the JSON say the same thing.
"""
from __future__ import annotations

import json
from pathlib import Path
import socket

import pytest
from typer.testing import CliRunner

from cairnmind import cli, doctor_attention, first_start, integrations
from cairnmind.config import Settings
from cairnmind.store import Store

runner = CliRunner()


def quiet(data_dir: Path) -> None:
    """Ports nothing listens on, so doctor asks nobody on this machine (the service, the model server)."""
    ports = []
    for _ in range(2):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            ports.append(probe.getsockname()[1])
    config = data_dir / "config.json"
    saved = json.loads(config.read_text()) if config.exists() else {}
    config.write_text(json.dumps({**saved, "port": ports[0], "ollama_url": f"http://127.0.0.1:{ports[1]}"}))
    config.chmod(0o600)


@pytest.fixture
def two(tmp_path, monkeypatch):
    for name in ("CAIRNMIND_SESSION_ID", "DEVBRAIN_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(integrations, "_probe_cli", lambda name: {
        "installed": False, "version": None, "verified_resume": False, "checks": {}, "notes": []})
    writer = Store(tmp_path / "writer")
    reader = tmp_path / "reader"
    reader.mkdir(mode=0o700)
    (reader / "config.json").write_text(json.dumps({"arcade_database": writer.settings.arcade_database}))
    Settings.load(reader, create=True)
    assert Store(reader).read_only
    for directory in (writer.data_dir, reader):
        quiet(directory)
    return writer, reader


def doctor(data_dir: Path, *arguments: str):
    return runner.invoke(cli.app, ["--data-dir", str(data_dir), "doctor", *arguments])


def test_a_healthy_reader_exits_0_and_says_it_only_reads_and_who_writes(two):
    writer, reader = two
    result = doctor(reader, "--json")
    report = json.loads(result.stdout)
    assert report["store_writer"]["role"] == "reader" and report["environment_blockers"] == []
    assert doctor_attention.required_missing(report) == []
    assert result.exit_code == 0, result.output
    lines = doctor_attention.summary(report)
    assert first_start.NOTHING_MISSING in lines and lines[-1] != first_start.NEXT_FIX
    said = [line for line in lines if line.startswith("  - Store: this installation only reads it")]
    assert len(said) == 1 and writer.installation.id[:8] in said[0] and "cairnmind store takeover --yes" in said[0]
    assert not any(line.startswith("  - Local service") for line in lines)  # no service runs on a reader, by design
    assert doctor(writer.data_dir, "--json").exit_code == 0


def test_a_reader_with_something_missing_exits_1_and_names_it(two, monkeypatch):
    _, reader = two
    monkeypatch.setenv("CAIRNMIND_SESSION_ID", "one")
    monkeypatch.setenv("DEVBRAIN_SESSION_ID", "two")
    result = doctor(reader, "--json")
    report = json.loads(result.stdout)
    assert result.exit_code == 1 and report["environment_blockers"]
    missing = doctor_attention.required_missing(report)
    assert missing and not any(line.startswith("Store writer") for line in missing)
    assert all(blocker in " ".join(missing) for blocker in report["environment_blockers"])


@pytest.mark.parametrize("report", [
    {"store_writer": {"role": "lost", "reason": "Installation abc took over writing."}},
    {"store_writer": {"role": "unknown", "reason": "The store cannot be read."}},
    {"store_writer": {"role": "reader", "problem": "The writer row and the journal head differ."}}])
def test_a_lost_unknown_or_mixed_role_is_still_missing(report):
    assert doctor_attention.required_missing({**report, "arcadedb": {"available": True}})
