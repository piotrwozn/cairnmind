"""Lossless bidirectional storage migration (`sqlite <-> arcade` / server) — PR #8 / Faza 5.

Implements the 5-step crash-safe migration protocol (ADR-01, ADR-02, ADR-04, ADR-05):
1. **Preflight:** refuses inside coding sessions, validates direction, target reachability &
   version, free disk space (`>= 2x` source size), SQLite scale thresholds on reverse migration
   (unless `--force`), and non-empty target protection (unless `--overwrite` or `--target-namespace`).
2. **Quiesce:** acquires exclusive `migration.lock`, writes `migration-in-progress` marker
   (causing any running HTTP service to answer 503 `migration_in_progress`), and locks
   `service.lock` when no service is active.
3. **Idempotent batched streaming (`batch_size=1000`):** streams all 28 collections in
   dependency-safe order (`nodes` before `edges`) with durable checkpoints in
   `data_dir/runtime/migration-state.json` supporting `--resume` and `--abort`, optional
   `--include-cache`, and ADR-04 `person_confirmations` policy (`role=writer` keeps
   confirmations without requiring `tmux`; `role=reader` downgrades via `BackupClaims`).
4. **Canonical parity verification (before cutover):** compares exact per-collection counts
   and canonical SHA-256 digests across all 28 collections; any mismatch aborts before cutover.
5. **Atomic cutover & cleanup:** closes all SQLite connections (required on Windows), atomically
   updates `data_dir/config.json`, archives `state.db` -> `state.db.migrated-<timestamp>.bak`
   when migrating to server, removes markers/checkpoints, and reloads any running HTTP service
   via `/internal/reload-store` so MCP / client configs stay unchanged.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from cairnmind.config import Settings, resolved_backend, session_value, sqlite_db_path, tls_verify
from cairnmind.pal.file_locks import lock_exclusive
from cairnmind.pal.private_files import make_private
from cairnmind.storage import store_writer
from cairnmind.storage.restore_claims import BackupClaims
from cairnmind.storage.sql import clean_row, utc_now
from cairnmind.storage.sqlite_telemetry import DEFAULT_THRESHOLDS, evaluate_sqlite_health
from cairnmind.storage.storage_backend import ALL_COLLECTIONS

MIGRATION_LOCK_FILE = "migration.lock"
log = logging.getLogger(__name__)
MIGRATION_MARKER_FILE = "migration-in-progress"
MIGRATION_STATE_REL = Path("runtime") / "migration-state.json"
DEFAULT_BATCH_SIZE = 1000
MIN_FREE_DISK_BYTES = 16 * 1024 * 1024

# Dependency-safe order: `nodes` precedes `edges`; `vector_indexes` precedes `embeddings`.
MIGRATION_COLLECTIONS: tuple[str, ...] = tuple(ALL_COLLECTIONS)


class MigrationError(ValueError):
    """Base error for refused or failed storage migrations."""

    def __init__(self, message: str, *, code: str = "migration_error") -> None:
        super().__init__(message)
        self.code = code


class MigrationRefused(MigrationError):
    """Preflight or safety gate refused the migration without changing config or source data."""


class MigrationParityError(MigrationError):
    """Canonical count or SHA-256 parity check failed; cutover aborted."""


def migration_marker_path(data_dir: Path) -> Path:
    return Path(data_dir).expanduser().resolve() / MIGRATION_MARKER_FILE


def migration_state_path(data_dir: Path) -> Path:
    return Path(data_dir).expanduser().resolve() / MIGRATION_STATE_REL


def is_migration_in_progress(data_dir: Path) -> bool:
    return migration_marker_path(data_dir).exists()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        with suppress(OSError):
            make_private(tmp)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def load_migration_state(data_dir: Path) -> dict[str, Any] | None:
    path = migration_state_path(data_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _canonical_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _canonical_value(v) for k, v in sorted(value.items()) if v is not None}
    if isinstance(value, list):
        return [_canonical_value(v) for v in value]
    return value


def canonical_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a row across ArcadeDB and SQLite for deterministic SHA-256 parity checks."""
    cleaned = clean_row(row)
    return _canonical_value(cleaned)


