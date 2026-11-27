"""Tests for bundling `ui/dist` into the Cairnmind wheel via `hatch_build.py`."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_hook_cls():
    hook_path = Path(__file__).resolve().parents[1] / "hatch_build.py"
    spec = importlib.util.spec_from_file_location("cairnmind_hatch_build", hook_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.CustomBuildHook


def test_hatch_hook_includes_prebuilt_ui_dist(tmp_path, monkeypatch):
    hook_cls = _load_hook_cls()
    monkeypatch.delenv("CAIRNMIND_BUILD_UI", raising=False)
    dist = tmp_path / "ui" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")

    hook = hook_cls(str(tmp_path), {}, None, None, str(tmp_path), "wheel")
    build_data: dict = {}
    hook.initialize("0.12.0", build_data)
    assert build_data["force_include"][str(dist)] == "cairnmind/ui_dist"


def test_hatch_hook_skips_when_optional_and_missing(tmp_path, monkeypatch):
    hook_cls = _load_hook_cls()
    monkeypatch.delenv("CAIRNMIND_BUILD_UI", raising=False)

    hook = hook_cls(str(tmp_path), {}, None, None, str(tmp_path), "wheel")
    build_data: dict = {}
    hook.initialize("0.12.0", build_data)
    assert "force_include" not in build_data


def test_hatch_hook_requires_npm_when_build_ui_flag_set(tmp_path, monkeypatch):
    hook_cls = _load_hook_cls()
    monkeypatch.setenv("CAIRNMIND_BUILD_UI", "1")
    monkeypatch.setattr("shutil.which", lambda name: None)

    hook = hook_cls(str(tmp_path), {}, None, None, str(tmp_path), "wheel")
    with pytest.raises(RuntimeError, match="CAIRNMIND_BUILD_UI=1 requires"):
        hook.initialize("0.12.0", {})
