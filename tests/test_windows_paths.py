"""Windows path shapes (drive letter, spaces + diacritics, >260 chars, UNC) and PowerShell command formatting (#444)."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from cairnmind import path_spelling, process_control
from cairnmind.config import Settings
from cairnmind.ingest import index_project
from cairnmind.retrieval import Retriever
from cairnmind.store import Store


def test_windows_path_shapes_register_index_and_search(arcade_test_engine, tmp_path):
    """Register, index, and retrieve from drive-letter, space+Polish, and long (>260 char) paths (#444)."""
    settings = Settings.load(tmp_path / "data", create=True)
    store = Store(settings.data_dir, settings=settings)
    store.init()
    # 1. Standard drive-letter / temp path + 2. Spaces and Polish diacritics
    polish_dir = tmp_path / "Projekt Zażółć Gęślą" / "moduł główny"
    polish_dir.mkdir(parents=True)
    src_file = polish_dir / "uwierzytelnianie.py"
    src_file.write_text(
        'def sprawdz_token():\n    """Weryfikuje podpisany token sesji (zażółć gęślą)."""\n    return "ok"\n',
        encoding="utf-8",
    )
    project = store.add_project("projekt-zażółć", polish_dir)
    index_project(store, project["id"])
    sources = store.query("SELECT uri FROM sources WHERE project_id=:p", {"p": project["id"]})
    assert any("uwierzytelnianie.py" in (row.get("uri") or "") for row in sources)

    retriever = Retriever(store, settings)
    hits = retriever.search(project["id"], "sprawdz_token", limit=5)
    assert hits, "Expected search hit for Polish/spaced path"
    assert any("uwierzytelnianie.py" in (h.get("uri") or "") for h in hits)

    # 3. Path exceeding 260 characters (Windows MAX_PATH boundary)
    deep = tmp_path / "longroot"
    parts = ["segment_" + ("a" * 32) for _ in range(9)]
    long_dir = deep.joinpath(*parts)
    assert len(str(long_dir)) > 260
    try:
        long_dir.mkdir(parents=True)
        long_file = long_dir / "deep_module.py"
        long_file.write_text("def deep_marker_fn():\n    return 42\n", encoding="utf-8")
    except OSError:
        if sys.platform == "win32":
            # Retry with extended-length prefix if host lacks LongPathsEnabled
            ext_dir = Path("\\\\?\\" + str(long_dir))
            ext_dir.mkdir(parents=True)
            long_file = ext_dir / "deep_module.py"
            long_file.write_text("def deep_marker_fn():\n    return 42\n", encoding="utf-8")
            long_dir = ext_dir
        else:
            raise
    long_project = store.add_project("long-path-project", long_dir)
    index_project(store, long_project["id"])
    long_sources = store.query("SELECT uri FROM sources WHERE project_id=:p", {"p": long_project["id"]})
    assert any("deep_module.py" in (row.get("uri") or "") for row in long_sources)


def test_unc_path_and_case_identity_handling(tmp_path):
    """Verify path_spelling handles Windows drive-letter case-insensitivity and UNC syntax safely (#444)."""
    sample = tmp_path / "CaseTest.txt"
    sample.write_text("hello", encoding="utf-8")
    root_id = path_spelling.path_identity(tmp_path)
    assert path_spelling.same_or_below(sample, root_id)
    if sys.platform == "win32":
        upper = Path(str(sample).upper())
        lower = Path(str(sample).lower())
        assert path_spelling.path_identity(upper) == path_spelling.path_identity(lower)
        # UNC path is recognized as non-drive path and never crashes normalization
        unc = Path(r"\\localhost\C$\Windows")
        assert unc.is_absolute()
        assert not path_spelling.same_or_below(unc, root_id)


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell command execution test is Windows-only")
def test_format_command_executes_cleanly_in_powershell(tmp_path):
    """Commands printed by format_command() can be pasted directly into PowerShell (#444)."""
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        pytest.skip("Neither pwsh nor powershell available on PATH")
    marker = tmp_path / "folder with spaces" / "out.txt"
    marker.parent.mkdir(parents=True)
    argv = [
        sys.executable, "-c",
        "import sys; from pathlib import Path; Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')",
        str(marker), "hello from PowerShell",
    ]
    rendered = process_control.format_command(argv)
    # PowerShell call operator `&` runs the formatted command directly when pasted
    ps_cmd = rendered if rendered.startswith("& ") else f"& {rendered}"
    done = subprocess.run([shell, "-NoProfile", "-Command", ps_cmd],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, f"cmd={ps_cmd}\nstdout={done.stdout}\nstderr={done.stderr}"
    assert marker.read_text(encoding="utf-8") == "hello from PowerShell"
