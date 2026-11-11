"""Real scoped filesystem, shell approval and cancellation regression tests."""
import asyncio
import hashlib
from pathlib import Path
import stat
import time

import pytest

from cairnmind.agent_journal import AgentJournal, JournalError
from cairnmind.agent_tools import MAX_COMMAND_OUTPUT_BYTES, ToolExecutor
from cairnmind.config import Settings


@pytest.fixture
def executor(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    settings = Settings.load(tmp_path / "data", create=True)
    return ToolExecutor(settings, root, session_id="session", run_id="run", generation=3,
                        journal=AgentJournal(settings, "session"))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def live(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] not in {"Z", "X"}
    except (OSError, IndexError):
        return False


async def wait_for(path):
    deadline = time.monotonic() + 3
    while not path.exists() and time.monotonic() < deadline:
        await asyncio.sleep(.01)
    assert path.exists()


async def test_create_read_patch_and_delete_record_effect_before_result(executor):
    executor.current_tool_call_id = "call-create"
    result = await executor.execute("write_file", {"path": "src/main.py", "content": "print('hello')\n",
                                                   "expected_sha256": None})
    assert result["ok"] and result["created"]
    file = executor.worktree / "src/main.py"
    assert file.read_text() == "print('hello')\n"
    events = executor.journal.events()
    assert [row["type"] for row in events] == ["action_started", "action_completed"]
    assert events[0]["data"]["action_id"] == events[1]["data"]["action_id"]
    assert all(row["data"]["tool_call_id"] == "call-create" for row in events)
    read = await executor.execute("read_file", {"path": "src/main.py"})
    assert read["sha256"] == digest("print('hello')\n") and not read["truncated"]
    patched = await executor.execute("apply_patch", {"path": "src/main.py", "expected_sha256": read["sha256"],
                                                     "old_text": "hello", "new_text": "world"})
    assert patched["ok"] and file.read_text() == "print('world')\n"
    deleted = await executor.execute("delete_file", {"path": "src/main.py", "expected_sha256": patched["sha256"]})
    assert deleted["ok"] and not file.exists() and not executor.journal.pending_actions()


async def test_existing_file_requires_exact_hash_and_preserves_mode(executor):
    file = executor.worktree / "run.sh"
    file.write_text("old")
    file.chmod(0o751)
    bad = await executor.execute("write_file", {"path": "run.sh", "content": "bad", "expected_sha256": "0" * 64})
    assert not bad["ok"] and bad["error_code"] == "file_changed" and file.read_text() == "old"
    good = await executor.execute("write_file", {"path": "run.sh", "content": "new", "expected_sha256": digest("old")})
    assert good["ok"] and stat.S_IMODE(file.stat().st_mode) == 0o751
    assert not list(executor.worktree.glob(".cairnmind-api-*"))


async def test_exclusive_create_never_overwrites_existing_file(executor):
    file = executor.worktree / "existing"
    file.write_text("user edit")
    result = await executor.execute("write_file", {"path": "existing", "content": "lost", "expected_sha256": None})
    assert not result["ok"] and result["error_code"] == "file_exists"
    assert file.read_text() == "user edit"


async def test_apply_patch_refuses_ambiguous_or_missing_passage(executor):
    file = executor.worktree / "file"
    file.write_text("same same")
    for old in ("same", "absent"):
        result = await executor.execute("apply_patch", {"path": "file", "expected_sha256": digest("same same"),
                                                         "old_text": old, "new_text": "new"})
        assert result["error_code"] == "patch_ambiguous"
    assert file.read_text() == "same same"


@pytest.mark.parametrize("path", ["../outside", "/tmp/absolute", "src/../../outside", "C:\\outside", ".git/config",
                                 ".memory/devbrain/manifest.json", "sub/.git/index"])
async def test_file_tools_refuse_escaping_and_protected_paths(executor, path):
    result = await executor.execute("write_file", {"path": path, "content": "bad", "expected_sha256": None})
    assert not result["ok"] and result["error_code"] in {"outside_worktree", "protected_path"}
    assert not executor.journal.pending_actions()


async def test_symlink_file_and_directory_are_not_followed(executor, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret"
    secret.write_text("private")
    (executor.worktree / "link").symlink_to(outside, target_is_directory=True)
    (executor.worktree / "file-link").symlink_to(secret)
    for name, args in [("read_file", {"path": "link/secret"}), ("read_file", {"path": "file-link"}),
                       ("write_file", {"path": "link/new", "content": "bad", "expected_sha256": None}),
                       ("delete_file", {"path": "file-link", "expected_sha256": digest("private")})]:
        assert not (await executor.execute(name, args))["ok"]
    assert secret.read_text() == "private" and not (outside / "new").exists()
    listed = await executor.execute("list_files", {"recursive": True})
    assert {row["type"] for row in listed["files"]} == {"symlink"}
    searched = await executor.execute("search_files", {"query": "private"})
    assert searched["matches"] == []


async def test_managed_snapshot_is_excluded_but_other_memory_is_normal(executor):
    (executor.worktree / ".memory/devbrain").mkdir(parents=True)
    (executor.worktree / ".memory/devbrain/manifest.json").write_text("needle snapshot")
    (executor.worktree / ".memory/notes.txt").write_text("needle normal")
    result = await executor.execute("search_files", {"query": "needle"})
    assert [row["path"] for row in result["matches"]] == [".memory/notes.txt"]
    result = await executor.execute("write_file", {"path": ".memory/extra", "content": "normal", "expected_sha256": None})
    assert result["ok"]


async def test_line_slice_still_returns_hash_of_complete_file(executor):
    file = executor.worktree / "lines"
    file.write_text("one\ntwo\nthree\n")
    result = await executor.execute("read_file", {"path": "lines", "start_line": 2, "max_lines": 1})
    assert result["content"] == "two\n" and result["sha256"] == digest(file.read_text())
    assert result["truncated"] and result["total_lines"] == 3


async def test_search_literal_not_regex_and_bounded_results(executor):
    file = executor.worktree / "data.txt"
    file.write_text("[literal]\nLITERAL\n[literal]\n")
    result = await executor.execute("search_files", {"query": "[literal]", "max_results": 1})
    assert result["matches"][0]["line"] == 1 and result["truncated"]
    result = await executor.execute("search_files", {"path": "data.txt", "query": "literal", "case_sensitive": False})
    assert len(result["matches"]) == 3


async def test_invalid_arguments_cannot_smuggle_command_approval(executor):
    result = await executor.execute("run_command", {"command": "touch forbidden", "approved": True})
    assert result["error_code"] == "invalid_arguments" and not (executor.worktree / "forbidden").exists()


async def test_no_callback_or_denied_approval_never_starts_command(executor):
    args = {"command": "touch forbidden"}
    assert (await executor.execute("run_command", args))["error_code"] == "approval_required"
    executor.approve = lambda request: False
    assert (await executor.execute("run_command", args))["error_code"] == "approval_denied"
    executor.approve = lambda request: {"approved": True}
    assert (await executor.execute("run_command", args))["error_code"] == "approval_denied"
    assert not (executor.worktree / "forbidden").exists()
    assert not executor.journal.pending_actions()
    assert all(event["type"] != "action_started" for event in executor.journal.events())


async def test_exact_approval_is_one_use_and_durable_start_precedes_shell(executor):
    requests = []
    async def approve(request):
        assert request["session_id"] == "session" and request["run_id"] == "run" and request["generation"] == 3
        requests.append(request)
        return True
    executor.approve = approve
    executor.current_tool_call_id = "call-shell"
    result = await executor.execute("run_command", {"command": "printf 'hello'; printf 'error' >&2; exit 7"})
    assert not result["ok"] and result["exit_code"] == 7
    assert result["stdout"] == "hello" and result["stderr"] == "error"
    assert requests[0]["command"] == "printf 'hello'; printf 'error' >&2; exit 7"
    assert requests[0]["tool_call_id"] == "call-shell"
    result = await executor.execute("run_command", {"command": "printf 'second'"})
    assert result["ok"] and len(requests) == 2 and requests[0]["action_id"] != requests[1]["action_id"]
    types = [e["type"] for e in executor.journal.events()]
    assert types == ["approval_requested", "approval_decided", "action_started", "action_completed"] * 2
    assert not executor.journal.pending_actions()


async def test_approval_callback_cannot_mutate_the_executed_command(executor):
    def approve(request):
        request["command"] = "touch tampered"
        request["generation"] = 1000
        return True
    executor.approve = approve
    result = await executor.execute("run_command", {"command": "printf original"})
    assert result["stdout"] == "original" and not (executor.worktree / "tampered").exists()


async def test_concurrent_sdk_tool_calls_keep_independent_recovery_ids(executor):
    first_waiting, release = asyncio.Event(), asyncio.Event()
    async def approve(request):
        if request["tool_call_id"] == "first":
            first_waiting.set()
            await release.wait()
        return True
    executor.approve = approve
    executor.current_tool_call_id = "first"
    first = asyncio.create_task(executor.execute("run_command", {"command": "printf first"}))
    await first_waiting.wait()
    executor.current_tool_call_id = "second"
    second = await executor.execute("run_command", {"command": "printf second"})
    executor.current_tool_call_id = None
    release.set()
    first_result = await first
    assert first_result["stdout"] == "first" and second["stdout"] == "second"
    completed = [event["data"] for event in executor.journal.events() if event["type"] == "action_completed"]
    assert {row["tool_call_id"]: row["result"]["stdout"] for row in completed} == {"first": "first", "second": "second"}


async def test_shell_directory_is_scoped_and_credential_env_is_removed(executor, monkeypatch):
    (executor.worktree / "nested").mkdir()
    monkeypatch.setenv("CUSTOM_MODEL_VALUE", "private-key-for-provider")
    monkeypatch.setenv("OTHER_API_KEY", "also-private")
    executor.journal.add_secrets("private-key-for-provider")
    executor.approve = lambda request: True
    result = await executor.execute("run_command", {"cwd": "nested", "command":
        "printf '%s|%s|' \"${CUSTOM_MODEL_VALUE:-absent}\" \"${OTHER_API_KEY:-absent}\"; pwd"})
    assert result["ok"] and result["stdout"] == "absent|absent|" + str(executor.worktree / "nested") + "\n"
    assert not (await executor.execute("run_command", {"cwd": "..", "command": "true"}))["ok"]


async def test_other_profile_custom_credential_env_never_reaches_shell(executor, monkeypatch):
    from cairnmind.cockpit_state import CockpitState
    state = CockpitState(executor.settings)
    profiles = state.load()["profiles"]
    state.update({"profiles": [*profiles, {"id": "other-vendor", "client": "api", "label": "Other vendor",
                                            "provider": "openai_compatible", "model": "tools-model",
                                            "base_url": "http://127.0.0.1:12345/v1",
                                            "credential_ref": "env:ACME_VALUE"}]})
    monkeypatch.setenv("ACME_VALUE", "another-profiles-private-value")
    executor.approve = lambda request: True
    result = await executor.execute("run_command", {"command": "printf '%s' \"${ACME_VALUE:-absent}\""})
    assert result["ok"] and result["stdout"] == "absent"
    assert "another-profiles-private-value" not in executor.journal.path.read_text()


async def test_shell_output_and_journal_redact_known_keys(executor):
    secret = "key-private-123456789"
    executor.journal.add_secrets(secret)
    executor.approve = lambda request: True
    (executor.worktree / "secret.txt").write_text(secret)
    result = await executor.execute("run_command", {"command": "cat secret.txt"})
    assert result["stdout"] == "[REDACTED]" and secret not in executor.journal.path.read_text()


async def test_known_credentials_cannot_enter_shell_argv(executor):
    executor.journal.add_secrets("private-api-key-123456")
    calls = []
    executor.approve = lambda request: calls.append(request) or True
    result = await executor.execute("run_command", {"command": "printf private-api-key-123456"})
    assert result["error_code"] == "credential_in_command" and not calls
    assert not executor.journal.path.exists()


async def test_large_shell_output_is_drained_without_deadlock(executor):
    executor.approve = lambda request: True
    result = await executor.execute("run_command", {"command":
        "python3 -c \"import sys; sys.stdout.write('x'*2000000); sys.stderr.write('err')\""})
    assert result["ok"] and result["truncated"]
    assert len(result["stdout"]) == MAX_COMMAND_OUTPUT_BYTES and result["stdout_bytes"] == 2000000
    assert result["stderr"] == "err"


async def test_timeout_terminates_background_group_and_is_not_replayed(executor):
    executor.approve = lambda request: True
    executor.command_timeout_seconds = .15
    started = time.monotonic()
    result = await executor.execute("run_command", {"command": "sleep 30 & echo $! > child.pid; wait", "timeout_seconds": 5})
    pid = int((executor.worktree / "child.pid").read_text())
    assert not result["ok"] and result["error_code"] == "command_timeout"
    assert time.monotonic() - started < 3 and not live(pid)
    assert not executor.journal.pending_actions()


async def test_cancellation_kills_owned_group_and_preserves_uncertainty(executor):
    executor.approve = lambda request: True
    task = asyncio.create_task(executor.execute("run_command", {"command": "sleep 30 & echo $! > child.pid; wait"}))
    await wait_for(executor.worktree / "child.pid")
    pid = int((executor.worktree / "child.pid").read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not live(pid) and not executor._running
    pending = executor.journal.pending_actions()
    assert len(pending) == 1 and pending[0]["last_state"] == "action_interrupted"
    assert (executor.worktree / "child.pid").read_text().strip() == str(pid)


async def test_explicit_interrupt_stops_owned_group_and_unrelated_process_survives(executor):
    executor.approve = lambda request: True
    unrelated = await asyncio.create_subprocess_exec("sleep", "30", start_new_session=True)
    try:
        task = asyncio.create_task(executor.execute("run_command", {"command": "sleep 30 & echo $! > child.pid; wait"}))
        await wait_for(executor.worktree / "child.pid")
        child = int((executor.worktree / "child.pid").read_text())
        await executor.interrupt()
        result = await task
        assert result["interrupted"] and result["error_code"] == "command_interrupted"
        assert not live(child) and live(unrelated.pid)
        assert len(executor.journal.pending_actions()) == 1
    finally:
        if unrelated.returncode is None:
            unrelated.terminate()
        await unrelated.wait()


async def test_journal_failure_prevents_file_effect(executor, monkeypatch):
    def refused(*args, **kwargs):
        raise JournalError("storage failed")
    monkeypatch.setattr(executor.journal, "append", refused)
    with pytest.raises(JournalError):
        await executor.execute("write_file", {"path": "must-not-exist", "content": "bad", "expected_sha256": None})
    assert not (executor.worktree / "must-not-exist").exists()


async def test_worktree_replacement_is_refused(executor):
    old = executor.worktree.with_name("previous")
    executor.worktree.rename(old)
    executor.worktree.mkdir()
    result = await executor.execute("write_file", {"path": "file", "content": "bad", "expected_sha256": None})
    assert result["error_code"] == "worktree_changed" and not (executor.worktree / "file").exists()


@pytest.mark.parametrize("operation", ["interrupt", "cancel"])
async def test_interrupt_during_subprocess_startup_cannot_lose_owned_child(executor, monkeypatch, operation):
    real = asyncio.create_subprocess_exec
    created, release = asyncio.Event(), asyncio.Event()
    async def delayed_spawn(*args, **kwargs):
        process = await real(*args, **kwargs)
        created.set()
        await release.wait()
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    executor.approve = lambda request: True
    task = asyncio.create_task(executor.execute("run_command", {"command": "sleep 30 & echo $! > child.pid; wait"}))
    await asyncio.wait_for(created.wait(), 3)
    await wait_for(executor.worktree / "child.pid")
    pid = int((executor.worktree / "child.pid").read_text())
    if operation == "interrupt":
        stopping = asyncio.create_task(executor.interrupt())
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(stopping, 3)
        result = await asyncio.wait_for(task, 3)
        assert result["interrupted"]
    else:
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
    assert not live(pid) and not executor._running and not executor._starting
    assert len(executor.journal.pending_actions()) == 1
