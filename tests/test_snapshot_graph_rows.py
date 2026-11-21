"""A snapshot carries memory, not the code graph, which is rebuilt from the files (#261).

The acceptance run of 0.12.0 refused a snapshot of a real repository because its manifest named about
150,000 graph rows. The graph follows from the files, so a snapshot does not carry it; the links between
records, sources and evidence (and a record's link to a code node) are memory and stay.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from cairnmind import memory as portable
from cairnmind.ingest import index_project
from cairnmind.memory import MemoryManager, inspect_snapshot
from cairnmind.memory_view import show_snapshot
from cairnmind.store import Store
from cairnmind.terminal_ui import summary_text

CODE_NODES = ("file", "symbol", "external_module", "unresolved_reference")
CODE_EDGES = ("imports", "defines", "calls", "references", "test_imports", "snapshot_of")
FILES = 40


@pytest.fixture
def origin(tmp_path):
    root = tmp_path / "origin"
    root.mkdir()
    for number in range(FILES):
        (root / f"mod{number}.py").write_text(f"def f{number}():\n    return g{number}()\n\ndef g{number}():\n    return {number}\n")
    store = Store(tmp_path / "origin-database")
    pid = store.add_project("big", root)["id"]
    index_project(store, pid)
    evidence = store.snapshot(pid, "note://decision", "Keep f1 in mod1.py.", "note")
    record = store.remember(pid, {"kind": "decision", "key": "where", "content": "Keep f1 in mod1.py."}, [evidence["id"]])
    symbol = store.query("SELECT id FROM nodes WHERE project_id=:p AND kind='symbol' AND label='f1'", {"p": pid})[0]["id"]
    link = {"id": hashlib.sha256(b"link").hexdigest(), "project_id": pid, "from_id": f"record:{record['id']}",
            "to_id": symbol, "kind": "relates_to", "source_id": evidence["id"], "status": "proposed"}
    store.upsert_edge(link)
    return {"store": store, "pid": pid, "root": root, "manager": MemoryManager(store), "link": link, "symbol": symbol,
            "record": record}


def written(result):
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    base = Path(result["manifest_path"]).parent
    return manifest, {table: [json.loads((base / item["path"]).read_text()) for item in items]
                      for table, items in manifest["objects"].items()}


def target_of(tmp_path, origin):
    """Another machine: the same files in another folder, and an empty database."""
    root = tmp_path / "target"
    root.mkdir()
    for file in origin["root"].glob("*.py"):
        (root / file.name).write_text(file.read_text())
    return Store(tmp_path / "target-database"), root


def test_a_repository_whose_graph_passes_the_manifest_limit_is_still_snapshotted(origin, monkeypatch):
    # Graph rows made 150,000 of the rows of this repository's own snapshot; here they are most of 380.
    store, pid = origin["store"], origin["pid"]
    graph = store.query("SELECT count(*) AS n FROM edges WHERE project_id=:p AND kind='defines'", {"p": pid})[0]["n"]
    assert graph >= 2 * FILES
    monkeypatch.setattr(portable, "MAX_MANIFEST_BYTES", 40_000)
    result = origin["manager"].snapshot(pid)
    manifest, rows = written(result)
    assert result["status"] == "created" and len(Path(result["manifest_path"]).read_bytes()) < 40_000
    assert manifest["counts"] == {table: len(items) for table, items in rows.items()}
    assert not {row["kind"] for row in rows["edges"]} & set(CODE_EDGES)
    # The one code node left is the one a record is linked to.
    assert [row["id"] for row in rows["nodes"] if row["kind"] in CODE_NODES] == [origin["symbol"]]
    assert {"relates_to", "supported_by"} <= {row["kind"] for row in rows["edges"]}
    assert {row["kind"] for row in rows["nodes"]} >= {"decision", "source"}


def test_a_snapshot_over_a_limit_names_it_with_the_size_and_the_limit(origin, monkeypatch):
    for name, label in (("MAX_MANIFEST_BYTES", "manifest bytes"), ("MAX_ROWS", "rows"), ("MAX_TOTAL_BYTES", "total bytes")):
        monkeypatch.setattr(portable, name, 10)
        with pytest.raises(ValueError, match=rf"exceeds its publication byte or row budget: .*\b{label} [\d,]+ over the limit of 10\b"):
            origin["manager"].snapshot(origin["pid"])
        monkeypatch.undo()
    monkeypatch.setattr(portable, "MAX_OBJECT_BYTES", 10)
    with pytest.raises(ValueError, match=r"object bytes [\d,]+ over the limit of 10\b"):
        origin["manager"].snapshot(origin["pid"])


def test_preview_warns_before_the_write_with_exact_figures(origin, monkeypatch):
    quiet = origin["manager"].preview(origin["pid"])["package_size"]
    assert quiet["warning"] is None and quiet["over"] is None
    monkeypatch.setattr(portable, "MAX_MANIFEST_BYTES", 10_000)
    preview = origin["manager"].preview(origin["pid"])
    size = preview["package_size"]
    assert size["over"] == ["manifest_bytes"] and "manifest_bytes" in size["warning"]
    assert size["manifest_bytes"]["used"] > 10_000 and size["manifest_bytes"]["limit"] == 10_000
    assert size["rows"]["used"] == sum(preview["counts"].values())
    assert not (origin["root"] / ".memory").exists()
    english, polish = summary_text(preview, "en"), summary_text(preview, "pl")
    assert "Over the publication limit (manifest bytes)" in english
    assert "Powyżej limitu publikacji (bajty manifestu)" in polish


def test_status_counts_what_a_snapshot_would_write(origin):
    size = origin["manager"].status(origin["pid"])["package_size"]
    captured, _ = origin["manager"]._capture(origin["pid"])
    # The linked code node is the one row that the figure leaves out (an estimate, never above the real count).
    assert 0 <= sum(captured["counts"].values()) - size["rows"]["used"] <= 1


def test_an_import_without_the_graph_says_so_and_indexing_builds_it(origin, tmp_path):
    published = origin["manager"].snapshot(origin["pid"])
    store, root = target_of(tmp_path, origin)
    manager = MemoryManager(store)
    dry = manager.import_snapshot(published["manifest_path"], root, dry_run=True)
    assert dry["code_graph"]["state"] == "not_built"
    result = manager.import_snapshot(published["manifest_path"], root)
    assert result["status"] == "imported" and result["code_graph"]["state"] == "not_built"
    assert "not built yet" in summary_text(result, "en") and "nie jest jeszcze zbudowany" in summary_text(result, "pl")
    assert manager.status(origin["pid"])["rebuilding"]["code_index_pending"] is True
    # Nothing but the linked node is there as code, and it is not current until the files are indexed.
    code = store.query("SELECT id,is_current FROM nodes WHERE project_id=:p AND kind IN :k", {"p": origin["pid"], "k": list(CODE_NODES)})
    assert [(row["id"], row["is_current"]) for row in code] == [(origin["symbol"], False)]
    assert not store.query("SELECT id FROM edges WHERE project_id=:p AND kind IN :k", {"p": origin["pid"], "k": list(CODE_EDGES)})
    index_project(store, origin["pid"])
    files = store.query("SELECT id FROM nodes WHERE project_id=:p AND kind='file' AND is_current=true", {"p": origin["pid"]})
    assert len(files) == FILES
    assert store.query("SELECT id FROM edges WHERE project_id=:p AND kind='defines'", {"p": origin["pid"]})
    # The linked node names the file in the folder of the other machine, as in 0.12.0: it stays as it was imported.
    assert store.query("SELECT is_current FROM nodes WHERE id=:id", {"id": origin["symbol"]})[0]["is_current"] is False
    assert store.query("SELECT id FROM edges WHERE id=:id AND kind='relates_to'", {"id": origin["link"]["id"]})
    assert manager.status(origin["pid"])["rebuilding"]["code_index_pending"] is False
    assert manager.import_snapshot(published["manifest_path"], root)["status"] == "unchanged"


def test_a_snapshot_of_0_12_0_with_graph_rows_still_imports_and_shows_and_its_graph_is_rebuilt(origin, tmp_path, monkeypatch):
    # The writer of 0.12.0 put every graph row into the package: the same writer without the filter.
    monkeypatch.setattr(portable, "_without_rebuilt_graph", lambda payload: payload)
    old = origin["manager"].snapshot(origin["pid"])
    monkeypatch.undo()
    manifest, rows = written(old)
    assert any(row["kind"] == "defines" for row in rows["edges"]) and manifest["format_version"] == 1
    assert inspect_snapshot(old["manifest_path"])["status"] == "valid"
    text = show_snapshot(old["manifest_path"])
    assert "1 record" in text and "decision" in text
    store, root = target_of(tmp_path, origin)
    manager = MemoryManager(store)
    result = manager.import_snapshot(old["manifest_path"], root)
    assert result["status"] == "imported" and result["counts"] == manifest["counts"]
    assert result["code_graph"]["state"] == "not_built"
    assert [row["id"] for row in store.query("SELECT id FROM nodes WHERE project_id=:p AND kind IN :k",
                                            {"p": origin["pid"], "k": list(CODE_NODES)})] == [origin["symbol"]]
    assert not store.query("SELECT id FROM edges WHERE project_id=:p AND kind IN :k", {"p": origin["pid"], "k": list(CODE_EDGES)})
    assert {row["id"] for row in store.list_records(origin["pid"])} == {origin["record"]["id"]}
    # A store that published the earlier snapshot moves on from it with one that has no graph rows.
    again = origin["manager"].snapshot(origin["pid"])
    assert again["parent_id"] == old["snapshot_id"] and again["state_hash"] == old["state_hash"]
    assert manager.import_snapshot(again["manifest_path"], root)["status"] in {"imported", "unchanged"}
