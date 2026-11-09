"""Pure account-controller regressions; no subprocess, database or model calls."""
import ast
import copy
import base64
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def controller(tmp_path):
    spec = importlib.util.spec_from_file_location("account080_guards", Path(__file__).with_name("validate-native-account080.py"))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    # Synthetic files, not the machine's cgroup hierarchy or boot identity.
    value.OAUTH_BOOT_ID_PATH = tmp_path / "boot-id"
    value.OAUTH_BOOT_ID_PATH.write_text("11111111-1111-1111-1111-111111111111")
    value.OAUTH_CGROUP_ROOT = tmp_path / "cgroup"
    value.OAUTH_CGROUP_ROOT.mkdir()
    (value.OAUTH_CGROUP_ROOT / "cgroup.controllers").write_text("memory pids")
    group = "/user.slice/test/devbrain-q-" + "1" * 32 + ".scope"
    path = value.OAUTH_CGROUP_ROOT / group.lstrip("/")
    path.mkdir(parents=True)
    (path / "cgroup.events").write_text("populated 0\nfrozen 0\n")
    info = path.stat()
    value.test_scope = SimpleNamespace(group=group, path=path, inode=f"{info.st_dev}:{info.st_ino}", check=lambda: None)
    return value


def phases():
    return [{"client": client, "status": "passed", "fresh_process": True,
             "previous_ack_invalidated": index > 0, "compaction_client": "claude" if index == 2 else None}
            for index, client in enumerate(("codex", "claude", "codex"))]


@pytest.mark.parametrize("ownership", ["verified", "changed_before", "changed_after", "foreign_group", "unreadable"])
def test_cleanup_failure_preserves_only_verified_owned_process_metadata(controller, monkeypatch, ownership):
    parent = {"pid": 101}
    row = {"pid": 202, "birth": "12345", "state": "S", "uid": 1000, "parent_pid": 303,
           "executable": "/synthetic/bin/tmux", "cmdline": "SECRET_COMMAND",
           "env": {"TOKEN": "SECRET_TOKEN"}, "cwd": "/PRIVATE_PROJECT"}
    group = "/user.slice/devbrain-q-" + "a" * 32 + ".scope"
    own_checks = iter([ownership != "changed_before", ownership != "changed_after"])
    scope = SimpleNamespace(unit="devbrain-q-" + "a" * 32 + ".scope", group=group, inode="25:123",
                            members=lambda **kwargs: [row], owns=lambda value: next(own_checks))
    def process_group(pid):
        assert pid == row["pid"]
        if ownership == "unreadable":
            raise PermissionError("PRIVATE_ERROR_DETAILS")
        return "/foreign.scope" if ownership == "foreign_group" else group
    supervision = SimpleNamespace(same_process=lambda value: value is parent, process_cgroup=process_group)
    monkeypatch.setattr(controller.os, "getppid", lambda: parent["pid"])
    with pytest.raises(controller.ValidationError, match="^owned_descendant_remains$") as caught:
        controller.quiet_scope(scope, supervision, parent)
    snapshot = caught.value.owned_descendant_snapshot
    assert snapshot["scope_unit"] == scope.unit and snapshot["scope_cgroup_identity"] == "25:123"
    assert snapshot["control_group"] == group
    if ownership == "verified":
        assert snapshot["capture_status"] == "verified_owned"
        assert snapshot["processes"] == [{**{key: row[key] for key in
            ("pid", "birth", "state", "uid", "parent_pid", "executable")}, "control_group": group}]
    else:
        assert snapshot["processes"] == []
        assert snapshot["capture_status"] == ("unavailable" if ownership == "unreadable" else "identity_changed")
    text = json.dumps(snapshot)
    assert all(value not in text for value in ("SECRET_COMMAND", "SECRET_TOKEN", "PRIVATE_PROJECT", "PRIVATE_ERROR_DETAILS"))


def test_exact_real_three_phase_handoff_is_required(controller):
    assert controller.native_passed(phases())


