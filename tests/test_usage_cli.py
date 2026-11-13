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


def codex_rollout(path, root):
    """A rollout in the shape of test_transcript_compatibility.py: three responses of one thread, the last hours.

    ``usage`` is the response, ``thread_token_usage`` the running sum. Without a ``root`` there is no cwd.
    """
    def counts(incoming, outgoing):
        return {"input_tokens": incoming, "cached_input_tokens": 0, "output_tokens": outgoing,
                "reasoning_output_tokens": 0, "total_tokens": incoming + outgoing}

    now = datetime.now(timezone.utc)
    events = [{"type": "session_meta", "payload": {"id": "native", "cwd": str(root)}}] if root else []
    for number, (own, total) in enumerate([((100, 10), (100, 10)), ((60, 6), (160, 16)), ((40, 4), (200, 20))], 1):
        events.append({"type": "token_usage_record", "timestamp": (now - timedelta(hours=4 - number)).isoformat(),
                       "payload": {"thread_id": "00000000-0000-4000-8000-000000000001", "turn_id": f"t{number}",
                                   "session_id": "00000000-0000-4000-8000-000000000002", "root_turn_id": "root",
                                   "response_id": f"r{number}", "usage": counts(*own),
                                   "turn_token_usage": counts(*own), "thread_token_usage": counts(*total)}})
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    return path


def codex_totals(settings, name, *args):
    payload = json.loads(run(settings, name, "--json", *args).output)
    return payload, payload["totals_by_client"].get("codex", {}).get("input_tokens")


