"""The development Python is pinned to the tested one, and the files that name it agree (#228)."""
from __future__ import annotations

from pathlib import Path
import re
import tomllib

ROOT = Path(__file__).resolve().parent.parent


def _pin() -> str:
    return (ROOT / ".python-version").read_text(encoding="utf-8").strip()


def test_python_version_file_pins_the_tested_minor() -> None:
    # Without it uv takes the newest Python on the machine, which can lack wheels (no tree-sitter for 3.15).
    assert re.fullmatch(r"3\.\d+", _pin())
    assert _pin() == "3.12"


def test_pin_satisfies_requires_python_and_matches_the_other_files() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lower = re.fullmatch(r">=(3\.\d+)", pyproject["project"]["requires-python"])
    assert lower and lower.group(1) == _pin()
    assert pyproject["tool"]["ruff"]["target-version"] == "py" + _pin().replace(".", "")
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    assert lock["requires-python"] == pyproject["project"]["requires-python"]


def test_dev_setup_docs_name_the_pin() -> None:
    assert ".python-version" in (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "3.12" in (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
