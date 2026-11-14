"""A dedicated database user on the real engine: created once as root, then used for everything (#72)."""
from __future__ import annotations

from contextlib import contextmanager, suppress
from dataclasses import replace
import json
import os
import secrets
import select
import socket
import ssl
import threading
import uuid

import pytest
from typer.testing import CliRunner

from cairnmind.arcade import ArcadeClient, ArcadeError
from cairnmind.cli import app
from cairnmind.config import Settings
from cairnmind.ingest import index_project
from cairnmind.store import Store
from test_database_credentials import certificate_authority


def private(path, text):
    path.write_text(text + "\n")
    os.chmod(path, 0o600)
    return path


@pytest.fixture
def dedicated_user(tmp_path, monkeypatch, arcade_test_engine):
    root_settings = arcade_test_engine[0]
    root_secret = os.environ["CAIRNMIND_ARCADE_PASSWORD"]
    for prefix in ("CAIRNMIND_", "DEVBRAIN_"):
        monkeypatch.delenv(prefix + "ARCADE_PASSWORD", raising=False)
        monkeypatch.delenv(prefix + "ARCADE_USER", raising=False)
    name = "cm_" + uuid.uuid4().hex[:10]
    secret = secrets.token_urlsafe(32)
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    (data / "config.json").write_text(json.dumps({
        "arcade_user": name, "arcade_password_file": str(private(tmp_path / "db-password", secret))}))
    os.chmod(data / "config.json", 0o600)
    admin_file = private(tmp_path / "admin-password", root_secret)
    try:
        yield data, name, secret, root_secret, admin_file
    finally:
        try:
            ArcadeClient(root_settings).server_command(f"drop user {name}")
        except ArcadeError:
            pass


def create(data, admin_file):
    return CliRunner().invoke(app, ["--data-dir", str(data), "db", "create-user",
                                    "--admin-password-file", str(admin_file)])


def test_a_dedicated_user_is_created_once_and_then_indexes_and_reads(dedicated_user, tmp_path):
    data, name, secret, root_secret, admin_file = dedicated_user
    made = create(data, admin_file)
    assert made.exit_code == 0, made.output
    report = json.loads(made.output)
    assert report["user"] == name and report["signed_in"] and report["group"] == "admin"
    assert secret not in made.output and root_secret not in made.output
    saved = (data / "config.json").read_text()
    assert secret not in saved and root_secret not in saved

    settings = Settings.load(data)
    assert settings.arcade_user == name and settings._origin["password"] == "file"
    # Store.init, the schema and its indexes, runs with database rights only.
    store = Store(data, settings=settings)
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "auth.py").write_text("def login():\n    return True\n")
    project = store.add_project("dedicated", project_root)
    assert index_project(store, project["id"])["changed"] == 1
    found = store.query("SELECT content FROM sources WHERE uri=:p0 AND is_current=true",
                        {"p0": str(project_root / "auth.py")})
    assert found and "login" in found[0]["content"]

    # The user has no rights on the server or on another database.
    other = replace(settings, arcade_database=settings.arcade_database + "_other")
    root = replace(settings, arcade_user="root", arcade_password=root_secret)
    ArcadeClient(replace(root, arcade_database=other.arcade_database)).ensure_database()
    with pytest.raises(ArcadeError) as refused:
        ArcadeClient(other).query("SELECT 1 AS ok")
    assert refused.value.status_code in (401, 403)
    with pytest.raises(ArcadeError):
        ArcadeClient(settings).server_command("create database " + settings.arcade_database + "_more")
    assert secret not in str(refused.value)


def test_creating_the_same_user_again_fails_without_showing_a_password(dedicated_user):
    data, name, secret, root_secret, admin_file = dedicated_user
    assert create(data, admin_file).exit_code == 0
    again = create(data, admin_file)
    assert again.exit_code != 0
    text = again.output + repr(again.exception)
    assert secret not in text and root_secret not in text


def test_the_user_is_not_created_while_its_password_is_in_the_settings_file(dedicated_user):
    data, name, secret, root_secret, admin_file = dedicated_user
    config = json.loads((data / "config.json").read_text())
    config.pop("arcade_password_file")
    config["arcade_password"] = secret
    (data / "config.json").write_text(json.dumps(config))
    refused = create(data, admin_file)
    assert refused.exit_code != 0 and "outside config.json" in str(refused.exception)


