"""Connect-only mode (#68): an installation that uses a database it never installs, starts or stops."""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import json
import os
import secrets
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import uuid

import httpx
import pytest
from typer.testing import CliRunner

from cairnmind import engine, store_writer
from cairnmind.arcade import ArcadeClient, ArcadeError
from cairnmind.cli import app
from cairnmind.config import Settings
from cairnmind.database_mode import connect_only, mode
from cairnmind.ingest import index_project
from cairnmind.store import Store
from cairnmind.store_readonly import StoreNotReady
from cairnmind.store_writer import StoreNotWriter, StoreWriterLost
from database_relay import Relay
from test_database_credentials import certificate_authority
from test_database_user import this_machine_address

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[1]
VARIABLES = ("ARCADE_URL", "ARCADE_USER", "ARCADE_PASSWORD", "ARCADE_DATABASE", "ARCADE_PASSWORD_FILE",
             "ARCADE_CA_FILE", "ARCADE_CONNECT_URL")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def test_engine(arcade_test_engine, monkeypatch):
    """(port, root password) of the test engine, with no database variable left in the environment.

    The database prefix stays, so that every database of a test is one this run removes.
    """
    settings = arcade_test_engine[0]
    secret = os.environ["CAIRNMIND_ARCADE_PASSWORD"]
    for prefix in ("CAIRNMIND_", "DEVBRAIN_"):
        for name in VARIABLES:
            monkeypatch.delenv(prefix + name, raising=False)
    return int(settings.arcade_url.rsplit(":", 1)[1]), secret


def data_directory(tmp_path, name="data", **values):
    """A new data directory whose service port is not the default one of an installation on this machine."""
    data = tmp_path / name
    data.mkdir(mode=0o700)
    (data / "config.json").write_text(json.dumps({"port": free_port(), **values}))
    os.chmod(data / "config.json", 0o600)
    return data


def connect(monkeypatch, url, secret, user="root"):
    monkeypatch.setenv("CAIRNMIND_ARCADE_CONNECT_URL", url)
    monkeypatch.setenv("CAIRNMIND_ARCADE_USER", user)
    monkeypatch.setenv("CAIRNMIND_ARCADE_PASSWORD", secret)


def cli(data, *arguments):
    return runner.invoke(app, ["--data-dir", str(data), *arguments])


def words(text: str) -> str:
    return " ".join(text.split())


def no_engine_files(data) -> bool:
    return not any((data / name).exists() for name in ("arcade", "runtime", "engine.lock"))


def forbid_local_engine(monkeypatch):
    """Fail the test when anything would install, probe or start an engine on this machine."""
    real = engine.subprocess.Popen

    def popen(command, *arguments, **options):
        if any("arcadedb" in str(part).lower() or str(part).endswith("java") for part in command):
            pytest.fail("an engine process was started")
        return real(command, *arguments, **options)

    monkeypatch.setattr(engine.subprocess, "Popen", popen)
    monkeypatch.setattr(engine, "probe_java", lambda java: pytest.fail("Java was run"))
    monkeypatch.setattr(engine, "resolve_java", lambda settings: pytest.fail("Java was looked for"))
    monkeypatch.setattr(engine.urllib.request, "urlopen", lambda *a, **k: pytest.fail("a download was started"))


def repository(tmp_path, files=3):
    root = tmp_path / "repo"
    root.mkdir()
    for index in range(files):
        (root / f"module_{index}.py").write_text(f"VALUE_{index} = {index}\n\n\ndef function_{index}():\n"
                                                 f"    return VALUE_{index}\n")
    return root


# --- The setting ---------------------------------------------------------------------------------------

def test_the_connect_address_is_the_one_used_and_the_stored_address_is_kept(tmp_path, test_engine):
    data = data_directory(tmp_path, arcade_url="http://127.0.0.1:2480", arcade_connect_url="http://127.0.0.1:2999")
    settings = Settings.load(data, create=True)
    assert settings.arcade_url == "http://127.0.0.1:2999" and connect_only(settings)
    saved = json.loads((data / "config.json").read_text())
    # Removing the setting brings the managed engine back at the address it had.
    assert saved["arcade_url"] == "http://127.0.0.1:2480" and saved["arcade_connect_url"] == "http://127.0.0.1:2999"
    # No engine is started here, so no password is generated for one.
    assert saved.get("arcade_password", "") == "" and saved["token"]
    with pytest.raises(ValueError, match="needs the password set where that server runs"):
        ArcadeClient(settings)


