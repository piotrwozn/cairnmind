"""scripts/measure_service_spend.py reads /proc text and runs a child; no engine, service or model is started."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys

import pytest

SPEC = importlib.util.spec_from_file_location(
    "measure_service_spend", Path(__file__).resolve().parents[1] / "scripts" / "measure_service_spend.py")
script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(script)

# A name with a space and a ")" must not shift the fields; utime 250 and stime 50 ticks at 100 a second.
STAT = "42 (cairn mind) worker) S 1 42 42 0 -1 4194304 100 0 0 0 250 50 0 0 20 0 5 0 1000 1000 1000"
STATUS = "Name:\tcairnmind\nVmHWM:\t  204800 kB\nVmRSS:\t  102400 kB\n"
SAFE = {"CAIRNMIND_HOME": "/tmp/x", "CAIRNMIND_PORT": "18765", "CAIRNMIND_ARCADE_URL": "http://127.0.0.1:12480"}
NEEDS_PROC = pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="no /proc on this system (macOS): the service's figures cannot be read")


def test_sample_proc_text_gives_cpu_seconds_and_peak_memory():
    assert script.service_figures(STAT, STATUS, 100) == (3.0, 200.0)


@NEEDS_PROC
def test_a_real_process_can_be_read_from_proc():
    cpu, peak = script.read_service(os.getpid())
    assert cpu >= 0 and peak > 0


def test_without_a_service_the_row_says_not_read(capsys, monkeypatch):
    for key, value in SAFE.items():
        monkeypatch.setenv(key, value)
    assert script.main(["--", sys.executable, "-c", "sum(range(10**6))"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("| `") and line.endswith("| not read | not read |") and line.count("|") == 4


@NEEDS_PROC
def test_the_service_figures_are_the_delta_while_the_command_ran(capsys, monkeypatch):
    for key, value in SAFE.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(script, "serves", lambda cmdline, home: True)
    assert script.main(["--service-pid", str(os.getpid()), "--queue-seconds", "0.1", "--", sys.executable, "-c", "pass"]) == 0
    line = capsys.readouterr().out.strip()
    assert "not read" not in line and line.count(" s, ") == 2 and line.endswith(" MB |")


def test_the_row_carries_both_figures():
    assert script.row(["x"], 1.25, (3.0, 200.0), (0.5, 210.0)) == "| `x` (1.2 s) | 3.0 s, 200 MB | 0.5 s, 210 MB |"


def test_only_the_service_of_this_directory_is_accepted():
    home = Path("/tmp/x").resolve()
    assert script.serves(b"python\0-m\0cairnmind\0--data-dir\0/tmp/x\0serve\0", home)
    assert script.serves(b"python\0-m\0cairnmind\0--data-dir=/tmp/x\0serve\0", home)
    assert not script.serves(b"python\0-m\0cairnmind\0--data-dir\0/tmp/y\0serve\0", home)
    assert not script.serves(b"python\0-m\0cairnmind\0serve\0", home)
    assert not script.serves(b"python\0-m\0cairnmind\0--data-dir\0/tmp/x\0status\0", home)


@pytest.mark.parametrize("change", [
    {"CAIRNMIND_PORT": "8765"}, {"CAIRNMIND_PORT": "08765"}, {"CAIRNMIND_PORT": " 8765"}, {"CAIRNMIND_PORT": "x"},
    {"CAIRNMIND_HOME": None}, {"CAIRNMIND_HOME": "~/.local/share/cairnmind"}, {"CAIRNMIND_HOME": "~/.local/share/devbrain/"},
    {"CAIRNMIND_ARCADE_URL": "http://127.0.0.1:2480/api"}, {"CAIRNMIND_ARCADE_URL": "http://127.0.0.1:x"},
    {"CAIRNMIND_ARCADE_CONNECT_URL": "http://db:2480"}])
def test_it_refuses_what_could_be_the_installation(capsys, monkeypatch, change):
    for key, value in {**SAFE, **change}.items():
        monkeypatch.delenv(key, raising=False) if value is None else monkeypatch.setenv(key, value)
    assert script.main(["--", "true"]) == 2
    assert capsys.readouterr().out == ""


def test_it_refuses_a_command_with_its_own_data_dir(monkeypatch):
    for key, value in SAFE.items():
        monkeypatch.setenv(key, value)
    assert script.main(["--", "python", "-m", "cairnmind", "--data-dir", "/x", "index"]) == 2
    assert script.main(["--", "python", "-m", "cairnmind", "--data-dir=/x", "index"]) == 2


@NEEDS_PROC
def test_a_pid_that_is_not_the_service_of_this_directory_is_refused(monkeypatch):
    for key, value in SAFE.items():
        monkeypatch.setenv(key, value)
    assert script.main(["--service-pid", str(os.getpid()), "--", "true"]) == 2
