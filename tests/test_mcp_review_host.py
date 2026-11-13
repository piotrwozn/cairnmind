"""Review fixes of #126 at the host and configuration level: stops, starts, schemas, paths and edits."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import time

import pytest

from cairnmind import mcp_host, mcp_schema
from cairnmind.cockpit_state import CockpitState
from cairnmind.config import Settings
from cairnmind.mcp_host import McpHost, McpServer
from cairnmind.mcp_schema import admit, valid_arguments
from cairnmind.mcp_servers import LOADER_ENV, inside_worktree, validate_server
from conftest import MCP_FIXTURE, mcp_fixture_processes


def server(*modes, identifier="files", **extra):
    return {"id": identifier, "command": sys.executable, "args": [MCP_FIXTURE, *modes], "env": {}, **extra}


async def until(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(.05)


def host_for(tmp_path, *definitions, **options):
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    return McpHost(list(definitions), worktree=worktree, reserved={"read_file"}, **options)


@pytest.mark.asyncio
async def test_a_stop_that_has_begun_completes_when_its_caller_is_cancelled(tmp_path, monkeypatch):
    """Finding 1: a server that ignores its input and SIGTERM, cancelled inside the stop after a start timeout."""
    monkeypatch.setattr(mcp_host, "START_SECONDS", 1)
    host = host_for(tmp_path, server("deaf"))
    task = asyncio.create_task(host.start())
    try:
        await until(lambda: len(mcp_fixture_processes()) >= 2)  # the server and the child in its group
        await until(lambda: "files" in host.servers and host.servers["files"].state == "off"
                    and host.servers["files"].process is not None)
        task.cancel()  # the person presses Ctrl+C while the stop waits for the process
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await host.close()
    assert mcp_fixture_processes() == []


@pytest.mark.asyncio
async def test_a_stop_goes_on_when_the_task_awaiting_it_is_cancelled(tmp_path):
    """Finding 1, shielded variant: nobody awaits the stop again, and it still ends with the group gone."""
    host = host_for(tmp_path, server("stubborn"))
    try:
        await host.start()
        details = json.loads((await host.call("mcp_files_children", {}, 10))["content"][0]["text"])
        stopping = asyncio.create_task(host.servers["files"].stop())
        await asyncio.sleep(.3)  # both ignore SIGTERM; the stop waits a second before SIGKILL
        assert {details["pid"], details["child"]} <= set(mcp_fixture_processes())
        stopping.cancel()
        await asyncio.sleep(2)
        assert mcp_fixture_processes() == [] and host.servers["files"].process is None
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_a_timeout_kills_the_whole_group_and_close_kills_children_of_a_dead_leader(tmp_path):
    host = host_for(tmp_path, server(), server(identifier="two"))
    try:
        await host.start()
        first = json.loads((await host.call("mcp_files_children", {}, 10))["content"][0]["text"])
        second = json.loads((await host.call("mcp_two_children", {}, 10))["content"][0]["text"])
        assert (await host.call("mcp_files_slow", {}, 1))["error_code"] == "mcp_call_timeout"
        assert {first["pid"], first["child"]}.isdisjoint(mcp_fixture_processes())  # the child ignored SIGTERM
        assert second["child"] in mcp_fixture_processes()
    finally:
        await host.close()
    assert mcp_fixture_processes() == []


@pytest.mark.asyncio
async def test_an_interrupted_start_stops_what_it_started_and_starts_again(tmp_path):
    """Findings 5 and 9: nothing counts as started until the start has finished."""
    host = host_for(tmp_path, server("slow_start", identifier="slow"), server(identifier="quick"))
    task = asyncio.create_task(host.start())
    try:
        await until(lambda: "quick" in host.servers and host.servers["quick"].state == "running")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert host.started is False and host.catalogue == {} and host.servers == {}
        assert mcp_fixture_processes() == []
        events = await host.start()
        assert [kind for kind, _ in events] == ["mcp_server_started", "mcp_server_started"]
        assert {"mcp_slow_echo", "mcp_quick_echo"} <= set(host.catalogue)
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_any_exception_class_is_only_that_servers_fixed_code(tmp_path, monkeypatch):
    """Finding 8: an unexpected exception class, a lone-surrogate cursor or description, an id it cannot echo."""
    original = McpServer.start

    async def start(self, deadline):
        if self.id == "bad":
            raise OverflowError("the repetition number is too large")
        return await original(self, deadline)
    monkeypatch.setattr(McpServer, "start", start)
    host = host_for(tmp_path, server(identifier="bad"), server("surrogate_cursor", identifier="cursor"),
                    server("surrogates", identifier="lone"), server(identifier="good"))
    try:
        events = dict((data["server_id"], (kind, data)) for kind, data in await host.start())
        assert events["bad"] == ("mcp_server_failed", {"server_id": "bad", "code": "start_failed"})
        assert events["cursor"] == ("mcp_server_failed", {"server_id": "cursor", "code": "catalogue_invalid"})
        assert events["good"][0] == events["lone"][0] == "mcp_server_started"
        description = host.catalogue["mcp_lone_lone"]["schema"]["description"]
        assert "\ud800" not in description and description.endswith("Description ? with a lone surrogate")
        json.dumps(events["lone"][1]).encode("utf-8")
        result = await host.call("mcp_lone_echo", {"text": "x"}, 10)  # the server first sends an id "\ud800"
        assert result["ok"] is True and result["content"][0]["text"] == "result ? text"
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_only_the_conservative_schema_subset_is_admitted(tmp_path):
    """Findings 3 and 4: what providers refuse or the engine cannot bound skips the tool with a reason."""
    host = host_for(tmp_path, server("regex_schemas", "picky_schemas"))
    try:
        events = await host.start()
        skipped = {row["tool"]: row["reason"] for row in events[0][1]["skipped"] if "tool" in row}
        assert skipped == {"selfref": "schema_unsupported", "danglingref": "schema_unsupported",
                           "deep": "schema_unsupported", "toplevel": "schema_unsupported",
                           "spacekey": "schema_unsupported", "noitems": "schema_unsupported",
                           "defs": "schema_unsupported"}
        catastrophic = host.catalogue["mcp_files_catastrophic"]["schema"]["parameters"]
        assert catastrophic == {"type": "object", "properties": {"q": {"type": "string"}}}
        assert host.catalogue["mcp_files_overflow"]["schema"]["parameters"] == {
            "type": "object", "properties": {"a": {"type": "string"}}}
        assert host.catalogue["mcp_files_nested"]["schema"]["parameters"] == {
            "type": "object", "properties": {"a": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
            "required": ["a"]}
        offered = json.dumps(host.schemas())
        for keyword in ("pattern", "format", "$ref", "$defs", "$schema", "title", "default"):
            assert keyword not in offered, keyword
    finally:
        await host.close()


def test_schema_subset_rules():
    assert admit({"type": "object", "properties": {"a": {"type": "array", "items": {"type": "integer"},
                                                         "maxItems": 3}}})[1] is None
    for refused in ({"type": "object", "oneOf": [{}]}, {"type": "object", "properties": {"a": {"const": 1}}},
                    {"type": "object", "properties": {"a": {"type": "array", "items": {}, "uniqueItems": True}}},
                    {"type": "object", "properties": {"a": {"enum": [{"x": 1}]}}},
                    {"type": "object", "properties": {"a": {"type": "string", "if": {}}}}):
        assert admit(refused) == (None, "schema_unsupported"), refused
    assert admit({"type": "string"}) == (None, "invalid_schema")


@pytest.mark.asyncio
async def test_argument_validation_is_bounded_and_never_blocks_the_loop(monkeypatch):
    class Slow:
        def __init__(self, schema):
            pass

        def is_valid(self, arguments):
            time.sleep(1.5)
            return True

    class Broken(Slow):
        def is_valid(self, arguments):
            raise RecursionError()
    monkeypatch.setattr(mcp_schema, "VALIDATION_SECONDS", .2)
    ticks = []

    async def ticker():
        for _ in range(5):
            ticks.append(time.monotonic())
            await asyncio.sleep(.02)
    monkeypatch.setattr(mcp_schema, "Draft202012Validator", Slow)
    started = time.monotonic()
    results = await asyncio.gather(valid_arguments({"type": "object"}, {}), ticker())
    assert results[0] is None and time.monotonic() - started < 1 and len(ticks) == 5
    await asyncio.sleep(1.6)  # the one validation thread finishes the slow check first
    monkeypatch.setattr(mcp_schema, "Draft202012Validator", Broken)
    assert await valid_arguments({"type": "object"}, {}) is False


def test_variables_that_change_loaded_code_are_refused():
    """Finding 6."""
    for name in (*LOADER_ENV, "LD_DEBUG_OUTPUT", "DYLD_INSERT_LIBRARIES", "pythonpath"):
        with pytest.raises(ValueError, match="changes which code"):
            validate_server({"id": "x", "command": "/bin/x", "env": {name: "/opt/lib"}})
    assert validate_server({"id": "x", "command": "/bin/x", "env": {"LOG_LEVEL": "warn"}})["env"] == {"LOG_LEVEL": "warn"}
    with pytest.raises(ValueError):
        validate_server({"id": "x", "command": "/bin/x", "args": ["\ud800"]})


def test_paths_into_the_worktree_are_compared_resolved_and_by_component(tmp_path):
    """Findings 6, 11 and 12."""
    worktree = tmp_path / "app"
    (worktree / "tools").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(worktree, target_is_directory=True)

    def refused(**definition):
        return inside_worktree({"id": "x", "command": "/usr/bin/python3", "args": [], "env": {}, **definition},
                               worktree)
    assert refused(env={"DATA_DIR": str(worktree / "tools")})
    assert refused(args=["--directory=" + str(alias) + "/tools", "server.py"])
    assert refused(args=["--require=" + str(worktree).replace("/", "//") + "/hook.js"])
    assert refused(args=[str(worktree) + "/./tools/srv.py"])
    assert refused(args=["--paths=/usr/lib:" + str(worktree) + "/tools"])
    assert refused(command=str(alias / "bin" / "server"))
    assert refused(args=["--root=" + str(worktree)])
    # Siblings that only share a name prefix are not the worktree.
    assert not refused(command=str(tmp_path / "app-mcp" / "bin" / "server"),
                       args=[str(tmp_path / "app.venv" / "bin" / "tool"), "--db=" + str(tmp_path / "app-data" / "x")],
                       env={"CACHE": str(tmp_path / "app-cache")})


@pytest.mark.asyncio
async def test_the_host_refuses_an_environment_value_into_the_worktree(tmp_path):
    host = host_for(tmp_path, server(env={"DATA_DIR": str(tmp_path / "worktree" / "data")}))
    try:
        assert await host.start() == [("mcp_server_failed", {"server_id": "files", "code": "definition_refused"})]
        assert mcp_fixture_processes() == []
    finally:
        await host.close()


def test_an_edit_replaces_only_its_project_and_only_the_list_it_showed(tmp_path):
    """Finding 13."""
    state = CockpitState(Settings.load(tmp_path / "data"))
    one = {"id": "one", "command": "/bin/one", "args": [], "env": {}}
    two = {"id": "two", "command": "/bin/two", "args": [], "env": {}}
    other = {"id": "x", "command": "/bin/x", "args": [], "env": {}}
    state.update({"project_mcp_servers": {"a": [one], "b": [other]}})
    shown_b = [other]
    # Elsewhere, meanwhile: x is removed from project b.
    state.update({"project_mcp_server_edit": {"project": "b", "expected": shown_b, "servers": []}})
    saved = state.update({"project_mcp_server_edit": {"project": "a", "expected": [one], "servers": [one, two]}})
    assert saved["project_mcp_servers"] == {"a": [one, two]}  # x is not brought back
    before = state.path.read_bytes()
    with pytest.raises(ValueError, match="changed meanwhile"):
        state.update({"project_mcp_server_edit": {"project": "a", "expected": [one], "servers": []}})
    with pytest.raises(ValueError):
        state.update({"project_mcp_servers": {}, "project_mcp_server_edit": {"project": "a", "expected": [one, two],
                                                                             "servers": []}})
    with pytest.raises(ValueError):
        state.update({"project_mcp_server_edit": {"project": "a", "expected": [one, two],
                                                  "servers": [{"id": "y", "command": "/bin/y", "env": {"PATH": "/x"}}]}})
    assert state.path.read_bytes() == before
    assert Path(state.path).exists()