def test_the_variable_names_the_address_in_both_spellings_and_its_first_use_saves_it(tmp_path, test_engine,
                                                                                    monkeypatch):
    data = data_directory(tmp_path)
    monkeypatch.setenv("DEVBRAIN_ARCADE_CONNECT_URL", "http://127.0.0.1:2998")
    # Reading settings writes nothing; the first initialising use saves the address and nothing else.
    assert Settings.load(data).arcade_url == "http://127.0.0.1:2998"
    assert "arcade_connect_url" not in json.loads((data / "config.json").read_text())
    settings = Settings.load(data, create=True)
    assert settings.arcade_url == "http://127.0.0.1:2998" and mode(settings) == "connect"
    assert settings._origin.get("connect_saved") is True
    saved = json.loads((data / "config.json").read_text())
    assert saved["arcade_connect_url"] == "http://127.0.0.1:2998"
    assert saved.get("arcade_url", "http://127.0.0.1:2480").endswith(":2480") and not saved.get("arcade_password")
    monkeypatch.setenv("CAIRNMIND_ARCADE_CONNECT_URL", "http://127.0.0.1:2997")
    again = Settings.load(data, create=True)
    assert again.arcade_url == "http://127.0.0.1:2997"  # the current spelling comes first, for this process
    # config.json already names an address: another one in the variable does not replace it.
    assert not again._origin.get("connect_saved")
    assert json.loads((data / "config.json").read_text())["arcade_connect_url"] == "http://127.0.0.1:2998"


def test_a_command_started_without_the_variable_stays_connect_only_and_starts_no_engine(tmp_path, test_engine,
                                                                                      monkeypatch):
    """The installer ran with the variable; a later shell, or a terminal server started earlier, has none.

    With an engine installed in the data directory (an installation switched from managed), that command
    would otherwise start it and write to the old local store.
    """
    data = data_directory(tmp_path, arcade_url=f"http://127.0.0.1:{free_port()}")
    (data / "runtime" / f"arcadedb-{Settings(data_dir=data).arcade_version}" / "lib").mkdir(parents=True)
    url = f"http://127.0.0.1:{free_port()}"  # nothing listens there
    connect(monkeypatch, url, "Some-password-1234", user="cairnmind")
    forbid_local_engine(monkeypatch)
    first = cli(data, "engine", "status")
    assert first.exit_code == 0 and f"Saved arcade_connect_url = {url}" in words(first.output)
    assert "remove arcade_connect_url from that file" in words(first.output)
    for name in ("CONNECT_URL", "USER", "PASSWORD"):
        monkeypatch.delenv("CAIRNMIND_ARCADE_" + name)
    settings = Settings.load(data, create=True)
    assert mode(settings) == "connect" and settings.arcade_url == url
    for command in (["init"], ["engine", "start"], ["engine", "install"]):
        result = cli(data, *command)
        text = words(result.output) + str(result.exception)
        assert result.exit_code != 0 and "ArcadeDB is not installed" not in text, (command, text)
        assert "Saved arcade_connect_url" not in text  # said once, when it was saved
    # `init` names what this process lacks; nothing was generated or locked for an engine.
    refused = cli(data, "init")
    assert "needs the password set where that server runs" in str(refused.exception)
    assert not json.loads((data / "config.json").read_text()).get("arcade_password")
    assert not (data / "arcade").exists() and not (data / "engine.lock").exists()


def test_an_address_that_is_refused_is_not_saved(tmp_path, test_engine, monkeypatch):
    data = data_directory(tmp_path)
    monkeypatch.setenv("CAIRNMIND_ARCADE_CONNECT_URL", "https://cairnmind:Hunter2-secret@db.example.org:2490")
    assert not Settings.load(data, create=True)._origin.get("connect_saved")
    monkeypatch.setenv("CAIRNMIND_ARCADE_CONNECT_URL", "http://127.0.0.1:2999")
    monkeypatch.setenv("CAIRNMIND_ARCADE_URL", "http://127.0.0.1:2480")  # two addresses: refused, so not saved
    assert not Settings.load(data, create=True)._origin.get("connect_saved")
    assert "arcade_connect_url" not in (data / "config.json").read_text()
    assert "Hunter2-secret" not in (data / "config.json").read_text()


