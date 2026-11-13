"""Second review of #126: the worktree check, the session taint, the retry, validation cost, spawns, wording."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time

import pytest

from cairnmind import mcp_host, mcp_schema
from cairnmind.api_providers import ProviderError, _error_code
from cairnmind.brief import build_brief
from cairnmind.evidence import THIRD_PARTY_WRITTEN, evidence_states, orphans_report
from cairnmind.impact import build_impact
from cairnmind.mcp_host import McpHost
from cairnmind.mcp_schema import admit
from cairnmind.mcp_servers import inside_worktree, server_directory
from cairnmind.retrieval import Retriever
from cairnmind.sessions import format_packet
from conftest import MCP_FIXTURE, mcp_fixture_processes
from test_api_agent import Memory, ScriptedProvider, call, reply, setup as setup
from test_impact_verdicts import changed as changed, entry, workspace as workspace
from test_mcp_agent import acked, agent, events, names, results, server
from test_mcp_review_agent import chunks_of, journal_of
from test_mcp_sources import workspace as make_workspace

MARK = {"origin_client": "mcp:tracker", "origin_session_id": "s-1", "origin_history": "archived",
        "origin_evidence": "degraded"}


# Item 1: the worktree check -------------------------------------------------------------------------------

def test_embedded_relative_and_linked_paths_into_the_worktree_are_refused(tmp_path):
    worktree = tmp_path / "wt"
    (worktree / ".venv" / "bin").mkdir(parents=True)
    (worktree / ".venv" / "bin" / "python").symlink_to(sys.executable)  # a link inside that points out
    outside_link = tmp_path / "shortcut"
    outside_link.symlink_to(worktree, target_is_directory=True)  # a link outside that points in

    def refused(command="/usr/bin/env", args=(), env=None):
        return inside_worktree({"id": "x", "command": command, "args": list(args), "env": env or {}}, worktree)
    relative = os.path.relpath(worktree / "srv.py", server_directory())
    assert relative.startswith("..")
    assert refused("/bin/sh", ["-c", f"cd {worktree} && ./srv"])          # inside a shell command
    assert refused(args=[f"-f{worktree}/x"])                                 # glued to an option
    assert refused(args=[f"--opt=key={worktree}/x"])                         # after a second '='
    assert refused(args=[f"a,{worktree}/x"])                                 # in a comma list
    assert refused(args=[relative])                                          # relative to the server's directory
    assert refused(str(worktree / ".venv" / "bin" / "python"), ["-m", "srv"])  # venv interpreter in the worktree
    assert refused(args=[str(outside_link / "srv.py")])                      # link outside pointing in
    assert refused(env={"DATA": f"{str(worktree).replace('/', '//')}//data"})
    # A sibling that shares a name prefix, and ordinary relative arguments, are not inside.
    assert not refused(str(tmp_path / "wt-mcp" / "bin" / "server"),
                       ["-m", "server", "--db=" + str(tmp_path / "wt.data"), str(tmp_path / "wt-cache") + "/x"],
                       {"CACHE": str(tmp_path / "wt_old")})


# Items 2 and 3: the session taint ---------------------------------------------------------------------------

RESULT = {"content": [{"type": "text", "text": "quetzalbanner is fixed"}], "ok": True,
          "third_party_untrusted": True}


def test_after_a_third_party_result_nothing_the_agent_says_or_does_is_searchable(tmp_path):
    store, project, root, manager = make_workspace(tmp_path)
    _, sources = journal_of(store, manager, project, [
        ("message", {"role": "user", "content": "Fetch it and write it down."}),
        ("action_completed", {"action_id": "a1", "name": "mcp_tracker_fetch", "tool_call_id": "c1", "result": RESULT}),
        ("tool_requested", {"tool_call_id": "c2", "name": "write_file", "arguments": {
            "path": "notes.md", "content": "quetzalbanner fixed, says the tracker", "expected_sha256": None}}),
        ("action_started", {"action_id": "a2", "name": "write_file", "tool_call_id": "c2",
                            "arguments": {"path": "notes.md", "content": "quetzalbanner fixed"}}),
        ("action_completed", {"action_id": "a2", "name": "write_file", "tool_call_id": "c2",
                              "result": {"ok": True, "path": "notes.md"}}),
        ("message", {"role": "tool", "tool_call_id": "c2", "name": "write_file", "content": "written quetzalbanner"}),
        ("approval_requested", {"action_id": "a3", "command": "echo quetzalbanner", "tool_call_id": "c3"}),
        ("message", {"role": "assistant", "content": "Done: quetzalbanner."}),
        ("action_resolved", {"action_id": "a3", "resolution": "I checked marmotlantern myself.", "actor": "user"}),
        ("message", {"role": "user", "content": "Thanks, ibexharbor."})])
    person = {sources[0]["id"], sources[8]["id"], sources[9]["id"]}
    for source in sources:
        if source["id"] in person:
            assert "origin_evidence" not in source["metadata"] and chunks_of(store, source["id"])
        else:
            assert source["metadata"]["origin_evidence"] == "degraded", source["metadata"]["agent_sequence"]
            assert chunks_of(store, source["id"]) == []
    retriever = Retriever(store, store.settings)
    assert retriever.search(project["id"], "quetzalbanner") == []
    assert retriever.search(project["id"], "marmotlantern") and retriever.search(project["id"], "ibexharbor")


@pytest.mark.parametrize("events_before", [
    [("approval_requested", {"kind": "mcp_tool", "name": "mcp_tracker_fetch", "action_id": "a"}),
     ("approval_decided", {"kind": "mcp_tool", "name": "mcp_tracker_fetch", "action_id": "a", "approved": False}),
     ("message", {"role": "tool", "name": "mcp_tracker_fetch", "tool_call_id": "c",
                  "content": json.dumps({"ok": False, "error_code": "approval_denied"})})],
    [("message", {"role": "tool", "name": "mcp_tracker_fetch", "tool_call_id": "c",
                  "content": json.dumps({"ok": False, "error_code": "invalid_tool_arguments"})})],
    [("message", {"role": "tool", "name": "mcp_tracker_fetch", "tool_call_id": "c",
                  "content": json.dumps({"ok": False, "error_code": "arguments_too_large"})})],
    [("message", {"role": "tool", "name": "mcp_tracker_fetch", "tool_call_id": "c",
                  "content": json.dumps({"ok": False, "error_code": "arguments_too_long_to_show"})})],
    [("message", {"role": "tool", "name": "mcp_foo_bar", "tool_call_id": "c",
                  "content": json.dumps({"ok": False, "error_code": "invalid_tool_call"})})],
    [("message", {"role": "tool", "name": "mcp_tracker_fetch", "tool_call_id": "c",
                  "content": json.dumps({"ok": False, "error_code": "resume_required"})})],
    [("action_completed", {"action_id": "a", "name": "mcp_tracker_fetch", "tool_call_id": "c", "result": {
        "ok": False, "error_code": "result_too_large", "bytes": 2000000, "sha256": "0" * 64}})],
    [("action_completed", {"action_id": "a", "name": "mcp_tracker_fetch", "tool_call_id": "c", "result": {
        "ok": False, "error_code": "mcp_call_timeout", "third_party_untrusted": True}})],
], ids=["approval_denied", "invalid_tool_arguments", "arguments_too_large", "arguments_too_long_to_show",
        "nonexistent_tool", "resume_required", "result_too_large", "call_timeout"])
def test_a_call_that_brought_no_server_text_does_not_taint(tmp_path, events_before):
    store, project, root, manager = make_workspace(tmp_path)
    _, sources = journal_of(store, manager, project, [
        *events_before, ("message", {"role": "assistant", "content": "Carrying on with gannetquill."})])
    after = sources[-1]
    assert "origin_evidence" not in after["metadata"] and chunks_of(store, after["id"])
    assert not any(source["metadata"].get("third_party_result") for source in sources)
    assert Retriever(store, store.settings).search(project["id"], "gannetquill")


def test_a_tool_error_with_server_text_taints(tmp_path):
    store, project, root, manager = make_workspace(tmp_path)
    error = {"ok": False, "error_code": "mcp_tool_error", "is_error": True,
             "content": [{"type": "text", "text": "server says no"}], "third_party_untrusted": True}
    _, sources = journal_of(store, manager, project, [
        ("action_completed", {"action_id": "a", "name": "mcp_tracker_fetch", "tool_call_id": "c", "result": error}),
        ("message", {"role": "assistant", "content": "It said no, pelicanquill."})])
    assert sources[0]["metadata"]["third_party_result"] is True
    assert sources[1]["metadata"]["origin_evidence"] == "degraded"


# Item 4: the retry without third-party tools ------------------------------------------------------------------

def test_provider_errors_name_the_tool_definitions_only_when_they_do():
    openai = {"error": {"type": "invalid_request_error", "param": "tools[3].parameters",
                        "message": "Invalid schema for function 'mcp_x_y': schema must be a JSON Schema"}}
    anthropic = {"type": "error", "error": {"type": "invalid_request_error",
                                            "message": "tools.2.custom.input_schema: JSON schema is invalid"}}
    assert _error_code(400, openai) == _error_code(400, anthropic) == "tools_rejected"
    assert _error_code(400, {"error": "registry/x does not support tools"}) == "provider_error"
    assert _error_code(400, {"error": {"message": "messages: roles must alternate"}}) == "provider_error"
    assert _error_code(400, {"error": {"code": "context_length_exceeded"}}) == "context_limit"


def picky(errors):
    """A provider that refuses every request offering third-party tools with the given errors in turn."""
    def respond(messages, tools):
        if any(tool["name"].startswith("mcp_") for tool in tools) and errors:
            return errors.pop(0)
        return reply(text="Done.")
    return respond


@pytest.mark.asyncio
@pytest.mark.parametrize("code,named", [("tools_rejected", True), ("provider_error", False)])
async def test_a_refusal_that_succeeds_without_the_tools_withdraws_them(setup, code, named):
    memory = Memory()
    provider = ScriptedProvider(reply(call("resume_context")), reply(call("ack_resume", memory.ack_arguments())),
                                picky([ProviderError(code)]), picky([]))
    engine = agent(setup, provider, memory)
    try:
        assert (await engine.turn("Work"))["status"] == "completed"
        assert [row["data"] for row in events(engine, "mcp_tools_withdrawn")] == [
            {"code": code, "servers": ["files"], "named_by_provider": named}]
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_a_refusal_that_repeats_without_the_tools_keeps_them_and_reports_the_original(setup):
    memory = Memory()
    seen = []
    provider = ScriptedProvider(reply(call("resume_context")), reply(call("ack_resume", memory.ack_arguments())),
                                ProviderError("provider_error"), ProviderError("unsupported_request"),
                                *acked(Memory()))
    engine = agent(setup, provider, memory, on_event=seen.append)
    try:
        result = await engine.turn("Work")
        assert result["status"] == "error" and result["error_code"] == "provider_error"
        assert events(engine, "mcp_tools_withdrawn") == []
        assert [row["data"] for row in events(engine, "mcp_tools_kept")] == [
            {"code": "provider_error", "servers": ["files"]}]
        assert "mcp_files_echo" in engine._schemas
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["context_limit", "rate_limited"])
async def test_other_refusals_and_shown_text_are_never_retried(setup, error):
    memory = Memory()
    provider = ScriptedProvider(reply(call("resume_context")), reply(call("ack_resume", memory.ack_arguments())),
                                ProviderError(error))
    engine = agent(setup, provider, memory)
    try:
        assert (await engine.turn("Work"))["status"] in {"error", "context_limit"}
        assert len(provider.requests) == 3 and events(engine, "mcp_tools_withdrawn", "mcp_tools_kept") == []
        assert "mcp_files_echo" in engine._schemas
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_a_refusal_after_text_was_shown_is_not_retried(setup):
    memory = Memory()

    async def partial(messages, tools):
        await engine._delta("Half an answer ")
        return ProviderError("provider_error")
    provider = ScriptedProvider(reply(call("resume_context")), reply(call("ack_resume", memory.ack_arguments())),
                                partial)
    engine = agent(setup, provider, memory)
    try:
        assert (await engine.turn("Work"))["status"] == "error"
        assert len(provider.requests) == 3 and events(engine, "mcp_tools_withdrawn", "mcp_tools_kept") == []
    finally:
        await engine.close()


# Item 5: the cost of checking arguments -----------------------------------------------------------------------

def test_schemas_that_make_checking_expensive_are_refused():
    reviewer = {"type": "object", "properties": {"p": {"type": "array", "items": {"anyOf": [
        {"anyOf": [{"anyOf": [{"enum": [0]}] * 8}] * 8}] * 8 + [{"type": "integer"}]}}}}
    assert admit(reviewer) == (None, "schema_unsupported")
    nested_few = {"type": "object", "properties": {"p": {"anyOf": [
        {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "null"}]}}, {"type": "integer"}]}}}
    assert admit(nested_few) == (None, "schema_unsupported")  # four branches, but one anyOf inside another
    many ={"type": "object", "properties": {f"p{i}": {"anyOf": [{"type": "string"}] * 6} for i in range(3)}}
    assert admit(many) == (None, "schema_unsupported")  # 18 branches in all
    enums = {"type": "object", "properties": {f"p{i}": {"enum": list(range(64))} for i in range(5)}}
    assert admit(enums) == (None, "schema_unsupported")  # 320 enum values
    wide = {"type": "object", "properties": {f"p{i}": {"type": "array", "items": {"type": "string"}}
                                             for i in range(64)}}
    assert admit(wide) == (None, "schema_unsupported")  # 129 nodes
    allowed = {"type": "object", "properties": {"p": {"type": "array", "items": {"anyOf": [
        {"enum": list(range(1000 + 32 * b, 1032 + 32 * b))} for b in range(8)]}}}}
    schema, reason = admit(allowed)
    assert reason is None
    worst = {"p": [1255] * 740}  # every element passes only the last branch
    assert mcp_schema.affordable(schema, worst) and not mcp_schema.affordable(schema, {"p": [1255] * 760})
    from jsonschema import Draft202012Validator
    started = time.perf_counter()
    assert Draft202012Validator(schema).is_valid(worst)
    assert time.perf_counter() - started < 1


@pytest.mark.asyncio
async def test_a_costly_argument_is_refused_and_a_timeout_withdraws_the_tool(setup, monkeypatch):
    memory = Memory()
    threads = []

    from jsonschema import Draft202012Validator

    class Slow:
        check_schema = staticmethod(Draft202012Validator.check_schema)  # admission is unchanged

        def __init__(self, schema):
            pass

        def is_valid(self, arguments):
            threads.append(threading.current_thread().name)
            time.sleep(1)
            return True
    provider = ScriptedProvider(*acked(memory, call("mcp_files_anything", {"ids": list(range(3000))}, "c0"),
                                       call("mcp_files_anything", {"x": 1}, "c1"),
                                       call("mcp_files_anything", {"x": 2}, "c2")))
    engine = agent(setup, provider, memory, servers=[server("permissive")], approve=lambda request: True)
    monkeypatch.setattr(mcp_schema, "MAX_COST", 2000)
    monkeypatch.setattr(mcp_schema, "VALIDATION_SECONDS", .2)
    monkeypatch.setattr(mcp_schema, "Draft202012Validator", Slow)
    try:
        await engine.turn("Call")
        assert results(engine)["c0"]["error_code"] == "arguments_too_costly"
        assert results(engine)["c1"]["error_code"] == "tool_withdrawn"
        assert results(engine)["c2"]["error_code"] == "invalid_tool_call"  # not offered, not evaluated again
        assert [row["data"]["reason"] for row in events(engine, "mcp_tool_withdrawn")] == ["validation_timeout"]
        assert "mcp_files_anything" not in names(provider.requests[-1])
        assert threads == ["cairnmind-mcp-validate_0"]
    finally:
        await engine.close()


# Item 6: a spawn is never lost --------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_two_cancellations_during_a_spawn_lose_no_process(tmp_path, monkeypatch):
    real = asyncio.create_subprocess_exec

    async def slow(*args, **kwargs):
        process = await real(*args, **kwargs)
        await asyncio.sleep(1)  # the process runs; the spawn has not returned yet
        return process
    monkeypatch.setattr(mcp_host.asyncio, "create_subprocess_exec", slow)
    (tmp_path / "worktree").mkdir()
    host = McpHost([{"id": "files", "command": sys.executable, "args": [MCP_FIXTURE, "stubborn"], "env": {}}],
                   worktree=tmp_path / "worktree")
    task = asyncio.create_task(host.start())
    deadline = time.monotonic() + 10
    while not mcp_fixture_processes() and time.monotonic() < deadline:
        await asyncio.sleep(.02)
    task.cancel()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()  # a second interrupt, in a later loop iteration
    with pytest.raises(asyncio.CancelledError):
        await task
    await host.close()
    assert mcp_fixture_processes() == []


# Item 7: the wording of the mcp: origin ----------------------------------------------------------------------

def test_records_and_checkpoints_after_a_third_party_result_have_their_own_wording(tmp_path):
    store, project, root, manager = make_workspace(tmp_path)
    note = store.snapshot(project["id"], "note://x", "a note", "manual")
    with store.write_origin(MARK):
        third = store.remember(project["id"], {"kind": "fact", "content": "Said after a tool result"}, [note["id"]])
        store.checkpoint(project["id"], {"summary": "Saved after a tool result"})
    external_mark = {**MARK, "origin_client": "external", "origin_history": "unarchived"}
    with store.write_origin(external_mark):
        external = store.remember(project["id"], {"kind": "fact", "content": "Said by an external editor"},
                                  [note["id"]])
    rows = store.query("SELECT FROM records WHERE id IN :ids", {"ids": [third["id"], external["id"]]})
    states = evidence_states(store, project["id"], rows)
    assert states[third["id"]]["degraded_by"] == "third_party_tool" and "degraded_by" not in states[external["id"]]
    text = orphans_report(store, project, root, rows)["text"]
    assert f"{THIRD_PARTY_WRITTEN}; never verified (1)" in text
    assert "external client's session; never verified (1)" in text
    lines = [line["text"] for line in build_brief(store, project["id"])["lines"]]
    assert ("It was saved by the built-in agent after a third-party tool result in its session (evidence "
            "degraded); nothing in it is verified.") in lines
    assert not any("external client" in line for line in lines)


def test_a_packet_names_the_cause_of_an_unverified_plan_or_task():
    base = {"session_id": "s", "epoch": 1, "id": "p", "hash": "h", "project_id": "pr", "worktree": "/w",
            "history": "archived"}
    third = format_packet({**base, "active_plan": {"id": "pl", "version": 1, "title": "T", **MARK},
                           "active_task": None})
    assert "written after a third-party tool result in a built-in agent session and is not verified" in third
    assert "[written after a third-party tool result in a built-in agent session; not verified]" in third
    assert "external client" not in third
    external = format_packet({**base, "active_plan": {"id": "pl", "version": 1, "title": "T",
                                                      **MARK, "origin_client": "external"}, "active_task": None})
    assert "written by an external client's session and is not verified" in external
    assert "[written by an external client's session; not verified]" in external


def test_impact_names_the_cause_of_a_degraded_record(changed, monkeypatch):
    import cairnmind.impact as impact_module
    store, pid, root, records = changed
    original = impact_module.evidence_states

    def degraded(store_, project_id, rows, **options):
        states = original(store_, project_id, rows, **options)
        for record_id in states:
            states[record_id].update(evidence_state="degraded", degraded_by="third_party_tool")
        return states
    monkeypatch.setattr(impact_module, "evidence_states", degraded)
    (root / "docs" / "login.md").write_text("# Login\n\nChanged.\n")  # the doc record rests on this file
    report = build_impact(store, pid, "HEAD")
    assert "written after a third-party tool result in a built-in agent session, or resting only on such text, " \
           "never verified" in report["text"]
    assert entry(report, records["doc"])["degraded_by"] == "third_party_tool"
    assert "external client" not in report["text"]
