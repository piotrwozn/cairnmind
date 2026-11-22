"""Check the database container of docs/DEPLOYMENT.md end to end, as the `deployment-image` CI job does.

    uv run python scripts/deployment_check.py
    uv run python scripts/deployment_check.py --source-url http://127.0.0.1:2480 --source-password-file A \\
        --target-url http://127.0.0.1:2481 --target-password-file B

With no arguments it builds deploy/arcadedb, starts separate copies of deploy/compose.yaml on free
loopback ports, each with a new random root password, and removes them with their volumes at the end.
With the URLs given it uses two servers that are already running and have no user `cairnmind` yet,
stops nothing and skips step 5. Each password file is the root password, in a file of mode 0600.

Then, with the cairnmind CLI and a temporary data directory for each server, each a connect-only
installation (CAIRNMIND_ARCADE_CONNECT_URL) that signs in as a dedicated user with its password in a file:
  1. the server answers /api/v1/ready, and `engine status` and `doctor` see the pinned version and say
     that the installation only connects; `db create-user` makes the database and the user, as root, once;
  2. `init`, `project add` and `remember` write a project and a record to the first one;
  3. `backup` from the first and `restore` into the second, empty one;
  4. `project list` and `records` read the same project and records back from the second;
  5. the first is stopped, its volume archived with tar and unpacked into a new volume, and a third
     server started on that volume reads the same project and records.
No engine is installed or started on this machine, and no data directory gets one. No model, Ollama
or coding client is used. Exit status 0 only when every step passed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "compose.yaml"
IMAGE = "cairnmind-arcadedb:local"  # the image deploy/compose.yaml builds
PROJECT = "cairnmind-deployment-check"
USER = "cairnmind"  # the dedicated database user every installation of this check signs in as


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def step(message: str) -> None:
    print(f"== {message}", flush=True)


def wait_ready(url: str, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(f"{url}/api/v1/ready", timeout=5) as response:
                if 200 <= response.status < 300:
                    return
        except (urllib.error.URLError, OSError):
            pass
        if time.monotonic() > deadline:
            raise SystemExit(f"{url} did not answer /api/v1/ready within {timeout:.0f} seconds")
        time.sleep(1)


class Server:
    """One ArcadeDB server and the environment the cairnmind CLI reaches it with."""

    def __init__(self, name: str, url: str, password_file: Path, data_dir: Path, user_file: Path | None = None):
        self.name, self.url, self.data_dir, self.password_file = name, url.rstrip("/"), data_dir, password_file
        # The dedicated user's password: a private file of its own, never the root password.
        self.user_file = user_file or data_dir.parent / f"{name}-user-password"
        if user_file is None:
            self.user_file.write_text(secrets.token_urlsafe(24) + "\n")
            self.user_file.chmod(0o600)

    def create_user(self) -> None:
        """The one step that signs in as root: the database and its user (docs/DEPLOYMENT.md)."""
        made = self.cli("db", "create-user", "--admin-password-file", str(self.password_file))
        if not made.get("signed_in"):
            raise SystemExit(f"{self.name}: create-user did not sign in as the new user: {made}")

    def cli(self, *arguments: str, check: bool = True):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("CAIRNMIND_", "DEVBRAIN_"))}
        env.update(CAIRNMIND_ARCADE_CONNECT_URL=self.url, CAIRNMIND_ARCADE_USER=USER,
                   CAIRNMIND_ARCADE_PASSWORD_FILE=str(self.user_file), CAIRNMIND_ARCADE_DATABASE="cairnmind")
        command = [sys.executable, "-m", "cairnmind", "--data-dir", str(self.data_dir), *arguments]
        result = subprocess.run(command, capture_output=True, text=True, env=env, timeout=300)
        if check and result.returncode != 0:
            raise SystemExit(f"{self.name}: cairnmind {' '.join(arguments)} failed ({result.returncode}):\n"
                             f"{result.stdout}{result.stderr}")
        return json.loads(result.stdout) if check else result


def check_server(server: Server, pinned: str) -> None:
    wait_ready(server.url)
    server.create_user()
    status = server.cli("engine", "status")
    if not status.get("available") or not status.get("version_matches"):
        raise SystemExit(f"{server.name}: engine status does not see ArcadeDB {pinned}: {status}")
    if status.get("mode") != "connect" or "java" in status:
        raise SystemExit(f"{server.name}: the installation is not connect-only: {status}")
    refused = server.cli("engine", "start", check=False)
    if refused.returncode != 2 or "only connects to ArcadeDB" not in refused.stderr:
        raise SystemExit(f"{server.name}: engine start did not say that nothing is managed here:\n"
                         f"{refused.stdout}{refused.stderr}")
    print(f"{server.name}: ArcadeDB {status['server'].get('version')} at {server.url}, connect-only")


def no_local_engine(server: Server) -> None:
    left = [name for name in ("arcade", "runtime", "engine.lock") if (server.data_dir / name).exists()]
    if left:
        raise SystemExit(f"{server.name}: a connect-only data directory holds engine files: {left}")


def roundtrip(source: Server, target: Server, work: Path) -> tuple[list, list]:
    from cairnmind.config import Settings
    pinned = Settings.__dataclass_fields__["arcade_version"].default
    for server in (source, target):
        step(f"{server.name}: the service answers; create-user as root, once")
        check_server(server, pinned)
    step("source: init, project add, remember")
    source.cli("init")
    doctor = source.cli("doctor")["arcadedb"]
    if not (doctor.get("available") and doctor.get("version_matches") and doctor.get("database_available")
            and doctor.get("mode") == "connect" and doctor.get("user") == USER
            and doctor.get("password_source") == "file"):
        raise SystemExit(f"source: doctor does not see the database as the dedicated user: {doctor}")
    tree = work / "demo"
    tree.mkdir()
    (tree / "README.md").write_text("# Demo\n\nA project for the deployment check.\n")
    source.cli("project", "add", "demo", str(tree))
    # Not --confirmed: the command line confirms nothing, a person does in the workspace.
    source.cli("remember", "demo", "The deployment check wrote this record.", "--key", "deployment-check")
    projects, records = source.cli("project", "list"), source.cli("records", "demo")
    if not any(record.get("key") == "deployment-check" for record in records):
        raise SystemExit(f"source: the record just remembered is missing: {records}")
    step("backup from the source, restore into the empty target")
    backup = source.cli("backup", str(work / "backup.json.gz"))["backup"]
    target.cli("init")
    if target.cli("project", "list"):
        raise SystemExit("target: the second server is not empty")
    target.cli("restore", backup)
    step("the target holds the same projects and records")
    restored_projects, restored_records = target.cli("project", "list"), target.cli("records", "demo")
    if restored_projects != projects or restored_records != records:
        raise SystemExit("target: the restored projects or records differ from the source:\n"
                         f"{json.dumps([projects, records], indent=1)}\n"
                         f"{json.dumps([restored_projects, restored_records], indent=1)}")
    print(f"restored {len(restored_projects)} project(s) and {len(restored_records)} record(s)")
    for server in (source, target):
        no_local_engine(server)
    return projects, records


class Compose:
    """Copies of deploy/compose.yaml, each its own Compose project with its own volume and password."""

    def __init__(self, work: Path):
        self.work, self.started = work, {}

    def project(self, name: str) -> str:
        return f"{PROJECT}-{name}"

    def run(self, name: str, *arguments: str, check: bool = True):
        command, env = self.started[name]
        return subprocess.run([*command, *arguments], env=env, check=check)

    def server(self, name: str, password_file: Path | None = None, *, restore_volume: Path | None = None,
               user_file: Path | None = None) -> Server:
        if password_file is None:
            password_file = self.work / f"{name}-root-password"
            password_file.write_text(secrets.token_urlsafe(24) + "\n")
            password_file.chmod(0o600)
        port = free_port()
        env = {**os.environ, "ARCADEDB_ROOT_PASSWORD_FILE": str(password_file),
               "ARCADEDB_BIND": "127.0.0.1", "ARCADEDB_PORT": str(port)}
        self.started[name] = (["docker", "compose", "-f", str(COMPOSE), "-p", self.project(name)], env)
        if restore_volume is not None:
            # As docs/DEPLOYMENT.md does it: create the service and its empty volume, unpack, then start.
            self.run(name, "up", "--no-start")
            self.tar(name, "-xzf", restore_volume, read_only=False)
        step(f"{name}: docker compose up on 127.0.0.1:{port}")
        self.run(name, "up", "-d", "--build", "--wait", "--wait-timeout", "180")
        return Server(name, f"http://127.0.0.1:{port}", password_file, self.work / f"{name}-data", user_file)

    def tar(self, name: str, mode: str, archive: Path, *, read_only: bool) -> None:
        volume = f"{self.project(name)}_arcadedb-data:/var/lib/arcadedb" + (":ro" if read_only else "")
        subprocess.run(["docker", "run", "--rm", "--entrypoint", "tar", "-v", volume,
                        "-v", f"{archive.parent}:/backup", IMAGE, "--numeric-owner", "-C", "/var/lib/arcadedb",
                        mode, f"/backup/{archive.name}", *([] if mode == "-xzf" else ["."])], check=True)

    def close(self) -> None:
        for name in self.started:
            self.run(name, "logs", "--no-color", "--tail", "200", check=False)
            self.run(name, "down", "--volumes", "--timeout", "60", check=False)


def volume_snapshot(compose: Compose, source: Server, expected: tuple[list, list]) -> None:
    """Stop the source, archive its volume, unpack it into a new volume and read the same data from it."""
    step("volume snapshot of the stopped source, unpacked into a third, new volume")
    compose.run("source", "stop", "--timeout", "60")
    archive = compose.work / "arcadedb-volume.tgz"
    compose.tar("source", "-czf", archive, read_only=True)
    # The snapshot holds the server's users too: the dedicated user signs in with the source's password.
    copy = compose.server("snapshot", source.password_file, restore_volume=archive, user_file=source.user_file)
    copy.cli("init")  # a new data directory, over the existing database: it reads, the source's wrote it
    found = copy.cli("project", "list"), copy.cli("records", "demo")
    if found != expected:
        raise SystemExit(f"snapshot: the projects or records differ from the source:\n{json.dumps(found, indent=1)}")
    print(f"the snapshot holds the same {len(found[0])} project(s) and {len(found[1])} record(s)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--source-url")
    parser.add_argument("--source-password-file", type=Path)
    parser.add_argument("--target-url")
    parser.add_argument("--target-password-file", type=Path)
    args = parser.parse_args()
    given = [args.source_url, args.source_password_file, args.target_url, args.target_password_file]
    if any(given) and not all(given):
        parser.error("give both URLs and both password files, or none of them")
    with tempfile.TemporaryDirectory(prefix="cairnmind-deployment-check-") as temporary:
        work = Path(temporary)
        if all(given):
            roundtrip(Server("source", args.source_url, args.source_password_file, work / "source-data"),
                      Server("target", args.target_url, args.target_password_file, work / "target-data"), work)
        else:
            compose = Compose(work)
            try:
                source, target = compose.server("source"), compose.server("target")
                expected = roundtrip(source, target, work)
                volume_snapshot(compose, source, expected)
            finally:
                compose.close()
    step("passed")


if __name__ == "__main__":
    main()