def test_a_password_written_into_the_address_is_never_shown_and_the_setting_is_named(tmp_path, test_engine,
                                                                                  monkeypatch):
    data = data_directory(tmp_path)
    connect(monkeypatch, "https://cairnmind:Hunter2-secret@db.example.org:2490?x=Query-secret#Fragment-secret",
            "Some-password-1234", user="cairnmind")
    forbid_local_engine(monkeypatch)
    shown = []
    # `engine status` is refused too (#242): a rule of #72 is broken, so the server was not asked.
    for command, code in ((["engine", "start"], 2), (["engine", "stop"], 2), (["engine", "install"], 2),
                          (["engine", "status"], 1), (["init"], 1)):
        result = cli(data, *command)
        assert result.exit_code == code, (command, result.output, result.exception)
        shown.append(result.output + str(result.exception))
    settings = Settings.load(data)
    from cairnmind.database_credentials import doctor_block
    from cairnmind.database_mode import address, missing_database, unavailable, version_problem
    block = doctor_block(settings)
    shown += [json.dumps(block), missing_database(settings), unavailable(settings),
              version_problem(settings, {"version": "1.0"})]
    assert address(settings) == "https://db.example.org:2490" == block["url"]
    assert "This installation only connects to ArcadeDB at https://db.example.org:2490 " in words(shown[0])
    for text in shown:
        assert not any(secret in text for secret in ("Hunter2", "Query-secret", "Fragment-secret")), text
    # The refusal names the setting the person wrote, and where the user and the password belong.
    assert "arcade_connect_url must not carry a user, a password" in block["error"]
    assert "arcade_user" in block["error"] and "arcade_url must not" not in block["error"]
    # An address without any of them is shown as it was written.
    monkeypatch.setenv("CAIRNMIND_ARCADE_CONNECT_URL", "https://db.example.org:2490")
    assert address(Settings.load(data)) == "https://db.example.org:2490"


def test_engine_status_with_only_the_address_is_refused_and_names_what_is_missing(tmp_path, test_engine, monkeypatch):
    """A run on 15 November (#242): with only the connect address set, `engine status` exited 0, reported a
    server that answers as not available and named only the password, while root and a derived database
    name were used in silence. The server is up here; nothing may be sent to it without a password."""
    port, _ = test_engine
    data = data_directory(tmp_path)
    monkeypatch.setenv("CAIRNMIND_ARCADE_CONNECT_URL", f"http://127.0.0.1:{port}")
    forbid_local_engine(monkeypatch)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request",
                        lambda self, request: pytest.fail(f"a request was sent: {request.url}"))
    shown = cli(data, "engine", "status")
    assert shown.exit_code == 1, shown.output
    status = json.loads(shown.output[shown.output.index("{"):])
    # Not asked is not "not available".
    assert status["available"] is None and "needs the password set where that server runs" in status["error"]
    settings = Settings.load(data)
    message = words(status["error"])
    assert "arcade_user (root, the default, would be used)" in message
    assert f"arcade_database (the name {settings.arcade_database}, derived from this data directory's path" in message
    # doctor says the same (second trial of #242): the server was not asked, so it is not reported as one that
    # does not answer, which an installer turns into "the database engine does not answer".
    from cairnmind.database_credentials import doctor_block
    block = doctor_block(settings)
    assert block["available"] is None and "needs the password set where that server runs" in block["error"]
    # The first command saved config.json with the default root in it (#242 review): later commands still
    # name the user, which was never set on purpose.
    assert json.loads((data / "config.json").read_text())["arcade_user"] == "root"
    later = words(json.loads(cli(data, "engine", "status").output)["error"])
    assert "arcade_user (root, the default, would be used)" in later and "arcade_database (the name" in later
    # With the user and the database set, only the password is named.
    monkeypatch.setenv("CAIRNMIND_ARCADE_USER", "cairnmind")
    monkeypatch.setenv("CAIRNMIND_ARCADE_DATABASE", "cairnmind")
    named = words(json.loads(cli(data, "engine", "status").output)["error"])
    assert "needs the password" in named and "arcade_user" not in named and "arcade_database" not in named


def test_another_host_with_only_the_address_names_root_the_password_and_the_database(tmp_path, test_engine,
                                                                                     monkeypatch):
    """On a host other than this machine the root refusal came alone (#242 review): it named neither the
    missing password nor the derived database name. A user set in config.json is not called the default."""
    from cairnmind.database_credentials import problem
    data = data_directory(tmp_path)
    monkeypatch.setenv("CAIRNMIND_ARCADE_CONNECT_URL", "https://db.example.org:2490")
    settings = Settings.load(data)
    said = words(problem(settings))
    assert "ArcadeDB user root is refused for db.example.org" in said
    assert "ArcadeDB on db.example.org needs a password" in said
    assert f"arcade_database (the name {settings.arcade_database}, derived" in said
    assert "arcade_user (root" not in said  # the root refusal already says to set it
    config = json.loads((data / "config.json").read_text())
    (data / "config.json").write_text(json.dumps({**config, "arcade_user": "cmuser"}))
    named = words(problem(Settings.load(data)))
    assert "needs a password" in named and "arcade_user" not in named and "arcade_database (the name" in named


