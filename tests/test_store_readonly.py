"""Store.open_readonly reads an existing store, and refuses one that is not ready without preparing anything."""
from __future__ import annotations

import socket

import pytest

from cairnmind.arcade import ArcadeClient, ArcadeError
from cairnmind.config import Settings
from cairnmind.store import SCHEMA_VERSION, Store
from cairnmind.store_readonly import ReadOnlyStoreError, StoreNotReady


def forbid_starting(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Opening the store for reading started the engine or a process")
    monkeypatch.setattr("cairnmind.engine.start_engine", forbidden)
    monkeypatch.setattr("cairnmind.engine.ensure_engine", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)


def record_requests(monkeypatch):
    sent = []
    original = ArcadeClient._request

    def request(self, method, path, **kwargs):
        sent.append((method, path, (kwargs.get("json") or {}).get("command")))
        return original(self, method, path, **kwargs)

    monkeypatch.setattr(ArcadeClient, "_request", request)
    return sent


def changes(sent):
    """Requests that could change the server: database and server commands, and commits."""
    return [item for item in sent if item[0] != "GET"
            and not item[1].startswith(("/api/v1/query/", "/api/v1/begin/", "/api/v1/rollback/"))]


def files(root):
    return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in root.rglob("*") if path.is_file()}


def ready(tmp_path):
    store = Store(tmp_path / "memory")
    root = tmp_path / "repo"
    root.mkdir()
    return store, store.add_project("demo", root)


def names(store):
    return [row["name"] for row in store.list_projects()]


def test_a_ready_store_is_read_with_no_command_commit_or_file(tmp_path, monkeypatch):
    store, project = ready(tmp_path)
    before = files(store.data_dir)
    forbid_starting(monkeypatch)
    sent = record_requests(monkeypatch)
    reader = Store.open_readonly(store.settings)
    assert reader.read_only is True and store.read_only is False
    assert [row["id"] for row in reader.list_projects()] == [project["id"]]
    assert reader.resolve_project("demo")["id"] == project["id"]
    # A read in a transaction: the same rows, and the server transaction is rolled back.
    assert [row["id"] for row in reader.export()["projects"]] == [project["id"]]
    assert changes(sent) == []
    assert any(path.startswith("/api/v1/rollback/") for _, path, _ in sent)
    assert files(store.data_dir) == before


def test_a_store_opened_for_reading_refuses_every_write(tmp_path, monkeypatch):
    store, _ = ready(tmp_path)
    reader = Store.open_readonly(store.settings)
    (tmp_path / "other").mkdir()
    monkeypatch.setattr(reader, "_lock", lambda: pytest.fail("A store opened for reading took the writer lock"))
    sent = record_requests(monkeypatch)
    with pytest.raises(ReadOnlyStoreError):
        reader.add_project("other", tmp_path / "other")
    with pytest.raises(ReadOnlyStoreError):
        reader.command("UPDATE projects SET name='changed'")
    with pytest.raises(ReadOnlyStoreError):
        reader.command("CREATE DOCUMENT TYPE extra IF NOT EXISTS")
    with pytest.raises(ReadOnlyStoreError):
        reader.query("UPDATE projects SET name='changed'")
    with pytest.raises(ReadOnlyStoreError):
        reader.client.command("DELETE FROM projects")
    with pytest.raises(ReadOnlyStoreError):
        reader.client.server_command(f"drop database {store.settings.arcade_database}")
    with pytest.raises(ReadOnlyStoreError):
        with reader.transaction() as tx:
            tx.command("DELETE FROM projects")
    assert changes(sent) == []
    assert names(store) == ["demo"]


def test_a_read_transaction_is_rolled_back_even_after_a_write_past_the_guard(tmp_path):
    store, _ = ready(tmp_path)
    reader = Store.open_readonly(store.settings)
    with reader.transaction() as tx:
        tx._client.command("UPDATE projects SET name='changed'")  # past the guard on purpose
        assert [row["name"] for row in tx.query("SELECT name FROM projects")] == ["changed"]
    assert names(store) == ["demo"]


def test_the_engine_query_endpoint_refuses_a_write_as_well(tmp_path):
    store, _ = ready(tmp_path)
    with pytest.raises(ArcadeError):
        ArcadeClient(store.settings).query("UPDATE projects SET name='changed'")
    assert names(store) == ["demo"]


