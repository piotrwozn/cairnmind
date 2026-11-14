"""The commands and service routes of published team memory (#170)."""
from __future__ import annotations

import shutil
import subprocess

from fastapi.testclient import TestClient
import pytest
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.inspector import Inspector
from cairnmind.memory_published import PUBLICATION_SWITCH
from cairnmind.service import create_app
from cairnmind.store import Store


class NoInference:
    async def models(self):
        pytest.fail("Published memory must not invoke a model")

    async def chat(self, *args, **kwargs):
        pytest.fail("Published memory must not invoke a model")

    async def embed(self, *args, **kwargs):
        pytest.fail("Published memory must not invoke a model")


def repository(root):
    root.mkdir()
    for args in (("init", "-q", "-b", "main"), ("config", "user.name", "Ada Nowak"),
                 ("config", "user.email", "ada@example.invalid")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (root / "app.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "first"], check=True, capture_output=True)
    return root


def test_publish_check_import_and_status_through_the_service_and_the_command_line(tmp_path, monkeypatch):
    monkeypatch.setenv(PUBLICATION_SWITCH, "1")
    root = repository(tmp_path / "ada")
    store = Store(tmp_path / "ada-data")
    project = store.add_project("team", root)
    source = store.snapshot(project["id"], "note://a", "Use cookies.", "note")
    record = store.remember(project["id"], {"kind": "decision", "content": "Use cookies."}, [source["id"]])
    Inspector(store).review(project["id"], record["id"], "confirm", 1)
    headers = {"Authorization": f"Bearer {store.settings.token}"}
    folder = root / ".memory" / "cairnmind-published"
    with TestClient(create_app(store.settings, start_worker=False, ollama=NoInference())) as client:
        preview = client.post("/memory/publish", headers=headers, json={"project": "team", "preview": True})
        assert preview.status_code == 200 and preview.json()["status"] == "preview" and not folder.exists()
        assert preview.json()["would_write"] == [".gitattributes", ".gitignore", "FORMAT.json",
                                                 f"decision/{record['id']}.md"]
        written = client.post("/memory/publish", headers=headers, json={"project": "team"})
        assert written.json()["status"] == "written" and (folder / f"decision/{record['id']}.md").is_file()
        assert client.post("/memory/publish", headers=headers, json={"project": "team"}).json()["status"] == "unchanged"
        assert client.post("/memory/publish", headers=headers, json={"project": "team", "colour": 1}).status_code == 422
        status = client.get("/memory/status", headers=headers, params={"project": "team"}).json()["published"]
        assert status["format_exists"] and status["publication_id"] == project["id"] and status["files"] == 4
    runner = CliRunner()
    checked = runner.invoke(cli.app, ["--data-dir", str(tmp_path / "unused"), "memory", "check-published", str(folder)])
    assert checked.exit_code == 0 and f"valid: 1 records and 0 plans of {project['id']}" in checked.output
    shown = runner.invoke(cli.app, ["--data-dir", str(tmp_path / "unused"), "memory", "show-published", str(folder)])
    assert shown.exit_code == 0 and "Use cookies." in shown.output and "not verified here" in shown.output
    assert not (tmp_path / "unused").exists()
    broken = folder / f"decision/{record['id']}.md"
    broken.write_text(broken.read_text().replace("    Use cookies.", "<<<<<<< ours\n    Use cookies.", 1))
    refused = runner.invoke(cli.app, ["--data-dir", str(tmp_path / "unused"), "memory", "check-published", str(folder)])
    assert refused.exit_code == 1
    assert refused.output.startswith(f"decision/{record['id']}.md: unresolved conflict in decision/{record['id']}.md line ")
    broken.write_text(broken.read_text().replace("<<<<<<< ours\n", ""))
    second = tmp_path / "bo"
    subprocess.run(["git", "clone", "-q", str(root), str(second)], check=True, capture_output=True)
    shutil.copytree(folder, second / ".memory" / "cairnmind-published")
    other = Store(tmp_path / "bo-data")
    other.add_project("team", second)
    other_headers = {"Authorization": f"Bearer {other.settings.token}"}
    with TestClient(create_app(other.settings, start_worker=False, ollama=NoInference())) as client:
        body = {"project": "team", "path": str(second / ".memory" / "cairnmind-published")}
        dry = client.post("/memory/import-published", headers=other_headers, json={**body, "dry_run": True}).json()
        assert dry["status"] == "dry_run" and dry["inserted"] and not other.list_records(dry["project_id"])
        imported = client.post("/memory/import-published", headers=other_headers, json=body).json()
        assert imported["inserted"] == [f"decision/{record['id']}.md"]
        assert imported["verification"] == "nothing imported is verified here"
        assert [row["id"] for row in other.list_records(imported["project_id"])] == [record["id"]]
