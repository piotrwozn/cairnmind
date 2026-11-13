from datetime import datetime, timedelta, timezone
import json
import os
from types import SimpleNamespace

import pytest

from cairnmind import usage as usage_module
from cairnmind.agent_journal import AgentJournal
from cairnmind.api_agent import AgentEngine
from cairnmind.config import Settings
from cairnmind.usage import build_usage, parse_since

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
PROJECT = {"id": "p1", "name": "My App"}


def stamp(days_ago=0.0, hours_ago=0.0):
    return (NOW - timedelta(days=days_ago, hours=hours_ago)).isoformat(timespec="microseconds")


def line(kind, data, at):
    return json.dumps({"type": kind, "data": data, "created_at": at}) + "\n"


def turn(run="r1", at=None, *usages, calls=None, end=True):
    """One turn as the engine writes it: a provider_started before each model call, a usage event after it."""
    base = datetime.fromisoformat(at or stamp(1))
    moments = iter(base + timedelta(seconds=n) for n in range(100))
    text = line("turn_started", {"run_id": run, "generation": 1}, base.isoformat(timespec="microseconds"))
    for number in range(max(len(usages), calls or 0)):
        text += line("provider_started", {"request_id": f"q{number}", "call": number + 1}, next(moments).isoformat())
        if number < len(usages):
            text += line("usage", usages[number], next(moments).isoformat())
    return text + (line("turn_finished", {"status": "completed"}, next(moments).isoformat()) if end else "")


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
        self.registry(json.dumps({"version": 1, "runs": self.runs}))

    def registry(self, text):
        (self.root / "runtime" / "runs.json").write_text(text)

    def journal(self, session, text, mtime=NOW):
        folder = self.root / "runtime" / "agents" / session
        folder.mkdir(exist_ok=True)
        path = folder / "events.jsonl"
        path.write_bytes(text if isinstance(text, bytes) else text.encode())
        os.utime(path, (mtime.timestamp(), mtime.timestamp()))

    def build(self, since="7d", now=NOW, **options):
        return build_usage(self.root, PROJECT, since, now=now, **options)


@pytest.fixture
def data(tmp_path):
    return Data(tmp_path / "data")


def test_only_the_last_usage_event_of_a_turn_counts(data):
    data.run("r1", "s1")
    data.journal("s1", turn("r1", stamp(1), done(100, 50), done(300, 80)))
    result = data.build()
    assert [(g["input_tokens"], g["output_tokens"], g["turns"], g["complete"]) for g in result["groups"]] == [
        (300, 80, 1, True)]
    assert result["totals"]["input_tokens"] == 300 and result["totals"]["tokens_are_lower_bound"] is False


def test_a_new_turn_starts_a_new_total(data):
    data.run("r1", "s1")
    data.journal("s1", turn("r1", stamp(1, 2), done(300, 80)) + turn("r1", stamp(1), done(10, 5)))
    group = data.build()["groups"][0]
    assert (group["input_tokens"], group["output_tokens"], group["turns"], group["complete_turns"]) == (310, 85, 2, 2)


def test_incomplete_usage_is_a_lower_bound_and_the_group_is_marked(data):
    data.run("r1", "s1")
    partial = {"usage_complete": False, "input_tokens": 70, "estimated_cost": 9.0, "cost_currency": "USD"}
    data.journal("s1", turn("r1", stamp(1, 3), done(100, 20, 0.5)) + turn("r1", stamp(1), partial))
    result = data.build()
    group = result["groups"][0]
    assert (group["input_tokens"], group["output_tokens"]) == (170, 20)
    assert group["complete"] is False and group["tokens_are_lower_bound"] is True
    assert group["complete_turns"] == 1 and group["turns"] == 2 and group["estimated_cost"] is None
    assert "incomplete  input at least 170  output at least 20  cost unknown" in result["text"]


def test_a_counter_nobody_reported_stays_unknown_and_the_flag_alone_is_not_trusted(data):
    data.run("r1", "s1")
    data.journal("s1", turn("r1", stamp(1), {"usage_complete": True}))
    group = data.build()["groups"][0]
    assert group["input_tokens"] is None and group["output_tokens"] is None and group["complete"] is False
    assert "input unknown  output unknown" in data.build()["text"]


def test_a_turn_without_any_usage_event_is_counted_as_incomplete(data):
    data.run("r1", "s1")
    data.journal("s1", turn("r1", stamp(1), calls=1))
    result = data.build()
    assert result["data"]["turns_seen"] == 1 and result["data"]["turns_complete"] == 0
    assert result["groups"][0]["input_tokens"] is None