def test_no_configuration_is_refused_before_any_request_or_directory(tmp_path, monkeypatch):
    forbid_starting(monkeypatch)
    sent = record_requests(monkeypatch)
    settings = Settings.load(tmp_path / "missing" / "memory")
    with pytest.raises(StoreNotReady, match="run `cairnmind init` first"):
        Store.open_readonly(settings)
    assert sent == []
    assert not (tmp_path / "missing").exists()


def test_a_stopped_engine_is_refused_without_starting_it(tmp_path, monkeypatch):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setenv("CAIRNMIND_ARCADE_URL", f"http://127.0.0.1:{port}")
    settings = Settings.load(tmp_path / "memory", create=True)
    before = files(settings.data_dir)
    forbid_starting(monkeypatch)
    with pytest.raises(StoreNotReady) as refused:
        Store.open_readonly(settings)
    assert str(refused.value) == ("The database engine is not running; start it with `cairnmind engine start` "
                                  "(this command does not start it).")
    assert files(settings.data_dir) == before


def test_a_missing_database_is_refused_and_not_created(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "memory", create=True)
    forbid_starting(monkeypatch)
    sent = record_requests(monkeypatch)
    with pytest.raises(StoreNotReady, match="does not exist yet; create it with `cairnmind init`"):
        Store.open_readonly(settings)
    assert changes(sent) == []
    assert ArcadeClient(settings).database_exists() is False


def test_a_database_without_schema_is_refused_and_gets_no_ddl(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "memory", create=True)
    ArcadeClient(settings).server_command(f"create database {settings.arcade_database}")
    forbid_starting(monkeypatch)
    sent = record_requests(monkeypatch)
    with pytest.raises(StoreNotReady, match="has no schema yet; create it with `cairnmind init`"):
        Store.open_readonly(settings)
    assert changes(sent) == []
    types = {row["name"] for row in ArcadeClient(settings).query("SELECT name FROM schema:types")}
    assert "devbrain_meta" not in types


@pytest.mark.parametrize(("version", "message"), [
    (3, "has schema version 3 and needs the upgrade to 4; run `cairnmind init`, which writes a backup first"),
    (1, "has schema version 1 and needs the upgrade to 4"),
    (99, "Database schema version is incompatible: the database has version 99"),
])
def test_another_schema_version_is_refused_and_not_migrated(tmp_path, monkeypatch, version, message):
    store, _ = ready(tmp_path)
    store.command(f"UPDATE devbrain_meta SET value={version} WHERE name='schema_version'")
    forbid_starting(monkeypatch)
    sent = record_requests(monkeypatch)
    with pytest.raises(StoreNotReady) as refused:
        Store.open_readonly(store.settings)
    assert message in str(refused.value)
    assert changes(sent) == []
    assert not (store.data_dir / "backups").exists()
    assert store.query("SELECT value FROM devbrain_meta WHERE name='schema_version'")[0]["value"] == version
    assert SCHEMA_VERSION == 4


def test_the_read_commands_work_on_a_ready_store_without_preparing_it(tmp_path, monkeypatch):
    import json
    import subprocess
    from typer.testing import CliRunner
    from cairnmind.cli import app
    from cairnmind.plans import PlanManager
    store, project = ready(tmp_path)
    root = tmp_path / "repo"
    (root / "app.py").write_text("def run():\n    return 1\n")
    for arguments in (("init",), ("config", "user.email", "fixture@example.invalid"),
                      ("config", "user.name", "Fixture"), ("add", "."), ("commit", "-qm", "fixture")):
        subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)
    source = store.snapshot(project["id"], "manual://note", "A note", "manual")
    plan = PlanManager(store).save(project["id"], {"title": "Ship"}, [{"key": "one", "content": "Do it"}],
                                   "plan-key", activate=True)
    before = files(store.data_dir)
    # Git runs as a process here, so only the engine start and the full store are forbidden.
    for name in ("cairnmind.engine.start_engine", "cairnmind.engine.ensure_engine", "cairnmind.cli.ensure_service"):
        monkeypatch.setattr(name, lambda *a, **k: pytest.fail("A read command started the engine or service"))
    monkeypatch.setattr(Store, "__init__", lambda *a, **k: pytest.fail("A read command prepared the store"))
    sent = record_requests(monkeypatch)
    runner = CliRunner()
    for arguments in (["project", "list"], ["records", "demo", "--history"], ["source", source["id"]],
                      ["task", "list", "demo"], ["plan", "list", "demo"], ["plan", "show", "demo", plan["plan"]["id"]],
                      ["plan", "next", "demo"], ["plan", "contract", "demo", "--json"], ["check", "profiles", "demo"],
                      ["memory", "orphans", "demo", "--json"], ["impact", "demo", "--since", "HEAD", "--json"],
                      ["usage", "demo", "--json"]):
        result = runner.invoke(app, ["--data-dir", str(store.data_dir), *arguments])
        assert result.exit_code == 0, (arguments, result.output, repr(result.exception))
        json.loads(result.output)
    assert changes(sent) == []
    assert files(store.data_dir) == before


