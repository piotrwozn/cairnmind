"""Edge-case tests for verification.py and agent_tools.py (pure fast-tier)."""
from __future__ import annotations

import asyncio
import stat
import subprocess
from types import SimpleNamespace

import pytest

from cairnmind.agents import agent_tools
from cairnmind.config import Settings
from cairnmind.memory import verification as ver


class _MiniStore:
    def __init__(self, root):
        self.root = str(root)

    def resolve_project(self, project_id):
        return {"id": project_id, "root": self.root}

    def query(self, sql, params=None):
        return []


def test_verification_entries_manifest_profiles_and_stop_group_edges(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.txt").write_text("hi", encoding="utf-8")

    # _entry: unsupported mode (FIFO-like via monkeypatched Path.lstat) and OSError unreadable
    from pathlib import Path
    unknowns = []
    monkeypatch.setattr(Path, "lstat", lambda self: SimpleNamespace(st_mode=stat.S_IFIFO | 0o600))
    entry = ver._entry(root, "a.txt", "100644", unknowns, lambda p: True)
    assert entry["kind"] == "unsupported" and any("unsupported input" in u for u in unknowns)

    unknowns = []
    monkeypatch.setattr(Path, "lstat", lambda self: (_ for _ in ()).throw(PermissionError("denied")))
    entry = ver._entry(root, "a.txt", "100644", unknowns, lambda p: True)
    assert entry["kind"] == "unreadable" and any("unreadable input" in u for u in unknowns)
    monkeypatch.undo()

    # _manifest: unmerged stage + extra_input directory recursion
    sub = root / "extra_dir"
    sub.mkdir()
    (sub / "nested.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        ver,
        "_git",
        lambda r, *args: b"100644 0000 2\ta.txt\0" if "--stage" in args else b"",
    )
    entries, man_unknowns = ver._manifest(root, ["extra_dir"])
    assert any("unmerged Git input" in u for u in man_unknowns)
    assert any(e["path"] == "extra_dir/nested.txt" for e in entries)

    # require_git_tree: check-ignore returncode 2 raises ValueError
    monkeypatch.setattr(ver, "_git", lambda r, *args: str(tmp_path).encode())
    monkeypatch.setattr(ver.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=2, stdout=b"", stderr=b""))
    with pytest.raises(ValueError, match="Cannot inspect Git worktree"):
        ver.require_git_tree(root)
    monkeypatch.undo()

    # load_profiles validation errors & declared_check_names invalid TOML
    cfg_dir = root / ".cairnmind"
    cfg_dir.mkdir()
    cfg = cfg_dir / "checks.toml"
    store = _MiniStore(root)
    monkeypatch.setattr(ver, "_root", lambda s, pid, wt=None: root.resolve())

    cfg.write_text("[checks.bad]\nargv=['pytest']\nunknown_key=1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid check profile"):
        ver.load_profiles(store, "p1")

    cfg.write_text("[checks.bad]\nargv=['pytest']\ntimeout_seconds=0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="timeout"):
        ver.load_profiles(store, "p1")

    cfg.write_text("[checks.bad]\nargv=['pytest']\noutput_limit_bytes=0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="output limit"):
        ver.load_profiles(store, "p1")

    cfg.write_text("not = [valid toml", encoding="utf-8")
    assert ver.declared_check_names(root) is None

    # _stop_group: job path with TimeoutExpired fallback to kill(), and job=None POSIX path
    killed = []

    class _SlowProc:
        pid = 12345

        def __init__(self):
            self.calls = 0

        def wait(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(["x"], timeout)
            return 0

        def kill(self):
            killed.append(True)

    ver._stop_group(_SlowProc(), job=SimpleNamespace(terminate=lambda grace_seconds=0.3: None))
    assert killed == [True]

    monkeypatch.setattr(ver, "terminate_group", lambda pid, force=False: (_ for _ in ()).throw(ProcessLookupError()))
    ver._stop_group(_SlowProc(), job=None)

    # _execute rejects escaped cwd
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    res = ver._execute({"argv": ["echo", "hi"], "cwd": "../outside", "timeout_seconds": 5, "output_limit_bytes": 1000}, root, out_dir)
    assert "escaped" in str(res)


@pytest.mark.asyncio
async def test_agent_tools_walk_search_and_terminate_edges(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    (work / "nul.bin").write_bytes(b"abc\x00def")
    (work / "bad.utf8").write_bytes(b"\xff\xfe")
    (work / "good.txt").write_text("hello world\n", encoding="utf-8")

    settings = Settings(data_dir=tmp_path / "data")
    journal = SimpleNamespace(append=lambda *a, **k: {"id": "e1"}, redact=lambda v: v)
    tools = agent_tools.ToolExecutor(settings, work, session_id="s1", run_id="r1", generation=1, journal=journal)

    # _list non-bool recursive
    with pytest.raises(agent_tools.ToolError, match="boolean"):
        tools._list({"recursive": "yes"})

    # _search invalid args + skips binary/invalid utf8 files
    with pytest.raises(agent_tools.ToolError, match="nonempty"):
        tools._search({"query": ""})
    with pytest.raises(agent_tools.ToolError, match="boolean"):
        tools._search({"query": "hello", "case_sensitive": "no"})
    found = tools._search({"query": "hello"})
    assert len(found["matches"]) == 1

    # _walk depth >= 32 truncation
    cur = work
    for i in range(34):
        cur = cur / f"d{i}"
        cur.mkdir()
    listed = tools._list({"recursive": True, "limit": 500})
    assert listed["truncated"] is True

    # _terminate on Windows: job timeout -> False; job raises -> False; no job + process wait timeout -> False
    monkeypatch.setattr(agent_tools, "IS_WINDOWS", True)
    monkeypatch.setattr(agent_tools, "KILL_WAIT_SECONDS", 0.05)

    class _FakeProc:
        pid = 777

        def kill(self):
            pass

        async def wait(self):
            await asyncio.sleep(5)

    proc = _FakeProc()
    tools._jobs[proc.pid] = SimpleNamespace(terminate=lambda: None, running=lambda: True)
    assert await tools._terminate(proc, birth=None) is False

    tools._jobs[proc.pid] = SimpleNamespace(
        terminate=lambda: None,
        running=lambda: (_ for _ in ()).throw(RuntimeError("job err")),
    )
    assert await tools._terminate(proc, birth=None) is False

    tools._jobs.pop(proc.pid)
    monkeypatch.setattr(agent_tools, "recorded", lambda pid, birth: True)
    assert await tools._terminate(proc, birth=None) is False

    # _kill_group swallows exception; _signal_group handles ProcessLookupError & owned=False
    agent_tools.ToolExecutor._kill_group(SimpleNamespace(pid=1, kill=lambda: (_ for _ in ()).throw(OSError("x"))), None)
    monkeypatch.setattr(agent_tools, "recorded", lambda pid, birth: False)
    assert await agent_tools.ToolExecutor._signal_group(123, proc, None) is True
    monkeypatch.setattr(agent_tools, "recorded", lambda pid, birth: True)
    monkeypatch.setattr(agent_tools.os, "killpg", lambda pgid, sig: (_ for _ in ()).throw(ProcessLookupError()), raising=False)
    monkeypatch.setattr(agent_tools.signal, "SIGKILL", 9, raising=False)
    assert await agent_tools.ToolExecutor._signal_group(123, proc, None) is True