def test_a_started_call_without_a_report_makes_the_turn_incomplete_and_unpriced(data):
    data.run("r1", "s1")
    data.journal("s1", turn("r1", stamp(1), done(1000, 100, 0.25), calls=2, end=False))
    group = data.build()["groups"][0]
    assert (group["input_tokens"], group["complete"], group["estimated_cost"]) == (1000, False, None)
    assert "input at least 1000" in data.build()["text"]
    data.journal("s1", turn("r1", stamp(1), done(1000, 100, 0.25), calls=1))
    assert data.build()["groups"][0]["estimated_cost"] == 0.25


def test_a_skipped_line_inside_a_turn_or_after_its_last_report_makes_it_incomplete(data):
    data.run("r1", "s1")
    start = line("turn_started", {"run_id": "r1"}, stamp(1))
    call = line("provider_started", {"call": 1}, stamp(1))
    report = line("usage", done(5, 5, 0.1), stamp(1))
    data.journal("s1", start + "{broken\n" + call + report)
    assert data.build()["groups"][0]["complete"] is False
    data.journal("s1", start + call + report + '{"type":"usage","data":{"inp')
    assert data.build()["groups"][0]["complete"] is False
    data.journal("s1", start + call + report)
    assert data.build()["groups"][0]["complete"] is True


def test_usage_after_a_lost_turn_start_is_its_own_turn_not_an_overwrite(data):
    data.run("r1", "s1")
    data.journal("s1", turn("r1", stamp(2), done(1000, 200)) + "{lost turn_started\n"
                 + line("usage", done(50, 10, 0.5), stamp(1)))
    result = data.build()
    seen = {(g["day"], g["profile"]): g["input_tokens"] for g in result["groups"]}
    assert seen == {("2026-11-11", "cheap"): 1000, ("2026-11-12", "unknown"): 50}
    assert result["data"]["turns_seen"] == 2 and result["data"]["lines_skipped"] == 1
    # Its started calls cannot be counted, so the turn without a start line is never complete or priced.
    orphan = next(g for g in result["groups"] if g["profile"] == "unknown")
    assert (orphan["complete"], orphan["estimated_cost"]) == (False, None)


def test_usage_before_any_turn_start_counts_under_unknown_profile_and_model(data):
    data.run("r1", "s1")
    data.journal("s1", line("usage", done(8, 2, 0.5), stamp(1)))
    group = data.build()["groups"][0]
    assert (group["profile"], group["model"], group["input_tokens"]) == ("unknown", "unknown", 8)
    assert (group["complete"], group["estimated_cost"]) == (False, None)


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


def test_small_costs_are_not_shown_or_stored_as_zero(data):
    data.run("r1", "s1")
    data.journal("s1", turn("r1", stamp(1), done(1, 1, 0.000042)))
    result = data.build()
    assert "<$0.0001 (estimate)" in result["text"] and "$0.0000" not in result["text"]
    assert result["totals"]["estimated_cost"] == 0.000042
    data.journal("s1", turn("r1", stamp(1), done(1, 1, 3e-7)))
    assert data.build()["totals"]["estimated_cost"] == 3e-7


@pytest.mark.parametrize("cost", [True, "0.5", -5.0, float("nan"), float("inf"), 1e10])
def test_a_cost_that_is_not_a_plausible_amount_is_unknown(data, cost):
    data.run("r1", "s1")
    data.journal("s1", turn("r1", stamp(1), {**done(10, 1), "estimated_cost": cost, "cost_currency": "USD"}))
    group = data.build()["groups"][0]
    assert (group["estimated_cost"], group["complete"], group["input_tokens"]) == (None, True, 10)


@pytest.mark.parametrize("extra", [{"cost_currency": "EUR"}, {}, {"cost_currency": None}])
def test_a_cost_needs_the_dollar_currency(data, extra):
    data.run("r1", "s1")
    data.journal("s1", turn("r1", stamp(1), {**done(10, 1), "estimated_cost": 0.5, **extra}))
    assert data.build()["groups"][0]["estimated_cost"] is None
    data.journal("s1", turn("r1", stamp(1), {**done(10, 1), "estimated_cost": 0.5, "cost_currency": "USD"}))
    assert data.build()["groups"][0]["estimated_cost"] == 0.5


