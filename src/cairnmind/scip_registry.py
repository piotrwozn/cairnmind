"""Catalog of supported SCIP languages, pinned indexer releases, and project language detection.

Covers 9 languages via 7 official SCIP indexers:
- go          -> scip-go
- rust        -> rust-analyzer
- java/kotlin -> scip-java
- c/cpp       -> scip-clang
- csharp      -> scip-dotnet
- php         -> scip-php (davidrjenni/scip-php)
- ruby        -> scip-ruby

Every direct binary download is pinned to an exact version and SHA-256 digest.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import platform
import sys
from typing import Any

import pathspec

from .private_files import is_reparse_point

# Directories skipped during fast language detection (matches ingest.py conventions).
_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    "target", "vendor", "dist", "build", "out", ".gradle", ".idea", ".vscode",
    "bin", "obj", ".memory", ".cairnmind", ".devbrain",
})

SUPPORTED_LANGUAGES = ("go", "rust", "java", "kotlin", "c", "cpp", "csharp", "php", "ruby")

LANGUAGE_TO_INDEXER: dict[str, str] = {
    "go": "scip-go",
    "rust": "rust-analyzer",
    "java": "scip-java",
    "kotlin": "scip-java",
    "c": "scip-clang",
    "cpp": "scip-clang",
    "csharp": "scip-dotnet",
    "php": "scip-php",
    "ruby": "scip-ruby",
}

EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".cs": "csharp",
    ".php": "php",
    ".rb": "ruby",
}

MANIFEST_HINTS: dict[str, tuple[str, ...]] = {
    "go": ("go.mod",),
    "rust": ("Cargo.toml",),
    "java": ("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "build.sbt"),
    "kotlin": ("build.gradle.kts", "build.gradle", "pom.xml"),
    "c": ("compile_commands.json", "CMakeLists.txt", "meson.build", "Makefile"),
    "cpp": ("compile_commands.json", "CMakeLists.txt", "meson.build", "Makefile"),
    "csharp": (".sln", ".csproj"),  # suffix-matched
    "php": ("composer.json",),
    "ruby": ("Gemfile", "Rakefile"),
}


@dataclass(frozen=True, slots=True)
class BinaryAsset:
    """One pinned downloadable release asset verified by exact SHA-256."""
    url: str
    sha256: str
    archive: str  # "raw" | "tar.gz" | "gz" | "zip"
    binary_name: str  # filename inside archive or target filename when raw/gz


@dataclass(frozen=True, slots=True)
class IndexerSpec:
    """Pinned SCIP indexer specification across Linux, macOS, and Windows."""
    indexer_id: str
    version: str
    languages: tuple[str, ...]
    # Platform key: (sys_platform_norm, arch_norm) -> BinaryAsset
    # sys_platform_norm in {"linux", "darwin", "windows"}; arch_norm in {"x64", "arm64"}
    assets: dict[tuple[str, str], BinaryAsset]
    # Ecosystem installer fallback when no direct prebuilt binary exists for a platform:
    # "go" | "dotnet" | "composer" | None
    ecosystem_installer: str | None = None
    ecosystem_package: str = ""
    # Host tools required at indexing time (e.g. ("go",), ("cargo",), ("java",), ("dotnet",), ("php", "composer"))
    required_tools: tuple[str, ...] = ()
    # Optional pinned Linux Docker image for platforms without a native binary (e.g. scip-clang/scip-ruby on Windows)
    docker_image: str = ""


# Verified upstream release versions & SHA-256 digests (checked against GitHub API).
INDEXERS: dict[str, IndexerSpec] = {
    "scip-go": IndexerSpec(
        indexer_id="scip-go",
        version="v0.2.10",
        languages=("go",),
        assets={
            ("linux", "x64"): BinaryAsset(
                url="https://github.com/sourcegraph/scip-go/releases/download/v0.2.10/scip-go-linux-amd64.tar.gz",
                sha256="880cc963e16bdf0e38fe624b8ed278eeddce32efd81056375a00dc1b6a0cfc93",
                archive="tar.gz",
                binary_name="scip-go",
            ),
            ("linux", "arm64"): BinaryAsset(
                url="https://github.com/sourcegraph/scip-go/releases/download/v0.2.10/scip-go-linux-arm64.tar.gz",
                sha256="ad2d37f19062441ba73ed5192e74d3410b0ee870cfcb5650e92e338f2aef1327",
                archive="tar.gz",
                binary_name="scip-go",
            ),
            ("darwin", "arm64"): BinaryAsset(
                url="https://github.com/sourcegraph/scip-go/releases/download/v0.2.10/scip-go-darwin-arm64.tar.gz",
                sha256="ea57a0a7bcd593aa4a520ddd3eff9503a9dc9c29ae890f503885b4650d976ecd",
                archive="tar.gz",
                binary_name="scip-go",
            ),
        },
        ecosystem_installer="go",
        ecosystem_package="github.com/sourcegraph/scip-go/cmd/scip-go@v0.2.10",
        required_tools=("go",),
    ),
    "rust-analyzer": IndexerSpec(
        indexer_id="rust-analyzer",
        version="2026-11-23",
        languages=("rust",),
        assets={
            ("linux", "x64"): BinaryAsset(
                url="https://github.com/rust-lang/rust-analyzer/releases/download/2026-11-23/rust-analyzer-x86_64-unknown-linux-gnu.gz",
                sha256="99748126069cc1360e2729e45ae7b563a2b885def41c0d600de8e5977a7adf13",
                archive="gz",
                binary_name="rust-analyzer",
            ),
            ("linux", "arm64"): BinaryAsset(
                url="https://github.com/rust-lang/rust-analyzer/releases/download/2026-11-23/rust-analyzer-aarch64-unknown-linux-gnu.gz",
                sha256="cf7e9c2cd34540da92473d47f0866c9515425da01116e7aaca988c9b19408285",
                archive="gz",
                binary_name="rust-analyzer",
            ),
            ("darwin", "x64"): BinaryAsset(
                url="https://github.com/rust-lang/rust-analyzer/releases/download/2026-11-23/rust-analyzer-x86_64-apple-darwin.gz",
                sha256="4673e51749f937567b77813ef12621b6e11e6ba9c1b591c58f8a98fbbc9f5bac",
                archive="gz",
                binary_name="rust-analyzer",
            ),
            ("darwin", "arm64"): BinaryAsset(
                url="https://github.com/rust-lang/rust-analyzer/releases/download/2026-11-23/rust-analyzer-aarch64-apple-darwin.gz",
                sha256="9d996784c9bc811642edd9d993c8c85e12c7111463a8e426d5e74c27da1bc0a5",
                archive="gz",
                binary_name="rust-analyzer",
            ),
            ("windows", "x64"): BinaryAsset(
                url="https://github.com/rust-lang/rust-analyzer/releases/download/2026-11-23/rust-analyzer-x86_64-pc-windows-msvc.zip",
                sha256="6d0c3835155bb1fbee9d93d1eca4270251e08cb9a4621dbcadaf7450a6a4add2",
                archive="zip",
                binary_name="rust-analyzer.exe",
            ),
            ("windows", "arm64"): BinaryAsset(
                url="https://github.com/rust-lang/rust-analyzer/releases/download/2026-11-23/rust-analyzer-aarch64-pc-windows-msvc.zip",
                sha256="c4bee363bec04ca721beb3a90a996d799ecfd94fe8128d65814f6d1ca844343d",
                archive="zip",
                binary_name="rust-analyzer.exe",
            ),
        },
        required_tools=("cargo",),
    ),
    "scip-java": IndexerSpec(
        indexer_id="scip-java",
        version="v0.14.2",
        languages=("java", "kotlin"),
        assets={
            (os_name, arch): BinaryAsset(
                url="https://github.com/sourcegraph/scip-java/releases/download/v0.14.2/scip-java-v0.14.2",
                sha256="6620a8f2af822ccfa23fbf8a852560edd05c3834dc8cf779ca507abce71586dc",
                archive="raw",
                binary_name="scip-java.jar",
            )
            for os_name in ("linux", "darwin", "windows")
            for arch in ("x64", "arm64")
        },
        required_tools=("java",),
    ),
    "scip-clang": IndexerSpec(
        indexer_id="scip-clang",
        version="v0.4.3",
        languages=("c", "cpp"),
        assets={
            ("linux", "x64"): BinaryAsset(
                url="https://github.com/sourcegraph/scip-clang/releases/download/v0.4.3/scip-clang-x86_64-linux",
                sha256="53921e32f4ac7f9aa2f333769fb8b3f694a1a4f7228a4ff0f92c4ce9019b55a2",
                archive="raw",
                binary_name="scip-clang",
            ),
            ("darwin", "arm64"): BinaryAsset(
                url="https://github.com/sourcegraph/scip-clang/releases/download/v0.4.3/scip-clang-arm64-darwin",
                sha256="5e893be2aaa6af28f23990b843cbb4052e1a16a26204f0d6a4f8b58c45c2b626",
                archive="raw",
                binary_name="scip-clang",
            ),
        },
        required_tools=(),
        docker_image="ghcr.io/sourcegraph/scip-clang:v0.4.3",
    ),
    "scip-dotnet": IndexerSpec(
        indexer_id="scip-dotnet",
        version="0.2.14",
        languages=("csharp",),
        assets={},
        ecosystem_installer="dotnet",
        ecosystem_package="scip-dotnet",
        required_tools=("dotnet",),
    ),
    "scip-php": IndexerSpec(
        indexer_id="scip-php",
        version="dev-main",
        languages=("php",),
        assets={},
        ecosystem_installer="composer",
        ecosystem_package="davidrjenni/scip-php",
        required_tools=("php", "composer"),
    ),
    "scip-ruby": IndexerSpec(
        indexer_id="scip-ruby",
        version="scip-ruby-v0.5.1",
        languages=("ruby",),
        assets={
            ("linux", "x64"): BinaryAsset(
                url="https://github.com/sourcegraph/scip-ruby/releases/download/scip-ruby-v0.5.1/scip-ruby-x86_64-linux",
                sha256="3b0eec58c00d2c742e182fc9070a048d933ff6485098bcc3e3c8b28fbcf32c8e",
                archive="raw",
                binary_name="scip-ruby",
            ),
            ("darwin", "arm64"): BinaryAsset(
                url="https://github.com/sourcegraph/scip-ruby/releases/download/scip-ruby-v0.5.1/scip-ruby-arm64-darwin",
                sha256="af0c8be10e714b56eb4a9ac64ec0014e7eff15cca86eb77a88eac58be222ddb1",
                archive="raw",
                binary_name="scip-ruby",
            ),
        },
        required_tools=(),
        docker_image="ghcr.io/sourcegraph/scip-ruby:scip-ruby-v0.5.1",
    ),
}


def host_platform() -> tuple[str, str]:
    """Return normalized (os_name, arch) such as ('windows', 'x64') or ('darwin', 'arm64')."""
    if sys.platform == "win32":
        os_name = "windows"
    elif sys.platform == "darwin":
        os_name = "darwin"
    else:
        os_name = "linux"
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        arch = "arm64"
    else:
        arch = "x64"
    return os_name, arch


def find_compile_commands(root: Path) -> Path | None:
    """Locate ``compile_commands.json`` in standard build locations inside ``root``."""
    for rel in ("compile_commands.json", "build/compile_commands.json",
                "out/compile_commands.json", "cmake-build-debug/compile_commands.json"):
        candidate = root / rel
        if candidate.is_file() and not candidate.is_symlink() and not is_reparse_point(candidate):
            return candidate
    return None


def detect_project_languages(root: Path, *, max_files: int = 5000) -> list[dict[str, Any]]:
    """Fast, safe language detection for SCIP indexing inside ``root``.

    Respects top-level ``.gitignore``, skips symlinks/reparse points and common build
    directories, and returns deduplicated detected languages ordered deterministically.
    """
    root = Path(root).resolve()
    if not root.is_dir():
        return []
    ignore_spec = None
    gitignore = root / ".gitignore"
    if gitignore.is_file() and not gitignore.is_symlink() and not is_reparse_point(gitignore):
        try:
            ignore_spec = pathspec.GitIgnoreSpec.from_lines(
                gitignore.read_text(encoding="utf-8", errors="replace").splitlines()
            )
        except OSError:
            ignore_spec = None

    counts: dict[str, int] = {}
    manifests: dict[str, list[str]] = {}
    scanned = 0

    for current, dirs, names in os.walk(root, followlinks=False):
        folder = Path(current)
        if folder != root and (folder.is_symlink() or is_reparse_point(folder)):
            dirs[:] = []
            continue
        kept_dirs: list[str] = []
        for dname in sorted(dirs):
            if dname in _SKIP_DIRS:
                continue
            child = folder / dname
            if child.is_symlink() or is_reparse_point(child):
                continue
            if ignore_spec is not None:
                rel = child.relative_to(root).as_posix() + "/"
                if ignore_spec.match_file(rel):
                    continue
            kept_dirs.append(dname)
        dirs[:] = kept_dirs

        for fname in sorted(names):
            child = folder / fname
            if child.is_symlink() or is_reparse_point(child):
                continue
            rel = child.relative_to(root).as_posix()
            if ignore_spec is not None and ignore_spec.match_file(rel):
                continue
            scanned += 1
            if scanned > max_files:
                dirs[:] = []
                break
            # Check source extensions
            ext = Path(fname).suffix.lower()
            lang = EXTENSION_TO_LANGUAGE.get(ext)
            if lang:
                counts[lang] = counts.get(lang, 0) + 1
            # Check top-level or near-top-level manifests
            depth = len(Path(rel).parts)
            if depth <= 2:
                for mlang, hints in MANIFEST_HINTS.items():
                    for hint in hints:
                        if (hint.startswith(".") and fname.lower().endswith(hint)) or fname == hint:
                            manifests.setdefault(mlang, []).append(rel)

    detected: list[dict[str, Any]] = []
    for lang in SUPPORTED_LANGUAGES:
        file_count = counts.get(lang, 0)
        found_manifests = sorted(set(manifests.get(lang, ())))
        if file_count == 0:
            continue
        # Header-only `.h` files beside `.cpp` count as C++ rather than triggering C separately.
        if lang == "c" and counts.get("cpp", 0) > 0 and not any(
            p.suffix.lower() == ".c" for p in root.rglob("*.c")
            if not any(part in _SKIP_DIRS for part in p.relative_to(root).parts)
        ):
            continue
        entry: dict[str, Any] = {
            "language": lang,
            "indexer": LANGUAGE_TO_INDEXER[lang],
            "files": file_count,
            "manifests": found_manifests,
        }
        if lang in {"c", "cpp"}:
            compdb = find_compile_commands(root)
            entry["compile_commands"] = str(compdb) if compdb else None
        detected.append(entry)
    return detected


def indexers_for_languages(languages: list[str]) -> list[str]:
    """Deduplicate indexer IDs for a list of language names in deterministic order."""
    seen: list[str] = []
    for lang in languages:
        indexer = LANGUAGE_TO_INDEXER.get(lang.lower())
        if indexer and indexer not in seen:
            seen.append(indexer)
    return seen
