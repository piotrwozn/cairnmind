"""Fast-tier edge-case and error-handling coverage tests for cairnmind.agents.agent_tools."""
from __future__ import annotations

import hashlib
import sys
from types import SimpleNamespace

import pytest

from cairnmind.agents import agent_tools
from cairnmind.agents.agent_journal import AgentJournal, JournalError
from cairnmind.agents.agent_tools import ToolError, ToolExecutor
from cairnmind.config import Settings


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def ex(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    settings = Settings.load(tmp_path / "data", create=True)
    return ToolExecutor(
        settings,
        root,
        session_id="sess-edge",
        run_id="run-edge",
        generation=1,
        journal=AgentJournal(settings, "sess-edge"),
    )


def test_journal_replay_error_marks_changes_incomplete(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    settings = Settings.load(tmp_path / "data", create=True)
    bad_journal = SimpleNamespace(
        events=lambda: (_ for _ in ()).throw(JournalError("corrupt")),
        append=lambda *a, **k: None,
        redact=lambda v: v,
    )
    te = ToolExecutor(settings, root, session_id="s", run_id="r", generation=1, journal=bad_journal)
    assert te.changes.complete is False


@pytest.mark.asyncio
async def test_read_list_search_write_and_patch_validation_edges(ex, monkeypatch):
    # Binary file read refused
    (ex.worktree / "bin.dat").write_bytes(b"hello\x00world")
    res_bin = await ex.execute("read_file", {"path": "bin.dat"})
    assert not res_bin["ok"] and res_bin["error_code"] == "binary_file"

    # Oversized file read refused
    (ex.worktree / "huge.txt").write_text("x" * 64, encoding="utf-8")
    monkeypatch.setattr(agent_tools, "MAX_FILE_BYTES", 16)
    res_big = await ex.execute("read_file", {"path": "huge.txt"})
    assert not res_big["ok"] and res_big["error_code"] == "file_too_large"
    # Oversized write content refused
    res_wbig = await ex.execute("write_file", {"path": "new.txt", "content": "y" * 32, "expected_sha256": None})
    assert not res_wbig["ok"] and res_wbig["error_code"] == "file_too_large"
    monkeypatch.setattr(agent_tools, "MAX_FILE_BYTES", 512 * 1024)

    # Invalid sha256 format on write_file & apply_patch
    res_bad_sha = await ex.execute("write_file", {"path": "huge.txt", "content": "ok", "expected_sha256": "not-a-hex"})
    assert not res_bad_sha["ok"] and res_bad_sha["error_code"] == "invalid_arguments"
    res_bad_sha2 = await ex.execute("apply_patch", {"path": "huge.txt", "expected_sha256": "zzz", "old_text": "x", "new_text": "y"})
    assert not res_bad_sha2["ok"] and res_bad_sha2["error_code"] == "invalid_arguments"

    # list_files: non-bool recursive, limit truncation, MAX_WALK_ENTRIES truncation
    res_bad_rec = await ex.execute("list_files", {"recursive": "yes"})
    assert not res_bad_rec["ok"] and res_bad_rec["error_code"] == "invalid_arguments"

    (ex.worktree / "a.txt").write_text("alpha\nbeta\n", encoding="utf-8", newline="\n")
    (ex.worktree / "b.txt").write_text("beta\ngamma\n", encoding="utf-8", newline="\n")
    (ex.worktree / "c.txt").write_text("delta\n", encoding="utf-8", newline="\n")
    res_lim = await ex.execute("list_files", {"limit": 2})
    assert res_lim["ok"] and len(res_lim["files"]) == 2 and res_lim["truncated"] is True

    monkeypatch.setattr(agent_tools, "MAX_WALK_ENTRIES", 1)
    res_walk_cap = await ex.execute("list_files", {"recursive": True})
    assert res_walk_cap["ok"] and res_walk_cap["truncated"] is True
    monkeypatch.setattr(agent_tools, "MAX_WALK_ENTRIES", 10000)

    # search_files: invalid query, non-bool case_sensitive, single file path, case-insensitive, max_results, MAX_SEARCH_BYTES, binary skip
    assert (await ex.execute("search_files", {"query": ""}))["error_code"] == "invalid_arguments"
    assert (await ex.execute("search_files", {"query": "a", "case_sensitive": "no"}))["error_code"] == "invalid_arguments"

    res_single = await ex.execute("search_files", {"path": "a.txt", "query": "ALPHA", "case_sensitive": False})
    assert res_single["ok"] and len(res_single["matches"]) == 1

    res_max = await ex.execute("search_files", {"query": "beta", "max_results": 1})
    assert res_max["ok"] and len(res_max["matches"]) == 1 and res_max["truncated"] is True

    monkeypatch.setattr(agent_tools, "MAX_SEARCH_BYTES", 1)
    res_bytes_cap = await ex.execute("search_files", {"query": "alpha"})
    assert res_bytes_cap["ok"] and res_bytes_cap["truncated"] is True
    monkeypatch.setattr(agent_tools, "MAX_SEARCH_BYTES", 20 * 1024 * 1024)

    # apply_patch ambiguous / not found
    res_amb = await ex.execute("apply_patch", {
        "path": "a.txt", "expected_sha256": _sha("alpha\nbeta\n"), "old_text": "missing", "new_text": "x"
    })
    assert not res_amb["ok"] and res_amb["error_code"] == "patch_ambiguous"

    # _approve_write paths on protected execution-config file (e.g. pyproject.toml)
    cfg_file = ex.worktree / "pyproject.toml"
    cfg_file.write_text("[project]\nname = 'a'\n", encoding="utf-8", newline="\n")
    ex.approve = lambda _req: True
    res_exists = await ex.execute("write_file", {"path": "pyproject.toml", "content": "x", "expected_sha256": None})
    assert not res_exists["ok"]
    res_amb_cfg = await ex.execute("apply_patch", {
        "path": "pyproject.toml",
        "expected_sha256": _sha("[project]\nname = 'a'\n"),
        "old_text": "nope",
        "new_text": "y",
    })
    assert not res_amb_cfg["ok"] and res_amb_cfg["error_code"] == "patch_ambiguous"


@pytest.mark.asyncio
async def test_command_validation_env_error_and_pre_exec_interrupt(ex, monkeypatch):
    # Invalid command & invalid timeout & missing sys.executable
    assert (await ex.execute("run_command", {"command": "   "}))["error_code"] == "invalid_arguments"
    assert (await ex.execute("run_command", {"command": "echo hi\x00"}))["error_code"] == "invalid_arguments"
    assert (await ex.execute("run_command", {"command": "echo hi", "timeout_seconds": 0.01}))["error_code"] == "invalid_arguments"

    monkeypatch.setattr(sys, "executable", "")
    assert (await ex.execute("run_command", {"command": "echo hi"}))["error_code"] == "command_unavailable"
    monkeypatch.undo()

    # Interrupt occurring during approval callback stops command before execution
    async def approve_then_interrupt(_req):
        ex._interrupt_epoch += 1
        return True

    ex.approve = approve_then_interrupt
    res_int = await ex.execute("run_command", {"command": "echo hi"})
    assert not res_int["ok"] and res_int["error_code"] == "command_interrupted"

    # Corrupt CockpitState refused by _clean_environment
    (ex.settings.data_dir / "cockpit").mkdir(parents=True, exist_ok=True)
    (ex.settings.data_dir / "cockpit" / "preferences.json").write_text("{corrupt", encoding="utf-8")
    with pytest.raises(ToolError) as exc:
        ex._clean_environment()
    assert exc.value.code == "configuration_error"

    # Unknown tool & non-dict args
    assert (await ex.execute("no_such_tool", {}))["error_code"] == "invalid_tool"
    assert (await ex.execute("read_file", "bad"))["error_code"] == "invalid_tool"
