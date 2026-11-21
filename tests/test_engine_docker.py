"""The database engine in Docker (#272): every Docker call is replaced here, no daemon is needed."""
from __future__ import annotations

import json
import os
import re
import string
import subprocess
from urllib.parse import urlparse

import pytest
from typer.testing import CliRunner

from cairnmind import config, engine, engine_docker
from cairnmind.cli import app
from cairnmind.arcade import ArcadeClient, ArcadeError
from cairnmind.config import Settings
from cairnmind.database_mode import MODE_INVALID, describe, engine_mode
from cairnmind.engine_docker import (CONTAINER_RUNS, EXITED, IMAGE_FILES_MISSING, NATIVE_RUNS, NOT_OURS,
                                     OTHER_IMAGE, PATH_UNSAFE, RECORD_NOT_OURS, ROOT_REFUSED, SHORT_PASSWORD,
                                     TIMED_OUT, UNPUBLISHED, container_name, labels, start_docker_engine,
                                     stop_docker_engine)

runner = CliRunner()
DIGEST = "sha256:" + "ab" * 32
PUBLISHED = f"ghcr.io/piotrwozn/cairnmind-arcadedb@{DIGEST}"


def settings_with(tmp_path, name="data", **values) -> Settings:
    settings = Settings.load(tmp_path / name, create=True)
    for field, value in values.items():
        setattr(settings, field, value)
    settings.save()
    return settings


