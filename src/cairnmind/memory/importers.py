"""External transcript converter registry, normalised JSONL v1 format, and CLI runner (#127).

Cairnmind never runs third-party code inside its own processes or service workers. A person registers a
converter command on their machine in ``<data_dir>/importers.json`` and invokes ``cairnmind import PATH --via NAME``
from the command line outside any coding session. Converted sources are stamped with the importer's name and
``origin_evidence="degraded"`` so they are searchable without ever anchoring records or altering verified resume.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from cairnmind.config import Settings, env_names
from cairnmind.mcp.mcp_host import INHERITED
from cairnmind.memory.secret_mask import ingest_masker, masked_metadata
from cairnmind.pal.file_locks import lock_exclusive, unlock
from cairnmind.pal.path_spelling import lies_in
from cairnmind.pal.private_files import open_no_follow, write_durable_private
from cairnmind.pal.process_control import ProcessJob, group_popen_kwargs, terminate_group
from cairnmind.scip.ingest import _identifier, _now, _source_project, _write_chunks
from cairnmind.storage.storage_query import db_upsert_document
from cairnmind.storage.store_writer import require_writer

log = logging.getLogger(__name__)

IMPORTERS_FILENAME = "importers.json"
IMPORTERS_LOCK = "importers.lock"
REGISTRY_VERSION = 1
CONVERTED_FORMAT_VERSION = 1

MAX_IMPORTERS = 64
MAX_ARGS = 32
MAX_TEXT = 4096
DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 3600
DEFAULT_OUTPUT_BYTES = 10 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_LINE_BYTES = 2 * 1024 * 1024

IMPORTER_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
RESERVED_NAMES = frozenset({"auto", "codex", "claude", "external", "unknown", "api", "copilot"})
ALLOWED_ROLES = frozenset({"user", "assistant", "system", "tool"})
ALLOWED_EVENT_KEYS = frozenset({"version", "role", "content", "session_id", "timestamp", "cwd", "event_id"})
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

IN_SESSION_REFUSAL = (
    "External transcript converters are managed and run only by a person outside a coding session "
    "(unset CAIRNMIND_SESSION_ID and CAIRNMIND_PACKET_ID)."
)


class ImporterRefused(ValueError):
    """Raised when an importer operation is refused by policy or session boundaries."""


class ImporterExecutionError(ValueError):
    """Raised when running a converter subprocess fails or exceeds its bounds."""


def require_outside_session(environ=None) -> None:
    """Refuse when invoked inside a launched or bound coding session."""
    env = os.environ if environ is None else environ
    for name in ("SESSION_ID", "PACKET_ID"):
        for key in env_names(name):
            if env.get(key):
                raise ImporterRefused(IN_SESSION_REFUSAL)


def _valid_text(value: Any, *, limit: int = MAX_TEXT) -> bool:
    if not isinstance(value, str) or not value or len(value) > limit or "\0" in value or _CONTROL.search(value):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _is_absolute_command(command: str) -> bool:
    if ".." in re.split(r"[\\/]", command):
        return False
    if command.startswith("/"):
        return not command.startswith("//")
    if sys.platform == "win32":
        return bool(re.match(r"^[A-Za-z]:[\\/]", command))
    return False


def validate_name(name: Any) -> str:
    if not isinstance(name, str) or not IMPORTER_NAME.fullmatch(name):
        raise ValueError("Importer name must be 1 to 64 lowercase letters, digits, hyphens or underscores.")
    if name in RESERVED_NAMES:
        raise ValueError(f"Importer name '{name}' is reserved for built-in clients.")
    return name


def validate_importer(
    name: Any,
    argv: Any,
    *,
    timeout_seconds: Any = DEFAULT_TIMEOUT_SECONDS,
    output_limit_bytes: Any = DEFAULT_OUTPUT_BYTES,
) -> dict[str, Any]:
    """Validate one registered converter definition before saving or running it."""
    clean_name = validate_name(name)
    if not isinstance(argv, list) or not 1 <= len(argv) <= MAX_ARGS or any(not _valid_text(part) for part in argv):
        raise ValueError("Importer command must be 1 to 32 nonempty strings of at most 4096 characters.")
    if not _is_absolute_command(argv[0]):
        raise ValueError("Importer command must start with an absolute executable path.")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be an integer between 1 and {MAX_TIMEOUT_SECONDS}.")
    if type(output_limit_bytes) is not int or not 1 <= output_limit_bytes <= MAX_OUTPUT_BYTES:
        raise ValueError(f"output_limit_bytes must be an integer between 1 and {MAX_OUTPUT_BYTES}.")
    return {
        "name": clean_name,
        "argv": list(argv),
        "timeout_seconds": timeout_seconds,
        "output_limit_bytes": output_limit_bytes,
    }


@contextmanager
def _registry_lock(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = data_dir / IMPORTERS_LOCK
    fd = open_no_follow(lock_path, os.O_CREAT | os.O_RDWR, 0o600, private_on_create=True)
    try:
        lock_exclusive(fd, blocking=True)
        yield
    finally:
        unlock(fd)
        os.close(fd)


def _registry_path(data_dir: Path) -> Path:
    return Path(data_dir) / IMPORTERS_FILENAME


def _read_registry(data_dir: Path) -> dict[str, dict[str, Any]]:
    path = _registry_path(data_dir)
    if not path.exists():
        return {}
    if path.is_symlink():
        raise ValueError("Importer registry cannot be a symbolic link.")
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw) if raw.strip() else {}
    if not isinstance(data, dict) or data.get("version") != REGISTRY_VERSION:
        raise ValueError("Invalid importer registry format.")
    rows = data.get("importers")
    if not isinstance(rows, dict) or len(rows) > MAX_IMPORTERS:
        raise ValueError("Invalid importer registry entries.")
    result: dict[str, dict[str, Any]] = {}
    for key, entry in rows.items():
        if not isinstance(entry, dict):
            raise ValueError("Invalid importer entry.")
        validated = validate_importer(
            key,
            entry.get("argv"),
            timeout_seconds=entry.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
            output_limit_bytes=entry.get("output_limit_bytes", DEFAULT_OUTPUT_BYTES),
        )
        if "created_at" in entry and isinstance(entry["created_at"], str):
            validated["created_at"] = entry["created_at"]
        result[key] = validated
    return result


def _write_registry(data_dir: Path, entries: dict[str, dict[str, Any]]) -> None:
    payload = {"version": REGISTRY_VERSION, "importers": {k: entries[k] for k in sorted(entries)}}
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    write_durable_private(_registry_path(data_dir), encoded, mode=0o600, prefix=".importers-")


def list_importers(data_dir: Path) -> list[dict[str, Any]]:
    """Return all registered importers in deterministic name order."""
    entries = _read_registry(Path(data_dir))
    return [entries[name] for name in sorted(entries)]


def get_importer(data_dir: Path, name: str) -> dict[str, Any]:
    clean_name = validate_name(name)
    entries = _read_registry(Path(data_dir))
    if clean_name not in entries:
        raise ValueError(f"Unknown importer '{clean_name}'. Register it first with `cairnmind importer add`.")
    return entries[clean_name]


def add_importer(
    data_dir: Path,
    name: str,
    argv: list[str],
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    output_limit_bytes: int = DEFAULT_OUTPUT_BYTES,
    replace: bool = False,
) -> dict[str, Any]:
    """Register or replace an external converter command in ``<data_dir>/importers.json``."""
    require_outside_session()
    root = Path(data_dir)
    entry = validate_importer(
        name,
        argv,
        timeout_seconds=timeout_seconds,
        output_limit_bytes=output_limit_bytes,
    )
    entry["created_at"] = _now()
    with _registry_lock(root):
        entries = _read_registry(root)
        if entry["name"] in entries and not replace:
            raise ValueError(f"Importer '{entry['name']}' already exists (pass --replace to overwrite).")
        if entry["name"] not in entries and len(entries) >= MAX_IMPORTERS:
            raise ValueError(f"At most {MAX_IMPORTERS} importers may be registered.")
        entries[entry["name"]] = entry
        _write_registry(root, entries)
    return entry


def remove_importer(data_dir: Path, name: str) -> dict[str, Any]:
    """Remove an importer from ``<data_dir>/importers.json``. Stored sources are kept untouched."""
    require_outside_session()
    clean_name = validate_name(name)
    root = Path(data_dir)
    with _registry_lock(root):
        entries = _read_registry(root)
        if clean_name not in entries:
            raise ValueError(f"Unknown importer '{clean_name}'.")
        removed = entries.pop(clean_name)
        _write_registry(root, entries)
    return {"removed": True, **removed}


def parse_converted_jsonl(raw_bytes: bytes, *, default_session_id: str) -> list[dict[str, Any]]:
    """Strictly parse normalised JSONL version 1 output produced by an external converter."""
    if not isinstance(raw_bytes, (bytes, bytearray)):
        raise ValueError("Converter output must be raw UTF-8 JSONL bytes.")
    raw = bytes(raw_bytes)
    if not raw:
        return []
    if not raw.endswith(b"\n"):
        raise ValueError("Converter output ended mid-line without a trailing newline.")
    try:
        raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Converter output is not valid UTF-8: {exc}") from None

    events: list[dict[str, Any]] = []
    for line_no, raw_line in enumerate(raw.splitlines(keepends=True), start=1):
        if len(raw_line) > MAX_LINE_BYTES:
            raise ValueError(f"Converter output line {line_no} exceeds {MAX_LINE_BYTES} bytes.")
        line_text = raw_line.decode("utf-8")
        stripped = line_text.strip()
        if not stripped:
            raise ValueError(f"Converter output line {line_no} is blank; every line must be a JSON object.")
        try:
            obj = json.loads(stripped)
        except ValueError as exc:
            raise ValueError(f"Converter output line {line_no} is not valid JSON: {exc}") from None
        if not isinstance(obj, dict):
            raise ValueError(f"Converter output line {line_no} must be a JSON object.")
        unknown_keys = sorted(set(obj) - ALLOWED_EVENT_KEYS)
        if unknown_keys:
            raise ValueError(
                f"Converter output line {line_no} contains unsupported fields: {', '.join(unknown_keys)}"
            )
        version = obj.get("version")
        if type(version) is not int or version != CONVERTED_FORMAT_VERSION:
            raise ValueError(
                f"Converter output line {line_no} must declare integer version {CONVERTED_FORMAT_VERSION}."
            )
        role = obj.get("role")
        if not isinstance(role, str) or role not in ALLOWED_ROLES:
            raise ValueError(
                f"Converter output line {line_no} has invalid role {role!r}; "
                f"expected one of {', '.join(sorted(ALLOWED_ROLES))}."
            )
        content = obj.get("content")
        if not isinstance(content, str) or "\0" in content or not content.strip():
            raise ValueError(f"Converter output line {line_no} must have nonempty UTF-8 string content.")
        session_id = obj.get("session_id")
        if session_id is not None:
            if not isinstance(session_id, str) or "\0" in session_id or not 1 <= len(session_id.strip()) <= 200:
                raise ValueError(f"Converter output line {line_no} has an invalid session_id.")
            session_id = session_id.strip()
        else:
            session_id = default_session_id
        for optional_key, limit in (("timestamp", 64), ("cwd", 4096), ("event_id", 200)):
            val = obj.get(optional_key)
            if val is not None and (not isinstance(val, str) or "\0" in val or len(val) > limit or not val.strip()):
                raise ValueError(f"Converter output line {line_no} has an invalid {optional_key}.")
        events.append({
            "line_no": line_no,
            "raw_line": line_text,
            "sha256": hashlib.sha256(raw_line).hexdigest(),
            "version": version,
            "role": role,
            "content": content,
            "session_id": session_id,
            "timestamp": obj.get("timestamp"),
            "cwd": obj.get("cwd"),
            "event_id": obj.get("event_id"),
        })
    return events


def clean_converter_env(environ=None) -> dict[str, str]:
    """Build a minimal process environment containing no Cairnmind secrets or variables."""
    source_env = os.environ if environ is None else environ
    result: dict[str, str] = {}
    for name in INHERITED:
        value = source_env.get(name)
        if value is not None and not value.startswith("()"):
            result[name] = value
    return result


def _stop_process_group(process: subprocess.Popen, job: ProcessJob | None) -> None:
    with suppress(OSError):
        terminate_group(process.pid, force=False)
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        with suppress(OSError):
            terminate_group(process.pid, force=True)
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2.0)
    if job is not None:
        job.close()


def execute_converter(entry: dict[str, Any], input_path: Path) -> bytes:
    """Run the converter command out of process with clean env, timeout, and output bounds."""
    command = [*entry["argv"], str(input_path)]
    timeout_seconds = int(entry["timeout_seconds"])
    output_limit_bytes = int(entry["output_limit_bytes"])
    stdout_buf = bytearray()
    stderr_buf = bytearray()
    total_bytes = 0
    process: subprocess.Popen | None = None
    job: ProcessJob | None = None
    readers: list[threading.Thread] = []
    outcome = "completed"

    with tempfile.TemporaryDirectory(prefix="cairnmind-importer-") as workdir:
        try:
            process = subprocess.Popen(
                command,
                cwd=workdir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=clean_converter_env(),
                shell=False,
                **group_popen_kwargs(),
            )
            job = ProcessJob()
            job.attach(process.pid)
        except OSError as exc:
            if job is not None:
                job.close()
            raise ImporterExecutionError(f"Failed to start importer '{entry['name']}': {exc}") from None

        chunks: queue.Queue[tuple[str, bytes | None]] = queue.Queue()
        open_streams = {"stdout", "stderr"}

        def pump(name: str, stream) -> None:
            try:
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    chunks.put((name, chunk))
            except OSError as exc:
                log.debug("Importer %s pipe closed during read: %s", name, exc)
            finally:
                chunks.put((name, None))

        for name in ("stdout", "stderr"):
            reader = threading.Thread(target=pump, args=(name, getattr(process, name)), daemon=True)
            reader.start()
            readers.append(reader)

        started = time.monotonic()
        try:
            while open_streams:
                elapsed = time.monotonic() - started
                if elapsed >= timeout_seconds:
                    outcome = "timeout"
                    break
                step = min(0.05, max(0.001, timeout_seconds - elapsed))
                try:
                    stream_name, chunk = chunks.get(timeout=step)
                except queue.Empty:
                    continue
                if chunk is None:
                    open_streams.discard(stream_name)
                    continue
                remaining = output_limit_bytes - total_bytes
                if len(chunk) > remaining:
                    if remaining > 0:
                        (stdout_buf if stream_name == "stdout" else stderr_buf).extend(chunk[:remaining])
                    outcome = "output_limit"
                    break
                (stdout_buf if stream_name == "stdout" else stderr_buf).extend(chunk)
                total_bytes += len(chunk)

            if outcome == "completed":
                left = max(0.001, timeout_seconds - (time.monotonic() - started))
                try:
                    process.wait(timeout=left)
                except subprocess.TimeoutExpired:
                    outcome = "timeout"
            if outcome != "completed":
                _stop_process_group(process, job)
                job = None
        finally:
            if process is not None:
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
            for reader in readers:
                reader.join(timeout=1.0)
            if job is not None:
                job.close()

    if outcome == "timeout":
        raise ImporterExecutionError(
            f"Importer '{entry['name']}' exceeded its timeout of {timeout_seconds}s."
        )
    if outcome == "output_limit":
        raise ImporterExecutionError(
            f"Importer '{entry['name']}' exceeded its output limit of {output_limit_bytes} bytes."
        )
    if process is None:
        raise ImporterExecutionError(f"Importer '{entry['name']}' did not start.")
    if process.returncode != 0:
        err_preview = " ".join(bytes(stderr_buf[:300]).decode("utf-8", errors="replace").split())
        suffix = f": {err_preview}" if err_preview else ""
        raise ImporterExecutionError(
            f"Importer '{entry['name']}' exited with code {process.returncode}{suffix}"
        )
    return bytes(stdout_buf)


def run_converter_import(
    store,
    settings: Settings,
    path: Path,
    importer_name: str,
    project: str | None = None,
) -> dict[str, Any]:
    """Convert an external transcript file via a registered converter and archive it as degraded sources."""
    require_outside_session()
    require_writer(store)
    entry = get_importer(settings.data_dir, importer_name)

    given = Path(path).expanduser()
    resolved = given.resolve()
    if not resolved.is_file():
        raise ValueError(f"History does not exist: {given}")
    if lies_in(resolved, settings.data_dir):
        raise ImporterRefused("Cannot import files located inside Cairnmind's data directory.")

    project_id = store.resolve_project(project)["id"] if project else None
    try:
        masker = ingest_masker(settings)
    except ValueError as exc:
        raise ValueError(f"Transcript import refused: {exc}") from None

    raw_output = execute_converter(entry, resolved)
    events = parse_converted_jsonl(raw_output, default_session_id=resolved.stem)

    archived = 0
    chunks = 0
    inbox = 0
    source_ids: list[str] = []
    errors: list[str] = []
    client_tag = f"importer:{entry['name']}"

    for event in events:
        resolved_project, conflict = _source_project(store, project_id, event.get("cwd"))
        if not resolved_project:
            reason = conflict or "No registered project matches working directory"
            inbox_id = _identifier(resolved, entry["name"], event["line_no"], event["raw_line"])
            db_upsert_document(
                store,
                "ingestion_inbox",
                {
                    "id": inbox_id,
                    "path": str(resolved),
                    "source_offset": event["line_no"],
                    "sha256": event["sha256"],
                    "raw": event["raw_line"],
                    "client": client_tag,
                    "cwd": event.get("cwd"),
                    "reason": reason,
                    "created_at": _now(),
                },
                key_field="id",
            )
            inbox += 1
            errors.append(f"{resolved}:{event['line_no']}: {reason}")
            continue

        masked_text, kinds = masker.mask(event["content"])
        meta: dict[str, Any] = {
            "importer": entry["name"],
            "client": client_tag,
            "role": event["role"],
            "session_id": event["session_id"],
            "cwd": event.get("cwd"),
            "import_path": str(resolved),
            "line_number": event["line_no"],
            "converted_format_version": CONVERTED_FORMAT_VERSION,
            "normalized_text": masked_text,
            "source_timestamp": event.get("timestamp"),
            "event_id": event.get("event_id"),
            "origin_client": client_tag,
            "origin_history": "converted",
            "origin_evidence": "degraded",
            "evidence_verified": False,
        }
        uri = (
            f"transcript://importer/{entry['name']}/"
            f"{_identifier(resolved, event['session_id'])}/{event['line_no']}/{event['sha256']}"
        )
        source = store.snapshot(
            resolved_project,
            uri,
            event["raw_line"],
            kind="converted_transcript",
            metadata=masked_metadata(meta, kinds),
        )
        archived += 1
        source_ids.append(source["id"])
        chunks += _write_chunks(
            store,
            resolved_project,
            source["id"],
            masked_text,
            {"role": event["role"], "importer": entry["name"]},
        )

    return {
        "importer": entry["name"],
        "path": str(resolved),
        "files": 1,
        "events": len(events),
        "archived": archived,
        "chunks": chunks,
        "inbox": inbox,
        "source_ids": source_ids,
        "errors": errors,
        "evidence_state": "degraded",
    }
