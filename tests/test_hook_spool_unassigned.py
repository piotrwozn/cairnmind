"""Hook events of a folder that is not registered (a removed project's, #327) wait without starving the others."""
import json
from types import SimpleNamespace

from cairnmind.integrations import drain_hook_spool


class Store:
    def resolve_project(self, value, recorded=False):
        if value != "/live":
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
