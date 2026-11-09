"""Synchronize the final test-only correction without opening the user's database."""
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from zipfile import ZipFile


def main():
    root = Path(__file__).resolve().parents[1]
    relative = "tests/test_native_evaluation.py"
    target = Path.home() / "src/devbrain" / relative
    with ZipFile(root / "releases/devbrain-0.7.1.zip") as archive:
        before = archive.read("devbrain/" + relative)
    with ZipFile(root / "releases/devbrain-0.7.2.zip") as archive:
        after = archive.read("devbrain/" + relative)
    if target.is_symlink() or not target.is_file():
        raise ValueError("The installed test is not a regular file")
    current = target.read_bytes()
    if current not in (before, after):
        raise ValueError("The installed test has local changes; nothing was overwritten")
    backup = None
    if current != after:
        backup = Path.home() / ".local/state/devbrain-updates" / (
            time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-final-test-sync")
        backup.mkdir(mode=0o700)
        saved = backup / "test_native_evaluation.py"
        saved.write_bytes(current)
        saved.chmod(0o600)
        fd, name = tempfile.mkstemp(prefix=".devbrain-test-", dir=target.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(after)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(target.stat().st_mode & 0o777)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    report = {
        "status": "passed", "path": str(target), "only_test_changed": True,
        "database_opened": False, "installed_application_changed": False,
        "updater_receipt_changed": False, "backup": str(backup) if backup else None,
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    }
    (root / "evidence/final-native-test-source-sync-20261107.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
