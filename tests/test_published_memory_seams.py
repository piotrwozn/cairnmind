"""Where the parts of team memory meet (#170 to #174): the secrets gate, `memory diff` and the import of a
commit, the carriers and the memory directory of a project in a subdirectory, on real ArcadeDB and Git."""
from __future__ import annotations

from pathlib import Path
import subprocess

import pytest
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.agent_tools import ToolError, _parts
from cairnmind.evidence import evidence_states
from cairnmind.ingest import index_project
from cairnmind.inspector import Inspector
from cairnmind.memory import MemoryConflict
from cairnmind.memory_paths import nested_reserved
from cairnmind.memory_published import Publisher
from cairnmind.memory_published_carrier import configure
from cairnmind.memory_published_git import project_files, published_files
from cairnmind.memory_published_import import PublishedImporter
from cairnmind.memory_published_main import import_commit
from cairnmind.memory_published_release import checkout_rules
from cairnmind.store import Store

GUIDE = "# Guide\n\nThe active backend is ArcadeDB.\nNothing else stores memory.\n"
PUBLISHED = Path(".memory/cairnmind-published")
KEY_BLOCK = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0Z3VS5JJcds3xfn\n-----END RSA " + "PRIVATE KEY-----"


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stderr + result.stdout)
    return result.stdout.strip()


def repository(root, files=None):
    root.mkdir(parents=True)
    for relative, text in (files or {"app.py": "VALUE = 1\n"}).items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(text)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Ada Nowak")
    git(root, "config", "user.email", "ada@example.invalid")
    git(root, "add", ".")
    git(root, "commit", "-qm", "first")
    return root


def confirmed(store, project_id, kind, text, key):
    source = store.snapshot(project_id, f"note://{key}", text, "note")
    record = store.remember(project_id, {"kind": kind, "key": key, "content": text}, [source["id"]])
    return Inspector(store).review(project_id, record["id"], "confirm", record["version"])["record"]


def quoting_guide(store, project_id, text, key):
    """A decision quoting the project's indexed docs/GUIDE.md, confirmed by a person."""
    guide = next(row for row in store.query(
        "SELECT id,metadata FROM sources WHERE project_id=:project AND is_current=true", {"project": project_id})
        if row["metadata"].get("relative_path") == "docs/GUIDE.md")
    note = store.snapshot(project_id, f"note://{key}", text, "note")
    record = store.remember(project_id, {"kind": "decision", "key": key, "content": text,
                                         "metadata": {"evidence": "The active backend is ArcadeDB."}},
                            [note["id"], guide["id"]])
    return Inspector(store).review(project_id, record["id"], "confirm", record["version"])["record"]


@pytest.fixture
def carried(tmp_path):
    """A project whose published memory goes to a checkout of a separate memory repository (#173)."""
    root = repository(tmp_path / "code")
    store = Store(tmp_path / "data")
    project = store.add_project("team", root)
    memory = repository(tmp_path / "memory", {"README.md": "Team memory\n"})
    configure(store, project["id"], "checkout", str(memory))
    return {"store": store, "pid": project["id"], "root": root, "memory": memory, "tmp": tmp_path}


# (a) The secrets gate of #171 runs before every carrier of #173 and for a project in a subdirectory (#174).

def test_the_real_secrets_gate_refuses_before_a_checkout_is_written(carried):
    store, pid, memory = carried["store"], carried["pid"], carried["memory"]
    leaked = confirmed(store, pid, "decision", f"Deploy with this key.\n{KEY_BLOCK}", "deploy")
    with pytest.raises(MemoryConflict, match=f"decision/{leaked['id']}.md: private key in text"):
        Publisher(store).publish(pid)
    assert sorted(path.name for path in memory.iterdir()) == [".git", "README.md"]
    with pytest.raises(MemoryConflict, match="private key in text"):
        Publisher(store).publish(pid, preview=True)


