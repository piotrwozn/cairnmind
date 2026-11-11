"""Native readiness commands never launch models or expose account details."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from cairnmind import client_auth as auth
from cairnmind.client_adapters import native_adapter, observed_capabilities


CANARY = "PRIVATE_AUTH_OUTPUT_AND_KEY_MUST_NOT_ESCAPE_050"


@pytest.fixture(autouse=True)
def clear_cache():
    auth.clear_probe_cache()
    yield
    auth.clear_probe_cache()


def capabilities(client="codex", *, installed=True, compatible=True):
    return {"installed": installed, "path": f"/fixture/{client}" if installed else None,
        "version": "codex-cli 0.166.0 " + CANARY if client == "codex" else "2.1.319 (Claude Code)",
        **{field: compatible for field in auth._CAPABILITY_KEYS}, "notes": [CANARY]}


def setup(monkeypatch, *, client="codex", stdout="", stderr="", code=0, cap=None, exception=None):
    calls = []

    def probe(name, *, env=None):
        assert name == client
        calls.append(("capability", env))
        return cap if cap is not None else capabilities(client)

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if exception is not None:
            raise exception
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=code)

    monkeypatch.setattr(auth, "_probe_cli", probe)
    monkeypatch.setattr(auth.subprocess, "run", run)
    return calls


def test_codex_reports_saved_chatgpt_login_without_probing_model_or_leaking_details(monkeypatch):
    calls = setup(monkeypatch, stderr="Logged in using ChatGPT\n" + CANARY)
    environment = {"PATH": "/fixture", "OPENAI_API_KEY": CANARY, "CODEX_HOME": "/fixture/account-a"}
    result = auth.probe_client("codex", env=environment)
    assert result["installation"] == "installed"
    assert result["auth"] == "reported_signed_in"
    assert result["auth_method"] == "chatgpt"
    assert result["ready_to_launch"] is True
    assert result["usage"] == "unknown" and result["connectivity"] == "unchecked"
    assert result["version"] == "0.166.0"
    assert result["credential_configuration"] == "environment_present"
    assert CANARY not in json.dumps(result)
    assert calls[0] == ("capability", environment)
    assert calls[1][0] == ["/fixture/codex", "login", "status"]
    assert calls[1][1]["env"] == environment
    assert calls[1][1]["timeout"] == auth.STATUS_TIMEOUT
    assert len(calls) == 2


def test_claude_json_is_sanitized_and_explicit_login_is_preserved(monkeypatch):
    calls = setup(monkeypatch, client="claude", stdout=json.dumps({
        "loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
        "email": CANARY, "orgId": CANARY, "organizationName": CANARY,
        "loginExpiresAt": "2099-12-05T17:09:11Z", "token": CANARY,
    }))
    result = auth.probe_client("claude", env={"PATH": "/fixture"})
    assert result["auth"] == "reported_signed_in"
    assert result["auth_method"] == "claude.ai"
    assert result["login_expires_at"] == "2099-12-05T17:09:11+00:00"
    assert result["ready_to_launch"] is True
    assert CANARY not in json.dumps(result)
    assert calls[1][0] == ["/fixture/claude", "auth", "status", "--json"]


@pytest.mark.parametrize("client,stdout,stderr", [
    ("codex", "", "Not logged in"),
    ("claude", '{"loggedIn":false,"authMethod":null}', ""),
])
def test_explicit_signed_out_is_distinct_from_unknown_or_network(monkeypatch, client, stdout, stderr):
    setup(monkeypatch, client=client, stdout=stdout, stderr=stderr, code=1)
    result = auth.probe_client(client, env={"PATH": "/fixture"})
    assert result["auth"] == "signed_out"
    assert result["login_required"] is True
    assert result["ready_to_launch"] is False
    assert result["connectivity"] == "unchecked"


def test_native_api_environment_is_a_configuration_hint_not_claimed_saved_login(monkeypatch):
    setup(monkeypatch, stderr="Not logged in", code=1)
    result = auth.probe_client("codex", env={"PATH": "/fixture", "OPENAI_API_KEY": CANARY})
    assert result["auth"] == "signed_out"
    assert result["credential_configuration"] == "environment_present"
    assert result["login_required"] is False
    assert result["ready_to_launch"] is True
    assert result["usage"] == "unknown"
    assert CANARY not in json.dumps(result)


@pytest.mark.parametrize("message,expected", [
    ("unrecognized subcommand 'status' " + CANARY, "unsupported_client"),
    ("Network is unreachable " + CANARY, "network_unavailable"),
    (CANARY, "auth_status_failed"),
])
def test_failed_status_does_not_invent_logged_out(monkeypatch, message, expected):
    setup(monkeypatch, stderr=message, code=2)
    result = auth.probe_client("codex", env={"PATH": "/fixture"})
    assert result["auth"] == "unknown"
    assert result["error_code"] == expected
    assert result["login_required"] is False
    assert CANARY not in json.dumps(result)
    assert result["connectivity"] == ("unavailable" if expected == "network_unavailable" else "unchecked")
    if expected == "unsupported_client":
        assert result["auth_status_supported"] is False
        assert result["login_supported"] is False


@pytest.mark.parametrize("output", [CANARY, '{"loggedIn":"true"}', '[]'])
def test_bad_claude_auth_schema_is_unknown_without_raw_payload(monkeypatch, output):
    setup(monkeypatch, client="claude", stdout=output)
    result = auth.probe_client("claude", env={"PATH": "/fixture"})
    assert result["auth"] == "unknown"
    assert result["error_code"] == "auth_status_invalid"
    assert result["ready_to_launch"] is False
    assert CANARY not in json.dumps(result)


def test_expired_saved_login_and_rejected_key_are_separate_from_quota(monkeypatch):
    setup(monkeypatch, client="claude", stdout=json.dumps({"loggedIn": True, "authMethod": "oauth",
        "loginExpiresAt": "2000-01-01T00:00:00+00:00"}))
    result = auth.probe_client("claude", env={"PATH": "/fixture"})
    assert result["auth"] == "expired_or_rejected" and result["error_code"] == "login_expired"
    assert result["usage"] == "unknown" and result["ready_to_launch"] is False
    auth.clear_probe_cache()
    setup(monkeypatch, stderr="invalid_api_key " + CANARY, code=1)
    result = auth.probe_client("codex", env={"PATH": "/fixture", "OPENAI_API_KEY": CANARY})
    assert result["auth"] == "expired_or_rejected"
    assert result["ready_to_launch"] is False


def test_usage_signal_does_not_clear_reported_login(monkeypatch):
    setup(monkeypatch, stderr="Logged in using ChatGPT\nYou've hit your usage limit.")
    result = auth.probe_client("codex", env={"PATH": "/fixture"})
    assert result["auth"] == "reported_signed_in"
    assert result["usage"] == "quota_exhausted"
    assert result["login_required"] is False
    assert result["ready_to_launch"] is False


def test_missing_client_never_runs_a_status_command(monkeypatch):
    calls = setup(monkeypatch, cap=capabilities(installed=False))
    result = auth.probe_client("codex", env={"PATH": "/fixture"})
    assert result["installation"] == "missing" and result["auth"] == "unknown"
    assert result["error_code"] == "client_missing"
    assert len(calls) == 1


@pytest.mark.parametrize("exception,expected", [
    (subprocess.TimeoutExpired([CANARY], 8, output=CANARY, stderr=CANARY), "auth_status_timeout"),
    (OSError(CANARY), "auth_status_unavailable"),
])
def test_status_failure_is_bounded_and_never_returns_exception_body(monkeypatch, exception, expected):
    setup(monkeypatch, exception=exception)
    result = auth.probe_client("codex", env={"PATH": "/fixture"})
    assert result["auth"] == "unknown"
    assert result["error_code"] == expected
    assert CANARY not in json.dumps(result)


def test_cache_is_environment_scoped_defensive_and_refreshable(monkeypatch):
    calls = setup(monkeypatch, stderr="Logged in using ChatGPT")
    env_a = {"PATH": "/fixture", "CODEX_HOME": "/account-a"}
    first = auth.probe_client("codex", env=env_a)
    first["capabilities"]["verified_resume"] = False
    second = auth.probe_client("codex", env=env_a)
    assert len(calls) == 2 and second["cached"] is True
    assert second["capabilities"]["verified_resume"] is True
    auth.probe_client("codex", env={**env_a, "CODEX_HOME": "/account-b"})
    assert len(calls) == 4
    auth.probe_client("codex", env=env_a, force=True)
    assert len(calls) == 6
    with auth._LOCK:
        for key, (_, value) in list(auth._CACHE.items()):
            auth._CACHE[key] = (time.monotonic() - auth.CACHE_SECONDS - 1, value)
    auth.probe_client("codex", env=env_a)
    assert len(calls) == 8


@pytest.mark.parametrize("text,code", [
    ("You're out of usage credits. " + CANARY, "quota_exhausted"),
    ("Rate_limit_error: retry later", "rate_limited"),
    ("API token has expired", "auth_rejected"),
    ("Please log in again", "auth_required"),
    ("ECONNREFUSED", "network_unavailable"),
    ("This source discusses a quota, and contains " + CANARY, "unknown"),
])
def test_runtime_error_hints_are_fixed_codes_and_not_arbitrary_text(text, code):
    assert auth.normalize_runtime_error(text) == code
    assert CANARY not in auth.normalize_runtime_error(text)


def test_login_commands_are_native_and_do_not_execute_or_archive(monkeypatch):
    monkeypatch.setattr(auth.shutil, "which", lambda client, path=None: "/native/" + client)
    monkeypatch.setattr(auth.platform, "release", lambda: "6.6.0-microsoft-standard-WSL2")
    monkeypatch.setattr(auth.subprocess, "run", lambda *a, **kw: pytest.fail("Login helper executed a process"))
    assert auth.auth_command("codex", env={"PATH": "/native"}) == ["/native/codex", "login", "--device-auth"]
    assert auth.auth_command("claude", env={"PATH": "/native"}) == ["/native/claude", "auth", "login"]
    with pytest.raises(ValueError, match="Only"):
        auth.auth_command("arbitrary-command")


def test_native_registry_does_not_enable_future_api_runtime_or_guess_verified_capabilities():
    assert native_adapter("codex").adapter_id == "native.codex"
    assert native_adapter("claude").runtime == "native"
    with pytest.raises(ValueError):
        native_adapter("devbrain_api")
    observed = observed_capabilities("codex", {"installed": True, "login_supported": True,
        "capabilities": {"verified_resume": False, "session_binding": True}})
    assert observed.terminal is True and observed.native_login is True
    assert observed.verified_resume is False and observed.session_binding is True
    assert observed.api_tools is False


def test_actual_profile_executable_runs_only_read_only_native_commands(tmp_path):
    binary, calls = tmp_path / "codex", tmp_path / "calls.jsonl"
    binary.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
with open(os.environ["PROBE_CALLS"], "a") as out:
    out.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:] == ["--version"]:
    print("codex-cli 0.166.0")
elif sys.argv[1:] == ["--help"]:
    print("mcp prompt --no-daemon")
elif sys.argv[1:] == ["features", "list"]:
    print("hooks stable true")
elif sys.argv[1:] == ["login", "status"]:
    print("Logged in using ChatGPT")
    print(os.environ["OUTPUT_CANARY"], file=sys.stderr)
else:
    raise SystemExit(97)
''')
    binary.chmod(0o700)
    result = auth.probe_client("codex", env={
        "PATH": str(tmp_path), "PROBE_CALLS": str(calls), "OUTPUT_CANARY": CANARY,
    })
    assert result["path"] == str(binary)
    assert result["ready_to_launch"] is True
    assert CANARY not in json.dumps(result)
    assert [json.loads(line) for line in calls.read_text().splitlines()] == [
        ["--version"], ["--help"], ["features", "list"], ["login", "status"],
    ]


def test_unreadable_capability_output_is_safely_unsupported(monkeypatch):
    def probe(*args, **kwargs):
        raise UnicodeDecodeError("utf8", b"bad", 0, 1, CANARY)

    monkeypatch.setattr(auth, "_probe_cli", probe)
    monkeypatch.setattr(auth.shutil, "which", lambda *a, **kw: "/fixture/codex")
    monkeypatch.setattr(auth.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        stdout="Logged in using ChatGPT", stderr="", returncode=0))
    result = auth.probe_client("codex", env={"PATH": "/fixture"})
    assert result["installation"] == "unsupported"
    assert result["auth"] == "reported_signed_in"
    assert result["error_code"] == "capability_probe_failed"
    assert result["ready_to_launch"] is False
    assert CANARY not in json.dumps(result)
