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


def test_the_image_runs_in_production_mode_without_the_mcp_plugin_and_the_local_engine_is_unchanged(
        tmp_path, monkeypatch):
    script = ENTRYPOINT.read_text()
    java = script[script.index("exec java"):]
    # Before ARCADEDB_JAVA_OPTS, so that an operator's own -Darcadedb.server.mode wins.
    assert java.index("-Darcadedb.server.mode=production") < java.index("${ARCADEDB_JAVA_OPTS:-}")
    dockerfile = DOCKERFILE.read_text()
    assert 'rm "/opt/arcadedb/lib/arcadedb-mcp-${ARCADEDB_VERSION}.jar"' in dockerfile
    assert 'test -f "/opt/arcadedb/lib/arcadedb-mcp-${ARCADEDB_VERSION}.jar"' in dockerfile
    # The local engine on loopback keeps ArcadeDB's default mode.
    assert not any("arcadedb.server.mode" in option for option in local_engine_command(tmp_path, monkeypatch))


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


# --- Kubernetes (#211). No cluster here: the `kubernetes-manifests` CI job applies them on kind. --------

KUBERNETES = ROOT / "deploy" / "kubernetes"


def manifest(name: str) -> str:
    return (KUBERNETES / name).read_text()


def test_the_statefulset_runs_the_image_compose_builds_and_probes_it_with_the_image_health_check():
    statefulset = manifest("30-statefulset.yaml")
    assert re.findall(r"^\s+image: (\S+)$", statefulset, re.M) == re.findall(r"^\s+image: (\S+)$", COMPOSE.read_text(),
                                                                             re.M) == ["cairnmind-arcadedb:local"]
    probes = re.findall(r"^\s+(startupProbe|readinessProbe|livenessProbe):\n\s+exec:\n\s+command: (.+)$",
                        statefulset, re.M)
    assert [name for name, _ in probes] == ["startupProbe", "readinessProbe", "livenessProbe"]
    assert {command for _, command in probes} == {'["/opt/arcadedb/healthcheck.sh"]'}
    assert "healthcheck.sh" in DOCKERFILE.read_text()
    # The server runs as the image's user, with the volume the image declares.
    assert "runAsUser: 10001" in statefulset and "mountPath: /var/lib/arcadedb" in statefulset
    assert "VOLUME [\"/var/lib/arcadedb\"]" in DOCKERFILE.read_text() and "volumeClaimTemplates:" in statefulset


def test_tls_ends_at_the_server_and_only_its_port_is_offered():
    statefulset = manifest("30-statefulset.yaml")
    for option in ("-Darcadedb.ssl.enabled=true", "-Darcadedb.server.httpsIncomingPort=2490",
                   "-Darcadedb.ssl.keyStore=/run/secrets/arcadedb-tls/keystore.p12",
                   "-Darcadedb.ssl.keyStorePassword=$(ARCADEDB_TLS_STORE_PASSWORD)"):
        assert option in statefulset
    # The entrypoint passes ARCADEDB_JAVA_OPTS after its own options, so these reach the server.
    assert "${ARCADEDB_JAVA_OPTS:-}" in ENTRYPOINT.read_text()
    assert re.findall(r"^\s+port: (\d+)$", manifest("20-service.yaml"), re.M) == ["2490"]
    policy = manifest("10-networkpolicy.yaml")
    assert re.findall(r"^\s+port: (\d+)$", policy, re.M) == ["2490"] and "egress: []" in policy
    assert 'cairnmind.io/database-client: "true"' in policy
    assert 'cairnmind.io/database-client: "true"' in manifest("check/connection-check.yaml")


def test_no_secret_value_is_in_the_manifests_and_the_template_is_not_applied_with_them():
    files = sorted(KUBERNETES.rglob("*.yaml"))
    assert len(files) == 8
    for path in files:
        text = path.read_text()
        assert "BEGIN" not in text and not re.search(r"(?i)password\s*[:=]\s*['\"]?\w{8,}", text), path
    template = manifest("templates/secrets.yaml")
    values = re.findall(r"^  ([\w.-]+): (.*)$", template, re.M)
    secret_values = [value for key, value in values if key in ("password", "store-password", "keystore.p12")]
    assert len(secret_values) == 4 and set(secret_values) == {'""'} and template.count("kind: Secret") == 3
    # `kubectl apply -f deploy/kubernetes` reads the top folder only: no Secret is there.
    assert all("kind: Secret" not in path.read_text() for path in KUBERNETES.glob("*.yaml"))
    # The server's Secrets are in its namespace; the user's password goes where a client pod runs.
    namespaces = dict(re.findall(r"^  name: ([\w-]+)\n  namespace: ([\w-]+)$", template, re.M))
    assert namespaces == {"arcadedb-root-password": "cairnmind-db", "arcadedb-tls": "cairnmind-db",
                          "cairnmind-db-user": "cairnmind-app"}


