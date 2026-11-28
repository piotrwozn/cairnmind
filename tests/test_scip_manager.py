"""Fast-tier tests for SCIP language detection, pinned SHA-256 downloads, isolation, and Docker fallback."""
from __future__ import annotations

import functools
import gzip
import hashlib
import io
import os
import sys
import tarfile
import threading
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from cairnmind.config import Settings
from cairnmind.pal.private_files import IS_WINDOWS
from cairnmind.scip.scip_manager import (
    ScipChecksumError,
    ScipError,
    ScipRunError,
    _indexer_command,
    _run_managed_process,
    ensure_indexer,
    scip_status,
)
from cairnmind.scip.scip_registry import (
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
        monkeypatch.setattr("cairnmind.scip.scip_manager.INDEXERS", custom_indexers)
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
    monkeypatch.setattr("cairnmind.scip.scip_manager.shutil.which", lambda name: "/usr/bin/docker" if name == "docker" else None)

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
    monkeypatch.setenv("CAIRNMIND_SCIP_MODE", "auto")
    monkeypatch.setenv("CAIRNMIND_SCIP_TIMEOUT", "-5")
    with pytest.raises(ValueError, match="positive number"):
        Settings.load(tmp_path / "data", create=False)
    monkeypatch.setenv("CAIRNMIND_SCIP_TIMEOUT", "60")
    monkeypatch.setenv("CAIRNMIND_SCIP_DOCKER_FALLBACK", "maybe")
    with pytest.raises(ValueError, match="true or false"):
        Settings.load(tmp_path / "data", create=False)
    os.environ.pop("CAIRNMIND_SCIP_MODE", None)


def test_registry_platform_compdb_header_only_and_dedupe(tmp_path, monkeypatch):
    from cairnmind.scip import scip_registry as sr

    for plat, mach, expected in (
        ("win32", "AMD64", ("windows", "x64")),
        ("darwin", "arm64", ("darwin", "arm64")),
        ("linux", "aarch64", ("linux", "arm64")),
        ("linux", "x86_64", ("linux", "x64")),
    ):
        monkeypatch.setattr(sr.sys, "platform", plat)
        monkeypatch.setattr(sr.platform, "machine", lambda m=mach: m)
        assert sr.host_platform() == expected

    # Non-existent dir -> []
    assert sr.detect_project_languages(tmp_path / "does_not_exist") == []

    # Header-only .h beside .cpp should NOT trigger 'c' separately; find_compile_commands in build/
    cpp_only = tmp_path / "cpp_only"
    cpp_only.mkdir()
    (cpp_only / "build").mkdir()
    (cpp_only / "build" / "compile_commands.json").write_text("[]\n", encoding="utf-8")
    (cpp_only / "foo.h").write_text("#pragma once\n", encoding="utf-8")
    (cpp_only / "foo.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    detected = sr.detect_project_languages(cpp_only)
    assert [d["language"] for d in detected] == ["cpp"]
    assert detected[0]["compile_commands"].endswith("compile_commands.json")

    # No compile_commands -> None
    empty = tmp_path / "empty_dir"
    empty.mkdir()
    assert sr.find_compile_commands(empty) is None

    # max_files truncation
    many = tmp_path / "many"
    many.mkdir()
    for i in range(5):
        (many / f"f{i}.go").write_text("package p\n", encoding="utf-8")
    assert sr.detect_project_languages(many, max_files=2)[0]["files"] == 2

    # indexers_for_languages deduplicates java+kotlin -> scip-java, c+cpp -> scip-clang
    assert sr.indexers_for_languages(["java", "kotlin", "c", "cpp", "unknown"]) == ["scip-java", "scip-clang"]


def test_scip_manager_urls_archives_ecosystems_and_commands(tmp_path, monkeypatch):
    from cairnmind.scip import scip_manager as sm

    # 1. URL validation & redirect guard
    sm._validate_download_url("https://github.com/sourcegraph/scip-go")
    sm._validate_download_url("http://127.0.0.1:8080/a.gz")
    with pytest.raises(ScipError, match="non-HTTPS"):
        sm._validate_download_url("http://example.com/evil.tar.gz")
    handler = sm._SafeRedirectHandler()
    with pytest.raises(ScipError, match="non-HTTPS"):
        handler.redirect_request(None, None, 302, "Found", {}, "http://evil.example/a")

    # 2. Archive error cases (raw, gz, tar missing/symlink, zip missing/symlink, unsupported, size cap)
    inst_dir = tmp_path / "inst"
    inst_dir.mkdir()
    raw_src = tmp_path / "raw.bin"
    raw_src.write_bytes(b"RAW-BIN")
    raw_out = sm._extract_asset(raw_src, BinaryAsset("https://x", "0" * 64, "raw", "tool.jar"), inst_dir)
    assert raw_out.read_bytes() == b"RAW-BIN"

    gz_src = tmp_path / "a.gz"
    gz_src.write_bytes(gzip.compress(b"GZ-BIN"))
    gz_out = sm._extract_asset(gz_src, BinaryAsset("https://x", "0" * 64, "gz", "tool-gz"), inst_dir)
    assert gz_out.read_bytes() == b"GZ-BIN"

    # tar with symlink refused
    sym_tar = tmp_path / "sym.tar.gz"
    with tarfile.open(sym_tar, "w:gz") as tf:
        ti = tarfile.TarInfo("link")
        ti.type = tarfile.SYMTYPE
        ti.linkname = "/etc/passwd"
        tf.addfile(ti)
    with pytest.raises(ScipError, match="special tar member"):
        sm._extract_asset(sym_tar, BinaryAsset("https://x", "0" * 64, "tar.gz", "link"), inst_dir)

    # tar missing target binary
    miss_tar = tmp_path / "miss.tar.gz"
    with tarfile.open(miss_tar, "w:gz") as tf:
        ti = tarfile.TarInfo("other")
        ti.size = 1
        tf.addfile(ti, io.BytesIO(b"x"))
    with pytest.raises(ScipError, match="not found in archive"):
        sm._extract_asset(miss_tar, BinaryAsset("https://x", "0" * 64, "tar.gz", "wanted"), inst_dir)

    # zip with symlink refused & missing binary
    sym_zip = tmp_path / "sym.zip"
    with zipfile.ZipFile(sym_zip, "w") as zf:
        zi = zipfile.ZipInfo("link")
        zi.external_attr = 0o120777 << 16
        zf.writestr(zi, "target")
    with pytest.raises(ScipError, match="symlink in zip"):
        sm._extract_asset(sym_zip, BinaryAsset("https://x", "0" * 64, "zip", "link"), inst_dir)

    miss_zip = tmp_path / "miss.zip"
    with zipfile.ZipFile(miss_zip, "w") as zf:
        zf.writestr("sub/", "")
        zf.writestr("sub/other", b"x")
    with pytest.raises(ScipError, match="not found in zip"):
        sm._extract_asset(miss_zip, BinaryAsset("https://x", "0" * 64, "zip", "wanted"), inst_dir)

    bad_fmt = tmp_path / "bad.7z"
    bad_fmt.write_bytes(b"123")
    with pytest.raises(ScipError, match="Unsupported archive format"):
        sm._extract_asset(bad_fmt, BinaryAsset("https://x", "0" * 64, "7z", "x"), inst_dir)

    # Decompression bomb cap
    monkeypatch.setattr(sm, "MAX_EXTRACTED_BYTES", 4)
    big_gz = tmp_path / "big.gz"
    big_gz.write_bytes(gzip.compress(b"0123456789"))
    with pytest.raises(ScipError, match="Extracted indexer exceeds"):
        sm._extract_asset(big_gz, BinaryAsset("https://x", "0" * 64, "gz", "big"), inst_dir)
    monkeypatch.setattr(sm, "MAX_EXTRACTED_BYTES", 512 * 1024 * 1024)

    # 3. Ecosystem installers (go, dotnet, composer) mocked cleanly
    settings = Settings.load(tmp_path / "data", create=True)
    monkeypatch.setattr(sm.shutil, "which", lambda tool: f"/fake/{tool}")

    def fake_install_proc(argv, *, cwd=None, env=None, timeout=300.0):
        if argv[0].endswith("go"):
            bin_dir = Path(env["GOBIN"])
            (bin_dir / "scip-go.exe").write_bytes(b"go-bin")
            return 0, "", ""
        if argv[0].endswith("dotnet"):
            tool_path = Path(argv[argv.index("--tool-path") + 1])
            (tool_path / "scip-dotnet").write_bytes(b"dotnet-bin")
            return 0, "", ""
        if argv[0].endswith("composer"):
            target = Path(cwd) / "vendor" / "bin" / "scip-php"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"php-bin")
            return 0, "", ""
        return 1, "", "unexpected"

    monkeypatch.setattr(sm, "_run_managed_process", fake_install_proc)
    go_inst = ensure_indexer(settings, "scip-go", platform_override=("windows", "x64"))
    assert go_inst.status == "ready" and go_inst.mode == "ecosystem" and go_inst.to_dict()["indexer_id"] == "scip-go"
    dn_inst = ensure_indexer(settings, "scip-dotnet", platform_override=("linux", "x64"))
    assert dn_inst.status == "ready" and dn_inst.mode == "ecosystem"
    php_inst = ensure_indexer(settings, "scip-php", platform_override=("linux", "x64"))
    assert php_inst.status == "ready" and php_inst.mode == "ecosystem"
    # Second call hits cache even with allow_download=False
    assert ensure_indexer(settings, "scip-dotnet", allow_download=False, platform_override=("linux", "x64")).status == "ready"

    # Unknown indexer, download_disabled, missing_toolchain
    with pytest.raises(ValueError, match="Unknown SCIP indexer"):
        ensure_indexer(settings, "scip-unknown")
    assert ensure_indexer(settings, "rust-analyzer", allow_download=False, platform_override=("linux", "x64")).status == "download_disabled"
    monkeypatch.setattr(sm.shutil, "which", lambda tool: None)
    assert ensure_indexer(settings, "scip-dotnet", allow_download=False, platform_override=("linux", "x64")).status == "missing_toolchain"

    # 4. _indexer_command for all 7 indexers + Docker + errors
    monkeypatch.setattr(sm.shutil, "which", lambda tool: f"/fake/{tool}")
    monkeypatch.setattr(sm, "_resolve_java_for_scip", lambda s: "/fake/java")
    proj = tmp_path / "proj"
    proj.mkdir()
    out_scip = tmp_path / "tmp" / "index.scip"
    out_scip.parent.mkdir()
    exe = tmp_path / "bin"
    exe.write_bytes(b"x")

    for iid in ("scip-go", "rust-analyzer", "scip-java", "scip-dotnet", "scip-php", "scip-ruby"):
        cmd, cwd = _indexer_command(settings, sm.InstalledIndexer(iid, "1", "ready", "native", exe), proj, out_scip)
        assert cwd == proj and any(str(out_scip) in part for part in cmd)

    # scip-clang requires compile_commands.json (both native and docker)
    clang_native = sm.InstalledIndexer("scip-clang", "1", "ready", "native", exe)
    with pytest.raises(ScipRunError, match="compile_commands.json"):
        _indexer_command(settings, clang_native, proj, out_scip)
    (proj / "compile_commands.json").write_text("[]\n", encoding="utf-8")
    cmd, _ = _indexer_command(settings, clang_native, proj, out_scip)
    assert "--compdb-path" in cmd

    ruby_docker = sm.InstalledIndexer("scip-ruby", "1", "docker", "docker", docker_image="ghcr.io/x/scip-ruby:v1")
    dcmd, _ = _indexer_command(settings, ruby_docker, proj, out_scip)
    assert "--network=none" in dcmd and "--index-file" in dcmd

    # 5. run_indexer success, non-zero exit, empty file, missing root, cleanup_temp_index
    with pytest.raises(ScipRunError, match="does not exist"):
        sm.run_indexer(settings, "scip-go", tmp_path / "no_such_dir")

    def fake_run_ok(argv, *, cwd=None, env=None, timeout=300.0):
        out_path = Path(argv[argv.index("--output") + 1])
        out_path.write_bytes(b"SCIP-DATA")
        return 0, "", ""

    monkeypatch.setattr(sm, "_run_managed_process", fake_run_ok)
    produced = sm.run_indexer(settings, "scip-go", proj, installed=go_inst)
    assert produced.is_file() and produced.read_bytes() == b"SCIP-DATA"
    sm.cleanup_temp_index(produced)
    assert not produced.parent.exists()

    monkeypatch.setattr(sm, "_run_managed_process", lambda *a, **k: (2, "", "boom"))
    with pytest.raises(ScipRunError, match="exited with code 2"):
        sm.run_indexer(settings, "scip-go", proj, installed=go_inst)

    monkeypatch.setattr(sm, "_run_managed_process", lambda *a, **k: (0, "", ""))
    with pytest.raises(ScipRunError, match="without writing"):
        sm.run_indexer(settings, "scip-go", proj, installed=go_inst)
