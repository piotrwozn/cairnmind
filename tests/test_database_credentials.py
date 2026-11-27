"""Database access outside this machine: the password's source, the transport rules and TLS (#72).

No engine: the rules are checked at the client's construction and with a local TLS endpoint made by
the openssl command, which the test skips when it is missing.
"""
from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import threading

import httpx
import pytest

from cairnmind.agents.agent_journal import AgentJournal
from cairnmind.storage.arcade import ArcadeClient, ArcadeError
from cairnmind.config import Settings

SECRET = "Db-secret-7f3a9c41e2b8d6"
STORED = "Stored-secret-0b1c2d3e4f5a"


@pytest.fixture(autouse=True)
def no_database_environment(monkeypatch):
    for prefix in ("CAIRNMIND_", "DEVBRAIN_"):
        for name in ("ARCADE_URL", "ARCADE_USER", "ARCADE_PASSWORD", "ARCADE_DATABASE", "ARCADE_DB_PREFIX",
                     "ARCADE_PASSWORD_FILE", "ARCADE_CA_FILE"):
            monkeypatch.delenv(prefix + name, raising=False)


def config(root: Path, **values) -> Path:
    from cairnmind.pal import private_files
    if sys.platform == "win32":
        if not root.exists():
            private_files.ensure_private_dir(root)
    else:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    (root / "config.json").write_text(json.dumps({"token": "t" * 43, **values}), encoding="utf-8")
    private_files.make_private(root / "config.json")
    return root


def private_file(path: Path, text: str, mode: int = 0o600) -> Path:
    from cairnmind.pal import private_files
    from conftest import make_world_readable
    path.write_text(text, encoding="utf-8")
    if sys.platform == "win32":
        if (mode & 0o077) == 0:
            private_files.make_private(path)
        else:
            make_world_readable(path)
    else:
        os.chmod(path, mode)
    return path


def remote(tmp_path, **values) -> Settings:
    base = {"data_dir": tmp_path, "arcade_url": "https://db.example.org:2480", "arcade_user": "cairnmind",
            "arcade_password": SECRET, "arcade_database": "remote_test"}
    return Settings(**{**base, **values})


# --- Transport rules ---------------------------------------------------------------------------------

def test_plain_http_to_a_remote_host_is_refused_and_says_which_rule(tmp_path):
    with pytest.raises(ValueError, match="https") as refused:
        ArcadeClient(remote(tmp_path, arcade_url="http://db.example.org:2480"))
    assert SECRET not in str(refused.value)


def test_root_on_a_remote_host_is_refused_and_says_which_rule(tmp_path):
    with pytest.raises(ValueError, match="root") as refused:
        ArcadeClient(remote(tmp_path, arcade_user="root"))
    assert "db create-user" in str(refused.value) and SECRET not in str(refused.value)


def test_a_remote_host_needs_a_password_from_outside_the_settings_file(tmp_path):
    with pytest.raises(ValueError, match="CAIRNMIND_ARCADE_PASSWORD"):
        ArcadeClient(remote(tmp_path, arcade_password=""))


def test_https_to_a_remote_host_verifies_the_certificate(tmp_path):
    client = ArcadeClient(remote(tmp_path))
    verify = client._connections._verify
    assert isinstance(verify, ssl.SSLContext)
    assert verify.verify_mode == ssl.CERT_REQUIRED and verify.check_hostname
    assert verify.minimum_version >= ssl.TLSVersion.TLSv1_2
    # No setting turns it off.
    assert not any("verify" in name or "insecure" in name for name in Settings.__dataclass_fields__)


def test_https_on_loopback_is_allowed_with_the_local_rules(tmp_path):
    client = ArcadeClient(Settings(data_dir=tmp_path, arcade_url="https://127.0.0.1:2480",
                                   arcade_password=SECRET, arcade_database="local_test"))
    assert isinstance(client._connections._verify, ssl.SSLContext)


