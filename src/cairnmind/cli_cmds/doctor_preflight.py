"""Pre-flight diagnostics for `cairnmind doctor`: Memory Pulse, MCP presets, handshake and host/mount checks.

Strictly read-only: creates no files or directories, starts no engine or service.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any

from cairnmind.pal import platform_hints, private_files
from cairnmind.config import Settings
from cairnmind.memory.memory_paths import MEMORY_RELATIVE, PUBLISHED_RELATIVE
from cairnmind.mcp.mcp_presets import build_server_entry, discover_configured_presets
from cairnmind.mcp.mcp_probe import probe_mcp_stdio

_WSL_DRIVE_RE = re.compile(r"^/mnt/[a-z](/|$)")
_NETWORK_FS_TYPES = frozenset({"nfs", "nfs4", "cifs", "smb", "smb3", "smbfs", "fuse.sshfs", "9p", "drvfs"})


def _nearest_existing_parent(path: Path) -> Path | None:
    current = path
    while True:
        if current.exists():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _mount_fs_type(path: Path) -> str | None:
    """Best-effort filesystem type from /proc/mounts on Linux/WSL; None elsewhere."""
    mounts_file = Path("/proc/mounts")
    if not mounts_file.is_file():
        return None
    try:
        target = str(path.resolve())
        best_mount = ""
        best_fs: str | None = None
        for line in mounts_file.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            mount_point = parts[1].replace("\\040", " ")
            fs_type = parts[2].lower()
            if target == mount_point or target.startswith(mount_point.rstrip("/") + "/"):
                if len(mount_point) > len(best_mount):
                    best_mount = mount_point
                    best_fs = fs_type
        return best_fs
    except OSError:
        return None


def _in_docker() -> bool:
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="replace")
        return "docker" in cgroup or "containerd" in cgroup or "kubepods" in cgroup
    except OSError:
        return False


def _check_writable(path: Path) -> tuple[bool, bool, str | None]:
    """Return (exists, writable, uid_warning) without creating or modifying anything."""
    exists = path.exists()
    target = path if exists else _nearest_existing_parent(path)
    if target is None:
        return False, False, None
    writable = os.access(target, os.W_OK | os.X_OK)
    uid_warning = None
    if exists:
        mismatch = private_files.owner_uid_mismatch(path)
        if mismatch is not None:
            file_uid, euid = mismatch
            uid_warning = (
                f"{path} is owned by UID {file_uid} while this process runs as UID {euid}; "
                "permissions may fail in Docker/WSL."
            )
    return exists, writable, uid_warning


def storage_preflight(settings: Settings, project_root: Path | None = None) -> dict[str, Any]:
    """Check directory write permissions, UID ownership, Docker/WSL2 and slow/network mounts."""
    data_dir = settings.data_dir.expanduser()
    try:
        data_resolved = data_dir.resolve()
    except OSError:
        data_resolved = data_dir.absolute()

    warnings: list[str] = []
    checks: list[dict[str, Any]] = []
    uid_mismatch = False

    targets: list[tuple[str, Path]] = [("data_dir", data_resolved)]
    if project_root is not None:
        root = project_root.expanduser().resolve()
        targets.append(("project_cairnmind", root / ".cairnmind"))
        targets.append(("project_memory", root / MEMORY_RELATIVE))

    all_writable = True
    for label, target in targets:
        exists, writable, uid_warn = _check_writable(target)
        checks.append({"name": label, "path": str(target), "exists": exists, "writable": writable})
        if not writable:
            all_writable = False
            warnings.append(f"Directory {target} is not writable by the current user.")
        if uid_warn:
            uid_mismatch = True
            warnings.append(uid_warn)

    wsl = platform_hints.is_wsl()
    in_docker = _in_docker()
    data_posix = str(data_resolved).replace("\\", "/")
    wsl_windows_mount = bool(wsl and _WSL_DRIVE_RE.match(data_posix))
    if wsl_windows_mount:
        warnings.append(
            f"Data directory {data_resolved} is on a Windows mount (/mnt/...) under WSL2; "
            "move it to the Linux filesystem (e.g. ~/.local/share/cairnmind) for reliable locking and performance."
        )

    fs_type = _mount_fs_type(data_resolved)
    network_fs = fs_type if fs_type in _NETWORK_FS_TYPES else None
    if network_fs and not wsl_windows_mount:
        warnings.append(
            f"Data directory {data_resolved} resides on a {network_fs} mount; "
            "local database files on network/9p filesystems risk lock contention or slow I/O."
        )

    return {
        "writable": all_writable,
        "uid_mismatch": uid_mismatch,
        "in_docker": in_docker,
        "wsl": wsl,
        "wsl_windows_mount": wsl_windows_mount,
        "network_fs": network_fs,
        "path_checks": checks,
        "warnings": warnings,
    }


def security_guards_status(settings: Settings, project_root: Path | None = None) -> dict[str, Any]:
    """Read-only summary of Cairnmind's active security contracts."""
    snapshot_profile = "none"
    if project_root is not None:
        root = project_root.expanduser().resolve()
        if (root / PUBLISHED_RELATIVE).is_dir():
            snapshot_profile = "team"
        elif (root / MEMORY_RELATIVE).is_dir():
            snapshot_profile = "local"
    return {
        "read_before_write_ack": True,
        "person_acts_require_workspace": True,
        "snapshot_profile": snapshot_profile,
        "mcp_auto_approve_disabled": True,
    }