def test_two_different_addresses_are_refused_and_both_settings_are_named(tmp_path, test_engine, monkeypatch):
    data = data_directory(tmp_path)
    connect(monkeypatch, "http://127.0.0.1:2999", "Some-password-1234")
    monkeypatch.setenv("CAIRNMIND_ARCADE_URL", "http://127.0.0.1:2480")
    with pytest.raises(ValueError, match="CAIRNMIND_ARCADE_URL and arcade_connect_url .* different database addresses"):
        ArcadeClient(Settings.load(data))
    monkeypatch.setenv("CAIRNMIND_ARCADE_URL", "http://127.0.0.1:2999/")  # the same address twice is no conflict
    assert ArcadeClient(Settings.load(data)).settings.arcade_url == "http://127.0.0.1:2999"


@pytest.mark.parametrize("values, expected", [
    ({"arcade_url": "http://127.0.0.1:2480"}, "managed"),
    ({"arcade_url": "http://localhost:2480"}, "managed"),
    ({"arcade_url": "https://127.0.0.1:2490"}, "connect"),
    ({"arcade_url": "https://db.example.org:2490"}, "connect"),
    ({"arcade_url": "http://127.0.0.1:2480", "arcade_connect_url": "http://127.0.0.1:2480"}, "connect"),
])
def test_which_installations_only_connect(tmp_path, values, expected):
    assert mode(Settings(data_dir=tmp_path, **values)) == expected


# --- Engine commands -----------------------------------------------------------------------------------

def test_engine_commands_say_plainly_that_nothing_is_managed_here(tmp_path, test_engine, monkeypatch):
    data = data_directory(tmp_path)
    url = f"http://127.0.0.1:{free_port()}"  # nothing listens there
    connect(monkeypatch, url, "Some-password-1234")
    forbid_local_engine(monkeypatch)
    for command, verb in ((["engine", "install"], "install"), (["engine", "start"], "start"),
                          (["engine", "stop"], "stop")):
        result = cli(data, *command)
        assert result.exit_code == 2, (command, result.output, result.exception)
        text = words(result.output)
        assert f"This installation only connects to ArcadeDB at {url}" in text, text
        assert f"it does not {verb} an engine" in text and "arcade_connect_url names that address" in text
        assert "Traceback" not in text
    # With the knowledge service running, which is the usual state, `engine stop` says the same: it does
    # not ask to stop the service for an engine that does not exist.
    from cairnmind import cli as cli_module
    monkeypatch.setattr(cli_module, "service_alive", lambda settings: True)
    stopped = cli(data, "engine", "stop")
    assert stopped.exit_code == 2 and "it does not stop an engine" in words(stopped.output), stopped.output
    assert "Run cairnmind stop first" not in stopped.output and "Usage" not in stopped.output
    assert no_engine_files(data)
    # An engine this data directory started before the switch is still guarded by the service check.
    (data / "arcade").mkdir()
    (data / "arcade" / "process.json").write_text(json.dumps({"pid": os.getpid()}))
    guarded = cli(data, "engine", "stop")
    assert guarded.exit_code == 2 and "Run cairnmind stop first" in words(guarded.output)


def test_a_server_that_does_not_answer_is_reported_and_no_engine_is_started(tmp_path, test_engine, monkeypatch):
    """With an engine installed in the data directory and Java present, a managed installation would start it."""
    data = data_directory(tmp_path)
    (data / "runtime" / f"arcadedb-{Settings(data_dir=data).arcade_version}" / "lib").mkdir(parents=True)
    url = f"http://127.0.0.1:{free_port()}"
    connect(monkeypatch, url, "Some-password-1234")
    forbid_local_engine(monkeypatch)
    with pytest.raises(ArcadeError) as refused:
        Store(data)
    message = str(refused.value)
    assert f"ArcadeDB at {url} is unavailable" in message and "starts no engine of its own" in message
    assert "engine start" not in message
    # A command that only reads says the same, and not "start it with cairnmind engine start".
    with pytest.raises(StoreNotReady) as not_ready:
        Store.open_readonly(Settings.load(data))
    assert "starts no engine of its own" in str(not_ready.value) and "engine start" not in str(not_ready.value)
    status = engine.engine_status(Settings.load(data))
    assert status["mode"] == "connect" and status["available"] is False and "java" not in status
    assert "installed" not in status and "unavailable" in status["error"]
    assert not (data / "arcade").exists() and not (data / "engine.lock").exists()


def test_an_engine_started_before_the_switch_is_named_and_can_still_be_stopped(tmp_path, test_engine, monkeypatch):
    data = data_directory(tmp_path)
    connect(monkeypatch, f"http://127.0.0.1:{free_port()}", "Some-password-1234")
    settings = Settings.load(data, create=True)
    monkeypatch.setattr(engine, "managed_java_executable", lambda settings: "/usr/bin/java")
    assert "`cairnmind engine stop` stops it" in engine.engine_status(settings)["local_engine"]
    # With a process record of this data directory, stop goes on to its own checks instead of refusing.
    (data / "arcade").mkdir()
    (data / "arcade" / "process.json").write_text(json.dumps({"pid": os.getpid()}))
    with pytest.raises(ArcadeError, match="no longer belongs to this environment"):
        engine.stop_engine(settings)