def test_a_certificate_authority_file_that_cannot_be_loaded_is_named(tmp_path):
    authority = private_file(tmp_path / "ca.pem", "not a certificate", 0o644)
    with pytest.raises(ValueError, match="arcade_ca_file"):
        ArcadeClient(remote(tmp_path, arcade_ca_file=str(authority)))
    with pytest.raises(ValueError, match="arcade_ca_file"):
        ArcadeClient(remote(tmp_path, arcade_ca_file=str(tmp_path / "absent.pem")))


# --- Where the password comes from -------------------------------------------------------------------

def test_an_existing_installation_keeps_its_stored_password_for_the_local_engine(tmp_path):
    root = config(tmp_path / "data", arcade_password=STORED)
    settings = Settings.load(root, create=True)
    assert settings.arcade_password == STORED
    assert settings._origin["password"] == "config"
    assert json.loads((root / "config.json").read_text())["arcade_password"] == STORED


def test_a_password_from_the_environment_is_never_written_to_the_settings_file(tmp_path, monkeypatch):
    root = config(tmp_path / "data", arcade_password=STORED)
    monkeypatch.setenv("CAIRNMIND_ARCADE_PASSWORD", SECRET)
    settings = Settings.load(root, create=True)
    assert settings.arcade_password == SECRET
    settings.save()
    saved = (root / "config.json").read_text()
    assert SECRET not in saved and json.loads(saved)["arcade_password"] == STORED


def test_a_url_and_user_from_the_environment_are_not_written_either(tmp_path, monkeypatch):
    root = config(tmp_path / "data", arcade_password=STORED)
    monkeypatch.setenv("CAIRNMIND_ARCADE_URL", "http://localhost:2999")
    monkeypatch.setenv("CAIRNMIND_ARCADE_USER", "someone")
    settings = Settings.load(root)
    settings.port = 9999
    settings.save()
    saved = json.loads((root / "config.json").read_text())
    assert saved["port"] == 9999 and "arcade_url" not in saved and "arcade_user" not in saved


def test_a_password_file_is_read_and_never_written_to_the_settings_file(tmp_path):
    secret = private_file(tmp_path / "db-password", SECRET + "\n")
    root = config(tmp_path / "data", arcade_url="https://db.example.org", arcade_user="cairnmind",
                  arcade_password_file=str(secret))
    settings = Settings.load(root, create=True)
    assert settings.arcade_password == SECRET and settings._origin["password"] == "file"
    settings.save()
    assert SECRET not in (root / "config.json").read_text()
    assert "arcade_password" not in json.loads((root / "config.json").read_text())


@pytest.mark.parametrize("problem", ["mode", "link", "missing", "relative", "empty", "spaces"])
def test_a_password_file_that_breaks_a_rule_is_refused_and_named(tmp_path, problem):
    from conftest import make_symlink_or_skip, make_world_readable
    target = private_file(tmp_path / "db-password", SECRET)
    named = str(target)
    if problem == "mode":
        make_world_readable(target)
    elif problem == "link":
        make_symlink_or_skip(tmp_path / "link", target)
        named = str(tmp_path / "link")
    elif problem == "missing":
        named = str(tmp_path / "absent")
    elif problem == "relative":
        named = "db-password"
    elif problem == "empty":
        private_file(target, "\n")
    elif problem == "spaces":
        private_file(target, SECRET + " " + SECRET)
    root = config(tmp_path / "data", arcade_url="https://db.example.org", arcade_user="cairnmind",
                  arcade_password_file=named)
    settings = Settings.load(root, create=True)
    assert settings.arcade_password == ""
    with pytest.raises(ValueError, match="arcade_password_file") as refused:
        ArcadeClient(settings)
    assert SECRET not in str(refused.value) and SECRET not in repr(settings)
    assert SECRET not in (root / "config.json").read_text()


# --- A password file mounted from a Kubernetes secret (#211) ------------------------------------------

TIMESTAMP = "..2026_11_15_10_20_30.123456789"


def mounted_secret(folder: Path, text: str = SECRET + "\n", mode: int = 0o400, key: str = "password") -> Path:
    """A folder laid out as the kubelet mounts a secret: the key is a link to ``..data/key``, ``..data``
    is a link to a folder named after the time of the update, and that folder holds the real file."""
    if sys.platform == "win32":
        pytest.skip("Kubernetes secret symlink mounts are POSIX-only")
    (folder / TIMESTAMP).mkdir(parents=True)
    private_file(folder / TIMESTAMP / key, text, mode)
    (folder / "..data").symlink_to(TIMESTAMP)
    (folder / key).symlink_to(f"..data/{key}")
    return folder / key