@pytest.mark.parametrize("problem", ["missing_phase", "not_run", "wrong_client", "no_process", "stale_ack2", "stale_ack3", "no_compaction"])
def test_partial_native_results_never_qualify(controller, problem):
    values = copy.deepcopy(phases())
    if problem == "missing_phase":
        values.pop()
    elif problem == "not_run":
        values[1]["status"] = "not_run"
    elif problem == "wrong_client":
        values[1]["client"] = "codex"
    elif problem == "no_process":
        values[1]["fresh_process"] = False
    elif problem.startswith("stale_ack"):
        values[int(problem[-1]) - 1]["previous_ack_invalidated"] = False
    else:
        values[2]["compaction_client"] = None
    assert not controller.native_passed(values)


def test_private_environment_omits_keys_routes_and_global_configuration(controller, monkeypatch, tmp_path):
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "COPILOT_PROVIDER_API_KEY", "GH_TOKEN", "CLAUDECODE",
                 "CLAUDE_CODE_OAUTH_TOKEN", "CODEX_THREAD_ID", "JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS"):
        monkeypatch.setenv(name, "synthetic-secret")
    runtime = {"java_executable": "/qualified/jdk/bin/java", "tmux_executable": "/qualified/bin/tmux"}
    value = controller.clean_environment(tmp_path, runtime, "synthetic-run")
    assert "synthetic-secret" not in value.values()
    assert value["HOME"] == str(tmp_path / "home") and value["CODEX_HOME"] == str(tmp_path / "codex-auth")
    assert value["CLAUDE_CONFIG_DIR"] == str(tmp_path / "claude-auth")
    assert value["DEVBRAIN_QUALIFICATION_RUN_ID"] == "synthetic-run"


@pytest.mark.parametrize("cleaner_fails", [False, True])
def test_account_entrypoint_builds_environment_before_clearing_it(controller, cleaner_fails):
    # The shared native/Copilot entrypoint's actual boundary, without executing
    # account setup, auth refresh, engine startup or any subprocess.
    tree = ast.parse(Path(controller.__file__).read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
    boundaries = []
    for node in ast.walk(function):
        for _, body in ast.iter_fields(node):
            if not isinstance(body, list):
                continue
            for index, statement in enumerate(body):
                if (isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Call)
                        and ast.unparse(statement.value.func) == "clean_environment"):
                    boundaries.append(body[index:index + 3])
    assert len(boundaries) == 1 and len(boundaries[0]) == 3
    assert ast.unparse(boundaries[0][1]) == "os.environ.clear()"
    incoming = {"LANG": "pl_PL.UTF-8", "LC_ALL": "C.UTF-8", "LC_CTYPE": "C.UTF-8",
                "TZ": "Europe/Warsaw", "PRIVATE_API_KEY": "synthetic-secret"}
    process_environment = dict(incoming)
    calls = []

    def clean(work, runtime, run_id, binaries):
        calls.append((work, runtime, run_id, binaries))
        assert process_environment == incoming
        if cleaner_fails:
            raise ValueError("synthetic_environment_failure")
        return {key: incoming[key] for key in ("LANG", "LC_ALL", "LC_CTYPE", "TZ")}

    values = {"os": SimpleNamespace(environ=process_environment), "clean_environment": clean,
              "work": Path("/synthetic/private"), "runtime": {"qualified": True},
              "args": SimpleNamespace(run_id="synthetic"), "binaries": {}}
    code = compile(ast.Module(body=boundaries[0], type_ignores=[]), controller.__file__, "exec")
    if cleaner_fails:
        with pytest.raises(ValueError, match="synthetic_environment_failure"):
            exec(code, values)
        assert process_environment == incoming
    else:
        exec(code, values)
        assert process_environment == {key: incoming[key] for key in ("LANG", "LC_ALL", "LC_CTYPE", "TZ")}
    assert calls == [(values["work"], values["runtime"], "synthetic", {})]