# --- The server's version ---------------------------------------------------------------------------

def answering(monkeypatch, version, seen):
    def handle(self, request):
        seen.append((request.method, request.url.path))
        if request.url.path == "/api/v1/server":
            return httpx.Response(200, json={"version": version, "serverName": "elsewhere"})
        return httpx.Response(500, json={"error": "the test server answers nothing else"})
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)


def test_an_unsupported_server_version_is_reported_and_refused_with_what_is_supported(tmp_path, test_engine,
                                                                                    monkeypatch):
    data = data_directory(tmp_path)
    connect(monkeypatch, "http://127.0.0.1:2999", "Some-password-1234")
    seen = []
    answering(monkeypatch, "25.12.1", seen)
    settings = Settings.load(data, create=True)
    pinned = settings.arcade_version

    status = engine.engine_status(settings)
    assert status["available"] and status["version"] == "25.12.1" and status["version_matches"] is False
    assert status["supported_versions"] == [pinned]
    assert "runs version 25.12.1" in status["problem"] and f"Supported: {pinned}" in status["problem"]
    shown = cli(data, "engine", "status")
    assert shown.exit_code == 1 and json.loads(shown.output)["problem"] == status["problem"]

    from cairnmind.database_credentials import doctor_block
    block = doctor_block(settings)
    assert block["mode"] == "connect" and block["version"] == "25.12.1" and block["version_matches"] is False
    assert f"Supported: {pinned}" in block["error"] and "database_available" not in block

    # Every way of opening the store stops at the version, before anything is read or written.
    seen.clear()
    with pytest.raises(ArcadeError, match=f"does not support. Supported: {pinned}"):
        Store(data)
    with pytest.raises(ArcadeError, match="does not support"):
        Store.open_readonly(settings)
    refused = cli(data, "init")
    assert refused.exit_code != 0 and "Supported" in str(refused.exception)
    # The one command that signs in as root asks for the version first too: it creates no database and
    # no user on a server this release does not support.
    admin = tmp_path / "admin-password"
    admin.write_text("Root-password-1234\n")
    os.chmod(admin, 0o600)
    monkeypatch.setenv("CAIRNMIND_ARCADE_USER", "cairnmind")
    made = cli(data, "db", "create-user", "--admin-password-file", str(admin))
    assert made.exit_code != 0 and f"Supported: {pinned}" in str(made.exception), (made.output, made.exception)
    assert "The user cairnmind was not created" in str(made.exception)
    assert "Root-password-1234" not in made.output + str(made.exception)
    assert set(seen) == {("GET", "/api/v1/server")}


def test_a_supported_server_version_is_reported_with_where_it_runs(tmp_path, test_engine, monkeypatch):
    data = data_directory(tmp_path)
    connect(monkeypatch, "http://127.0.0.1:2999", "Some-password-1234", user="cairnmind")
    settings = Settings.load(data, create=True)
    answering(monkeypatch, settings.arcade_version + " (build abc)", [])
    shown = cli(data, "engine", "status")
    assert shown.exit_code == 0, shown.output
    status = json.loads(shown.output)
    assert status["mode"] == "connect" and status["host"] == "127.0.0.1" and status["user"] == "cairnmind"
    assert status["version"] == settings.arcade_version and status["version_matches"] is True
    assert status["transport"].startswith("http (loopback") and "problem" not in status
    assert "Some-password-1234" not in shown.output


def test_a_refused_sign_in_names_the_step_that_creates_the_user(tmp_path, test_engine, monkeypatch):
    """The usual first run: the address is right, and the database and its user do not exist yet."""
    data = data_directory(tmp_path)
    connect(monkeypatch, "http://127.0.0.1:2999", "Some-password-1234", user="cairnmind")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda self, request: httpx.Response(
        401, json={"error": "Authentication failed"}))
    status = engine.engine_status(Settings.load(data, create=True))
    assert status["available"] is False and "ArcadeDB HTTP 401: Authentication failed" in status["error"]
    assert "did not accept the user cairnmind" in status["error"] and "cairnmind db create-user" in status["error"]
    # ArcadeDB answers the same 401 for about 30 seconds after several failed sign-ins, the right password
    # included (measured on 26.11.1: accepted again after 31 s); the message names that cause too (#242).
    assert "about 30 seconds after several failed sign-ins" in words(status["error"])
    refused = cli(data, "init")
    assert refused.exit_code != 0 and "cairnmind db create-user" in str(refused.exception)
    assert "Some-password-1234" not in status["error"] + str(refused.exception)