def password_from(tmp_path, named: Path) -> Settings:
    root = config(tmp_path / "data", arcade_url="https://db.example.org", arcade_user="cairnmind",
                  arcade_password_file=str(named))
    return Settings.load(root, create=True)


@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_a_password_file_mounted_from_a_kubernetes_secret_is_read(tmp_path, mode):
    settings = password_from(tmp_path, mounted_secret(tmp_path / "secret", mode=mode))
    assert settings.arcade_password == SECRET and settings._origin["password"] == "file"
    assert ArcadeClient(settings)
    assert SECRET not in (tmp_path / "data" / "config.json").read_text()


def test_a_secret_updated_by_the_kubelet_is_read_from_its_new_folder(tmp_path):
    named = mounted_secret(tmp_path / "secret", text="Old-secret-1234567")
    newer = tmp_path / "secret" / "..2026_11_16_08_00_00.000000001"
    newer.mkdir()
    private_file(newer / "password", SECRET, 0o400)
    (tmp_path / "secret" / "..data_tmp").symlink_to(newer.name)
    os.replace(tmp_path / "secret" / "..data_tmp", tmp_path / "secret" / "..data")
    assert password_from(tmp_path, named).arcade_password == SECRET


def refused_secret(tmp_path, named: Path) -> str:
    settings = password_from(tmp_path, named)
    assert settings.arcade_password == ""
    with pytest.raises(ValueError, match="arcade_password_file") as refused:
        ArcadeClient(settings)
    message = str(refused.value)
    assert SECRET not in message and SECRET not in repr(settings)
    return message


@pytest.mark.parametrize("mode", [0o440, 0o444, 0o404, 0o640])
def test_a_mounted_secret_that_group_or_others_can_read_is_refused(tmp_path, mode):
    message = refused_secret(tmp_path, mounted_secret(tmp_path / "secret", mode=mode))
    assert "group and others" in message and "defaultMode 0400" in message


def build_link(tmp_path, shape: str) -> Path:
    folder = tmp_path / "secret"
    named = mounted_secret(folder)
    outside = tmp_path / "outside"
    outside.mkdir()
    private_file(outside / "password", SECRET, 0o400)
    data = folder / "..data"
    if shape == "key leaves the folder":
        named.unlink()
        named.symlink_to("../outside/password")
    elif shape == "key is absolute":
        named.unlink()
        named.symlink_to(outside / "password")
    elif shape == "key to a file beside it":
        named.unlink()
        private_file(folder / "real", SECRET)
        named.symlink_to("real")
    elif shape == "data leaves the folder":
        data.unlink()
        data.symlink_to("../outside")
    elif shape == "data is absolute":
        data.unlink()
        data.symlink_to(outside)
    elif shape == "data is a link to a link":
        (folder / "..hop").symlink_to(TIMESTAMP)
        data.unlink()
        data.symlink_to("..hop")
    elif shape == "loop":
        data.unlink()
        data.symlink_to("..data")
    elif shape == "target is a directory":
        (folder / TIMESTAMP / "password").unlink()
        (folder / TIMESTAMP / "password").mkdir()
    elif shape == "target is a link":
        (folder / TIMESTAMP / "password").unlink()
        (folder / TIMESTAMP / "password").symlink_to(outside / "password")
    elif shape == "key under another name":
        named.unlink()
        named.symlink_to("..data/other")
        private_file(folder / TIMESTAMP / "other", SECRET, 0o400)
    return named


@pytest.mark.parametrize("shape", [
    "key leaves the folder", "key is absolute", "key to a file beside it", "data leaves the folder",
    "data is absolute", "data is a link to a link", "loop", "target is a directory", "target is a link",
    "key under another name"])
