"""The messages that stop a first start name what is missing and the next step (#287)."""
import json
import types

import pytest
from typer.testing import CliRunner

from cairnmind import cli, engine, first_start, integrations
from cairnmind.arcade import ArcadeError
from cairnmind.config import Settings
from cairnmind.store import Store


def project_store(tmp_path):
    store = Store(tmp_path / "memory")
    root = tmp_path / "app"
    root.mkdir()
    return store, root, store.add_project("demo", root)


def test_an_unknown_project_says_how_to_list_and_register_projects_and_lists_none(tmp_path):
    store, _, _ = project_store(tmp_path)
    with pytest.raises(ValueError) as refused:
        store.resolve_project("nope")
    assert str(refused.value) == f"Unknown project: nope. {first_start.UNKNOWN_PROJECT_NEXT}"
    assert "demo" not in str(refused.value)  # a refusal an external MCP client reads names no other project
    with pytest.raises(ValueError) as by_path:
        store.resolve_project(str(tmp_path))  # a folder that exists and is not registered
    assert str(by_path.value).endswith(first_start.UNKNOWN_PROJECT_NEXT)


def test_a_taken_name_names_its_folder_and_a_taken_folder_names_its_project(tmp_path):
    store, root, _ = project_store(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ValueError) as name:
        store.add_project("demo", other)
    assert str(name.value) == first_start.PROJECT_NAME_TAKEN.format(name="demo", root=root.resolve())
    with pytest.raises(ValueError) as folder:
        store.add_project("another", root)
    assert str(folder.value) == first_start.PROJECT_ROOT_TAKEN.format(name="demo", root=root.resolve())
    assert store.add_project("demo", root)["name"] == "demo"  # the same pair is still accepted as before


def test_run_without_the_client_names_where_to_get_it_and_what_works_without_one(tmp_path, monkeypatch):
    store, root, _ = project_store(tmp_path)
    monkeypatch.setattr(cli, "_ensure_service_or_refuse", lambda settings: None)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)

    def refuse(*args, **kwargs):
        raise ValueError("codex is not installed in this WSL distribution")
    monkeypatch.setattr(integrations, "run_client", refuse)
    result = CliRunner().invoke(cli.app, ["--data-dir", str(store.data_dir), "run", "demo", "codex"])
    message = str(result.exception)
    assert message == ("codex is not installed in this WSL distribution. "
                       + first_start.RUN_NO_CLIENT.format(url="https://developers.openai.com/codex/cli"))
    assert "cairnmind remember" in message
    # Any other refusal, and a client that is installed, are left as they were.
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/bin/" + name)
    result = CliRunner().invoke(cli.app, ["--data-dir", str(store.data_dir), "run", "demo", "claude"])
    assert str(result.exception) == "codex is not installed in this WSL distribution"
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(integrations, "run_client", lambda *a, **k: (_ for _ in ()).throw(ValueError("Other")))
    assert str(CliRunner().invoke(cli.app, ["--data-dir", str(store.data_dir), "run", "demo", "claude"]).exception) == (
        "Other. " + first_start.RUN_NO_CLIENT.format(url="https://code.claude.com/docs/en/setup"))


def test_a_refused_verified_launch_names_the_parts_not_in_place_and_doctor_contract(tmp_path, monkeypatch):
    store, root, project = project_store(tmp_path)
    settings = Settings.load(store.data_dir, create=True)
    monkeypatch.setattr(integrations.shutil, "which", lambda name: "/bin/" + name)

    def launch(**in_place):
        info = {"verified_resume": False, "mcp_configured": False, "hooks_configured": False,
                "instructions_configured": False, **in_place}
        with pytest.raises(ValueError) as refused:
            integrations.prepare_client_launch(settings, project, "claude", None,
                                               installation={"warnings": [], "clients": {"claude": info}})
        return str(refused.value)
    text = launch()
    assert text.startswith("Verified claude launch refused; not in place: the client contract checks, the MCP entry, "
                           "the session hooks, the instruction block. ")
    assert text.endswith("cairnmind doctor --contract shows which check fails and what it needs.")
    assert "not in place: the session hooks. " in launch(verified_resume=True, mcp_configured=True,
                                                          instructions_configured=True)


def test_status_without_a_service_says_the_next_command_starts_it(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "memory", create=True)
    monkeypatch.setattr("cairnmind.store_writer.inspect", lambda s: {"role": "writer"})
    assert cli._service_unavailable(settings) == first_start.STATUS_NOT_RUNNING
    assert "serve" not in first_start.STATUS_NOT_RUNNING


def test_an_engine_that_is_not_installed_or_exits_names_its_cause_and_the_next_command(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "memory", create=True)
    monkeypatch.setattr(engine, "connect_only", lambda s: False)
    monkeypatch.setattr(engine, "engine_status", lambda s: {"available": False})
    monkeypatch.setattr("cairnmind.engine_docker.refuse_if_container_runs", lambda s: None)
    monkeypatch.setattr(engine, "runtime_dir", lambda s: tmp_path / "no-runtime")
    with pytest.raises(ArcadeError) as missing:
        engine.start_engine(settings)
    assert str(missing.value) == first_start.ENGINE_NOT_INSTALLED
    runtime = tmp_path / "runtime"
    (runtime / "lib").mkdir(parents=True)
    log = settings.data_dir / "arcade" / "engine.log"
    monkeypatch.setattr(engine, "runtime_dir", lambda s: runtime)
    monkeypatch.setattr(engine, "resolve_java", lambda s: "/usr/bin/java")
    monkeypatch.setattr(engine, "java_major", lambda java: 21)
    monkeypatch.setattr(engine, "write_root_password", lambda s: tmp_path / "pw")
    monkeypatch.setattr(engine.subprocess, "Popen", lambda *a, **k: types.SimpleNamespace(
        pid=1, poll=lambda: 1))
    with pytest.raises(ArcadeError) as exited:
        engine.start_engine(settings)
    assert str(exited.value) == first_start.ENGINE_EXITED.format(log=log, url=settings.arcade_url)
    assert "engine start" in str(exited.value) and "port" in str(exited.value)


def test_the_message_for_a_timed_out_engine_keeps_its_wording_and_gains_the_next_command():
    assert "startup timed out" in first_start.ENGINE_TIMED_OUT
    assert first_start.ENGINE_TIMED_OUT.endswith("run cairnmind engine start.")


def test_opening_the_workspace_without_tmux_names_the_commands_that_work():
    assert first_start.WITHOUT_WORKSPACE == ("Without it, every other command works, for example cairnmind brief, "
                                             "cairnmind remember, cairnmind ask and cairnmind run.")