def plan_fixture(tmp_path):
    from cairnmind.plans import PlanManager
    store, project = ready(tmp_path)
    first = PlanManager(store).save(project["id"], {"title": "Ship"}, [{"key": "one", "content": "Do it"}], "first")
    return store, project, first


def save_second_version(store, project, first):
    from cairnmind.plans import PlanManager
    PlanManager(store).save(project["id"], {"id": first["plan"]["id"], "title": "Ship again"},
                            [{"key": "one", "content": "Do it better"}], "second", expected_version=1,
                            task_versions={task["id"]: task["version"] for task in first["tasks"]})


def test_a_commit_between_the_queries_of_plan_show_is_never_shown_half(tmp_path, monkeypatch):
    import json
    from typer.testing import CliRunner
    import cairnmind.plans as plans
    from cairnmind.cli import app
    store, project, first = plan_fixture(tmp_path)
    real, calls = plans._tasks, []

    def tasks_after_a_commit(tx, project_id, plan):
        if getattr(tx, "changes", None) is not None:
            return real(tx, project_id, plan)  # the writer's own transaction, inside the save below
        calls.append(plan["version"])
        if len(calls) == 1:  # the plan row was read; the service saves version 2 before the tasks are read
            save_second_version(store, project, first)
        return real(tx, project_id, plan)

    monkeypatch.setattr(plans, "_tasks", tasks_after_a_commit)
    shown = CliRunner().invoke(app, ["--data-dir", str(store.data_dir), "plan", "show", "demo", first["plan"]["id"]])
    assert shown.exit_code == 0, (shown.output, repr(shown.exception))
    result = json.loads(shown.output)
    assert (result["plan"]["version"], result["plan"]["title"]) == (2, "Ship again")
    assert [task["content"] for task in result["tasks"]] == ["Do it better"]
    assert calls == [1, 2]


def test_a_store_that_keeps_changing_is_refused_with_one_sentence(tmp_path, monkeypatch, capsys):
    import cairnmind.plans as plans
    from cairnmind import cli
    store, project, first = plan_fixture(tmp_path)
    real, saved = plans._tasks, []

    def tasks_after_a_commit(tx, project_id, plan):
        saved.append(True)
        source = store.snapshot(project["id"], f"manual://{len(saved)}", "x", "manual")
        store.remember(project["id"], {"kind": "fact", "content": f"change {len(saved)}"}, [source["id"]])
        return real(tx, project_id, plan)

    monkeypatch.setattr(plans, "_tasks", tasks_after_a_commit)
    monkeypatch.setattr("sys.argv", ["cairnmind", "--data-dir", str(store.data_dir), "plan", "show", "demo",
                                     first["plan"]["id"]])
    with pytest.raises(SystemExit) as stopped:
        cli.entrypoint()
    assert stopped.value.code == 1 and len(saved) == 3
    shown = capsys.readouterr()
    assert " ".join(shown.err.split()) == ("Cairnmind: The store changed during each of 3 attempts to read it; "
                                           "run the command again when the current work is done.")
    assert shown.out == ""


def test_a_missing_journal_row_is_refused(tmp_path, monkeypatch):
    store, _ = ready(tmp_path)
    ArcadeClient(store.settings).command("DELETE FROM journal_state")
    forbid_starting(monkeypatch)
    sent = record_requests(monkeypatch)
    with pytest.raises(StoreNotReady, match="has no journal state; any ordinary write, for example `cairnmind "
                                            "remember`, recreates it, and `cairnmind restore FILE` is for when"):
        Store.open_readonly(store.settings)
    assert changes(sent) == []
    assert not ArcadeClient(store.settings).query("SELECT FROM journal_state")
