"""scripts/measure_service_memory.py parses its arguments and reads /proc text; no engine or service is started."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

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


@pytest.mark.parametrize("bad", [["--steps", "x"], ["--steps", "8"], ["--steps", ""], ["--repeat", "0"]])
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


def test_an_installation_directory_is_refused(tmp_path):
    with pytest.raises(SystemExit):
        script.throwaway(Path.home() / ".local" / "share" / "cairnmind")
    assert script.throwaway(tmp_path / "w") == (tmp_path / "w").resolve()


def test_a_free_port_is_never_one_of_the_installation():
    assert script.free_port() not in script.FORBIDDEN_PORTS
