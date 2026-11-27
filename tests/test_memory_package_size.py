"""`memory status` shows the package against its publication limits and warns near them (#191)."""
from __future__ import annotations

import pytest

from cairnmind.memory import memory as portable
from cairnmind.memory import memory_size
from cairnmind.memory.memory import MemoryManager
from cairnmind.store import Store
from cairnmind.terminal.terminal_ui import summary_text

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
    args = dict(max_rows=10, max_bytes=10 ** 6, max_manifest_bytes=10 ** 6, max_object_bytes=10 ** 6,
                now=lambda: clock[0])
    first = memory_size.package_size(store, pid, portable.TABLES, **args)
    # Only aggregates: every statement returns counts, sums or a maximum, never a row.
    assert statements and all(("count(*)" in s) or ("sum(" in s) for s in statements)
    assert not any(" * FROM" in s or "content" in s for s in statements)
    assert SECRET not in repr(first)
    assert first["age_seconds"] == 0
    seen = len(statements)
    clock[0] += 30
    again = memory_size.package_size(store, pid, portable.TABLES, **args)
    assert len(statements) == seen and again["age_seconds"] == 30
    assert {**again, "age_seconds": 0} == first
    clock[0] -= 30
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


def _real_bytes(manager, pid):
    from pathlib import Path
    path = manager.snapshot(pid)["path"]
    return sum(f.stat().st_size for f in Path(path).parent.rglob("*.json"))


def test_the_estimate_counts_utf8_bytes_not_characters(tmp_path, monkeypatch):
    """A character beyond ASCII takes two to four bytes in a snapshot; counting characters said 60% of a store
    that a snapshot refused (about half of the real size for Japanese)."""
    root = tmp_path / "repo"
    root.mkdir()
    store = Store(tmp_path / "database")
    pid = store.add_project("wide", root)["id"]
    texts = {"japanese": "日本語のテキスト。" * 3000, "cyrillic": "Привет, мир! " * 2000,
             "polish": "Zażółć gęślą jaźń. " * 1500, "emoji": "\U0001F600\U0001F680 ok " * 2000}
    for name, text in texts.items():
        store.snapshot(pid, f"note://{name}", text, "note")
    manager = MemoryManager(store)
    # A low object limit, so that the rows that can be near it are measured in bytes (see memory_size._measure).
    monkeypatch.setattr(portable, "MAX_OBJECT_BYTES", 100_000)
    size = manager.status(pid)["package_size"]
    monkeypatch.setattr(portable, "MAX_OBJECT_BYTES", 16 * 1024 * 1024)
    real = _real_bytes(manager, pid)
    assert 0.85 * real <= size["bytes"]["used"] <= 1.4 * real, (size["bytes"]["used"], real)
    # Counting characters alone would be far below: the text is mostly 3 and 4 byte characters.
    assert sum(len(t) for t in texts.values()) < 0.6 * real
    assert size["object_bytes"]["used"] >= max(len(t.encode("utf-8")) for t in texts.values())


def test_the_largest_object_is_shown_and_warned_on(project, monkeypatch):
    _, pid, manager = project
    monkeypatch.setattr(portable, "MAX_OBJECT_BYTES", 3000)
    size = manager.status(pid)["package_size"]
    assert size["object_bytes"]["limit"] == 3000 and size["object_bytes"]["used"] > 2000
    assert size["warning"] == ["object_bytes"]
    english = summary_text({"package_size": size}, "en")
    assert "Largest object:" in english and "(object bytes)" in english
    assert "bajty największego obiektu" in summary_text({"package_size": size}, "pl")


def test_an_unmeasured_package_is_said_not_left_out():
    assert "not measured" in summary_text({"package_size": None}, "en")
    assert "nie zmierzono" in summary_text({"package_size": None}, "pl")
    assert "Package" not in summary_text({"counts": {"records": 1}}, "en")


