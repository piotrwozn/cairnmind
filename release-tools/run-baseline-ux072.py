"""Read-only visual comparison of immutable 0.7.2 on the qualified new runtime.

Separate entrypoint, never an 0.8 human acceptance. Reuses real UI lifecycle and
attach-only viewer; copies no account files and refuses available native clients.
"""
import hashlib
import importlib.util
from pathlib import Path
import sys
import zipfile

BASELINE_SHA256 = "6b65e7315895b8c44b84bafc9146dfcdae732c3890f959d0e5e58cbf4fd9aa2c"


def helper(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), Path(__file__).with_name(name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def verify(archive, runtime):
    if not archive.is_absolute() or archive.is_symlink():
        raise ValueError("Use the immutable baseline archive without a linked file")
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != BASELINE_SHA256:
            raise ValueError("Immutable 0.7.2 archive differs")
    with zipfile.ZipFile(archive) as bundle:
        files = {name.removeprefix("devbrain/"): bundle.read(name)
                 for name in bundle.namelist() if name.startswith("devbrain/") and not name.endswith("/")}
    builder = helper("build-devbrain-update-080")
    updater = helper("devbrain-update080-template")
    # Same exact base/origin/non-editable/full package/lock pins barrier used by
    # the updater's separately qualified backup-python. No old binary is used.
    return updater.verify_package(Path(sys.executable), runtime, "0.7.2", builder.package_manifest(files),
                                  builder.dependency_pins(files["uv.lock"], files["pyproject.toml"]))


if __name__ == "__main__":
    raise SystemExit(helper("run-interactive-ux080").main(baseline=True))
