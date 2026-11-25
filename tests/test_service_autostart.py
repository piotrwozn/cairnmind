"""Tests for Windows per-user Task Scheduler service autostart (#447)."""
from __future__ import annotations

import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from cairnmind import service_autostart
from cairnmind.config import Settings


def test_build_task_xml_is_valid_logon_and_restart_on_failure(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    xml_text = service_autostart.build_task_xml(settings, executable=r"C:\Tools\cairnmind.exe", user_id="piotr")
    root = ET.fromstring(xml_text)
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.find(".//t:LogonTrigger/t:Enabled", ns).text == "true"
    assert root.find(".//t:Principal/t:RunLevel", ns).text == "LeastPrivilege"
    assert root.find(".//t:RestartOnFailure/t:Interval", ns).text == "PT1M"
    assert root.find(".//t:RestartOnFailure/t:Count", ns).text == "3"
    assert root.find(".//t:Exec/t:Command", ns).text == r"C:\Tools\cairnmind.exe"
    assert "serve" in root.find(".//t:Exec/t:Arguments", ns).text


def test_task_name_is_deterministic_and_unique_per_data_dir(tmp_path):
    a = Settings(data_dir=tmp_path / "one")
    b = Settings(data_dir=tmp_path / "two")
    assert service_autostart.task_name(a) == service_autostart.task_name(a)
    assert service_autostart.task_name(a) != service_autostart.task_name(b)
    assert service_autostart.task_name(a).startswith("CairnmindService-")


def test_install_status_remove_lifecycle_with_mocked_schtasks(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data")
    monkeypatch.setattr(service_autostart.sys, "platform", "win32")
    monkeypatch.setattr(service_autostart, "_schtasks_path", lambda: "schtasks.exe")
    monkeypatch.setattr(service_autostart.shutil, "which", lambda name: r"C:\Tools\cairnmind.exe")

    registered = {}

    def fake_run(args):
        if args[0] == "/Create":
            tn = args[args.index("/TN") + 1]
            xml_path = args[args.index("/XML") + 1]
            with open(xml_path, "rb") as f:
                registered[tn] = f.read().decode("utf-16")
            return subprocess.CompletedProcess(args, 0, "SUCCESS", "")
        if args[0] == "/Query":
            tn = args[args.index("/TN") + 1]
            if tn in registered:
                return subprocess.CompletedProcess(args, 0, "TaskName: \\" + tn + "\nStatus: Ready\n", "")
            return subprocess.CompletedProcess(args, 1, "", "ERROR: The system cannot find the file specified.")
        if args[0] == "/Delete":
            tn = args[args.index("/TN") + 1]
            registered.pop(tn, None)
            return subprocess.CompletedProcess(args, 0, "SUCCESS", "")
        raise AssertionError(args)

    monkeypatch.setattr(service_autostart, "_run_schtasks", fake_run)

    before = service_autostart.status(settings)
    assert before["supported"] is True and before["installed"] is False

    installed = service_autostart.install(settings)
    assert installed["installed"] is True and installed["state"] == "Ready"
    assert installed["restart_on_failure"] == {"interval": "PT1M", "count": 3}

    after = service_autostart.remove(settings)
    assert after["installed"] is False and after["state"] == "not_installed"


def test_non_windows_reports_unsupported_and_refuses_install(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data")
    monkeypatch.setattr(service_autostart.sys, "platform", "linux")
    st = service_autostart.status(settings)
    assert st["supported"] is False and st["installed"] is False
    with pytest.raises(ValueError, match="Windows"):
        service_autostart.install(settings)
    with pytest.raises(ValueError, match="Windows"):
        service_autostart.remove(settings)


@pytest.mark.skipif(sys.platform != "win32", reason="Real Windows Task Scheduler check")
def test_real_windows_status_is_nonmutating(tmp_path):
    settings = Settings(data_dir=tmp_path / "never-registered")
    st = service_autostart.status(settings)
    assert st["supported"] is True and st["installed"] is False
    assert not settings.data_dir.exists()
