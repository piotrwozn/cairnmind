"""CI runs the suite in parts (`--shard I/N`). The parts must be disjoint and together be the whole suite,
whoever does the split: the xdist controller, a worker, or another job of the same run."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from conftest import in_shard, shard_of

IDS = [f"tests/test_module_{m}.py::test_case_{c}[{p}]" for m in range(40) for c in range(25) for p in ("en", "pl")]


@pytest.mark.parametrize("count", [1, 2, 3, 4, 7])
def test_every_test_is_in_exactly_one_part(count):
    parts = [[nodeid for nodeid in IDS if in_shard(nodeid, index, count)] for index in range(1, count + 1)]
    assert sorted(nodeid for part in parts for nodeid in part) == sorted(IDS)
    assert sum(len(part) for part in parts) == len(IDS)
    # No part is far from its share: a part twice the size of another would decide the time of the run.
    assert max(len(part) for part in parts) < 1.3 * len(IDS) / count


def test_the_option_is_checked():
    assert shard_of("") == (1, 1) and shard_of("2/4") == (2, 4)
    for wrong in ("0/4", "5/4", "2", "a/b", "2/0", "1/65", "-1/4"):
        with pytest.raises(pytest.UsageError):
            shard_of(wrong)


def test_the_parts_of_a_real_collection_add_up(tmp_path):
    """Collected by pytest itself, in separate processes as the CI jobs are."""
    def collected(*options):
        result = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
                                 "tests/test_shards.py", "tests/test_parallel_isolation.py", *options],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr
        return sorted(line for line in result.stdout.splitlines() if "::" in line)
    whole = collected()
    parts = [collected("--shard", f"{index}/3") for index in (1, 2, 3)]
    assert whole and sorted(nodeid for part in parts for nodeid in part) == whole
