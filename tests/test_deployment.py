"""The database container of docs/DEPLOYMENT.md stays in step with the engine Cairnmind starts itself.

Docker is not needed here: these tests read the files. The non-required `deployment-image` CI job
builds the image and checks the running service.
"""
from __future__ import annotations

from pathlib import Path
import re
import subprocess

import pytest
import typer

from cairnmind import engine
from cairnmind.cli import app
from cairnmind.config import Settings

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "deploy" / "arcadedb" / "Dockerfile"
ENTRYPOINT = ROOT / "deploy" / "arcadedb" / "entrypoint.sh"
COMPOSE = ROOT / "deploy" / "compose.yaml"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
GUIDE = ROOT / "docs" / "DEPLOYMENT.md"


def local_engine_command(tmp_path, monkeypatch) -> list[str]:
    """The java command line start_engine builds today, captured before any process starts."""
    settings = Settings.load(tmp_path / "data", create=True)
    (engine.runtime_dir(settings) / "lib").mkdir(parents=True)
    monkeypatch.setattr(engine, "engine_status", lambda settings: {"available": False})
    monkeypatch.setattr(engine, "resolve_java", lambda settings: "/usr/bin/java")
    monkeypatch.setattr(engine, "probe_java", lambda java: subprocess.CompletedProcess(
        [java], 0, "", 'openjdk version "21.0.1"'))
    captured = []

    class Captured(Exception):
        pass

    def popen(command, **kwargs):
        captured.extend(command)
        raise Captured

    monkeypatch.setattr(engine.subprocess, "Popen", popen)
    with pytest.raises(Captured):
        engine.start_engine(settings)
    return captured


def test_image_builds_the_pinned_arcadedb_version():
    pinned = Settings.__dataclass_fields__["arcade_version"].default
    declared = re.findall(r"^ARG ARCADEDB_VERSION=(\S+)$", DOCKERFILE.read_text(), re.M)
    assert declared == [pinned]
    # The release is checked against its published SHA-256, as engine.install_engine does.
    assert "sha256sum -c" in DOCKERFILE.read_text()


def test_image_starts_the_server_with_the_options_of_the_local_engine(tmp_path, monkeypatch):
    command = local_engine_command(tmp_path, monkeypatch)
    script = ENTRYPOINT.read_text()
    assert command[-1] == "com.arcadedb.server.ArcadeDBServer" and command[-1] in script
    # Options without a path, host or port of this machine are passed exactly as they are.
    fixed = [option for option in command[1:-3] if "=" not in option or option.startswith(
        ("--add-modules=", "--enable-native-access=", "-Djava.awt.", "-Dfile.", "-Darcadedb.profile="))]
    assert fixed and [option for option in fixed if option not in script] == []
    # Every server setting the local engine gives is given in the image too, with the image's own value.
    settings = [option.split("=", 1)[0] for option in command if option.startswith("-D") and option not in fixed]
    assert settings and [name for name in settings if name + "=" not in script] == []


def test_entrypoint_is_a_valid_shell_script_that_refuses_to_start_without_a_password(tmp_path):
    subprocess.run(["sh", "-n", str(ENTRYPOINT)], check=True)
    result = subprocess.run(["sh", str(ENTRYPOINT)], capture_output=True, text=True, env={
        "PATH": "/usr/bin:/bin", "ARCADEDB_HOME": str(tmp_path / "home"), "ARCADEDB_DATA": str(tmp_path / "data"),
        "ARCADEDB_ROOT_PASSWORD_FILE": str(tmp_path / "missing")})
    assert result.returncode != 0 and "root password" in result.stderr


def test_no_password_is_baked_into_the_image_or_committed_in_the_compose_file():
    for path in (DOCKERFILE, ENTRYPOINT, COMPOSE):
        text = path.read_text()
        assert "rootPassword=" not in text and not re.search(r"(?i)password\s*[:=]\s*['\"]?\w{8,}", text), path
    compose = COMPOSE.read_text()
    # The password reaches the server as a file, from a path the operator names in the environment.
    assert "${ARCADEDB_ROOT_PASSWORD_FILE:?" in compose
    assert "rootPasswordPath" in ENTRYPOINT.read_text()


def test_compose_publishes_the_http_port_on_loopback_only():
    ports = re.findall(r"^\s+- \"([^\"]+):2480\"$", COMPOSE.read_text(), re.M)
    assert ports and all(port.startswith("${ARCADEDB_BIND:-127.0.0.1}:") for port in ports)


def test_ci_builds_the_image_in_a_job_of_its_own_beside_the_required_ones():
    text = WORKFLOW.read_text()
    jobs = re.findall(r"^  ([a-z][a-z-]*):$", text, re.M)
    assert {"lint", "fast-tests", "full-tests", "coverage", "deployment-image"} <= set(jobs)
    job = text.split("\n  deployment-image:\n", 1)[1].split("\n  coverage-badge:\n", 1)[0]
    assert "scripts/deployment_check.py" in job and "deploy/" in (ROOT / "scripts" / "deployment_check.py").read_text()


def cli_commands() -> set[tuple[str, ...]]:
    found = set()

    def walk(command, path):
        found.add(path)
        for name, sub in getattr(command, "commands", {}).items():
            walk(sub, (*path, name))

    walk(typer.main.get_command(app), ())
    return found


def test_every_cairnmind_command_in_the_guide_exists():
    commands = cli_commands()
    used = []
    for line in GUIDE.read_text().splitlines():
        match = re.match(r"\s*(?:\$ )?cairnmind (.+)", line)
        if not match:
            continue
        # Options, their values (paths and variables) and file names are left out.
        words = [word for word in match.group(1).split() if re.fullmatch(r"[a-z][a-z-]*", word)]
        used.append(tuple(words[:2]))
    assert used
    unknown = [words for words in used if words[:2] not in commands and words[:1] not in commands]
    assert unknown == []