def test_every_other_link_is_refused_with_a_message_naming_the_rule(tmp_path, shape):
    message = refused_secret(tmp_path, build_link(tmp_path, shape))
    assert "a link is followed only as Kubernetes mounts a secret" in message


def test_a_mounted_secret_of_another_user_is_refused(tmp_path, monkeypatch):
    named = mounted_secret(tmp_path / "secret")
    monkeypatch.setattr(os, "getuid", lambda: os.stat(named).st_uid + 1)
    assert "of this user" in refused_secret(tmp_path, named)


@pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="POSIX chmod 0000 permission test (skipped as root or on Windows)")
@pytest.mark.parametrize("blocked", ["the file", "its folder"])
def test_an_ordinary_file_that_cannot_be_opened_says_why_and_not_the_link_rule(tmp_path, blocked):
    folder = tmp_path / "private"
    folder.mkdir()
    named = private_file(folder / "db-password", SECRET)
    locked = named if blocked == "the file" else folder
    os.chmod(locked, 0o000 if blocked == "the file" else 0o600)
    try:
        message = refused_secret(tmp_path, named)
    finally:
        os.chmod(locked, 0o600 if blocked == "the file" else 0o700)
    assert "cannot be opened (Permission denied)" in message and "link" not in message


def test_a_password_file_that_is_a_directory_is_refused_with_the_rule_and_the_setting(tmp_path):
    """The trial of 0.11.0 (#242): a folder named as the file gave "[Errno 21] Is a directory: 3"."""
    folder = tmp_path / "a-folder"
    folder.mkdir(mode=0o700)
    message = refused_secret(tmp_path, folder)
    assert f"arcade_password_file names {folder}, which must be a regular file of this user" in message
    assert "Errno" not in message


def test_a_file_that_becomes_a_link_before_it_is_opened_is_refused_with_the_link_rule(tmp_path, monkeypatch):
    from conftest import make_symlink_or_skip
    target = private_file(tmp_path / "db-password", SECRET)
    named = tmp_path / "link"
    make_symlink_or_skip(named, target)
    regular = os.lstat(target)
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):  # the check sees the regular file it was before the swap
        return regular if str(path) == str(named) else real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat)
    assert "a link is followed only as Kubernetes mounts a secret" in refused_secret(tmp_path, named)


def test_a_stored_password_is_not_used_for_a_remote_host(tmp_path):
    root = config(tmp_path / "data", arcade_url="https://db.example.org", arcade_user="cairnmind",
                  arcade_password=STORED)
    settings = Settings.load(root, create=True)
    assert settings.arcade_password == "" and settings._origin["password"] == "config_refused"
    with pytest.raises(ValueError, match="config.json") as refused:
        ArcadeClient(settings)
    assert STORED not in str(refused.value)
    # The stored value is kept as it was: going back to the local engine still works.
    assert json.loads((root / "config.json").read_text())["arcade_password"] == STORED


def test_no_password_is_generated_for_a_remote_host(tmp_path):
    root = config(tmp_path / "data", arcade_url="https://db.example.org", arcade_user="cairnmind")
    settings = Settings.load(root, create=True)
    assert settings.arcade_password == ""
    assert "arcade_password" not in json.loads((root / "config.json").read_text())


def test_a_new_local_installation_still_gets_a_generated_password(tmp_path):
    settings = Settings.load(tmp_path / "data", create=True)
    assert len(settings.arcade_password) >= 32 and settings._origin["password"] == "config"
    assert json.loads((tmp_path / "data" / "config.json").read_text())["arcade_password"] == settings.arcade_password


def test_the_password_is_not_part_of_the_settings_repr(tmp_path):
    assert SECRET not in repr(remote(tmp_path))


# --- What doctor says, and that no secret reaches it, an error or the journal ---------------------

def doctor_block(settings, monkeypatch, answer):
    from cairnmind.storage import database_credentials
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda self, request: answer(request))
    return database_credentials.doctor_block(settings, timeout=1.5)


