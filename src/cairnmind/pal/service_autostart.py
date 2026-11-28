"""Per-user service autostart and automatic restart on failure on native Windows (#447).

Uses Windows Task Scheduler (`schtasks.exe`) with a per-user `LogonTrigger` and
`RestartOnFailure` (no Administrator / UAC elevation required).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

from cairnmind.config import Settings

TASK_PREFIX = "CairnmindService"
RESTART_INTERVAL = "PT1M"
RESTART_COUNT = 3


def task_name(settings: Settings) -> str:
    """Stable per-data-dir Windows Task Scheduler task name."""
    digest = hashlib.sha256(str(settings.data_dir.resolve()).lower().encode("utf-8")).hexdigest()[:12]
    return f"{TASK_PREFIX}-{digest}"


def resolve_service_command(settings: Settings, *, executable: str | None = None) -> tuple[str, str]:
    """Return (command, arguments) to run the foreground knowledge service for settings.data_dir."""
    data_dir = str(settings.data_dir.resolve())
    if executable:
        return executable, f'--data-dir "{data_dir}" serve'
    found = shutil.which("cairnmind")
    if found:
        return str(Path(found).resolve()), f'--data-dir "{data_dir}" serve'
    return sys.executable, f'-m cairnmind.cli --data-dir "{data_dir}" serve'


def build_task_xml(settings: Settings, *, executable: str | None = None, user_id: str | None = None) -> str:
    """Render a Task Scheduler 1.2 XML definition: per-user LogonTrigger + RestartOnFailure."""
    command, arguments = resolve_service_command(settings, executable=executable)
    principal_user = user_id or os.environ.get("USERNAME") or ""
    user_block = f"      <UserId>{escape(principal_user)}</UserId>\n" if principal_user else ""
    trigger_user = f"      <UserId>{escape(principal_user)}</UserId>\n" if principal_user else ""
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        f"    <Description>Cairnmind local knowledge service for {escape(str(settings.data_dir.resolve()))}</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <LogonTrigger>\n"
        "      <Enabled>true</Enabled>\n"
        f"{trigger_user}"
        "    </LogonTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        f"{user_block}"
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "    <Hidden>true</Hidden>\n"
        "    <RestartOnFailure>\n"
        f"      <Interval>{RESTART_INTERVAL}</Interval>\n"
        f"      <Count>{RESTART_COUNT}</Count>\n"
        "    </RestartOnFailure>\n"
        "    <Enabled>true</Enabled>\n"
        "  </Settings>\n"
        "  <Actions Context=\"Author\">\n"
        "    <Exec>\n"
        f"      <Command>{escape(command)}</Command>\n"
        f"      <Arguments>{escape(arguments)}</Arguments>\n"
        f"      <WorkingDirectory>{escape(str(settings.data_dir.resolve()))}</WorkingDirectory>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def _schtasks_path() -> str | None:
    return shutil.which("schtasks") or shutil.which("schtasks.exe")


def _run_schtasks(args: list[str]) -> subprocess.CompletedProcess[str]:
    exe = _schtasks_path()
    if not exe:
        raise ValueError("schtasks.exe was not found on PATH.")
    return subprocess.run(
        [exe, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )


def status(settings: Settings) -> dict:
    """Non-mutating status of per-user Windows Task Scheduler autostart (safe for `cairnmind doctor`)."""
    name = task_name(settings)
    command, arguments = resolve_service_command(settings)
    if sys.platform != "win32":
        return {
            "supported": False,
            "platform": sys.platform,
            "mechanism": "windows_task_scheduler",
            "task_name": name,
            "installed": False,
            "state": "unsupported",
            "command": f"{command} {arguments}",
        }
    if not _schtasks_path():
        return {
            "supported": False,
            "platform": "win32",
            "mechanism": "windows_task_scheduler",
            "task_name": name,
            "installed": False,
            "state": "unavailable",
            "reason": "schtasks.exe not found",
            "command": f"{command} {arguments}",
        }
    try:
        done = _run_schtasks(["/Query", "/TN", name, "/FO", "LIST"])
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "supported": True,
            "platform": "win32",
            "mechanism": "windows_task_scheduler",
            "task_name": name,
            "installed": False,
            "state": "error",
            "reason": type(exc).__name__,
            "command": f"{command} {arguments}",
        }
    if done.returncode != 0:
        return {
            "supported": True,
            "platform": "win32",
            "mechanism": "windows_task_scheduler",
            "task_name": name,
            "installed": False,
            "state": "not_installed",
            "command": f"{command} {arguments}",
        }
    task_state = "installed"
    for line in done.stdout.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            if key.strip().lower() in {"status", "stan"} and val.strip():
                task_state = val.strip()
                break
    return {
        "supported": True,
        "platform": "win32",
        "mechanism": "windows_task_scheduler",
        "task_name": name,
        "installed": True,
        "state": task_state,
        "restart_on_failure": {"interval": RESTART_INTERVAL, "count": RESTART_COUNT},
        "command": f"{command} {arguments}",
    }


def install(settings: Settings, *, executable: str | None = None) -> dict:
    """Register or update the per-user logon autostart task in Windows Task Scheduler."""
    if sys.platform != "win32":
        raise ValueError("Service autostart via Windows Task Scheduler is only supported on native Windows.")
    name = task_name(settings)
    xml_text = build_task_xml(settings, executable=executable)
    fd, tmp_path = tempfile.mkstemp(prefix="cairnmind-task-", suffix=".xml")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(xml_text.encode("utf-16"))
        done = _run_schtasks(["/Create", "/TN", name, "/XML", tmp_path, "/F"])
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or f"exit {done.returncode}").strip()
        raise ValueError(f"Could not register Windows autostart task {name}: {detail}")
    return status(settings)


def remove(settings: Settings) -> dict:
    """Remove the per-user logon autostart task from Windows Task Scheduler."""
    if sys.platform != "win32":
        raise ValueError("Service autostart via Windows Task Scheduler is only supported on native Windows.")
    name = task_name(settings)
    current = status(settings)
    if not current["installed"]:
        return current
    done = _run_schtasks(["/Delete", "/TN", name, "/F"])
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or f"exit {done.returncode}").strip()
        raise ValueError(f"Could not remove Windows autostart task {name}: {detail}")
    return status(settings)
