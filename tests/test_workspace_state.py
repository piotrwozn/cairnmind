from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from cairnmind.config import Settings
from cairnmind.workspace_state import WorkspaceState


def test_workspace_read_is_pure_and_updates_are_private_cas(tmp_path):
    settings = Settings.load(tmp_path / "missing")
    state = WorkspaceState(settings)
    assert state.read()["revision"] == 0
    assert not settings.data_dir.exists()
    saved = state.update({"focus": "graph"}, expected_revision=0)
    assert saved["revision"] == 1
    assert state.directory.stat().st_mode & 0o777 == 0o700
    assert state.path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="stale_workspace_revision"):
        state.update({"focus": "inspector"}, expected_revision=0)
    assert state.update({"focus": "graph"})["revision"] == 1


def test_work_is_explicit_and_entity_selection_belongs_to_selected_project(tmp_path):
    state = WorkspaceState(Settings.load(tmp_path / "data"))
    project = {"id": "project-a", "name": "Project A", "root": str(tmp_path)}
    inspected = state.select_project(project)
    assert inspected["intent"] == "inspect" and inspected["work_request"] is None
    working = state.select_project(project, intent="work", requested_slot_ids=["first"])
    assert working["work_request"]
    assert state.select_project(project, intent="work", requested_slot_ids=["first"])["work_request"] != working["work_request"]
    selected = {"project_id": "project-a", "entity_type": "revision", "id": "revision-id",
                "revision": 3, "owner_type": "record", "owner_id": "record-id"}
    state.update({"selected_entity": selected})
    with pytest.raises(ValueError, match="innego projektu"):
        state.update({"selected_entity": {**selected, "project_id": "project-b"}})
    state.select_project(None)
    state.update({"selected_entity": {"project_id": None, "entity_type": "inbox", "id": "unassigned"}})
    with pytest.raises(ValueError):
        state.select_project(None, intent="work", requested_slot_ids=["first"])


def test_concurrent_state_edits_never_lose_revision_and_reject_unsafe_paths(tmp_path):
    state = WorkspaceState(Settings.load(tmp_path / "data"))
    def change(index):
        return state.update({"last_error": str(index)})
    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(change, range(20)))
    assert max(row["revision"] for row in results) == 20
    assert state.read()["revision"] == 20
    for change in ({"workspace_id": "foreign"}, {"run_ids": [{}]}, {"worktree": "relative"},
                   {"pane_ids": {"sidebar": "%0; command"}}, {"visible_slot_ids": ["missing"]}):
        with pytest.raises(ValueError):
            state.update(change)
    path = state.path
    path.unlink()
    path.symlink_to(tmp_path / "outside")
    with pytest.raises(OSError):
        state.read()


def test_assistant_preferences_are_private_scoped_cas_without_start(tmp_path):
    state = WorkspaceState(Settings.load(tmp_path / "data"))
    first = {"id": "a", "name": "A", "root": str(tmp_path)}
    second = {**first, "id": "b", "name": "B"}
    state.select_project(first)
    before = state.read()
    choices = [{"project_id": "a", "worktree": str(tmp_path), "slot_id": "claude"}]
    state.update({"assistant_selections": choices}, expected_revision=before["revision"])
    state.select_project(second)
    with pytest.raises(ValueError, match="stale_workspace_revision"):
        state.update({"assistant_selections": []}, expected_revision=before["revision"])
    state.select_project(first)
    after = WorkspaceState(state.settings).read()
    assert after["assistant_selections"] == choices
    assert after["intent"] == "inspect" and after["work_request"] is None
    assert after["requested_slot_ids"] == [] and after["focus"] == before["focus"]
    assert after["run_ids"] == [] and after["slots"] == []
    assert state.path.stat().st_mode & 0o777 == 0o600
    # The field is optional in already-written version1 state files.
    import json
    old = json.loads(state.path.read_text())
    del old["assistant_selections"]
    state.path.write_text(json.dumps(old))
    assert state.read()["assistant_selections"] == []


@pytest.mark.parametrize("change", [
    {"project_id": None}, {"slot_id": ""}, {"worktree": "relative"},
    {"worktree": "/a/../b"}, {"worktree": "/a\0b"}, {"extra": "unexpected"},
])
def test_assistant_preferences_reject_invalid_records_without_committing(tmp_path, change):
    state = WorkspaceState(Settings.load(tmp_path / "data"))
    row = {"project_id": "a", "worktree": str(tmp_path), "slot_id": "claude", **change}
    with pytest.raises(ValueError):
        state.update({"assistant_selections": [row]})
    assert not state.path.exists()


@pytest.mark.parametrize("kind", ["duplicate", "overflow", "not-list"])
def test_assistant_preferences_reject_ambiguous_or_unbounded_history(tmp_path, kind):
    state = WorkspaceState(Settings.load(tmp_path / "data"))
    row = {"project_id": "a", "worktree": str(tmp_path), "slot_id": "claude"}
    rows = [row, dict(row)] if kind == "duplicate" else [dict(row, project_id=str(i)) for i in range(1001)]
    with pytest.raises(ValueError):
        state.update({"assistant_selections": {} if kind == "not-list" else rows})
    assert not state.path.exists()


def menu_request(tmp_path, row):
    """What choosing this menu row writes: the real menu dispatch and the real request_command, a real state."""
    from types import SimpleNamespace

    from cairnmind.terminal_commands import dispatch_command
    from cairnmind.workspace import WorkspaceManager

    state = WorkspaceState(Settings(data_dir=tmp_path / "data"))
    stub = SimpleNamespace(state=state, _layout=lambda: None)
    stub.request_command = lambda action: WorkspaceManager.request_command(stub, action)
    dispatch_command(stub, row)
    return state.read()["command_request"]


def test_every_command_the_menu_lists_is_accepted_by_the_workspace_state(tmp_path):
    from cairnmind.terminal_commands import COMMANDS

    for row in COMMANDS:
        if row[2] != "command":
            continue
        request = menu_request(tmp_path / row[3], row)
        assert request["action"] == row[3] and request["target_role"] == "sidebar", row


def test_ui_command_option_takes_every_command_of_the_menu_and_of_the_state(tmp_path, monkeypatch):
    """`--ui-command` used to list eight of the twelve commands, so `ask`, `brief`, `next_stage` and `notices`
    worked from the menu and were refused on the command line."""
    import sys

    from cairnmind import workspace
    from cairnmind.terminal_commands import COMMANDS
    from cairnmind.workspace_state import UI_COMMANDS

    assert {row[3] for row in COMMANDS if row[2] == "command"} <= set(UI_COMMANDS)
    assert len(UI_COMMANDS) == len(set(UI_COMMANDS))
    for action in UI_COMMANDS:
        calls = []
        monkeypatch.setattr(workspace.WorkspaceManager, "request_command", lambda self, value: calls.append(value))
        monkeypatch.setattr(sys, "argv", ["workspace", "--data-dir", str(tmp_path / "data"), "--ui-command", action])
        workspace.main()
        assert calls == [action]
        state = WorkspaceState(Settings(data_dir=tmp_path / "state"))
        state.update({"command_request": {"id": "one", "action": action, "target_role": "sidebar"}})
    monkeypatch.setattr(sys, "argv", ["workspace", "--data-dir", str(tmp_path / "data"), "--ui-command", "no_such"])
    with pytest.raises(SystemExit):
        workspace.main()