def test_private_environment_preserves_locale_without_copying_unrelated_values(controller, monkeypatch, tmp_path):
    allowed = {"LANG": "pl_PL.UTF-8", "LC_ALL": "C.UTF-8", "LC_CTYPE": "C.UTF-8",
               "TZ": "Europe/Warsaw", "SSL_CERT_FILE": "/synthetic/cert.pem", "SSL_CERT_DIR": "/synthetic/certs"}
    for key, value in allowed.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("UNRELATED_PRIVATE_VALUE", "synthetic-secret")
    runtime = {"java_executable": "/qualified/jdk/bin/java", "tmux_executable": "/qualified/bin/tmux"}
    value = controller.clean_environment(tmp_path, runtime, "synthetic-run")
    assert {key: value.get(key) for key in allowed} == allowed
    assert "UNRELATED_PRIVATE_VALUE" not in value and "synthetic-secret" not in value.values()


def payload(generation=1, *, account="synthetic-workspace", user="synthetic-user", legacy=True):
    claims = {"sub": "synthetic-subject", "iss": "https://synthetic.invalid", "aud": "synthetic-client",
              "https://api.openai.com/auth": {"chatgpt_user_id": user, "chatgpt_account_id": account}}
    token = "header." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".signature"
    tokens = {"access_token": f"synthetic-access-{generation}", "refresh_token": f"synthetic-refresh-{generation}",
              "id_token": token, "account_id": account}
    if not legacy:
        tokens.update(login_owner_id="synthetic-owner", oauth_provenance={"issuer": "synthetic", "client_id": "test"})
    return {"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": tokens,
            "last_refresh": f"2026-11-{generation:02d}T12:00:00Z"}


