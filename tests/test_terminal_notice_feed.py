"""The private notice copy the activity panel reads (#144): bounded, private, never trusted."""
from __future__ import annotations

import json
import os
import stat

import pytest

from cairnmind import terminal_notice_feed as feed
from cairnmind.cockpit_state import _private_directory
from cairnmind.config import Settings
from cairnmind.terminal_notice_feed import NoticeFeed, read_all
from cairnmind.terminal_notices import NoticeHistory
from cairnmind.terminal_panels import SidebarApp
from test_terminal_panels import settle
from test_terminal_ux080 import setup_ui

SCOPE = {"project_id": "p", "project_name": "P", "worktree": "/w"}


def open_workspace(settings, workspace_id="default"):
    """The folders the workspace state creates (0700) before any panel runs."""
    _private_directory(settings.data_dir / "workspaces")
    _private_directory(settings.data_dir / "workspaces" / workspace_id)


def settings_at(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    open_workspace(settings)
    return settings


def test_without_a_workspace_folder_nothing_is_created(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    history = NoticeHistory()
    history.add("hello", SCOPE)
    NoticeFeed(settings, "default", "sidebar").publish(history)
    assert not (tmp_path / "data").exists() and read_all(settings, "default") == []


def test_round_trip_keeps_the_newest_thirty_cut_and_private(tmp_path):
    settings = settings_at(tmp_path)
    history = NoticeHistory()
    for index in range(40):
        history.add(f"notice {index}", SCOPE, "warn" if index % 2 else "info")
    history.add("x" * 2000, SCOPE, "error")
    NoticeFeed(settings, "default", "sidebar").publish(history)
    entries = read_all(settings, "default")
    assert len(entries) == 30
    assert entries[0]["text"] == "x" * 500 + "…" and entries[0]["kind"] == "error"
    assert entries[1]["text"] == "notice 39" and entries[1]["kind"] == "warn"
    path = feed.directory(settings, "default") / "sidebar.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_entries_of_all_panels_merge_newest_first(tmp_path):
    settings = settings_at(tmp_path)
    for role in ("sidebar", "inspector", "graph"):
        history = NoticeHistory()
        history.add(role + " says hi", SCOPE)
        NoticeFeed(settings, "default", role).publish(history)
    assert sorted(entry["role"] for entry in read_all(settings, "default")) == ["graph", "inspector", "sidebar"]
    times = [entry["when"] for entry in read_all(settings, "default")]
    assert times == sorted(times, reverse=True)


def test_only_ui_panels_keep_a_copy_and_the_workspace_id_is_checked(tmp_path):
    settings = settings_at(tmp_path)
    with pytest.raises(ValueError):
        NoticeFeed(settings, "default", "activity")
    with pytest.raises(ValueError):
        NoticeFeed(settings, "../escape", "sidebar")


def test_a_linked_file_or_folder_is_refused(tmp_path):
    settings = settings_at(tmp_path)
    folder = feed.directory(settings, "default")
    folder.mkdir(parents=True, mode=0o700)
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"version": 1, "entries": [{"when": "2026-11-14T10:00:00", "text": "planted"}]}))
    (folder / "sidebar.json").symlink_to(target)
    assert read_all(settings, "default") == []
    history = NoticeHistory()
    history.add("mine", SCOPE)
    NoticeFeed(settings, "default", "sidebar").publish(history)  # replaces the link, never writes through it
    assert json.loads(target.read_text())["entries"][0]["text"] == "planted"
    os.unlink(folder / "sidebar.json")
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    os.rmdir(folder)
    os.symlink(other, folder)
    with pytest.raises(ValueError):
        NoticeFeed(settings, "default", "sidebar").publish(history)
    assert read_all(settings, "default") == []


def test_a_hostile_file_is_read_safely(tmp_path):
    settings = settings_at(tmp_path)
    folder = feed.directory(settings, "default")
    folder.mkdir(parents=True, mode=0o700)
    rows = [{"when": "2026-11-14T10:00:00", "text": "\x1b]52;c;x\x07[bold]hi‮", "kind": "evil",
             "project_id": 5, "count": "9", "seq": -1},
            {"when": "not a date", "text": "skipped"}, {"when": "2026-11-14T10:00:01", "text": 7},
            "junk", {"when": "2026-11-14T10:00:02", "text": "   "}]
    (folder / "sidebar.json").write_text(json.dumps({"version": 1, "entries": rows}))
    (folder / "inspector.json").write_text("{" * 5000)
    (folder / "graph.json").write_text(json.dumps({"version": 1, "entries": [
        {"when": "2026-11-14T10:00:00", "text": "big " * 30000}]}))
    entries = read_all(settings, "default")
    assert len(entries) == 1
    entry = entries[0]
    assert "\x1b" not in entry["text"] and "‮" not in entry["text"] and "[bold]hi" in entry["text"]
    assert (entry["kind"], entry["project_id"], entry["count"], entry["seq"]) == ("info", None, 1, 0)


async def test_starting_a_panel_replaces_a_stale_copy_with_its_own_history(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    open_workspace(settings)
    stale = NoticeHistory()
    stale.add("from an earlier run", SCOPE)
    NoticeFeed(settings, "default", "sidebar").publish(stale)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        assert all(entry["text"] != "from an earlier run" for entry in read_all(settings, "default"))
        app.notice("Check failed: unit", "error")
        app.notice("Check failed: unit", "error")
        for _ in range(40):
            await pilot.pause(.05)
            if any(entry["text"] == "Check failed: unit" for entry in read_all(settings, "default")):
                break
        entries = read_all(settings, "default")
        assert ("Check failed: unit", "error", 2) in [(entry["text"], entry["kind"], entry["count"]) for entry in entries]


def test_a_notice_of_another_kind_is_not_merged_with_the_last_one():
    history = NoticeHistory()
    history.add("same", SCOPE, "info")
    history.add("same", SCOPE, "error")
    history.add("same", SCOPE, "nonsense")
    assert [(entry.kind, entry.count) for entry in history.entries] == [("info", 1), ("error", 1), ("info", 1)]
