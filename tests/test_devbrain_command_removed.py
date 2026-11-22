"""The devbrain command is gone in 0.13.0; what an upgrade needs stays (#314)."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_package_installs_one_command():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["scripts"] == {"cairnmind": "cairnmind.cli:entrypoint"}
    # The import alias that DevBrain 0.8.0 entries run through is still packaged.
    assert data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/cairnmind", "src/devbrain"]
