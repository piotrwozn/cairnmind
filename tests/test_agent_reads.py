"""A whole-file write needs the whole current file in the model's context (#157)."""
from __future__ import annotations

import hashlib
import json

from cairnmind.agent_reads import ReadLedger
from cairnmind.api_agent import TOOL_CONTEXT_CHARS

from test_api_agent import Memory, ScriptedProvider, call, engine, reply, tools_results
from test_api_agent import setup as setup  # noqa: F401  (a fixture)


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def read_result(path, text, start=1, lines=None, **extra):
    rows = text.splitlines(keepends=True)
    chosen = rows[start - 1:] if lines is None else rows[start - 1:start - 1 + lines]
    return {"ok": True, "path": path, "content": "".join(chosen), "sha256": digest(text), "bytes": len(text),
            "start_line": start, "end_line": start - 1 + len(chosen), "total_lines": len(rows), "truncated": False,
            **extra}


TEXT = "".join(f"line {n}\n" for n in range(1, 11))


def test_whole_read_allows_replacement_and_its_hash_only():
    ledger = ReadLedger()
    ledger.observe("read_file", read_result("a.md", TEXT))
    assert ledger.refusal("a.md", digest(TEXT)) is None
    assert ledger.refusal("./a.md", digest(TEXT)) is None
    assert ledger.refusal("a.md", digest("other"))["error_code"] == "full_read_required"
    assert ledger.refusal("b.md", digest(TEXT))["error_code"] == "full_read_required"


def test_an_exclusive_create_and_an_invalid_path_are_left_to_the_executor():
    ledger = ReadLedger()
    assert ledger.refusal("new.md", None) is None
    assert ledger.refusal("../outside", digest(TEXT)) is None


def test_a_slice_is_refused_with_the_lines_not_seen():
    ledger = ReadLedger()
    ledger.observe("read_file", read_result("a.md", TEXT, 1, 4))
    ledger.observe("read_file", read_result("a.md", TEXT, 7, 2))
    refusal = ledger.refusal("a.md", digest(TEXT))
    assert refusal["error_code"] == "full_read_required" and "apply_patch" in refusal["message"]
    assert refusal["total_lines"] == 10 and refusal["unread_lines"] == [[5, 6], [9, 10]]
    ledger.observe("read_file", read_result("a.md", TEXT, 5, 2))
    ledger.observe("read_file", read_result("a.md", TEXT, 9))
    assert ledger.refusal("a.md", digest(TEXT)) is None


def test_only_lines_delivered_whole_count():
    ledger = ReadLedger()
    cut = read_result("a.md", TEXT)
    cut.update(content=cut["content"][:-3], end_line=9)  # ends inside "line 10"
    ledger.observe("read_file", cut)
    assert ledger.refusal("a.md", digest(TEXT))["unread_lines"] == [[10, 10]]
    last = "one\ntwo"  # the last line of a file needs no line end
    ledger.observe("read_file", read_result("b.md", last))
    assert ledger.refusal("b.md", digest(last)) is None
    claimed = read_result("c.md", TEXT, 1, 2)
    claimed["end_line"] = 10  # more lines claimed than the content holds
    ledger.observe("read_file", claimed)
    assert ledger.refusal("c.md", digest(TEXT))["error_code"] == "full_read_required"
    empty = read_result("d.md", "")
    ledger.observe("read_file", empty)
    assert empty["end_line"] == 0 and ledger.refusal("d.md", digest("")) is None


def test_cut_masked_or_failed_reads_never_count():
    ledger = ReadLedger()
    ledger.observe("read_file", read_result("a.md", TEXT, host_context_truncated=True))
    ledger.observe("read_file", {**read_result("b.md", TEXT), "content": "[REDACTED]\n" * 10}, masked=True)
    ledger.observe("read_file", {**read_result("c.md", TEXT), "ok": False})
    ledger.observe("read_file", read_result("d.md", TEXT, masked=True))  # the executor's own mark
    for path in ("a.md", "b.md", "c.md", "d.md"):
        assert ledger.refusal(path, digest(TEXT))["error_code"] == "full_read_required"
        assert ledger.refusal(path, digest(TEXT), "delete_file")["error_code"] == "full_read_required"


def test_text_that_only_looks_like_the_mask_still_counts():
    # #157 review: a file whose own text holds the mask's words could never be replaced whole.
    ledger, notes = ReadLedger(), "Logs show [REDACTED] where a key was.\n"
    ledger.observe("read_file", read_result("notes.md", notes))
    assert ledger.refusal("notes.md", digest(notes)) is None