def test_a_project_in_a_subdirectory_is_held_to_its_own_publication_rules(tmp_path):
    root = repository(tmp_path / "repository", {"services/a/app.py": "A = 1\n", "services/b/app.py": "B = 1\n",
                                                "services/a/.cairnmind/publication.toml":
                                                    '[personal]\ndetect = ["email"]\n'})
    store = Store(tmp_path / "data")
    a = store.add_project("team-a", root / "services" / "a")
    b = store.add_project("team-b", root / "services" / "b")
    mail = "Ask " + "ola.kowalska" + "@example.com about the deploy."
    held = confirmed(store, a["id"], "fact", mail, "who")
    with pytest.raises(MemoryConflict, match=f"fact/{held['id']}.md: email"):
        Publisher(store).publish(a["id"])
    assert not (root / "services" / "a" / ".memory").exists()
    # Team B's rules turn no personal data on, so the same text is its to publish; the check that CI runs on
    # each directory reads the rules of the project that holds it.
    confirmed(store, b["id"], "fact", mail, "who")
    assert Publisher(store).publish(b["id"])["status"] == "written"
    assert checkout_rules(root / "services" / "a" / PUBLISHED).personal == ("email",)
    assert checkout_rules(root / "services" / "b" / PUBLISHED).personal == ()


def test_memory_diff_and_the_import_of_a_commit_apply_the_commits_publication_rules(tmp_path):
    root = repository(tmp_path / "code")
    store = Store(tmp_path / "data")
    pid = store.add_project("team", root)["id"]
    start = git(root, "rev-parse", "HEAD")
    held = confirmed(store, pid, "fact", "Ask " + "ola.kowalska" + "@example.com about the deploy.", "who")
    Publisher(store).publish(pid)  # No rules turn personal data on yet.
    git(root, "add", "-A")
    git(root, "commit", "-qm", "Publish")
    (root / ".cairnmind").mkdir()
    (root / ".cairnmind" / "publication.toml").write_text('[personal]\ndetect = ["email"]\n')
    git(root, "add", "-A")
    git(root, "commit", "-qm", "Turn on personal data")

    def diff(head):
        return CliRunner().invoke(cli.app, ["--data-dir", str(tmp_path / "unused"), "memory", "diff", start, head,
                                            "--project-root", str(root)])

    assert diff("HEAD~1").exit_code == 0
    refused = diff("HEAD")
    assert refused.exit_code == 1 and f"fact/{held['id']}.md: email found in text" in refused.output
    assert "ola.kowalska" not in refused.output
    server = tmp_path / "server"
    git(tmp_path, "clone", "-q", str(root), str(server))
    main = Store(tmp_path / "server-data")
    main_pid = main.add_project("team", server)["id"]
    with pytest.raises(MemoryConflict, match=f"fact/{held['id']}.md: email found in text"):
        import_commit(main, main_pid, "HEAD")
    assert main.list_records(main_pid) == []
    assert import_commit(main, main_pid, "HEAD~1")["inserted"] == [f"fact/{held['id']}.md"]


# (b) `memory diff` and the import of a commit (#172) with the carriers and subdirectory projects.

def test_memory_diff_reads_a_package_in_a_separate_memory_repository(carried):
    store, pid, memory, tmp = carried["store"], carried["pid"], carried["memory"], carried["tmp"]
    start = git(memory, "rev-parse", "HEAD")
    confirmed(store, pid, "decision", "ArcadeDB is the only storage engine.", "storage")
    written = Publisher(store).publish(pid)
    identity = Path(written["destination"]).name
    git(memory, "add", "-A")
    git(memory, "commit", "-qm", "Publish")
    head = git(memory, "rev-parse", "HEAD")
    commit, files, errors = published_files(memory, head, identity)
    assert commit == head and errors == [] and "FORMAT.json" in files

    def diff(*extra):
        result = CliRunner().invoke(cli.app, ["--data-dir", str(tmp / "unused"), "memory", "diff", start, head,
                                              "--project-root", str(memory), *extra])
        assert not (tmp / "unused").exists()
        return result

    found = diff("--package", identity)
    assert found.exit_code == 0, found.output
    assert "1 decision added." in found.output and "ArcadeDB is the only storage engine." in found.output
    # Without the package the repository's own place is read, where this repository holds nothing.
    assert "1 decision added." not in diff().output
    assert diff("--package", "not-a-uuid").exit_code == 2


def test_the_import_of_a_commit_refuses_clearly_for_a_checkout_carrier(carried):
    store, pid, memory = carried["store"], carried["pid"], carried["memory"]
    with pytest.raises(ValueError, match=f"separate repository.*import-published {memory}/"):
        import_commit(store, pid, "HEAD")