def test_the_installer_asks_for_java_and_installs_an_engine_only_when_it_will_run_one(tmp_path):
    script = (ROOT / "scripts" / "install-wsl.sh").read_text()
    start = script.index("# Connect-only: the database runs elsewhere")
    check = script[start:script.index("# tmux 3.4+ is needed only by the terminal workspace", start)]
    empty = tmp_path / "no-tools"
    empty.mkdir()

    def java_check(**variables):
        return subprocess.run([shutil.which("bash"), "-c", "set -euo pipefail\n" + check + "\necho passed"],
                              env={"PATH": str(empty), **variables}, capture_output=True, text=True, timeout=30)

    without = java_check()
    assert without.returncode == 1 and "Java not found" in without.stderr
    for name in ("CAIRNMIND_ARCADE_CONNECT_URL", "DEVBRAIN_ARCADE_CONNECT_URL"):
        skipped = java_check(**{name: "https://db.example.org:2490"})
        assert skipped.returncode == 0 and skipped.stdout.strip() == "passed", skipped.stderr
    # The engine is installed only in the branch without a connect address; doctor runs in both.
    branch = script.index('if [[ -n "$connect_url" ]]; then')
    otherwise = script.index("\nelse\n", branch)
    assert otherwise < script.index('"$cairnmind_bin" engine install') < script.index('\n"$cairnmind_bin" doctor')
    assert "engine install" not in script[branch:otherwise]
    subprocess.run(["bash", "-n", str(ROOT / "scripts" / "install-wsl.sh")], check=True)


def test_evaluations_that_create_databases_are_refused_in_one_sentence(tmp_path, test_engine, monkeypatch):
    data = data_directory(tmp_path)
    connect(monkeypatch, "http://127.0.0.1:2999", "Some-password-1234")
    seen = []
    answering(monkeypatch, "26.11.1", seen)
    for command in (["eval", "run"], ["eval", "memory"], ["eval", "repository", str(tmp_path)]):
        result = cli(data, *command)
        assert result.exit_code == 2, (command, result.output, result.exception)
        assert "creates and drops temporary databases" in words(result.output)
    assert not seen


def test_doctor_does_not_look_for_java_on_an_installation_that_only_connects(tmp_path, test_engine, monkeypatch):
    from cairnmind import integrations
    data = data_directory(tmp_path)
    connect(monkeypatch, "http://127.0.0.1:2999", "Some-password-1234")
    settings = Settings.load(data, create=True)
    forbid_local_engine(monkeypatch)
    monkeypatch.setattr(integrations, "_probe_cli", lambda *a: {"installed": False})
    real_client = httpx.Client

    def backend(request):
        if request.url.path == "/api/v1/server":
            return httpx.Response(200, json={"version": settings.arcade_version})
        if request.url.path.startswith("/api/v1/query/"):
            return httpx.Response(200, json={"result": [{"ok": 1}]})
        return httpx.Response(503)

    monkeypatch.setattr(integrations.httpx, "Client", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(backend)))
    result = integrations.doctor(settings)
    assert result["java"]["needed"] is False
    assert result["arcadedb"]["mode"] == "connect" and result["arcadedb"]["host"] == "127.0.0.1"
    assert result["arcadedb"]["url"] == "http://127.0.0.1:2999" and result["arcadedb"]["database_available"]
    assert any("derived from this data directory" in note for note in result["arcadedb"]["notes"])


# --- A real server behind a relay ---------------------------------------------------------------------

@pytest.mark.no_schema_template
def test_a_database_that_does_not_exist_is_never_created_and_the_message_names_the_step(tmp_path, test_engine,
                                                                                       monkeypatch):
    port, secret = test_engine
    data = data_directory(tmp_path)
    with Relay(port) as relay:
        connect(monkeypatch, relay.url, secret)
        settings = Settings.load(data, create=True)
        result = cli(data, "init")
        assert result.exit_code != 0
        message = str(result.exception)
        assert f"The database {settings.arcade_database} does not exist" in message
        assert "a connect-only installation creates none" in message and "cairnmind db create-user" in message
        assert "derived from this data directory's path" in message
        with closing(ArcadeClient(settings)) as client:
            assert client.database_exists() is False
        assert not any(b"POST /api/v1/server" in line for line in relay.seen)
        # The one step that creates it signs in as root once and makes the user of this installation.
        name, password = "cm_" + uuid.uuid4().hex[:10], secrets.token_urlsafe(24)
        admin = tmp_path / "admin-password"
        admin.write_text(secret + "\n")
        os.chmod(admin, 0o600)
        connect(monkeypatch, relay.url, password, user=name)
        try:
            made = cli(data, "db", "create-user", "--admin-password-file", str(admin))
            assert made.exit_code == 0, (made.output, made.exception)
            assert cli(data, "init").exit_code == 0
        finally:
            with closing(ArcadeClient(replace(settings, arcade_user="root", arcade_password=secret,
                                              arcade_connect_url="", arcade_url=f"http://127.0.0.1:{port}"))) as root:
                try:
                    root.server_command(f"drop user {name}")
                except ArcadeError:
                    pass
    assert no_engine_files(data)