@pytest.mark.parametrize("usage, expected", [
    ({"usage_complete": True, "input_tokens": True, "output_tokens": 1}, (None, 1, False)),
    ({"usage_complete": True, "input_tokens": 12.5, "output_tokens": 1}, (None, 1, False)),
    ({"usage_complete": True, "input_tokens": -1, "output_tokens": 1}, (None, 1, False)),
    ({"usage_complete": True, "input_tokens": 10**12 + 1, "output_tokens": 1}, (None, 1, False)),
    ({"usage_complete": True, "input_tokens": "5", "output_tokens": 1}, (None, 1, False)),
    ({"usage_complete": True, "input_tokens": 10**12, "output_tokens": 0}, (10**12, 0, True)),
    ({"usage_complete": 1, "input_tokens": 10, "output_tokens": 1}, (10, 1, False)),
    ({"usage_complete": "true", "input_tokens": 10, "output_tokens": 1}, (10, 1, False)),
    ({"input_tokens": 10, "output_tokens": 1}, (10, 1, False)),
    ({"usage_complete": True, "input_tokens": 10, "output_tokens": 1}, (10, 1, True)),
])
def test_every_value_rule_of_a_turn_summary(data, usage, expected):
    data.run("r1", "s1")
    data.journal("s1", turn("r1", stamp(1), usage))
    group = data.build()["groups"][0]
    assert (group["input_tokens"], group["output_tokens"], group["complete"]) == expected


def test_subscription_usage_is_never_turned_into_money(data):
    data.run("r1", "s1")
    copilot = {**done(5, 5), "copilot": {"premium_requests": 3, "quota_remaining": 12},
               "provider_observations": {"cost": 7.5, "credits": 2}}
    data.journal("s1", turn("r1", stamp(1), copilot))
    result = data.build()
    assert result["groups"][0]["estimated_cost"] is None and result["groups"][0]["input_tokens"] == 5
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
    # The usage event of the late turn is stamped after its start, which is already the next UTC day.
    assert keys == [("2026-11-11", "a", "m1"), ("2026-11-11", "a", "m9"), ("2026-11-11", "b", "m2"),
                    ("2026-11-13", "b", "m2")]


def test_a_timestamp_with_an_offset_is_grouped_by_its_utc_day(data):
    data.run("r1", "s1")
    data.journal("s1", turn("r1", "2026-11-12T00:30:00+02:00", done(1, 1)))
    assert data.build()["groups"][0]["day"] == "2026-11-11"


def test_an_old_stray_journal_does_not_make_the_report_partial_but_a_recent_one_does(data):
    data.run("r1", "s1")
    data.journal("s1", turn("r1", stamp(1), done(1, 1)))
    data.journal("stray", turn("rx", stamp(1), done(1, 1)), mtime=NOW - timedelta(days=30))
    data.journal("pilot", turn("rp", stamp(1), done(1, 1)), mtime=NOW - timedelta(days=30))
    data.runs.append({"id": "rp", "session_id": "pilot", "project_id": "p1", "client": "copilot"})
    data.registry(json.dumps({"version": 1, "runs": data.runs}))
    old = data.build()
    assert old["partial"] is False and old["data"]["sessions_skipped"] == 0 and old["data"]["sessions_not_api"] == 0
    data.journal("stray", turn("rx", stamp(1), done(1, 1)))
    data.journal("pilot", turn("rp", stamp(1), done(1, 1)))
    recent = data.build()
    assert recent["partial"] is True and recent["data"]["sessions_skipped"] == 1
    assert recent["data"]["sessions_not_api"] == 1


def test_a_journal_untouched_since_before_the_period_is_not_opened(data):
    data.run("r1", "s1")
    old = NOW - timedelta(days=30)
    data.journal("s1", turn("r1", stamp(1), done(1, 1)), mtime=old)
    skipped = data.build()
    assert skipped["data"]["sessions_read"] == 0 and skipped["totals"]["turns"] == 0
    data.journal("s1", turn("r1", stamp(20), done(1, 1)))
    opened = data.build()
    assert opened["data"]["sessions_read"] == 1 and opened["totals"]["turns"] == 0


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


def test_skipped_sessions_make_the_whole_report_partial_and_say_so_before_the_rows(data):
    data.run("r1", "s1")
    data.journal("s1", turn("r1", stamp(1), done(10, 1, 0.5)))
    assert data.build()["partial"] is False and data.build()["totals"]["estimated_cost"] == 0.5
    data.journal("orphan", turn("rx", stamp(1), done(7, 7)))
    result = data.build()
    assert result["partial"] is True and result["groups"][0]["estimated_cost"] == 0.5
    totals = result["totals"]
    assert (totals["complete"], totals["estimated_cost"], totals["tokens_are_lower_bound"]) == (False, None, True)
    text = result["text"]
    assert "PARTIAL REPORT" in text and text.index("PARTIAL REPORT") < text.index("cheap")
    assert "Total  turns 1/1 incomplete  input at least 10" in text