def write_private(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


@pytest.fixture
def auth(tmp_path):
    tmp_path.chmod(0o700)
    original, private = tmp_path / "original", tmp_path / "private"
    original.mkdir(mode=0o700)
    private.mkdir(mode=0o700)
    source, destination = original / "auth.json", private / "auth.json"
    write_private(source, payload())
    return source, destination


@pytest.fixture
def seeded(controller, auth):
    row = controller.copy_oauth(*auth, "codex", scope=controller.test_scope)
    try:
        yield row
    finally:
        controller.release_native_auth([row], close_journal=False)


def abandon(row):
    # Simulates the OS releasing a dead controller's lease, with durable files
    # intact. No subprocess, real account or network operation is involved.
    os.close(row.pop("run_lock"))


def journal(row):
    return json.loads(row["journal_path"].read_bytes())


def test_codex_seed_is_private_durable_and_official_schema_unchanged(controller, seeded):
    source, destination = seeded["source"], seeded["destination"]
    value = journal(seeded)
    assert source.read_bytes() == destination.read_bytes()
    assert value["state"] == "prepared" and value["closed"] is False
    assert set(json.loads(source.read_bytes())) == set(payload())
    assert value["source"] == str(source) and value["destination"] == str(destination)
    assert seeded["journal_path"].parent.stat().st_mode & 0o777 == 0o700
    for path in (source, destination, seeded["journal_path"], Path(value["baseline_path"])):
        assert path.stat().st_mode & 0o777 == 0o600


def test_same_account_refresh_commits_and_release_closes_journal(controller, seeded):
    write_private(seeded["destination"], payload(2))
    result = controller.sync_native_auth([seeded])
    assert result["passed"] and result["codex_cache_synced"]
    assert json.loads(seeded["source"].read_bytes()) == payload(2)
    assert journal(seeded)["state"] == "committed"
    assert controller.sync_native_auth([seeded])["passed"]
    assert controller.release_native_auth([seeded])["passed"]
    assert journal(seeded)["closed"]
    assert "synthetic" not in json.dumps(result)
    assert controller.sha(seeded["source"]) not in json.dumps(result)


@pytest.mark.parametrize("change", ["account", "user", "api_key", "false_api_key", "other_field", "old_timestamp"])
def test_changed_account_or_non_refresh_fields_are_refused(controller, seeded, change):
    candidate = payload(2, account="different" if change == "account" else "synthetic-workspace",
                        user="different" if change == "user" else "synthetic-user")
    if change == "api_key":
        candidate["OPENAI_API_KEY"] = "synthetic-key"
    elif change == "false_api_key":
        candidate["OPENAI_API_KEY"] = False
    elif change == "other_field":
        candidate["new_setting"] = True
    elif change == "old_timestamp":
        candidate["last_refresh"] = payload()["last_refresh"]
    original = seeded["source"].read_bytes()
    write_private(seeded["destination"], candidate)
    assert not controller.sync_native_auth([seeded])["passed"]
    assert seeded["source"].read_bytes() == original
    assert not journal(seeded)["closed"]


@pytest.mark.parametrize("field", ["login_owner_id", "oauth_provenance"])
def test_existing_optional_owner_and_provenance_must_be_preserved(controller, auth, field):
    source, destination = auth
    write_private(source, payload(legacy=False))
    row = controller.copy_oauth(source, destination, "codex", scope=controller.test_scope)
    try:
        candidate = payload(2, legacy=False)
        candidate["tokens"].pop(field)
        write_private(destination, candidate)
        assert not controller.sync_native_auth([row])["passed"]
        assert json.loads(source.read_bytes()) == payload(legacy=False)
    finally:
        controller.release_native_auth([row], close_journal=False)


def test_legacy_cache_need_not_invent_owner_or_provenance(controller, seeded):
    write_private(seeded["destination"], payload(2))
    assert controller.sync_native_auth([seeded])["passed"]
    assert "login_owner_id" not in json.loads(seeded["source"].read_bytes())["tokens"]


@pytest.mark.parametrize("field", ["account_id", "chatgpt_user_id"])
def test_nonempty_workspace_and_user_are_required(controller, field):
    candidate = payload(account="" if field == "account_id" else "synthetic-workspace",
                        user="" if field == "chatgpt_user_id" else "synthetic-user")
    with pytest.raises(controller.ValidationError, match="principal_required"):
        controller._codex_identity(candidate)


def test_source_conflict_retains_both_caches_and_never_overwrites(controller, seeded):
    write_private(seeded["destination"], payload(2))
    write_private(seeded["source"], payload(3, account="other-workspace"))
    before = seeded["source"].read_bytes()
    result = controller.sync_native_auth([seeded])
    assert not result["passed"] and result["conflict"]
    assert seeded["source"].read_bytes() == before
    assert json.loads(seeded["destination"].read_bytes()) == payload(2)
    assert not controller.release_native_auth([seeded])["passed"]
    assert not journal(seeded)["closed"]


def test_lifetime_lease_serializes_two_test_runs(controller, seeded, tmp_path):
    other = tmp_path / "another"
    other.mkdir(mode=0o700)
    with pytest.raises(controller.ValidationError, match="source_in_use"):
        controller.copy_oauth(seeded["source"], other / "auth.json", "codex", scope=controller.test_scope)
    assert not (other / "auth.json").exists()


def test_official_auth_lock_blocks_sync_without_replacing_lock(controller, seeded, monkeypatch):
    path = seeded["source"].parent / "auth.lock"
    path.chmod(0o644)  # The official client's default umask is supported.
    descriptor = controller._open_oauth_lock(path, official=True)
    inode = path.stat().st_ino
    clock = iter((0, 10))
    monkeypatch.setattr(controller.time, "monotonic", lambda: next(clock))
    try:
        write_private(seeded["destination"], payload(2))
        result = controller.sync_native_auth([seeded])
        assert result["error_codes"] == ["oauth_source_in_use"]
        assert path.stat().st_ino == inode and path.stat().st_mode & 0o777 == 0o644
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("kind", ["symlink", "writable", "hardlink"])
def test_unsafe_official_lock_is_rejected(controller, auth, kind, tmp_path):
    path = auth[0].parent / "auth.lock"
    outside = tmp_path / "outside"
    outside.write_text("")
    outside.chmod(0o600)
    if kind == "symlink":
        path.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, path)
    else:
        path.write_text("")
        path.chmod(0o666)
    with pytest.raises(controller.ValidationError):
        controller._open_oauth_lock(path, official=True)


