"""The devbrain command is gone in 0.13.0; what an upgrade needs stays (#314)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install-wsl.sh"
BASH = "/bin/bash" if Path("/bin/bash").exists() else shutil.which("bash")


def test_the_package_installs_one_command():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["scripts"] == {"cairnmind": "cairnmind.cli:entrypoint"}
    # The import alias that DevBrain 0.8.0 entries run through is still packaged.
    assert data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/cairnmind", "src/devbrain"]


def _leftover_function():
    script = SCRIPT.read_text(encoding="utf-8")
    start = script.index("leftover_devbrain() {")
    return script[start:script.index("\n}\n", start) + 3]


def test_a_devbrain_left_in_the_tool_folder_is_named_and_not_removed(tmp_path):
    function = _leftover_function()
    folder = tmp_path / "bin"
    folder.mkdir()
    empty_path = tmp_path / "no-tools"
    empty_path.mkdir()
    leftover = folder / "devbrain"

    def run():
        return subprocess.run(
            [BASH, "-c", "set -euo pipefail\n" + function + f'\nleftover_devbrain "{folder}"\necho continued'],
            env={"PATH": str(empty_path)}, capture_output=True, text=True, timeout=30)

    result = run()
    assert (result.returncode, result.stdout, result.stderr) == (0, "continued\n", "")

    named = (f'Cairnmind no longer installs the devbrain command, but {leftover} is still there. '
             f'Use cairnmind instead; if nothing else uses that file, remove it: rm "{leftover}"\n')
    leftover.symlink_to(tmp_path / "gone" / "devbrain")  # a link that points nowhere
    result = run()
    assert (result.returncode, result.stdout, result.stderr) == (0, "continued\n", named)
    assert os.path.lexists(leftover)

    leftover.unlink()
    leftover.write_text("#!/bin/sh\n")
    leftover.chmod(0o755)
    result = run()
    assert (result.returncode, result.stdout, result.stderr) == (0, "continued\n", named)
    assert leftover.exists()


def test_the_installer_looks_for_the_leftover_after_installing_and_still_replaces_devbrain_0_8_0():
    script = SCRIPT.read_text(encoding="utf-8")
    install = script.index("uv tool install --python")
    assert install < script.index('leftover_devbrain "$tool_bin_dir"') < script.index("stored_engine_mode() {")
    stop = script.index('stop_installed "$(uv tool dir --bin)/devbrain" devbrain "$(uv tool dir)/devbrain/bin/python"')
    assert stop < script.index("uv tool uninstall devbrain") < install
    subprocess.run([BASH, "-n", str(SCRIPT)], check=True)
