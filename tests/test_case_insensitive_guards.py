"""Protected and excluded paths spelled with other letter case, or by a short alias (#230).

A Windows drive seen from WSL (/mnt/c) and macOS by default ignore letter case, and a Windows drive also
answers to the short 8.3 alias of a long name (`GIT~1` for `.git`). The tests with mixed-case names run
everywhere; the tests that write through another spelling need a directory that ignores case: `tmp_path`
when it does (macOS), else the folder named by CAIRNMIND_TEST_CASELESS_DIR (in WSL, one under /mnt/c).
"""
import hashlib
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace

import pytest

from cairnmind import agent_tools
from cairnmind.agent_journal import AgentJournal
from cairnmind.agent_tools import ToolExecutor
from cairnmind.config import Settings
from cairnmind.ingest import current_source, index_project
from cairnmind.mcp_servers import inside_worktree
from cairnmind.memory_paths import (RESERVED_DIRECTORIES, is_memory_path, is_reserved_below, is_reserved_memory,
                                    nested_reserved)
from cairnmind.store import Store, validate_scope
from cairnmind.verification import _input_patterns


def _ignores_case(directory):
    probe = Path(tempfile.mkdtemp(prefix="Case230-", dir=directory))
    try:
        return (probe.parent / probe.name.lower()).exists() and (probe.parent / probe.name.upper()).exists()
    finally:
        probe.rmdir()


@pytest.fixture
def caseless(tmp_path):
    if _ignores_case(tmp_path):
        yield tmp_path
        return
    base = os.environ.get("CAIRNMIND_TEST_CASELESS_DIR")
    if not base or not Path(base).is_dir() or not _ignores_case(base):
        pytest.skip("No directory that ignores letter case: tmp_path respects case and CAIRNMIND_TEST_CASELESS_DIR "
                    "does not name one (in WSL, set it to a folder on a Windows drive under /mnt/c)")
    directory = Path(tempfile.mkdtemp(prefix="cm230-", dir=base))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def make_executor(tmp_path, root, approve=None):
    settings = Settings.load(tmp_path / "data", create=True)
    return ToolExecutor(settings, root, session_id="session", run_id="run", generation=1,
                        journal=AgentJournal(settings, "session"), approve=approve)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


# The built-in agent's file tools ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [".GIT/hooks/pre-commit", ".Git/config", "sub/.GIT/hooks/x", ".gIT/index",
                                  ".MEMORY/Cairnmind/manifest.json", ".Memory/DevBrain/x", ".memory/Cairnmind-Published/a.md"])
async def test_the_agent_refuses_every_spelling_of_git_and_managed_memory(tmp_path, path):
    root = tmp_path / "project"
    root.mkdir()
    executor = make_executor(tmp_path, root)
    result = await executor.execute("write_file", {"path": path, "content": "#!/bin/sh\n", "expected_sha256": None})
    assert not result["ok"] and result["error_code"] == "protected_path"
    assert not executor.journal.pending_actions()
    assert [item.name for item in root.iterdir()] == []
    folder = path.rsplit("/", 1)[0]
    for name, arguments in (("read_file", {"path": path}), ("list_files", {"path": folder}),
                            ("run_command", {"command": "true", "cwd": folder})):
        refused = await executor.execute(name, arguments)
        assert not refused["ok"] and refused["error_code"] == "protected_path"


async def test_a_listing_leaves_out_git_in_any_spelling(tmp_path):
    root = tmp_path / "project"
    for name in (".GIT", "src"):
        (root / name).mkdir(parents=True)
        (root / name / "file").write_text("x")
    listed = await make_executor(tmp_path, root).execute("list_files", {"recursive": True})
    assert [row["path"] for row in listed["files"]] == ["src", "src/file"]