@pytest.mark.parametrize("content, state", [(None, "missing"), ("{not json", "unreadable"),
                                            ('{"version": 1, "runs": 5}', "unreadable"), ("[]", "unreadable"),
                                            ('{"version": 1, "runs": []}', "ok")])
def test_the_state_of_the_run_registry_is_reported_and_poisons_the_totals(data, content, state):
    if content is not None:
        data.registry(content)
    data.journal("s1", turn("r1", stamp(1), done(10, 1, 0.5)))
    result = data.build()
    assert result["data"]["registry"] == state
    assert result["partial"] is True
    assert (result["totals"]["complete"], result["totals"]["estimated_cost"]) == (False, None)
    assert ("PARTIAL REPORT: the run registry is " + state in result["text"]) == (state != "ok")


def test_an_oversized_registry_is_unreadable(data, monkeypatch):
    data.run("r1", "s1")
    data.journal("s1", turn("r1", stamp(1), done(10, 1)))
    assert data.build()["data"]["registry"] == "ok"
    monkeypatch.setattr(usage_module, "MAX_REGISTRY_BYTES", 10)
    assert data.build()["data"]["registry"] == "unreadable"


def test_without_counted_turns_there_is_no_total_row_and_no_zero_cost(data):
    data.run("r1", "s1")
    result = data.build()
    assert "No turns of the built-in API agent" in result["text"] and "Total" not in result["text"]
    assert "$0.0000" not in result["text"]
    assert (result["totals"]["complete"], result["totals"]["estimated_cost"], result["partial"]) == (False, None, False)


def test_malformed_values_never_crash_the_command(data):
    data.registry(json.dumps({"version": 1, "runs": [
        {"id": ["x"], "session_id": "s1", "project_id": "p1", "client": "api"},
        {"id": {}, "session_id": "s2", "project_id": {}, "client": "api"},
        {"id": "r1", "session_id": "s3", "project_id": "p1", "client": "api", "profile_id": ["p"], "model": 5},
        {"id": "r9", "session_id": 7, "project_id": "p1"}, "text", None,
        {"id": "ok", "session_id": "s1", "project_id": "p1", "client": "api", "profile_id": "pp", "model": "mm"}]}))
    data.journal("s1", turn("ok", stamp(1), done(1, 1)))
    data.journal("s2", turn("r1", stamp(1), done(1, 1)))
    data.journal("s3", line("turn_started", {"run_id": ["unhashable"]}, stamp(1))
                 + line("turn_started", {"run_id": {"a": 1}}, stamp(1))
                 + line("usage", done(3, 3), "0001-01-01T00:00:00+10:00")
                 + line("usage", done(3, 3), "9999-12-31T23:59:59-10:00")
                 + turn("r1", stamp(1), done(2, 2)))
    result = data.build()
    assert result["data"]["lines_skipped"] == 2 and result["data"]["sessions_skipped"] == 1
    assert sorted((g["profile"], g["model"]) for g in result["groups"]) == [("pp", "mm"), ("unknown", "unknown")]


