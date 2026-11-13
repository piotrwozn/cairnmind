"""The `usage` command through the real CLI (it opens the store to resolve the project, so it needs the database)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re

import pytest
from typer.testing import CliRunner

from cairnmind import cli, ingest
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


def test_by_week_groups_by_iso_week_and_a_bad_value_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVBRAIN_SESSION_ID", raising=False)
    settings, _ = workspace(tmp_path)
    year, number, _ = (datetime.now(timezone.utc) - timedelta(hours=1)).isocalendar()
    expected = f"{year}-W{number:02d}"
    shown = run(settings, "demo", "--by", "week")
    assert shown.exit_code == 0, shown.output or repr(shown.exception)
    assert f"{expected}  api  cheap  m1  turns 1/1 complete" in shown.output and "ISO weeks" in shown.output
    payload = json.loads(run(settings, "demo", "--by", "week", "--json").output)
    assert payload["by"] == "week" and payload["groups"][0]["period"] == expected
    assert "day" not in payload["groups"][0]
    bad = run(settings, "demo", "--by", "month")
    assert bad.exit_code != 0 and "month" in re.sub(r"\x1b\[[0-9;]*m", "", bad.output)


def codex_rollout(tmp_path, root):
    """A rollout in the shape of test_transcript_compatibility.py, three reports of one thread in the last hours."""
    def counts(incoming, outgoing):
        return {"input_tokens": incoming, "cached_input_tokens": 0, "output_tokens": outgoing,
                "reasoning_output_tokens": 0, "total_tokens": incoming + outgoing}

    now = datetime.now(timezone.utc)
    events = [{"type": "session_meta", "payload": {"id": "native", "cwd": str(root)}}]
    for number, (own, total) in enumerate([((100, 10), (100, 10)), ((60, 6), (160, 16)), ((40, 4), (200, 20))], 1):
        events.append({"type": "token_usage_record", "timestamp": (now - timedelta(hours=4 - number)).isoformat(),
                       "payload": {"thread_id": "00000000-0000-4000-8000-000000000001", "turn_id": f"t{number}",
                                   "session_id": "00000000-0000-4000-8000-000000000002", "root_turn_id": "root",
                                   "response_id": f"r{number}", "usage": counts(*own),
                                   "turn_token_usage": counts(*own), "thread_token_usage": counts(*total)}})
    path = tmp_path / "rollout.jsonl"
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    return path


def test_codex_usage_comes_from_the_rollouts_that_import_follows(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVBRAIN_SESSION_ID", raising=False)
    settings, pid = workspace(tmp_path)
    rollout = codex_rollout(tmp_path, tmp_path / "repo")
    assert ingest.import_history(Store(settings.data_dir), rollout, pid, "codex")["errors"] == []
    shown = run(settings, "demo")
    assert shown.exit_code == 0, shown.output or repr(shown.exception)
    # The reports may fall on two UTC days, so the rows are not compared, the per-client totals are.
    assert "Total codex  turns 3/3 complete  input 200  output 20  cost unknown" in shown.output
    assert "Total api  turns 1/1 complete  input 30  output 4  cost $0.5000 (estimate)" in shown.output
    assert "  codex  unknown  unknown  turns " in shown.output
    assert "PARTIAL" not in shown.output
    payload = json.loads(run(settings, "demo", "--json").output)
    assert {g["client"] for g in payload["groups"]} == {"api", "codex"}
    assert payload["totals_by_client"]["codex"]["input_tokens"] == 200 and payload["partial"] is False
    assert payload["data"]["transcripts_read"] == 1 and payload["totals"]["estimated_cost"] is None
    # The original file is what is read: once it is gone the report says so and is partial.
    rollout.unlink()
    gone = json.loads(run(settings, "demo", "--json").output)
    assert gone["partial"] is True and gone["data"]["transcripts_missing"] == 1
    assert run(settings, "demo").output.splitlines()[3].startswith("PARTIAL REPORT: 1 Codex transcripts are gone")


def test_warn_over_adds_a_warning_and_never_changes_the_exit_code(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVBRAIN_SESSION_ID", raising=False)
    settings, _ = workspace(tmp_path)
    warned = run(settings, "demo", "--warn-over", "10")
    assert warned.exit_code == 0, warned.output or repr(warned.exception)
    assert warned.output.splitlines()[0].startswith("WARNING: 34 tokens (input plus output, all clients)")
    assert "over the warning threshold of 10; nothing is blocked." in warned.output.splitlines()[0]
    payload = json.loads(run(settings, "demo", "--warn-over", "10", "--json").output)
    assert payload["warning"]["observed_tokens"] == 34 and payload["warning"]["threshold_tokens"] == 10
    for quiet in (run(settings, "demo", "--warn-over", "34"), run(settings, "demo")):
        assert quiet.exit_code == 0 and "WARNING" not in quiet.output
    assert "warning" not in json.loads(run(settings, "demo", "--warn-over", "34", "--json").output)
    assert run(settings, "demo", "--warn-over", "0").exit_code != 0
    assert run(settings, "demo", "--warn-over", "many").exit_code != 0


def test_the_default_threshold_comes_from_the_checks_file_and_the_option_wins(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVBRAIN_SESSION_ID", raising=False)
    settings, pid = workspace(tmp_path)
    config = tmp_path / "repo" / ".cairnmind" / "checks.toml"
    config.parent.mkdir()
    config.write_text('[checks.unit]\nargv = ["true"]\n\n[usage]\nwarn_over_tokens = 20\n')
    from_file = run(settings, "demo")
    assert from_file.exit_code == 0 and from_file.output.startswith("WARNING: 34 tokens")
    assert "over the warning threshold of 20" in from_file.output.splitlines()[0]
    assert "WARNING" not in run(settings, "demo", "--warn-over", "1000").output
    over = json.loads(run(settings, "demo", "--warn-over", "5", "--json").output)
    assert over["warning"]["threshold_tokens"] == 5
    # The check profiles beside it are read as before.
    from cairnmind.verification import load_profiles
    assert list(load_profiles(Store(settings.data_dir), pid)) == ["unit"]


@pytest.mark.parametrize("text", ["[usage]\nwarn_over_tokens = 0\n", '[usage]\nwarn_over_tokens = "many"\n',
                                  "[usage]\nwarn_over_tokens = 5\nlimit = 1\n", "[usage]\nwarn_over_tokens = true\n",
                                  "usage = 3\n", "[usage\n"])
def test_a_bad_checks_file_setting_is_an_error_unless_the_option_is_given(tmp_path, monkeypatch, text):
    monkeypatch.delenv("DEVBRAIN_SESSION_ID", raising=False)
    settings, _ = workspace(tmp_path)
    config = tmp_path / "repo" / ".cairnmind" / "checks.toml"
    config.parent.mkdir()
    config.write_text(text)
    bad = run(settings, "demo")
    assert bad.exit_code != 0 and "WARNING" not in bad.output
    assert run(settings, "demo", "--warn-over", "1000").exit_code == 0


def test_a_bad_since_fails_with_its_message(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVBRAIN_SESSION_ID", raising=False)
    settings, _ = workspace(tmp_path)
    bad = run(settings, "demo", "--since", "1w")
    # Where a terminal is assumed, the error is drawn in a coloured box and wrapped: compare the words only.
    words = " ".join(re.sub(r"[^\w\s-]", " ", re.sub(r"\x1b\[[0-9;]*m", "", bad.output)).split())
    assert bad.exit_code != 0 and "--since must be a number of days or hours such as 7d or 36h" in words, bad.output


def test_an_unknown_project_is_reported_by_the_entrypoint_as_a_message(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("DEVBRAIN_SESSION_ID", raising=False)
    settings, _ = workspace(tmp_path)
    # Run it the way a user does: the entrypoint turns the store's error into a message and exit code 1.
    monkeypatch.setattr("sys.argv", ["cairnmind", "--data-dir", str(settings.data_dir), "usage", "no-such-project"])
    with pytest.raises(SystemExit) as stopped:
        cli.entrypoint()
    assert stopped.value.code == 1
    assert "Cairnmind: Unknown project: no-such-project" in capsys.readouterr().err
