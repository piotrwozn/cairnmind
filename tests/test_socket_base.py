"""Where the workspace and native-run tmux sockets live (#235): a fixed per-user base, short paths."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from cairnmind.agents.client_runtime import NativeRuntime
from cairnmind.config import Settings
from cairnmind.pal import runtime_sockets
from cairnmind.pal.runtime_sockets import (
    KINDS,
    MAX_SOCKET_PATH,
    SUN_PATH,
    prepare_socket_base,
    socket_base,
    socket_directory,
)
from cairnmind.terminal.workspace import WorkspaceManager

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Tests POSIX Unix domain socket directories")

# What a terminal app gets on macOS; ssh and launchd see another one, or none.
MAC_TMPDIR = "/var/folders/zz/zyxvpxvq6csfxvn_n0000000000000/T/"
# Bytes every socket path the product creates by default keeps free under the system's limit.
MARGIN = 32


def _sockets(tmp_path):
    settings = Settings.load(tmp_path / "data", create=True)
    return NativeRuntime(settings).socket, WorkspaceManager(settings).socket


@pytest.fixture
def mac_temp(monkeypatch):
    """A stand-in for macOS's /tmp: a link to private/tmp, sticky and writable by all, short like the real one."""
    root = Path(tempfile.mkdtemp(prefix="cm-", dir="/tmp"))
    (root / "private" / "tmp").mkdir(parents=True)
    (root / "private" / "tmp").chmod(0o1777)
    (root / "tmp").symlink_to("private/tmp")
    monkeypatch.setattr(runtime_sockets, "SYSTEM_TEMP", str(root / "tmp"))
    yield root / "private" / "tmp"
    shutil.rmtree(root, ignore_errors=True)


def test_on_macos_the_sockets_do_not_follow_tmpdir(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    seen = []
    for value in (MAC_TMPDIR, str(tmp_path)):
        monkeypatch.setenv("TMPDIR", value)
        monkeypatch.setattr(tempfile, "tempdir", None)
        seen.append(_sockets(tmp_path))
    assert seen[0] == seen[1]
    base = Path(os.path.realpath("/tmp")) / f"cairnmind-{os.getuid()}"
    assert all(socket.parent.parent == base for socket in seen[0])


def test_on_linux_the_sockets_stay_where_an_earlier_version_put_them(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.delenv("TMPDIR", raising=False)
    native, ui = _sockets(tmp_path)
    root, uid = Path(tempfile.gettempdir()), os.getuid()
    assert native.parent.parent == root and native.parent.name.startswith(f"cairnmind-native-{uid}-")
    assert ui.parent.parent == root and ui.parent.name.startswith(f"cairnmind-ui-{uid}-")
    assert native.name == ui.name == "tmux.sock"


@pytest.mark.parametrize(("platform", "system_temp"), [
    ("darwin", "/private/tmp"),  # what /tmp resolves to on macOS
    # Linux without TMPDIR: the longest place tempfile.gettempdir() falls back to.
    *(("linux", candidate) for candidate in ("/tmp", "/var/tmp", "/usr/tmp"))])
def test_the_longest_socket_path_keeps_a_margin_under_the_limit(monkeypatch, platform, system_temp):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: system_temp)
    namespace = "f" * 16  # the namespaces are 16 hexadecimal digits of a SHA-256
    longest = max(len(os.fsencode(socket_directory(kind, namespace, platform=platform, uid=2**32 - 1,
                                                   system_temp=system_temp) / "tmux.sock")) for kind in KINDS)
    assert longest + MARGIN <= SUN_PATH[platform]
    assert longest <= MAX_SOCKET_PATH < SUN_PATH[platform]  # the product's own check never refuses the default place


def test_the_macos_base_resolves_the_link_of_tmp_and_is_made_private(mac_temp):
    base = socket_base(platform="darwin")
    assert base == mac_temp.resolve() / f"cairnmind-{os.getuid()}"
    prepare_socket_base(platform="darwin")
    info = base.lstat()
    assert base.is_dir() and not base.is_symlink() and info.st_uid == os.getuid() and info.st_mode & 0o777 == 0o700
    prepare_socket_base(platform="darwin")  # found again, unchanged
    assert socket_directory("native", "0" * 16, platform="darwin").parent == base


def test_a_base_made_by_someone_else_is_refused(mac_temp):
    base = mac_temp.resolve() / f"cairnmind-{os.getuid()}"
    elsewhere = mac_temp.parent / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    base.symlink_to(elsewhere)
    with pytest.raises(ValueError, match="not be a link"):
        prepare_socket_base(platform="darwin")
    base.unlink()
    base.mkdir()
    base.chmod(0o755)
    with pytest.raises(ValueError, match="permissions 0700"):
        prepare_socket_base(platform="darwin")
    base.rmdir()
    base.write_text("")
    with pytest.raises(ValueError):
        prepare_socket_base(platform="darwin")


def test_a_system_temporary_folder_others_could_rewrite_is_refused(mac_temp):
    mac_temp.chmod(0o777)  # writable by all, not sticky: anyone could replace the user's folder
    with pytest.raises(ValueError, match="not safe"):
        prepare_socket_base(platform="darwin")
    assert not (mac_temp / f"cairnmind-{os.getuid()}").exists()


def test_on_linux_nothing_is_made_beside_the_socket_folders(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_sockets, "SYSTEM_TEMP", str(tmp_path))
    prepare_socket_base(platform="linux")
    assert list(tmp_path.iterdir()) == []


def test_an_unknown_kind_of_socket_is_refused():
    with pytest.raises(ValueError):
        socket_directory("../ui", "0" * 16)


def test_a_native_runtime_on_macos_makes_its_socket_folder_in_the_base(tmp_path, monkeypatch, mac_temp):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", None)
    runtime = NativeRuntime(Settings.load(tmp_path / "data", create=True))
    runtime._initialize()
    base = mac_temp.resolve() / f"cairnmind-{os.getuid()}"
    assert runtime.socket_directory.parent == base and runtime.socket_directory.name.startswith("native-")
    assert base.stat().st_mode & 0o777 == runtime.socket_directory.stat().st_mode & 0o777 == 0o700


def test_the_refusals_read_in_polish_too():
    from cairnmind.terminal.terminal_i18n import tr
    for text in ("The system temporary folder is not safe for private sockets.",
                 "The socket folder must belong to the user, have permissions 0700 and not be a link."):
        assert tr(text, "en") == text and tr(text, "pl") != text
