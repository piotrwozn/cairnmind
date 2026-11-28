"""SQLite scale threshold telemetry & unstable filesystem detection (PR #8 / Faza 5).

Persists rolling latency and lock-contention telemetry to ``data_dir/runtime/sqlite-metrics.json``,
detects network / DrvFS filesystems where SQLite WAL locks are unsafe (NFS, CIFS/SMB, WSL ``/mnt/c/``,
Windows ``DRIVE_REMOTE`` / UNC paths), reports scale diagnostics in ``cairnmind doctor``, and emits a
rate-limited (max once per 24h) passive recommendation on ``stderr`` to migrate via
``cairnmind migrate --to server``.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, TextIO

from cairnmind.config import Settings, resolved_backend, sqlite_db_path
from cairnmind.pal import platform_hints
from cairnmind.pal.private_files import make_private

log = logging.getLogger(__name__)

# Scale thresholds recommending migration from SQLite to ArcadeDB server (Faza 5).
MAX_DB_BYTES = 500 * 1024 * 1024  # 500 MB
MAX_FILES_COUNT = 25_000
MAX_EDGES_COUNT = 250_000
MAX_P95_MS = 250.0
MAX_BUSY_RETRIES_10M = 10
HINT_COOLDOWN_SECONDS = 24 * 3600

DEFAULT_THRESHOLDS: dict[str, int | float] = {
    "max_db_bytes": MAX_DB_BYTES,
    "max_files_count": MAX_FILES_COUNT,
    "max_edges_count": MAX_EDGES_COUNT,
    "max_fts_p95_ms": MAX_P95_MS,
    "max_graph_p95_ms": MAX_P95_MS,
    "max_busy_retries_10m": MAX_BUSY_RETRIES_10M,
}

# Filesystem types known to have unsafe POSIX flock / WAL semantics for SQLite.
UNSAFE_FSTYPES = frozenset({
    "nfs",
    "nfs4",
    "cifs",
    "smb",
    "smb2",
    "smb3",
    "smbfs",
    "9p",
    "drvfs",
    "fuse.sshfs",
    "afs",
    "glusterfs",
    "ceph",
})

_WSL_MNT_RE = re.compile(r"^/mnt/[a-zA-Z](?:/|$)")
_DRIVE_REMOTE = 4  # Win32 GetDriveTypeW constant


def metrics_path(data_dir: Path) -> Path:
    return Path(data_dir).expanduser().resolve() / "runtime" / "sqlite-metrics.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".sqlite-metrics-", dir=path.parent)
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


def load_sqlite_metrics(data_dir: Path) -> dict[str, Any]:
    path = metrics_path(data_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record_sqlite_metrics(data_dir: Path, snapshot: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """Persist rolling SQLite telemetry into ``data_dir/runtime/sqlite-metrics.json``."""
    existing = load_sqlite_metrics(data_dir)
    ts = time.time() if now is None else float(now)
    merged: dict[str, Any] = {
        **existing,
        "db_bytes": int(snapshot.get("db_bytes", existing.get("db_bytes", 0)) or 0),
        "busy_retries_total": int(snapshot.get("busy_retries_total", existing.get("busy_retries_total", 0)) or 0),
        "busy_retries_10m": int(snapshot.get("busy_retries_10m", existing.get("busy_retries_10m", 0)) or 0),
        "fts_p95_ms": float(snapshot.get("fts_p95_ms", existing.get("fts_p95_ms", 0.0)) or 0.0),
        "graph_p95_ms": float(snapshot.get("graph_p95_ms", existing.get("graph_p95_ms", 0.0)) or 0.0),
        "sqlite_vec_loaded": bool(snapshot.get("sqlite_vec_loaded", existing.get("sqlite_vec_loaded", False))),
        "updated_at": ts,
    }
    with suppress(OSError):
        _atomic_write_json(metrics_path(data_dir), merged)
    return merged


def _windows_drive_type(path_str: str) -> int | None:
    if sys.platform != "win32":
        return None
    drive, _ = os.path.splitdrive(path_str)
    if not drive:
        return None
    root = drive + "\\"
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
        kernel32.GetDriveTypeW.restype = ctypes.c_uint
        return int(kernel32.GetDriveTypeW(root))
    except (OSError, AttributeError, ValueError, TypeError) as exc:
        log.debug("Windows GetDriveTypeW lookup failed for %s: %s", root, exc)
        return None


def _match_mount_fstype(target_path: str, mounts_text: str) -> tuple[str | None, str | None]:
    """Return ``(mount_point, fstype)`` for the longest matching mount entry."""
    normalized = target_path.rstrip("/") or "/"
    best_mount: str | None = None
    best_fs: str | None = None
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mount_point = parts[1].replace("\\040", " ").rstrip("/") or "/"
        fstype = parts[2].lower()
        if (
            (mount_point == "/" or normalized == mount_point or normalized.startswith(mount_point + "/"))
            and (best_mount is None or len(mount_point) > len(best_mount))
        ):
            best_mount = mount_point
            best_fs = fstype
    return best_mount, best_fs


def detect_filesystem_risk(
    db_path: Path | str,
    *,
    platform_name: str | None = None,
    is_wsl: bool | None = None,
    mounts_text: str | None = None,
    drive_type_fn: Callable[[str], int | None] | None = None,
) -> dict[str, Any]:
    """Detect network or DrvFS filesystems where SQLite WAL locking is unreliable."""
    raw = str(db_path)
    if raw == ":memory:":
        return {"risk": False, "kind": None, "fstype": "memory", "path": ":memory:", "reason": None}

    plat = (platform_name or sys.platform).lower()
    wsl = platform_hints.is_wsl() if is_wsl is None else bool(is_wsl)

    # UNC network share paths (\\server\share or //server/share)
    if raw.startswith("\\\\") or raw.startswith("//"):
        return {
            "risk": True,
            "kind": "unc",
            "fstype": "unc",
            "path": raw,
            "reason": (
                f"SQLite database path {raw} is on a UNC network share; SQLite WAL file locks are "
                "unreliable on network shares. Move the database to a local drive (--db-path) or "
                "migrate to server mode (`cairnmind migrate --to server`)."
            ),
        }

    # Windows remote network drive check via Win32 GetDriveTypeW
    if plat == "win32":
        probe = drive_type_fn or _windows_drive_type
        dtype = probe(raw)
        if dtype == _DRIVE_REMOTE:
            return {
                "risk": True,
                "kind": "network_drive",
                "fstype": "drive_remote",
                "path": raw,
                "reason": (
                    f"SQLite database path {raw} resides on a remote/network Windows drive "
                    "(DRIVE_REMOTE); WAL locking can corrupt or stall. Move to a local drive "
                    "(--db-path) or run `cairnmind migrate --to server`."
                ),
            }
        return {"risk": False, "kind": None, "fstype": "local", "path": raw, "reason": None}

    # Linux / WSL mount table inspection
    resolved_str = raw
    if not raw.startswith("/"):
        try:
            resolved_str = str(Path(raw).expanduser().resolve())
        except OSError:
            resolved_str = raw

    if mounts_text is None:
        try:
            mounts_text = Path("/proc/mounts").read_text(encoding="utf-8", errors="replace")
        except OSError:
            mounts_text = ""

    mount_point, fstype = _match_mount_fstype(resolved_str, mounts_text) if mounts_text else (None, None)
    if fstype in UNSAFE_FSTYPES:
        kind = "wsl_drvfs" if fstype in {"9p", "drvfs"} else "network_fs"
        return {
            "risk": True,
            "kind": kind,
            "fstype": fstype,
            "mount_point": mount_point,
            "path": resolved_str,
            "reason": (
                f"SQLite database path {resolved_str} is on {fstype} (mounted at {mount_point}); "
                "SQLite WAL locks are unsafe or severely degraded on network/DrvFS mounts. "
                "Keep data_dir on a native local filesystem (e.g. ~/.local/share/cairnmind) or "
                "run `cairnmind migrate --to server`."
            ),
        }

    # Explicit WSL /mnt/<drive>/ safeguard even if /proc/mounts was unavailable or customized.
    if wsl and _WSL_MNT_RE.match(resolved_str):
        return {
            "risk": True,
            "kind": "wsl_drvfs",
            "fstype": fstype or "drvfs",
            "mount_point": resolved_str[:6],
            "path": resolved_str,
            "reason": (
                f"SQLite database path {resolved_str} is under WSL Windows mount (/mnt/<drive>/); "
                "SQLite WAL locks are unreliable across 9p/DrvFS. Keep data_dir inside the Linux "
                "filesystem or run `cairnmind migrate --to server`."
            ),
        }

    return {"risk": False, "kind": None, "fstype": fstype or "local", "path": resolved_str, "reason": None}


def _read_db_counts(db_path: Path | str, store: Any = None) -> tuple[int, int, int]:
    """Return ``(db_bytes, files_count, edges_count)`` without creating or mutating any DB."""
    if store is not None and getattr(getattr(store, "backend", None), "backend_kind", None) == "sqlite":
        from cairnmind.storage.storage_query import db_count, eq, is_in
        snap = store.backend.metrics_snapshot()
        db_bytes = int(snap.get("db_bytes", 0))
        files_count = db_count(
            store, "sources", [eq("is_current", True), is_in("kind", ("code", "file", "document"))]
        )
        edges_count = db_count(store, "edges", {"is_current": True})
        return db_bytes, files_count, edges_count

    if str(db_path) == ":memory:":
        return 0, 0, 0
    path = Path(db_path)
    if not path.is_file():
        return 0, 0, 0

    db_bytes = 0
    try:
        db_bytes = path.stat().st_size
        wal = Path(str(path) + "-wal")
        if wal.exists():
            db_bytes += wal.stat().st_size
    except OSError:
        pass

    files_count = 0
    edges_count = 0
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1.0)
        conn.execute("PRAGMA query_only=ON")
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "sources" in tables:
            row = conn.execute(
                "SELECT count(*) FROM sources WHERE is_current=1 AND kind IN ('code','file','document')"
            ).fetchone()
            files_count = int(row[0]) if row else 0
        if "edges" in tables:
            row = conn.execute("SELECT count(*) FROM edges WHERE is_current=1").fetchone()
            edges_count = int(row[0]) if row else 0
    except sqlite3.Error:
        pass
    finally:
        if conn is not None:
            conn.close()
    return db_bytes, files_count, edges_count


def evaluate_sqlite_health(
    settings: Settings,
    *,
    store: Any = None,
    thresholds: dict[str, int | float] | None = None,
    platform_name: str | None = None,
    is_wsl: bool | None = None,
    mounts_text: str | None = None,
    drive_type_fn: Callable[[str], int | None] | None = None,
) -> dict[str, Any]:
    """Evaluate SQLite database size, scale metrics, and filesystem safety (read-only)."""
    limits = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    db_path = sqlite_db_path(settings)
    db_exists = str(db_path) == ":memory:" or Path(db_path).is_file()
    persisted = load_sqlite_metrics(settings.data_dir)
    live = (
        store.backend.metrics_snapshot()
        if store is not None and getattr(getattr(store, "backend", None), "backend_kind", None) == "sqlite"
        else {}
    )
    db_bytes, files_count, edges_count = _read_db_counts(db_path, store=store)
    if db_bytes == 0 and persisted.get("db_bytes"):
        db_bytes = int(persisted.get("db_bytes", 0))

    fts_p95_ms = float(live.get("fts_p95_ms") or persisted.get("fts_p95_ms") or 0.0)
    graph_p95_ms = float(live.get("graph_p95_ms") or persisted.get("graph_p95_ms") or 0.0)
    busy_retries_10m = max(
        int(live.get("busy_retries_10m") or 0),
        int(persisted.get("busy_retries_10m") or 0),
    )
    busy_retries_total = max(
        int(live.get("busy_retries_total") or 0),
        int(persisted.get("busy_retries_total") or 0),
    )

    metrics = {
        "db_bytes": db_bytes,
        "db_mb": round(db_bytes / (1024 * 1024), 2),
        "files_count": files_count,
        "edges_count": edges_count,
        "fts_p95_ms": round(fts_p95_ms, 2),
        "graph_p95_ms": round(graph_p95_ms, 2),
        "busy_retries_10m": busy_retries_10m,
        "busy_retries_total": busy_retries_total,
        "sqlite_vec_loaded": bool(live.get("sqlite_vec_loaded", persisted.get("sqlite_vec_loaded", False))),
    }

    fs_info = detect_filesystem_risk(
        db_path,
        platform_name=platform_name,
        is_wsl=is_wsl,
        mounts_text=mounts_text,
        drive_type_fn=drive_type_fn,
    )

    breached: list[dict[str, Any]] = []
    warnings: list[str] = []

    checks = [
        ("db_bytes", db_bytes, limits["max_db_bytes"], f"SQLite database size ({metrics['db_mb']} MB) exceeds {round(limits['max_db_bytes'] / (1024 * 1024), 1)} MB"),
        ("files_count", files_count, limits["max_files_count"], f"Indexed file count ({files_count:,}) exceeds {int(limits['max_files_count']):,}"),
        ("edges_count", edges_count, limits["max_edges_count"], f"Graph edge count ({edges_count:,}) exceeds {int(limits['max_edges_count']):,}"),
        ("fts_p95_ms", fts_p95_ms, limits["max_fts_p95_ms"], f"FTS5 query p95 latency ({metrics['fts_p95_ms']} ms) exceeds {limits['max_fts_p95_ms']} ms"),
        ("graph_p95_ms", graph_p95_ms, limits["max_graph_p95_ms"], f"Graph traversal p95 latency ({metrics['graph_p95_ms']} ms) exceeds {limits['max_graph_p95_ms']} ms"),
        ("busy_retries_10m", busy_retries_10m, limits["max_busy_retries_10m"], f"SQLITE_BUSY retries in last 10m ({busy_retries_10m}) exceeds {int(limits['max_busy_retries_10m'])}"),
    ]
    for metric_name, observed, threshold, message in checks:
        if observed > threshold:
            breached.append({"metric": metric_name, "observed": observed, "threshold": threshold})
            warnings.append(f"{message}. Recommend migrating via `cairnmind migrate --to server`.")

    if fs_info.get("risk") and fs_info.get("reason"):
        warnings.append(str(fs_info["reason"]))

    return {
        "backend": resolved_backend(settings),
        "db_path": str(db_path),
        "exists": db_exists,
        "metrics": metrics,
        "thresholds": limits,
        "filesystem": fs_info,
        "breached_thresholds": breached,
        "warnings": warnings,
        "recommend_server_migration": bool(breached or fs_info.get("risk")),
    }


def maybe_emit_scale_hint(
    settings: Settings,
    health: dict[str, Any],
    *,
    now: float | None = None,
    stream: TextIO | None = None,
) -> bool:
    """Emit at most one passive stderr hint per 24h when SQLite thresholds or FS risks trigger.

    Never writes to stdout so ``--json`` output remains strictly valid JSON.
    """
    if not health.get("recommend_server_migration"):
        return False
    ts = time.time() if now is None else float(now)
    existing = load_sqlite_metrics(settings.data_dir)
    last_warned = existing.get("last_warned_at")
    if isinstance(last_warned, (int, float)) and (ts - float(last_warned)) < HINT_COOLDOWN_SECONDS:
        return False

    reasons = health.get("warnings") or []
    summary = reasons[0] if reasons else "SQLite scale threshold exceeded."
    target_stream = stream if stream is not None else sys.stderr
    print(f"Cairnmind hint: {summary}", file=target_stream)
    existing["last_warned_at"] = ts
    with suppress(OSError):
        _atomic_write_json(metrics_path(settings.data_dir), existing)
    return True
