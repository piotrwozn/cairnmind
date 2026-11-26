"""Fast-tier tests for SCIP language detection, pinned SHA-256 downloads, isolation, and Docker fallback."""
from __future__ import annotations

import functools
import gzip
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import io
import os
from pathlib import Path
import sys
import tarfile
import threading
import zipfile

import pytest

from cairnmind.config import Settings
from cairnmind.private_files import IS_WINDOWS
from cairnmind.scip_manager import (
    ScipChecksumError,
    ScipError,
    ScipRunError,
    _indexer_command,
    _run_managed_process,
    ensure_indexer,
    scip_status,
)
from cairnmind.scip_registry import (
    INDEXERS,
    SUPPORTED_LANGUAGES,
    BinaryAsset,
    IndexerSpec,
    detect_project_languages,
)


def test_detect_all_9_languages_and_respect_gitignore(tmp_path):
    repo = tmp_path / "polyglot"
    repo.mkdir()
    (repo / ".gitignore").write_text("ignored_dir/\n*.gen.go\n", encoding="utf-8")
    (repo / "ignored_dir").mkdir()
    (repo / "ignored_dir" / "skip.rs").write_text("fn main() {}\n", encoding="utf-8")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "skip.rb").write_text("puts 1\n", encoding="utf-8")

    # Populate all 9 supported languages
    (repo / "go.mod").write_text("module demo\n", encoding="utf-8")
    (repo / "main.go").write_text("package main\n", encoding="utf-8")
    (repo / "Cargo.toml").write_text('[package]\nname="demo"\n', encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "lib.rs").write_text("pub fn f() {}\n", encoding="utf-8")
    (repo / "App.java").write_text("class App {}\n", encoding="utf-8")
    (repo / "Main.kt").write_text("fun main() {}\n", encoding="utf-8")
    (repo / "util.c").write_text("int add(int a, int b) { return a + b; }\n", encoding="utf-8")
    (repo / "engine.cpp").write_text("int sub(int a, int b) { return a - b; }\n", encoding="utf-8")
    (repo / "compile_commands.json").write_text("[]\n", encoding="utf-8")
    (repo / "App.csproj").write_text("<Project />\n", encoding="utf-8")
    (repo / "Program.cs").write_text("class Program {}\n", encoding="utf-8")
    (repo / "composer.json").write_text("{}\n", encoding="utf-8")
    (repo / "index.php").write_text("<?php echo 1;\n", encoding="utf-8")
    (repo / "Gemfile").write_text("source 'https://rubygems.org'\n", encoding="utf-8")
    (repo / "app.rb").write_text("def hi; end\n", encoding="utf-8")

    detected = detect_project_languages(repo)
    langs = [item["language"] for item in detected]
    assert langs == list(SUPPORTED_LANGUAGES)
    by_lang = {item["language"]: item for item in detected}
    assert by_lang["go"]["manifests"] == ["go.mod"]
    assert by_lang["rust"]["files"] == 1  # ignored_dir/skip.rs excluded by .gitignore
    assert by_lang["ruby"]["files"] == 1  # node_modules/skip.rb excluded
    assert by_lang["cpp"]["compile_commands"].endswith("compile_commands.json")


def _serve_dir(directory: Path):
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