def test_an_installation_that_only_connects_indexes_backs_up_and_restores(tmp_path, test_engine, monkeypatch):
    port, secret = test_engine
    data = data_directory(tmp_path)
    root = repository(tmp_path)
    forbid_local_engine(monkeypatch)
    with Relay(port) as relay:
        connect(monkeypatch, relay.url, secret)
        assert cli(data, "init").exit_code == 0
        added = cli(data, "project", "add", "demo", str(root))
        assert added.exit_code == 0, (added.output, added.exception)
        assert json.loads(added.output)["indexed"]["changed"] == 3
        store = Store(data)
        assert store.status()["sources"] >= 3 and store.writer_state()["role"] == "writer"

        # The logical backup is read through the client and written where the command says, on this machine.
        made = cli(data, "backup", str(tmp_path))
        assert made.exit_code == 0, (made.output, made.exception)
        backup = json.loads(made.output)["backup"]
        assert os.path.dirname(backup) == str(tmp_path) and os.path.getsize(backup) > 0

        (root / "module_0.py").write_text("VALUE_0 = 'changed after the backup'\n")
        index_project(store, store.resolve_project("demo")["id"])
        changed = store.query("SELECT count(*) AS n FROM sources")[0]["n"]
        restored = cli(data, "restore", backup)
        assert restored.exit_code == 0, (restored.output, restored.exception)
        # No workspace of this installation is open: the real proof finds none and the restore says so (#214).
        assert json.loads(restored.stdout)["person_confirmations"] == "claims_of_the_backup"
        assert Store(data).query("SELECT count(*) AS n FROM sources")[0]["n"] == changed - 1
        # The safety copy made before the restore is a file of this installation too.
        assert len(list((data / "backups").glob("before-restore-*"))) == 1
        assert relay.requests > 50
    assert no_engine_files(data)


def test_the_size_report_says_that_the_database_files_are_on_the_server(tmp_path, test_engine, monkeypatch):
    port, secret = test_engine
    data = data_directory(tmp_path)
    with Relay(port) as relay:
        connect(monkeypatch, relay.url, secret)
        assert cli(data, "init").exit_code == 0
        shown = cli(data, "store", "size")
        assert shown.exit_code == 0, (shown.output, shown.exception)
        assert f"The database files and the server log are on the server at {relay.url}" in words(shown.output)
        report = json.loads(cli(data, "store", "size", "--json").output)
        assert report["disk"]["storage"]["mode"] == "connect" and not report["disk"]["database"]["exists"]


# --- One writer, across hosts ---------------------------------------------------------------------------

