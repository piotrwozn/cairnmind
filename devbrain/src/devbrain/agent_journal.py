"""Durable owner-local API/Copilot history, with finite hash-checked events.

The journal records observations and action outcomes. It never grants tool
permissions, restores an ACK, or interprets unfinished actions as retryable.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import uuid4


MAX_EVENT_BYTES = 4 * 1024 * 1024
MAX_JOURNAL_BYTES = 64 * 1024 * 1024
ZERO_HASH = "0" * 64
_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_TYPE = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")
_SENSITIVE_FIELDS = {"api_key", "apikey", "access_token", "refresh_token", "authorization",
                     "proxy_authorization", "password", "secret", "credential", "credentials",
                     "private_key", "cookie", "set_cookie", "headers"}


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
                      allow_nan=False).encode("utf-8")


class JournalError(ValueError):
    """The journal cannot be trusted as complete durable history."""


def _private_dir(path: Path):
    path.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise JournalError("Agent journal directories must be private and owned by this user")


class AgentJournal:
    def __init__(self, settings, session_id: str):
        if not isinstance(session_id, str) or not _ID.fullmatch(session_id):
            raise JournalError("Invalid agent session ID")
        self.settings, self.session_id = settings, session_id
        self.directory = Path(settings.data_dir) / "runtime" / "agents" / session_id
        self.path = self.directory / "events.jsonl"
        self.lock_path = self.directory / "events.lock"
        self._secrets: set[str] = set()
        self.add_secrets(getattr(settings, "token", None), getattr(settings, "arcade_password", None))

    def add_secrets(self, *values):
        """Register in-memory credentials before recording any corresponding data."""
        self._secrets.update(value for value in values if isinstance(value, str) and value)

    def redact(self, value):
        if isinstance(value, str):
            for secret in sorted(self._secrets, key=len, reverse=True):
                value = value.replace(secret, "[REDACTED]")
                # A bounded tool result may end in a truncated credential.
                for length in range(min(len(secret) - 1, len(value)), 5, -1):
                    if value.endswith(secret[:length]):
                        value = value[:-length] + "[REDACTED]"
                        break
            return value
        if isinstance(value, dict):
            return {str(key): ("[REDACTED]" if str(key).casefold().replace("-", "_") in _SENSITIVE_FIELDS
                               else self.redact(item)) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(item) for item in value]
        if value is None or type(value) in (int, float, bool):
            return value
        raise JournalError("Agent journal accepts only JSON values")

    def _initialize(self):
        root = Path(self.settings.data_dir)
        if not root.exists():
            root.mkdir(parents=True, mode=0o700)
        if root.is_symlink():
            raise JournalError("Agent data directory cannot be a symbolic link")
        for path in (root / "runtime", root / "runtime" / "agents", self.directory):
            _private_dir(path)

    @contextmanager
    def lock(self):
        self._initialize()
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise JournalError("Agent journal lock must be private")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _read_locked(self) -> list[dict]:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        except FileNotFoundError:
            return []
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > MAX_JOURNAL_BYTES):
                raise JournalError("Agent journal is not a private bounded regular file")
            raw = handle.read(MAX_JOURNAL_BYTES + 1)
        if len(raw) > MAX_JOURNAL_BYTES or raw and not raw.endswith(b"\n"):
            raise JournalError("Agent journal contains an incomplete or oversized tail")
        events, previous = [], ZERO_HASH
        for number, line in enumerate(raw.splitlines(), 1):
            if not line or len(line) + 1 > MAX_EVENT_BYTES:
                raise JournalError("Agent journal event exceeds its byte budget")
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or set(event) != {
                    "version", "id", "session_id", "seq", "created_at", "type", "data", "previous_hash", "hash"
                }:
                    raise ValueError()
                claimed = event["hash"]
                envelope = {key: value for key, value in event.items() if key != "hash"}
                if (type(event["version"]) is not int or event["version"] != 1
                        or type(event["seq"]) is not int or event["seq"] != number
                        or event["session_id"] != self.session_id or event["previous_hash"] != previous
                        or not isinstance(event["id"], str) or not _ID.fullmatch(event["id"])
                        or not isinstance(event["type"], str) or not _TYPE.fullmatch(event["type"])
                        or not isinstance(event["created_at"], str)
                        or claimed != hashlib.sha256(canonical(envelope)).hexdigest()
                        or line != canonical(event)):
                    raise ValueError()
            except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
                raise JournalError("Agent journal hash chain or schema is invalid") from None
            events.append(event)
            previous = claimed
        return events

    def events(self) -> list[dict]:
        """Read complete canonical events; a corrupt tail is never silently ignored."""
        with self.lock():
            return self._read_locked()

    def append(self, event_type: str, data) -> dict:
        if not isinstance(event_type, str) or not _TYPE.fullmatch(event_type):
            raise JournalError("Invalid agent event type")
        clean = self.redact(data)
        try:
            canonical(clean)
        except (ValueError, TypeError, RecursionError, OverflowError):
            raise JournalError("Agent event must be finite JSON") from None
        with self.lock():
            events = self._read_locked()
            event = {"version": 1, "id": uuid4().hex, "session_id": self.session_id, "seq": len(events) + 1,
                     "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                     "type": event_type, "data": clean,
                     "previous_hash": events[-1]["hash"] if events else ZERO_HASH}
            event["hash"] = hashlib.sha256(canonical(event)).hexdigest()
            raw = canonical(event) + b"\n"
            if len(raw) > MAX_EVENT_BYTES:
                raise JournalError("Agent event exceeds its byte budget")
            fd = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW
                         | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size + len(raw) > MAX_JOURNAL_BYTES):
                    raise JournalError("Agent journal is not private or its byte budget is exhausted")
                remaining = memoryview(raw)
                while remaining:
                    written = os.write(fd, remaining)
                    if written <= 0:
                        raise OSError("Agent journal append did not complete")
                    remaining = remaining[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            directory = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return event

    def messages(self) -> list[dict]:
        return [deepcopy(event["data"]) for event in self.events()
                if event["type"] == "message" and isinstance(event["data"], dict)]

    def pending_actions(self) -> list[dict]:
        pending = {}
        for event in self.events():
            data = event.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("action_id"), str):
                continue
            action_id = data["action_id"]
            if event["type"] == "action_started":
                pending[action_id] = {**event, "last_state": "action_started"}
            elif event["type"] in {"action_completed", "action_resolved"}:
                pending.pop(action_id, None)
            elif event["type"] == "action_interrupted" and action_id in pending:
                pending[action_id]["last_state"] = "action_interrupted"
        return list(pending.values())
