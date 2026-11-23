"""scripts/measure_snapshot.py describes the package it timed; no engine is started."""
from __future__ import annotations

import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "measure_snapshot", Path(__file__).resolve().parents[1] / "scripts" / "measure_snapshot.py")
script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(script)


def test_the_package_line_names_rows_chunks_and_bytes(tmp_path):
    package = tmp_path / "cairnmind"
    (package / "objects").mkdir(parents=True)
    (package / "manifest.json").write_text("{}" * 5)
    (package / "objects" / "a.json").write_text("x" * 90)
    result = {"path": str(package / "manifest.json"), "counts": {"sources": 1, "records": 3}}
    assert script.describe_package(result, 7) == "package: sources 1, records 3; store chunks 7; 100 bytes in 2 files"
