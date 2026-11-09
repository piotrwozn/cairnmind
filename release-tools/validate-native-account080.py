"""Explicit live account controller; inert without --execute and a passed gate.

Reuses the real native evaluator. Codex refreshes are committed with an account-
checked CAS; pinned Claude reuses only its official secure credential storage.
The production database and global MCP/hook settings are never opened or changed.
External supervision must verify this controller's exit and the final empty scope.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from uuid import uuid4


def module(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), Path(__file__).with_name(name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def require(condition, code):
    if not condition:
        raise ValidationError(code)


class ValidationError(ValueError):
    pass


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def private_directory(path, *, create=False):
    if create:
        path.mkdir(mode=0o700)
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o700 and path.resolve() == path, "private_directory_required")
    return path


CLAUDE_SECURE_STORAGE_BINARY_SHA256 = "dabafec82ca1319fa43d8be36a10fc61b1d11821cb98ad5df04ad95e199b8eed"
OAUTH_LIMIT = 1024 * 1024
OAUTH_CGROUP_ROOT = Path("/sys/fs/cgroup")
OAUTH_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "oauth_duplicate_json_key")
        result[key] = value
    return result


def _oauth_json(content):
    try:
        value = json.loads(content, object_pairs_hook=_unique_object)
        require(isinstance(value, dict), "oauth_object_required")
        return value
    except (UnicodeError, ValueError, TypeError) as exc:
        if isinstance(exc, ValidationError):
            raise
        raise ValidationError("oauth_invalid_json") from None


def _read_oauth(path):
    """Private data stays in memory; callers must never serialize this return value."""
    require(path.is_absolute() and path.resolve() == path, "oauth_path_must_be_canonical")
    private_directory(path.parent)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
                    and stat.S_IMODE(info.st_mode) == 0o600 and 0 < info.st_size <= OAUTH_LIMIT,
                    "oauth_file_must_be_private_regular_owned")
            content = stream.read(OAUTH_LIMIT + 1)
        require(0 < len(content) <= OAUTH_LIMIT, "oauth_file_size_changed")
        return content, _oauth_json(content), (info.st_dev, info.st_ino)
    except OSError:
        raise ValidationError("oauth_file_unavailable") from None


def _jwt_payload(value):
    try:
        require(isinstance(value, str) and len(value) <= 128 * 1024, "oauth_id_token_required")
        parts = value.split(".")
        require(len(parts) == 3 and all(parts), "oauth_id_token_required")
        payload = base64.b64decode(parts[1] + "=" * (-len(parts[1]) % 4), altchars=b"-_", validate=True)
        return _oauth_json(payload)
    except (ValueError, TypeError):
        raise ValidationError("oauth_id_token_required") from None


def _codex_identity(value):
    # Matches the pinned client's account_grant.same_account_login/provenance.
    # Decoding compares local identity; it does not claim JWT signature validation.
    require(value.get("auth_mode") in (None, "chatgpt")
            and (value.get("OPENAI_API_KEY") is None or value.get("OPENAI_API_KEY") == ""),
            "oauth_chatgpt_without_api_key_required")
    tokens = value.get("tokens")
    require(isinstance(tokens, dict) and all(isinstance(tokens.get(key), str) and 0 < len(tokens[key]) <= 128 * 1024
                                            for key in ("access_token", "refresh_token", "id_token")),
            "saved_refreshable_oauth_required")
    claims = _jwt_payload(tokens["id_token"])
    auth = claims.get("https://api.openai.com/auth")
    require(isinstance(auth, dict), "oauth_principal_required")
    user = auth.get("chatgpt_user_id") or auth.get("user_id")
    account = tokens.get("account_id")
    require(all(isinstance(item, str) and item and item.strip() == item for item in (user, account)),
            "oauth_principal_required")
    return (account, user, tokens.get("login_owner_id"), tokens.get("oauth_provenance"),
            claims.get("sub"), claims.get("iss"), claims.get("aud"), auth.get("chatgpt_account_id"))


def _codex_non_tokens(value):
    result = dict(value)
    result.pop("last_refresh", None)
    result["tokens"] = {key: item for key, item in value["tokens"].items()
                        if key not in {"id_token", "access_token", "refresh_token"}}
    return result


def _refresh_time(value):
    try:
        stamp = value.get("last_refresh")
        require(isinstance(stamp, str), "oauth_refresh_timestamp_required")
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        require(parsed.tzinfo is not None, "oauth_refresh_timestamp_required")
        return parsed.timestamp()
    except (TypeError, ValueError):
        raise ValidationError("oauth_refresh_timestamp_required") from None


def _open_oauth_lock(path, *, wait=False, official=False):
    private_directory(path.parent)
    require(path.resolve() == path, "oauth_lock_must_be_canonical")
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        info = os.fstat(descriptor)
        mode = stat.S_IMODE(info.st_mode)
        # The official Rust writer creates its non-secret lock with OpenOptions
        # and the current umask (often 0644). Never chmod or replace that lock.
        safe_mode = mode == 0o600 or (official and mode == 0o644)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and safe_mode and info.st_nlink == 1,
                "oauth_lock_must_be_private_owned")
        deadline = time.monotonic() + (5 if wait else 0)
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                require(time.monotonic() < deadline, "oauth_source_in_use")
                time.sleep(.05)
        current = path.lstat()
        require((current.st_dev, current.st_ino) == (info.st_dev, info.st_ino), "oauth_lock_changed")
        return descriptor
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise


@contextmanager
def _codex_commit_lock(directory):
    # Rust File::lock() on Linux uses the same flock protocol and auth.lock path.
    descriptor = _open_oauth_lock(directory / "auth.lock", wait=True, official=True)
    try:
        yield
    finally:
        os.close(descriptor)


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_private_directory(path):
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    private_directory(path)
    _fsync_directory(path.parent)
    return path


def _write_private(path, content):
    private_directory(path.parent)
    require(path.is_absolute() and path.resolve() == path, "oauth_path_must_be_canonical")
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    _fsync_directory(path.parent)


def _journal_path(source):
    # Contains private paths and digests, never published in external evidence.
    private_directory(source.parent)
    return _ensure_private_directory(source.parent / ".devbrain-validation-auth") / "journal.json"


def _oauth_boot_id():
    value = OAUTH_BOOT_ID_PATH.read_text().strip()
    require(re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", value), "oauth_boot_identity_unavailable")
    return value


def _validate_scope_binding(value):
    require(isinstance(value, dict) and set(value) == {"boot_id", "group", "inode"}, "oauth_scope_binding_invalid")
    group = value["group"]
    require(isinstance(group, str) and group.startswith("/user.slice/")
            and str(Path(group)) == group and ".." not in Path(group).parts
            and re.fullmatch(r"devbrain-q-[a-f0-9]{32}\.scope", Path(group).name)
            and isinstance(value["inode"], str) and re.fullmatch(r"[0-9]+:[0-9]+", value["inode"])
            and isinstance(value["boot_id"], str)
            and re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", value["boot_id"]),
            "oauth_scope_binding_invalid")


def _capture_oauth_scope(scope):
    require(scope is not None, "oauth_owned_scope_required")
    scope.check()
    result = {"boot_id": _oauth_boot_id(), "group": scope.group, "inode": scope.inode}
    _validate_scope_binding(result)
    path = OAUTH_CGROUP_ROOT / scope.group.lstrip("/")
    info = path.lstat()
    require(path == scope.path and path.resolve() == path and stat.S_ISDIR(info.st_mode)
            and f"{info.st_dev}:{info.st_ino}" == scope.inode, "oauth_scope_identity_changed")
    return result


def _require_previous_scope_empty(binding):
    """Read-only admission: an orphan can refresh after its controller dies."""
    _validate_scope_binding(binding)
    boot = _oauth_boot_id()
    if boot != binding["boot_id"]:
        return  # All processes belonging to the previous Linux boot are gone.
    root = OAUTH_CGROUP_ROOT
    require(root.is_dir() and root.resolve() == root and (root / "cgroup.controllers").is_file(),
            "oauth_scope_evidence_unavailable")
    path = root / binding["group"].lstrip("/")
    require(path.resolve() == path, "oauth_scope_identity_changed")
    try:
        info = path.lstat()
    except FileNotFoundError:
        require(_oauth_boot_id() == boot, "oauth_boot_identity_changed")
        return  # Removed cgroups cannot retain living member processes.
    require(stat.S_ISDIR(info.st_mode) and f"{info.st_dev}:{info.st_ino}" == binding["inode"],
            "oauth_scope_identity_changed")
    try:
        lines = (path / "cgroup.events").read_text().splitlines()
    except FileNotFoundError:
        require(not path.exists() and _oauth_boot_id() == boot, "oauth_scope_evidence_unavailable")
        return
    values = dict(line.split() for line in lines)
    require(values.get("populated") == "0", "oauth_previous_scope_not_empty")
    current = path.lstat()
    require((current.st_dev, current.st_ino) == (info.st_dev, info.st_ino)
            and _oauth_boot_id() == boot, "oauth_scope_identity_changed")


def _save_journal(path, value):
    temporary = path.with_name(".journal-" + uuid4().hex + ".tmp")
    try:
        _write_private(temporary, json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _load_journal(path, source):
    _, value, _ = _read_oauth(path)
    keys = {"schema_version", "source", "destination", "snapshot_root", "run_id", "transaction_id",
            "state", "closed", "baseline_path", "baseline_sha256", "candidate_path", "candidate_sha256",
            "binary_path", "binary_sha256", "scope_binding"}
    require(set(value) == keys and type(value["schema_version"]) is int and value["schema_version"] == 1
            and value["source"] == str(source) and value["state"] in {"prepared", "commit_pending", "committed"}
            and type(value["closed"]) is bool, "oauth_journal_invalid")
    require(isinstance(value["run_id"], str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value["run_id"])
            and isinstance(value["transaction_id"], str) and re.fullmatch(r"[a-f0-9]{32}", value["transaction_id"]),
            "oauth_journal_invalid")
    _validate_scope_binding(value["scope_binding"])
    destination = Path(value["destination"])
    root = Path(value["snapshot_root"])
    require(destination.is_absolute() and destination.resolve() == destination and destination.parent != source.parent
            and root == destination.parent / ".devbrain-validation-auth", "oauth_journal_path_invalid")
    private_directory(destination.parent)
    private_directory(root)
    for role in ("baseline", "candidate"):
        item, digest = value[role + "_path"], value[role + "_sha256"]
        if role == "candidate" and value["state"] == "prepared":
            require(item is None and digest is None, "oauth_journal_invalid")
            continue
        require(isinstance(item, str) and isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest),
                "oauth_journal_invalid")
        target = Path(item)
        require(target.parent == root and re.fullmatch(role + r"-[a-f0-9]{32}\.json", target.name)
                and target.resolve() == target, "oauth_journal_path_invalid")
        content, _, _ = _read_oauth(target)
        require(hashlib.sha256(content).hexdigest() == digest, "oauth_snapshot_changed")
    binary, digest = value["binary_path"], value["binary_sha256"]
    require((binary is None and digest is None) or (isinstance(binary, str) and Path(binary).is_absolute()
            and Path(binary).resolve() == Path(binary) and isinstance(digest, str)
            and re.fullmatch(r"[a-f0-9]{64}", digest)), "oauth_journal_invalid")
    return value


def _snapshot(root, role, content):
    path = root / (role + "-" + uuid4().hex + ".json")
    _write_private(path, content)
    return str(path), hashlib.sha256(content).hexdigest()


def _validate_codex_refresh(baseline, candidate):
    before, after = _codex_identity(baseline), _codex_identity(candidate)
    require(before[:2] == after[:2], "oauth_account_identity_changed")
    # Legacy caches may omit owner/provenance and some ID claims. Preserve
    # everything already present, but do not invent absent legacy metadata.
    require(all(old is None or old == new for old, new in zip(before[2:], after[2:])),
            "oauth_account_identity_changed")
    original_fields, new_fields = _codex_non_tokens(baseline), _codex_non_tokens(candidate)
    for key in ("login_owner_id", "oauth_provenance"):
        if key not in original_fields["tokens"]:
            new_fields["tokens"].pop(key, None)
    require(original_fields == new_fields, "oauth_non_token_fields_changed")
    require(_refresh_time(candidate) > _refresh_time(baseline), "oauth_refresh_not_newer")


def _journal_binary_unchanged(journal):
    if journal["binary_path"] is not None:
        require(sha(Path(journal["binary_path"])) == journal["binary_sha256"], "native_binary_changed")


def _replace_auth(source, content, baseline_sha):
    original, _, identity = _read_oauth(source)
    require(hashlib.sha256(original).hexdigest() == baseline_sha, "oauth_source_conflict")
    temporary = source.with_name(".devbrain-auth-" + uuid4().hex + ".tmp")
    try:
        _write_private(temporary, content)
        again, _, current = _read_oauth(source)
        require(current == identity and hashlib.sha256(again).hexdigest() == baseline_sha, "oauth_source_conflict")
        os.replace(temporary, source)
        _fsync_directory(source.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _recover_locked(path, journal):
    """Both auth.lock files and the lifetime lease must already be held.

    This reads local files only. A durable pending candidate remains available
    even when replace succeeded and the following directory fsync failed.
    """
    source = Path(journal["source"])
    destination = Path(journal["destination"])
    root = Path(journal["snapshot_root"])
    changed = False
    while True:
        original, _, _ = _read_oauth(source)
        source_sha = hashlib.sha256(original).hexdigest()
        baseline_content, baseline, _ = _read_oauth(Path(journal["baseline_path"]))
        require(hashlib.sha256(baseline_content).hexdigest() == journal["baseline_sha256"], "oauth_snapshot_changed")
        _codex_identity(baseline)
        _refresh_time(baseline)
        if journal["state"] in {"commit_pending", "committed"}:
            content, candidate, _ = _read_oauth(Path(journal["candidate_path"]))
            require(hashlib.sha256(content).hexdigest() == journal["candidate_sha256"], "oauth_snapshot_changed")
            _validate_codex_refresh(baseline, candidate)
            candidate_sha = journal["candidate_sha256"]
            require(source_sha in {journal["baseline_sha256"], candidate_sha}, "oauth_source_conflict")
            if journal["state"] == "committed":
                require(source_sha == candidate_sha, "oauth_source_conflict")
            elif source_sha == journal["baseline_sha256"]:
                _replace_auth(source, content, journal["baseline_sha256"])
            else:
                # A former replace is ours: make its durability explicit before
                # marking committed. Never rewrite an unrelated newer cache.
                _fsync_directory(source.parent)
            if journal["state"] != "committed":
                journal.update(state="committed")
                _save_journal(path, journal)
            changed = True
            current, _, _ = _read_oauth(destination)
            if hashlib.sha256(current).hexdigest() == candidate_sha:
                return journal, changed
            baseline_path, baseline_sha = _snapshot(root, "baseline", content)
            journal.update(state="prepared", baseline_path=baseline_path, baseline_sha256=baseline_sha,
                           candidate_path=None, candidate_sha256=None)
            _save_journal(path, journal)
            continue
        require(source_sha == journal["baseline_sha256"], "oauth_source_conflict")
        content, candidate, _ = _read_oauth(destination)
        if hashlib.sha256(content).hexdigest() == journal["baseline_sha256"]:
            return journal, changed
        _journal_binary_unchanged(journal)
        _validate_codex_refresh(baseline, candidate)
        candidate_path, candidate_sha = _snapshot(root, "candidate", content)
        journal.update(state="commit_pending", candidate_path=candidate_path, candidate_sha256=candidate_sha)
        # Commit intent and immutable bytes are durable BEFORE the account file.
        _save_journal(path, journal)


def _recover_previous(path, source):
    if not path.exists():
        return False
    journal = _load_journal(path, source)
    if journal["closed"]:
        return False
    _require_previous_scope_empty(journal["scope_binding"])
    with _codex_commit_lock(source.parent), _codex_commit_lock(Path(journal["destination"]).parent):
        journal, changed = _recover_locked(path, journal)
        journal["closed"] = True
        _save_journal(path, journal)
        return changed


def recover_native_auth(source):
    """Local-only recovery after the former supervised scope is confirmed empty.

    Does not launch any CLI, log in, refresh over the network or retry inference.
    The return value contains no paths, token payloads, principals or digests.
    """
    result = {"passed": False, "codex_cache_synced": False, "conflict": False, "error_codes": []}
    descriptor = None
    try:
        require(source.is_absolute() and source.resolve() == source, "oauth_path_must_be_canonical")
        path = _journal_path(source)
        descriptor = _open_oauth_lock(path.parent / "lease.lock")
        result["codex_cache_synced"] = _recover_previous(path, source)
        result["passed"] = True
    except Exception as exc:
        code = str(exc) if isinstance(exc, ValidationError) else "oauth_cache_recovery_failed"
        result["error_codes"].append(code)
        result["conflict"] = code == "oauth_source_conflict"
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return result


def copy_oauth(source, destination, client, *, binary=None, binary_sha256=None, scope=None):
    """Codex-only seed with a durable journal and lease; never serialize row."""
    require(client == "codex", "claude_requires_official_shared_storage")
    scope_binding = _capture_oauth_scope(scope)
    private_directory(source.parent)
    private_directory(destination.parent)
    require(source.is_absolute() and source.resolve() == source and destination.is_absolute()
            and destination.resolve() == destination and source.parent != destination.parent,
            "oauth_path_must_be_canonical")
    path = _journal_path(source)
    descriptor = _open_oauth_lock(path.parent / "lease.lock")
    try:
        _recover_previous(path, source)
        root = _ensure_private_directory(destination.parent / ".devbrain-validation-auth")
        run_id = os.environ.get("DEVBRAIN_QUALIFICATION_RUN_ID", uuid4().hex)
        require(re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", run_id), "oauth_run_id_invalid")
        with _codex_commit_lock(source.parent):
            content, value, _ = _read_oauth(source)
            _codex_identity(value)
            _refresh_time(value)
            baseline_path, baseline_sha = _snapshot(root, "baseline", content)
            _write_private(destination, content)
            journal = {"schema_version": 1, "source": str(source), "destination": str(destination),
                       "snapshot_root": str(root), "run_id": run_id, "transaction_id": uuid4().hex,
                       "state": "prepared", "closed": False, "baseline_path": baseline_path,
                       "baseline_sha256": baseline_sha, "candidate_path": None, "candidate_sha256": None,
                       "binary_path": str(binary) if binary is not None else None, "binary_sha256": binary_sha256,
                       "scope_binding": scope_binding}
            _save_journal(path, journal)
        return {"client": client, "mode": "codex_copy_cas", "source": source, "destination": destination,
                "journal_path": path, "transaction_id": journal["transaction_id"], "run_lock": descriptor,
                "conflict": False, "commits": 0, "sync_ok": False}
    except BaseException:
        os.close(descriptor)
        raise


def prepare_native_auth(client, source, destination, binary, *, scope=None):
    if client == "codex":
        require(binary.is_absolute() and binary.resolve() == binary and binary.is_file(), "native_executable_missing")
        digest = sha(binary)
        return copy_oauth(source, destination, client, binary=binary, binary_sha256=digest, scope=scope)
    require(client == "claude" and source.name == ".credentials.json", "native_oauth_client_required")
    require(binary.is_absolute() and binary.resolve() == binary and sha(binary) == CLAUDE_SECURE_STORAGE_BINARY_SHA256,
            "claude_secure_storage_binary_not_pinned")
    _, value, _ = _read_oauth(source)
    _validate_claude_oauth(value)
    return {"client": client, "mode": "claude_official_storage", "source": source, "binary": binary}


def _validate_claude_oauth(value):
    tokens = value.get("claudeAiOauth", {})
    require(isinstance(tokens, dict) and all(isinstance(tokens.get(key), str) and tokens[key]
                                           and len(tokens[key]) <= 128 * 1024
                                           for key in ("accessToken", "refreshToken")), "saved_refreshable_oauth_required")


def native_auth_environment(rows):
    shared = [row for row in rows if row["mode"] == "claude_official_storage"]
    require(len(shared) <= 1, "duplicate_claude_storage")
    if not shared:
        return {}
    row = shared[0]
    require(sha(row["binary"]) == CLAUDE_SECURE_STORAGE_BINARY_SHA256, "claude_secure_storage_binary_changed")
    private_directory(row["source"].parent)
    return {"CLAUDE_SECURESTORAGE_CONFIG_DIR": str(row["source"].parent)}


def _sync_codex(row):
    require(not row["conflict"], "oauth_source_conflict")
    source, destination = row["source"], row["destination"]
    row["sync_ok"] = False
    descriptor = row.get("run_lock")
    require(descriptor is not None, "oauth_lease_not_held")
    info = os.fstat(descriptor)
    current = (row["journal_path"].parent / "lease.lock").lstat()
    require((info.st_dev, info.st_ino) == (current.st_dev, current.st_ino), "oauth_lock_changed")
    try:
        with _codex_commit_lock(source.parent), _codex_commit_lock(destination.parent):
            journal = _load_journal(row["journal_path"], source)
            require(journal["transaction_id"] == row["transaction_id"] and not journal["closed"], "oauth_journal_changed")
            _journal_binary_unchanged(journal)
            _, changed = _recover_locked(row["journal_path"], journal)
            row["commits"] += int(changed)
            row["sync_ok"] = True
            return changed
    except ValidationError as exc:
        row["conflict"] = str(exc) == "oauth_source_conflict"
        raise


def sync_native_auth(rows):
    """Safe verdict only. Run after each phase and independently on FAIL cleanup."""
    result = {"passed": True, "codex_cache_synced": False, "claude_store_reused": False,
              "conflict": False, "error_codes": []}
    for row in rows:
        try:
            if row["mode"] == "codex_copy_cas":
                _sync_codex(row)
                result["codex_cache_synced"] = row["commits"] > 0
            else:
                native_auth_environment([row])
                _, value, _ = _read_oauth(row["source"])
                _validate_claude_oauth(value)
                result["claude_store_reused"] = True
        except Exception as exc:
            result["passed"] = False
            result["conflict"] |= bool(row.get("conflict"))
            result["error_codes"].append(str(exc) if isinstance(exc, ValidationError) else "oauth_cache_sync_failed")
    return result


def release_native_auth(rows, *, close_journal=True):
    """Release every lease; callers must include this safe verdict in success."""
    result = {"passed": True, "error_codes": []}
    for row in rows:
        descriptor = row.pop("run_lock", None)
        if descriptor is not None:
            try:
                require(close_journal, "oauth_recovery_required")
                require(row.get("sync_ok"), "oauth_final_sync_required")
                journal = _load_journal(row["journal_path"], row["source"])
                require(journal["transaction_id"] == row["transaction_id"], "oauth_journal_changed")
                journal["closed"] = True
                _save_journal(row["journal_path"], journal)
            except Exception as exc:
                result["passed"] = False
                result["error_codes"].append(str(exc) if isinstance(exc, ValidationError) else "oauth_lease_finalize_failed")
            finally:
                try:
                    os.close(descriptor)
                except OSError:
                    result["passed"] = False
                    result["error_codes"].append("oauth_lease_release_failed")
    return result


def clean_environment(work, runtime, run_id, native_paths=()):
    # An allowlist prevents API keys, provider overrides, JVM tuning and inherited
    # invocation/session markers from becoming fallback authentication/configuration.
    result = {key: os.environ[key] for key in ("LANG", "LC_ALL", "LC_CTYPE", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR")
              if key in os.environ}
    result.update(HOME=str(work / "home"), XDG_CONFIG_HOME=str(work / "home/.config"),
                  XDG_CACHE_HOME=str(work / "home/.cache"), XDG_DATA_HOME=str(work / "home/.local/share"),
                  CODEX_HOME=str(work / "codex-auth"), CLAUDE_CONFIG_DIR=str(work / "claude-auth"),
                  TMPDIR=str(work / "tmp"), DEVBRAIN_HOME=str(work / "engine-home"),
                  DEVBRAIN_QUALIFICATION_RUN_ID=run_id,
                  DEVBRAIN_JAVA_EXECUTABLE=runtime["java_executable"],
                  DEVBRAIN_TMUX_EXECUTABLE=runtime["tmux_executable"],
                  JAVA_HOME=str(Path(runtime["java_executable"]).parent.parent),
                  PATH=os.pathsep.join([str(work / "bin"), str(Path(runtime["java_executable"]).parent),
                                        str(Path(runtime["tmux_executable"]).parent), str(Path(sys.executable).parent),
                                        *dict.fromkeys(str(Path(path).parent) for path in native_paths),
                                        str(Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local/bin"),
                                        "/usr/local/bin", "/usr/bin", "/bin"]))
    return result


class OwnedEngine:
    """One fresh fixture JVM, retained Popen + pidfd; never numeric PID signals."""
    def __init__(self, settings, distribution, runtime, scope, supervision, work):
        self.settings, self.runtime, self.scope, self.supervision = settings, runtime, scope, supervision
        self.process, self.row, self.handle, self.log = None, None, None, None
        self.distribution, self.work = distribution, work
        self.report = {"started": False, "actual_exit_verified": False, "forced_cleanup": False}

    def start(self):
        from devbrain.engine import install_engine
        from devbrain.arcade import ArcadeClient
        require((self.distribution / "lib").is_dir(), "pinned_arcade_distribution_required")
        home = install_engine(self.settings, self.distribution)
        state = private_directory(self.settings.data_dir / "arcade", create=True)
        for name in ("config", "databases", "backups", "log"):
            private_directory(state / name, create=True)
        for source in (home / "config").glob("*"):
            if source.is_file():
                shutil.copyfile(source, state / "config" / source.name)
        password = state / "root-password"
        with os.fdopen(os.open(password, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
            output.write(self.settings.arcade_password)
        port = self.settings.arcade_url.rsplit(":", 1)[1]
        command = [self.runtime["java_executable"], "-Xms64m", "-Xmx1024m", "--add-modules=jdk.incubator.vector",
                   "--enable-native-access=ALL-UNNAMED", "-Djava.awt.headless=true", "-Dfile.encoding=UTF8",
                   "-Darcadedb.profile=low-ram", f"-Darcadedb.server.rootPath={state}",
                   f"-Darcadedb.server.configDirectory={state / 'config'}",
                   f"-Darcadedb.server.databaseDirectory={state / 'databases'}",
                   f"-Darcadedb.server.backupDirectory={state / 'backups'}",
                   f"-Darcadedb.server.logsDirectory={state / 'log'}",
                   f"-Darcadedb.server.rootPasswordPath={password}", "-Darcadedb.server.httpIncomingHost=127.0.0.1",
                   f"-Darcadedb.server.httpIncomingPort={port}",
                   f"-Djava.util.logging.config.file={state / 'config/arcadedb-log.properties'}",
                   "-cp", str(home / "lib/*"), "com.arcadedb.server.ArcadeDBServer"]
        self.log = (self.work / "engine.log").open("xb")
        os.chmod(self.work / "engine.log", 0o600)
        self.process = subprocess.Popen(command, cwd=state, stdin=subprocess.DEVNULL,
                                        stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        self.report["started"] = True
        self.row = self.supervision.identity(self.process.pid)
        require(self.row and self.row["executable"] == self.runtime["java_executable"]
                and self.row["parent_pid"] == os.getpid() and self.scope.owns(self.row), "engine_identity_not_owned")
        self.handle = os.pidfd_open(self.process.pid)
        require(self.scope.owns(self.row), "engine_identity_changed")
        client = ArcadeClient(self.settings)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline and self.process.poll() is None:
            try:
                info = client.server_info()
                if self.settings.arcade_version in str(info):
                    self.report["ready"] = True
                    return
            except Exception:
                pass  # No response bodies, URLs or credentials enter evidence.
            time.sleep(.2)
        raise ValidationError("fixture_engine_not_ready")

    def stop(self):
        if self.process is None:
            self.report.update(actual_exit_verified=True, cleanup_complete=True)
            return True
        try:
            self.report["unexpected_exit_before_shutdown"] = self.process.poll() is not None
            if self.process.poll() is None:
                require(self.handle is not None and self.row is not None and self.scope.owns(self.row)
                        and self.row["executable"] == self.runtime["java_executable"], "engine_stop_identity_not_owned")
                signal.pidfd_send_signal(self.handle, signal.SIGTERM)
                self.report["graceful_shutdown_requested"] = True
                try:
                    self.process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    self.report["forced_cleanup"] = True
                    require(self.scope.owns(self.row), "engine_force_stop_identity_changed")
                    signal.pidfd_send_signal(self.handle, signal.SIGKILL)
                    self.process.wait(timeout=5)
            code = self.process.wait(timeout=1)
            exited = self.handle is not None and bool(select.select([self.handle], [], [], 0)[0])
            self.report.update(returncode=code, actual_exit_verified=exited,
                               cleanup_complete=exited and not self.report["forced_cleanup"]
                               and self.report.get("graceful_shutdown_requested") is True
                               and not self.report["unexpected_exit_before_shutdown"] and code in (0, 143, -15))
            return self.report["cleanup_complete"]
        finally:
            if self.handle is not None:
                os.close(self.handle)
                self.handle = None
            if self.log is not None:
                self.log.close()


def quiet_scope(scope, supervision, parent, seconds=30):
    """Observe, do not kill: outer supervisor owns cleanup of unexpected children."""
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        require(supervision.same_process(parent) and os.getppid() == parent["pid"], "supervisor_parent_changed")
        for row in scope.members(exclude={parent["pid"]}):
            if row["parent_pid"] == parent["pid"] and row["executable"] == str(Path("/usr/bin/dmesg").resolve()):
                try:
                    command = (Path("/proc") / str(row["pid"]) / "cmdline").read_bytes()
                except FileNotFoundError:
                    continue
                if command == b"/usr/bin/dmesg\0--raw\0" and scope.owns(row):
                    continue
            error = ValidationError("owned_descendant_remains")
            snapshot = {"scope_unit": scope.unit, "control_group": scope.group,
                        "scope_cgroup_identity": scope.inode, "processes": [],
                        "capture_status": "identity_changed"}
            try:
                if scope.owns(row):
                    group = supervision.process_cgroup(row["pid"])
                    if (group and (group == scope.group or group.startswith(scope.group + "/"))
                            and scope.owns(row)):
                        process = {key: row[key] for key in
                                   ("pid", "birth", "state", "uid", "parent_pid", "executable")}
                        process["control_group"] = group
                        snapshot["processes"] = [process]
                        snapshot["capture_status"] = "verified_owned"
            except (OSError, RuntimeError, ValueError, KeyError):
                snapshot["capture_status"] = "unavailable"
            error.owned_descendant_snapshot = snapshot
            raise error
        time.sleep(.2)
    return round(time.monotonic() - started, 3)


def native_passed(results):
    return (len(results) == 3 and [row.get("client") for row in results] == ["codex", "claude", "codex"]
            and all(row.get("status") == "passed" and row.get("fresh_process") is True for row in results)
            and results[1].get("previous_ack_invalidated") is True
            and results[2].get("previous_ack_invalidated") is True
            and results[2].get("compaction_client") == "claude")


def native_workload(settings, work, artifact_root, supervision, auth_rows):
    from devbrain.evaluation import _clients, _fixture_settings, _git, _native_client, _seed
    from devbrain.store import Store
    project_root, other = work / "project", work / "other"
    private_directory(project_root, create=True)
    private_directory(other, create=True)
    _git(project_root, "init", "--quiet")
    (project_root / "README.md").write_text("Synthetic native account recall fixture.\n")
    _git(project_root, "add", "README.md")
    _git(project_root, "-c", "user.name=DevBrain Evaluation", "-c", "user.email=eval@localhost",
         "commit", "--quiet", "-m", "Isolated recall fixture")
    isolated = _fixture_settings(settings, work / "native-data")
    store = None
    result = {"scope": "real_native_clients_only", "status": "running", "phases": [], "owned_database_removed": False}
    def runner(client, *args, **kwargs):
        try:
            raw = _native_client(client, *args, **kwargs)
        finally:
            # subprocess.run inside the real evaluator waits for the native CLI
            # on both success and timeout. A failed model turn can still refresh.
            sync = sync_native_auth(auth_rows)
            result.setdefault("oauth_phase_sync", []).append(sync)
            supervision.atomic_json(artifact_root / "native-results.json", result)
            require(sync["passed"], "native_phase_oauth_preservation_failed")
        # Progress contains only allowlisted metadata, never model/account text.
        result["phases"].append({"client": client, "status": raw.get("status"),
                                 "process_started": raw.get("process_started") is True})
        supervision.atomic_json(artifact_root / "native-results.json", result)
        return raw
    try:
        store = Store(isolated.data_dir, settings=isolated)
        project, _, canonical = _seed(store, project_root, other)
        results = _clients(store, isolated, project, canonical, runner)
        passed = native_passed(results)
        result.update(status="passed" if passed else "failed", exact_packet_ack_verified=passed,
                      previous_ack_invalidated=[row.get("previous_ack_invalidated") for row in results],
                      phases=[{"phase": row["phase"], "client": row["client"], "status": row["status"],
                               "fresh_process": row["fresh_process"],
                               "answer_matches_expected": row.get("answer") == row.get("expected") and row.get("status") == "passed"}
                              for row in results],
                      real_native_compaction_tested=False, synthetic_precompact_event=True,
                      local_model_inference=False)
    finally:
        # Construction can fail after database creation: cleanup by the original
        # generated namespace, never a name supplied by native/model output.
        from devbrain.arcade import ArcadeClient
        client = store.client if store is not None else ArcadeClient(isolated)
        require(isolated.arcade_database.startswith("devbrain_eval_"), "unexpected_fixture_namespace")
        exists = client._request("GET", "/api/v1/exists/" + isolated.arcade_database).json().get("result")
        if exists is True:
            client.server_command("drop database " + isolated.arcade_database)
        require(exists in (True, False), "fixture_database_state_unknown")
        result["owned_database_removed"] = True
        supervision.atomic_json(artifact_root / "native-results.json", result)
    require(result.get("status") == "passed", "native_three_phase_validation_failed")
    return result


def copilot_workload(args, work, artifact_root, supervision):
    helper = Path(__file__).with_name("validate-copilot-account-e2e.py").resolve()
    original_hash = sha(helper)
    raw_report = work / "copilot-private-results.json"
    with (work / "copilot-private.log").open("xb") as output:
        os.chmod(work / "copilot-private.log", 0o600)
        process = subprocess.Popen([sys.executable, "-I", "-B", str(helper), "--auth-home", str(args.auth_home),
                                    "--engine-home", str(work / "engine-home"), "--work-root", str(work / "copilot-work"),
                                    "--report", str(raw_report)], stdout=output, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL)
        # Outer scope supervision owns timeouts and orphan cleanup; no numeric kill.
        code = process.wait(timeout=180)
    require(sha(helper) == original_hash, "copilot_helper_changed")
    value = supervision.read_json(raw_report)
    fields = ("actual_model_ack_verified", "file_read_verified", "protocol_order_verified", "project_unchanged",
              "response_contains_marker", "only_owned_database_removed", "shared_engine_kept_running")
    result = {"scope": "real_copilot_account_only", "status": "failed", "helper_returncode": code,
              "helper_sha256": original_hash, "oauth_store_reused": True, "oauth_cache_refresh_allowed": True,
              "api_key_provided": False, "local_model_inference": False,
              **{key: value.get(key) is True for key in fields}}
    result["status"] = "passed" if (code == 0 and value.get("status") == "passed" and all(result[key] for key in fields)
        and value.get("action_events") == 0 and value.get("pending_actions") == 0) else "failed"
    supervision.atomic_json(artifact_root / "copilot-results.json", result)
    require(result["status"] == "passed", "copilot_account_validation_failed")
    return result


def main(mode="native"):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--arcade-dist", type=Path, required=True)
    if mode == "native":
        for flag in ("codex-bin", "claude-bin", "codex-auth", "claude-auth"):
            parser.add_argument("--" + flag, type=Path, required=True)
    else:
        parser.add_argument("--auth-home", type=Path, required=True)
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({"status": "not_run", "execute": False, "reason": "Requires passed gate and external supervision"}))
        return 0
    supervision, guard = module("supervise-080"), module("attest-full-suite-080")
    root = args.artifact_root.resolve(strict=True)
    require(root == args.artifact_root and root.is_relative_to(Path("/mnt/c")), "mapped_artifact_root_required")
    require(not (root / "controller-report.json").exists(), "fresh_artifact_root_required")
    runtime = supervision.qualified_runtime(supervision.read_json(root / "gate.json"))
    require(sys.flags.isolated == 1 and sys.prefix != sys.base_prefix, "literal_qualified_venv_isolated_python_required")
    require(os.environ.get("DEVBRAIN_QUALIFICATION_RUN_ID") == args.run_id, "external_supervision_required")
    proof = supervision.read_json(root / "mapping-proof.json")
    require(proof.get("run_id") == args.run_id, "scope_proof_run_id_mismatch")
    scope = supervision.ScopeOwnership(proof.get("scope_unit"))
    parent = supervision.identity(os.getppid())
    require(parent is not None and scope.owns(parent), "supervisor_parent_required")
    source = args.source_root.resolve(strict=True)
    origin = importlib.util.find_spec("devbrain")
    require(origin and Path(origin.origin).resolve() == source / "src/devbrain/__init__.py"
            and importlib.metadata.version("devbrain") == "0.8.0", "installed_frozen_devbrain080_required")
    source_hashes = guard.source_manifest(source)
    report = {"schema_version": 1, "scope": f"devbrain_{mode}_account_validation", "run_id": args.run_id,
              "status": "running", "runtime": runtime, "python_launcher": sys.executable,
              "runtime_gate_sha256": sha(root / "gate.json"), "source_hashes": source_hashes,
              "cleanup_complete": False, "controller_exit_verified": False, "forced_cleanup": False,
              "workload_criteria_passed": False, "errors": [], "private_oauth_contents_exported": False}
    supervision.atomic_json(root / "controller-report.json", report)
    engine, copies, binaries, work = None, [], {}, None
    previous_environment = dict(os.environ)
    old_umask = os.umask(0o077)
    try:
        private_parent = private_directory(args.private_root.resolve(strict=True))
        cache = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".cache"
        require(private_parent == args.private_root and private_parent.is_relative_to(cache), "private_linux_cache_root_required")
        work = private_directory(private_parent / (mode + "-" + uuid4().hex), create=True)
        report["private_work_root"] = str(work)
        for name in ("home", "codex-auth", "claude-auth", "tmp", "bin", "engine-home"):
            private_directory(work / name, create=True)
        if mode == "native":
            for client in ("codex", "claude"):
                binary = getattr(args, client + "_bin").resolve(strict=True)
                require(binary.is_file() and os.access(binary, os.X_OK), "native_executable_missing")
                binaries[str(binary)] = sha(binary)
                (work / "bin" / client).symlink_to(binary)
                filename = "auth.json" if client == "codex" else ".credentials.json"
                copies.append(prepare_native_auth(client, getattr(args, client + "_auth"),
                                                  work / (client + "-auth") / filename, binary, scope=scope))
            report["native_binary_sha256"] = binaries
            report["oauth_copy_policy"] = "codex_private_copy_checked_cas_claude_official_shared_secure_storage"
        else:
            require(args.auth_home.is_absolute() and args.auth_home.resolve() == args.auth_home
                    and not args.auth_home.is_relative_to(private_parent), "existing_separate_copilot_account_home_required")
            require((args.auth_home / "runtime/copilot").is_dir()
                    and (args.auth_home / "runtime/copilot-runtime/receipt.json").is_file(), "existing_copilot_runtime_required")
            report.update(oauth_store_reused=True, oauth_cache_refresh_allowed=True,
                          production_database_opened=False, production_profiles_modified=False)
        environment = clean_environment(work, runtime, args.run_id, binaries)
        os.environ.clear()
        os.environ.update(environment)
        os.environ.update(native_auth_environment(copies))
        from devbrain.config import Settings
        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            port = port_socket.getsockname()[1]
        settings = Settings(data_dir=work / "engine-home", arcade_url=f"http://127.0.0.1:{port}",
                            arcade_password=secrets.token_urlsafe(32), token=secrets.token_urlsafe(32),
                            arcade_database="devbrain_account_" + uuid4().hex,
                            java_executable=runtime["java_executable"], tmux_executable=runtime["tmux_executable"],
                            ollama_url="http://127.0.0.1:1")
        settings.save()
        engine = OwnedEngine(settings, args.arcade_dist.resolve(strict=True), runtime, scope, supervision, work)
        engine.start()
        if mode == "native":
            native_workload(settings, work, root, supervision, copies)
        else:
            copilot_workload(args, work, root, supervision)
        report["workload_criteria_passed"] = True
    except Exception as exc:
        report["errors"].append(str(exc) if isinstance(exc, ValidationError) else type(exc).__name__)
    finally:
        try:
            stopped = engine.stop() if engine is not None else True
            report["engine"] = engine.report if engine is not None else {"started": False, "cleanup_complete": True}
            report["forced_cleanup"] = bool(report["engine"].get("forced_cleanup"))
            require(stopped, "owned_engine_cleanup_failed")
            report["cleanup_grace_seconds"] = quiet_scope(scope, supervision, parent)
            report["cleanup_complete"] = True
            report["source_unchanged"] = source_hashes == guard.source_manifest(source)
            require(report["source_unchanged"], "frozen_source_changed")
            require(supervision.qualified_runtime(supervision.read_json(root / "gate.json")) == runtime
                    and sha(root / "gate.json") == report["runtime_gate_sha256"], "qualified_runtime_or_gate_changed")
            require(all(sha(Path(path)) == expected for path, expected in binaries.items()), "native_binary_changed")
        except Exception as exc:
            report["errors"].append(str(exc) if isinstance(exc, ValidationError) else type(exc).__name__)
        # Independent of model, database and process-cleanup success. Preserve
        # an official refresh even when another validation step already failed.
        try:
            if copies:
                report["oauth_cache_sync"] = sync_native_auth(copies)
                require(report["oauth_cache_sync"]["passed"], "native_final_oauth_preservation_failed")
        except Exception as exc:
            report["errors"].append(str(exc) if isinstance(exc, ValidationError) else "oauth_cache_sync_failed")
        finally:
            report["oauth_lease_release"] = release_native_auth(copies, close_journal=report["cleanup_complete"])
            if not report["oauth_lease_release"]["passed"]:
                report["errors"].append("native_oauth_lease_finalize_failed")
        os.environ.clear()
        os.environ.update(previous_environment)
        os.umask(old_umask)
        report["status"] = (f"{mode}_account_completed_pending_attestation" if report["workload_criteria_passed"]
                            and report["cleanup_complete"] and not report["forced_cleanup"] and not report["errors"] else "failed")
        supervision.atomic_json(root / "controller-report.json", report)
    return 0 if report["status"].endswith("_completed_pending_attestation") else 1


if __name__ == "__main__":
    raise SystemExit(main())
