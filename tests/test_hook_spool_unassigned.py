"""Hook events of a folder that is not registered (a removed project's, #327) wait without starving the others."""
import json
from types import SimpleNamespace

from cairnmind.memory.integrations import drain_hook_spool


class Store:
    def __init__(self):
        self.roots, self.resolved = ["/live"], 0

    def query(self, sql, params=None):
        return [{"path": path} for path in self.roots] if sql == "SELECT path FROM project_roots" else []

    def resolve_project(self, value, recorded=False):
        self.resolved += 1
        if value not in self.roots:
            raise ValueError("Unknown project")
        return {"id": "p1"}


def test_more_unassigned_events_than_the_limit_do_not_hide_a_registered_one(tmp_path):
    """No engine, no tmux, no path rules: the same on macOS."""
    spool = tmp_path / "hook-events"
    spool.mkdir()
    for number in range(120):
        (spool / f"a{number:03}.json").write_text(json.dumps({"client": "claude", "cwd": "/removed"}))
    (spool / "zzz.json").write_text(json.dumps({"client": "claude", "cwd": "/live"}))  # sorts after all of them
    counts = drain_hook_spool(SimpleNamespace(data_dir=tmp_path), Store(), limit=100)
    assert counts["unassigned"] == 120 and counts["errors"] == []
    assert not (spool / "zzz.json").exists()  # the registered folder's event was handled
    assert len(list(spool.glob("a*.json"))) == 120  # the others still wait, untouched


def test_waiting_events_are_not_read_again_until_the_registered_folders_change(tmp_path):
    """A drain's work does not grow with the pile: an event no folder took is looked at again only after a
    project was added or removed. No engine: the same on macOS."""
    spool = tmp_path / "hook-events"
    spool.mkdir()
    for number in range(50):
        (spool / f"a{number:03}.json").write_text(json.dumps({"client": "claude", "cwd": "/removed"}))
    settings, store = SimpleNamespace(data_dir=tmp_path), Store()
    assert drain_hook_spool(settings, store)["unassigned"] == 50 and store.resolved == 50
    (spool / "new.json").write_text(json.dumps({"client": "claude", "cwd": "/removed"}))
    assert drain_hook_spool(settings, store)["unassigned"] == 51 and store.resolved == 51  # only the new one
    store.roots.append("/removed")  # the project is back
    counts = drain_hook_spool(settings, store)
    assert counts["unassigned"] == 0 and store.resolved == 102 and list(spool.glob("*.json")) == []
