from datetime import datetime, timedelta, timezone
import json

import pytest

from cairnmind import usage as usage_module
from cairnmind.agent_journal import AgentJournal
from cairnmind.config import Settings
from cairnmind.usage import build_usage, parse_since

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
PROJECT = {"id": "p1", "name": "My App"}


def stamp(days_ago=0.0, hours_ago=0.0):
    return (NOW - timedelta(days=days_ago, hours=hours_ago)).isoformat(timespec="microseconds")


def line(kind, data, at):
    return json.dumps({"type": kind, "data": data, "created_at": at}) + "\n"


def turn(run="r1", at=None, *usages):
    """One turn: its start, then one usage event per model call, each later than the one before."""
    at = at or stamp(1)
    base = datetime.fromisoformat(at)
    text = line("turn_started", {"run_id": run, "generation": 1}, at)
    for number, data in enumerate(usages, 1):
        text += line("usage", data, (base + timedelta(seconds=number)).isoformat(timespec="microseconds"))
    return text


def done(input_tokens, output_tokens, cost=None):
    data = {"usage_complete": True, "input_tokens": input_tokens, "output_tokens": output_tokens,
            "estimated_cost": cost}
    if cost is not None:
        data["cost_currency"] = "USD"
    return data


class Data:
    def __init__(self, root):
        self.root = root
        (root / "runtime" / "agents").mkdir(parents=True)
        self.runs = []

    def run(self, run_id, session, project="p1", client="api", profile="cheap", model="m1"):
        self.runs.append({"id": run_id, "session_id": session, "project_id": project, "client": client,
                          "profile_id": profile, "model": model})
        (self.root / "runtime" / "runs.json").write_text(json.dumps({"version": 1, "runs": self.runs}))

    def journal(self, session, text):
        folder = self.root / "runtime" / "agents" / session
        folder.mkdir(exist_ok=True)
        (folder / "events.jsonl").write_bytes(text if isinstance(text, bytes) else text.encode())

    def build(self, since="7d"):
        return build_usage(self.root, PROJECT, since, now=NOW)


@pytest.fixture
def data(tmp_path):
    return Data(tmp_path / "data")


def test_only_the_last_usage_event_of_a_turn_counts(data):
    data.run("r1", "s1")
    data.journal("s1", turn("r1", stamp(1), done(100, 50), done(300, 80)))
    result = data.build()
    assert [(g["input_tokens"], g["output_tokens"], g["turns"]) for g in result["groups"]] == [(300, 80, 1)]
    assert result["totals"]["input_tokens"] == 300


def test_a_new_turn_starts_a_new_total(data):
    data.run("r1", "s1")
    data.journal("s1", turn("r1", stamp(1, 2), done(300, 80)) + turn("r1", stamp(1), done(10, 5)))
    group = data.build()["groups"][0]
    assert (group["input_tokens"], group["output_tokens"], group["turns"], group["complete_turns"]) == (310, 85, 2, 2)


def test_incomplete_usage_contributes_what_was_seen_and_marks_the_group(data):
    data.run("r1", "s1")
    partial = {"usage_complete": False, "input_tokens": 70, "estimated_cost": 9.0, "cost_currency": "USD"}
    data.journal("s1", turn("r1", stamp(1, 3), done(100, 20, 0.5)) + turn("r1", stamp(1), partial))
    group = data.build()["groups"][0]
    assert (group["input_tokens"], group["output_tokens"]) == (170, 20)
    assert group["complete"] is False and group["complete_turns"] == 1 and group["turns"] == 2
    assert group["estimated_cost"] is None
    assert "incomplete" in data.build()["text"] and "cost unknown" in data.build()["text"]


def test_nothing_observed_stays_unknown_and_flag_alone_is_not_trusted(data):
    data.run("r1", "s1")
    data.journal("s1", turn("r1", stamp(1), {"usage_complete": True}))
    group = data.build()["groups"][0]
    assert group["input_tokens"] is None and group["output_tokens"] is None and group["complete"] is False
    assert "input unknown" in data.build()["text"]


def test_a_turn_without_any_usage_event_is_counted_as_incomplete(data):
    data.run("r1", "s1")
    data.journal("s1", turn("r1", stamp(1)))
    result = data.build()
    assert result["data"]["turns_seen"] == 1 and result["data"]["turns_complete"] == 0
    assert result["groups"][0]["input_tokens"] is None


