"""scripts/store_growth.py measures the growth of the store per transcript event and per file revision (#185).

Tiny numbers against the test engine, with no model: the report must have a step for every size of event and
for the first index and every revision, and each step must have grown the store and the backup.
"""
import importlib.util
from pathlib import Path

import pytest

from cairnmind.store import Store

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "store_growth.py"


@pytest.fixture(scope="module")
def growth():
    spec = importlib.util.spec_from_file_location("store_growth", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_measuring_function_reports_growth_per_event_and_per_revision(growth, tmp_path):
    store = Store(tmp_path / "memory")
    result = growth.grow(store, tmp_path / "scratch", events=3, event_sizes=(120, 600), files=2,
                         file_sizes=(1500, 3000), revisions=2, dimension=8)
    steps = {step["step"]: step for step in result["steps"]}
    assert list(steps) == ["empty store", "3 events of 120 B", "3 events of 600 B", "first index of the files",
                           "revision 1", "revision 2"]
    assert steps["empty store"]["collections"]["embeddings"]["rows"] == 0 and "per_unit" not in steps["empty store"]
    for name in ("3 events of 120 B", "3 events of 600 B", "first index of the files"):
        added = steps[name]["per_unit"]
        # Sources, chunks, both vector copies and the backup all grew.
        assert added["sources"] > 0 and added["chunks"] > 0 and added["embeddings"] > 0, name
        assert added["vector_bytes"] > 0 and added["backup_bytes"] > 0 and added["database_bytes"] >= 0, name
    for name in ("revision 1", "revision 2"):
        # A revision replaces the chunks and vectors of the file (#186): what grows is the replaced source and the backup.
        added = steps[name]["per_unit"]
        assert added["sources"] == 1 and added["backup_bytes"] > 0 and added["jobs"] > 0, name
    assert steps["revision 2"]["collections"]["embeddings"]["rows"] == steps["revision 2"]["collections"]["chunks"]["rows"]
    assert steps["revision 2"]["units"] == 2 and steps["3 events of 600 B"]["units"] == 3
    table = growth.table(result, {"date": "2026-11-22", "commit": "abc", "engine": "26.11.1"})
    assert table.startswith("Measured 2026-11-22 on commit abc, ArcadeDB 26.11.1")
    assert table.count("\n| ") == 1 + len(steps)  # the header row and one row for each step
