"""The notice copies the activity panel reads (#144): bounded, never on disk, never trusted."""
from __future__ import annotations

import base64
import json
import threading
from datetime import datetime, timedelta

import pytest

from cairnmind import terminal_notice_feed as feed
from cairnmind.config import Settings
from cairnmind.terminal_notices import NoticeHistory
from cairnmind.terminal_panels import SidebarApp
from test_terminal_panels import settle
from test_terminal_ux080 import setup_ui

SCOPE = {"project_id": "p", "project_name": "P", "worktree": "/w"}


class Memory:
    """Stands in for the pane options of the workspace's tmux server."""

    def __init__(self):
        self.values, self.threads = {}, []

    def publish(self, role, value):
        self.threads.append(threading.current_thread())
        self.values[role] = value

    def read(self):
        return dict(self.values)


def install(monkeypatch):
    memory = Memory()
    monkeypatch.setattr(feed, "channel", lambda settings, workspace_id: memory)
    return memory


def publish(settings, role, history):
    feed.publish(settings, "default", role, feed.snapshot(history))


def raw(rows):
    return base64.urlsafe_b64encode(json.dumps({"version": 1, "entries": rows}).encode()).decode()


def test_round_trip_keeps_the_newest_thirty_cut_and_bounded(tmp_path, monkeypatch):
    install(monkeypatch)
    settings = Settings(data_dir=tmp_path / "data")
    history = NoticeHistory()
    for index in range(40):
        history.add(f"notice {index}", SCOPE, "warn" if index % 2 else "info")
    history.add("x" * 2000, SCOPE, "error")
    publish(settings, "sidebar", history)
    entries = feed.read_all(settings, "default")
    assert len(entries) == 30
    assert entries[0]["text"] == "x" * feed.TEXT_CHARS + "…" and entries[0]["kind"] == "error"
    assert entries[1]["text"] == "notice 39" and entries[1]["kind"] == "warn"
    assert not (tmp_path / "data").exists()  # nothing on disk


def test_a_large_copy_drops_its_oldest_entries_to_fit_one_tmux_command():
    rows = [{"seq": i, "when": "2026-11-14T10:00:00", "kind": "info", "text": "é" * 200, "project_id": "p",
             "project_name": "P", "worktree": "/w" * 40, "count": 1} for i in range(30)]
    value = feed.encode(rows)
    assert len(value) <= feed.MAX_BYTES and set(value) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=")
    kept = feed.decode("sidebar", value, now=datetime(2026, 11, 14, 12))
    assert 0 < len(kept) < 30 and kept[-1]["seq"] == 29  # the newest stay


def test_entries_of_all_panels_merge_newest_first(tmp_path, monkeypatch):
    install(monkeypatch)
    settings = Settings(data_dir=tmp_path / "data")
    for role in feed.ROLES:
        history = NoticeHistory()
        history.add(role + " says hi", SCOPE)
        publish(settings, role, history)
    entries = feed.read_all(settings, "default")
    assert sorted(entry["role"] for entry in entries) == ["graph", "inspector", "sidebar"]
    assert [entry["when"] for entry in entries] == sorted((entry["when"] for entry in entries), reverse=True)


def test_only_ui_panels_keep_a_copy(tmp_path, monkeypatch):
    install(monkeypatch)
    with pytest.raises(ValueError):
        feed.publish(Settings(data_dir=tmp_path / "data"), "default", "activity", [])


def test_a_hostile_copy_is_read_safely():
    now = datetime(2026, 11, 14, 12)
    rows = [{"when": "2026-11-14T10:00:00", "text": "\x1b]52;c;x\x07[bold]hi‮", "kind": "evil",
             "project_id": 5, "count": "9", "seq": -1},
            {"when": "not a date", "text": "skipped"}, {"when": "2026-11-14T10:00:01", "text": 7},
            "junk", {"when": "2026-11-14T10:00:02", "text": "   "},
            {"when": (now + timedelta(days=1)).isoformat(), "text": "Review approved"}]  # dated in the future
    entries = feed.decode("sidebar", raw(rows), now=now)
    assert len(entries) == 1
    entry = entries[0]
    assert "\x1b" not in entry["text"] and "‮" not in entry["text"] and "[bold]hi" in entry["text"]
    assert (entry["kind"], entry["project_id"], entry["count"], entry["seq"]) == ("info", None, 1, 0)
    for value in ("{" * 5000, "not base64!", raw([]) * 400, base64.urlsafe_b64encode(b"[1,2]").decode(), None, 7):
        assert feed.decode("sidebar", value, now=now) == []


def test_a_broken_channel_reads_as_no_notices(tmp_path, monkeypatch):
    class Broken:
        def read(self):
            raise OSError("tmux is gone")
    monkeypatch.setattr(feed, "channel", lambda settings, workspace_id: Broken())
    assert feed.read_all(Settings(data_dir=tmp_path / "data"), "default") == []


async def test_a_panel_replaces_its_copy_on_start_and_copies_off_the_event_loop(tmp_path, monkeypatch):
    memory = install(monkeypatch)
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    stale = NoticeHistory()
    stale.add("from an earlier run", SCOPE)
    publish(settings, "sidebar", stale)
    memory.threads.clear()
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        for _ in range(40):
            await pilot.pause(.05)
            if all(entry["text"] != "from an earlier run" for entry in feed.read_all(settings, "default")):
                break
        assert all(entry["text"] != "from an earlier run" for entry in feed.read_all(settings, "default"))
        app.notice("Check failed: unit", "error")
        app.notice("Check failed: unit", "error")
        for _ in range(40):
            await pilot.pause(.05)
            if any(entry["text"] == "Check failed: unit" for entry in feed.read_all(settings, "default")):
                break
        entries = feed.read_all(settings, "default")
        assert ("Check failed: unit", "error", 2) in [(entry["text"], entry["kind"], entry["count"]) for entry in entries]
        assert memory.threads and threading.main_thread() not in memory.threads


def test_a_notice_of_another_kind_is_not_merged_with_the_last_one():
    history = NoticeHistory()
    history.add("same", SCOPE, "info")
    history.add("same", SCOPE, "error")
    history.add("same", SCOPE, "nonsense")
    assert [(entry.kind, entry.count) for entry in history.entries] == [("info", 1), ("error", 1), ("info", 1)]
