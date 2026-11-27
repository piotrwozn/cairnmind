"""Cross-platform private file, DACL/mode, symlink guard, atomic replace, and pread tests."""
from __future__ import annotations

import os
import sys

import pytest

from conftest import make_symlink_or_skip
from cairnmind.pal import private_files


def test_make_private_and_is_private_stat_for_file_and_directory(tmp_path):
    folder = private_files.ensure_private_dir(tmp_path / "secret-dir")
    assert private_files.is_private_stat(folder.lstat(), folder, directory=True)
    assert not private_files.is_private_stat(folder.lstat(), folder, directory=False)

    file_path = folder / "secret.txt"
    fd = private_files.open_no_follow(file_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("top-secret\n")
    private_files.make_private(file_path)
    assert private_files.is_private_stat(file_path.lstat(), file_path, directory=False)
    assert not private_files.is_private_stat(file_path.lstat(), file_path, directory=True)


def test_ensure_private_dir_rejects_file_and_link(tmp_path):
    plain = tmp_path / "plain"
    plain.write_text("not a dir", encoding="utf-8")
    with pytest.raises(ValueError):
        private_files.ensure_private_dir(plain)

    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "linked-dir"
    make_symlink_or_skip(link, outside, target_is_directory=True)
    assert private_files.is_reparse_point(link)
    with pytest.raises(ValueError):
        private_files.ensure_private_dir(link)


def test_open_no_follow_rejects_symlink(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    link = tmp_path / "link.txt"
    make_symlink_or_skip(link, outside)
    with pytest.raises(OSError):
        private_files.open_no_follow(link, os.O_RDONLY, private_on_create=False)


def test_atomic_replace_replaces_existing_content(tmp_path):
    target = tmp_path / "state.json"
    target.write_text("old", encoding="utf-8")
    staging = tmp_path / "state.json.tmp"
    staging.write_text("new", encoding="utf-8")
    private_files.atomic_replace(staging, target)
    assert target.read_text(encoding="utf-8") == "new"
    assert not staging.exists()


def test_pread_at_reads_at_offset_without_moving_cursor(tmp_path):
    path = tmp_path / "data.bin"
    path.write_bytes(b"0123456789abcdef")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        assert os.read(fd, 4) == b"0123"
        assert private_files.pread_at(fd, 4, 10) == b"abcd"
        assert os.read(fd, 4) == b"4567"
    finally:
        os.close(fd)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows NTFS DACL verification test")
def test_windows_dacl_checker_rejects_non_owner_sids(tmp_path, monkeypatch):
    """Verify _check_private_dacl fails closed on Everyone/Users/Authenticated Users even in sandboxes."""
    import ctypes
    from ctypes import wintypes

    target = tmp_path / "sample.txt"
    target.write_text("x", encoding="utf-8")
    me = private_files.current_user_sid()

    def build_sd(sddl: str):
        sd = ctypes.c_void_p()
        assert private_files._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(sd), None
        )
        return sd

    def check_sddl(sddl: str) -> bool:
        sd = build_sd(sddl)
        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        defaulted = wintypes.BOOL()
        present = wintypes.BOOL()
        assert private_files._advapi32.GetSecurityDescriptorOwner(sd, ctypes.byref(owner), ctypes.byref(defaulted))
        assert private_files._advapi32.GetSecurityDescriptorDacl(
            sd, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
        )

        def fake_get_named(name, obj_type, sec_info, out_owner, out_group, out_dacl, out_sacl, out_sd):
            out_owner._obj.value = owner.value
            out_dacl._obj.value = dacl.value
            out_sd._obj.value = sd.value
            return 0

        monkeypatch.setattr(private_files._advapi32, "GetNamedSecurityInfoW", fake_get_named)
        # _check_private_dacl calls LocalFree(sd) in finally
        return private_files._check_private_dacl(target)

    # Owner + SYSTEM only: accepted
    assert check_sddl(f"O:{me}D:P(A;;FA;;;{me})(A;;FA;;;SY)") is True
    # Everyone (WD / S-1-1-0) granted read: rejected
    assert check_sddl(f"O:{me}D:P(A;;FA;;;{me})(A;;GR;;;WD)") is False
    # BUILTIN\Users (BU / S-1-5-32-545) granted read: rejected
    assert check_sddl(f"O:{me}D:P(A;;FA;;;{me})(A;;GR;;;BU)") is False
    # Authenticated Users (AU / S-1-5-11) granted read: rejected
    assert check_sddl(f"O:{me}D:P(A;;FA;;;{me})(A;;GR;;;AU)") is False
    # Foreign owner: rejected
    assert check_sddl(f"O:S-1-5-32-545D:P(A;;FA;;;{me})") is False
    # BUILTIN\Administrators-only ACE: accepted only when current token is in Administrators
    monkeypatch.setattr(private_files, "_current_token_is_admin", lambda: True)
    assert check_sddl("O:BAD:P(A;;FA;;;BA)(A;;FA;;;SY)") is True
    monkeypatch.setattr(private_files, "_current_token_is_admin", lambda: False)
    assert check_sddl("O:BAD:P(A;;FA;;;BA)(A;;FA;;;SY)") is False


def test_current_user_sid_is_always_importable_and_typed():
    sid = private_files.current_user_sid()
    if sys.platform == "win32":
        assert isinstance(sid, str) and sid.startswith("S-1-")
    else:
        assert sid is None


def test_write_durable_private_creates_private_file_atomically(tmp_path):
    target = tmp_path / "secret.json"
    private_files.write_durable_private(target, '{"secret": true}\n')
    assert target.read_text(encoding="utf-8") == '{"secret": true}\n'
    assert private_files.is_private_stat(target.lstat(), target, directory=False)


def test_product_secret_files_have_real_private_permissions(tmp_path):
    """Verify real ACL/mode on config.json, engine root-password, installation.json, and API credentials (#436)."""
    from cairnmind.agents.api_credentials import CredentialStore
    from cairnmind.config import Settings
    from cairnmind.storage.engine import write_root_password
    from cairnmind.storage.store_writer import installation

    data_dir = tmp_path / "data"
    settings = Settings.load(data_dir, create=True)
    cfg = data_dir / "config.json"
    assert private_files.is_private_stat(data_dir.lstat(), data_dir, directory=True)
    assert private_files.is_private_stat(cfg.lstat(), cfg, directory=False)

    private_files.ensure_private_dir(data_dir / "arcade")
    pw = write_root_password(settings)
    assert private_files.is_private_stat(pw.lstat(), pw, directory=False)

    assert installation(data_dir).id
    inst = data_dir / "installation.json"
    assert private_files.is_private_stat(inst.lstat(), inst, directory=False)

    creds = CredentialStore(settings)
    creds.set("openai", "sk-test-1234567890", persist=True)
    assert private_files.is_private_stat(creds.path.lstat(), creds.path, directory=False)
