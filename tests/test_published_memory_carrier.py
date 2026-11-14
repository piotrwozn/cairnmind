"""Where published memory goes (#173): the repository, or a checkout of a separate memory repository."""
from __future__ import annotations

import os
import subprocess

from fastapi.testclient import TestClient
import pytest
from typer.testing import CliRunner

from cairnmind import cli, memory_published
from cairnmind.inspector import Inspector
from cairnmind.memory import MemoryConflict
from cairnmind.memory_published import PUBLICATION_SWITCH, Publisher, publication_status
from cairnmind.memory_published_carrier import configure
from cairnmind.memory_published_import import PublishedImporter
from cairnmind.service import create_app
from cairnmind.store import Store


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def repository(root):
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Ada Nowak")
    git(root, "config", "user.email", "ada@example.invalid")
    (root / "app.py").write_text("VALUE = 1\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "first")
    return root


def reviewed(store, pid, kind, text, key):
    source = store.snapshot(pid, f"note://{key}", text, "note")
    record = store.remember(pid, {"kind": kind, "key": key, "content": text}, [source["id"]])
    return Inspector(store).review(pid, record["id"], "confirm", record["version"])["record"]


def tree(folder):
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


def records(store, pid):
    return sorted((r["id"], r["kind"], r["key"], r["content"], r["status"], r["version"])
                  for r in store.list_records(pid))


@pytest.fixture
def ada(tmp_path, monkeypatch):
    monkeypatch.setenv(PUBLICATION_SWITCH, "1")
    root = repository(tmp_path / "code")
    store = Store(tmp_path / "data")
    project = store.add_project("team", root)
    memory = repository(tmp_path / "memory")
    return {"store": store, "pid": project["id"], "root": root, "memory": memory, "tmp": tmp_path}


def importer(tmp_path, name, folder):
    store = Store(tmp_path / f"{name}-data")
    project = store.add_project("team", repository(tmp_path / f"{name}-code"))
    PublishedImporter(store).import_published(folder, project["id"])
    return records(store, project["id"])


def test_the_same_package_through_the_repository_and_a_checkout_imports_to_the_same_records(ada):
    store, pid, root, memory = ada["store"], ada["pid"], ada["root"], ada["memory"]
    reviewed(store, pid, "decision", "ArcadeDB is the only storage engine.", "storage")
    reviewed(store, pid, "fact", "The service listens on localhost only.", "listen")
    in_repository = root / ".memory" / "cairnmind-published"
    assert Publisher(store).publish(pid)["destination"] == str(in_repository)
    first = tree(in_repository)
    changed = configure(store, pid, "checkout", str(memory))
    # Nothing moves: the command says where the earlier package is and what to do with it.
    assert changed["status"] == "configured" and changed["moved"] == []
    assert changed["earlier_package"] == {"destination": str(in_repository), "files": len(first)}
    assert str(in_repository) in changed["message"] and "import-published" in changed["message"]
    assert tree(in_repository) == first and not (memory / pid).exists()
    written = Publisher(store).publish(pid)
    assert written["destination"] == str(memory / pid) and written["carrier"] == "checkout"
    assert tree(memory / pid) == first  # The same bytes, under the project's identity.
    assert subprocess.run(["git", "-C", str(memory), "status", "--porcelain"], capture_output=True,
                          text=True).stdout.startswith("?? ")  # Never staged, committed or pushed.
    status = publication_status(store, pid)
    assert status["carrier"]["kind"] == "checkout" and status["destination"] == str(memory / pid)
    assert status["other_package"] == {"destination": str(in_repository), "files": len(first)}
    assert importer(ada["tmp"], "from-repository", in_repository) == importer(ada["tmp"], "from-checkout", memory / pid)
    assert [r[:5] for r in importer(ada["tmp"], "again", memory / pid)] == [r[:5] for r in records(store, pid)]
    back = configure(store, pid, "repository")
    assert back["earlier_package"] == {"destination": str(memory / pid), "files": len(first)}
    assert str(memory / pid) in back["message"]
    assert configure(store, pid, "repository")["status"] == "unchanged"


def test_a_checkout_path_is_validated_every_time_it_is_used(ada):
    store, pid, root, memory, tmp = ada["store"], ada["pid"], ada["root"], ada["memory"], ada["tmp"]
    other = store.add_project("other", repository(tmp / "other"))
    (tmp / "other" / ".memory" / "cairnmind").mkdir(parents=True)
    git(tmp / "other" / ".memory" / "cairnmind", "init", "-q")
    (tmp / "plain").mkdir()
    os.symlink(memory, tmp / "linked")
    (memory / "sub").mkdir()
    refusals = {
        "memory": "must be absolute",
        str(tmp / "missing"): "not an existing directory",
        str(tmp / "plain"): "not in a Git checkout",
        str(tmp / "linked"): "symbolic link",
        str(tmp / "linked" / "sub"): "symbolic link",
        str(tmp / "other" / ".memory" / "cairnmind"): "inside the memory of project other",
        str(tmp / "other"): "inside the files of project other",
        str(root): "this project's own files",
        str(root / ".memory" / "cairnmind-published"): "inside the memory of project team",
        str(store.data_dir): "Cairnmind's own data directory",
        str(memory) + "‮": "plain absolute path",
    }
    for path, reason in refusals.items():
        with pytest.raises(ValueError, match=reason):
            configure(store, pid, "checkout", path)
    with pytest.raises(ValueError, match="needs a receiving Cairnmind service"):
        configure(store, pid, "server")
    with pytest.raises(ValueError, match="takes no path"):
        configure(store, pid, "repository", str(memory))
    assert other["id"] != pid and publication_status(store, pid)["carrier"] == {"kind": "repository"}
    configure(store, pid, "checkout", str(memory))
    reviewed(store, pid, "decision", "Use cookies.", "auth")
    os.symlink(tmp / "plain", memory / pid)
    with pytest.raises(ValueError, match="symbolic link"):
        Publisher(store).publish(pid)
    (memory / pid).unlink()
    moved = tmp / "moved"
    memory.rename(moved)
    with pytest.raises(ValueError, match="not an existing directory"):
        Publisher(store).publish(pid)
    assert "not an existing directory" in publication_status(store, pid)["error"]


def test_the_secrets_gate_refuses_before_a_checkout_is_written(ada, monkeypatch):
    store, pid, memory = ada["store"], ada["pid"], ada["memory"]
    reviewed(store, pid, "decision", "Use cookies.", "auth")
    configure(store, pid, "checkout", str(memory))
    monkeypatch.setattr(memory_published, "scan", lambda models: [
        {"path": "decision/x.md", "field": "text", "kind": "private key"}])
    with pytest.raises(MemoryConflict, match="private key in text"):
        Publisher(store).publish(pid)
    assert not (memory / pid).exists()
    monkeypatch.undo()
    monkeypatch.setenv(PUBLICATION_SWITCH, "1")
    assert Publisher(store).publish(pid)["status"] == "written" and (memory / pid / "FORMAT.json").is_file()


def test_a_checkout_that_holds_another_package_needs_an_import_or_a_new_package(ada, tmp_path):
    store, pid, memory = ada["store"], ada["pid"], ada["memory"]
    reviewed(store, pid, "decision", "Use cookies.", "auth")
    configure(store, pid, "checkout", str(memory))
    Publisher(store).publish(pid)
    bo = Store(tmp_path / "bo-data")
    bo_project = bo.add_project("team", repository(tmp_path / "bo-code"))["id"]
    reviewed(bo, bo_project, "fact", "Port 8080 is free.", "port")
    told = configure(bo, bo_project, "checkout", str(memory))
    assert f"already holds published memory of {pid}" in told["message"]
    with pytest.raises(MemoryConflict, match=f"holds published memory of {pid}.*import-published"):
        Publisher(bo).publish(bo_project)
    assert sorted(p.name for p in memory.iterdir() if p.name not in (".git", "app.py")) == [pid]
    PublishedImporter(bo).import_published(memory / pid, bo_project)
    result = Publisher(bo).publish(bo_project)
    assert result["destination"] == str(memory / pid) and len(result["written"]) == 1
    carol = Store(tmp_path / "carol-data")
    carol_project = carol.add_project("solo", repository(tmp_path / "carol-code"))["id"]
    reviewed(carol, carol_project, "fact", "Carol keeps her own memory.", "own")
    configure(carol, carol_project, "checkout", str(memory))
    assert Publisher(carol).publish(carol_project, new_package=True)["destination"] == str(memory / carol_project)


def test_the_carrier_through_the_service_and_the_command_line(ada, monkeypatch):
    store, pid, memory = ada["store"], ada["pid"], ada["memory"]
    headers = {"Authorization": f"Bearer {store.settings.token}"}
    with TestClient(create_app(store.settings, start_worker=False)) as client:
        body = {"project": "team", "kind": "checkout", "path": str(memory)}
        assert client.post("/memory/carrier", headers=headers, json={**body, "colour": 1}).status_code == 422
        refused = client.post("/memory/carrier", headers=headers, json={**body, "session_id": "s"})
        assert refused.status_code == 400 and "only by a person" in refused.json()["detail"]
        done = client.post("/memory/carrier", headers=headers, json=body).json()
        assert done["status"] == "configured" and done["destination"] == str(memory / pid)
        status = client.get("/memory/status", headers=headers, params={"project": "team"}).json()["published"]
        assert status["carrier"] == {"kind": "checkout", "path": str(memory)}
    sent = []
    monkeypatch.setattr(cli, "request", lambda settings, method, path, **kwargs: sent.append(
        (method, path, kwargs.get("json"))) or {"status": "configured"})
    runner = CliRunner()
    base = ["--data-dir", str(store.data_dir), "memory", "carrier", "team"]
    assert runner.invoke(cli.app, [*base, "--checkout", str(memory)]).exit_code == 0
    assert runner.invoke(cli.app, [*base, "--repository"]).exit_code == 0
    assert sent == [("POST", "/memory/carrier", {"project": "team", "kind": "checkout", "path": str(memory)}),
                    ("POST", "/memory/carrier", {"project": "team", "kind": "repository", "path": None})]
    assert runner.invoke(cli.app, base).exit_code == 2
    assert runner.invoke(cli.app, [*base, "--repository", "--checkout", str(memory)]).exit_code == 2
    monkeypatch.setenv("CAIRNMIND_SESSION_ID", "s")
    inside = runner.invoke(cli.app, [*base, "--repository"])
    assert inside.exit_code == 2 and len(sent) == 2