def test_the_manifest_check_creates_every_namespace_the_manifests_name_before_the_server_side_dry_run():
    """A server-side dry run refuses an object whose namespace does not exist (seen on kind: the template's
    placeholder namespace cairnmind-app), so the check creates each one first and deletes it at the end."""
    named = set()
    for path in KUBERNETES.rglob("*.yaml"):
        named |= set(re.findall(r"^\s+namespace: ([\w-]+)$", path.read_text(), re.M))
    declared = set(re.findall(r"^  name: ([\w-]+)$", manifest("00-namespace.yaml"), re.M))
    script = (ROOT / "scripts" / "kubernetes_check.py").read_text()
    values = dict(re.findall(r'^(TEMPLATE_CLIENTS) = "([\w-]+)"$', script, re.M))
    values.update(zip(("DATABASE", "CLIENTS"), re.findall(r'^DATABASE, CLIENTS = "([\w-]+)", "([\w-]+)"$', script,
                                                         re.M)[0]))
    created = {values[name] for name in re.findall(r'kubectl\("create", "namespace", (\w+)\)', script)}
    assert named <= declared | created
    dry_run = script.index('"--dry-run=server"')
    assert all(script.index(f'kubectl("create", "namespace", {name})') < dry_run
               for name, value in values.items() if value in created)
    cleanup = script[script.index('kubectl("delete", "namespace"'):].split(")", 1)[0]
    assert all(name in cleanup for name in ("DATABASE", "CLIENTS", "TEMPLATE_CLIENTS"))


def test_the_volume_is_unpacked_without_setting_the_root_owned_top_folder():
    """The volume pod runs as 10001, the new volume's top folder belongs to root: GNU tar fails setting
    the time of the archive's "." entry on it unless --no-overwrite-dir leaves the folder alone."""
    assert "runAsUser: 10001" in manifest("backup/volume-pod.yaml")
    script = (ROOT / "scripts" / "kubernetes_check.py").read_text()
    unpack = [call for call in re.findall(r'volume_pod\("tar",[^)]*\)', script) if '"-xzf"' in call]
    assert len(unpack) == 1 and '"--no-overwrite-dir"' in unpack[0]
    documented = re.findall(r"tar [^\n]*(?:\\\n[^\n]*)*-xzf -", (ROOT / "docs" / "DEPLOYMENT.md").read_text())
    assert len(documented) == 1 and "--no-overwrite-dir" in documented[0]


def test_the_mounted_password_mode_of_the_manifests_is_one_the_password_file_rule_accepts(tmp_path):
    from cairnmind.database_credentials import read_private_file
    from test_database_credentials import SECRET, mounted_secret
    job = manifest("check/connection-check.yaml")
    modes = set(re.findall(r"^\s+defaultMode: (0\d+)$", job, re.M))
    assert modes == {"0400"} and not re.search(r"^\s+fsGroup:", job, re.M)
    named = mounted_secret(tmp_path / "secret", mode=int(modes.pop(), 8))
    assert read_private_file(str(named), "arcade_password_file") == SECRET
    # The server's own mount is read by the entrypoint through its group (fsGroup), not by Cairnmind.
    assert set(re.findall(r"^\s+defaultMode: (0\d+)$", manifest("30-statefulset.yaml"), re.M)) == {"0440"}


def test_ci_applies_the_manifests_in_a_job_of_its_own_that_no_required_check_waits_for():
    text = WORKFLOW.read_text()
    jobs = re.findall(r"^  ([a-z][a-z-]*):$", text, re.M)
    assert {"lint", "fast-tests", "full-tests", "kubernetes-manifests"} <= set(jobs)
    job = text.split("\n  kubernetes-manifests:\n", 1)[1]
    assert "scripts/kubernetes_check.py" in job and "kind create cluster" in job and "needs:" not in job
    full_tests = text.split("\n  full-tests:\n", 1)[1].split("\n  coverage-tests:\n", 1)[0]
    assert re.search(r"^    needs: tests$", full_tests, re.M)
    script = (ROOT / "scripts" / "kubernetes_check.py").read_text()
    assert "--dry-run=server" in script and "--validate=strict" in script and "connection-check.yaml" in script
    subprocess.run(["sh", "-n", str(KUBERNETES / "check" / "connection-check.sh")], check=True)


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