def test_a_delete_needs_the_same_whole_read_as_a_replacement():
    ledger = ReadLedger()
    ledger.observe("read_file", read_result("a.md", TEXT, 1, 4))
    refusal = ledger.refusal("a.md", digest(TEXT), "delete_file")
    assert refusal["error_code"] == "full_read_required" and "delete_file" in refusal["message"]
    assert refusal["unread_lines"] == [[5, 10]]
    ledger.observe("read_file", read_result("a.md", TEXT, 5))
    assert ledger.refusal("a.md", digest(TEXT), "delete_file") is None
    ledger.observe("write_file", {"ok": True, "path": "b.md", "sha256": digest("mine"), "previous_sha256": None})
    assert ledger.refusal("b.md", digest("mine"), "delete_file") is None


def test_own_writes_count_and_a_patch_keeps_only_what_was_whole():
    ledger = ReadLedger()
    ledger.observe("write_file", {"ok": True, "path": "a.md", "sha256": digest("mine"), "previous_sha256": None})
    assert ledger.refusal("a.md", digest("mine")) is None
    ledger.observe("apply_patch", {"ok": True, "path": "a.md", "sha256": digest("patched"),
                                   "previous_sha256": digest("mine")})
    assert ledger.refusal("a.md", digest("patched")) is None
    ledger.observe("read_file", read_result("b.md", TEXT, 1, 2))
    ledger.observe("apply_patch", {"ok": True, "path": "b.md", "sha256": digest("b2"),
                                   "previous_sha256": digest(TEXT)})
    assert ledger.refusal("b.md", digest("b2"))["error_code"] == "full_read_required"
    ledger.observe("delete_file", {"ok": True, "path": "a.md", "deleted": True})
    assert ledger.refusal("a.md", digest("patched"))["error_code"] == "full_read_required"
    ledger.observe("read_file", read_result("c.md", TEXT))
    ledger.clear()
    assert ledger.refusal("c.md", digest(TEXT))["error_code"] == "full_read_required"


README = "".join(f"Line {n:03d} of the project README, with enough words to make a long document. {'x' * 60}\n"
                 for n in range(1, 201))


def last_tool(messages):
    return json.loads([row for row in messages if row["role"] == "tool"][-1]["content"])


def write_back_what_was_seen(messages, tools):
    seen = last_tool(messages)
    return reply(call("write_file", {"path": "README.md", "content": seen["content"] + "<!-- note -->\n",
                                     "expected_sha256": seen["sha256"]}))


async def test_a_read_cut_for_the_context_cannot_be_written_back_over_the_whole_file(setup):
    memory = Memory()
    readme = setup[3] / "README.md"
    readme.write_text(README)
    assert len(README) > TOOL_CONTEXT_CHARS
    provider = ScriptedProvider(
        reply(call("resume_context")), reply(call("ack_resume", memory.ack_arguments())),
        reply(call("read_file", {"path": "README.md"})), write_back_what_was_seen, reply(text="Done."))
    agent = engine(setup, provider, memory)
    result = await agent.turn("Add one comment line to README.md")
    assert result["status"] == "completed"
    assert readme.read_text() == README
    seen = last_tool(provider.requests[3]["messages"])
    assert seen["host_context_truncated"] is True and seen["truncated"] is True
    refused = tools_results(agent)[-1]
    assert refused["error_code"] == "full_read_required" and "apply_patch" in refused["message"]
    # The refusal is a tool result, so the person's per-call approval path never ran.
    assert not [row for row in agent.journal.events() if row["type"] == "approval_requested"]


async def test_slices_that_each_arrive_whole_allow_a_full_rewrite(setup):
    memory = Memory()
    readme = setup[3] / "README.md"
    readme.write_text(README)
    slices = [reply(call("read_file", {"path": "README.md", "start_line": first, "max_lines": 80}))
              for first in range(1, 201, 80)]
    rewritten = README.replace("Line 001", "Line one")

    def rewrite(messages, tools):
        return reply(call("write_file", {"path": "README.md", "content": rewritten,
                                         "expected_sha256": last_tool(messages)["sha256"]}))

    provider = ScriptedProvider(
        reply(call("resume_context")), reply(call("ack_resume", memory.ack_arguments())),
        *slices, rewrite, reply(text="Done."))
    agent = engine(setup, provider, memory)
    result = await agent.turn("Rename the first line")
    assert result["status"] == "completed"
    delivered = [json.loads(row["content"]) for row in provider.requests[-1]["messages"] if row["role"] == "tool"]
    assert len(delivered) == 6 and not any(row.get("host_context_truncated") for row in delivered)
    assert tools_results(agent)[-1]["ok"] is True
    assert readme.read_text() == rewritten


