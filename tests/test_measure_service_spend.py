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


@NEEDS_PROC
def test_the_row_holds_the_cpu_delta_of_each_interval_and_waits_for_the_queue(capsys, monkeypatch):
    from types import SimpleNamespace
    for key, value in SAFE.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(script, "serves", lambda cmdline, home: True)
    readings = iter([(10.0, 100.0), (13.5, 150.0), (14.0, 160.0)])  # before, when the command ended, after the wait
    monkeypatch.setattr(script, "figures", lambda pid: next(readings))
    slept = []
    monkeypatch.setattr(script, "time", SimpleNamespace(monotonic=lambda: 0.0, sleep=slept.append))
    assert script.main(["--service-pid", str(os.getpid()), "--queue-seconds", "7", "--", "true"]) == 0
    assert capsys.readouterr().out.strip() == "| `true` (0.0 s) | 3.5 s, 150 MB | 0.5 s, 160 MB |"
    assert slept == [7.0]


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
    {"CAIRNMIND_ARCADE_CONNECT_URL": "http://db:2480"}, {"DEVBRAIN_ARCADE_CONNECT_URL": "http://db:2480"},
    {"CAIRNMIND_ARCADE_DATABASE": "cairnmind"}, {"DEVBRAIN_ARCADE_DATABASE": "cairnmind"},
    {"CAIRNMIND_ARCADE_URL": "https://db.example:2490"}])
def test_it_refuses_what_could_be_the_installation(capsys, monkeypatch, change):
    for key, value in {**SAFE, **change}.items():
        monkeypatch.delenv(key, raising=False) if value is None else monkeypatch.setenv(key, value)
    assert script.main(["--", "true"]) == 2
    assert capsys.readouterr().out == ""


def test_a_throwaway_database_name_or_a_local_engine_is_accepted():
    env = {**SAFE, "CAIRNMIND_ARCADE_DATABASE": "cairnmind_measure_1"}
    assert script.refuse_an_installation(env, ["true"]) is None
    assert script.refuse_an_installation({**env, "CAIRNMIND_ARCADE_URL": "https://db.example:2490"}, ["true"]) is None
    assert script.refuse_an_installation(SAFE, ["true"]) is None


def test_it_refuses_a_command_with_its_own_data_dir(capsys, monkeypatch):
    for key, value in SAFE.items():
        monkeypatch.setenv(key, value)
    # `true` with the argument: a command that merely fails with a usage error would pass for a refusal.
    for argument in (["--data-dir", "/x"], ["--data-dir=/x"]):
        assert script.main(["--", "true", *argument]) == 2
        captured = capsys.readouterr()
        assert captured.out == "" and captured.err.startswith("refused:")


@NEEDS_PROC
def test_a_pid_that_is_not_the_service_of_this_directory_is_refused(monkeypatch):
    for key, value in SAFE.items():
        monkeypatch.setenv(key, value)
    assert script.main(["--service-pid", str(os.getpid()), "--", "true"]) == 2


NOTHING = '{"files": 586, "changed": 0, "unchanged": 586, "read": 0, "graph_unchanged": true}'
CHANGED = '{"files": 586, "changed": 3, "unchanged": 583, "read": 3, "graph_unchanged": false}'


def run_printing(text, capsys, monkeypatch, *flags):
    for key, value in SAFE.items():
        monkeypatch.setenv(key, value)
    code = script.main([*flags, "--", sys.executable, "-c", f"print({text!r})"])
    return code, capsys.readouterr()


def test_an_index_that_changed_nothing_prints_no_row(capsys, monkeypatch):
    code, captured = run_printing(NOTHING, capsys, monkeypatch)
    assert code == 3 and "| `" not in captured.out and '"read": 0' in captured.out  # its output is still shown
    assert "poll_seconds" in captured.err and "settled" in captured.err


def test_an_index_that_changed_files_gets_its_row(capsys, monkeypatch):
    code, captured = run_printing(CHANGED, capsys, monkeypatch)
    assert code == 0 and captured.out.strip().endswith("| not read | not read |")


def test_a_command_that_is_not_an_index_gets_its_row(capsys, monkeypatch):
    for text in ("hello", "[]", '{"status": "ok"}'):
        code, captured = run_printing(text, capsys, monkeypatch)
        assert code == 0 and captured.out.strip().endswith("| not read | not read |")


def test_allow_unchanged_prints_the_row_anyway(capsys, monkeypatch):
    code, captured = run_printing(NOTHING, capsys, monkeypatch, "--allow-unchanged")
    assert code == 0 and captured.out.strip().endswith("| not read | not read |") and captured.err == ""