def test_cost_needs_complete_usage_and_a_recorded_estimate_in_every_turn(data):
    data.run("r1", "s1", profile="priced")
    data.run("r2", "s2", profile="unpriced")
    data.journal("s1", turn("r1", stamp(1, 3), done(1000, 100, 0.25)) + turn("r1", stamp(1), done(10, 1, 0.125)))
    data.journal("s2", turn("r2", stamp(1), done(1000, 100, None)))
    groups = {g["profile"]: g for g in data.build()["groups"]}
    assert groups["priced"]["estimated_cost"] == 0.375 and groups["priced"]["cost_currency"] == "USD"
    assert groups["unpriced"]["estimated_cost"] is None
    # One group without rates makes the total unknown instead of a partial sum.
    assert data.build()["totals"]["estimated_cost"] is None


def test_cost_text_calls_itself_an_estimate_and_not_an_invoice_in_utc(data):
    data.run("r1", "s1")
    data.journal("s1", turn("r1", stamp(1), done(1, 1, 0.5)))
    result = data.build()
    assert "$0.5000 (estimate)" in result["text"]
    assert "not an invoice" in result["text"] and "UTC" in result["text"]
    assert result["timezone"] == "UTC"


def test_subscription_usage_is_never_turned_into_money(data):
    data.run("r1", "s1")
    copilot = {**done(5, 5), "copilot": {"premium_requests": 3, "quota_remaining": 12},
               "provider_observations": {"cost": 7.5, "credits": 2}}
    data.journal("s1", turn("r1", stamp(1), copilot))
    result = data.build()
    assert result["groups"][0]["estimated_cost"] is None
    dumped = json.dumps(result)
    assert "premium_requests" not in dumped and "quota_remaining" not in dumped and "7.5" not in dumped


def test_since_accepts_days_and_hours_and_rejects_the_rest():
    assert parse_since("7d") == timedelta(days=7) and parse_since("36h") == timedelta(hours=36)
    for bad in ("", "7", "d", "1w", "0d", "-1d", "1.5d", "7 d days", "7D", "9999d", "99999h"):
        with pytest.raises(ValueError, match="--since"):
            parse_since(bad)


def test_the_period_limits_turns_by_the_time_of_the_turn(data):
    data.run("r1", "s1")
    data.journal("s1", turn("r1", stamp(3), done(1, 1)) + turn("r1", stamp(0, 5), done(2, 2)))
    assert data.build("7d")["totals"]["turns"] == 2
    assert data.build("1d")["totals"]["input_tokens"] == 2
    assert data.build("4h")["totals"]["turns"] == 0


def test_groups_are_sorted_by_day_profile_and_model_with_utc_days(data):
    data.run("r1", "s1", profile="b", model="m2")
    data.run("r2", "s1", profile="a", model="m9")
    data.run("r3", "s1", profile="a", model="m1")
    late = datetime(2026, 11, 12, 23, 59, 59, 500000, tzinfo=timezone.utc).isoformat()
    data.journal("s1", turn("r1", stamp(2), done(1, 1)) + turn("r2", stamp(2, 1), done(2, 2))
                 + turn("r3", stamp(2, 2), done(3, 3)) + turn("r1", late, done(4, 4)))
    keys = [(g["day"], g["profile"], g["model"]) for g in data.build()["groups"]]
    # The usage event of the late turn is stamped one second after its start, which is already the next UTC day.
    assert keys == [("2026-11-11", "a", "m1"), ("2026-11-11", "a", "m9"), ("2026-11-11", "b", "m2"),
                    ("2026-11-13", "b", "m2")]


def test_only_sessions_of_the_project_are_counted_and_unknown_ones_are_reported(data):
    data.run("r1", "mine")
    data.run("r2", "theirs", project="other")
    data.run("r3", "pilot", client="copilot")
    data.journal("mine", turn("r1", stamp(1), done(10, 1)))
    data.journal("theirs", turn("r2", stamp(1), done(1000, 1000)))
    data.journal("pilot", turn("r3", stamp(1), done(500, 500)))
    data.journal("orphan", turn("rx", stamp(1), done(7, 7)))
    result = data.build()
    assert result["totals"]["input_tokens"] == 10
    assert result["data"]["sessions_skipped"] == 1 and result["data"]["sessions_not_api"] == 1
    assert "1 sessions skipped because their project is unknown" in result["text"]