def test_damaged_oversized_and_truncated_lines_are_skipped_and_counted(data, monkeypatch):
    monkeypatch.setattr(usage_module, "MAX_EVENT_BYTES", 400)
    data.run("r1", "s1")
    huge = line("message", {"content": "x" * 2000}, stamp(1))
    text = ("not json\n" + "[1]\n" + '{"type": "usage"}\n' + huge + turn("r1", stamp(1), done(10, 1))
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


def test_a_naive_now_is_an_error_and_an_aware_one_is_shown_in_utc(data):
    with pytest.raises(ValueError, match="timezone-aware"):
        data.build(now=datetime(2026, 11, 13, 12, 0))
    result = data.build(now=NOW.astimezone(timezone(timedelta(hours=1))))
    assert result["from"] == "2026-11-06T12:00:00+00:00" and result["to"].endswith("+00:00")
    assert "from 2026-11-06T12:00:00+00:00" in result["text"]


def test_json_has_stable_keys_and_text_and_json_are_printable(data):
    data.run("r1", "s1", profile="evil\x1b[31m name", model="m\u009b‮" + "x" * 500)
    data.journal("s1", turn("r1", stamp(1), done(10, 1)))
    result = build_usage(data.root, {"id": "p1", "name": "bad\u009bname "}, "7d", now=NOW)
    assert list(result) == ["project", "since", "timezone", "from", "to", "partial", "groups", "totals",
                            "totals_by_client", "data", "cost_note", "by", "text"]
    assert list(result["groups"][0]) == ["period", "day", "client", "profile", "model", "turns", "complete_turns",
                                         "complete", "tokens_are_lower_bound", "input_tokens", "output_tokens",
                                         "estimated_cost", "cost_currency"]
    assert list(result["data"]) == ["turns_seen", "turns_complete", "lines_skipped", "sessions_skipped",
                                    "sessions_not_api", "sessions_read", "registry", "codex_covered",
                                    "transcripts_read", "transcripts_missing", "transcripts_skipped",
                                    "transcript_lines_skipped"]
    dumped = json.dumps(result, ensure_ascii=False)
    for bad in ("\x1b", " ", "\u009b", "‮"):
        assert bad not in dumped and bad not in result["text"]
    assert len(result["groups"][0]["model"]) <= 60


def test_long_listings_keep_the_newest_rows_in_the_text_and_all_in_json(data):
    data.run("r1", "s1")
    data.journal("s1", "".join(turn("r1", stamp(days), done(1, 1)) for days in range(1, 71)))
    result = data.build("100d")
    assert len(result["groups"]) == 70
    assert "10 older rows are only in the JSON output." in result["text"]
    assert "2026-11-12" in result["text"] and "2026-09-04" not in result["text"]
    assert result["groups"][0]["day"] == "2026-09-04"


def test_turns_built_with_the_real_engine_usage_method(tmp_path):
    settings = Settings.load(tmp_path / "data", create=True)
    pricing = {"input_per_million": 1.0, "output_per_million": 2.0}

    def call(journal, engine, number, reply):
        journal.append("provider_started", {"request_id": f"q{number}", "call": number, "provider": "api"})
        if reply:
            AgentEngine._usage(engine, reply)
            journal.append("provider_completed", {"request_id": f"q{number}"})

    for session, replies in (("whole", [{"input_tokens": 1000, "output_tokens": 100},
                                        {"input_tokens": 1500, "output_tokens": 50}]),
                             ("lost", [{"input_tokens": 10, "output_tokens": 10}, None])):
        journal = AgentJournal(settings, session)
        engine = SimpleNamespace(_turn_usage={}, profile={"pricing": pricing}, _record=journal.append)
        journal.append("turn_started", {"run_id": "r-" + session, "generation": 1})
        for number, reply in enumerate(replies, 1):
            call(journal, engine, number, reply)
        journal.append("turn_finished", {"status": "completed"})
    (settings.data_dir / "runtime" / "runs.json").write_text(json.dumps({"version": 1, "runs": [
        {"id": "r-" + name, "session_id": name, "project_id": "p1", "client": "api", "profile_id": name,
         "model": "m1"} for name in ("whole", "lost")]}))
    result = build_usage(settings.data_dir, PROJECT, "1d", now=datetime.now(timezone.utc) + timedelta(minutes=1))
    groups = {g["profile"]: g for g in result["groups"]}
    # The engine records running totals, so the two calls of the first turn count once: 2500 and 150.
    assert (groups["whole"]["input_tokens"], groups["whole"]["output_tokens"]) == (2500, 150)
    assert groups["whole"]["complete"] is True and groups["whole"]["estimated_cost"] == 0.0028
    assert (groups["lost"]["input_tokens"], groups["lost"]["complete"], groups["lost"]["estimated_cost"]) == (
        10, False, None)
    assert result["data"]["lines_skipped"] == 0 and result["data"]["registry"] == "ok"


def test_weeks_are_iso_weeks_in_utc_and_follow_the_year_boundary(data):
    data.run("r1", "s1")
    year_end = datetime(2025, 1, 5, 12, 0, tzinfo=timezone.utc)
    # Sunday 29 Dec 2024 ends 2024-W52; Monday 30 Dec and Wednesday 1 Jan are both in 2025-W01.
    data.journal("s1", turn("r1", "2024-12-29T23:30:00+00:00", done(1, 1))
                 + turn("r1", "2024-12-30T00:30:00+00:00", done(2, 2))
                 + turn("r1", "2025-01-01T10:00:00+00:00", done(4, 4)))
    result = data.build("30d", now=year_end, by="week")
    assert [(g["period"], g["input_tokens"], g["turns"]) for g in result["groups"]] == [
        ("2024-W52", 1, 1), ("2025-W01", 6, 2)]
    assert all("day" not in g for g in result["groups"]) and result["by"] == "week"
    assert "weeks are ISO weeks (Monday to Sunday) in UTC" in result["text"] and "days are UTC" not in result["text"]
    assert "2025-W01  api  cheap  m1  turns 2/2 complete  input 6" in result["text"]
    assert result["totals"]["input_tokens"] == 7 and result["totals"]["turns"] == 3


def test_a_week_is_numbered_by_the_year_of_its_thursday(data):
    data.run("r1", "s1")
    new_year = datetime(2027, 1, 3, tzinfo=timezone.utc)
    # 31 Dec 2026 is a Thursday: week 53 of 2026, which also holds Friday 1 Jan 2027.
    data.journal("s1", turn("r1", "2026-12-31T10:00:00+00:00", done(1, 1))
                 + turn("r1", "2027-01-01T10:00:00+00:00", done(1, 1)), mtime=new_year)
    result = data.build("30d", now=new_year, by="week")
    assert [(g["period"], g["turns"]) for g in result["groups"]] == [("2026-W53", 2)]


def test_days_stay_the_default_and_keep_their_day_key(data):
    data.run("r1", "s1")
    data.journal("s1", turn("r1", stamp(1), done(1, 1)))
    for result in (data.build(), data.build(by="day")):
        assert result["by"] == "day" and result["groups"][0]["day"] == result["groups"][0]["period"] == "2026-11-12"
        assert "days are UTC" in result["text"]
    with pytest.raises(ValueError, match="by must be day or week"):
        data.build(by="month")


def test_the_row_cap_and_the_lower_bound_rule_hold_for_weeks(data):
    for number in (1, 2, 3):
        data.run(f"r{number}", "s1", model=f"m{number}")
    # 25 weeks of three models are 75 rows; one more, older week of the first model lacks an output counter.
    text = "".join(turn(f"r{number}", stamp(7 * week + 1), done(1, 1)) for week in range(25) for number in (1, 2, 3))
    data.journal("s1", text + turn("r1", stamp(7 * 25 + 1), {"usage_complete": False, "input_tokens": 5}))
    result = data.build("200d", by="week")
    assert len(result["groups"]) == 76 and result["totals"]["turns"] == 76
    assert "16 older rows are only in the JSON output." in result["text"]
    shown = [row for row in result["text"].splitlines() if "  turns " in row and not row.startswith("Total")]
    assert len(shown) == 60 and shown[-1].startswith(result["groups"][-1]["period"])
    assert result["groups"][0]["tokens_are_lower_bound"] is True
    assert (result["groups"][0]["input_tokens"], result["groups"][0]["output_tokens"]) == (5, None)
    assert "Total  turns 75/76 incomplete  input at least 80  output at least 75" in result["text"]


# Codex: record shapes follow technical() in test_transcript_compatibility.py (the Codex 0.166 rollout).
def _counts(incoming, outgoing, **extra):
    return {"input_tokens": incoming, "cached_input_tokens": 0, "output_tokens": outgoing,
            "reasoning_output_tokens": 0, "total_tokens": incoming + outgoing, **extra}


def record(number, total, own, at, thread="00000000-0000-4000-8000-000000000001", **extra):
    """A token_usage_record: ``own`` is the usage of this response, ``total`` the running total of the thread."""
    payload = {"thread_id": thread, "turn_id": f"turn-{number}", "session_id": "00000000-0000-4000-8000-000000000002",
               "root_turn_id": "root-turn", "response_id": f"resp-{number}", "usage": _counts(*own, **extra),
               "turn_token_usage": _counts(*own), "thread_token_usage": _counts(*total, **extra)}
    return json.dumps({"type": "token_usage_record", "timestamp": at, "payload": payload}) + "\n"


def rollout(data, name, *lines, owner="p1", client="codex", mtime=NOW):
    """Write a rollout file and return the import cursor that names it."""
    folder = data.root.parent / "codex"
    folder.mkdir(exist_ok=True)
    path = folder / f"{name}.jsonl"
    meta = json.dumps({"type": "session_meta", "payload": {"id": name, "cwd": "/work/app"}}) + "\n"
    path.write_text(meta + "".join(lines))
    os.utime(path, (mtime.timestamp(), mtime.timestamp()))
    if not (data.root / "runtime" / "runs.json").exists():
        data.registry(json.dumps({"version": 1, "runs": data.runs}))  # a readable registry, as in real use
    return {"path": str(path), "project_id": owner, "client": client}


def three_reports():
    return (record(1, (100, 10), (100, 10), stamp(5)), record(2, (160, 16), (60, 6), stamp(1.4)),
            record(3, (200, 20), (40, 4), stamp(1.4, -1)))


def codex_rows(result):
    return [g for g in result["groups"] if g["client"] == "codex"]


def test_codex_running_totals_count_once_and_only_the_growth_inside_the_period(data):
    cursor = rollout(data, "a", *three_reports())
    # The same thread copied to a second file (an archived rollout) must not count twice.
    twin = rollout(data, "b", *three_reports())
    before = sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in data.root.parent.rglob("*"))
    result = data.build("2d", transcripts=[cursor, twin])
    assert sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size)
                  for p in data.root.parent.rglob("*")) == before
    (row,) = codex_rows(result)
    # 200 - 100 = 100: the report before the period is only the baseline; adding the totals would give 360.
    assert (row["period"], row["turns"], row["input_tokens"], row["output_tokens"]) == ("2026-11-12", 2, 100, 10)
    assert row["complete"] is True and result["partial"] is False
    assert (result["data"]["transcripts_read"], result["data"]["codex_covered"]) == (2, True)
    assert "2026-11-12  codex  unknown  unknown  turns 2/2 complete  input 100  output 10  cost unknown" in result["text"]
    assert "Claude Code is not covered" in result["text"]
    assert data.build("2d")["data"]["codex_covered"] is False and codex_rows(data.build("2d")) == []