def test_two_installations_on_two_hosts_one_writes_the_other_reads_and_can_take_over(tmp_path, test_engine,
                                                                                    monkeypatch):
    """Each installation has its own data directory and so its own lock files: nothing local is shared, as
    between two machines. Only the writer row in the database decides. Reached as a host that is not this
    machine, over TLS, with a dedicated user and its password in a file."""
    address = this_machine_address()
    if address is None:
        pytest.skip("this machine has no address other than loopback")
    port, secret = test_engine
    folder = certificate_authority(tmp_path / "tls", address)
    name, password = "cm_" + uuid.uuid4().hex[:10], secrets.token_urlsafe(24)
    database = os.environ["CAIRNMIND_ARCADE_DB_PREFIX"] + "_shared_" + uuid.uuid4().hex[:8]
    secret_file = tmp_path / "db-password"
    secret_file.write_text(password + "\n")
    os.chmod(secret_file, 0o600)
    admin = tmp_path / "admin-password"
    admin.write_text(secret + "\n")
    os.chmod(admin, 0o600)
    root_client = ArcadeClient(Settings(data_dir=tmp_path, arcade_url=f"http://127.0.0.1:{port}",
                                        arcade_password=secret, arcade_database=database))
    with Relay(port, address=address, tls=folder) as relay:
        values = {"arcade_connect_url": relay.url, "arcade_user": name, "arcade_database": database,
                  "arcade_password_file": str(secret_file), "arcade_ca_file": str(folder / "ca.pem")}
        laptop, desktop = data_directory(tmp_path, "laptop", **values), data_directory(tmp_path, "desktop", **values)
        try:
            made = cli(laptop, "db", "create-user", "--admin-password-file", str(admin))
            assert made.exit_code == 0, (made.output, made.exception)
            machines = {str(laptop): "machine-id:laptop", str(desktop): "machine-id:desktop"}
            current = {"machine": None}
            monkeypatch.setattr(store_writer, "machine_identity", lambda: current["machine"])

            def opened(data):
                current["machine"] = machines[str(data)]
                return Store(data)

            first = opened(laptop)
            pid = first.add_project("demo", repository(tmp_path))["id"]
            index_project(first, pid)
            second = opened(desktop)
            assert second.installation.id != first.installation.id
            assert second.read_only and "only reads this store" in second.not_writer
            assert [row["name"] for row in second.list_projects()] == ["demo"]
            assert second.query("SELECT count(*) AS n FROM sources WHERE is_current=true")[0]["n"] == 3
            (tmp_path / "repo" / "module_1.py").write_text("VALUE_1 = 'edited on the second host'\n")
            # The folder is the laptop's (#371): on the desktop it is named as elsewhere and nothing is written, so
            # nothing is refused and nothing of the edited file is indexed.
            elsewhere = index_project(second, pid)
            assert elsewhere["unavailable"][0]["state"] == "elsewhere" and elsewhere["changed"] == 0
            # Where the folder is this machine's, a reader's indexing is refused at its first write.
            current["machine"] = machines[str(laptop)]
            with pytest.raises(StoreNotWriter, match="writes it"):
                index_project(second, pid)
            current["machine"] = machines[str(desktop)]
            with pytest.raises(StoreNotWriter):
                second.command("CREATE DOCUMENT TYPE vector_never IF NOT EXISTS")
            # A reader can take the logical backup: it only reads, and the file is written on its own machine.
            copy = second.backup(tmp_path / "reader-backup.cairnmind.json.gz")
            assert len(Store._validated_backup(copy)["sources"]) == 3
            doctor = store_writer.inspect(Settings.load(desktop))
            assert doctor["role"] == "reader" and doctor["next"] == "cairnmind store takeover --yes"

            # A person moves writing to the second host; the first one stops at its next write.
            current["machine"] = machines[str(desktop)]
            taken = cli(desktop, "store", "takeover", "--yes", "--note", "the laptop is away")
            assert taken.exit_code == 0, (taken.output, taken.exception)
            assert json.loads(taken.output)["taken_over"] is True
            current["machine"] = machines[str(laptop)]
            with pytest.raises(StoreWriterLost, match="took over writing"):
                first.add_project("late", tmp_path / "tls")
            (tmp_path / "repo" / "module_0.py").write_text("VALUE_0 = 'after the takeover'\n")
            index_project(opened(desktop), pid)
            assert opened(laptop).read_only
            # No lock file of one installation exists in the other's directory, and none on the server side.
            assert not (desktop / "arcade").exists() and not (laptop / "arcade").exists()
        finally:
            for command in (f"drop database {database}", f"drop user {name}"):
                try:
                    root_client.server_command(command)
                except ArcadeError:
                    pass
            root_client.close()


def test_two_installations_that_create_one_store_at_the_same_moment_end_as_a_writer_and_a_reader(
        tmp_path, test_engine, monkeypatch):
    """The one whose commit comes second opens the store as a reader, not with the engine's conflict message."""
    port, secret = test_engine
    connect(monkeypatch, f"http://127.0.0.1:{port}", secret)
    for round_number in range(3):
        database = os.environ["CAIRNMIND_ARCADE_DB_PREFIX"] + "_together_" + uuid.uuid4().hex[:8]
        with closing(ArcadeClient(Settings(data_dir=tmp_path, arcade_url=f"http://127.0.0.1:{port}",
                                           arcade_password=secret, arcade_database=database))) as root:
            root.ensure_database()
            try:
                folders = [data_directory(tmp_path, f"{name}{round_number}", arcade_database=database)
                           for name in ("laptop", "desktop")]
                barrier, opened = threading.Barrier(2), {}

                def open_store(data):
                    barrier.wait()
                    try:
                        opened[data.name] = "reader" if Store(data).read_only else "writer"
                    except Exception as exc:  # noqa: BLE001 - the assertion below shows what it was
                        opened[data.name] = f"{type(exc).__name__}: {exc}"

                threads = [threading.Thread(target=open_store, args=(data,)) for data in folders]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(120)
                assert sorted(opened.values()) == ["reader", "writer"], opened
                counts = {row["name"]: row["n"] for row in root.query(
                    "SELECT name, count(*) AS n FROM devbrain_meta GROUP BY name")}
                assert counts["writer"] == 1 and counts["schema_version"] == 1
                assert root.query("SELECT count(*) AS n FROM journal_state")[0]["n"] == 1
                # Each keeps its role when opened again.
                again = {data.name: "reader" if Store(data).read_only else "writer" for data in folders}
                assert again == opened
            finally:
                root.server_command(f"drop database {database}")
