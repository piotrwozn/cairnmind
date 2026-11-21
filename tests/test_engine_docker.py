"""The database engine in Docker (#272): every Docker call is replaced here, no daemon is needed."""
from __future__ import annotations

import json
import os
import re
import string
import subprocess

import pytest

from cairnmind import config, engine, engine_docker
from cairnmind.arcade import ArcadeClient, ArcadeError
from cairnmind.config import Settings
from cairnmind.database_mode import MODE_INVALID, describe, engine_mode
from cairnmind.engine_docker import (CONTAINER_RUNS, EXITED, IMAGE_FILES_MISSING, NATIVE_RUNS, NOT_OURS,
                                     OTHER_IMAGE, PATH_UNSAFE, RECORD_NOT_OURS, ROOT_REFUSED, SHORT_PASSWORD,
                                     TIMED_OUT, UNPUBLISHED, container_name, labels, start_docker_engine,
                                     stop_docker_engine)

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


def started(docker, tmp_path, **values):
    settings = settings_with(tmp_path, **values)
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
