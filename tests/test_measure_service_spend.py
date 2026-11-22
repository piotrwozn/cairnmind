"""scripts/measure_service_spend.py reads /proc text and runs a child; no engine, service or model is started."""
from __future__ import annotations

import importlib.util
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


def test_sample_proc_text_gives_cpu_seconds_and_peak_memory():
    assert script.service_figures(STAT, STATUS, 100) == (3.0, 200.0)


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="no /proc on this system (macOS): the service's figures cannot be read")
def test_a_real_process_can_be_read_from_proc():
    cpu, peak = script.read_service(1)
    assert cpu >= 0 and peak > 0


def test_a_stand_in_child_is_measured_and_one_row_is_printed(capsys, monkeypatch):
    for key, value in SAFE.items():
        monkeypatch.setenv(key, value)
    assert script.main(["--", sys.executable, "-c", "sum(range(10**6))"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("| `") and line.endswith("| not read | not read |") and line.count("|") == 7


def test_the_row_carries_the_service_figures():
    assert script.row(["x"], 1.25, (2.0, 30.0), (3.0, 200.0)) == "| `x` | 1.2 s | 2.0 s | 30 MB | 3.0 s | 200 MB |"


def test_it_refuses_the_installations_port_and_a_missing_directory(capsys, monkeypatch):
    for key, value in SAFE.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("CAIRNMIND_PORT", "8765")
    assert script.main(["--", "true"]) == 2
    monkeypatch.delenv("CAIRNMIND_HOME")
    monkeypatch.setenv("CAIRNMIND_PORT", "18765")
    assert script.main(["--", "true"]) == 2
    assert capsys.readouterr().out == ""