async def test_on_a_directory_that_ignores_case_no_spelling_reaches_git(tmp_path, caseless):
    root = caseless / "project"
    (root / ".git" / "hooks").mkdir(parents=True)
    (root / ".git" / "config").write_text("[core]\n")
    executor = make_executor(tmp_path, root)
    spellings = [".GIT/hooks/pre-commit", ".Git/hooks/pre-commit", ".gIt/hooks/pre-commit"]
    if (root / "GIT~1").exists():  # the short 8.3 alias of a Windows drive
        spellings.append("GIT~1/hooks/pre-commit")
    for path in spellings:
        result = await executor.execute("write_file", {"path": path, "content": "#!/bin/sh\nexit 1\n",
                                                       "expected_sha256": None})
        assert not result["ok"] and result["error_code"] in {"protected_path", "invalid_path"}, path
    assert list((root / ".git" / "hooks").iterdir()) == []
    config = ".GIT/config" if not (root / "GIT~1").exists() else "GIT~1/config"
    for name, arguments in (("delete_file", {"path": config, "expected_sha256": digest("[core]\n")}),
                            ("read_file", {"path": config})):
        result = await executor.execute(name, arguments)
        assert not result["ok"] and result["error_code"] in {"protected_path", "invalid_path"}
    assert (root / ".git" / "config").read_text() == "[core]\n"
    # An ordinary file of the same worktree is written as before.
    assert (await executor.execute("write_file", {"path": "src/app.py", "content": "x = 1\n",
                                                  "expected_sha256": None}))["ok"]
    assert (root / "src" / "app.py").read_text() == "x = 1\n"


async def test_identity_refuses_git_when_no_text_check_does(tmp_path, caseless, monkeypatch):
    # The second line of defence: with the spelling checks switched off, the opened directory is compared
    # with the worktree's .git by device and inode.
    root = caseless / "project"
    (root / ".git" / "hooks").mkdir(parents=True)
    executor = make_executor(tmp_path, root)
    monkeypatch.setattr(agent_tools, "_parts", lambda value, allow_root=False: tuple(value.split("/")))
    monkeypatch.setattr(agent_tools, "is_alias", lambda name, directory: False)
    result = await executor.execute("write_file", {"path": ".GIT/hooks/pre-commit", "content": "x",
                                                   "expected_sha256": None})
    assert not result["ok"] and result["error_code"] == "protected_path"
    assert list((root / ".git" / "hooks").iterdir()) == []


async def test_a_short_alias_does_not_pass_the_gate_for_files_that_configure_execution(tmp_path, caseless):
    root = caseless / "project"
    (root / ".claude").mkdir(parents=True)
    (root / ".github" / "workflows").mkdir(parents=True)
    asked = []
    executor = make_executor(tmp_path, root, approve=lambda request: asked.append(request["path"]) or False)
    paths = [".CLAUDE/settings.json", ".GitHub/Workflows/ci.yml"]
    paths += [alias for alias in ("CLAUDE~1/settings.json", "GITHUB~1/workflows/ci.yml")
              if (root / alias.split("/")[0]).exists()]
    for path in paths:
        result = await executor.execute("write_file", {"path": path, "content": "{}", "expected_sha256": None})
        assert not result["ok"] and result["error_code"] in {"approval_denied", "invalid_path"}, path
    assert asked == [".CLAUDE/settings.json", ".GitHub/Workflows/ci.yml"]
    assert list((root / ".claude").iterdir()) == [] and list((root / ".github" / "workflows").iterdir()) == []


# Windows resolves these to `.git` (a trailing dot or space is dropped, a backslash separates, `::$INDEX_ALLOCATION`
# names the directory itself); elsewhere they are entries that `git add` refuses.
_WINDOWS_SPELLINGS = [".git./hooks/pre-commit", ".git /hooks/x", ".GIT\\hooks\\x", ".GIT::$INDEX_ALLOCATION/hooks/x",
                      "sub/.git../config", "src/file:stream", "src/notes.", "src/notes ", "src\\app.py"]


@pytest.mark.parametrize("path", _WINDOWS_SPELLINGS)
async def test_every_file_tool_refuses_parts_that_windows_rewrites(tmp_path, path):
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "notes").write_text("x")
    executor = make_executor(tmp_path, root)
    for name, arguments in (("write_file", {"path": path, "content": "#!/bin/sh\n", "expected_sha256": None}),
                            ("apply_patch", {"path": path, "expected_sha256": digest("x"), "old_text": "x",
                                             "new_text": "y"}),
                            ("delete_file", {"path": path, "expected_sha256": digest("x")}),
                            ("read_file", {"path": path}), ("list_files", {"path": path}),
                            ("search_files", {"path": path, "query": "x"}),
                            ("run_command", {"command": "true", "cwd": path})):
        result = await executor.execute(name, arguments)
        assert not result["ok"] and result["error_code"] == "invalid_path", (name, path)
    assert not executor.journal.pending_actions()
    assert sorted(item.name for item in root.iterdir()) == ["src"]
    assert [item.name for item in (root / "src").iterdir()] == ["notes"]