def test_the_import_of_a_commit_reads_and_rechecks_a_project_in_a_subdirectory(tmp_path):
    files = {f"services/{team}/docs/GUIDE.md": GUIDE for team in ("a", "b")}
    root = repository(tmp_path / "repository", {**files, "README.md": "# Monorepo\n"})
    store = Store(tmp_path / "data")
    a = store.add_project("team-a", root / "services" / "a")
    b = store.add_project("team-b", root / "services" / "b")
    for project in (a, b):
        index_project(store, project["id"])
    mine = quoting_guide(store, a["id"], "Team A stores memory in ArcadeDB.", "storage")
    quoting_guide(store, b["id"], "Team B stores memory in ArcadeDB too.", "storage")
    Publisher(store).publish(a["id"])
    Publisher(store).publish(b["id"])
    git(root, "add", "-A")
    git(root, "commit", "-qm", "Memory of both teams")
    server = tmp_path / "server"
    git(tmp_path, "clone", "-q", str(root), str(server))
    main = Store(tmp_path / "server-data")
    a2 = main.add_project("team-a", server / "services" / "a")
    result = import_commit(main, a2["id"], "HEAD")
    assert result["inserted"] == [f"decision/{mine['id']}.md"]
    assert [item["id"] for item in result["evidence"]["anchored"]] == [mine["id"]]
    # Anchored by this commit's code, never verified, in the output and in the store.
    assert "does not verify" in result["verification"]
    row = next(r for r in main.list_records(a2["id"]) if r["id"] == mine["id"])
    assert evidence_states(main, a2["id"], [row])[mine["id"]]["evidence_state"] == "anchored"
    assert not row["metadata"].get("verified") and "verification_status" not in row["metadata"]
    # The passage gone at a later commit orphans it, read at the project's own path.
    (server / "services" / "a" / "docs" / "GUIDE.md").write_text("# Guide\n\nSomething else.\n")
    git(server, "-c", "user.name=Bo", "-c", "user.email=bo@example.invalid", "commit", "-qam", "Rewrite team A's guide")
    later = import_commit(main, a2["id"], "HEAD")
    assert [item["id"] for item in later["evidence"]["orphaned"]] == [mine["id"]]


# (c) The reserved directories: a registered project's, at the root and below it, and nothing else of that name.

def test_a_recheck_compares_a_directory_of_that_name_that_is_no_registered_projects(tmp_path):
    root = repository(tmp_path / "repository", {
        "app.py": "A = 1\n",
        ".memory/cairnmind-published/FORMAT.json": "{}\n",
        "services/a/.memory/cairnmind-published/FORMAT.json": "{}\n",
        "vendor/lib/.memory/cairnmind-published/notes.py": "QUOTED = 1\n",
        "vendor/lib/.memory/other/kept.py": "KEPT = 1\n",
    })
    store = Store(tmp_path / "data")
    store.add_project("root", root)
    store.add_project("team-a", root / "services" / "a")
    nested = nested_reserved(store, root)
    listed = set(project_files(root, git(root, "rev-parse", "HEAD"), nested))
    assert listed == {"app.py", "vendor/lib/.memory/cairnmind-published/notes.py", "vendor/lib/.memory/other/kept.py"}


# (d) Nothing imported is shown as verified: a package carried by a checkout imports unverified.

def test_a_package_from_a_checkout_imports_unverified(carried):
    store, pid, tmp = carried["store"], carried["pid"], carried["tmp"]
    record = confirmed(store, pid, "decision", "ArcadeDB is the only storage engine.", "storage")
    folder = Path(Publisher(store).publish(pid)["destination"])
    other = Store(tmp / "bo-data")
    bo = other.add_project("team", repository(tmp / "bo-code"))
    assert "nothing imported is verified" in PublishedImporter(other).import_published(folder, bo["id"])["verification"]
    rows = other.list_records(bo["id"])
    state = evidence_states(other, bo["id"], rows)[record["id"]]
    assert state["evidence_state"] == "unverified" and state["unverified_by"] == "published_elsewhere"


# (e) The built-in agent cannot write the published directory, wherever the project and however it is cased.

@pytest.mark.parametrize("path", [
    ".memory/cairnmind-published/decision/x.md",
    "services/a/.memory/cairnmind-published/FORMAT.json",
    ".Memory/Cairnmind-Published/decision/x.md",
    "services/a/.MEMORY/cairnmind-published/.gitattributes",
])
def test_the_agent_may_not_write_a_published_directory(path):
    with pytest.raises(ToolError) as refused:
        _parts(path)
    assert refused.value.code == "protected_path"
