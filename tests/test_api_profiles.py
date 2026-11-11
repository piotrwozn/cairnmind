from __future__ import annotations

import json

import pytest

from cairnmind.api_profiles import normalize_profile
from cairnmind.cockpit_state import CockpitState
from cairnmind.config import Settings


def api(**changes):
    return {"id": "coding", "client": "api", "label": "Coding", "provider": "openai", "model": "model-fixture", **changes}


def test_native_profiles_keep_their_public_shape():
    for client in ("codex", "claude"):
        row = {"id": client, "client": client, "label": client, "model": "native-model"}
        assert normalize_profile(row) == row


@pytest.mark.parametrize("provider,protocol,base", [
    ("openai", "openai_responses", "https://api.openai.com/v1"),
    ("anthropic", "anthropic_messages", "https://api.anthropic.com/v1"),
    ("ollama", "ollama_chat", "http://127.0.0.1:11434"),
    ("openai_compatible", "openai_chat_completions", "http://localhost:9911/v1"),
])
def test_provider_defaults_are_explicit_and_local(provider, protocol, base):
    row = api(provider=provider)
    if provider == "openai_compatible":
        row["base_url"] = base + "/"
    normalized = normalize_profile(row)
    assert normalized["protocol"] == protocol
    assert normalized["base_url"] == base
    assert normalized["limits"] == {"max_model_calls": 20, "max_output_tokens": 4096, "command_timeout_seconds": 120,
                                    **({"context_window_tokens": 32768} if provider == "ollama" else {})}
    assert normalized["credential_ref"] == ("profile:coding" if provider in {"openai", "anthropic"} else None)
    assert "limits" not in row
    assert normalize_profile(normalized) == normalized


def test_copilot_is_a_distinct_adapter_and_does_not_invent_api_auth():
    value = normalize_profile({"id": "copilot", "client": "copilot", "label": "GitHub Copilot"})
    assert value["credential_ref"] is None
    assert value["limits"] == {"max_tool_calls": 20, "max_turn_seconds": 900, "command_timeout_seconds": 120}
    with pytest.raises(ValueError):
        normalize_profile({**value, "base_url": "https://models.github.ai"})


@pytest.mark.parametrize("url", [
    "http://api.example.com/v1", "http://127.0.0.1.evil.example/v1", "https://user:private@api.example.com/v1",
    "https://api.example.com/v1?api_key=private", "https://api.example.com/#private", "ftp://api.example.com",
    "https://api.example.com:bad/v1", "http://[::1]:99999", "https://api.example.com/../v1",
    "https://api.example.com/\nprivate", "https://api.example.com\\private", "", None,
])
def test_api_address_rejects_credentials_and_plain_remote_http(url):
    with pytest.raises(ValueError):
        normalize_profile(api(base_url=url))


@pytest.mark.parametrize("change", [
    {"api_key": "PRIVATE_KEY_MARKER"}, {"headers": {"Authorization": "PRIVATE_KEY_MARKER"}},
    {"client": []}, {"model": "--skip-permissions"}, {"provider": []}, {"provider": "unknown"},
    {"protocol": "anthropic_messages"}, {"limits": {"max_model_calls": True}},
    {"limits": {"max_output_tokens": 0}}, {"limits": {"unknown": 4}},
    {"credential_ref": "PRIVATE_KEY_MARKER"}, {"credential_ref": "env:BAD=NAME"},
    {"pricing": {"input_per_million": float("nan"), "output_per_million": 1}},
    {"pricing": {"input_per_million": 1}}, {"label": "hello\x1b[2J"},
])
def test_invalid_profiles_fail_without_echoing_secret_values(change):
    with pytest.raises(ValueError) as error:
        normalize_profile(api(**change))
    assert "PRIVATE_KEY_MARKER" not in str(error.value)


def test_official_explicit_none_auth_still_requires_a_key():
    assert normalize_profile(api(credential_ref=None))["credential_ref"] == "profile:coding"
    value = normalize_profile(api(provider="openai_compatible", base_url="http://[::1]:9001/v1", credential_ref=None))
    assert value["credential_ref"] is None


@pytest.mark.parametrize("window", [4095, 262145, True, 16384.0, None])
def test_ollama_context_window_is_an_explicit_bounded_integer(window):
    with pytest.raises(ValueError):
        normalize_profile(api(provider="ollama", limits={"context_window_tokens": window}))


def test_ollama_context_configuration_does_not_leak_into_other_protocols():
    value = normalize_profile(api(provider="ollama", limits={"context_window_tokens": 65536}))
    assert value["limits"]["context_window_tokens"] == 65536
    with pytest.raises(ValueError):
        normalize_profile(api(limits={"context_window_tokens": 65536}))


def test_preferences_v1_read_is_nonmutating_and_migration_has_exact_backup(tmp_path):
    settings = Settings.load(tmp_path / "data")
    state = CockpitState(settings)
    assert state.load()["version"] == 2
    assert not settings.data_dir.exists()
    state.root.mkdir(parents=True, mode=0o700)
    original = b'{"version":1,"profiles":[{"id":"codex","client":"codex","label":"Code"},{"id":"claude","client":"claude","label":"Review"}]}\n'
    state.path.write_bytes(original)
    state.path.chmod(0o600)
    assert state.load()["version"] == 1
    assert state.path.read_bytes() == original
    assert not list(state.root.glob("*backup*"))
    with pytest.raises(ValueError):
        state.update({"default_slots": []})
    assert not list(state.root.glob("*backup*"))
    native = state.load()["profiles"]
    saved = state.update({"profiles": native + [api(), {"id": "local", "client": "api", "provider": "ollama",
                                                         "model": "qwen3.5:9b", "label": "Local"}]})
    backups = list(state.root.glob("preferences-v1-backup-*.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert backups[0].stat().st_mode & 0o777 == 0o600
    assert saved == state.load()
    assert saved["version"] == 2
    assert saved["profiles"][:2] == native
    state.update({"last_project": "project-fixture"})
    assert len(list(state.root.glob("preferences-v1-backup-*.json"))) == 1
    assert json.loads(state.path.read_text())["version"] == 2


def test_preferences_reject_embedded_keys_on_read_without_rewriting(tmp_path):
    state = CockpitState(Settings.load(tmp_path / "data"))
    state.root.mkdir(parents=True, mode=0o700)
    raw = json.dumps({"version": 2, "profiles": [api(api_key="PRIVATE_KEY_MARKER")]}).encode()
    state.path.write_bytes(raw)
    with pytest.raises(ValueError):
        state.load()
    assert state.path.read_bytes() == raw
