"""scripts/measure_service_memory.py parses its arguments and reads /proc text; no engine or service is started."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import own_time

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "measure_service_memory.py"
SPEC = importlib.util.spec_from_file_location("measure_service_memory", SCRIPT)
script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(script)


def test_it_starts_and_says_how_to_use_it():
    done = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and "--steps" in done.stdout and "--quick" in done.stdout


def test_the_defaults_run_every_step_and_quick_shortens_the_series():
    args = script.parse_args([])
    assert args.step_list == [1, 2, 3, 5, 6, 7, 4] and (args.repeat, args.idle, args.long_idle) == (20, 60, 1800)
    quick = script.parse_args(["--quick", "--steps", "1,3"])
    assert quick.step_list == [1, 3] and (quick.repeat, quick.idle, quick.long_idle) == (3, 5, 30)


@pytest.mark.parametrize("bad", [["--steps", "x"], ["--steps", "9"], ["--steps", ""], ["--repeat", "0"]])
def test_a_wrong_argument_is_refused(bad):
    with pytest.raises(SystemExit) as stopped:
        script.parse_args(bad)
    assert stopped.value.code == 2


def test_proc_status_text_gives_resident_peak_and_threads():
    text = "Name:\tpython\nVmHWM:\t  204800 kB\nVmRSS:\t  102400 kB\nThreads:\t7\n"
    assert script.memory_figures(text) == (100.0, 200.0, 7)


def test_the_growth_per_repetition_is_the_slope_of_the_readings():
    assert script.slope([100.0, 102.0, 104.0, 106.0]) == pytest.approx(2.0)
    assert script.slope([100.0] * 5) == 0 and script.slope([100.0]) == 0


def test_the_growth_of_step_six_counts_the_graph_requests_only(monkeypatch):
    measurement = object.__new__(script.Measurement)
    measurement.args, measurement.steps = script.parse_args(["--repeat", "20"]), []
    monkeypatch.setattr(measurement, "sample", lambda full=False: {"rss_mb": 1.0})
    own_time(monkeypatch, script, sleep=lambda seconds: None)
    # 20 flat status readings, then 20 graph readings rising 3 MB each: the slope is 3, not about half of it
    measurement.run_step(6, lambda readings: readings.extend([300.0] * 20 + [300.0 + 3 * n for n in range(20)]), count=20, idle=0)
    assert measurement.steps[0]["growth_per_repetition_mb"] == 3.0


def test_the_requests_are_summed_up_in_blocks_of_fifty_with_their_median_and_worst_time():
    blocks = script.request_blocks([float(100 + n) for n in range(120)], [float(n) for n in range(1, 121)])
    assert blocks == {"request_blocks_mb": [124.5, 174.5, 209.5], "request_ms_median": 60.5, "request_ms_worst": 120.0}


def test_the_requests_of_step_six_are_graph_groups_and_neighbors_and_nothing_else():
    assert script.parse_args(["--routes", "graph,groups,neighbors"]).routes == "graph,groups,neighbors"
    assert script.parse_args(["--routes", "scoped-graph,scoped-groups,scoped-neighbors"]).second_root is False
    with pytest.raises(SystemExit):
        script.parse_args(["--routes", "graph,summary"])


def test_the_blocks_have_a_size_of_their_own_and_the_threads_at_their_ends_and_the_traces_are_asked_for_by_number():
    blocks = script.request_blocks([float(n) for n in range(250)], [1.0] * 250, 100, [10 + n // 100 for n in range(250)])
    assert blocks["request_blocks_mb"] == [49.5, 149.5, 224.5] and blocks["request_blocks_threads"] == [10, 11, 12]
    args = script.parse_args(["--block", "100", "--trace-at", "200,1800", "--second-root"])
    assert (args.block, args.trace_points, args.second_root) == (100, [200, 1800], True)
    with pytest.raises(SystemExit):
        script.parse_args(["--block", "0"])
    with pytest.raises(SystemExit):
        script.parse_args(["--trace-at", "two"])


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_the_memory_of_a_process_tree_counts_the_process_and_what_it_started():
    alone, count = script.tree_rss(os.getpid())
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with_child, with_count = script.tree_rss(os.getpid())
    finally:
        child.kill()
        child.wait()
    assert alone > 0 and with_count == count + 1 and with_child > alone


def test_an_installation_directory_is_refused(tmp_path):
    with pytest.raises(SystemExit):
        script.throwaway(Path.home() / ".local" / "share" / "cairnmind")
    assert script.throwaway(tmp_path / "w") == (tmp_path / "w").resolve()


def test_a_free_port_is_never_one_of_the_installation():
    assert script.free_port() not in script.FORBIDDEN_PORTS