def done(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


class FakeDocker:
    """A scripted stand-in for run_docker: containers by name, images, and every call recorded."""

    def __init__(self):
        self.up, self.calls, self.containers, self.images = True, [], {}, set()
        self.answers, self.odd_reply, self.state_after_run, self.state_at_run = True, False, "running", {}
        self.settings = None

    def verbs(self, *names):
        return [call for call, _ in self.calls if call[0] in names]

    def add(self, settings, *, state="running", image=PUBLISHED, ours=True, name=None, labelled=None):
        name = name or container_name(settings)
        held = (labelled if labelled is not None else labels(settings)) if ours else {}
        self.containers[name] = {"Name": "/" + name, "Config": {"Labels": held, "Image": image},
                                 "State": {"Status": state, "Running": state == "running"}}
        return name

    def server_info(self, *_):
        if not self.answers:
            raise ArcadeError("Local ArcadeDB is unavailable (ConnectError); start it")
        return {"version": "26.11.1 (build)"}

    def __call__(self, arguments, *, timeout=15, show=False):
        arguments = list(arguments)
        self.calls.append((arguments, show))
        verb = arguments[0]
        if not self.up and verb != "version":
            return done(1, err="Cannot connect to the Docker daemon at unix:///var/run/docker.sock")
        if verb == "version":
            return done(0, "27.3.1\n") if self.up else done(1, err="Cannot connect to the Docker daemon")
        if verb == "container":
            name = arguments[2]
            if self.odd_reply:
                return done(0, "{}")
            if name in self.containers:
                return done(0, json.dumps([self.containers[name]]))
            return done(1, err=f"Error: No such container: {name}")
        if verb == "image":
            return done(0 if arguments[-1] in self.images else 1, err="Error: No such image")
        if verb in ("pull", "build"):
            self.images.add(arguments[-1] if verb == "pull" else arguments[2])
            return done(0)
        if verb == "run":
            name, image = arguments[arguments.index("--name") + 1], arguments[-1]
            state = self.settings.data_dir / "arcade"
            self.state_at_run = {path.name: oct(path.stat().st_mode & 0o777) for path in state.iterdir()}
            held = dict(item.split("=", 1) for i, item in enumerate(arguments) if arguments[i - 1] == "--label")
            self.containers[name] = {"Name": "/" + name, "Config": {"Labels": held, "Image": image},
                                     "State": {"Status": self.state_after_run, "Running": self.state_after_run == "running"}}
            return done(0, "0123456789abcdef\n")
        if verb == "stop":
            info = self.containers[arguments[-1]]
            info["State"] = {"Status": "exited", "Running": False}
            return done(0)
        if verb == "rm":
            return done(0) if self.containers.pop(arguments[1], None) else done(1, err="Error: No such container")
        raise AssertionError(f"unexpected docker call {arguments}")


@pytest.fixture
def docker(monkeypatch, tmp_path):
    fake = FakeDocker()
    monkeypatch.setattr(engine_docker, "run_docker", fake)
    monkeypatch.setattr(config, "ARCADE_IMAGE_DIGEST", DIGEST)
    monkeypatch.setattr(ArcadeClient, "server_info", fake.server_info)
    monkeypatch.setattr(engine_docker.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(engine, "native_engine_pid", lambda settings: None)
    fake.images.add(PUBLISHED)
    return fake


def started(docker, tmp_path, name="data", **values):
    settings = settings_with(tmp_path, name, **values)
    docker.settings = settings
    return settings


# --- The setting and the pinned image ----------------------------------------------------------------------

def test_the_engine_image_is_pinned_by_a_real_digest():
    # Fails until the release job has published the image and its digest replaces the placeholder.
    assert config.ARCADE_IMAGE == "ghcr.io/piotrwozn/cairnmind-arcadedb"
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", config.ARCADE_IMAGE_DIGEST)
    assert config.ARCADE_IMAGE_DIGEST != "sha256:" + "0" * 64


def test_an_empty_engine_mode_is_native_and_a_wrong_one_is_refused_naming_it(tmp_path):
    settings = settings_with(tmp_path)
    assert settings.engine_mode == "" and settings.engine_docker_build is False
    assert engine_mode(settings) == "native" and describe(settings)["engine"] == "native"
    saved = json.loads((tmp_path / "data" / "config.json").read_text())
    assert saved["engine_mode"] == "" and saved["engine_docker_build"] is False
    settings.engine_mode = "docker"
    assert engine_mode(settings) == "docker" and describe(settings)["engine"] == "docker"
    settings.engine_mode = "dokker"
    with pytest.raises(ValueError, match="engine_mode in config.json is 'dokker'; it must be native or docker"):
        engine_mode(settings)
    shown = describe(settings)
    assert shown["engine"] == "unknown" and shown["engine_problem"] == MODE_INVALID.format(value="'dokker'")


# --- Start ---------------------------------------------------------------------------------------------------

def test_start_runs_the_image_as_the_host_user_on_loopback_with_the_password_as_a_mounted_file(docker, tmp_path):
    settings = started(docker, tmp_path, arcade_url="http://127.0.0.1:2499")
    status = start_docker_engine(settings)
    assert status["available"] and status["engine"] == "docker" and status["container"]["state"] == "running"
    name, held, state = container_name(settings), labels(settings), settings.data_dir / "arcade"
    assert name == "cairnmind-arcadedb-" + held["io.cairnmind.installation"][:12]
    (run,) = docker.verbs("run")
    assert run == ["run", "--detach", "--name", name,
                   "--label", f"io.cairnmind.installation={held['io.cairnmind.installation']}",
                   "--label", f"io.cairnmind.data-dir={settings.data_dir}",
                   "--user", f"{os.getuid()}:{os.getgid()}",
                   "--publish", "127.0.0.1:2499:2480",
                   "--volume", f"{state}:/var/lib/arcadedb",
                   "--volume", f"{state / 'root-password'}:/run/secrets/arcadedb_root_password:ro",
                   "--env", "ARCADEDB_JAVA_OPTS=-Darcadedb.server.mode=development",
                   "--pull", "never", PUBLISHED]
    assert not any(settings.arcade_password in argument for call, _ in docker.calls for argument in call)
    assert docker.state_at_run["root-password"] == "0o600"
    assert {name: docker.state_at_run[name] for name in ("config", "databases", "backups", "log")} == \
           dict.fromkeys(("config", "databases", "backups", "log"), "0o700")
    assert (state.stat().st_mode & 0o777) == 0o700
    record = json.loads((state / "container.json").read_text())
    assert record["name"] == name and record["image"] == PUBLISHED and record["installation"] == held[
        "io.cairnmind.installation"]
    assert (state / "container.json").stat().st_mode & 0o777 == 0o600
    assert (state / "root-password").read_text() == settings.arcade_password


def test_start_refuses_while_the_digest_is_the_placeholder_and_names_build(docker, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARCADE_IMAGE_DIGEST", "sha256:" + "0" * 64)
    settings = started(docker, tmp_path)
    with pytest.raises(ArcadeError) as refused:
        start_docker_engine(settings)
    assert str(refused.value) == UNPUBLISHED and "cairnmind engine start --docker --build" in UNPUBLISHED
    assert docker.calls == []


def test_build_builds_the_shipped_files_and_runs_the_local_tag_without_pulling(docker, tmp_path, monkeypatch):
    context = tmp_path / "image"
    context.mkdir()
    (context / "Dockerfile").write_text("FROM scratch\n")
    monkeypatch.setattr(engine_docker, "build_context", lambda: context)
    settings = started(docker, tmp_path)
    tag = engine_docker.local_image_tag()
    start_docker_engine(settings, build=True, pull=True)
    assert [(call[0], show) for call, show in docker.calls if call[0] in ("build", "pull")] == [("build", True)]
    assert docker.verbs("build") == [["build", "--tag", tag, str(context)]]
    assert docker.verbs("run")[0][-3:] == ["--pull", "never", tag] and tag.endswith("-local")


def test_a_missing_published_image_is_pulled_only_when_asked(docker, tmp_path):
    docker.images.clear()
    settings = started(docker, tmp_path)
    with pytest.raises(ArcadeError, match="is not on this machine") as refused:
        start_docker_engine(settings)
    assert PUBLISHED in str(refused.value) and "cairnmind engine start --docker" in str(refused.value)
    assert docker.verbs("pull", "run") == []
    start_docker_engine(settings, pull=True)
    assert [(call[0], show) for call, show in docker.calls if call[0] == "pull"] == [("pull", True)]
    assert docker.verbs("pull") == [["pull", PUBLISHED]] and docker.verbs("run")


def test_a_missing_local_image_is_never_replaced_by_the_published_one(docker, tmp_path):
    settings = started(docker, tmp_path)
    with pytest.raises(ArcadeError, match="built on this machine, is missing"):
        start_docker_engine(settings, local=True, pull=True)
    assert docker.verbs("pull", "run") == []


def test_start_refuses_when_the_docker_build_setting_is_not_a_boolean(docker, tmp_path):
    settings = started(docker, tmp_path, engine_docker_build="yes")
    with pytest.raises(ValueError, match="engine_docker_build in config.json is 'yes'; it must be true or false"):
        start_docker_engine(settings)


def test_the_wheel_ships_deploy_arcadedb_as_the_build_context(tmp_path, monkeypatch):
    import tomllib
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())
    shipped = project["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert shipped == {"deploy/arcadedb": "cairnmind/arcadedb_image"}
    assert (root / "deploy" / "arcadedb" / "Dockerfile").is_file()  # what the mapping ships
    # The context is the package's own folder: an installed wheel has it, a source checkout does not.
    monkeypatch.setattr(engine_docker, "__file__", str(tmp_path / "package" / "engine_docker.py"))
    with pytest.raises(ArcadeError) as missing:
        engine_docker.build_context()
    assert str(missing.value) == IMAGE_FILES_MISSING.format(path=tmp_path / "package" / "arcadedb_image")
    (tmp_path / "package" / "arcadedb_image").mkdir(parents=True)
    (tmp_path / "package" / "arcadedb_image" / "Dockerfile").write_text("FROM scratch\n")
    assert engine_docker.build_context() == (tmp_path / "package").resolve() / "arcadedb_image"


# --- A container that is not ours, one that cannot be asked about, stop --------------------------------------

def test_stop_never_touches_a_container_without_this_installations_labels(docker, tmp_path):
    settings = started(docker, tmp_path)
    for held in ({"io.cairnmind.installation": "someone-else", "io.cairnmind.data-dir": str(settings.data_dir)},
                 {"io.cairnmind.installation": labels(settings)["io.cairnmind.installation"]}, {}):
        docker.containers.clear()
        docker.add(settings, labelled=held)
        with pytest.raises(ArcadeError) as refused:
            stop_docker_engine(settings)
        assert str(refused.value) == NOT_OURS.format(name=container_name(settings))
        assert docker.verbs("stop", "rm") == []
        with pytest.raises(ArcadeError, match="does not carry this installation's labels"):
            start_docker_engine(settings)
        assert docker.verbs("stop", "rm", "run") == []


def test_stop_refuses_when_the_container_cannot_be_inspected(docker, tmp_path):
    settings = started(docker, tmp_path)
    docker.add(settings)
    (settings.data_dir / "arcade").mkdir(exist_ok=True)
    record = settings.data_dir / "arcade" / "container.json"
    record.write_text(json.dumps({"name": container_name(settings)}))
    name = container_name(settings)
    for problem in ("daemon down", "odd reply"):
        docker.up, docker.odd_reply = problem != "daemon down", problem == "odd reply"
        with pytest.raises(ArcadeError) as refused:
            stop_docker_engine(settings)
        assert str(refused.value).startswith(f"Docker cannot be asked about the engine container {name}: ")
        assert str(refused.value).endswith("Nothing was started or stopped.")
        assert record.exists() and docker.verbs("stop", "rm") == []


def test_stop_stops_and_removes_this_installations_container_and_its_record(docker, tmp_path):
    settings = started(docker, tmp_path)
    name = docker.add(settings)
    record = settings.data_dir / "arcade" / "container.json"
    record.parent.mkdir(exist_ok=True)
    record.write_text(json.dumps({"name": name}))
    assert stop_docker_engine(settings) == {"stopping": True, "container": name}
    assert docker.verbs("stop", "rm") == [["stop", "--time", "60", name], ["rm", name]]
    assert not record.exists() and name not in docker.containers
    record.write_text(json.dumps({"name": name}))
    result = stop_docker_engine(settings)  # now absent
    assert result["stopping"] is False and name in result["reason"] and not record.exists()


def test_a_record_of_another_installation_is_refused(docker, tmp_path):
    settings = started(docker, tmp_path)
    record = settings.data_dir / "arcade" / "container.json"
    record.parent.mkdir(exist_ok=True)
    record.write_text(json.dumps({"name": "cairnmind-arcadedb-000000000000"}))
    with pytest.raises(ArcadeError) as refused:
        stop_docker_engine(settings)
    assert str(refused.value) == RECORD_NOT_OURS.format(recorded="cairnmind-arcadedb-000000000000",
                                                        name=container_name(settings))
    assert docker.calls == [] and record.exists()


def test_the_docker_start_is_refused_while_the_native_engine_runs(docker, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "native_engine_pid", lambda settings: 4242)
    settings = started(docker, tmp_path)
    with pytest.raises(ArcadeError) as refused:
        start_docker_engine(settings)
    assert str(refused.value) == NATIVE_RUNS.format(pid=4242) and "cairnmind engine stop" in str(refused.value)
    assert docker.verbs("run") == [] and docker.calls == []


def test_a_foreign_or_exited_container_at_start(docker, tmp_path):
    settings = started(docker, tmp_path)
    name = docker.add(settings, ours=False)
    with pytest.raises(ArcadeError, match="does not touch it"):
        start_docker_engine(settings)
    assert docker.verbs("rm", "run") == []
    docker.containers.clear()
    docker.add(settings, state="exited")
    start_docker_engine(settings)
    assert [call[0] for call, _ in docker.calls if call[0] in ("rm", "run")] == ["rm", "run"]
    docker.calls.clear()
    docker.containers.clear()
    docker.add(settings, image="ghcr.io/piotrwozn/cairnmind-arcadedb@sha256:" + "cd" * 32)
    with pytest.raises(ArcadeError) as other:
        start_docker_engine(settings)
    assert str(other.value) == OTHER_IMAGE.format(name=name, running="ghcr.io/piotrwozn/cairnmind-arcadedb@sha256:"
                                                  + "cd" * 32, wanted=PUBLISHED)
    docker.containers.clear()
    docker.add(settings)
    docker.calls.clear()
    assert start_docker_engine(settings)["available"] and docker.verbs("run", "rm", "stop") == []


def test_a_container_that_stops_or_never_answers_during_startup(docker, tmp_path, monkeypatch):
    settings = started(docker, tmp_path)
    docker.answers, docker.state_after_run = False, "exited"
    with pytest.raises(ArcadeError) as exited:
        start_docker_engine(settings)
    assert str(exited.value) == EXITED.format(name=container_name(settings)) and "docker logs" in str(exited.value)
    docker.containers.clear()
    docker.state_after_run = "running"
    monkeypatch.setattr(engine_docker, "WAIT_ROUNDS", 3)
    with pytest.raises(ArcadeError) as late:
        start_docker_engine(settings)
    name = container_name(settings)
    assert docker.verbs("stop") == [["stop", "--time", "60", name]]
    assert str(late.value) == TIMED_OUT.format(name=name, seconds=0)


def test_docker_mode_refuses_root_an_unsafe_path_and_a_short_password(docker, tmp_path, monkeypatch):
    settings = started(docker, tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(os, "getuid", lambda: 0)
        with pytest.raises(ArcadeError) as root:
            start_docker_engine(settings)
    assert str(root.value) == ROOT_REFUSED
    odd = Settings.load(tmp_path / "da:ta", create=True)
    with pytest.raises(ArcadeError) as unsafe:
        start_docker_engine(odd)
    assert str(unsafe.value) == PATH_UNSAFE.format(path=str(odd.data_dir / "arcade"))
    settings.arcade_password = "1234567"
    with pytest.raises(ArcadeError) as short:
        start_docker_engine(settings)
    assert str(short.value) == SHORT_PASSWORD
    assert docker.calls == []


# --- The engine in each mode -----------------------------------------------------------------------------------

def test_engine_status_in_docker_mode_names_the_container_and_looks_for_no_java(docker, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "resolve_java", lambda settings: pytest.fail("Java was looked for"))
    monkeypatch.setattr(engine, "managed_java_executable", lambda settings: pytest.fail("Java was looked for"))
    settings = started(docker, tmp_path, engine_mode="docker")
    name = container_name(settings)

    def asked():
        status = engine.engine_status(settings)
        shown = runner.invoke(app, ["--data-dir", str(settings.data_dir), "engine", "status"])
        assert shown.exit_code == 0, shown.output
        assert json.loads(shown.stdout)["container"] == status["container"]
        return status

    docker.add(settings)
    running = asked()
    assert running["engine"] == "docker" and running["available"] and running["version_matches"]
    assert running["container"] == {"name": name, "state": "running"} and running["image"] == PUBLISHED
    docker.containers.clear()
    docker.answers = False
    absent = asked()
    assert absent["available"] is False and absent["container"] == {"name": name, "state": "absent"}
    assert absent["error"] == "Local ArcadeDB is not running; start it with `cairnmind engine start`."
    docker.add(settings, ours=False)
    foreign = asked()
    assert foreign["container"] == {"name": name, "state": "not ours", "error": NOT_OURS.format(name=name)}
    docker.up = False
    unknown = asked()["container"]
    assert unknown["state"] == "unknown" and unknown["error"].startswith(
        f"Docker cannot be asked about the engine container {name}: ")
    docker.up, docker.odd_reply = True, True
    assert asked()["container"]["state"] == "unknown"


def test_a_stored_docker_mode_without_docker_is_an_error_naming_docker_never_a_native_start(docker, tmp_path,
                                                                                            monkeypatch):
    def missing(arguments, **options):
        raise ArcadeError(engine_docker.DOCKER_NOT_FOUND)

    monkeypatch.setattr(engine_docker, "run_docker", missing)
    monkeypatch.setattr(engine, "start_engine", lambda settings: pytest.fail("the native engine was started"))
    settings = started(docker, tmp_path, engine_mode="docker")
    docker.answers = False
    with pytest.raises(ArcadeError) as refused:
        engine.ensure_engine(settings)
    assert "docker was not found on PATH." in str(refused.value) and "cairnmind engine start --native" in str(
        refused.value)


def test_ensure_engine_never_downloads_an_image_and_starts_the_container_of_the_stored_image(docker, tmp_path,
                                                                                            monkeypatch):
    settings = started(docker, tmp_path, engine_mode="docker")
    docker.answers = False
    docker.images.clear()
    with pytest.raises(ArcadeError, match="is not on this machine"):
        engine.ensure_engine(settings)
    assert docker.verbs("pull", "build", "run") == []
    docker.images.add(PUBLISHED)
    docker.answers = True
    engine.ensure_engine(settings)  # answers: nothing to start
    assert docker.verbs("run") == []
    docker.answers = False
    docker.state_after_run = "running"

    def answers_once_started(*_):
        if docker.verbs("run"):
            return {"version": "26.11.1"}
        raise ArcadeError("Local ArcadeDB is unavailable (ConnectError); start it")

    monkeypatch.setattr(ArcadeClient, "server_info", answers_once_started)
    engine.ensure_engine(settings)
    assert len(docker.verbs("run")) == 1


def test_the_native_engine_is_told_from_its_record_and_the_check_fails_closed(tmp_path, monkeypatch):
    settings = settings_with(tmp_path)
    state = settings.data_dir / "arcade"
    assert engine.native_engine_pid(settings) is None
    state.mkdir()
    record = state / "process.json"
    record.write_text("not json")
    with pytest.raises(ArcadeError, match="command line cannot be read"):
        engine.native_engine_pid(settings)
    record.write_text(json.dumps({"pid": 4242}))
    command, alive = {"value": None}, {"value": True}
    monkeypatch.setattr(engine.process_identity, "command_line", lambda pid: command["value"])
    monkeypatch.setattr(engine.process_identity, "alive", lambda pid: alive["value"])
    with pytest.raises(ArcadeError) as unknown:
        engine.native_engine_pid(settings)
    assert str(unknown.value) == engine_docker.NATIVE_UNKNOWN.format(pid=4242)
    alive["value"] = False
    assert engine.native_engine_pid(settings) is None
    command["value"] = ["bash", "-c", "something else"]
    assert engine.native_engine_pid(settings) is None  # the id was reused by another process
    command["value"] = ["java", f"-Darcadedb.server.rootPath={state}", "com.arcadedb.server.ArcadeDBServer"]
    assert engine.native_engine_pid(settings) == 4242
    command["value"] = ["java", "-Darcadedb.server.rootPath=/elsewhere", "com.arcadedb.server.ArcadeDBServer"]
    assert engine.native_engine_pid(settings) is None  # another data directory's engine


def native_harness(settings, monkeypatch):
    """start_engine up to the point where it would start Java: the process is not started, the call is captured."""
    (engine.runtime_dir(settings) / "lib").mkdir(parents=True)
    monkeypatch.setattr(engine, "engine_status", lambda settings: {"available": False})
    monkeypatch.setattr(engine, "resolve_java", lambda settings: "/usr/bin/java")
    monkeypatch.setattr(engine, "probe_java", lambda java: subprocess.CompletedProcess(
        [java], 0, "", 'openjdk version "21.0.1"'))

    class Started(Exception):
        pass

    def popen(command, **options):
        raise Started

    monkeypatch.setattr(engine.subprocess, "Popen", popen)
    return Started


def test_the_native_start_is_refused_while_a_recorded_container_runs_and_when_docker_cannot_be_asked(
        docker, tmp_path, monkeypatch):
    settings = started(docker, tmp_path)
    started_error = native_harness(settings, monkeypatch)
    with pytest.raises(started_error):  # no record: Docker is never asked
        engine.start_engine(settings)
    assert docker.calls == []
    record = settings.data_dir / "arcade" / "container.json"
    name = docker.add(settings)
    record.write_text(json.dumps({"name": name}))
    with pytest.raises(ArcadeError) as running:
        engine.start_engine(settings)
    assert str(running.value) == CONTAINER_RUNS.format(name=name) and record.exists()
    docker.up = False
    with pytest.raises(ArcadeError, match="Nothing was started or stopped") as unknown:
        engine.start_engine(settings)
    assert "Docker cannot be asked" in str(unknown.value) and record.exists()
    docker.up = True
    docker.containers.clear()
    docker.add(settings, state="exited")
    with pytest.raises(started_error):  # an exited container does not hold the directory
        engine.start_engine(settings)
    assert not record.exists()
    docker.containers.clear()
    docker.add(settings, ours=False)
    record.write_text(json.dumps({"name": name}))
    with pytest.raises(ArcadeError, match="does not touch it"):
        engine.start_engine(settings)


# --- engine start, engine up -----------------------------------------------------------------------------------

def stored(settings) -> dict:
    return json.loads((settings.data_dir / "config.json").read_text())


def cli(settings, *arguments):
    return runner.invoke(app, ["--data-dir", str(settings.data_dir), *arguments])


def test_engine_start_flags_save_the_mode_only_after_the_engine_answers(docker, tmp_path, monkeypatch):
    settings = started(docker, tmp_path)
    docker.up = False
    failed = cli(settings, "engine", "start", "--docker")
    assert isinstance(failed.exception, ArcadeError) and "Docker cannot be used" in str(failed.exception)
    assert stored(settings)["engine_mode"] == ""
    docker.up = True
    ok = cli(settings, "engine", "start", "--docker")
    assert ok.exit_code == 0, ok.output
    assert stored(settings)["engine_mode"] == "docker" and stored(settings)["engine_docker_build"] is False
    assert f"Engine mode: docker, saved as engine_mode in {settings.data_dir / 'config.json'}." in ok.output
    again = cli(settings, "engine", "start", "--docker")  # nothing changed: nothing said
    assert again.exit_code == 0 and "saved as engine_mode" not in again.output
    context = tmp_path / "image"
    context.mkdir()
    (context / "Dockerfile").write_text("FROM scratch\n")
    monkeypatch.setattr(engine_docker, "build_context", lambda: context)
    docker.containers.clear()
    built = cli(settings, "engine", "start", "--docker", "--build")
    assert built.exit_code == 0, built.output
    assert stored(settings)["engine_docker_build"] is True
    assert json.loads(built.stdout)["image"] == engine_docker.local_image_tag()
    docker.containers.clear()
    docker.calls.clear()
    plain = cli(settings, "engine", "start")  # the stored mode and the stored image
    assert plain.exit_code == 0 and docker.verbs("run")[0][-1] == engine_docker.local_image_tag()
    monkeypatch.setattr(engine, "start_engine", lambda chosen: {"available": True, "engine": "native"})
    native = cli(settings, "engine", "start", "--native")
    assert native.exit_code == 0, native.output
    assert stored(settings)["engine_mode"] == "native" and stored(settings)["engine_docker_build"] is False
    for arguments in (("--docker", "--native"), ("--build",), ("--native", "--build")):
        refused = cli(settings, "engine", "start", *arguments)
        assert refused.exit_code == 2, arguments


def test_a_stored_docker_mode_without_docker_makes_engine_start_say_so(docker, tmp_path, monkeypatch):
    settings = started(docker, tmp_path, engine_mode="docker")
    docker.up = False
    monkeypatch.setattr(engine, "start_engine", lambda chosen: pytest.fail("the native engine was started"))
    result = cli(settings, "engine", "start")
    assert isinstance(result.exception, ArcadeError)
    assert "Docker cannot be used" in str(result.exception) and "--native" in str(result.exception)
    assert stored(settings)["engine_mode"] == "docker"


def test_engine_up_is_engine_start(docker, tmp_path):
    second = settings_with(tmp_path, "two")
    first = started(docker, tmp_path, "one")
    assert cli(first, "engine", "start", "--docker").exit_code == 0
    docker.settings, start_calls = second, [call for call, _ in docker.calls]
    docker.calls.clear()
    assert cli(second, "engine", "up", "--docker").exit_code == 0
    assert [call[0] for call, _ in docker.calls] == [call[0] for call in start_calls]
    assert stored(second)["engine_mode"] == "docker"
    assert "up" in cli(first, "engine", "--help").output


def test_invalid_stored_values_are_refused_naming_them(docker, tmp_path):
    settings = started(docker, tmp_path, engine_mode="dokker")
    result = cli(settings, "engine", "status")
    assert result.exit_code == 1 and isinstance(result.exception, ValueError)
    assert str(result.exception) == MODE_INVALID.format(value="'dokker'")
    settings = started(docker, tmp_path, "other", engine_mode="docker", engine_docker_build="yes")
    result = cli(settings, "engine", "status")
    assert isinstance(result.exception, ValueError) and "engine_docker_build in config.json is 'yes'" in str(
        result.exception)


def test_engine_start_docker_through_a_docker_stand_in_on_path(tmp_path, monkeypatch):
    bin_dir, work = tmp_path / "bin", tmp_path / "work"
    bin_dir.mkdir()
    work.mkdir()
    (bin_dir / "docker").write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$FAKE_DOCKER/log"\ncase "$1" in\n'
        '  version) echo 27.3.1 ;;\n'
        '  container) if [ -e "$FAKE_DOCKER/ran" ]; then cat "$FAKE_DOCKER/inspect.json";'
        ' else echo "Error: No such container: $3" >&2; exit 1; fi ;;\n'
        '  image) exit 0 ;;\n  run) touch "$FAKE_DOCKER/ran" ;;\n  stop) ;;\n  rm) rm -f "$FAKE_DOCKER/ran" ;;\n'
        '  *) echo "unexpected $*" >&2; exit 9 ;;\nesac\n')
    (bin_dir / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER", str(work))
    monkeypatch.setattr(config, "ARCADE_IMAGE_DIGEST", DIGEST)
    monkeypatch.setattr(engine, "native_engine_pid", lambda settings: None)
    monkeypatch.setattr(engine_docker.time, "sleep", lambda seconds: None)

    def server_info(self):
        if (work / "ran").exists():
            return {"version": "26.11.1"}
        raise ArcadeError("Local ArcadeDB is unavailable (ConnectError); start it")

    monkeypatch.setattr(ArcadeClient, "server_info", server_info)
    settings = settings_with(tmp_path)
    name = container_name(settings)
    (work / "inspect.json").write_text(json.dumps([{
        "Name": "/" + name, "Config": {"Labels": labels(settings), "Image": PUBLISHED},
        "State": {"Status": "running", "Running": True}}]))
    started_engine = cli(settings, "engine", "start", "--docker")
    assert started_engine.exit_code == 0, started_engine.output
    stopped = cli(settings, "engine", "stop")
    assert stopped.exit_code == 0 and json.loads(stopped.stdout) == {"stopping": True, "container": name}
    state = settings.data_dir / "arcade"
    held = labels(settings)
    log = (work / "log").read_text().splitlines()
    assert log[0].startswith("version") and sum(line.startswith("run ") for line in log) == 1
    assert log[-2:] == [f"stop --time 60 {name}", f"rm {name}"] and not (state / "container.json").exists()
    assert next(line for line in log if line.startswith("run ")) == (
        f"run --detach --name {name} --label io.cairnmind.installation={held['io.cairnmind.installation']} "
        f"--label io.cairnmind.data-dir={settings.data_dir} --user {os.getuid()}:{os.getgid()} "
        f"--publish 127.0.0.1:{urlparse(settings.arcade_url).port}:2480 --volume {state}:/var/lib/arcadedb "
        f"--volume {state / 'root-password'}:/run/secrets/arcadedb_root_password:ro "
        f"--env ARCADEDB_JAVA_OPTS=-Darcadedb.server.mode=development --pull never {PUBLISHED}")


# --- init chooses the mode -------------------------------------------------------------------------------------

@pytest.fixture
def init_run(docker, tmp_path, monkeypatch):
    """`cairnmind init` with the store replaced, Java and Docker as each test sets them; records the order."""
    import types
    from cairnmind import cli
    order, java = [], {"path": None, "major": None}

    def resolve_java(settings):
        order.append("java")
        return java["path"]

    monkeypatch.setattr(engine, "resolve_java", resolve_java)
    monkeypatch.setattr(engine, "probe_java", lambda path: subprocess.CompletedProcess(
        [path], 0, "", f'openjdk version "{java["major"]}.0.1"') if java["major"] else subprocess.CompletedProcess(
        [path], 1, "", ""))
    monkeypatch.setattr(cli, "store_for", lambda settings: order.append("store") or types.SimpleNamespace())
    monkeypatch.setattr(engine_docker, "start_docker_engine",
                        lambda settings, **options: order.append(("container", options)))
    docker.settings = settings_with(tmp_path)

    def run(settings=None):
        return cli_run(settings or docker.settings)

    def cli_run(settings):
        return runner.invoke(app, ["--data-dir", str(settings.data_dir), "init"])

    return run, order, java, docker


def test_init_chooses_and_says_the_engine_mode(init_run, tmp_path):
    run, order, java, docker = init_run
    settings = docker.settings
    where = settings.data_dir / "config.json"
    # (a) no Java 21 and Docker answers: the container, started (and the image downloaded) before the store opens
    done_ = run()
    assert done_.exit_code == 0, done_.output
    assert engine_docker.CHOSE_DOCKER.format(config=where) in done_.stderr
    assert order == ["java", ("container", {"pull": True}), "store"]
    assert json.loads(done_.stdout)["engine_mode"] == "docker" and stored(settings)["engine_mode"] == "docker"
    # (e) a stored mode is not asked again: nothing probed, nothing said
    order.clear()
    again = run()
    assert again.exit_code == 0 and "Engine mode" not in again.stderr and "java" not in order
    # (b) Java 21 found: native, with the path
    other = settings_with(tmp_path, "java")
    java.update(path="/opt/jdk-21/bin/java", major=21)
    native = run(other)
    assert engine_docker.CHOSE_NATIVE.format(java="/opt/jdk-21/bin/java", config=other.data_dir / "config.json") \
        in native.stderr
    assert stored(other)["engine_mode"] == "native" and json.loads(native.stdout)["engine_mode"] == "native"
    assert order[-2:] == ["java", "store"]  # no container
    # Java 17 is older than 21: Docker is chosen, as when there is no Java
    old = settings_with(tmp_path, "old")
    java.update(path="/usr/bin/java", major=17)
    assert engine_docker.CHOSE_DOCKER.format(config=old.data_dir / "config.json") in run(old).stderr


def test_init_keeps_native_for_a_data_directory_that_already_has_an_engine_and_never_probes(init_run, tmp_path,
                                                                                            monkeypatch):
    run, order, java, docker = init_run
    settings = docker.settings
    (settings.data_dir / "arcade").mkdir()
    monkeypatch.setattr(engine, "resolve_java", lambda settings: pytest.fail("Java was looked for"))
    kept = run()
    assert kept.exit_code == 0, kept.output
    assert engine_docker.KEPT_NATIVE.format(config=settings.data_dir / "config.json") in kept.stderr
    assert stored(settings)["engine_mode"] == "native" and docker.calls == []


def test_init_with_neither_java_nor_docker_says_so_and_stores_nothing(init_run):
    run, order, java, docker = init_run
    docker.up = False
    failed = run()
    assert isinstance(failed.exception, ArcadeError)
    text = str(failed.exception)
    assert text.startswith("Neither Java 21 nor Docker can run the database engine here. Java: not found. Docker: ")
    assert text.endswith("Install one of them, then run `cairnmind init` again.")
    assert stored(docker.settings)["engine_mode"] == "" and "store" not in order


def test_init_of_a_connect_only_installation_probes_neither_java_nor_docker(init_run):
    run, order, java, docker = init_run
    settings = docker.settings
    settings.arcade_connect_url = "http://127.0.0.1:2999"
    settings.save()
    result = run()
    assert result.exit_code == 0, result.output
    assert "engine_mode" not in json.loads(result.stdout) and order == ["store"] and docker.calls == []


def test_init_never_passes_over_a_configured_java_for_docker(init_run, monkeypatch):
    run, order, java, docker = init_run

    def unusable(settings):
        raise ArcadeError("Configured java_executable is unavailable or not executable")

    monkeypatch.setattr(engine, "resolve_java", unusable)
    failed = run()
    assert isinstance(failed.exception, ArcadeError) and "java_executable" in str(failed.exception)
    assert stored(docker.settings)["engine_mode"] == "" and docker.calls == []


# --- doctor ----------------------------------------------------------------------------------------------------

def quiet_settings(tmp_path, name="data", **values) -> Settings:
    """A data directory whose service, database and model server are on ports nothing listens on, so a report
    asks nobody on this machine."""
    import socket
    ports = []
    for _ in range(3):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            ports.append(sock.getsockname()[1])
    return settings_with(tmp_path, name, port=ports[0], arcade_url=f"http://127.0.0.1:{ports[1]}",
                         ollama_url=f"http://127.0.0.1:{ports[2]}", **values)


@pytest.fixture
def doctor_cli(docker, monkeypatch):
    from cairnmind import cli, integrations, store_writer
    monkeypatch.setattr(integrations, "_probe_cli", lambda name: {
        "installed": False, "version": None, "verified_resume": False, "checks": {}, "notes": []})
    monkeypatch.setattr(cli, "service_alive", lambda settings: False)
    monkeypatch.setattr(store_writer, "inspect", lambda settings: {"role": "writer"})
    monkeypatch.setattr(cli, "store_for_read", lambda settings: (_ for _ in ()).throw(ArcadeError("no store")))

    def report(settings):
        result = cli_(settings)
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)

    def cli_(settings):
        return runner.invoke(app, ["--data-dir", str(settings.data_dir), "doctor"])

    return report


def test_doctor_in_docker_mode_needs_no_java_names_the_mode_and_exits_0(docker, doctor_cli, tmp_path, monkeypatch):
    from cairnmind.doctor_attention import attention
    native = doctor_cli(quiet_settings(tmp_path, "native"))
    assert native["arcadedb"]["engine"] == "native" and "docker" not in native
    assert set(native["java"]) >= {"on_path", "minimum_major"}
    monkeypatch.setattr(engine, "resolve_java", lambda settings: pytest.fail("Java was looked for"))
    settings = quiet_settings(tmp_path, engine_mode="docker")
    report = doctor_cli(settings)
    assert report["java"] == {"needed": False, "reason": engine_docker.JAVA_NOT_NEEDED}
    assert report["arcadedb"]["engine"] == "docker"
    assert report["docker"] == {"available": True, "server_version": "27.3.1",
                                "container": {"name": container_name(settings), "state": "absent"}}
    assert not any("Java" in line or "Docker" in line for line in attention(report))
    docker.up = False
    report = doctor_cli(settings)
    assert report["docker"]["available"] is False and "container" not in report["docker"]
    assert engine_docker.DOCKER_ATTENTION.format(error=report["docker"]["error"]) in attention(report)


def test_doctor_names_a_wrong_engine_mode_and_still_exits_0(docker, doctor_cli, tmp_path):
    from cairnmind.doctor_attention import attention
    report = doctor_cli(quiet_settings(tmp_path, engine_mode="dokker"))
    assert report["arcadedb"]["engine"] == "unknown"
    assert report["arcadedb"]["engine_problem"] == MODE_INVALID.format(value="'dokker'")
    assert MODE_INVALID.format(value="'dokker'") in attention(report) and "docker" not in report


# --- Words ---------------------------------------------------------------------------------------------------

def test_every_engine_docker_message_has_a_polish_translation_with_the_same_placeholders():
    from cairnmind import database_mode
    from cairnmind.terminal_i18n import tr
    names = {name for name, value in vars(engine_docker).items()
             if name.isupper() and isinstance(value, str) and " " in value and name != "MESSAGES"}
    sentences = {getattr(engine_docker, name) for name in names} | {database_mode.MODE_INVALID}
    assert len(names) >= 30 and sentences <= set(engine_docker.MESSAGES)
    assert set(engine_docker.MESSAGES) <= sentences
    for english in sentences:
        polish = engine_docker.MESSAGES[english]

        def holes(text):
            return {name for _, name, _, _ in string.Formatter().parse(text) if name}
        assert polish != english and holes(polish) == holes(english), english
        assert tr(english, "pl") == polish and tr(english, "en") == english
    assert IMAGE_FILES_MISSING in sentences


# --- The container's identity ----------------------------------------------------------------------------------

def test_a_container_is_named_and_labelled_from_the_installation(tmp_path):
    first, second = settings_with(tmp_path, "one"), settings_with(tmp_path, "two")
    assert container_name(first) != container_name(second)
    assert labels(first)["io.cairnmind.data-dir"] == str(first.data_dir)
    assert CONTAINER_RUNS.format(name="x") and "Stop it with `cairnmind engine stop`" in CONTAINER_RUNS