async def test_a_small_file_read_whole_is_replaced_as_before(setup):
    memory = Memory()
    target = setup[3] / "app.py"
    target.write_text("VALUE = 1\n")

    def write(messages, tools):
        return reply(call("write_file", {"path": "app.py", "content": "VALUE = 2\n",
                                         "expected_sha256": last_tool(messages)["sha256"]}))

    def again(messages, tools):
        return reply(call("write_file", {"path": "app.py", "content": "VALUE = 3\n",
                                         "expected_sha256": last_tool(messages)["sha256"]}))

    provider = ScriptedProvider(
        reply(call("resume_context")), reply(call("ack_resume", memory.ack_arguments())),
        reply(call("read_file", {"path": "app.py"})), write, again, reply(text="Done."))
    agent = engine(setup, provider, memory)
    assert (await agent.turn("Set VALUE"))["status"] == "completed"
    assert target.read_text() == "VALUE = 3\n"


async def test_a_cut_read_cannot_delete_the_file_and_create_it_from_the_part_seen(setup):
    # #157 review: delete_file then an exclusive create shrank a 27200-char README to 4011, with no approval.
    memory = Memory()
    readme = setup[3] / "README.md"
    readme.write_text(README)
    seen = {}

    def delete(messages, tools):
        seen.update(last_tool(messages))
        return reply(call("delete_file", {"path": "README.md", "expected_sha256": seen["sha256"]}))

    def create(messages, tools):
        return reply(call("write_file", {"path": "README.md", "content": seen["content"], "expected_sha256": None}))

    provider = ScriptedProvider(
        reply(call("resume_context")), reply(call("ack_resume", memory.ack_arguments())),
        reply(call("read_file", {"path": "README.md"})), delete, create, reply(text="Done."))
    agent = engine(setup, provider, memory)
    assert (await agent.turn("Shorten README.md"))["status"] == "completed"
    assert seen["host_context_truncated"] is True
    assert readme.read_text() == README
    refused, create_result = tools_results(agent)[-2:]
    assert refused["error_code"] == "full_read_required" and "delete_file" in refused["message"]
    assert create_result["ok"] is False  # the file is still there, so the exclusive create fails
    assert not [row for row in agent.journal.events() if row["type"] == "approval_requested"]


async def test_a_file_whose_text_holds_the_mask_words_is_replaced_after_a_whole_read(setup):
    memory = Memory()
    notes = setup[3] / "notes.md"
    notes.write_text("Logs show [REDACTED] where a key was.\n")

    def write(messages, tools):
        return reply(call("write_file", {"path": "notes.md", "content": "Rewritten.\n",
                                         "expected_sha256": last_tool(messages)["sha256"]}))

    provider = ScriptedProvider(
        reply(call("resume_context")), reply(call("ack_resume", memory.ack_arguments())),
        reply(call("read_file", {"path": "notes.md"})), write, reply(text="Done."))
    agent = engine(setup, provider, memory)
    assert (await agent.turn("Rewrite notes.md"))["status"] == "completed"
    assert notes.read_text() == "Rewritten.\n"


async def test_a_registered_secret_masked_in_a_read_still_blocks_a_whole_write(setup):
    memory = Memory()
    config = setup[3] / "config.txt"
    config.write_text("token = sk-sensitive-file\n")

    def write(messages, tools):
        seen = last_tool(messages)
        return reply(call("write_file", {"path": "config.txt", "content": seen["content"],
                                         "expected_sha256": seen["sha256"]}))

    provider = ScriptedProvider(
        reply(call("resume_context")), reply(call("ack_resume", memory.ack_arguments())),
        reply(call("read_file", {"path": "config.txt"})), write, reply(text="Done."))
    agent = engine(setup, provider, memory, key="sk-sensitive-file")
    assert (await agent.turn("Copy config.txt"))["status"] == "completed"
    assert config.read_text() == "token = sk-sensitive-file\n"
    assert tools_results(agent)[-1]["error_code"] == "full_read_required"
