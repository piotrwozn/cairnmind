"""A mistyped project name never acts on another project (#238).

Store.resolve_project is the one place that turns a project argument into a project: an id or a name,
or a path inside a registered folder. Before #238 any value that matched no id or name was read as a
path from the working directory, so `remember nope-xyz` inside a project's folder, or any request to a
service started there, acted on that project.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.service import create_app
from cairnmind.store import Store


class NoInference:
    async def models(self):
        pytest.fail("resolving a project must not invoke a model")

    async def chat(self, *args, **kwargs):
        pytest.fail("resolving a project must not invoke a model")

    async def embed(self, *args, **kwargs):
        pytest.fail("resolving a project must not invoke a model")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "memory")


@pytest.fixture
def project(store, tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    return store.add_project("example", root)


def records(store, project):
    return store.query("SELECT id FROM records WHERE project_id=:p", {"p": project["id"]})


@pytest.mark.parametrize("value", ["nope-xyz", "zzz-typo", "examp1e", "", "src"])
def test_a_name_that_matches_nothing_is_unknown_even_inside_a_registered_folder(store, project, monkeypatch, value):
    monkeypatch.chdir(Path(project["root"]) / ("src" if value != "src" else "."))
    with pytest.raises(ValueError) as refused:
        store.resolve_project(value)
    assert str(refused.value).startswith(f"Unknown project: {value}")


def test_ids_names_and_written_paths_still_resolve(store, project, monkeypatch):
    root = Path(project["root"])
    assert store.resolve_project(project["id"])["id"] == project["id"]
    assert store.resolve_project("example")["id"] == project["id"]
    assert store.resolve_project(root / "src")["id"] == project["id"]
    monkeypatch.chdir(root / "src")
    for written in (".", "..", "./", "../src"):
        assert store.resolve_project(written)["id"] == project["id"]
    monkeypatch.setenv("HOME", str(root))
    assert store.resolve_project("~/src")["id"] == project["id"]


def test_a_path_that_does_not_exist_is_unknown(store, project):
    missing = Path(project["root"]) / "no-such-dir"
    with pytest.raises(ValueError, match=r"^Unknown project: .*no-such-dir \(no such file or folder\)"):
        store.resolve_project(missing)


def test_a_recorded_working_directory_may_be_gone_but_must_be_absolute(store, project, monkeypatch):
    # A transcript or hook names where a session ran; that folder may have been removed since.
    gone = Path(project["root"]) / "removed-worktree"
    assert store.resolve_project(str(gone), recorded=True)["id"] == project["id"]
    monkeypatch.chdir(project["root"])
    for value in (".", "src", "", "nope-xyz"):
        with pytest.raises(ValueError, match="^Unknown project"):
            store.resolve_project(value, recorded=True)


def test_a_store_of_another_working_directory_refuses_a_relative_path(store, project, monkeypatch):
    monkeypatch.chdir(project["root"])
    store.relative_paths = False
    with pytest.raises(ValueError, match=r"^Unknown project: \./src \(a relative path is not accepted here"):
        store.resolve_project("./src")
    assert store.resolve_project(str(Path(project["root"]) / "src"))["id"] == project["id"]


def test_cli_remember_with_a_mistyped_name_inside_a_project_writes_nothing(store, project, monkeypatch):
    monkeypatch.chdir(project["root"])
    result = CliRunner().invoke(cli.app, ["--data-dir", str(store.data_dir), "remember", "nope-xyz", "a note"])
    assert result.exit_code != 0
    assert "Unknown project: nope-xyz" in str(result.exception)
    assert records(store, project) == []


def test_cli_sends_a_written_path_as_an_absolute_one(project, monkeypatch, tmp_path):
    root = Path(project["root"])
    monkeypatch.chdir(root)
    assert cli.caller_path(".") == str(root)
    assert cli.caller_path("./src") == str(root / "src")
    # A name, or a path that does not exist here, goes as typed; the service names it in its refusal.
    assert cli.caller_path("example") == "example"
    assert cli.caller_path("nope-xyz") == "nope-xyz"
    assert cli.caller_path("./missing") == "./missing"
    assert cli.caller_path("./src", lambda: frozenset({"./src"})) == "./src"
    assert cli.caller_path("./src", lambda: None) == "./src"
    sent = fake_service(monkeypatch, [{"id": project["id"], "name": "example"}])
    cli.request(SETTINGS, "POST", "/memory/publish", session=False, json={"project": ".", "preview": True})
    assert sent["json"] == {"project": str(root), "preview": True}
    cli.request(SETTINGS, "GET", "/context", session=False, params={"project": "nope-xyz"})
    assert sent["params"] == {"project": "nope-xyz"}


SETTINGS = type("S", (), {"ask_timeout": 1, "url": "http://127.0.0.1:1", "token": "t"})()


def fake_service(monkeypatch, projects):
    """A service whose /projects answers ``projects`` (None: it fails); returns what the last request sent."""
    sent = {}

    class Response:
        def __init__(self, body, ok=True):
            self.body, self.is_success = body, ok

        def json(self):
            return self.body

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def request(self, method, url, **kwargs):
            if url.endswith("/projects"):
                return Response(projects, ok=projects is not None)
            sent.clear()
            sent.update(kwargs)
            return Response({})

    monkeypatch.setattr(cli, "ensure_service", lambda settings: None)
    monkeypatch.setattr(cli.httpx, "Client", Client)
    return sent


@pytest.mark.parametrize("name", ["web/api", ".config"])
def test_cli_sends_a_registered_name_that_looks_like_a_local_folder_unchanged(project, monkeypatch, name):
    # A project named "web/api" or ".config" while the working directory holds a folder of that name:
    # the name wins, as it does in Store.resolve_project, instead of the working directory's project.
    root = Path(project["root"])
    (root / name).mkdir(parents=True)
    monkeypatch.chdir(root)
    sent = fake_service(monkeypatch, [{"id": project["id"], "name": "example"}, {"id": "p2", "name": name}])
    cli.request(SETTINGS, "POST", "/ask", session=False, json={"project": name, "question": "q"})
    assert sent["json"]["project"] == name
    cli.request(SETTINGS, "GET", "/context", session=False, params={"project": "p2"})
    assert sent["params"]["project"] == "p2"
    # A folder that is no registered name is still sent as an absolute path.
    cli.request(SETTINGS, "GET", "/context", session=False, params={"project": "./src"})
    assert sent["params"]["project"] == str(root / "src")


def test_cli_sends_a_written_path_as_given_when_the_service_cannot_list_projects(project, monkeypatch):
    # Without the list a local folder could shadow a name; the service decides and refuses a relative path.
    monkeypatch.chdir(project["root"])
    sent = fake_service(monkeypatch, None)
    cli.request(SETTINGS, "GET", "/context", session=False, params={"project": "./src"})
    assert sent["params"]["project"] == "./src"


def test_service_routes_never_resolve_against_the_services_directory(store, project, monkeypatch):
    # The service started from inside the project's folder: a typo or a relative path must not land there.
    monkeypatch.chdir(Path(project["root"]) / "src")
    headers = {"Authorization": f"Bearer {store.settings.token}"}
    with TestClient(create_app(store.settings, start_worker=False, ollama=NoInference())) as client:
        for value in ("zzz-typo", "nope-xyz", ".", "./src", ""):
            preview = client.post("/memory/preview", json={"project": value}, headers=headers)
            assert preview.status_code in {400, 404, 422}, (value, preview.text)
            roots = client.get("/project-roots", params={"project": value}, headers=headers)
            assert roots.status_code in {400, 404, 422}, (value, roots.text)
        named = client.get("/project-roots", params={"project": "nope-xyz"}, headers=headers)
        assert "Unknown project: nope-xyz" in named.json()["detail"]
        absolute = client.get("/project-roots", params={"project": str(Path(project["root"]) / "src")},
                              headers=headers)
        assert absolute.status_code == 200, absolute.text
    assert records(store, project) == []
