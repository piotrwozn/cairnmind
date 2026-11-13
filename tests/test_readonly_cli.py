"""Commands that only read open the store for reading: with the engine down they say so and start nothing.

Run the way a user runs them, through the entrypoint, which prints the store's error as one line.
No engine is needed: the configured engine address is a closed port.
"""
from __future__ import annotations

import re
import socket

import pytest

from cairnmind import cli
from cairnmind.arcade import ArcadeError
from cairnmind.config import Settings
from cairnmind.store import Store

READ_COMMANDS = [
    ["project", "list"],
    ["records", "demo"],
    ["records", "demo", "--history"],
    ["source", "source-one"],
    ["task", "list", "demo"],
    ["plan", "list", "demo"],
    ["plan", "show", "demo", "plan-one"],
    ["plan", "next", "demo"],
    ["plan", "contract", "demo"],
    ["check", "profiles", "demo"],
    ["memory", "orphans", "demo"],
    ["impact", "demo", "--since", "HEAD"],
    ["usage", "demo"],
]

# Commands that read but may write, or whose behaviour the owner kept: they prepare the store as before.
PREPARING_COMMANDS = [
    ["brief", "demo"],
    ["check", "list", "demo"],
    ["plan", "contract", "demo", "--write", "0123456789ab"],
    ["export", "export.json"],
    ["backup", "backups"],
]

ENGINE_DOWN = ("The database engine is not running; start it with `cairnmind engine start` "
               "(this command does not start it).")


def words(text):
    """The words of an error, without colour codes, box characters or line wrapping."""
    return " ".join(re.sub(r"[^\w\s`.;()-]", " ", re.sub(r"\x1b\[[0-9;]*m", "", text)).split())


def files(root):
    return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in root.rglob("*") if path.is_file()}


def engine_down(tmp_path, monkeypatch):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    for name in ("CAIRNMIND_ARCADE_URL", "DEVBRAIN_ARCADE_URL"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{port}")
    return Settings.load(tmp_path / "memory", create=True)


def forbid_preparing(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("A command that only reads started something or prepared the store")
    monkeypatch.setattr("cairnmind.engine.start_engine", forbidden)
    monkeypatch.setattr("cairnmind.engine.ensure_engine", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr(cli, "ensure_service", forbidden)
    monkeypatch.setattr(Store, "__init__", forbidden)
    original = Settings.load

    def load(cls, data_dir=None, *, create=False):
        assert create is False, "A command that only reads created configuration"
        return original(data_dir, create=False)

    monkeypatch.setattr(Settings, "load", classmethod(load))


def run(monkeypatch, capsys, data_dir, arguments):
    monkeypatch.setattr("sys.argv", ["cairnmind", "--data-dir", str(data_dir), *arguments])
    with pytest.raises(SystemExit) as stopped:
        cli.entrypoint()
    return stopped.value.code, capsys.readouterr()


@pytest.mark.parametrize("arguments", READ_COMMANDS, ids=" ".join)
def test_a_read_command_with_the_engine_down_says_so_and_starts_nothing(tmp_path, monkeypatch, capsys, arguments):
    settings = engine_down(tmp_path, monkeypatch)
    before = files(settings.data_dir)
    forbid_preparing(monkeypatch)
    code, shown = run(monkeypatch, capsys, settings.data_dir, arguments)
    assert code == 1
    assert words(shown.err) == words("Cairnmind: " + ENGINE_DOWN)
    assert shown.out == ""
    assert files(settings.data_dir) == before


@pytest.mark.parametrize("arguments", READ_COMMANDS, ids=" ".join)
def test_a_read_command_without_configuration_says_to_run_init_and_creates_nothing(
        tmp_path, monkeypatch, capsys, arguments):
    data = tmp_path / "missing" / "memory"
    forbid_preparing(monkeypatch)
    code, shown = run(monkeypatch, capsys, data, arguments)
    assert code == 1
    assert words(shown.err) == words(f"Cairnmind: There is no Cairnmind configuration in {data}; "
                                     "run `cairnmind init` first.")
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("arguments", PREPARING_COMMANDS, ids=" ".join)
def test_the_commands_left_on_the_full_store_still_start_the_engine(tmp_path, monkeypatch, capsys, arguments):
    settings = engine_down(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    started = []

    def start(selected):
        started.append(selected.data_dir)
        raise ArcadeError("the engine start was reached")

    monkeypatch.setattr("cairnmind.engine.start_engine", start)
    code, shown = run(monkeypatch, capsys, settings.data_dir, arguments)
    assert code == 1
    assert started == [settings.data_dir]
    assert words(shown.err) == words("Cairnmind: the engine start was reached")
