"""The per-project list of third-party MCP servers is a validated owner preference (#126)."""
from __future__ import annotations

import pytest

from cairnmind.cockpit_state import CockpitState
from cairnmind.config import Settings


def server(**changes):
    value = {"id": "files", "command": "/usr/bin/env", "args": ["python3", "-m", "server"],
             "env": {"LOG_LEVEL": "quiet"}}
    value.update(changes)
    return value


def test_server_list_round_trips_per_project_and_old_files_load(tmp_path):
    settings = Settings.load(tmp_path / "data")
    state = CockpitState(settings)
    assert state.load()["project_mcp_servers"] == {}
    state.path.parent.mkdir(parents=True, mode=0o700)
    state.path.write_text('{"version": 2, "language": "pl"}')
    assert state.load()["project_mcp_servers"] == {}
    saved = state.update({"project_mcp_servers": {"project-a": [server()]}})
    assert saved["project_mcp_servers"] == {"project-a": [server()]} == state.load()["project_mcp_servers"]
    assert "project-b" not in state.load()["project_mcp_servers"]
    # Optional fields get their empty value; other preferences keep the list.
    minimal = state.update({"project_mcp_servers": {"project-a": [{"id": "x", "command": "/bin/x"}]}})
    assert minimal["project_mcp_servers"]["project-a"] == [{"id": "x", "command": "/bin/x", "args": [], "env": {}}]
    assert state.update({"view": "cockpit"})["project_mcp_servers"] == minimal["project_mcp_servers"]


@pytest.mark.parametrize("bad", [
    [server(url="http://example.test")],                      # unknown field
    [server(credential_ref="env:TOKEN")],                     # no credential fields in this version
    [server(command="python3")],                              # relative command
    [server(command="/bin/x\x1b[31m")],                       # control characters in the command
    [server(), server()],                                     # duplicate id
    [server(id="my_server")],                                 # underscore is the name separator
    [server(id="Files")], [server(id="")], [server(id="a" * 25)],
    [server(env={"API_KEY": "x"})], [server(env={"GITHUB_TOKEN": "x"})], [server(env={"DB_PASSWORD": "x"})],
    [server(env={"CAIRNMIND_DATA_DIR": "/x"})], [server(env={"devbrain_x": "1"})],
    [server(env={"OPENAI_API_KEY": "x"})], [server(env={"MY_SECRET": "x"})], [server(env={"BAD NAME": "x"})],
    [server(env={"OK": 1})], [server(env={"OK": "a\0b"})], [server(env={f"V{i}": "x" for i in range(17)})],
    [server(args=["a\0b"])], [server(args=["x"] * 33)], [server(args=["x" * 4097])], [server(args="--x")],
    [server(id=f"s{i}") for i in range(5)],                   # a fifth server
    "not a list", [None],
])
def test_invalid_server_lists_are_refused_and_change_nothing(tmp_path, bad):
    state = CockpitState(Settings.load(tmp_path / "data"))
    state.update({"project_mcp_servers": {"project": [server()]}})
    before = state.path.read_bytes()
    with pytest.raises(ValueError):
        state.update({"project_mcp_servers": {"project": bad}})
    assert state.path.read_bytes() == before


def test_project_keys_are_bounded_and_profiles_stay_strict(tmp_path):
    state = CockpitState(Settings.load(tmp_path / "data"))
    for bad in ({"": []}, {"x" * 161: []}, ["project"]):
        with pytest.raises(ValueError):
            state.update({"project_mcp_servers": bad})
    # The server list is not a profile field: a profile with it is still refused.
    with pytest.raises(ValueError):
        state.update({"profiles": [{"id": "api", "client": "api", "label": "API", "provider": "ollama",
                                    "model": "m", "mcp_servers": [server()]}]})