def test_failed_model_phase_still_preserves_completed_refresh(controller, seeded):
    class ModelFailed(Exception):
        pass
    with pytest.raises(ModelFailed):
        try:
            write_private(seeded["destination"], payload(2))
            raise ModelFailed()
        finally:
            result = controller.sync_native_auth([seeded])
    assert result["passed"] and json.loads(seeded["source"].read_bytes()) == payload(2)


def test_replace_then_fsync_failure_is_recovered_as_our_commit(controller, seeded, monkeypatch):
    source = seeded["source"]
    write_private(seeded["destination"], payload(2))
    real_fsync = controller._fsync_directory
    def fail_after_replace(path):
        if path == source.parent and json.loads(source.read_bytes()) == payload(2):
            raise OSError("synthetic fsync failure")
        return real_fsync(path)
    monkeypatch.setattr(controller, "_fsync_directory", fail_after_replace)
    result = controller.sync_native_auth([seeded])
    assert not result["passed"] and not result["conflict"]
    assert journal(seeded)["state"] == "commit_pending"
    assert json.loads(source.read_bytes()) == payload(2)
    abandon(seeded)
    monkeypatch.setattr(controller, "_fsync_directory", real_fsync)
    monkeypatch.setattr(controller, "_replace_auth", lambda *args: pytest.fail("Already committed bytes must not be replaced"))
    assert controller.recover_native_auth(source)["passed"]
    assert journal(seeded)["state"] == "committed" and journal(seeded)["closed"]


