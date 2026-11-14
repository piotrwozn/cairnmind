"""The database container of docs/DEPLOYMENT.md stays in step with the engine Cairnmind starts itself.

Docker is not needed here: these tests read the files. The non-required `deployment-image` CI job
builds the image and checks the running service.
"""
from __future__ import annotations

import os
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


def options_missing_from(script: str, options: list[str], machine: tuple[str, ...]) -> list[str]:
    """The JVM options that `script` does not pass. Each is looked for exactly as it is, except one whose
    value is a path, host or port of this machine: that is looked for by its name, with any value."""
    missing = []
    for option in options:
        name, equals, value = option.partition("=")
        local = equals and (name.endswith("Port") or any(part in value for part in machine))
        if (name + "=" if local else option) not in script:
            missing.append(option)
    return missing


def test_the_option_check_wants_every_option_exactly_unless_its_value_belongs_to_the_machine():
    script = "java -Xmx1024m -Dname.rootPath=\"$data\" -Dname.httpIncomingPort=\"$port\""
    machine = ("/home/someone/data", "127.0.0.1")
    assert options_missing_from(script, ["-Xmx1024m", "-Dname.rootPath=/home/someone/data/arcade",
                                         "-Dname.httpIncomingPort=2481"], machine) == []
    new = ["-XX:MaxRAMPercentage=50", "-Xlog:gc=info", "--add-opens=java.base/java.lang=ALL-UNNAMED",
           "-Xmx2048m", "-Dname.other=1"]
    assert options_missing_from(script, new, machine) == new


def test_image_starts_the_server_with_the_options_of_the_local_engine(tmp_path, monkeypatch):
    command = local_engine_command(tmp_path, monkeypatch)
    script = ENTRYPOINT.read_text()
    assert command[-3] == "-cp" and command[-1] == "com.arcadedb.server.ArcadeDBServer" and command[-1] in script
    # Every option before the class path, the ones start_engine may add later included.
    assert options_missing_from(script, command[1:-3], (str(tmp_path), "127.0.0.1")) == []


def test_entrypoint_is_a_valid_shell_script_that_refuses_to_start_without_a_password(tmp_path):
    subprocess.run(["sh", "-n", str(ENTRYPOINT)], check=True)
    result = subprocess.run(["sh", str(ENTRYPOINT)], capture_output=True, text=True, env={
        "PATH": "/usr/bin:/bin", "ARCADEDB_HOME": str(tmp_path / "home"), "ARCADEDB_DATA": str(tmp_path / "data"),
        "ARCADEDB_ROOT_PASSWORD_FILE": str(tmp_path / "missing")})
    assert result.returncode != 0 and "root password" in result.stderr


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="the non-root start is checked")
def test_each_start_leaves_one_copy_of_the_password_in_the_temporary_folder(tmp_path):
    # A restarted container keeps its /tmp: the copies of earlier starts must not pile up there.
    bin_dir, temporary = tmp_path / "bin", tmp_path / "tmp"
    bin_dir.mkdir()
    temporary.mkdir()
    (bin_dir / "java").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "java").chmod(0o755)
    password = tmp_path / "password"
    password.write_text("a long enough password\n")
    environment = {"PATH": f"{bin_dir}:/usr/bin:/bin", "TMPDIR": str(temporary), "ARCADEDB_HOME": str(tmp_path / "home"),
                   "ARCADEDB_DATA": str(tmp_path / "data"), "ARCADEDB_ROOT_PASSWORD_FILE": str(password)}
    for _ in range(3):
        subprocess.run(["sh", str(ENTRYPOINT)], check=True, env=environment)
    copies = list(temporary.glob("arcadedb-root-password.*"))
    assert len(copies) == 1 and copies[0].read_text() == "a long enough password"
    assert copies[0].stat().st_mode & 0o077 == 0


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


def unknown_commands(text: str, commands: set[tuple[str, ...]]) -> list[tuple[str, ...]]:
    """The `cairnmind ...` commands of a Markdown text, at the start of a line or in backticks, that do not exist."""
    groups = {path[:-1] for path in commands if path}
    used = re.findall(r"^\s*(?:\$ )?cairnmind ([^`\n]+)", text, re.M) + re.findall(r"`cairnmind ([^`\n]+)`", text)
    unknown = []
    for arguments in used:
        # Comments, options, their values (paths and variables) and file names are left out.
        words = [word for word in arguments.split("#", 1)[0].split() if re.fullmatch(r"[a-z][a-z-]*", word)]
        path: tuple[str, ...] = ()
        for word in words:
            if path not in groups:
                break
            path = (*path, word)
        # Each word must name a command until one without subcommands is reached.
        if path not in commands or path in groups:
            unknown.append(tuple(words[:3]))
    return unknown


def test_the_guide_check_catches_a_wrong_word_at_every_level_and_in_prose():
    commands = cli_commands()
    assert unknown_commands("cairnmind project list\n`cairnmind engine stop`, `cairnmind doctor --json`", commands) == []
    text = "cairnmind project lst\n  cairnmind engine strat\nRun `cairnmind projects list`.\n`cairnmind engine`"
    assert unknown_commands(text, commands) == [("project", "lst"), ("engine", "strat"), ("projects", "list"),
                                                ("engine",)]


def test_every_cairnmind_command_in_the_guide_exists():
    text = GUIDE.read_text()
    assert len(re.findall(r"`cairnmind [a-z]", text)) > 5
    assert unknown_commands(text, cli_commands()) == []
