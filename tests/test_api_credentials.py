from __future__ import annotations

import json

import pytest

from cairnmind.api_credentials import CredentialStore, credential_environment_names
from cairnmind.config import Settings


def profile(reference="profile:code"):
    return {"id": "code", "client": "api", "credential_ref": reference}


def test_ephemeral_key_lifetime_and_status_are_not_filesystem_mutations(tmp_path):
    settings = Settings.load(tmp_path / "data")
    store = CredentialStore(settings)
    assert not store.status(profile())["configured"]
    assert not settings.data_dir.exists()
    result = store.set("code", "PRIVATE_SESSION_MARKER")
    assert result == {"configured": True, "storage": "session", "code": "configured_unchecked"}
    assert store.resolve(profile()) == "PRIVATE_SESSION_MARKER"
    assert "PRIVATE_SESSION_MARKER" not in json.dumps(store.status(profile()))
    assert not settings.data_dir.exists()
    assert CredentialStore(settings).resolve(profile()) is None
    store.clear_session()
    assert store.resolve(profile()) is None


def test_opt_in_persistence_private_modes_replacement_and_deletion(tmp_path):
    settings = Settings.load(tmp_path / "data")
    store = CredentialStore(settings)
    store.set("code", "PRIVATE_OLD_MARKER", persist=True)
    assert store.root.stat().st_mode & 0o777 == 0o700
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert CredentialStore(settings).resolve(profile()) == "PRIVATE_OLD_MARKER"
    assert store.status(profile())["storage"] == "local"
    store.set("code", "PRIVATE_NEW_SESSION_MARKER", persist=False)
    assert "PRIVATE_OLD_MARKER" not in store.path.read_text()
    assert "PRIVATE_NEW_SESSION_MARKER" not in store.path.read_text()
    assert store.resolve(profile()) == "PRIVATE_NEW_SESSION_MARKER"
    assert CredentialStore(settings).resolve(profile()) is None
    store.set("other", "PRIVATE_OTHER_MARKER", persist=True)
    store.delete("code")
    assert store.resolve(profile()) is None
    assert store.resolve(profile("profile:other")) == "PRIVATE_OTHER_MARKER"
    assert store.delete("other")["configured"] is False
    assert CredentialStore(settings).resolve(profile("profile:other")) is None


def test_custom_environment_reference_never_copies_key_to_disk_or_public_status(tmp_path):
    settings = Settings.load(tmp_path / "data")
    store = CredentialStore(settings, environ={"CUSTOM_PROVIDER_KEY": "PRIVATE_ENV_MARKER"})
    selected = profile("env:CUSTOM_PROVIDER_KEY")
    assert store.resolve(selected) == "PRIVATE_ENV_MARKER"
    status = store.status(selected)
    assert status == {"configured": True, "storage": "environment", "code": "configured_unchecked"}
    assert "PRIVATE_ENV_MARKER" not in json.dumps(status)
    assert not settings.data_dir.exists()
    assert {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"} <= set(credential_environment_names())


@pytest.mark.parametrize("key", ["", None, "unsafe\nPRIVATE", "unsafe\rPRIVATE", "x\0PRIVATE", "ümlaut", "x" * 16385])
def test_invalid_key_is_not_echoed_or_written(tmp_path, key):
    store = CredentialStore(Settings.load(tmp_path / "data"))
    with pytest.raises(ValueError) as error:
        store.set("code", key, persist=True)
    assert "PRIVATE" not in str(error.value)
    assert not store.root.exists()


def test_credential_store_rejects_symlinks_and_shared_file_permissions(tmp_path):
    settings = Settings.load(tmp_path / "data")
    store = CredentialStore(settings)
    store.set("code", "SAFE_MARKER", persist=True)
    store.path.chmod(0o644)
    with pytest.raises(ValueError):
        store.resolve(profile())
    store.path.chmod(0o600)
    outside = tmp_path / "outside"
    outside.write_text("PRIVATE_OUTSIDE")
    store.path.unlink()
    store.path.symlink_to(outside)
    with pytest.raises(ValueError):
        store.set("code", "ANOTHER", persist=True)
    assert outside.read_text() == "PRIVATE_OUTSIDE"


def test_delete_unknown_key_does_not_create_state(tmp_path):
    settings = Settings.load(tmp_path / "data")
    CredentialStore(settings).delete("missing")
    assert not settings.data_dir.exists()