def test_a_wrong_admin_password_is_refused_without_showing_it(dedicated_user, tmp_path):
    data, name, secret, root_secret, admin_file = dedicated_user
    wrong = private(tmp_path / "wrong-admin", "Wrong-admin-password-1234")
    refused = create(data, wrong)
    assert refused.exit_code != 0
    text = refused.output + repr(refused.exception)
    assert "Wrong-admin-password-1234" not in text and secret not in text


# --- The acceptance scenario: the engine reached as a host other than this one, over TLS ------------

def this_machine_address() -> str | None:
    """An address of this machine that is not loopback, or None. Connecting a UDP socket sends nothing."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))
            address = probe.getsockname()[0]
        except OSError:
            return None
    return None if address.startswith("127.") or address == "0.0.0.0" else address


@contextmanager
def tls_proxy(address: str, upstream_port: int, folder):
    """A TLS endpoint on ``address`` in front of the test engine, which listens on loopback only."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(folder / "server.pem", folder / "server.key")
    listener = socket.create_server((address, 0))
    listener.settimeout(0.2)
    stop = threading.Event()
    threads = []

    def relay(raw):
        # Every TLS operation of one connection happens on this one thread.
        try:
            raw.settimeout(10)
            tls = context.wrap_socket(raw, server_side=True)
        except (OSError, ssl.SSLError):
            raw.close()
            return
        upstream = socket.create_connection(("127.0.0.1", upstream_port))
        try:
            while not stop.is_set():
                ready = [tls] if tls.pending() else select.select([tls, upstream], [], [], 0.2)[0]
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    (upstream if source is tls else tls).sendall(data)
        except (OSError, ssl.SSLError):
            pass
        finally:
            for sock in (tls, upstream):
                with suppress(OSError):
                    sock.close()

    def accept():
        while not stop.is_set():
            try:
                raw, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            thread = threading.Thread(target=relay, args=(raw,), daemon=True)
            thread.start()
            threads.append(thread)

    acceptor = threading.Thread(target=accept, daemon=True)
    acceptor.start()
    try:
        yield f"https://{address}:{listener.getsockname()[1]}"
    finally:
        stop.set()
        acceptor.join(5)
        listener.close()
        for thread in threads:
            thread.join(5)


def test_a_remote_url_a_dedicated_user_and_a_password_file_index_and_read(dedicated_user, tmp_path, monkeypatch,
                                                                         arcade_test_engine):
    address = this_machine_address()
    if address is None:
        pytest.skip("this machine has no address other than loopback")
    data, name, secret, root_secret, admin_file = dedicated_user
    folder = certificate_authority(tmp_path / "tls", address)
    engine_port = int(arcade_test_engine[0].arcade_url.rsplit(":", 1)[1])
    with tls_proxy(address, engine_port, folder) as url:
        monkeypatch.delenv("DEVBRAIN_ARCADE_URL", raising=False)
        monkeypatch.setenv("CAIRNMIND_ARCADE_URL", url)
        config = json.loads((data / "config.json").read_text())
        (data / "config.json").write_text(json.dumps({**config, "arcade_ca_file": str(folder / "ca.pem")}))
        # The setup step signs in as root once, over the same verified TLS.
        made = create(data, admin_file)
        assert made.exit_code == 0, made.output

        settings = Settings.load(data, create=True)
        assert settings.arcade_url == url and settings.arcade_user == name
        store = Store(data, settings=settings)
        project_root = tmp_path / "repo"
        project_root.mkdir()
        (project_root / "auth.py").write_text("def login():\n    return True\n")
        project = store.add_project("remote", project_root)
        assert index_project(store, project["id"])["changed"] == 1
        assert store.query("SELECT count(*) AS n FROM sources WHERE is_current=true")[0]["n"] == 1
        store.client.close()
        assert secret not in (data / "config.json").read_text()

        # Each broken rule stops with a message that names it, before anything is sent.
        with pytest.raises(ValueError, match="https://"):
            Store(data, settings=replace(settings, arcade_url=url.replace("https://", "http://")))
        with pytest.raises(ValueError, match="root is refused"):
            Store(data, settings=replace(settings, arcade_user="root"))
        # Without the private authority, the certificate is not trusted.
        with pytest.raises(ArcadeError, match="certificate"):
            ArcadeClient(replace(settings, arcade_ca_file="")).query("SELECT 1 AS ok")
