"""A memory directory per project inside one repository (#174), on real ArcadeDB and real Git.

Two teams register two subdirectories of one repository as two projects, beside an existing project
registered at the repository root. Each publishes into its own directory and imports only its own,
and what one project's memory writes changes no other project's code state, checks or acknowledgements.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from cairnmind.config import Settings
from cairnmind.impact import build_impact
from cairnmind.ingest import index_project
from cairnmind.inspector import Inspector
from cairnmind.memory import MemoryConflict
from cairnmind.memory_paths import is_reserved_below, nested_reserved
from cairnmind.memory_published import PUBLICATION_SWITCH, Publisher
from cairnmind.memory_published_import import PublishedImporter
from cairnmind.memory_published_parse import parse_file
from cairnmind.scope import path_states
from cairnmind.sessions import SessionManager
from cairnmind.store import Store
from cairnmind.verification import capture_code_state, run_check, verification_status

GUIDE = "# Guide\n\nThe active backend is ArcadeDB.\nNothing else stores memory.\n"
PUBLISHED = Path(".memory/cairnmind-published")


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stderr + result.stdout)
    return result.stdout


def monorepo(root):
    for team in ("a", "b"):
        folder = root / "services" / team
        (folder / "docs").mkdir(parents=True)
        (folder / "docs" / "GUIDE.md").write_text(GUIDE.replace("Guide", f"Guide of {team}"))
        (folder / "app.py").write_text(f"TEAM = {team!r}\n")
    (root / "README.md").write_text("# Monorepo\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Ada Nowak")
    git(root, "config", "user.email", "team@example.invalid")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    return root


def reviewed(store, project_id, text, key):
    """A decision quoting the project's indexed guide, confirmed by a person."""
    guide = next(row for row in store.query(
        "SELECT id,metadata FROM sources WHERE project_id=:project AND is_current=true", {"project": project_id})
        if row["metadata"].get("relative_path") == "docs/GUIDE.md")
    note = store.snapshot(project_id, f"note://{key}", text, "note")
    record = store.remember(project_id, {"kind": "decision", "key": key, "content": text,
                                         "metadata": {"evidence": "The active backend is ArcadeDB."}},
                            [note["id"], guide["id"]])
    return Inspector(store).review(project_id, record["id"], "confirm", record["version"])["record"]


@pytest.fixture
def teams(tmp_path, monkeypatch):
    monkeypatch.setenv(PUBLICATION_SWITCH, "1")
    root = monorepo(tmp_path / "repository")
    store = Store(tmp_path / "database")
    a = store.add_project("team-a", root / "services" / "a")
    b = store.add_project("team-b", root / "services" / "b")
    return {"store": store, "root": root, "a": a, "b": b, "tmp": tmp_path}


def test_nested_reserved_names_only_the_directories_of_registered_roots_below(tmp_path):
    class Rows:
        def query(self, sql, params=None):
            return [{"path": str(tmp_path / "services" / "a")}, {"path": str(tmp_path)},
                    {"path": str(tmp_path.parent / "elsewhere")}]
    nested = nested_reserved(Rows(), tmp_path)
    assert nested == (Path("services/a/.memory/cairnmind"), Path("services/a/.memory/cairnmind-published"),
                      Path("services/a/.memory/devbrain"))
    assert is_reserved_below("services/a/.memory/cairnmind-published/decision/x.md", nested)
    assert is_reserved_below("services/a/.memory/cairnmind/manifest.json", nested)
    assert not is_reserved_below("services/b/.memory/cairnmind-published/decision/x.md", nested)
    assert not is_reserved_below("services/a/.memory/notes.md", nested)
    assert not is_reserved_below("services/a/app.py", nested)
    # The reserved directories of the root itself are is_reserved_memory's, unchanged.
    assert not is_reserved_below(".memory/cairnmind-published/x.md", nested)
    assert nested_reserved(Rows(), tmp_path / "services" / "a") == ()


