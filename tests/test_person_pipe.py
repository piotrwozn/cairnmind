"""Windows Named Pipe person_proof tests (#214): real Win32 pipe, single-instance lock, fail-closed checks."""
from __future__ import annotations

import sys

import pytest

from cairnmind.config import Settings
from cairnmind.pal import person_pipe
from cairnmind.terminal import person_proof

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Named Pipe tests run on native Windows")


def test_pipe_name_is_deterministic_and_scoped_to_data_dir(tmp_path):
    s1 = Settings.load(tmp_path / "one", create=True)
    s2 = Settings.load(tmp_path / "two", create=True)
    p1 = person_pipe.pipe_name_for(s1.data_dir)
    p2 = person_pipe.pipe_name_for(s2.data_dir)
    assert p1.startswith(person_pipe.PIPE_PREFIX)
    assert p1 == person_pipe.pipe_name_for(s1.data_dir)
    assert p1 != p2
    assert person_pipe.is_valid_pipe_name(s1, p1)
    assert not person_pipe.is_valid_pipe_name(s1, p2)
    assert not person_pipe.is_valid_pipe_name(s1, "")
    assert not person_pipe.is_valid_pipe_name(s1, None)
    assert not person_pipe.is_valid_pipe_name(s1, p1 + "\0extra")


def test_person_pipe_server_and_client_round_trip(tmp_path):
    settings = Settings.load(tmp_path / "data", create=True)
    key = person_proof.new_key()
    pipe_name = person_pipe.pipe_name_for(settings.data_dir)
    assert person_pipe.read_pipe_key(settings, pipe_name) == ("unavailable", None)
    with person_pipe.PersonPipeServer(settings.data_dir, key) as server:
        assert server.pipe_name == pipe_name
        assert person_pipe.read_pipe_key(settings, pipe_name) == ("key", key)
        # Multiple sequential reads work while the workspace stays open.
        assert person_pipe.read_pipe_key(settings, pipe_name) == ("key", key)
    assert person_pipe.read_pipe_key(settings, pipe_name) == ("unavailable", None)


def test_person_pipe_single_instance_blocks_squatting(tmp_path):
    settings = Settings.load(tmp_path / "data", create=True)
    k1 = person_proof.new_key()
    k2 = person_proof.new_key()
    with (
        person_pipe.PersonPipeServer(settings.data_dir, k1),
        pytest.raises(OSError),
        person_pipe.PersonPipeServer(settings.data_dir, k2),
    ):
        pass


def test_person_pipe_rejects_malformed_keys(tmp_path):
    settings = Settings.load(tmp_path / "data", create=True)
    for bad in ("", "abc", "AB" * 32, "a" * 63, "a" * 65):
        with pytest.raises(ValueError):
            person_pipe.PersonPipeServer(settings.data_dir, bad)


def test_person_proof_windows_end_to_end_check_restore_and_headers(tmp_path):
    settings = Settings.load(tmp_path / "data", create=True)
    other = Settings.load(tmp_path / "other", create=True)
    key = person_proof.new_key()
    wrong = person_proof.new_key()
    pipe_name = person_pipe.pipe_name_for(settings.data_dir)

    # Closed workspace fails closed everywhere.
    assert person_proof.restore_proof(settings) == "unavailable"
    assert person_proof.workspace_headers(settings) == {}
    with pytest.raises(person_proof.PersonProofRefused) as missing:
        person_proof.check(settings, {})
    assert missing.value.message == person_proof.PROOF_MISSING
    with pytest.raises(person_proof.PersonProofRefused) as closed:
        person_proof.check(settings, {person_proof.SOCKET_HEADER: pipe_name, person_proof.KEY_HEADER: key})
    assert closed.value.message == person_proof.PROOF_REFUSED

    with person_pipe.PersonPipeServer(settings.data_dir, key):
        assert person_proof.restore_proof(settings) == "key"
        assert person_proof.restore_proof(other) == "unavailable"
        # Outside the active standalone UI process registry, workspace_headers stays empty.
        assert person_proof.workspace_headers(settings) == {}

        person_proof.register_active_pipe(pipe_name)
        try:
            hdrs = person_proof.workspace_headers(settings)
            assert hdrs == {person_proof.KEY_HEADER: key, person_proof.SOCKET_HEADER: pipe_name}
            person_proof.check(settings, hdrs)
            # Forwardable keeps the Named Pipe header intact.
            assert person_proof.forwardable(hdrs) == hdrs
        finally:
            person_proof.unregister_active_pipe(pipe_name)

        # Wrong key, missing header, or other installation pipe all fail closed.
        for bad_hdrs in (
            {person_proof.SOCKET_HEADER: pipe_name, person_proof.KEY_HEADER: wrong},
            {person_proof.SOCKET_HEADER: pipe_name},
            {person_proof.KEY_HEADER: key},
            {person_proof.SOCKET_HEADER: person_pipe.pipe_name_for(other.data_dir), person_proof.KEY_HEADER: key},
        ):
            with pytest.raises(person_proof.PersonProofRefused) as refused:
                person_proof.check(settings, bad_hdrs)
            assert refused.value.message == person_proof.PROOF_REFUSED

    # Once closed, previously valid headers are refused again.
    assert person_proof.restore_proof(settings) == "unavailable"
    with pytest.raises(person_proof.PersonProofRefused) as after_close:
        person_proof.check(settings, {person_proof.SOCKET_HEADER: pipe_name, person_proof.KEY_HEADER: key})
    assert after_close.value.message == person_proof.PROOF_REFUSED
