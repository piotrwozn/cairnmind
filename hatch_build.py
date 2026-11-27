"""Hatchling wheel build hook that bundles the compiled React UI (`ui/dist`) as `cairnmind/ui_dist`.

Behavior:
- When `CAIRNMIND_BUILD_UI=1` is set (CI and release builds), runs `npm ci && npm run build`
  inside `ui/` if `ui/dist/index.html` is not yet built, and fails the build if the bundle is
  still missing.
- Whenever `ui/dist/index.html` exists on disk, includes the entire `ui/dist/` tree inside
  the wheel under `cairnmind/ui_dist/`, so `uv tool install cairnmind && cairnmind ui` works
  out of the box with zero Node.js requirement on the user's machine.
- During normal local Python-only `uv sync` / editable installs (without `CAIRNMIND_BUILD_UI=1`
  and without a pre-built `ui/dist`), skips the npm build so Python development stays fast.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

try:
    from hatchling.builders.hooks.plugin.interface import BuildHookInterface
except ImportError:  # pragma: no cover - runtime test env without PEP 517 build backend installed
    class BuildHookInterface:  # type: ignore[no-redef]
        def __init__(self, root: str, config: dict[str, Any], build_config: Any, metadata: Any, directory: str, target_name: str) -> None:
            self.root = root
            self.config = config
            self.build_config = build_config
            self.metadata = metadata
            self.directory = directory
            self.target_name = target_name


def bundle_ui_dist(root: Path, build_data: dict[str, Any], *, target_name: str = "wheel") -> None:
    """Attach `ui/dist` into `build_data['force_include']` for wheel targets."""
    if target_name != "wheel":
        return

    ui_dir = root / "ui"
    dist_dir = ui_dir / "dist"
    index_html = dist_dir / "index.html"
    require_build = os.environ.get("CAIRNMIND_BUILD_UI", "").strip() in {"1", "true", "TRUE", "yes"}

    if require_build and not index_html.is_file():
        npm = shutil.which("npm")
        if not npm:
            raise RuntimeError(
                "CAIRNMIND_BUILD_UI=1 requires `npm` on PATH to build `ui/dist`."
            )
        subprocess.run([npm, "ci"], cwd=ui_dir, check=True)
        subprocess.run([npm, "run", "build"], cwd=ui_dir, check=True)

    if index_html.is_file():
        force_include = build_data.setdefault("force_include", {})
        force_include[str(dist_dir)] = "cairnmind/ui_dist"
    elif require_build:
        raise RuntimeError(
            f"CAIRNMIND_BUILD_UI=1 was set, but {index_html} was not produced."
        )


class CustomBuildHook(BuildHookInterface):
    """Bundle `ui/dist` into `cairnmind/ui_dist` during wheel builds."""

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        bundle_ui_dist(Path(self.root), build_data, target_name=self.target_name)
