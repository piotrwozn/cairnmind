"""`memory status` shows the package against its publication limits and warns near them (#191)."""
from __future__ import annotations

import pytest

from cairnmind import memory as portable
from cairnmind import memory_size
from cairnmind.memory import MemoryManager
from cairnmind.store import Store
from cairnmind.terminal_ui import summary_text

SECRET = "SECRET-PASSAGE-191"


@pytest.fixture(autouse=True)
def empty_cache():
    memory_size._cache.clear()
    yield
    memory_size._cache.clear()


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    store = Store(tmp_path / "database")
    found = store.add_project("sized", root)
    pid = found["id"]
    first = store.snapshot(pid, "note://a", SECRET + " one", "note")
    store.snapshot(pid, "note://a", SECRET + " two", "note")
    store.snapshot(pid, "agent://s1", SECRET + " " + "turn " * 400, "agent_transcript")
    store.remember(pid, {"kind": "fact", "content": SECRET + " fact"}, [first["id"]])
    return store, pid, MemoryManager(store)


def test_status_shows_rows_and_bytes_against_each_limit(project):
    store, pid, manager = project
    size = manager.status(pid)["package_size"]
    captured, _ = manager._capture(pid)
    assert size["measured"] == "estimate"
    assert size["rows"] == {"used": sum(captured["counts"].values()), "limit": portable.MAX_ROWS,
                            "fraction": round(sum(captured["counts"].values()) / portable.MAX_ROWS, 4)}
    assert size["bytes"]["limit"] == portable.MAX_TOTAL_BYTES and size["bytes"]["used"] > 0
    assert size["manifest_bytes"]["limit"] == portable.MAX_MANIFEST_BYTES
    assert size["warning"] is None
    # The estimate is of the size of a real package: not smaller than the objects, not many times larger.
    path = manager.snapshot(pid)["path"]
    from pathlib import Path
    real = sum(f.stat().st_size for f in Path(path).parent.rglob("*.json"))
    assert 0.5 * real <= size["bytes"]["used"] <= 2.5 * real


def test_sources_are_split_into_replaced_versions_and_transcripts(project):
    _, pid, manager = project
    sources = manager.status(pid)["package_size"]["sources_bytes"]
    assert sources["replaced"] > 0 and sources["transcripts"] > 2000
    assert sources["current"] > sources["transcripts"] * 0.5


@pytest.mark.parametrize("limit, warned", [(10, True), (1000, False)])
def test_warns_from_the_set_fraction_of_any_limit(project, monkeypatch, limit, warned):
    store, pid, manager = project
    monkeypatch.setattr(portable, "MAX_ROWS", limit)
    size = manager.status(pid)["package_size"]
    assert (size["warning"] == ["rows"]) is warned
    assert (size["rows"]["fraction"] >= memory_size.WARN_FRACTION) is warned


def test_a_byte_limit_warns_too_and_the_manifest_limit_is_its_own(project, monkeypatch):
    _, pid, manager = project
    monkeypatch.setattr(portable, "MAX_TOTAL_BYTES", 8000)
    monkeypatch.setattr(portable, "MAX_MANIFEST_BYTES", 10 ** 9)
    assert manager.status(pid)["package_size"]["warning"] == ["bytes"]
    memory_size._cache.clear()
    monkeypatch.setattr(portable, "MAX_TOTAL_BYTES", 10 ** 9)
    monkeypatch.setattr(portable, "MAX_MANIFEST_BYTES", 6000)
    assert manager.status(pid)["package_size"]["warning"] == ["manifest_bytes"]


def test_the_behaviour_at_the_limit_is_unchanged(project, monkeypatch):
    _, pid, manager = project
    monkeypatch.setattr(portable, "MAX_ROWS", 3)
    assert manager.status(pid)["package_size"]["warning"] == ["rows"]
    with pytest.raises(ValueError, match="exceeds its publication byte or row budget"):
        manager.snapshot(pid)


def test_measuring_returns_only_numbers_and_is_kept_for_a_while(project, monkeypatch):
    store, pid, manager = project
    statements = []
    original = store.query

    def recording(sql, params=None):
        statements.append(sql)
        return original(sql, params)

    monkeypatch.setattr(store, "query", recording)
    clock = [100.0]
    args = dict(max_rows=10, max_bytes=10 ** 6, max_manifest_bytes=10 ** 6, now=lambda: clock[0])
    first = memory_size.package_size(store, pid, portable.TABLES, **args)
    assert statements and all("count(*)" in s for s in statements)
    assert SECRET not in repr(first)
    seen = len(statements)
    assert memory_size.package_size(store, pid, portable.TABLES, **args) is first and len(statements) == seen
    memory_size.package_size(store, pid, portable.TABLES, fresh=True, **args)
    assert len(statements) == 2 * seen
    clock[0] += memory_size.CACHE_SECONDS + 1
    memory_size.package_size(store, pid, portable.TABLES, **args)
    assert len(statements) == 3 * seen


def test_a_store_that_cannot_be_measured_leaves_status_working(project, monkeypatch):
    store, pid, manager = project

    def broken(*_):
        raise RuntimeError("down")

    monkeypatch.setattr(memory_size, "_measure", broken)
    assert manager.status(pid)["package_size"] is None


def test_the_terminal_shows_the_figures_and_the_warning_in_both_languages():
    size = {"rows": {"used": 800, "limit": 1000, "fraction": .8}, "bytes": {"used": 3 * 1048576, "limit": 512 * 1048576},
            "warning": ["rows", "manifest_bytes", "<script>"]}
    english = summary_text({"package_size": size}, "en")
    assert "800 of 1000 rows, 3.0 MB of 512 MB" in english and "(rows, manifest bytes)" in english
    polish = summary_text({"package_size": size}, "pl")
    assert "800 z 1000 wierszy" in polish and "(wiersze, bajty manifestu)" in polish
    quiet = summary_text({"package_size": {**size, "warning": None}}, "en")
    assert "Near" not in quiet
    assert summary_text({"package_size": "garbage"}, "en") == "Operation completed."
