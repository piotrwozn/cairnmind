"""Durability, integrity and operational recovery tests for the agent journal."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import stat

import pytest

from devbrain.agent_journal import AgentJournal, JournalError, ZERO_HASH, canonical
from devbrain.config import Settings


@pytest.fixture
def journal(tmp_path):
    return AgentJournal(Settings.load(tmp_path / "data", create=True), "test-session")


def test_constructor_has_no_filesystem_side_effects(tmp_path):
    journal = AgentJournal(Settings(tmp_path / "absent"), "session")
    assert not journal.path.exists() and not journal.settings.data_dir.exists()


def test_durable_canonical_chain_reopens_with_exact_messages(journal):
    one = journal.append("message", {"role": "user", "content": "Zażółć 🧠"})
    two = journal.append("message", {"role": "assistant", "content": "Gotowe"})
    assert one["seq"] == 1 and one["previous_hash"] == ZERO_HASH
    assert two["seq"] == 2 and two["previous_hash"] == one["hash"]
    for event in (one, two):
        expected = hashlib.sha256(canonical({k: v for k, v in event.items() if k != "hash"})).hexdigest()
        assert event["hash"] == expected
    reopened = AgentJournal(journal.settings, journal.session_id)
    assert reopened.events() == [one, two]
    assert reopened.messages() == [one["data"], two["data"]]
    assert journal.path.read_bytes() == canonical(one) + b"\n" + canonical(two) + b"\n"
    assert stat.S_IMODE(journal.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(journal.directory.stat().st_mode) == 0o700


def test_concurrent_instances_serialize_without_missing_events(journal):
    def append(number):
        AgentJournal(journal.settings, journal.session_id).append("message", {"content": str(number)})
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(append, range(30)))
    events = journal.events()
    assert [row["seq"] for row in events] == list(range(1, 31))
    assert {row["data"]["content"] for row in events} == {str(n) for n in range(30)}


def test_pending_actions_survive_restart_and_need_explicit_resolution(journal):
    data = {"action_id": "one", "name": "run_command", "arguments": {"command": "build"},
            "tool_call_id": "tool-one"}
    journal.append("action_started", data)
    journal.append("action_interrupted", {**data, "reason": "lost reply"})
    journal.append("action_started", {"action_id": "two", "name": "write_file"})
    journal.append("action_completed", {"action_id": "two", "result": {"ok": True}})
    pending = AgentJournal(journal.settings, journal.session_id).pending_actions()
    assert len(pending) == 1 and pending[0]["data"] == data
    assert pending[0]["last_state"] == "action_interrupted"
    journal.append("action_resolved", {"action_id": "one", "resolution": "user checked output"})
    assert not journal.pending_actions()


def test_missing_completion_remains_pending_without_replaying_anything(journal, tmp_path):
    path = tmp_path / "side-effect"
    journal.append("action_started", {"action_id": "uncertain", "name": "run_command",
                                       "arguments": {"command": "touch " + str(path)}})
    assert journal.pending_actions()[0]["data"]["action_id"] == "uncertain"
    assert not path.exists()


def test_registered_credentials_and_http_headers_are_never_serialized(journal):
    key = "sk-provider-private-123456789"
    journal.add_secrets(key)
    event = journal.append("message", {"role": "tool", "content": "value=" + key,
                                       "metadata": {"api_key": "other-private-key",
                                                    "headers": {"Authorization": "Bearer another-secret"}},
                                       "local_token": journal.settings.token})
    raw = journal.path.read_text()
    for value in (key, "other-private-key", "another-secret", journal.settings.token):
        assert value not in raw
    assert event["data"]["content"] == "value=[REDACTED]"
    assert event["data"]["metadata"]["headers"] == "[REDACTED]"
    assert journal.events() == [event]


def test_truncated_known_credential_prefix_is_redacted(journal):
    journal.add_secrets("provider-key-123456789")
    assert journal.redact("output provider-key-123") == "output [REDACTED]"


@pytest.mark.parametrize("corruption", ["changed", "partial", "noncanonical", "missing", "foreign"])
def test_corrupt_history_is_rejected_before_further_append(journal, corruption):
    first = journal.append("message", {"content": "one"})
    second = journal.append("message", {"content": "two"})
    if corruption == "changed":
        raw = journal.path.read_bytes().replace(b'"one"', b'"bad"')
    elif corruption == "partial":
        raw = journal.path.read_bytes()[:-1]
    elif corruption == "noncanonical":
        raw = json.dumps(first).encode() + b"\n" + canonical(second) + b"\n"
    elif corruption == "missing":
        raw = canonical(second) + b"\n"
    else:
        first["session_id"] = "foreign-session"
        first["hash"] = hashlib.sha256(canonical({k: v for k, v in first.items() if k != "hash"})).hexdigest()
        raw = canonical(first) + b"\n"
    journal.path.write_bytes(raw)
    with pytest.raises(JournalError):
        journal.events()
    with pytest.raises(JournalError):
        journal.append("message", {"content": "must not hide damage"})
    assert journal.path.read_bytes() == raw


def test_exhausted_event_budget_prevents_any_append(journal, monkeypatch):
    from devbrain import agent_journal
    journal.append("message", {"content": "existing"})
    before = journal.path.read_bytes()
    monkeypatch.setattr(agent_journal, "MAX_EVENT_BYTES", 400)
    with pytest.raises(JournalError):
        journal.append("message", {"content": "x" * 1000})
    assert journal.path.read_bytes() == before


def test_total_budget_exhaustion_does_not_truncate_history(journal, monkeypatch):
    from devbrain import agent_journal
    journal.append("message", {"content": "existing"})
    before = journal.path.read_bytes()
    monkeypatch.setattr(agent_journal, "MAX_JOURNAL_BYTES", len(before) + 20)
    with pytest.raises(JournalError):
        journal.append("message", {"content": "more"})
    assert journal.path.read_bytes() == before


@pytest.mark.parametrize("bad", ["../escape", "/absolute", "", "a/b", "x\0z"])
def test_session_identifiers_cannot_escape_storage(tmp_path, bad):
    with pytest.raises(JournalError):
        AgentJournal(Settings(tmp_path), bad)


def test_refuses_symlinked_storage_and_insecure_existing_file(journal, tmp_path):
    journal.append("message", {"content": "one"})
    journal.path.chmod(0o644)
    with pytest.raises(JournalError):
        journal.events()
    journal.path.chmod(0o600)
    target = tmp_path / "outside"
    target.write_text("outside")
    journal.path.unlink()
    journal.path.symlink_to(target)
    with pytest.raises(OSError):
        journal.append("message", {"content": "bad"})
    assert target.read_text() == "outside"


def test_refuses_symlinked_parent_without_creating_inside_target(journal, tmp_path):
    journal.settings.data_dir.joinpath("runtime").mkdir(mode=0o700)
    target = tmp_path / "outside"
    target.mkdir(mode=0o700)
    journal.settings.data_dir.joinpath("runtime", "agents").symlink_to(target, target_is_directory=True)
    with pytest.raises(JournalError):
        journal.events()
    assert list(target.iterdir()) == []


def test_lock_and_events_are_fsynced_before_success(journal, monkeypatch):
    real = os.fsync
    calls = []
    def fsync(fd):
        calls.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        return real(fd)
    monkeypatch.setattr(os, "fsync", fsync)
    journal.append("message", {"content": "durable"})
    assert False in calls and True in calls


def test_rejects_nan_and_non_json_before_touching_disk(journal):
    with pytest.raises(JournalError):
        journal.append("message", {"value": float("nan")})
    with pytest.raises(JournalError):
        journal.append("message", {"value": Path("path")})
    assert not journal.path.exists()


def test_existing_fifo_is_refused_without_blocking(journal):
    journal.append("message", {"content": "one"})
    journal.path.unlink()
    os.mkfifo(journal.path, 0o600)
    with pytest.raises(JournalError):
        journal.events()
