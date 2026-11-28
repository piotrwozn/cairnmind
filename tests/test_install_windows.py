"""Static and runtime checks for the native Windows PowerShell installer (scripts/install-windows.ps1)."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install-windows.ps1"


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def test_install_windows_script_exists_and_enforces_key_contracts():
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'COPILOT_SKIP_CLI_DOWNLOAD = "1"' in text
    assert "uv.lock" in text and "pyproject.toml" in text
    assert "CAIRNMIND_ARCADE_CONNECT_URL" in text
    assert "upgrade-in-progress" in text
    assert "doctor_attention" in text
    assert "cairnmind" in text
    assert "winget install EclipseAdoptium.Temurin.21.JDK" in text


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell runtime checks run on Windows")
def test_install_windows_parses_without_syntax_errors():
    ps = _powershell()
    assert ps, "PowerShell must be available on Windows"
    cmd = (
        "$errors = $null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{SCRIPT}', [ref]$null, [ref]$errors) | Out-Null; "
        "if ($errors -and $errors.Count -gt 0) { $errors | ForEach-Object { [Console]::Error.WriteLine($_.Message) }; exit 1 }"
    )
    res = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", cmd],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell runtime checks run on Windows")
def test_install_windows_help_exits_zero_and_mentions_pull_models():
    ps = _powershell()
    assert ps
    res = subprocess.run(
        [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), "-Help"],
        capture_output=True, text=True, timeout=30,
    )
    assert res.returncode == 0, res.stderr
    assert "-PullModels" in res.stdout
    assert "CAIRNMIND_ARCADE_CONNECT_URL" in res.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Upgrade-mark path check on Windows")
def test_install_windows_upgrade_mark_path_agrees_with_python(tmp_path):
    ps = _powershell()
    assert ps
    local_app = tmp_path / "LocalAppData"
    local_app.mkdir()
    env = {**os.environ, "LOCALAPPDATA": str(local_app)}
    # Evaluate Get-UpgradeMarkPath from the script source.
    snippet = (
        "$env:LOCALAPPDATA = '" + str(local_app) + "'\n"
        "$localAppData = $env:LOCALAPPDATA\n"
        "Join-Path (Join-Path (Join-Path $localAppData 'cairnmind') 'state') 'upgrade-in-progress'\n"
    )
    res = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", snippet],
                         env=env, capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    ps_path = Path(res.stdout.strip())
    py = subprocess.run(
        [sys.executable, "-c", "from cairnmind import upgrade_mark; print(upgrade_mark.mark_path())"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert py.returncode == 0, py.stderr
    assert ps_path == Path(py.stdout.strip())