def test_a_session_claimed_by_two_projects_is_not_counted(data):
    data.run("r1", "s1")
    data.run("r2", "s1", project="other")
    data.journal("s1", turn("r1", stamp(1), done(10, 1)))
    result = data.build()
    assert result["totals"]["turns"] == 0 and result["data"]["sessions_skipped"] == 1


def test_a_turn_of_an_unregistered_run_has_unknown_profile_and_model(data):
    data.run("r1", "s1")
    data.journal("s1", turn("gone", stamp(1), done(10, 1)))
    group = data.build()["groups"][0]
    assert (group["profile"], group["model"]) == ("unknown", "unknown")


def test_damaged_oversized_and_truncated_lines_are_skipped_and_counted(data, monkeypatch):
    monkeypatch.setattr(usage_module, "MAX_EVENT_BYTES", 400)
    data.run("r1", "s1")
    good = turn("r1", stamp(1), done(10, 1))
    huge = line("message", {"content": "x" * 2000}, stamp(1))
    text = ("not json\n" + "[1]\n" + '{"type": "usage"}\n' + huge + good
            + line("usage", {"input_tokens": "nine"}, "yesterday") + '{"type":"usage","data":{"input_tok')
    data.journal("s1", text.encode())
    result = data.build()
    assert result["data"]["lines_skipped"] == 6
    assert result["totals"]["input_tokens"] == 10 and result["totals"]["turns"] == 1
    assert "6 journal lines skipped" in result["text"]


def test_nothing_is_written_and_missing_directories_are_not_created(data, tmp_path):
    data.run("r1", "s1")
    data.journal("s1", turn("r1", stamp(1), done(10, 1, 0.1)))

    def snapshot(root):
        return sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in root.rglob("*"))

    before = snapshot(data.root)
    data.build()
    assert snapshot(data.root) == before
    empty = build_usage(tmp_path / "absent", PROJECT, "7d", now=NOW)
    assert empty["totals"]["turns"] == 0 and not (tmp_path / "absent").exists()
    assert "No turns of the built-in API agent" in empty["text"]


def test_json_has_stable_keys_and_text_is_printable(data):
    data.run("r1", "s1", profile="evil\x1b[31m name")
    data.journal("s1", turn("r1", stamp(1), done(10, 1)))
    result = data.build()
    assert list(result) == ["project", "since", "timezone", "from", "to", "groups", "totals", "data",
                            "cost_note", "text"]
    assert list(result["groups"][0]) == ["day", "profile", "model", "turns", "complete_turns", "complete",
                                         "input_tokens", "output_tokens", "estimated_cost", "cost_currency"]
    assert list(result["data"]) == ["turns_seen", "turns_complete", "lines_skipped", "sessions_skipped",
                                    "sessions_not_api", "sessions_read"]
    json.dumps(result)
    assert "\x1b" not in result["text"] and " " not in result["text"]


def test_long_listings_are_cut_in_the_text_but_complete_in_json(data):
    for number in range(70):
        data.run(f"p{number}", "s1", profile=f"profile{number:02d}")
    text = "".join(turn(f"p{number}", stamp(1), done(1, 1)) for number in range(70))
    data.journal("s1", text)
    result = data.build()
    assert len(result["groups"]) == 70
    assert "10 more rows are in the JSON output." in result["text"]
    assert len(result["text"].splitlines()) < 70


def test_events_written_by_the_real_journal_are_read(tmp_path):
    settings = Settings.load(tmp_path / "data", create=True)
    journal = AgentJournal(settings, "real")
    journal.append("turn_started", {"run_id": "r1", "generation": 1})
    journal.append("usage", {"usage_complete": True, "input_tokens": 4, "output_tokens": 2})
    journal.append("usage", {"usage_complete": True, "input_tokens": 9, "output_tokens": 3,
                             "estimated_cost": 0.5, "cost_currency": "USD"})
    (settings.data_dir / "runtime" / "runs.json").write_text(json.dumps({"version": 1, "runs": [
        {"id": "r1", "session_id": "real", "project_id": "p1", "client": "api", "profile_id": "cheap",
         "model": "m1"}]}))
    result = build_usage(settings.data_dir, PROJECT, "1d", now=datetime.now(timezone.utc) + timedelta(minutes=1))
    group = result["groups"][0]
    assert (group["input_tokens"], group["output_tokens"], group["estimated_cost"]) == (9, 3, 0.5)
    assert (group["profile"], group["model"]) == ("cheap", "m1")
