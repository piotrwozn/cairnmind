"""Local credentials, separate from project memory and public profile metadata."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from cairnmind.agents.api_profiles import ENV_NAME, PROFILE_ID
from cairnmind.pal.file_locks import lock_exclusive
from cairnmind.pal.private_files import (
    atomic_replace,
    ensure_private_dir,
    fsync_directory,
    is_private_stat,
    is_reparse_point,
    make_private,
    open_no_follow,
)


def credential_environment_names() -> tuple[str, ...]:
    """Known credential variables to exclude from child command environments."""
    return ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN",
            "CODEX_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_ADMIN_KEY", "AZURE_OPENAI_API_KEY")


class CredentialStore:
    """Ephemeral keys live in this broker instance; persisted keys are opt-in.

    ``status`` never returns key text and does not create files. ``resolve`` is
    a private worker operation, not a public profile or UI endpoint.
    """
    def __init__(self, settings, *, environ=None):
        self.root = Path(settings.data_dir) / "credentials"
        self.path = self.root / "api-keys.json"
        self._ephemeral = {}
        self._mutex = threading.RLock()
        self._environ = os.environ if environ is None else environ

    @staticmethod
    def _id(value):
        if not isinstance(value, str) or not PROFILE_ID.fullmatch(value):
            raise ValueError("Invalid credential identifier.")
        return value

    @staticmethod
    def _key(value):
        if (not isinstance(value, str) or not value or len(value) > 16384
                or any(ord(c) < 33 or ord(c) > 126 for c in value)):
            raise ValueError("Invalid API key.")
        return value

    def _directory(self, create=False):
        if create:
            ensure_private_dir(self.root, error="The credential store must be a private folder of the user.")
            return True
        if not self.root.exists() and not is_reparse_point(self.root):
            return False
        info = self.root.lstat()
        if not is_private_stat(info, self.root, directory=True):
            raise ValueError("The credential store must be a private folder of the user.")
        return True

    @contextmanager
    def _lock(self):
        self._directory(create=True)
        lock_path = self.root / "keys.lock"
        fd = open_no_follow(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            info = os.fstat(fd)
            if not is_private_stat(info, lock_path, directory=False):
                raise ValueError("The credential lock must be private.")
            lock_exclusive(fd)
            yield
        finally:
            os.close(fd)

    def _read(self):
        if not self._directory():
            return {}
        try:
            fd = open_no_follow(self.path, os.O_RDONLY, private_on_create=False)
        except FileNotFoundError:
            return {}
        except OSError:
            raise ValueError("The credential store cannot be read safely.") from None
        with os.fdopen(fd, encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if not is_private_stat(info, self.path, directory=False) or info.st_size > 1024 * 1024:
                raise ValueError("The credential file must be a private file of the user.")
            try:
                value = json.load(handle)
            except (ValueError, UnicodeError):
                raise ValueError("Invalid credential store.") from None
        if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("keys"), dict):
            raise ValueError("Unsupported credential store.")
        for name, key in value["keys"].items():
            self._id(name)
            self._key(key)
        return value["keys"]

    def _write(self, keys):
        raw = json.dumps({"version": 1, "keys": keys}, sort_keys=True, separators=(",", ":"))
        if len(raw.encode("utf-8")) > 1024 * 1024:
            raise ValueError("The credential store exceeds its size limit.")
        fd, temporary = tempfile.mkstemp(prefix=".api-keys-", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            make_private(temporary)
            atomic_replace(temporary, self.path)
            fsync_directory(self.root)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def set(self, profile_id, key, persist=False):
        profile_id, key = self._id(profile_id), self._key(key)
        if type(persist) is not bool:
            raise ValueError("Choose how the key is stored.")
        with self._mutex:
            if persist:
                with self._lock():
                    keys = self._read()
                    keys[profile_id] = key
                    self._write(keys)
                self._ephemeral.pop(profile_id, None)
            else:
                # Explicitly replacing a saved key with a session key must not
                # resurrect the old saved key after a broker restart.
                if self.path.exists() or self.path.is_symlink():
                    with self._lock():
                        keys = self._read()
                        if profile_id in keys:
                            keys.pop(profile_id)
                            self._write(keys)
                self._ephemeral[profile_id] = key
        return {"configured": True, "storage": "local" if persist else "session", "code": "configured_unchecked"}

    def delete(self, profile_id):
        profile_id = self._id(profile_id)
        with self._mutex:
            self._ephemeral.pop(profile_id, None)
            if self.path.exists() or self.path.is_symlink():
                with self._lock():
                    keys = self._read()
                    if profile_id in keys:
                        keys.pop(profile_id)
                        self._write(keys)
        return {"configured": False, "storage": "none", "code": "credential_missing"}

    @staticmethod
    def _reference(profile):
        if not isinstance(profile, dict):
            raise ValueError("Invalid credential profile.")
        reference = profile.get("credential_ref")
        if reference is None:
            return None, None
        if not isinstance(reference, str):
            raise ValueError("Invalid credential reference.")
        kind, _, name = reference.partition(":")
        if kind == "env" and ENV_NAME.fullmatch(name):
            return kind, name
        if kind == "profile" and PROFILE_ID.fullmatch(name):
            return kind, name
        raise ValueError("Invalid credential reference.")

    def registered_values(self):
        """Every key this instance holds: the saved ones, and the session ones set on this same instance.

        The secrets gate (#171) builds a new instance in the memory service, so it sees the saved keys only;
        a key entered for one session lives in the cockpit's own instance and is not registered there."""
        with self._mutex:
            return [*self._read().values(), *self._ephemeral.values()]

    def resolve(self, profile):
        kind, name = self._reference(profile)
        with self._mutex:
            if kind is None or name is None:
                return None
            if kind == "env":
                value = self._environ.get(name)
                return self._key(value) if value else None
            if name in self._ephemeral:
                return self._ephemeral[name]
            return self._read().get(name)

    def status(self, profile):
        kind, name = self._reference(profile)
        with self._mutex:
            if kind is None or name is None:
                return {"configured": True, "storage": "none", "code": "not_required"}
            if kind == "env":
                value = self._environ.get(name)
                configured = bool(value)
                storage = "environment" if configured else "none"
                if configured:
                    # The same check resolve() applies, so a status never says "configured" for a
                    # value the launcher refuses. The value is neither returned nor logged.
                    try:
                        self._key(value)
                    except ValueError:
                        return {"configured": False, "storage": storage, "code": "credential_invalid"}
            else:
                configured = name in self._ephemeral
                storage = "session" if configured else "local" if name in self._read() else "none"
                configured = storage != "none"
            return {"configured": configured, "storage": storage,
                    "code": "configured_unchecked" if configured else "credential_missing"}

    def clear_session(self):
        with self._mutex:
            self._ephemeral.clear()