def test_doctor_says_when_the_password_is_stored_in_the_settings_file(tmp_path, monkeypatch):
    settings = Settings.load(config(tmp_path / "data", arcade_password=STORED), create=True)
    block = doctor_block(settings, monkeypatch, lambda request: httpx.Response(
        200, json={"version": settings.arcade_version, "serverName": "t", "result": [{"ok": 1}]}))
    assert block["user"] == "root" and block["transport"] == "http (loopback, not encrypted)"
    assert block["password_source"] == "config.json"
    assert any("config.json" in note for note in block["notes"])
    assert block["available"] and block["database_available"]
    assert STORED not in json.dumps(block)


def test_doctor_reports_the_user_and_the_encryption_and_refuses_root_on_a_remote_host(tmp_path, monkeypatch):
    secret = private_file(tmp_path / "db-password", SECRET)
    root = config(tmp_path / "data", arcade_url="https://db.example.org", arcade_password_file=str(secret))
    settings = Settings.load(root)
    block = doctor_block(settings, monkeypatch, lambda request: pytest.fail("a refused setup sent a request"))
    assert block["user"] == "root" and block["transport"] == "https (certificate verification on)"
    # Refused before anything is sent: whether the server answers is not known (#242).
    assert block["mode"] == "connect" and block["password_source"] == "file" and block["available"] is None
    assert "root" in block["error"] and SECRET not in json.dumps(block)


def test_a_rejected_sign_in_does_not_show_the_password(tmp_path, monkeypatch):
    secret = private_file(tmp_path / "db-password", SECRET)
    settings = Settings.load(config(tmp_path / "data", arcade_url="https://db.example.org",
                                    arcade_user="cairnmind", arcade_password_file=str(secret)))
    block = doctor_block(settings, monkeypatch,
                         lambda request: httpx.Response(401, json={"error": "bad password " + SECRET}))
    assert block["available"] is False and "401" in block["error"]
    assert SECRET not in json.dumps(block)


def test_the_journal_masks_a_password_read_from_a_file(tmp_path):
    secret = private_file(tmp_path / "db-password", SECRET)
    settings = Settings.load(config(tmp_path / "data", arcade_password_file=str(secret)), create=True)
    journal = AgentJournal(settings, "session-72")
    journal.append("tool_result", {"text": "the database said " + SECRET})
    assert SECRET not in journal.path.read_text()


# --- A local TLS endpoint ----------------------------------------------------------------------------

def _openssl(*args, cwd):
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True, timeout=60)


def certificate_authority(folder: Path, address: str = "127.0.0.1") -> Path:
    """A private authority (ca.pem) and a server certificate for ``address`` signed by it, made by openssl."""
    if not shutil.which("openssl"):
        pytest.skip("the openssl command is needed to make a local certificate authority")
    folder.mkdir()
    # A config of our own, so the system one adds nothing: the openssl of macOS (LibreSSL) appends the v3_ca
    # extensions of /etc/ssl/openssl.cnf next to -addext, and OpenSSL 3 refuses an authority whose
    # basicConstraints is there twice. The key identifiers are named here, since LibreSSL adds none and
    # strict verification (the default from Python 3.13) refuses a chain without them.
    (folder / "req.cnf").write_text("[req]\ndistinguished_name = dn\n[dn]\n")
    _openssl("req", "-config", "req.cnf", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key",
             "-out", "ca.pem", "-days", "2",
             "-subj", "/CN=Cairnmind test authority", "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-addext", "subjectKeyIdentifier=hash",
             cwd=folder)
    _openssl("req", "-config", "req.cnf", "-newkey", "rsa:2048", "-nodes", "-keyout", "server.key",
             "-out", "server.csr", "-subj", "/CN=localhost", cwd=folder)
    (folder / "server.ext").write_text(f"subjectAltName=IP:{address},DNS:localhost\nbasicConstraints=CA:FALSE\n"
                                       "keyUsage=critical,digitalSignature,keyEncipherment\n"
                                       "extendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\n"
                                       "authorityKeyIdentifier=keyid\n")
    _openssl("x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
             "-out", "server.pem", "-days", "2", "-extfile", "server.ext", cwd=folder)
    return folder