def resolve_doctor_project_root(store, project: str | None = None) -> tuple[dict[str, Any] | None, Path | None]:
    """Resolve the project & root for doctor pre-flight checks, or (None, None) if outside any registered project."""
    if store is None or not hasattr(store, "query"):
        return None, None
    try:
        from cairnmind.memory.pulse import _resolve_project_and_root

        selected, root = _resolve_project_and_root(store, project, None)
        return selected, root
    except (ValueError, AttributeError):
        return None, None


def doctor_pulse_block(settings: Settings, store, project: str | None = None) -> tuple[dict[str, Any], Path | None]:
    """Run Memory Pulse for `--project` or the current repository if registered."""
    if store is None:
        if project is not None:
            from cairnmind.memory.pulse import run_pulse

            return run_pulse(settings, project), None
        return {"status": "not_checked", "reason": "Store is not readable; Memory Pulse skipped."}, None

    selected, root = resolve_doctor_project_root(store, project)
    if selected is None or root is None:
        if project is not None:
            from cairnmind.memory.pulse import run_pulse

            return run_pulse(settings, project, store=store), None
        return {
            "status": "not_checked",
            "reason": "Current directory is not inside a registered project; pass --project PROJECT to check Memory Pulse.",
        }, None

    from cairnmind.memory.pulse import run_pulse

    report = run_pulse(settings, selected["id"], worktree=root, store=store)
    return report, root


def doctor_mcp_presets(project_root: Path | None = None) -> list[dict[str, Any]]:
    """List configured Cairnmind MCP client presets (project + user scopes)."""
    return discover_configured_presets(project_root=project_root)


def doctor_mcp_handshake(
    settings: Settings,
    project_root: Path | None = None,
    *,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Run a bounded (default 5s) stdio JSON-RPC handshake probe for `cairnmind doctor --check-mcp`."""
    target_root = (project_root or Path.cwd()).expanduser().resolve()
    entry, _ = build_server_entry(
        "generic",
        settings,
        target_root,
        scope="stdout",
        external=True,
    )
    return probe_mcp_stdio(
        entry["command"],
        entry["args"],
        env=entry.get("env"),
        cwd=target_root,
        timeout=min(max(0.5, float(timeout)), 5.0),
        settings=settings,
        project_root=target_root,
    )
