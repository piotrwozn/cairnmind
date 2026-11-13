"""The `usage` command through the real CLI (it opens the store to resolve the project, so it needs the database)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.cli import app
from cairnmind.config import Settings
from cairnmind.store import Store


def event(kind, data, at):
    return json.dumps({"type": kind, "data": data, "created_at": at.isoformat(timespec="microseconds")}) + "\n"


def workspace(tmp_path):
    settings = Settings.load(tmp_path / "memory", create=True)
    root = tmp_path / "repo"
    root.mkdir()
    pid = Store(settings.data_dir).add_project("demo", root)["id"]
    runtime = settings.data_dir / "runtime"
    (runtime / "agents" / "s1").mkdir(parents=True)
    (runtime / "runs.json").write_text(json.dumps({"version": 1, "runs": [
        {"id": "r1", "session_id": "s1", "project_id": pid, "client": "api", "profile_id": "cheap", "model": "m1"}]}))
    at = datetime.now(timezone.utc) - timedelta(hours=1)
    (runtime / "agents" / "s1" / "events.jsonl").write_text(
        event("turn_started", {"run_id": "r1", "generation": 1}, at)
        + event("provider_started", {"call": 1}, at + timedelta(seconds=1))
        + event("usage", {"usage_complete": True, "input_tokens": 30, "output_tokens": 4, "estimated_cost": 0.5,
                          "cost_currency": "USD"}, at + timedelta(seconds=2))
        + event("turn_finished", {"status": "completed"}, at + timedelta(seconds=3)))
    return settings, pid


def run(settings, *args):
    return CliRunner().invoke(app, ["--data-dir", str(settings.data_dir), "usage", *args])


def test_the_command_prints_the_report_and_json(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVBRAIN_SESSION_ID", raising=False)
    settings, pid = workspace(tmp_path)
    shown = run(settings, "demo")
    assert shown.exit_code == 0, shown.output or repr(shown.exception)
    assert "Usage of the built-in API agent for demo, last 7d" in shown.output
    assert "cheap  m1  turns 1/1 complete  input 30  output 4  cost $0.5000 (estimate)" in shown.output
    assert "not an invoice" in shown.output and "PARTIAL" not in shown.output
    as_json = run(settings, "demo", "--since", "36h", "--json")
    payload = json.loads(as_json.output)
    assert payload["project"] == {"id": pid, "name": "demo"} and payload["since"] == "36h"
    assert payload["groups"][0]["input_tokens"] == 30 and payload["totals"]["estimated_cost"] == 0.5
    assert payload["partial"] is False and payload["data"]["registry"] == "ok"


def test_a_bad_since_fails_with_its_message(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVBRAIN_SESSION_ID", raising=False)
    settings, _ = workspace(tmp_path)
    bad = run(settings, "demo", "--since", "1w")
    assert bad.exit_code != 0 and "--since must be a number of days or hours" in bad.output


def test_an_unknown_project_is_reported_by_the_entrypoint_as_a_message(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("DEVBRAIN_SESSION_ID", raising=False)
    settings, _ = workspace(tmp_path)
    # Run it the way a user does: the entrypoint turns the store's error into a message and exit code 1.
    monkeypatch.setattr("sys.argv", ["cairnmind", "--data-dir", str(settings.data_dir), "usage", "no-such-project"])
    with pytest.raises(SystemExit) as stopped:
        cli.entrypoint()
    assert stopped.value.code == 1
    assert "Cairnmind: Unknown project: no-such-project" in capsys.readouterr().err