@pytest.fixture
def tls_endpoint(tmp_path):
    folder = certificate_authority(tmp_path / "tls")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - the name http.server calls
            body = json.dumps({"version": "26.11.1", "serverName": "tls-test"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(folder / "server.pem", folder / "server.key")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_address[1]}", folder / "ca.pem"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_the_test_certificates_pass_strict_verification(tls_endpoint):
    # Python 3.13 verifies strictly by default, so the test chain must not rely on the laxer 3.12 default.
    url, ca_file = tls_endpoint
    context = ssl.create_default_context(cafile=ca_file)
    context.verify_flags |= ssl.VERIFY_X509_STRICT
    with httpx.Client(verify=context) as client:
        assert client.get(url).json()["serverName"] == "tls-test"


def test_a_certificate_from_an_unknown_authority_is_refused(tmp_path, tls_endpoint):
    url, _ = tls_endpoint
    client = ArcadeClient(Settings(data_dir=tmp_path, arcade_url=url, arcade_password=SECRET,
                                   arcade_database="tls_test"))
    with pytest.raises(ArcadeError, match="certificate") as refused:
        client.server_info()
    assert "arcade_ca_file" in str(refused.value) and SECRET not in str(refused.value)
    client.close()


def test_a_private_certificate_authority_is_trusted_by_path(tmp_path, tls_endpoint):
    url, authority = tls_endpoint
    client = ArcadeClient(Settings(data_dir=tmp_path, arcade_url=url, arcade_password=SECRET,
                                   arcade_database="tls_test", arcade_ca_file=str(authority)))
    assert client.server_info()["serverName"] == "tls-test"
    client.close()


def test_the_authorities_named_by_ssl_cert_file_are_the_system_ones(tmp_path, tls_endpoint, monkeypatch):
    # Documented in SECURITY.md: without arcade_ca_file, OpenSSL's defaults apply, and SSL_CERT_FILE
    # replaces them. Verification stays on: the certificate is checked against that file.
    url, authority = tls_endpoint
    monkeypatch.setenv("SSL_CERT_FILE", str(authority))
    client = ArcadeClient(Settings(data_dir=tmp_path, arcade_url=url, arcade_password=SECRET,
                                   arcade_database="tls_test"))
    assert client.server_info()["serverName"] == "tls-test"
    client.close()


def test_an_empty_current_spelling_does_not_hide_the_earlier_one(tmp_path, monkeypatch):
    root = config(tmp_path / "data", arcade_url="https://db.example.org", arcade_user="cairnmind")
    monkeypatch.setenv("CAIRNMIND_ARCADE_PASSWORD", "")
    monkeypatch.setenv("DEVBRAIN_ARCADE_PASSWORD", SECRET)
    settings = Settings.load(root)
    assert settings.arcade_password == SECRET and settings._origin["password"] == "environment"
    monkeypatch.setenv("CAIRNMIND_ARCADE_PASSWORD", STORED)
    assert Settings.load(root).arcade_password == STORED  # the current spelling still comes first


# --- A server on another host is never started here ---------------------------------------------------

def test_an_unreachable_remote_server_is_reported_and_nothing_is_started_or_written(tmp_path, monkeypatch):
    from cairnmind.storage import engine

    def unreachable(self, request):
        raise httpx.ConnectError("no route", request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", unreachable)
    (tmp_path / "runtime" / "lib").mkdir(parents=True)
    monkeypatch.setattr(engine, "runtime_dir", lambda settings: tmp_path / "runtime")
    monkeypatch.setattr(engine, "resolve_java", lambda settings: "/usr/bin/java")
    monkeypatch.setattr(engine, "probe_java", lambda java: pytest.fail("Java was run for a remote server"))
    settings = remote(tmp_path, arcade_url="https://192.0.2.1:2480")
    with pytest.raises(ArcadeError, match=r"ArcadeDB at https://192\.0\.2\.1:2480 is unavailable") as refused:
        engine.ensure_engine(settings)
    with pytest.raises(ArcadeError, match="not on this machine"):
        engine.start_engine(settings)
    assert SECRET not in str(refused.value)
    assert not (tmp_path / "arcade").exists()
    assert not any(SECRET in path.read_text(errors="replace") for path in tmp_path.rglob("*") if path.is_file())