async def test_ordinary_names_near_those_spellings_still_work(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    executor = make_executor(tmp_path, root)
    for path in (".gitignore", ".gitkeep","x.git/a", "my notes/a b.txt", ".env.local", "a.b.c/d.e"):
        result = await executor.execute("write_file", {"path": path, "content": "x", "expected_sha256": None})
        assert result["ok"], (path, result)
        assert (root / path).read_text() == "x"


async def test_a_listing_leaves_out_names_that_windows_rewrites(tmp_path):
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    for name in ("app.py", "notes.", "a:b"):
        (root / "src" / name).write_text("x")
    listed = await make_executor(tmp_path, root).execute("list_files", {"recursive": True})
    assert [row["path"] for row in listed["files"]] == ["src", "src/app.py"]


# Third-party MCP servers: the worktree check ----------------------------------------------------------------

def test_a_server_in_the_worktree_spelled_with_other_case_is_refused(caseless):
    worktree = caseless / "Wt"
    worktree.mkdir()
    (worktree / "srv.py").write_text("print()\n")
    other = caseless / "WT"

    def refused(command="/usr/bin/env", args=()):
        return inside_worktree({"id": "x", "command": command, "args": list(args), "env": {}}, worktree)
    assert refused(sys.executable, [str(other / "srv.py")])
    assert refused(str(other / "srv.py"))
    assert refused("/bin/sh", ["-c", f"cd {other} && ./srv"])
    assert not refused(sys.executable, [str(caseless / "wt-other" / "srv.py")])


def test_a_short_alias_of_the_worktree_inside_a_shell_command_is_refused(caseless):
    worktree = caseless / "worktreelongname"
    worktree.mkdir()
    (worktree / "srv.py").write_text("print()\n")
    alias = caseless / "WORKTR~1"
    if not alias.is_dir() or alias.stat().st_ino != worktree.stat().st_ino:
        pytest.skip("This directory gives no short 8.3 alias (8.3 names are off on its volume)")

    def refused(script):
        return inside_worktree({"id": "x", "command": "/bin/sh", "args": ["-c", script], "env": {}}, worktree)
    for script in (f"cd {alias}&&python3 srv.py", f"cd {alias}||exit", f"python3 {alias}/srv.py|cat",
                   f"x=`ls {alias}`", f"x=$(cat {alias}/srv.py)", f"python3 <{alias}/srv.py", f"'{alias}'/srv.py"):
        assert refused(script), script
    assert not refused(f"cd {caseless / 'other'}&&python3 srv.py")


def test_a_directory_that_differs_only_in_case_stays_outside_where_case_counts(tmp_path):
    if _ignores_case(tmp_path):
        pytest.skip("tmp_path ignores letter case here; the case-sensitive behaviour is tested on Linux")
    worktree, sibling = tmp_path / "wt", tmp_path / "WT"
    worktree.mkdir()
    sibling.mkdir()
    (sibling / "srv.py").write_text("print()\n")
    definition = {"id": "x", "command": sys.executable, "args": [str(sibling / "srv.py")], "env": {}}
    assert not inside_worktree(definition, worktree)
    assert not inside_worktree({**definition, "args": ["-c", f"cd {sibling} && ./srv"], "command": "/bin/sh"}, worktree)


# Memory and indexing exclusions ------------------------------------------------------------------------------

@pytest.mark.parametrize("path", [".MEMORY/devbrain", ".Memory/DevBrain/x.json", "a/../.MEMORY/devbrain",
                                  ".memory/DEVBRAIN", ".Memory/Cairnmind/manifest.json", ".MEMORY/CAIRNMIND-PUBLISHED/a.md"])
def test_reserved_memory_is_compared_without_case(path, tmp_path):
    assert is_reserved_memory(path)
    assert is_memory_path(tmp_path / path, tmp_path)


def test_reserved_directories_below_keep_the_case_of_their_project():
    below = (Path("services/a/.memory/cairnmind"),)
    assert is_reserved_below("services/a/.Memory/Cairnmind/x.md", below)
    assert not is_reserved_below("services/A-other/.memory/cairnmind/x.md", below)
    assert not is_reserved_below(".memory/notes.md", below)


def test_memory_of_a_nested_project_registered_with_other_case_is_not_code_of_its_parent(caseless, tmp_path):
    store = Store(tmp_path / "memory")
    root = caseless / "Repo"
    published = root / "services" / "alpha" / ".memory" / "cairnmind-published"
    published.mkdir(parents=True)
    (published / "note.md").write_text("# note\n")
    (root / "public.py").write_text("value = 1\n")
    parent = store.add_project("parent", root)
    store.add_project("alpha", caseless / "REPO" / "Services" / "Alpha")
    assert nested_reserved(store, root) == tuple(Path("services/alpha") / directory
                                                 for directory in sorted(RESERVED_DIRECTORIES))
    index_project(store, parent["id"])
    assert {Path(row["uri"]).name for row in store.query("SELECT * FROM sources")} == {"public.py"}


def test_a_nested_project_keeps_its_place_where_case_counts(tmp_path):
    if _ignores_case(tmp_path):
        pytest.skip("tmp_path ignores letter case here; the case-sensitive behaviour is tested on Linux")
    (tmp_path / "repo" / "Alpha").mkdir(parents=True)
    (tmp_path / "repo" / "alpha").mkdir()
    rows = [{"path": str(tmp_path / "repo")}, {"path": str(tmp_path / "repo" / "Alpha")},
            {"path": str(tmp_path / "repo" / "gone" / "beta")}, {"path": str(tmp_path / "REPO" / "x")}]
    store = SimpleNamespace(query=lambda *_: rows)
    assert {path.parts[0] for path in nested_reserved(store, tmp_path / "repo")} == {"Alpha", "gone"}


def test_git_in_any_spelling_is_refused_in_scope_and_verification_inputs():
    for pattern in (".GIT/config", "sub/.Git/hooks/*"):
        with pytest.raises(ValueError):
            validate_scope([pattern])
        with pytest.raises(ValueError):
            _input_patterns([pattern])
    with pytest.raises(ValueError):
        _input_patterns([".Memory/Cairnmind/x"])


def test_a_root_spelled_with_other_case_still_holds_the_data_directory(caseless, tmp_path, monkeypatch):
    # The checks that keep the data directory (and its credential store) out of a project, and out of what
    # is indexed, compare by identity when the two are spelled with different letter case.
    data = caseless / "repo" / "mem"
    (data / "credentials").mkdir(parents=True)
    (data / "credentials" / "api-keys.json").write_text("{}")
    with pytest.raises(ValueError, match="credential store"):
        Store.require_safe_project_root(SimpleNamespace(data_dir=data), caseless / "REPO")
    assert Store.require_safe_project_root(SimpleNamespace(data_dir=data), caseless / "repo-other")
    store = Store(tmp_path / "memory")
    root = caseless / "Repo"
    project = store.add_project("demo", root)
    (root / "public.py").write_text("value = 1\n")
    (data / "config.py").write_text("token = 'private'\n")
    monkeypatch.setattr(store, "data_dir", data)
    index_project(store, project["id"])
    assert {Path(row["uri"]).name for row in store.query("SELECT * FROM sources")} == {"public.py"}
    spelled = root / "MEM" / "config.py"
    added = store.snapshot(project["id"], str(spelled), "old", kind="document", metadata={"path": str(spelled)})
    with pytest.raises(ValueError, match="excluded"):
        current_source(store, added["id"])


def test_excluded_directories_are_compared_without_case(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    store = Store(tmp_path / "memory")
    project = store.add_project("demo", root)
    for directory in (".SSH", ".Codex", ".CLAUDE", ".Aws", ".GIT", ".Memory/Cairnmind"):
        folder = root / directory
        folder.mkdir(parents=True)
        (folder / "config.json").write_text('{"token": "private"}')
    (root / "public.py").write_text("value = 1\n")
    index_project(store, project["id"])
    assert {row["uri"] for row in store.query("SELECT * FROM sources")} == {str(root / "public.py")}
    secret = root / ".SSH" / "config.json"
    added = store.snapshot(project["id"], str(secret), "old", kind="document", metadata={"path": str(secret)})
    with pytest.raises(ValueError, match="excluded"):
        current_source(store, added["id"])