def test_codex_thread_totals_are_kept_apart_and_a_period_without_reports_is_empty(data):
    other = "00000000-0000-4000-8000-0000000000aa"
    cursor = rollout(data, "a", record(1, (100, 10), (100, 10), stamp(1)),
                     record(2, (7, 1), (7, 1), stamp(1), thread=other),
                     record(3, (150, 12), (50, 2), stamp(0, 3)), record(4, (9, 2), (2, 1), stamp(0, 2), thread=other))
    result = data.build("1d", transcripts=[cursor])
    assert [(g["period"], g["input_tokens"], g["output_tokens"]) for g in codex_rows(result)] == [
        ("2026-11-12", 107, 11), ("2026-11-13", 52, 3)]
    assert result["totals"]["turns"] == 4
    assert codex_rows(data.build("1h", transcripts=[cursor])) == []


def test_codex_never_has_a_cost_and_a_price_in_the_records_is_ignored(data):
    data.run("r1", "s1")
    data.journal("s1", turn("r1", stamp(1), done(10, 1, 0.5)))
    cursor = rollout(data, "a", *three_reports(), record(4, (210, 21), (10, 1), stamp(0, 1),
                                                        estimated_cost=3.0, cost_currency="USD"))
    result = data.build("2d", transcripts=[cursor])
    assert {g["client"]: g["estimated_cost"] for g in result["groups"]} == {"api": 0.5, "codex": None}
    # The estimate of the API agent stays visible in its own total; the mixed total cannot be a cost.
    assert result["totals_by_client"]["api"]["estimated_cost"] == 0.5
    assert result["totals_by_client"]["codex"]["estimated_cost"] is None
    assert result["totals"]["estimated_cost"] is None and result["totals"]["cost_currency"] is None
    assert "Total api  turns 1/1 complete  input 10  output 1  cost $0.5000 (estimate)" in result["text"]
    assert "Total codex  turns 3/3 complete  input 110  output 11  cost unknown" in result["text"]
    assert "$3" not in result["text"] and "subscription client" in result["text"]