@pytest.mark.parametrize("source_state", ["baseline", "candidate", "other"])
def test_powercut_pending_recovery_uses_exact_digests_without_network(controller, seeded, monkeypatch, source_state):
    class PowerCut(BaseException):
        pass
    original_replace = controller._replace_auth
    monkeypatch.setattr(controller, "_replace_auth", lambda *args: (_ for _ in ()).throw(PowerCut()))
    monkeypatch.setattr(controller.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Recovery cannot launch any process"))
    write_private(seeded["destination"], payload(2))
    with pytest.raises(PowerCut):
        controller.sync_native_auth([seeded])
    assert journal(seeded)["state"] == "commit_pending"
    if source_state != "baseline":
        write_private(seeded["source"], payload(2 if source_state == "candidate" else 3))
    before = seeded["source"].read_bytes()
    abandon(seeded)
    monkeypatch.setattr(controller, "_replace_auth", original_replace)
    result = controller.recover_native_auth(seeded["source"])
    assert result["passed"] is (source_state != "other")
    if source_state == "other":
        assert result["conflict"] and seeded["source"].read_bytes() == before
        assert not journal(seeded)["closed"]
    else:
        assert json.loads(seeded["source"].read_bytes()) == payload(2)
        assert journal(seeded)["closed"]


def test_powercut_prepared_with_completed_cli_refresh_is_recoverable(controller, seeded):
    write_private(seeded["destination"], payload(2))
    abandon(seeded)
    assert journal(seeded)["state"] == "prepared"
    assert controller.recover_native_auth(seeded["source"])["passed"]
    assert json.loads(seeded["source"].read_bytes()) == payload(2)


def test_another_refresh_after_pending_candidate_is_also_preserved(controller, seeded, monkeypatch):
    class PowerCut(BaseException):
        pass
    original_replace = controller._replace_auth
    monkeypatch.setattr(controller, "_replace_auth", lambda *args: (_ for _ in ()).throw(PowerCut()))
    write_private(seeded["destination"], payload(2))
    with pytest.raises(PowerCut):
        controller.sync_native_auth([seeded])
    write_private(seeded["destination"], payload(3))
    abandon(seeded)
    monkeypatch.setattr(controller, "_replace_auth", original_replace)
    assert controller.recover_native_auth(seeded["source"])["passed"]
    assert json.loads(seeded["source"].read_bytes()) == payload(3)


@pytest.mark.parametrize("problem", ["source", "schema", "snapshot_hash", "snapshot_symlink", "mode"])
def test_journal_and_snapshot_tampering_fail_closed(controller, seeded, problem):
    value = journal(seeded)
    path = seeded["journal_path"]
    if problem == "source":
        value["source"] = "/unrelated/auth.json"
    elif problem == "schema":
        value["extra"] = True
    elif problem == "snapshot_hash":
        value["baseline_sha256"] = "0" * 64
    elif problem == "snapshot_symlink":
        snapshot = Path(value["baseline_path"])
        snapshot.unlink()
        snapshot.symlink_to(seeded["source"])
    write_private(path, value)
    if problem == "mode":
        path.chmod(0o644)
    original = seeded["source"].read_bytes()
    abandon(seeded)
    assert not controller.recover_native_auth(seeded["source"])["passed"]
    assert seeded["source"].read_bytes() == original


def test_uncertain_cli_cleanup_keeps_recoverable_journal(controller, seeded):
    assert controller.sync_native_auth([seeded])["passed"]
    result = controller.release_native_auth([seeded], close_journal=False)
    assert not result["passed"] and not journal(seeded)["closed"]
    write_private(seeded["destination"], payload(2))
    assert controller.recover_native_auth(seeded["source"])["passed"]


def test_codex_setup_requires_verified_current_scope(controller, auth):
    with pytest.raises(controller.ValidationError, match="owned_scope_required"):
        controller.copy_oauth(*auth, "codex")
    assert not auth[1].exists()


@pytest.mark.parametrize("problem", ["alive", "missing_events", "changed_inode", "unavailable_root"])
def test_recovery_requires_old_scope_empty_not_just_released_lease(controller, seeded, problem):
    write_private(seeded["destination"], payload(2))
    abandon(seeded)
    path = controller.test_scope.path
    if problem == "alive":
        (path / "cgroup.events").write_text("populated 1\nfrozen 0\n")
    elif problem == "missing_events":
        (path / "cgroup.events").unlink()
    elif problem == "changed_inode":
        record = journal(seeded)
        record["scope_binding"]["inode"] = "0:0"
        write_private(seeded["journal_path"], record)
    else:
        (controller.OAUTH_CGROUP_ROOT / "cgroup.controllers").unlink()
    original = seeded["source"].read_bytes()
    assert not controller.recover_native_auth(seeded["source"])["passed"]
    assert seeded["source"].read_bytes() == original
    assert not journal(seeded)["closed"]


@pytest.mark.parametrize("proof", ["removed_scope", "new_boot"])
def test_recovery_accepts_removed_previous_scope_or_new_linux_boot(controller, seeded, proof):
    write_private(seeded["destination"], payload(2))
    abandon(seeded)
    if proof == "new_boot":
        (controller.test_scope.path / "cgroup.events").write_text("populated 1\n")
        controller.OAUTH_BOOT_ID_PATH.write_text("22222222-2222-2222-2222-222222222222")
    else:
        (controller.test_scope.path / "cgroup.events").unlink()
        controller.test_scope.path.rmdir()
    assert controller.recover_native_auth(seeded["source"])["passed"]
    assert json.loads(seeded["source"].read_bytes()) == payload(2)


def test_next_setup_cannot_discard_live_orphan_transaction(controller, seeded, tmp_path):
    write_private(seeded["destination"], payload(2))
    abandon(seeded)
    (controller.test_scope.path / "cgroup.events").write_text("populated 1\n")
    next_home = tmp_path / "blocked-next"
    next_home.mkdir(mode=0o700)
    before = seeded["journal_path"].read_bytes()
    with pytest.raises(controller.ValidationError, match="previous_scope_not_empty"):
        controller.copy_oauth(seeded["source"], next_home / "auth.json", "codex", scope=controller.test_scope)
    assert seeded["journal_path"].read_bytes() == before
    assert not (next_home / "auth.json").exists()


def test_next_setup_recovers_before_seeding_new_private_home(controller, seeded, tmp_path):
    write_private(seeded["destination"], payload(2))
    abandon(seeded)
    next_home = tmp_path / "next"
    next_home.mkdir(mode=0o700)
    row = controller.copy_oauth(seeded["source"], next_home / "auth.json", "codex", scope=controller.test_scope)
    try:
        assert json.loads(row["destination"].read_bytes()) == payload(2)
    finally:
        controller.release_native_auth([row], close_journal=False)


def test_claude_uses_only_pinned_official_secure_storage(controller, auth, tmp_path, monkeypatch):
    source = auth[0].with_name(".credentials.json")
    write_private(source, {"claudeAiOauth": {"accessToken": "synthetic-access", "refreshToken": "synthetic-refresh"}})
    binary = tmp_path / "claude"
    binary.write_bytes(b"synthetic pinned executable")
    monkeypatch.setattr(controller, "CLAUDE_SECURE_STORAGE_BINARY_SHA256", controller.sha(binary))
    destination = auth[1].with_name(".credentials.json")
    row = controller.prepare_native_auth("claude", source, destination, binary)
    assert not destination.exists()
    assert controller.native_auth_environment([row]) == {"CLAUDE_SECURESTORAGE_CONFIG_DIR": str(source.parent)}
    assert controller.sync_native_auth([row])["passed"]
    write_private(source, {})
    assert not controller.sync_native_auth([row])["passed"]


def test_claude_binary_pin_is_checked_before_reading_credentials(controller, auth, tmp_path, monkeypatch):
    binary = tmp_path / "claude"
    binary.write_bytes(b"unqualified executable")
    monkeypatch.setattr(controller, "_read_oauth", lambda *_: pytest.fail("Unpinned binary must not read OAuth"))
    with pytest.raises(controller.ValidationError, match="binary_not_pinned"):
        controller.prepare_native_auth("claude", auth[0].with_name(".credentials.json"), auth[1], binary)


def test_claude_credentials_are_never_copied(controller, auth):
    with pytest.raises(controller.ValidationError, match="official_shared_storage"):
        controller.copy_oauth(*auth, "claude")


def test_shared_readable_oauth_is_refused(controller, tmp_path):
    tmp_path.chmod(0o700)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    source = tmp_path / "oauth.json"
    source.write_text("{}")
    source.chmod(0o644)
    with pytest.raises(controller.ValidationError, match="private_regular_owned"):
        controller.copy_oauth(source, private / "copy.json", "codex", scope=controller.test_scope)


def test_engine_signal_requires_original_cgroup_identity(controller, monkeypatch):
    class Process:
        def poll(self):
            return None
    engine = controller.OwnedEngine(None, None, {"java_executable": "/qualified/java"}, None, None, None)
    engine.process, engine.row, engine.handle = Process(), {"executable": "/qualified/java"}, 42
    engine.scope = type("Scope", (), {"owns": lambda self, row: False})()
    monkeypatch.setattr(controller.os, "close", lambda _: None)
    monkeypatch.setattr(controller.signal, "pidfd_send_signal", lambda *_: pytest.fail("Do not signal a changed identity"))
    with pytest.raises(controller.ValidationError, match="stop_identity_not_owned"):
        engine.stop()
    assert engine.handle is None


def test_engine_exit_between_polls_cannot_claim_controlled_shutdown(controller, monkeypatch):
    polls = iter((None, 0))
    class Process:
        def poll(self):
            return next(polls)
        def wait(self, timeout):
            return 0
    engine = controller.OwnedEngine(None, None, {"java_executable": "/qualified/java"}, None, None, None)
    engine.process, engine.handle = Process(), 42
    monkeypatch.setattr(controller.os, "close", lambda _: None)
    monkeypatch.setattr(controller.select, "select", lambda *args: ([42], [], []))
    monkeypatch.setattr(controller.signal, "pidfd_send_signal", lambda *_: pytest.fail("Already exited child must not be signalled"))
    assert not engine.stop()
    assert engine.report["actual_exit_verified"] is True and engine.report["cleanup_complete"] is False
    assert engine.report.get("graceful_shutdown_requested") is not True
