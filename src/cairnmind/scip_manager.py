"""Download, verify, isolate, and execute pinned SCIP indexers across Linux, macOS, and Windows.

Security invariants:
- Direct binary downloads are verified against exact pinned SHA-256 digests before use.
- Non-loopback downloads require HTTPS with TLS verification enabled.
- All downloaded binaries, toolchains, and temporary index outputs live under
  ``<data_dir>/runtime/scip/`` with owner-only private permissions.
- Archive extraction rejects absolute paths, path traversal (``..``), symlinks, and
  decompression bombs.
- Indexer subprocesses never write ``index.scip`` into the user's git working tree; they
  run in isolated process groups/Job Objects killed completely on timeout.
- Optional Docker fallback (for indexers without native binaries on a host, such as
  ``scip-clang`` and ``scip-ruby`` on Windows) runs with ``--network=none`` and mounts
  the project root read-only (``:ro``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import gzip
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import subprocess
import tarfile
import time
from typing import Any
from urllib.parse import urlparse
import urllib.request
from uuid import uuid4
import zipfile

from .config import Settings
from .file_locks import locked_path
from .private_files import (
    atomic_replace,
    ensure_private_dir,
    is_reparse_point,
    make_executable,
    make_private,
    write_durable_private,
)
from .process_control import ProcessJob, group_popen_kwargs
from .scip_registry import (
    INDEXERS,
    BinaryAsset,
    IndexerSpec,
    detect_project_languages,
    find_compile_commands,
    host_platform,
)

MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024  # 256 MiB compressed asset ceiling
MAX_EXTRACTED_BYTES = 512 * 1024 * 1024  # 512 MiB extracted binary ceiling
_COPY_CHUNK_BYTES = 1024 * 1024
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class ScipError(RuntimeError):
    """Base error for SCIP indexer installation or execution failures."""


class ScipChecksumError(ScipError):
    """Raised when a downloaded indexer asset fails exact SHA-256 verification."""


class ScipRunError(ScipError):
    """Raised when running a SCIP indexer fails or times out."""


@dataclass(slots=True)
class InstalledIndexer:
    """Resolved installation state for one SCIP indexer on the current host."""
    indexer_id: str
    version: str
    status: str  # ready | docker | missing_toolchain | unsupported_platform | download_disabled | install_failed
    mode: str  # native | ecosystem | docker | none
    executable: Path | None = None
    missing_tools: list[str] = field(default_factory=list)
    docker_image: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "indexer_id": self.indexer_id,
            "version": self.version,
            "status": self.status,
            "mode": self.mode,
            "executable": str(self.executable) if self.executable else None,
            "missing_tools": list(self.missing_tools),
            "docker_image": self.docker_image or None,
            "detail": self.detail or None,
        }


def scip_runtime_dir(settings: Settings) -> Path:
    """Private runtime root for cached SCIP indexers and temporary index outputs."""
    return settings.data_dir / "runtime" / "scip"


def _ensure_private_tree(root: Path, *parts: str) -> Path:
    """Create nested private directories under ``root`` without weakening permissions."""
    current = ensure_private_dir(root)
    for part in parts:
        current = ensure_private_dir(current / part)
    return current


def _resolve_java_for_scip(settings: Settings) -> str | None:
    """Locate Java 21+ using Cairnmind's managed runtime first, then PATH."""
    try:
        from .engine import managed_java_executable, resolve_java
        managed = managed_java_executable(settings)
        if managed and Path(managed).is_file():
            return managed
        resolved = resolve_java(settings)
        if resolved:
            return resolved
    except Exception:
        pass
    return shutil.which("java")


def _find_host_tool(settings: Settings, tool: str, *, passive: bool = False) -> str | None:
    if tool == "java":
        if passive:
            configured = settings.java_executable
            if isinstance(configured, str) and configured and Path(configured).is_file():
                return configured
            return shutil.which("java")
        return _resolve_java_for_scip(settings)
    return shutil.which(tool)


def _missing_host_tools(settings: Settings, spec: IndexerSpec, *, passive: bool = False) -> list[str]:
    return [tool for tool in spec.required_tools if not _find_host_tool(settings, tool, passive=passive)]