def test_each_project_publishes_into_its_own_directory_with_paths_relative_to_it(teams):
    store, root, a, b = teams["store"], teams["root"], teams["a"], teams["b"]
    for project in (a, b):
        index_project(store, project["id"])
    decision = reviewed(store, a["id"], "Team A stores memory in ArcadeDB.", "storage")
    reviewed(store, b["id"], "Team B stores memory in ArcadeDB too.", "storage")
    first = Publisher(store).publish(a["id"])
    Publisher(store).publish(b["id"])
    assert first["destination"] == str(root / "services" / "a" / PUBLISHED)
    assert not (root / ".memory").exists()
    folder = root / "services" / "a" / PUBLISHED
    model = parse_file((folder / f"decision/{decision['id']}.md").read_bytes())
    paths = [source["ref"]["path"] for source in model["sources"].values() if source["ref"]["kind"] == "document"]
    # Relative to the project's directory, not the repository's, so the project can move inside the repository.
    assert paths == ["docs/GUIDE.md"]
    assert model["revisions"][0]["quote"]["lines"] == [3, 3]
    assert json.loads((folder / "FORMAT.json").read_text())["project_id"] == a["id"]
    assert json.loads((root / "services" / "b" / PUBLISHED / "FORMAT.json").read_text())["project_id"] == b["id"]
    assert Publisher(store).publish(a["id"])["status"] == "unchanged"


def test_each_project_imports_only_its_own_directory(teams):
    store, root, a, b, tmp = teams["store"], teams["root"], teams["a"], teams["b"], teams["tmp"]
    for project in (a, b):
        index_project(store, project["id"])
    mine = reviewed(store, a["id"], "Team A stores memory in ArcadeDB.", "storage")
    theirs = reviewed(store, b["id"], "Team B stores memory in ArcadeDB too.", "storage")
    Publisher(store).publish(a["id"])
    Publisher(store).publish(b["id"])
    git(root, "add", "-A")
    git(root, "commit", "-qm", "memory of both teams")
    clone = tmp / "clone"
    git(tmp, "clone", "-q", str(root), str(clone))
    other = Store(tmp / "clone-database")
    a2 = other.add_project("team-a", clone / "services" / "a")
    a_folder, b_folder = (clone / "services" / team / PUBLISHED for team in ("a", "b"))
    # Team B is not registered on this machine: its directory is still not team A's to import.
    with pytest.raises(MemoryConflict, match="each project imports only its own"):
        PublishedImporter(other).import_published(b_folder, a2["id"])
    assert other.list_records(a2["id"]) == []
    assert other.query("SELECT publication_id FROM projects WHERE id=:id", {"id": a2["id"]})[0].get("publication_id") is None
    imported = PublishedImporter(other).import_published(a_folder, a2["id"])
    assert imported["inserted"] == [f"decision/{mine['id']}.md"]
    b2 = other.add_project("team-b", clone / "services" / "b")
    with pytest.raises(MemoryConflict, match="publishes as"):
        PublishedImporter(other).import_published(b_folder, a2["id"])
    # Not even by replacing the binding on purpose.
    with pytest.raises(MemoryConflict, match="each project imports only its own"):
        PublishedImporter(other).import_published(b_folder, a2["id"], bind=True)
    assert PublishedImporter(other).import_published(b_folder, b2["id"])["inserted"] == [f"decision/{theirs['id']}.md"]
    assert [r["id"] for r in other.list_records(a2["id"])] == [mine["id"]]
    assert [r["id"] for r in other.list_records(b2["id"])] == [theirs["id"]]


def profile(folder, name="unit"):
    script = folder / f"check_{name}.py"
    script.write_text("print('checked')\n")
    config = folder / ".cairnmind" / "checks.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text(f"[checks.{name}]\nargv = {json.dumps([sys.executable, str(script)])}\ncwd = \".\"\n")


def checked_task(store, project_id):
    source = store.snapshot(project_id, "fixture://task", "Run the check", "manual")
    return store.remember(project_id, {"kind": "task", "content": "Implement the team's part", "metadata": {
        "state": "needs_test", "requires_test": True, "required_profiles": ["unit"]}}, [source["id"]])


