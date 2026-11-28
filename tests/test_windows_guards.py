"""Permanent cross-platform guard tests for Windows support (#435, #436, #437, #439, #440, #445, #448)."""
from __future__ import annotations

import ast
import importlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import make_symlink_or_skip

from cairnmind.pal import private_files

SRC_DIR = Path(__file__).resolve().parents[1] / "src" / "cairnmind"
TESTS_DIR = Path(__file__).resolve().parent


def test_import_every_module_on_this_platform():
    """Every module under src/cairnmind/ must import cleanly on Windows, macOS, and Linux (#437)."""
    failures = []
    for path in sorted(SRC_DIR.rglob("*.py")):
        if path.name == "__main__.py":
            continue
        rel = path.relative_to(SRC_DIR)
        if rel == Path("__init__.py"):
            mod_name = "cairnmind"
        elif path.name == "__init__.py":
            mod_name = "cairnmind." + ".".join(rel.parent.parts)
        else:
            mod_name = "cairnmind." + ".".join(rel.with_suffix("").parts)
        try:
            importlib.import_module(mod_name)
        except Exception as exc:
            failures.append(f"{mod_name}: {type(exc).__name__}: {exc}")
    assert not failures, "Modules failed to import:\n" + "\n".join(failures)


def test_no_fcntl_flock_or_unapproved_fcntl_imports():
    """Only file_locks.py may call flock; fcntl imports are restricted to file_locks and POSIX PTY modules (#435)."""
    allowed_fcntl = {
        "file_locks.py",
        # POSIX-only PTY/tmux modules (ioctl / F_GETFL, zero flock calls)
        "client_runtime.py", "native_worker.py", "terminal_bridge.py", "terminal_preview.py",
    }
    bad_imports = []
    bad_flocks = []
    for path in sorted(SRC_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "fcntl" and path.name not in allowed_fcntl:
                        bad_imports.append(f"{path.name}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom) and node.module == "fcntl":
                if path.name not in allowed_fcntl:
                    bad_imports.append(f"{path.name}:{node.lineno}")
            elif isinstance(node, ast.Attribute) and node.attr == "flock" and path.name != "file_locks.py":
                bad_flocks.append(f"{path.name}:{node.lineno}")
    assert not bad_imports, f"Unexpected fcntl imports: {bad_imports}"
    assert not bad_flocks, f"Direct flock() calls outside file_locks.py: {bad_flocks}"


def test_no_unapproved_chmod_or_getuid_calls():
    """Direct chmod/fchmod/getuid/st_uid must go through private_files.py outside the closed allowlist (#436)."""
    allowed = {
        "private_files.py",
        # POSIX-only PTY / tmux / Unix socket / kubelet / executable-bit preservation sites
        "agent_tools.py", "client_runtime.py", "contract.py", "copilot_adapter.py",
        "engine_docker.py", "integrations.py", "native_worker.py", "person_proof.py",
        "runtime_sockets.py", "system_links.py", "terminal_preview.py", "workspace.py",
    }
    violations = []
    for path in sorted(SRC_DIR.rglob("*.py")):
        if path.name in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {"chmod", "fchmod", "getuid", "st_uid"}:
                violations.append(f"{path.name}:{node.lineno}: .{node.attr}")
    assert not violations, "Direct privacy/uid calls outside private_files.py:\n" + "\n".join(violations)


def test_no_direct_os_pread_outside_private_files():
    """All offset reads must go through private_files.pread_at (#440)."""
    violations = []
    for path in sorted(SRC_DIR.rglob("*.py")):
        if path.name == "private_files.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "pread":
                violations.append(f"{path.name}:{node.lineno}")
    assert not violations, f"Direct os.pread calls outside private_files.py: {violations}"


def test_explicit_utf8_encoding_on_all_text_file_io():
    """Every text-mode read_text/write_text/open in src/cairnmind/ must pass encoding= (#445)."""
    violations = []
    for path in sorted(SRC_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            kws = {kw.arg for kw in node.keywords}
            if isinstance(fn, ast.Attribute) and fn.attr in {"read_text", "write_text"}:
                if "encoding" not in kws:
                    violations.append(f"{path.name}:{node.lineno}: .{fn.attr}() without encoding")
            elif isinstance(fn, ast.Name) and fn.id == "open":
                mode = None
                if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                    mode = node.args[1].value
                for kw in node.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                        mode = kw.value.value
                if (mode is None or "b" not in mode) and "encoding" not in kws:
                    violations.append(f"{path.name}:{node.lineno}: open(mode={mode!r}) without encoding")
            elif (
                isinstance(fn, ast.Attribute)
                and fn.attr == "open"
                # Only flag Path.open("r"/"w"/"a"/...) with explicit string mode lacking 'b' and 'encoding'
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                mode = node.args[0].value
                if mode and set(mode) <= set("rwax+t") and "b" not in mode and "encoding" not in kws:
                    violations.append(f"{path.name}:{node.lineno}: .open({mode!r}) without encoding")
    assert not violations, "Text I/O without explicit encoding=:\n" + "\n".join(violations)


def test_windows_skip_count_stays_bounded():
    """Prevent silent growth of tests skipped on Windows (#448)."""
    skip_pattern = re.compile(r'skipif\([^)]*(?:sys\.platform\s*==\s*["\']win32["\']|os\.name\s*==\s*["\']nt["\']|IS_WINDOWS\b)')
    count = 0
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        count += len(skip_pattern.findall(path.read_text(encoding="utf-8")))
    assert count <= 35, f"Windows skipif count grew to {count} (budget: 35); justify or fix rather than skipping"


@pytest.mark.asyncio
async def test_agent_tools_rejects_junction_and_symlink_escape(tmp_path):
    """ToolExecutor must fail closed on NTFS directory junctions and symlinks pointing outside worktree (#437)."""
    from cairnmind.agents.agent_journal import AgentJournal
    from cairnmind.agents.agent_tools import ToolExecutor
    from cairnmind.config import Settings

    settings = Settings.load(tmp_path / "data", create=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "inside.txt").write_text("inside\n", encoding="utf-8")

    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("do-not-touch\n", encoding="utf-8")

    escape = worktree / "escape"
    make_symlink_or_skip(escape, outside, target_is_directory=True)

    journal = AgentJournal(settings, "session-junction-guard")
    tools = ToolExecutor(settings, worktree, session_id="session-junction-guard", run_id="run", generation=1,
                         journal=journal)

    listed = await tools.execute("list_files", {"recursive": True})
    assert all("secret.txt" not in row["path"] for row in listed["files"])

    read_res = await tools.execute("read_file", {"path": "escape/secret.txt"})
    assert read_res["ok"] is False and "do-not-touch" not in str(read_res)

    write_res = await tools.execute("write_file", {"path": "escape/new.txt", "content": "pwned", "expected_sha256": None})
    assert write_res["ok"] is False
    assert not (outside / "new.txt").exists()

    patch_res = await tools.execute("apply_patch", {
        "path": "escape/secret.txt",
        "expected_sha256": "0" * 64,
        "old_text": "do-not-touch",
        "new_text": "changed",
    })
    assert patch_res["ok"] is False
    assert secret.read_text(encoding="utf-8") == "do-not-touch\n"

    del_res = await tools.execute("delete_file", {"path": "escape/secret.txt", "expected_sha256": "0" * 64})
    assert del_res["ok"] is False
    assert secret.exists()


def test_atomic_replace_succeeds_while_concurrent_reader_holds_handle(tmp_path):
    """Concurrent open_no_follow readers must not cause Windows sharing violations on atomic replace (#439)."""
    target = tmp_path / "state.json"
    private_files.write_durable_private(target, '{"version": 1}\n')

    reader_code = (
        "import os, sys; from pathlib import Path; from cairnmind.pal import private_files\n"
        "fd = private_files.open_no_follow(Path(sys.argv[1]), os.O_RDONLY, private_on_create=False)\n"
        "with os.fdopen(fd, 'rb') as f:\n"
        "    before = f.read().decode('utf-8')\n"
        "    print('OPENED', flush=True)\n"
        "    sys.stdin.readline()\n"
        "    print(before.strip(), flush=True)\n"
    )
    reader = subprocess.Popen(
        [sys.executable, "-c", reader_code, str(target)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert reader.stdout.readline().strip() == "OPENED"
        private_files.write_durable_private(target, '{"version": 2}\n')
        assert target.read_text(encoding="utf-8") == '{"version": 2}\n'
    finally:
        reader.stdin.close()
        out = reader.stdout.read()
        reader.wait(timeout=15)
    assert '{"version": 1}' in out


def test_transcript_watermark_crlf_and_lf_parity(tmp_path):
    """_transcript_watermark handles CRLF and LF transcripts cleanly without spurious change errors (#440)."""
    from cairnmind.memory.integrations import _transcript_watermark

    lf_path = tmp_path / "lf.jsonl"
    crlf_path = tmp_path / "crlf.jsonl"
    line1 = '{"role": "user", "text": "zażółć gęślą jaźń"}'
    line2 = '{"role": "assistant", "text": "ok"}'
    partial = '{"role": "user", "text": "unfinished'

    lf_raw = f"{line1}\n{line2}\n{partial}".encode()
    crlf_raw = f"{line1}\r\n{line2}\r\n{partial}".encode()
    lf_path.write_bytes(lf_raw)
    crlf_path.write_bytes(crlf_raw)

    wm_lf = _transcript_watermark(lf_path)
    wm_crlf = _transcript_watermark(crlf_path)

    assert wm_lf["observed_size"] == len(lf_raw)
    assert wm_crlf["observed_size"] == len(crlf_raw)
    assert wm_lf["pending_tail_bytes"] == len(partial.encode("utf-8"))
    assert wm_crlf["pending_tail_bytes"] == len(partial.encode("utf-8"))
    assert wm_lf["pending_tail_sha256"] == wm_crlf["pending_tail_sha256"]
    assert wm_lf["size"] == len(f"{line1}\n{line2}\n".encode())
    assert wm_crlf["size"] == len(f"{line1}\r\n{line2}\r\n".encode())
    assert wm_lf["stable"] is True and wm_crlf["stable"] is True


def test_polish_utf8_round_trip_without_pythonutf8(tmp_path):
    """All core local files round-trip Polish UTF-8 without PYTHONUTF8=1 set in environment (#445)."""
    env = {k: v for k, v in os.environ.items() if k.upper() != "PYTHONUTF8"}
    script = (
        "import json, sys; from pathlib import Path\n"
        "from cairnmind.agents.agent_journal import AgentJournal\n"
        "from cairnmind.terminal.cockpit_state import CockpitState\n"
        "from cairnmind.config import Settings\n"
        "from cairnmind.pal.private_files import write_durable_private\n"
        "from cairnmind.storage.store_writer import installation\n"
        "text = 'Zażółć gęślą jaźń — ąęćłńóśźż'\n"
        "root = Path(sys.argv[1])\n"
        "settings = Settings.load(root / 'data', create=True)\n"
        "settings.chat_model = text\n"
        "settings.save()\n"
        "assert Settings.load(root / 'data').chat_model == text\n"
        "assert installation(settings.data_dir).id\n"
        "state = CockpitState(settings)\n"
        "state.update({'language': 'pl'})\n"
        "assert state.load()['language'] == 'pl'\n"
        "journal = AgentJournal(settings, 'utf8-check')\n"
        "journal.append('message', {'content': text})\n"
        "assert text in json.dumps(journal.events()[0], ensure_ascii=False)\n"
        "note = root / 'note.txt'\n"
        "write_durable_private(note, text)\n"
        "assert note.read_text(encoding='utf-8') == text\n"
        "print('UTF8_OK')\n"
    )
    done = subprocess.run([sys.executable, "-c", script, str(tmp_path)],
                          env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert done.returncode == 0, f"stdout={done.stdout}\nstderr={done.stderr}"
    assert "UTF8_OK" in done.stdout