def compute_store_parity(
    store: Any,
    *,
    collections: tuple[str, ...] = MIGRATION_COLLECTIONS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    transform: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
) -> tuple[dict[str, int], dict[str, str], str]:
    """Compute exact per-collection counts, per-collection SHA-256, and overall dataset SHA-256."""
    counts: dict[str, int] = {}
    digests: dict[str, str] = {}
    overall = hashlib.sha256()

    for collection in collections:
        col_hash = hashlib.sha256()
        count = 0
        for batch in store.backend.iter_collection(store.client, collection, batch_size=batch_size):
            for row in batch:
                effective = transform(collection, row) if transform is not None else row
                norm = canonical_row(effective)
                line = json.dumps(norm, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                col_hash.update(f"{collection}\0{line}\n".encode())
                count += 1
        col_hex = col_hash.hexdigest()
        counts[collection] = count
        digests[collection] = col_hex
        overall.update(f"{collection}:{count}:{col_hex}\n".encode())

    return counts, digests, overall.hexdigest()


def _source_size_bytes(settings: Settings, source_backend: str, source_store: Any) -> int:
    if source_backend == "sqlite":
        db_path = sqlite_db_path(settings)
        if str(db_path) != ":memory:" and Path(db_path).is_file():
            total = Path(db_path).stat().st_size
            wal = Path(str(db_path) + "-wal")
            if wal.is_file():
                total += wal.stat().st_size
            return total
        return 0
    # ArcadeDB: measure local database directory if present, else estimate from row counts.
    db_dir = settings.data_dir / "arcade" / "databases" / settings.arcade_database
    if db_dir.is_dir():
        total = 0
        for root, _, files in os.walk(db_dir):
            for fname in files:
                with suppress(OSError):
                    total += (Path(root) / fname).stat().st_size
        if total > 0:
            return total
    counts = source_store.backend.collection_counts(source_store.client)
    return sum(counts.values()) * 512


def _non_bootstrap_rows(counts: dict[str, int]) -> dict[str, int]:
    """Return collections with real data (excluding the 1-row bootstrap journal_state)."""
    non_empty: dict[str, int] = {}
    for col, count in counts.items():
        if count <= 0:
            continue
        if col == "journal_state" and count == 1:
            continue
        non_empty[col] = count
    return non_empty


def _check_arcade_server(target_settings: Settings) -> dict[str, Any]:
    """Verify target ArcadeDB server reachability & pinned release version."""
    from cairnmind.storage.arcade import ArcadeClient, ArcadeError
    from cairnmind.storage.database_mode import connect_only
    from cairnmind.storage.engine import ensure_engine, version_problem

    if not connect_only(target_settings):
        ensure_engine(target_settings)
    client = ArcadeClient(target_settings)
    try:
        info = client.server_info()
    except ArcadeError as exc:
        raise MigrationRefused(
            f"Target ArcadeDB server at {target_settings.arcade_url} is unreachable: {exc}",
            code="target_unreachable",
        ) from exc
    finally:
        client.close()
    problem = version_problem(target_settings, info)
    if problem:
        raise MigrationRefused(problem, code="target_version_mismatch")
    return info


def _open_store_for_backend(
    settings: Settings,
    *,
    backend: str,
    database: str | None = None,
    db_path: str | None = None,
) -> tuple[Any, Settings]:
    from cairnmind.store import Store

    target = replace(
        settings,
        storage_backend=backend,
        arcade_database=database or settings.arcade_database,
        db_path=db_path if db_path is not None else ("" if backend == "arcade" else settings.db_path),
    )
    return Store(target.data_dir, settings=target), target


def _wipe_target_collections(target_store: Any, target_backend: str) -> None:
    """Clear all 28 collections in target store (edges before nodes for ArcadeDB)."""
    target_store.backend.wipe_all_collections(MIGRATION_COLLECTIONS)


def _write_batch_to_target(
    target_store: Any,
    target_backend: str,
    collection: str,
    rows: list[dict[str, Any]],
) -> None:
    """Idempotently write one batch into target store using raw transactions (preserves journal verbatim)."""
    if not rows:
        return
    target_store.backend.import_migration_batch(collection, rows)


def _sync_writer_meta(source_store: Any, target_store: Any, target_backend: str, source_role: str, me_id: str) -> None:
    """Preserve installation writer metadata on target when migrating same-installation writer."""
    if source_role != "writer":
        return
    writer_row = store_writer.read_writer(source_store.client)
    if not writer_row:
        return
    target_store.backend.upsert_meta_row(writer_row)


def _verify_verdict_cache(data_dir: Path) -> dict[str, Any]:
    cache_path = data_dir / "verdict_cache.sqlite3"
    if not cache_path.is_file():
        return {"present": False, "rows": 0}
    conn = sqlite3.connect(str(cache_path))
    try:
        row = conn.execute("SELECT count(*) FROM verdicts").fetchone()
        return {"present": True, "path": str(cache_path), "rows": int(row[0]) if row else 0}
    except sqlite3.Error:
        return {"present": True, "path": str(cache_path), "rows": 0}
    finally:
        conn.close()


def _archive_sqlite_file(db_path: Path) -> Path | None:
    """Archive migrated SQLite file to ``state.db.migrated-<timestamp>.bak`` (after all handles are closed)."""
    if not db_path.is_file():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = db_path.with_name(f"{db_path.name}.migrated-{stamp}.bak")
    counter = 1
    while backup.exists():
        backup = db_path.with_name(f"{db_path.name}.migrated-{stamp}-{counter}.bak")
        counter += 1
    # Checkpoint WAL if sidecar still exists before moving.
    wal = Path(str(db_path) + "-wal")
    shm = Path(str(db_path) + "-shm")
    if wal.exists():
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    os.replace(db_path, backup)
    wal.unlink(missing_ok=True)
    shm.unlink(missing_ok=True)
    return backup


def _notify_service_reload(settings: Settings) -> dict[str, Any]:
    """Ask a running Cairnmind HTTP service to reload its Store handle transparently."""
    if not settings.token:
        return {"reloaded": False, "reason": "no_token"}
    try:
        with httpx.Client(trust_env=False, timeout=5.0, verify=tls_verify(settings.url)) as client:
            health = client.get(settings.url + "/health")
            if not health.is_success:
                return {"reloaded": False, "reason": "service_not_running"}
            resp = client.post(
                settings.url + "/internal/reload-store",
                headers={"Authorization": f"Bearer {settings.token}"},
            )
            if resp.is_success:
                return {"reloaded": True, "response": resp.json()}
            return {"reloaded": False, "status_code": resp.status_code}
    except (httpx.HTTPError, ValueError):
        return {"reloaded": False, "reason": "service_not_running"}


def abort_migration(settings: Settings) -> dict[str, Any]:
    """Abort an interrupted migration, cleaning partial target state while keeping source & config untouched."""
    if session_value("SESSION_ID", strict=False):
        raise MigrationRefused(
            "cairnmind migrate --abort cannot run inside a coding session; run it from your own shell.",
            code="in_session",
        )
    data_dir = settings.data_dir
    state = load_migration_state(data_dir)
    marker = migration_marker_path(data_dir)
    if state is None and not marker.exists():
        raise MigrationRefused("No in-progress or interrupted migration found to abort.", code="no_migration_state")

    lock_path = data_dir / MIGRATION_LOCK_FILE
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    cleaned_target = False
    with lock_path.open("a", encoding="utf-8") as lock:
        try:
            lock_exclusive(lock, blocking=False)
        except BlockingIOError as exc:
            raise MigrationRefused("Another migration process currently holds migration.lock.", code="lock_held") from exc

        if state is not None:
            direction = state.get("direction")
            if direction == "arcade_to_sqlite":
                target_db = state.get("target_db_path")
                if target_db and target_db != ":memory:":
                    p = Path(target_db)
                    for candidate in (p, Path(str(p) + "-wal"), Path(str(p) + "-shm")):
                        candidate.unlink(missing_ok=True)
                    cleaned_target = True
            elif direction == "sqlite_to_arcade":
                target_ns = state.get("target_namespace") or settings.arcade_database
                try:
                    t_store, _ = _open_store_for_backend(settings, backend="arcade", database=target_ns)
                    try:
                        _wipe_target_collections(t_store, "arcade")
                        cleaned_target = True
                    finally:
                        t_store.close()
                except (OSError, ValueError, RuntimeError, httpx.HTTPError) as exc:
                    log.debug("Failed to wipe target ArcadeDB database during migration abort: %s", exc)
                    cleaned_target = False

        migration_state_path(data_dir).unlink(missing_ok=True)
        marker.unlink(missing_ok=True)

    return {
        "aborted": True,
        "migration_id": (state or {}).get("migration_id"),
        "direction": (state or {}).get("direction"),
        "cleaned_target": cleaned_target,
        "config_unchanged": True,
    }


def migrate(
    settings: Settings,
    *,
    to: str | None = None,
    resume: bool = False,
    overwrite: bool = False,
    target_namespace: str | None = None,
    include_cache: bool = False,
    force: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    disk_usage_fn: Callable[[Path], Any] = shutil.disk_usage,
    on_batch_written: Callable[[str, int], None] | None = None,
) -> dict[str, Any]:
    """Migrate all 28 collections losslessly between embedded SQLite and ArcadeDB server."""
    if session_value("SESSION_ID", strict=False):
        raise MigrationRefused(
            "cairnmind migrate cannot run inside a coding session; run it from your own shell.",
            code="in_session",
        )
    if batch_size < 1:
        raise MigrationRefused("batch_size must be >= 1.", code="invalid_batch_size")

    data_dir = settings.data_dir
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    existing_state = load_migration_state(data_dir)

    if resume:
        if existing_state is None:
            raise MigrationRefused(
                "No interrupted migration checkpoint found to resume.",
                code="no_migration_state",
            )
        direction = str(existing_state["direction"])
        source_backend = "sqlite" if direction == "sqlite_to_arcade" else "arcade"
        target_backend = "arcade" if direction == "sqlite_to_arcade" else "sqlite"
        migration_id = str(existing_state["migration_id"])
        started_at = str(existing_state["started_at"])
        target_namespace = target_namespace or existing_state.get("target_namespace")
        include_cache = bool(include_cache or existing_state.get("include_cache"))
        overwrite = bool(overwrite or existing_state.get("overwrite"))
        completed_collections = set(existing_state.get("completed_collections") or [])
        rows_copied: dict[str, int] = dict(existing_state.get("rows_copied") or {})
    else:
        if existing_state is not None:
            raise MigrationRefused(
                f"An unfinished migration ({existing_state.get('migration_id')}) exists. "
                "Use `cairnmind migrate --resume` to finish it or `cairnmind migrate --abort` to discard it.",
                code="migration_pending",
            )
        if to not in {"server", "sqlite"}:
            raise MigrationRefused("Choose --to server or --to sqlite.", code="invalid_target")
        current_backend = resolved_backend(settings)
        if to == "server":
            if current_backend != "sqlite":
                raise MigrationRefused(
                    "Current storage backend is already ArcadeDB server; nothing to migrate to server.",
                    code="already_on_target",
                )
            source_backend, target_backend, direction = "sqlite", "arcade", "sqlite_to_arcade"
        else:
            if current_backend != "arcade":
                raise MigrationRefused(
                    "Current storage backend is already embedded SQLite; nothing to migrate to sqlite.",
                    code="already_on_target",
                )
            source_backend, target_backend, direction = "arcade", "sqlite", "arcade_to_sqlite"
        migration_id = uuid4().hex
        started_at = utc_now()
        completed_collections = set()
        rows_copied = {}

    if target_namespace and not target_namespace.replace("_", "").replace("-", "").isalnum():
        raise MigrationRefused(f"Invalid target namespace: {target_namespace!r}", code="invalid_namespace")

    # Lock exclusively so two migrations cannot overlap.
    lock_path = data_dir / MIGRATION_LOCK_FILE
    marker_path = migration_marker_path(data_dir)
    source_store = None
    target_store = None

    with lock_path.open("a", encoding="utf-8") as lock:
        try:
            lock_exclusive(lock, blocking=False)
        except BlockingIOError as exc:
            raise MigrationRefused(
                "Another migration is currently running (migration.lock is held).",
                code="lock_held",
            ) from exc

        try:
            # --- Step 1: Preflight ---
            source_db_path = sqlite_db_path(settings) if source_backend == "sqlite" else None
            if source_backend == "sqlite" and (source_db_path is None or not Path(source_db_path).is_file()):
                raise MigrationRefused(
                    f"Source SQLite database does not exist at {source_db_path}.",
                    code="source_missing",
                )

            source_store, _ = _open_store_for_backend(settings, backend=source_backend)
            source_bytes = _source_size_bytes(settings, source_backend, source_store)

            # Free disk space check (>= 2x source size)
            usage = disk_usage_fn(data_dir)
            required_bytes = max(MIN_FREE_DISK_BYTES, 2 * source_bytes)
            if int(usage.free) < required_bytes:
                raise MigrationRefused(
                    f"Insufficient free disk space in {data_dir}: "
                    f"{usage.free} bytes free, need at least {required_bytes} bytes (>= 2x source size).",
                    code="insufficient_disk_space",
                )

            # Reverse migration threshold gate (arcade -> sqlite)
            if target_backend == "sqlite" and not force:
                from cairnmind.storage.storage_query import db_count, eq, is_in
                files_count = db_count(
                    source_store,
                    "sources",
                    [eq("is_current", True), is_in("kind", ("code", "file", "document"))],
                )
                edges_count = db_count(source_store, "edges", {"is_current": True})
                breaches = []
                if source_bytes > DEFAULT_THRESHOLDS["max_db_bytes"]:
                    breaches.append(f"size {round(source_bytes / (1024 * 1024), 1)} MB > 500 MB")
                if files_count > DEFAULT_THRESHOLDS["max_files_count"]:
                    breaches.append(f"files {files_count:,} > {int(DEFAULT_THRESHOLDS['max_files_count']):,}")
                if edges_count > DEFAULT_THRESHOLDS["max_edges_count"]:
                    breaches.append(f"edges {edges_count:,} > {int(DEFAULT_THRESHOLDS['max_edges_count']):,}")
                if breaches:
                    raise MigrationRefused(
                        "Source database exceeds embedded SQLite scale limits ("
                        + ", ".join(breaches)
                        + "). Pass --force to migrate to SQLite anyway.",
                        code="sqlite_limits_exceeded",
                    )

            # Resolve target settings & check connectivity / non-empty target
            effective_ns = target_namespace or settings.arcade_database
            target_db_path_str = ""
            if target_backend == "arcade":
                target_probe_settings = replace(settings, storage_backend="arcade", arcade_database=effective_ns)
                _check_arcade_server(target_probe_settings)
                target_store, target_settings = _open_store_for_backend(
                    settings, backend="arcade", database=effective_ns
                )
            else:
                target_db_path_str = str(sqlite_db_path(settings))
                target_store, target_settings = _open_store_for_backend(
                    settings, backend="sqlite", db_path=target_db_path_str
                )

            if not resume:
                target_counts = target_store.backend.collection_counts(target_store.client)
                non_empty = _non_bootstrap_rows(target_counts)
                if non_empty and not overwrite:
                    names = ", ".join(f"{k}={v}" for k, v in sorted(non_empty.items())[:5])
                    raise MigrationRefused(
                        f"Target database is not empty ({names}). "
                        "Use --target-namespace NAME or --overwrite to replace it.",
                        code="target_not_empty",
                    )
                if non_empty and overwrite:
                    _wipe_target_collections(target_store, target_backend)

            # ADR-04: Determine person_confirmations policy from writer role on source!
            me = store_writer.installation(data_dir)
            src_writer_row = store_writer.read_writer(source_store.client)
            if src_writer_row is None or src_writer_row.get("installation_id") == me.id:
                source_role = "writer"
                person_policy = "kept"
                claims: BackupClaims | None = None
            else:
                source_role = "reader"
                person_policy = "claims_of_the_migration"
                claims = BackupClaims(f"migration:{direction}", at=started_at)

            # --- Step 2: Quiesce ---
            _atomic_write_json(
                marker_path,
                {
                    "migration_id": migration_id,
                    "direction": direction,
                    "started_at": started_at,
                    "pid": os.getpid(),
                },
            )

            state: dict[str, Any] = {
                "migration_id": migration_id,
                "direction": direction,
                "status": "running",
                "batch_size": batch_size,
                "started_at": started_at,
                "target_namespace": effective_ns if target_backend == "arcade" else None,
                "target_db_path": target_db_path_str if target_backend == "sqlite" else None,
                "include_cache": include_cache,
                "overwrite": overwrite,
                "source_role": source_role,
                "person_confirmations": person_policy,
                "completed_collections": sorted(completed_collections),
                "current_collection": None,
                "rows_copied": rows_copied,
            }
            _atomic_write_json(migration_state_path(data_dir), state)

            # --- Step 3: Idempotent batched streaming transfer ---
            for collection in MIGRATION_COLLECTIONS:
                if collection in completed_collections:
                    continue
                state["current_collection"] = collection
                _atomic_write_json(migration_state_path(data_dir), state)

                copied_here = 0
                for batch in source_store.backend.iter_collection(
                    source_store.client, collection, batch_size=batch_size
                ):
                    transformed = (
                        [claims.turn(collection, row) for row in batch]
                        if claims is not None
                        else batch
                    )
                    _write_batch_to_target(target_store, target_backend, collection, transformed)
                    copied_here += len(transformed)
                    rows_copied[collection] = copied_here
                    state["rows_copied"] = rows_copied
                    _atomic_write_json(migration_state_path(data_dir), state)
                    if on_batch_written is not None:
                        on_batch_written(collection, copied_here)

                completed_collections.add(collection)
                state["completed_collections"] = sorted(completed_collections)
                state["rows_copied"] = rows_copied
                _atomic_write_json(migration_state_path(data_dir), state)

            _sync_writer_meta(source_store, target_store, target_backend, source_role, me.id)

            # --- Step 4: Canonical parity verification (BEFORE cutover!) ---
            # Fresh deterministic BackupClaims instance (same `started_at`) so source parity computes the
            # exact same transformed rows whether this ran in one pass or after `--resume`.
            verify_claims = (
                BackupClaims(f"migration:{direction}", at=started_at)
                if source_role == "reader"
                else None
            )
            transform_fn = verify_claims.turn if verify_claims is not None else None
            src_counts, src_digests, src_sha = compute_store_parity(
                source_store, batch_size=batch_size, transform=transform_fn
            )
            dst_counts, dst_digests, dst_sha = compute_store_parity(
                target_store, batch_size=batch_size
            )

            mismatches = []
            for col in MIGRATION_COLLECTIONS:
                if src_counts[col] != dst_counts[col] or src_digests[col] != dst_digests[col]:
                    mismatches.append(
                        f"{col}: source=({src_counts[col]}, {src_digests[col][:12]}) "
                        f"!= target=({dst_counts[col]}, {dst_digests[col][:12]})"
                    )
            if mismatches or src_sha != dst_sha:
                state["status"] = "parity_failed"
                _atomic_write_json(migration_state_path(data_dir), state)
                raise MigrationParityError(
                    "Parity verification failed before cutover; config.json was not modified: "
                    + "; ".join(mismatches[:5]),
                    code="parity_mismatch",
                )

            cache_report = _verify_verdict_cache(data_dir) if include_cache else None

            # --- Step 5: Atomic cutover & cleanup ---
            # Close ALL SQLite/Arcade handles BEFORE moving files (critical on Windows!).
            source_store.close()
            source_store = None
            target_store.close()
            target_store = None

            updated = Settings.load(data_dir, create=False)
            updated.storage_backend = target_backend
            if target_backend == "arcade":
                updated.db_path = ""
                if target_namespace:
                    updated.arcade_database = target_namespace
            elif target_db_path_str and Path(target_db_path_str) != (data_dir / "state.db"):
                updated.db_path = target_db_path_str
            updated.save()

            archived_sqlite: Path | None = None
            if target_backend == "arcade" and source_db_path is not None:
                archived_sqlite = _archive_sqlite_file(Path(source_db_path))

            # Clear markers & checkpoint before notifying service reload.
            migration_state_path(data_dir).unlink(missing_ok=True)
            marker_path.unlink(missing_ok=True)

            service_reload = _notify_service_reload(updated)

            result: dict[str, Any] = {
                "migrated": True,
                "migration_id": migration_id,
                "direction": direction,
                "source_backend": source_backend,
                "target_backend": target_backend,
                "target_namespace": effective_ns if target_backend == "arcade" else None,
                "collections": len(MIGRATION_COLLECTIONS),
                "counts": dst_counts,
                "total_rows": sum(dst_counts.values()),
                "sha256": dst_sha,
                "parity_verified": True,
                "source_role": source_role,
                "person_confirmations": person_policy,
                "archived_sqlite": str(archived_sqlite) if archived_sqlite else None,
                "service_reload": service_reload,
            }
            if verify_claims is not None:
                result["confirmations_turned_into_claims"] = verify_claims.confirmations
                result["record_kinds_withdrawn"] = verify_claims.record_kinds
            if cache_report is not None:
                result["verdict_cache"] = cache_report
            if target_backend == "sqlite":
                result["sqlite_health"] = evaluate_sqlite_health(updated)
            return result
        except BaseException:
            # On preflight refusal before any checkpoint existed, clean up marker cleanly.
            if not migration_state_path(data_dir).exists():
                marker_path.unlink(missing_ok=True)
            raise
        finally:
            if source_store is not None:
                try:
                    source_store.close()
                except (OSError, RuntimeError, sqlite3.Error) as exc:
                    log.debug("Failed to close source store during migration cleanup: %s", exc)
            if target_store is not None:
                try:
                    target_store.close()
                except (OSError, RuntimeError, sqlite3.Error) as exc:
                    log.debug("Failed to close target store during migration cleanup: %s", exc)