def test_one_project_s_memory_stales_no_other_project_s_checks_or_acknowledgements(teams):
    store, root, a, b = teams["store"], teams["root"], teams["a"], teams["b"]
    whole = store.add_project("monorepo", root)  # an existing project at the repository root
    profile(root / "services" / "b")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "check profile of team B")
    for project in (a, b, whole):
        index_project(store, project["id"])
    task = checked_task(store, b["id"])
    run = run_check(store, Settings.load(store.data_dir), b["id"], "unit", task["id"])
    assert run["passed"] and run["code_before"]["status"] == "fresh", run["code_before"]["unknowns"]
    current = next(r for r in store.list_records(b["id"]) if r["id"] == task["id"])
    assert verification_status(store, current)["status"] == "fresh"
    manager = SessionManager(store, store.settings)
    sessions = {}
    for project in (b, whole):
        ready = manager.prepare(project["id"], "codex", timeout=0)
        assert ready["status"] == "ready", ready["unknowns"]
        manager.ack(ready["session_id"], ready["packet"]["id"], ready["packet"]["hash"], {"plan": None, "task": None})
        sessions[project["id"]] = ready["session_id"]
    before = {p["id"]: capture_code_state(store, p["id"]) for p in (a, b, whole)}
    scope_before = path_states(store, whole["id"])
    impact_before = build_impact(store, whole["id"], "HEAD")["changed_files"]
    assert all(state["status"] == "fresh" for state in before.values()), before
    reviewed(store, a["id"], "Team A stores memory in ArcadeDB.", "storage")
    archive = root / "services" / "a" / ".memory" / "cairnmind" / "objects"
    archive.mkdir(parents=True)
    (archive / "snapshot.json").write_text("{}")  # what `memory snapshot` of team A leaves
    assert Publisher(store).publish(a["id"])["status"] == "written"
    after = {p["id"]: capture_code_state(store, p["id"]) for p in (a, b, whole)}
    # Not even team A's own code state moves: its memory directory is reserved for it as before.
    assert {k: v["fingerprint"] for k, v in after.items()} == {k: v["fingerprint"] for k, v in before.items()}
    assert verification_status(store, current)["status"] == "fresh"
    for session_id in sessions.values():
        manager.require_ack(session_id)
    assert path_states(store, whole["id"])["paths"].keys() == scope_before["paths"].keys()
    index_project(store, whole["id"])
    indexed = {row["metadata"].get("relative_path") for row in store.query(
        "SELECT metadata FROM sources WHERE project_id=:project AND is_current=true", {"project": whole["id"]})}
    assert "services/a/app.py" in indexed
    assert not any(".memory/" in str(path) for path in indexed)
    assert build_impact(store, whole["id"], "HEAD")["changed_files"] == impact_before
    # Code is still code: an edit in team A's directory stales the root project, and only the projects that hold it.
    (root / "services" / "a" / "app.py").write_text("TEAM = 'A'\n")
    edited = {p["id"]: capture_code_state(store, p["id"])["fingerprint"] for p in (a, b, whole)}
    assert edited[b["id"]] == before[b["id"]]["fingerprint"]
    assert edited[a["id"]] != before[a["id"]]["fingerprint"]
    assert edited[whole["id"]] != before[whole["id"]]["fingerprint"]


def test_a_memory_directory_of_no_registered_project_is_ordinary_files(teams):
    store, root = teams["store"], teams["root"]
    whole = store.add_project("monorepo", root)
    before = capture_code_state(store, whole["id"])
    # Not a registered project's: the rule applies to registered projects' directories only.
    stray = root / "vendor" / "tool" / PUBLISHED
    stray.mkdir(parents=True)
    (stray / "notes.md").write_text("Somebody's notes\n")
    after = capture_code_state(store, whole["id"])
    assert after["status"] == "fresh" and after["fingerprint"] != before["fingerprint"]
    assert "vendor/tool/.memory/cairnmind-published/notes.md" in path_states(store, whole["id"])["paths"]


def test_an_existing_project_at_the_repository_root_keeps_its_directory(teams):
    store, root = teams["store"], teams["root"]
    whole = store.add_project("monorepo", root)
    index_project(store, whole["id"])
    readme = next(row for row in store.query(
        "SELECT id,metadata FROM sources WHERE project_id=:project AND is_current=true", {"project": whole["id"]})
        if row["metadata"].get("relative_path") == "README.md")
    record = store.remember(whole["id"], {"kind": "decision", "key": "layout", "content": "One repository."},
                            [readme["id"]])
    Inspector(store).review(whole["id"], record["id"], "confirm", record["version"])
    result = Publisher(store).publish(whole["id"])
    assert result["destination"] == str(root / PUBLISHED)
    model = parse_file((root / PUBLISHED / "decision" / f"{record['id']}.md").read_bytes())
    assert [s["ref"]["path"] for s in model["sources"].values() if s["ref"]["kind"] == "document"] == ["README.md"]
    before = capture_code_state(store, whole["id"])
    (root / PUBLISHED / "decision" / f"{record['id']}.md").write_text("changed by hand\n")
    assert capture_code_state(store, whole["id"])["fingerprint"] == before["fingerprint"]