def test_download_sha256_verification_and_archive_formats(tmp_path, monkeypatch):
    serve_root = tmp_path / "mirror"
    serve_root.mkdir()

    # 1. tar.gz asset
    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="bin/fake-go")
        payload = b"#!/bin/sh\necho ok\n"
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    tar_path = serve_root / "fake-go.tar.gz"
    tar_path.write_bytes(tar_bytes.getvalue())
    tar_sha = hashlib.sha256(tar_path.read_bytes()).hexdigest()

    # 2. zip asset
    zip_path = serve_root / "fake-ra.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("fake-ra.exe", b"MZ-FAKE-BINARY")
    zip_sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()

    # 3. gz asset with bad sha256
    gz_path = serve_root / "fake-bad.gz"
    gz_path.write_bytes(gzip.compress(b"corrupted-binary"))

    # 4. zip-slip malicious archive
    evil_zip = serve_root / "evil.zip"
    with zipfile.ZipFile(evil_zip, "w") as zf:
        zf.writestr("../../pwned.exe", b"evil")
    evil_sha = hashlib.sha256(evil_zip.read_bytes()).hexdigest()

    server, base_url = _serve_dir(serve_root)
    try:
        custom_indexers = {
            "fake-tar": IndexerSpec(
                indexer_id="fake-tar",
                version="1.0.0",
                languages=("go",),
                assets={
                    ("linux", "x64"): BinaryAsset(
                        url=f"{base_url}/fake-go.tar.gz",
                        sha256=tar_sha,
                        archive="tar.gz",
                        binary_name="fake-go",
                    )
                },
            ),
            "fake-zip": IndexerSpec(
                indexer_id="fake-zip",
                version="1.0.0",
                languages=("rust",),
                assets={
                    ("windows", "x64"): BinaryAsset(
                        url=f"{base_url}/fake-ra.zip",
                        sha256=zip_sha,
                        archive="zip",
                        binary_name="fake-ra.exe",
                    )
                },
            ),
            "fake-bad": IndexerSpec(
                indexer_id="fake-bad",
                version="1.0.0",
                languages=("ruby",),
                assets={
                    ("linux", "x64"): BinaryAsset(
                        url=f"{base_url}/fake-bad.gz",
                        sha256="0" * 64,
                        archive="gz",
                        binary_name="fake-bad",
                    )
                },
            ),
            "fake-evil": IndexerSpec(
                indexer_id="fake-evil",
                version="1.0.0",
                languages=("c",),
                assets={
                    ("windows", "x64"): BinaryAsset(
                        url=f"{base_url}/evil.zip",
                        sha256=evil_sha,
                        archive="zip",
                        binary_name="pwned.exe",
                    )
                },
            ),
        }
        monkeypatch.setattr("cairnmind.scip_manager.INDEXERS", custom_indexers)
        settings = Settings.load(tmp_path / "data", create=True)

        # Successful tar.gz install
        installed_tar = ensure_indexer(settings, "fake-tar", platform_override=("linux", "x64"))
        assert installed_tar.status == "ready"
        assert installed_tar.executable is not None and installed_tar.executable.is_file()
        assert installed_tar.executable.read_bytes() == b"#!/bin/sh\necho ok\n"
        if not IS_WINDOWS:
            assert (installed_tar.executable.stat().st_mode & 0o777) == 0o700

        # Successful zip install + cache hit when download disabled
        installed_zip = ensure_indexer(settings, "fake-zip", platform_override=("windows", "x64"))
        assert installed_zip.status == "ready"
        assert installed_zip.executable.read_bytes() == b"MZ-FAKE-BINARY"
        cached = ensure_indexer(settings, "fake-zip", allow_download=False, platform_override=("windows", "x64"))
        assert cached.status == "ready"
        assert cached.executable == installed_zip.executable

        # SHA-256 mismatch refused fail-closed
        with pytest.raises(ScipChecksumError, match="SHA-256 mismatch"):
            ensure_indexer(settings, "fake-bad", platform_override=("linux", "x64"))
        assert not (settings.data_dir / "runtime" / "scip" / "fake-bad" / "1.0.0" / "fake-bad").exists()

        # Zip-slip archive refused fail-closed
        with pytest.raises(ScipError, match="Unsafe archive member path"):
            ensure_indexer(settings, "fake-evil", platform_override=("windows", "x64"))
        assert not (settings.data_dir / "runtime" / "scip" / "pwned.exe").exists()
    finally:
        server.shutdown()


def test_docker_fallback_windows_clang_and_ruby_security_flags(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data", create=True)
    settings.scip_docker_fallback = True
    monkeypatch.setattr("cairnmind.scip_manager.shutil.which", lambda name: "/usr/bin/docker" if name == "docker" else None)

    clang_win = ensure_indexer(settings, "scip-clang", allow_download=False, platform_override=("windows", "x64"))
    assert clang_win.status == "docker"
    assert clang_win.mode == "docker"
    assert clang_win.docker_image == INDEXERS["scip-clang"].docker_image

    proj = tmp_path / "cpp_proj"
    proj.mkdir()
    (proj / "compile_commands.json").write_text("[]\n", encoding="utf-8")
    out_file = tmp_path / "out" / "index.scip"
    out_file.parent.mkdir()

    argv, _ = _indexer_command(settings, clang_win, proj, out_file)
    assert "--network=none" in argv
    assert "--rm" in argv
    assert f"{proj}:/workspace:ro" in argv
    assert f"{out_file.parent}:/out:rw" in argv

    # When scip_docker_fallback=False, returns unsupported_platform cleanly without failing
    settings.scip_docker_fallback = False
    ruby_win = ensure_indexer(settings, "scip-ruby", allow_download=False, platform_override=("windows", "x64"))
    assert ruby_win.status == "unsupported_platform"
    assert "cairnmind scip import" in ruby_win.detail


def test_managed_process_timeout_kills_process_tree():
    with pytest.raises(ScipRunError, match="timed out"):
        _run_managed_process([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.4)


def test_scip_status_is_strictly_readonly(tmp_path):
    data_dir = tmp_path / "fresh_data"
    settings = Settings.load(data_dir, create=False)
    report = scip_status(settings)
    assert not data_dir.exists()  # zero filesystem writes
    assert set(report["indexers"].keys()) == set(INDEXERS.keys())
    assert report["mode"] in {"auto", "manual", "off"}
    assert "platform" in report


def test_config_scip_env_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRNMIND_SCIP_MODE", "manual")
    monkeypatch.setenv("CAIRNMIND_SCIP_TIMEOUT", "180")
    monkeypatch.setenv("CAIRNMIND_SCIP_DOCKER_FALLBACK", "false")
    loaded = Settings.load(tmp_path / "data", create=False)
    assert loaded.scip_mode == "manual"
    assert loaded.scip_timeout == 180.0
    assert loaded.scip_docker_fallback is False

    monkeypatch.setenv("CAIRNMIND_SCIP_MODE", "bogus")
    with pytest.raises(ValueError, match="auto, manual, off"):
        Settings.load(tmp_path / "data", create=False)
    os.environ.pop("CAIRNMIND_SCIP_MODE", None)