def _stopping(store, monkeypatch, when):
    """Make the engine 'stop' the statements that ``when(sql, params)`` selects, as its regex timeout does."""
    from cairnmind.storage.arcade import ArcadeError
    original = store.query

    def query(sql, params=None):
        if "normalize" in sql and when(sql, params or {}):
            raise ArcadeError("Regular expression operation aborted (arcadedb.command.regexTimeout)")
        return original(sql, params)

    monkeypatch.setattr(store, "query", query)


def test_a_stopped_statement_is_repeated_in_parts_and_stays_exact(project, monkeypatch):
    store, pid, manager = project
    exact = manager.status(pid)["package_size"]
    memory_size._cache.clear()
    # The whole table is too much for the engine's regex time; each part (one id prefix) is not.
    _stopping(store, monkeypatch, lambda sql, params: "low" not in params and "high" not in params)
    parts = manager.status(pid)["package_size"]
    assert parts["exact_bytes"] is True and parts["bytes"] == exact["bytes"]
    assert parts["sources_bytes"] == exact["sources_bytes"]


def test_a_measurement_that_stays_stopped_is_a_marked_lower_bound(project, monkeypatch):
    store, pid, manager = project
    exact = manager.status(pid)["package_size"]
    memory_size._cache.clear()
    _stopping(store, monkeypatch, lambda sql, params: True)
    size = manager.status(pid)["package_size"]
    assert size["exact_bytes"] is False and 0 < size["bytes"]["used"] <= exact["bytes"]["used"]
    assert "can be larger" in summary_text({"package_size": size}, "en")
    assert "może być większa" in summary_text({"package_size": size}, "pl")
    assert "can be larger" not in summary_text({"package_size": exact}, "en")


def test_a_slow_measurement_does_not_hold_the_status_and_is_found_afterwards(project, monkeypatch):
    import threading
    store, pid, manager = project
    release, calls = threading.Event(), []
    original = memory_size._measure

    def slow(*args):
        calls.append(1)
        release.wait(10)
        return original(*args)

    monkeypatch.setattr(memory_size, "_measure", slow)
    args = dict(max_rows=10 ** 6, max_bytes=10 ** 9, max_manifest_bytes=10 ** 9, max_object_bytes=10 ** 9)
    pending = memory_size.package_size(store, pid, portable.TABLES, wait=0.05, **args)
    assert pending["measured"] == "pending" and pending["warning"] is None
    assert "still being measured" in summary_text({"package_size": pending}, "en")
    assert "wciąż jest mierzony" in summary_text({"package_size": pending}, "pl")
    # Asking again joins the measurement that runs: one at a time.
    assert memory_size.package_size(store, pid, portable.TABLES, wait=0.05, **args)["measured"] == "pending"
    release.set()
    done = memory_size.package_size(store, pid, portable.TABLES, wait=10, **args)
    assert done["measured"] == "estimate" and len(calls) == 1
    assert memory_size.package_size(store, pid, portable.TABLES, wait=0, **args)["rows"] == done["rows"]
    assert len(calls) == 1


def test_a_measurement_that_took_long_is_kept_longer(project):
    store, pid, manager = project
    clock = [0.0]

    def now():
        clock[0] += 5.0  # every look at the clock is 5 s later: the measurement "took" 5 s
        return clock[0]

    args = dict(max_rows=10 ** 6, max_bytes=10 ** 9, max_manifest_bytes=10 ** 9, max_object_bytes=10 ** 9, now=now)
    memory_size.package_size(store, pid, portable.TABLES, **args)
    entry = memory_size._cache[(id(store), pid)]
    assert entry[3] == max(memory_size.CACHE_SECONDS, memory_size.CACHE_FACTOR * 5)


def test_an_old_measurement_says_how_old_it_is():
    size = {"rows": {"used": 1, "limit": 10}, "bytes": {"used": 1, "limit": 10}, "age_seconds": 45, "warning": None}
    assert "Measured 45 s ago" in summary_text({"package_size": size}, "en")
    assert "Zmierzono 45 s temu" in summary_text({"package_size": size}, "pl")
    assert "Measured" not in summary_text({"package_size": {**size, "age_seconds": 2}}, "en")
