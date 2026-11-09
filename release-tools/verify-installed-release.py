"""Compare deployed source/package and protected account files to release evidence."""
import argparse
import hashlib
import importlib.metadata
import json
import stat
import tomllib
from pathlib import Path
from zipfile import ZipFile

import devbrain


def fingerprint(path):
    if not path.exists():
        return {"exists": False, "sha256": None, "mode": None}
    return {"exists": True, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mode": stat.S_IMODE(path.stat().st_mode)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = Path.home() / "src/devbrain"
    package = Path(devbrain.__file__).resolve().parent
    baseline = json.loads((Path.home() / ".cache/devbrain-validation-20261107/protected-before-production-update.json").read_text())
    protected = {}
    for relative, before in baseline.items():
        after = fingerprint(Path.home() / relative)
        # The baseline may represent an absent file with only the exists field.
        protected[relative] = (before.get("exists") == after["exists"]
            and (not after["exists"] or all(before.get(key) == after[key] for key in ("sha256", "mode"))))
    source_mismatches, installed_mismatches = [], []
    source_count = installed_count = 0
    with ZipFile(root / "releases/devbrain-0.7.2.zip") as archive:
        for name in archive.namelist():
            relative = Path(name).relative_to("devbrain")
            expected = archive.read(name)
            source_count += 1
            path = source / relative
            if not path.is_file() or path.read_bytes() != expected:
                source_mismatches.append(str(relative))
            if relative.parts[:2] == ("src", "devbrain"):
                installed_count += 1
                path = package.joinpath(*relative.parts[2:])
                if not path.is_file() or path.read_bytes() != expected:
                    installed_mismatches.append(str(relative))
    report = {
        "installed_version": importlib.metadata.version("devbrain"),
        "source_version": tomllib.loads((source / "pyproject.toml").read_text())["project"]["version"],
        "protected_files_unchanged": protected,
        "source_files_verified": source_count,
        "installed_files_verified": installed_count,
        "source_mismatches": source_mismatches,
        "installed_mismatches": installed_mismatches,
        "backup": args.backup,
        "release_updater_sha256": hashlib.sha256((root / "releases/devbrain-update-0.7.2.py").read_bytes()).hexdigest(),
        "applied_updater_sha256": json.loads((root / "evidence/devbrain-0.7.2-production-update.json").read_text())["updater_sha256"],
        "final_test_only_sync": json.loads((root / "evidence/final-native-test-source-sync-20261107.json").read_text()),
        "release_zip_sha256": hashlib.sha256((root / "releases/devbrain-0.7.2.zip").read_bytes()).hexdigest(),
    }
    report["status"] = "passed" if (
        report["installed_version"] == report["source_version"] == "0.7.2"
        and all(protected.values()) and not source_mismatches and not installed_mismatches
        and (Path(args.backup) / "database.devbrain.json.gz").is_file()
    ) else "failed"
    output = root / "evidence/devbrain-0.7.2-production-final.json"
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