def test_codex_usage_comes_from_what_import_stored_for_the_project(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVBRAIN_SESSION_ID", raising=False)
    monkeypatch.setattr("cairnmind.codex_usage.PAGE", 2)  # three records: the second page is read too
    settings, pid = workspace(tmp_path)
    rollout = codex_rollout(tmp_path / "rollout.jsonl", tmp_path / "repo")
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
    assert payload["data"]["codex_records"] == 3 and payload["totals"]["estimated_cost"] is None
    assert codex_totals(settings, "demo", "--since", "2h")[1] == 40  # only the last response is that recent
    # The stored lines are what is read, not the file: a moved or deleted rollout changes nothing.
    rollout.unlink()
    gone, codex = codex_totals(settings, "demo")
    assert codex == 200 and gone["partial"] is False


def test_codex_usage_follows_the_project_import_attributed_each_event_to(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVBRAIN_SESSION_ID", raising=False)
    settings, pid = workspace(tmp_path)
    store = Store(settings.data_dir)
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = store.add_project("other", other_root)["id"]
    mine = codex_rollout(tmp_path / "mine.jsonl", tmp_path / "repo")
    foreign = codex_rollout(tmp_path / "foreign.jsonl", other_root)
    # `cairnmind import DIR --project demo` saves a cursor for demo on every rollout, but attributes each event
    # by its own working directory and parks the foreign ones in the inbox.
    for path in (mine, foreign):
        ingest.import_history(store, path, pid, "codex")
    payload, codex = codex_totals(settings, "demo")
    assert codex == 200 and payload["partial"] is False and payload["data"]["codex_waiting"] == 0
    # The other project has nothing yet, and its records waiting in the inbox make its report partial.
    waiting, codex = codex_totals(settings, "other")
    assert codex is None and waiting["partial"] is True and waiting["data"]["codex_waiting"] == 3
    assert "PARTIAL REPORT: 3 Codex usage records are waiting in the ingestion inbox" in run(settings, "other").output
    # Once import assigns them, they are other's usage and no longer waiting.
    ingest.import_history(store, foreign, other, "codex")
    assigned, codex = codex_totals(settings, "other")
    assert codex == 200 and assigned["partial"] is False and assigned["data"]["codex_waiting"] == 0
    assert codex_totals(settings, "demo")[1] == 200


def test_codex_records_of_no_known_directory_wait_in_the_inbox_for_every_project(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVBRAIN_SESSION_ID", raising=False)
    settings, _ = workspace(tmp_path)
    ingest.import_history(Store(settings.data_dir), codex_rollout(tmp_path / "loose.jsonl", None), None, "codex")
    payload, codex = codex_totals(settings, "demo")
    assert codex is None and payload["partial"] is True and payload["data"]["codex_waiting"] == 3


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
def test_an_unreadable_default_threshold_never_stops_the_report(tmp_path, monkeypatch, text):
    monkeypatch.delenv("DEVBRAIN_SESSION_ID", raising=False)
    settings, _ = workspace(tmp_path)
    config = tmp_path / "repo" / ".cairnmind" / "checks.toml"
    config.parent.mkdir()
    config.write_text(text)
    shown = run(settings, "demo")
    assert shown.exit_code == 0, shown.output or repr(shown.exception)
    assert shown.output.startswith("Warning threshold not read: ") and "WARNING" not in shown.output
    assert "Usage of the built-in API agent for demo" in shown.output
    payload = json.loads(run(settings, "demo", "--json").output)
    assert payload["warning_unavailable"] and "warning" not in payload and payload["groups"][0]["input_tokens"] == 30
    # With the option nothing is read from the file, so there is nothing to report about it.
    given = run(settings, "demo", "--warn-over", "1000")
    assert given.exit_code == 0 and "Warning threshold not read" not in given.output


def test_other_problems_of_the_checks_file_do_not_stop_the_report_either(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVBRAIN_SESSION_ID", raising=False)
    settings, _ = workspace(tmp_path)
    root = tmp_path / "repo"
    (root / ".cairnmind").mkdir()
    config = root / ".cairnmind" / "checks.toml"
    # A broken profile is not the threshold's business: it is still read.
    config.write_text('[checks.unit]\nargv = 3\n\n[usage]\nwarn_over_tokens = 20\n')
    assert run(settings, "demo").output.startswith("WARNING: 34 tokens")
    # Two declarations of the file make the threshold unreadable, not the report.
    (root / ".devbrain").mkdir()
    (root / ".devbrain" / "checks.toml").write_text('[usage]\nwarn_over_tokens = 20\n')
    both = run(settings, "demo")
    assert both.exit_code == 0 and both.output.startswith("Warning threshold not read: Check profiles are declared in both")
    assert json.loads(run(settings, "demo", "--json").output)["warning_unavailable"].endswith("keep one of them")


def test_load_profiles_still_raises_the_errors_it_raised_before(tmp_path, monkeypatch):
    from cairnmind.verification import load_profiles
    monkeypatch.delenv("DEVBRAIN_SESSION_ID", raising=False)
    settings, pid = workspace(tmp_path)
    store, root = Store(settings.data_dir), tmp_path / "repo"
    assert load_profiles(store, pid) == {}
    (root / ".cairnmind").mkdir()
    (root / ".cairnmind" / "checks.toml").write_text("[checks.unit\n")
    with pytest.raises(ValueError, match="^Cannot read check profiles$") as parse_error:
        load_profiles(store, pid)
    assert type(parse_error.value.__cause__).__name__ == "TOMLDecodeError"
    (root / ".cairnmind" / "checks.toml").write_text('[checks.unit]\nargv = ["true"]\n')
    (root / ".devbrain").mkdir()
    (root / ".devbrain" / "checks.toml").write_text('[checks.unit]\nargv = ["true"]\n')
    with pytest.raises(ValueError, match="^Check profiles are declared in both .cairnmind/checks.toml and "
                                         ".devbrain/checks.toml; keep one of them$"):
        load_profiles(store, pid)
    (root / ".devbrain" / "checks.toml").unlink()
    (root / ".cairnmind" / "checks.toml").write_text("checks = 3\n")
    with pytest.raises(ValueError, match="^checks must contain named profiles$"):
        load_profiles(store, pid)


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