def _ecosystem_binary_path(install_dir: Path, spec: IndexerSpec, os_name: str) -> Path:
    suffix = ".exe" if os_name == "windows" else ""
    if spec.ecosystem_installer == "go":
        return install_dir / f"scip-go{suffix}"
    if spec.ecosystem_installer == "dotnet":
        return install_dir / f"scip-dotnet{suffix}"
    if spec.ecosystem_installer == "composer":
        return install_dir / "vendor" / "bin" / "scip-php"
    return install_dir / f"{spec.indexer_id}{suffix}"


def _is_usable_file(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink() and not is_reparse_point(path)
    except OSError:
        return False


def _validate_download_url(url: str) -> None:
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme == "https":
        return
    if scheme == "http" and host in _LOOPBACK_HOSTS:
        return
    raise ScipError(f"Refusing non-HTTPS indexer download URL: {url}")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validate every redirect target against the HTTPS-only (or loopback HTTP) rule."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download_verified(url: str, expected_sha256: str, staging_dir: Path) -> Path:
    """Download ``url`` into ``staging_dir``, enforcing size cap and exact SHA-256."""
    _validate_download_url(url)
    tmp_path = staging_dir / f".download-{secrets.token_hex(8)}.tmp"
    hasher = hashlib.sha256()
    total = 0
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    try:
        # Keep the environment's proxy and CA trust for release downloads (as in engine.py).
        with opener.open(url, timeout=120) as response, tmp_path.open("wb") as handle:
            while True:
                chunk = response.read(_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ScipError(
                        f"Downloaded indexer exceeds {MAX_DOWNLOAD_BYTES} bytes limit"
                    )
                hasher.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        make_private(tmp_path)
        actual = hasher.hexdigest().lower()
        if not hmac.compare_digest(actual, expected_sha256.lower()):
            raise ScipChecksumError(
                f"SHA-256 mismatch for {url}: expected {expected_sha256.lower()}, got {actual}"
            )
        return tmp_path
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _safe_member_name(name: str) -> str:
    """Reject absolute or traversal archive member paths and return clean POSIX path."""
    norm = name.replace("\\", "/")
    if not norm or norm.startswith("/") or "\x00" in norm:
        raise ScipError(f"Unsafe archive member path: {name!r}")
    parts = PurePosixPath(norm).parts
    if any(part in ("..", "") for part in parts) or (parts and ":" in parts[0]):
        raise ScipError(f"Unsafe archive member path: {name!r}")
    return norm


def _copy_bounded(src, dst_path: Path) -> None:
    """Copy bytes from stream ``src`` to ``dst_path`` with a strict decompression cap."""
    written = 0
    with dst_path.open("wb") as handle:
        while True:
            chunk = src.read(_COPY_CHUNK_BYTES)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_EXTRACTED_BYTES:
                raise ScipError(f"Extracted indexer exceeds {MAX_EXTRACTED_BYTES} bytes limit")
            handle.write(chunk)
        handle.flush()
        os.fsync(handle.fileno())


def _extract_asset(downloaded: Path, asset: BinaryAsset, install_dir: Path) -> Path:
    """Extract verified ``downloaded`` archive/binary safely into ``install_dir``."""
    target = install_dir / asset.binary_name
    staged = install_dir / f".extract-{secrets.token_hex(8)}.tmp"
    try:
        if asset.archive == "raw":
            atomic_replace(downloaded, target)
            make_executable(target)
            return target
        if asset.archive == "gz":
            with gzip.open(downloaded, "rb") as gz_in:
                _copy_bounded(gz_in, staged)
        elif asset.archive == "tar.gz":
            found = False
            with tarfile.open(downloaded, "r:gz") as archive:
                for member in archive.getmembers():
                    clean = _safe_member_name(member.name)
                    if member.issym() or member.islnk() or member.isdev():
                        raise ScipError(f"Refusing special tar member: {member.name!r}")
                    if member.isfile() and PurePosixPath(clean).name == asset.binary_name:
                        extracted = archive.extractfile(member)
                        if extracted is None:
                            raise ScipError(f"Unable to read tar member {member.name!r}")
                        with extracted:
                            _copy_bounded(extracted, staged)
                        found = True
                        break
            if not found:
                raise ScipError(f"Binary {asset.binary_name!r} not found in archive")
        elif asset.archive == "zip":
            found = False
            with zipfile.ZipFile(downloaded, "r") as archive:
                for info in archive.infolist():
                    clean = _safe_member_name(info.filename)
                    if info.is_dir():
                        continue
                    # Reject Unix symlink entries encoded in zip external_attr
                    unix_mode = (info.external_attr >> 16) & 0o170000
                    if unix_mode == 0o120000:
                        raise ScipError(f"Refusing symlink in zip archive: {info.filename!r}")
                    if PurePosixPath(clean).name == asset.binary_name:
                        with archive.open(info, "r") as zipped:
                            _copy_bounded(zipped, staged)
                        found = True
                        break
            if not found:
                raise ScipError(f"Binary {asset.binary_name!r} not found in zip archive")
        else:
            raise ScipError(f"Unsupported archive format: {asset.archive!r}")
        make_executable(staged)
        atomic_replace(staged, target)
        return target
    finally:
        staged.unlink(missing_ok=True)
        downloaded.unlink(missing_ok=True)


def _run_managed_process(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 300.0,
) -> tuple[int, str, str]:
    """Run subprocess in an isolated process group / Windows Job Object, killing whole tree on timeout."""
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **group_popen_kwargs(),
    )
    job = ProcessJob()
    job.attach(proc.pid)
    try:
        stdout_b, stderr_b = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        job.terminate()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
        raise ScipRunError(f"Command timed out after {timeout:.0f}s: {argv[0]}") from None
    except BaseException:
        job.terminate()
        raise
    finally:
        job.close()
    stdout = (stdout_b or b"").decode("utf-8", "replace")
    stderr = (stderr_b or b"").decode("utf-8", "replace")
    return proc.returncode, stdout, stderr


def _install_ecosystem(
    settings: Settings,
    spec: IndexerSpec,
    install_dir: Path,
    os_name: str,
    timeout: float,
) -> Path:
    """Install an ecosystem-managed SCIP indexer strictly into ``install_dir``."""
    target = _ecosystem_binary_path(install_dir, spec, os_name)
    if spec.ecosystem_installer == "go":
        go_exe = shutil.which("go")
        if not go_exe:
            raise ScipError("go executable is not installed on PATH")
        env = dict(os.environ)
        env["GOBIN"] = str(install_dir)
        rc, _, stderr = _run_managed_process(
            [go_exe, "install", spec.ecosystem_package],
            env=env,
            timeout=timeout,
        )
        if rc != 0 or not _is_usable_file(target):
            raise ScipError(f"go install failed (exit {rc}): {stderr.strip()[-500:]}")
        make_executable(target)
        return target

    if spec.ecosystem_installer == "dotnet":
        dotnet_exe = shutil.which("dotnet")
        if not dotnet_exe:
            raise ScipError("dotnet executable is not installed on PATH")
        rc, stdout, stderr = _run_managed_process(
            [
                dotnet_exe,
                "tool",
                "install",
                "--tool-path",
                str(install_dir),
                spec.ecosystem_package,
                "--version",
                spec.version,
            ],
            timeout=timeout,
        )
        if rc != 0 or not _is_usable_file(target):
            detail = (stderr or stdout).strip()[-500:]
            raise ScipError(f"dotnet tool install failed (exit {rc}): {detail}")
        make_executable(target)
        return target

    if spec.ecosystem_installer == "composer":
        composer_exe = shutil.which("composer")
        if not composer_exe:
            raise ScipError("composer executable is not installed on PATH")
        composer_json = install_dir / "composer.json"
        if not composer_json.exists():
            write_durable_private(
                composer_json,
                json.dumps({"minimum-stability": "dev", "prefer-stable": True}, indent=2) + "\n",
                prefix=".composer-",
            )
        rc, stdout, stderr = _run_managed_process(
            [
                composer_exe,
                "require",
                spec.ecosystem_package,
                "--no-interaction",
                "--no-progress",
                "--prefer-dist",
            ],
            cwd=install_dir,
            timeout=timeout,
        )
        if rc != 0 or not _is_usable_file(target):
            detail = (stderr or stdout).strip()[-500:]
            raise ScipError(f"composer require failed (exit {rc}): {detail}")
        return target

    raise ScipError(f"Unsupported ecosystem installer: {spec.ecosystem_installer!r}")


def ensure_indexer(
    settings: Settings,
    indexer_id: str,
    *,
    allow_download: bool = True,
    platform_override: tuple[str, str] | None = None,
) -> InstalledIndexer:
    """Ensure ``indexer_id`` is available in ``<data_dir>/runtime/scip/`` or via Docker fallback."""
    if indexer_id not in INDEXERS:
        raise ValueError(f"Unknown SCIP indexer: {indexer_id!r}")
    spec = INDEXERS[indexer_id]
    os_name, arch = platform_override or host_platform()
    asset = spec.assets.get((os_name, arch))
    missing_tools = _missing_host_tools(settings, spec)

    runtime_root = scip_runtime_dir(settings)
    install_dir = runtime_root / spec.indexer_id / spec.version

    # 1. Direct pinned binary release asset for this platform.
    if asset is not None:
        target = install_dir / asset.binary_name
        if not _is_usable_file(target):
            if not allow_download:
                return InstalledIndexer(
                    indexer_id=spec.indexer_id,
                    version=spec.version,
                    status="download_disabled",
                    mode="native",
                    missing_tools=missing_tools,
                    detail="Indexer binary is not installed locally and downloading is disabled.",
                )
            _ensure_private_tree(settings.data_dir, "runtime", "scip", spec.indexer_id, spec.version)
            staging_dir = _ensure_private_tree(settings.data_dir, "runtime", "scip", "downloads")
            lock_path = runtime_root / f".{spec.indexer_id}.lock"
            with locked_path(lock_path):
                if not _is_usable_file(target):
                    downloaded = _download_verified(asset.url, asset.sha256, staging_dir)
                    target = _extract_asset(downloaded, asset, install_dir)
                    write_durable_private(
                        install_dir / ".installed.json",
                        json.dumps(
                            {
                                "indexer_id": spec.indexer_id,
                                "version": spec.version,
                                "sha256": asset.sha256,
                                "binary": asset.binary_name,
                                "installed_at": time.time(),
                            },
                            indent=2,
                        )
                        + "\n",
                        prefix=".meta-",
                    )
        if missing_tools:
            return InstalledIndexer(
                indexer_id=spec.indexer_id,
                version=spec.version,
                status="missing_toolchain",
                mode="native",
                executable=target,
                missing_tools=missing_tools,
                detail=f"Missing required host tool(s): {', '.join(missing_tools)}",
            )
        return InstalledIndexer(
            indexer_id=spec.indexer_id,
            version=spec.version,
            status="ready",
            mode="native",
            executable=target,
        )

    # 2. Isolated ecosystem installer (go install, dotnet tool --tool-path, isolated composer).
    if spec.ecosystem_installer:
        target = _ecosystem_binary_path(install_dir, spec, os_name)
        if _is_usable_file(target):
            if missing_tools:
                return InstalledIndexer(
                    indexer_id=spec.indexer_id,
                    version=spec.version,
                    status="missing_toolchain",
                    mode="ecosystem",
                    executable=target,
                    missing_tools=missing_tools,
                    detail=f"Missing required host tool(s): {', '.join(missing_tools)}",
                )
            return InstalledIndexer(
                indexer_id=spec.indexer_id,
                version=spec.version,
                status="ready",
                mode="ecosystem",
                executable=target,
            )
        if missing_tools:
            return InstalledIndexer(
                indexer_id=spec.indexer_id,
                version=spec.version,
                status="missing_toolchain",
                mode="ecosystem",
                missing_tools=missing_tools,
                detail=f"Install {', '.join(missing_tools)} to enable {spec.indexer_id}.",
            )
        if not allow_download:
            return InstalledIndexer(
                indexer_id=spec.indexer_id,
                version=spec.version,
                status="download_disabled",
                mode="ecosystem",
                detail="Indexer is not installed locally and downloading is disabled.",
            )
        _ensure_private_tree(settings.data_dir, "runtime", "scip", spec.indexer_id, spec.version)
        lock_path = runtime_root / f".{spec.indexer_id}.lock"
        with locked_path(lock_path):
            if not _is_usable_file(target):
                target = _install_ecosystem(settings, spec, install_dir, os_name, settings.scip_timeout)
                write_durable_private(
                    install_dir / ".installed.json",
                    json.dumps(
                        {
                            "indexer_id": spec.indexer_id,
                            "version": spec.version,
                            "ecosystem": spec.ecosystem_installer,
                            "package": spec.ecosystem_package,
                            "binary": str(target.relative_to(install_dir)),
                            "installed_at": time.time(),
                        },
                        indent=2,
                    )
                    + "\n",
                    prefix=".meta-",
                )
        return InstalledIndexer(
            indexer_id=spec.indexer_id,
            version=spec.version,
            status="ready",
            mode="ecosystem",
            executable=target,
        )

    # 3. Optional Docker fallback for platforms without native binaries (e.g. scip-clang/scip-ruby on Windows).
    if spec.docker_image and settings.scip_docker_fallback and shutil.which("docker"):
        return InstalledIndexer(
            indexer_id=spec.indexer_id,
            version=spec.version,
            status="docker",
            mode="docker",
            docker_image=spec.docker_image,
            detail=f"Using Docker fallback ({spec.docker_image}) with --network=none.",
        )

    return InstalledIndexer(
        indexer_id=spec.indexer_id,
        version=spec.version,
        status="unsupported_platform",
        mode="none",
        docker_image=spec.docker_image,
        detail=(
            f"{spec.indexer_id} has no native binary for {os_name}/{arch}. "
            + ("Install Docker for offline container fallback or use `cairnmind scip import`."
               if spec.docker_image else "Use `cairnmind scip import` with a CI-generated index.")
        ),
    )


def _indexer_command(
    settings: Settings,
    installed: InstalledIndexer,
    root: Path,
    out_file: Path,
) -> tuple[list[str], Path]:
    """Build exact argv and cwd for executing an installed SCIP indexer."""
    indexer_id = installed.indexer_id
    if installed.mode == "docker":
        docker_exe = shutil.which("docker")
        if not docker_exe:
            raise ScipRunError("Docker executable is not available for SCIP fallback.")
        out_dir = out_file.parent
        docker_prefix = [
            docker_exe,
            "run",
            "--rm",
            "--network=none",
            "-v",
            f"{root}:/workspace:ro",
            "-v",
            f"{out_dir}:/out:rw",
            "-w",
            "/workspace",
            installed.docker_image,
        ]
        if indexer_id == "scip-clang":
            compdb = find_compile_commands(root)
            if compdb is None:
                raise ScipRunError(
                    "C/C++ SCIP indexing requires compile_commands.json "
                    "(e.g. `cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON`) or `cairnmind scip import`."
                )
            rel_compdb = compdb.relative_to(root).as_posix()
            return (
                [*docker_prefix, "--compdb-path", f"/workspace/{rel_compdb}", "--index-output-path", "/out/index.scip"],
                root,
            )
        if indexer_id == "scip-ruby":
            return ([*docker_prefix, "--index-file", "/out/index.scip"], root)
        raise ScipRunError(f"Docker fallback is not configured for {indexer_id}")

    if installed.executable is None:
        raise ScipRunError(f"Indexer {indexer_id} is not ready ({installed.status}): {installed.detail}")
    exe = str(installed.executable)

    if indexer_id == "scip-go":
        return ([exe, "--output", str(out_file)], root)
    if indexer_id == "rust-analyzer":
        return ([exe, "scip", ".", "--output", str(out_file)], root)
    if indexer_id == "scip-java":
        java_exe = _resolve_java_for_scip(settings)
        if not java_exe:
            raise ScipRunError("Java 21+ is required to run scip-java.")
        return ([java_exe, "-jar", exe, "index", "--output", str(out_file)], root)
    if indexer_id == "scip-clang":
        compdb = find_compile_commands(root)
        if compdb is None:
            raise ScipRunError(
                "C/C++ SCIP indexing requires compile_commands.json "
                "(e.g. `cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON`) or `cairnmind scip import`."
            )
        return ([exe, "--compdb-path", str(compdb), "--index-output-path", str(out_file)], root)
    if indexer_id == "scip-dotnet":
        return ([exe, "index", "--output", str(out_file)], root)
    if indexer_id == "scip-php":
        php_exe = shutil.which("php")
        if not php_exe:
            raise ScipRunError("php executable is required to run scip-php.")
        return ([php_exe, exe, "--output", str(out_file)], root)
    if indexer_id == "scip-ruby":
        return ([exe, "--index-file", str(out_file)], root)
    raise ScipRunError(f"No invocation command defined for {indexer_id}")


def run_indexer(
    settings: Settings,
    indexer_id: str,
    root: Path,
    *,
    installed: InstalledIndexer | None = None,
    timeout: float | None = None,
) -> Path:
    """Run ``indexer_id`` against ``root`` and return path to generated ``index.scip``.

    Caller is responsible for deleting the returned file's parent temp directory after
    ingestion (``cleanup_temp_index(path)``).
    """
    root = Path(root).resolve()
    if not root.is_dir():
        raise ScipRunError(f"Project directory does not exist: {root}")
    resolved = installed or ensure_indexer(settings, indexer_id, allow_download=True)
    if resolved.status not in {"ready", "docker"}:
        raise ScipRunError(resolved.detail or f"Indexer {indexer_id} unavailable ({resolved.status})")

    run_dir = _ensure_private_tree(settings.data_dir, "runtime", "scip", "tmp", uuid4().hex)
    out_file = run_dir / "index.scip"
    budget = float(timeout if timeout is not None else settings.scip_timeout)
    try:
        argv, cwd = _indexer_command(settings, resolved, root, out_file)
        rc, stdout, stderr = _run_managed_process(argv, cwd=cwd, timeout=budget)
        if rc != 0:
            snippet = (stderr or stdout).strip()[-800:]
            raise ScipRunError(f"{indexer_id} exited with code {rc}: {snippet}")
        if not _is_usable_file(out_file) or out_file.stat().st_size == 0:
            # Some indexers (e.g. older scip-dotnet/scip-ruby) write index.scip in cwd if --output is ignored.
            raise ScipRunError(f"{indexer_id} completed without writing {out_file.name}")
        return out_file
    except BaseException:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise


def cleanup_temp_index(scip_path: Path) -> None:
    """Remove temporary directory created by ``run_indexer``."""
    parent = Path(scip_path).parent
    if parent.parent.name == "tmp":
        shutil.rmtree(parent, ignore_errors=True)


def scip_status(
    settings: Settings,
    root: Path | None = None,
    *,
    platform_override: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Strictly read-only SCIP readiness report (zero downloads, zero filesystem writes)."""
    os_name, arch = platform_override or host_platform()
    runtime_root = scip_runtime_dir(settings)
    docker_ok = bool(shutil.which("docker"))
    indexers_report: dict[str, dict[str, Any]] = {}

    for indexer_id, spec in INDEXERS.items():
        asset = spec.assets.get((os_name, arch))
        missing_tools = _missing_host_tools(settings, spec, passive=True)
        install_dir = runtime_root / spec.indexer_id / spec.version
        if asset is not None:
            candidate = install_dir / asset.binary_name
            is_installed = _is_usable_file(candidate)
            mode = "native"
        elif spec.ecosystem_installer:
            candidate = _ecosystem_binary_path(install_dir, spec, os_name)
            is_installed = _is_usable_file(candidate)
            mode = "ecosystem"
        elif spec.docker_image and settings.scip_docker_fallback and docker_ok:
            candidate = None
            is_installed = False
            mode = "docker"
        else:
            candidate = None
            is_installed = False
            mode = "none"

        indexers_report[indexer_id] = {
            "version": spec.version,
            "languages": list(spec.languages),
            "installed": is_installed,
            "mode": mode,
            "executable": str(candidate) if (candidate and is_installed) else None,
            "missing_tools": missing_tools,
            "docker_image": spec.docker_image or None,
        }

    report: dict[str, Any] = {
        "mode": settings.scip_mode,
        "timeout": settings.scip_timeout,
        "docker_fallback": settings.scip_docker_fallback,
        "docker_available": docker_ok,
        "platform": f"{os_name}/{arch}",
        "runtime_dir": str(runtime_root),
        "indexers": indexers_report,
    }
    if root is not None:
        report["detected_languages"] = detect_project_languages(root)
    return report