def test_a_codex_session_of_another_project_is_left_alone_and_one_without_a_project_is_skipped(data):
    mine = rollout(data, "mine", *three_reports())
    foreign = rollout(data, "foreign", *three_reports(), owner="p2")
    result = data.build("2d", transcripts=[mine, foreign])
    assert result["totals"]["input_tokens"] == 100 and result["partial"] is False
    assert result["data"]["transcripts_read"] == 1 and result["data"]["transcripts_skipped"] == 0
    unknown = rollout(data, "unknown", *three_reports(), owner=None)
    result = data.build("2d", transcripts=[mine, unknown])
    assert result["partial"] is True and result["data"]["transcripts_skipped"] == 1
    assert result["totals"]["input_tokens"] == 100 and result["totals"]["tokens_are_lower_bound"] is True
    assert result["text"].splitlines()[3].startswith("PARTIAL REPORT: 1 Codex transcripts were skipped because")
    # A stray unknown rollout from before the period does not make the report partial.
    old = rollout(data, "old", *three_reports(), owner=None, mtime=NOW - timedelta(days=30))
    assert data.build("2d", transcripts=[mine, old])["partial"] is False


def test_a_missing_or_unreadable_rollout_makes_the_report_partial(data, tmp_path):
    cursor = rollout(data, "a", *three_reports())
    gone = {"path": str(tmp_path / "gone.jsonl"), "project_id": "p1", "client": "codex"}
    link = tmp_path / "link.jsonl"
    link.symlink_to(cursor["path"])
    os.utime(tmp_path, (NOW.timestamp(), NOW.timestamp()))  # a directory is not a rollout, and not a stale one
    result = data.build("2d", transcripts=[cursor, gone, {**gone, "path": str(link)}, {**gone, "path": str(tmp_path)}])
    assert result["partial"] is True and result["data"]["transcripts_missing"] == 3
    assert result["data"]["transcripts_read"] == 1
    assert result["totals"]["tokens_are_lower_bound"] is True and result["totals"]["complete"] is False
    assert codex_rows(result)[0]["complete"] is True
    assert "PARTIAL REPORT: 3 Codex transcripts are gone or unreadable" in result["text"]
    assert "Total  turns 2/2 incomplete  input at least 100" in result["text"]


def test_a_rollout_not_touched_since_before_the_period_is_not_read_and_other_clients_are_ignored(data):
    stale = rollout(data, "a", *three_reports(), mtime=NOW - timedelta(days=4))
    claude = rollout(data, "c", *three_reports(), client="claude")
    result = data.build("2d", transcripts=[stale, claude])
    assert result["data"]["transcripts_read"] == 0 and codex_rows(result) == []
    assert "No turns of the built-in API agent or Codex were found" in result["text"]


def test_a_codex_report_without_a_known_earlier_total_is_a_lower_bound(data):
    # The first report in the files is not a first response (its own usage is smaller than the thread total),
    # so what the thread used before it is unknown: it counts nothing and the row is incomplete.
    cursor = rollout(data, "a", record(1, (500, 50), (100, 10), stamp(1)), record(2, (560, 56), (60, 6), stamp(1)))
    (row,) = codex_rows(data.build("2d", transcripts=[cursor]))
    assert (row["input_tokens"], row["output_tokens"], row["turns"], row["complete_turns"]) == (60, 6, 2, 1)
    assert row["complete"] is False and row["tokens_are_lower_bound"] is True


def test_a_codex_total_that_goes_down_is_not_counted_as_negative_usage(data):
    cursor = rollout(data, "a", record(1, (100, 10), (100, 10), stamp(1)), record(2, (30, 3), (30, 3), stamp(1)),
                     record(3, (50, 5), (20, 2), stamp(1)))
    (row,) = codex_rows(data.build("2d", transcripts=[cursor]))
    assert (row["input_tokens"], row["output_tokens"], row["complete_turns"], row["complete"]) == (120, 12, 2, False)


def test_damaged_codex_records_are_counted_and_other_lines_are_not_parsed(data, monkeypatch):
    junk = ('{"type": "event_msg", "payload": {"type": "user_message", "message": "about token_usage_record"}}\n'
            '{"type": "token_usage_record", "timestamp": "' + stamp(1) + '", "payload": {"thread_id": 5}}\n'
            '{"type": "token_usage_record", "timestamp": "no time", "payload": ' + "{" + '\n'
            '{"type": "response_item", "payload": ' + json.dumps("x" * 200) + "}\n")
    cursor = rollout(data, "a", *three_reports(), junk, "x" * 3000 + "\n", '{"type": "token_usage_record"')
    monkeypatch.setattr("cairnmind.codex_usage.MAX_EVENT_BYTES", 2000)
    result = data.build("2d", transcripts=[cursor])
    # The user message that merely mentions the type is not damage; the two broken records and the cut tail are.
    assert result["data"]["transcript_lines_skipped"] == 3
    (row,) = codex_rows(result)
    assert (row["input_tokens"], row["complete"]) == (100, False)
    assert "3 usage records unreadable" in result["text"]


def test_codex_weeks_use_the_same_iso_weeks(data):
    year_end = datetime(2025, 1, 5, 12, 0, tzinfo=timezone.utc)
    cursor = rollout(data, "a", record(1, (10, 1), (10, 1), "2024-12-29T23:30:00+00:00"),
                     record(2, (25, 3), (15, 2), "2025-01-01T10:00:00+00:00"), mtime=year_end)
    result = data.build("30d", now=year_end, by="week", transcripts=[cursor])
    assert [(g["period"], g["input_tokens"]) for g in codex_rows(result)] == [("2024-W52", 10), ("2025-W01", 15)]
