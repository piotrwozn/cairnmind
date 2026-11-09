"""Read-only full release-source comparison; no DevBrain import or data access.

Standalone procedural check, NOT a registered qualification controller. A parent
must observe actual exit and native/power/boot grace. A report cannot authorize
installation, repair, retry, or source overwriting. Default execution is inert.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import time
import tomllib
import zipfile

SOURCE = Path("/home/piotr/src/devbrain")
ARTIFACT_BASE = Path("/mnt/c/Users/piotr/AppData/Local/Packages/OpenAI.Codex_2p2nqsd0c76g0/LocalCache/Local/DevBrain/qualification")
GATE_SHA = "a6bd365cea68986ed6795990272c766679bbd29fd9407e904a12c2956bcdf7fb"
EXPECTED = {
    "baseline072": ("0.7.2", "6b65e7315895b8c44b84bafc9146dfcdae732c3890f959d0e5e58cbf4fd9aa2c", 182),
    "target080": ("0.8.0", "070469b5a63e28a19ed1e3d12ac96965c182a03a077dbe410bdcb6071bb35e7f", 198),
}
# Only these exact generated/non-source subtrees are excluded. No glob blanket.
OPAQUE_EXCLUSIONS = {
    ".git", ".venv", ".pytest_cache", ".ruff_cache",
    "frontend/node_modules", "frontend/dist", "frontend/test-results",
    "frontend/playwright-report", "frontend/.vite",
}
CACHE_FILE = re.compile(r"[^/]+\.(?:cpython|pypy)-[0-9]+(?:\.opt-[0-9]+)?\.pyc\Z")
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_ENTRIES = 20000
TIMEOUT_SECONDS = 30


class Refusal(RuntimeError):
    """Fixed safe reason code; never a private file name or value."""


def require(condition, code):
    if not condition:
        raise Refusal(code)


def identity(row):
    return (row.st_dev, row.st_ino, row.st_uid, row.st_mode,
            row.st_nlink, row.st_size, row.st_mtime_ns, row.st_ctime_ns)


def deadline(end):
    require(time.monotonic() < end, "bounded_read_timeout")


def canonical(path, *, directory=False):
    require(path.is_absolute() and path.resolve(strict=True) == path, "noncanonical_path")
    for part in (path, *path.parents):
        require(not part.is_symlink(), "symlink_path")
    require(path.is_dir() if directory else path.is_file(), "wrong_path_type")
    return path


def digest_fd(fd, limit, end):
    before = os.fstat(fd)
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, "nonregular_or_linked_file")
    require(before.st_size <= limit, "read_size_limit")
    checksum = hashlib.sha256()
    total = 0
    while True:
        deadline(end)
        block = os.read(fd, 1024 * 1024)
        if not block:
            break
        total += len(block)
        require(total <= limit, "read_size_limit")
        checksum.update(block)
    require(identity(before) == identity(os.fstat(fd)), "file_changed_during_read")
    return checksum.hexdigest(), before


def read_public(path, end, limit=MAX_ARCHIVE_BYTES):
    canonical(path)
    parent_fd = open_directory_chain(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
        try:
            before = os.fstat(fd)
            require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= limit, "public_input_type_or_size")
            chunks, size = [], 0
            while True:
                deadline(end)
                block = os.read(fd, 1024 * 1024)
                if not block:
                    break
                size += len(block)
                require(size <= limit, "read_size_limit")
                chunks.append(block)
            require(identity(before) == identity(os.fstat(fd)) == identity(os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)), "public_input_changed")
            canonical(path)
            require(identity(before) == identity(os.stat(path, follow_symlinks=False)), "public_input_changed")
            return b"".join(chunks)
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def hash_public(path, end, limit=MAX_ARCHIVE_BYTES):
    return hashlib.sha256(read_public(path, end, limit)).hexdigest()


def release_inventory(path, mode, end):
    version, expected_sha, expected_count = EXPECTED[mode]
    archive_bytes = read_public(path, end)
    require(hashlib.sha256(archive_bytes).hexdigest() == expected_sha, "release_zip_digest_mismatch")
    files, contents, total = {}, {}, 0
    # The immutable ZIP is public release code; its expected digests may be used
    # internally. Neither actual source digests nor source bytes leave this process.
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        for row in archive.infolist():
            deadline(end)
            require(not row.is_dir(), "unexpected_zip_directory")
            name = PurePosixPath(row.filename)
            require(row.filename.startswith("devbrain/") and "\\" not in row.filename
                    and ":" not in row.filename and not name.is_absolute()
                    and ".." not in name.parts and len(name.parts) >= 2,
                    "unsafe_zip_member")
            relative = name.relative_to("devbrain").as_posix()
            require(relative not in files and stat.S_ISREG(row.external_attr >> 16), "duplicate_or_linked_zip_member")
            require(row.file_size <= MAX_FILE_BYTES, "zip_member_size_limit")
            total += row.file_size
            require(total <= MAX_ARCHIVE_BYTES, "zip_total_size_limit")
            value = archive.read(row)
            files[relative] = hashlib.sha256(value).hexdigest()
            if relative == "pyproject.toml":
                contents[relative] = value
    require(len(files) == expected_count and tomllib.loads(contents["pyproject.toml"].decode())["project"]["version"] == version,
            "release_inventory_or_version_mismatch")
    require(hash_public(path, end) == expected_sha, "release_zip_changed")
    return files


def scan_source(expected, end):
    canonical(SOURCE, directory=True)
    root_fd = open_directory_chain(SOURCE)
    root_identity = identity(os.fstat(root_fd))
    expected_dirs = {parent.as_posix() for name in expected for parent in PurePosixPath(name).parents if parent.as_posix() != "."}
    found, directories = {}, {}
    counters = {"missing": 0, "modified": 0, "unexpected": 0, "ignored_subtrees": 0, "ignored_bytecode": 0, "entries": 0}

    def visit(fd, prefix="", bytecode_cache=False):
        before = os.fstat(fd)
        require(stat.S_ISDIR(before.st_mode) and before.st_uid == os.getuid(), "source_directory_owner_or_type")
        directories[prefix] = identity(before)
        with os.scandir(fd) as entries:
            for entry in entries:
                deadline(end)
                counters["entries"] += 1
                require(counters["entries"] <= MAX_ENTRIES, "source_entry_limit")
                row = entry.stat(follow_symlinks=False)
                require(not stat.S_ISLNK(row.st_mode) and row.st_uid == os.getuid(), "source_symlink_or_owner_mismatch")
                name = prefix + "/" + entry.name if prefix else entry.name
                if stat.S_ISDIR(row.st_mode):
                    if not bytecode_cache and name in OPAQUE_EXCLUSIONS:
                        counters["ignored_subtrees"] += 1
                        continue
                    cache = entry.name == "__pycache__" and (prefix == "src" or prefix.startswith("src/") or prefix == "tests" or prefix.startswith("tests/"))
                    if name not in expected_dirs and not cache:
                        counters["unexpected"] += 1
                        continue  # Do not inspect unknown private subtrees.
                    child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                    try:
                        require(identity(os.fstat(child)) == identity(row), "source_directory_replaced")
                        visit(child, name, cache)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(row.st_mode):
                    require(row.st_nlink == 1, "source_hardlink")
                    if bytecode_cache:
                        require(CACHE_FILE.fullmatch(entry.name), "unexpected_cache_file")
                        counters["ignored_bytecode"] += 1
                        continue
                    if name not in expected:
                        counters["unexpected"] += 1
                        continue  # Do not read unrecognized file contents.
                    child = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                    try:
                        require(identity(os.fstat(child)) == identity(row), "source_file_replaced")
                        actual, stable = digest_fd(child, MAX_FILE_BYTES, end)
                        require(identity(os.stat(entry.name, dir_fd=fd, follow_symlinks=False)) == identity(stable), "source_file_replaced")
                        found[name] = identity(stable)
                        if actual != expected[name]:
                            counters["modified"] += 1
                    finally:
                        os.close(child)
                else:
                    raise Refusal("source_special_file")
        require(identity(before) == identity(os.fstat(fd)), "source_directory_changed")
    try:
        require(os.fstat(root_fd).st_uid == os.getuid(), "source_owner_mismatch")
        visit(root_fd)
        canonical(SOURCE, directory=True)
        require(identity(os.stat(SOURCE, follow_symlinks=False)) == root_identity, "source_root_replaced")
    finally:
        os.close(root_fd)
    counters["missing"] = len(set(expected) - found.keys())
    counters["matched"] = len(found) - counters["modified"]
    return counters, found, directories


def open_directory_chain(path):
    """Retain each checked directory while opening its child without following links."""
    require(path.is_absolute() and str(path) == os.path.normpath(str(path)), "noncanonical_directory_chain")
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            expected = os.stat(part, dir_fd=current, follow_symlinks=False)
            require(stat.S_ISDIR(expected.st_mode), "directory_component_not_directory")
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=current)
            try:
                require(identity(expected) == identity(os.fstat(child)), "directory_component_replaced")
            except Exception:
                os.close(child)
                raise
            os.close(current)
            current = child
        result, current = current, None
        return result
    finally:
        if current is not None:
            os.close(current)


def publish(path, report):
    require(path.is_absolute() and path.parent.is_relative_to(ARTIFACT_BASE), "output_outside_qualification")
    canonical(path.parent, directory=True)
    parent_fd = open_directory_chain(path.parent)
    def parent_identity(row):
        # Creating the result necessarily changes directory size/mtime.
        return row.st_dev, row.st_ino, row.st_uid, row.st_mode
    try:
        parent = parent_identity(os.fstat(parent_fd))
        require(parent == parent_identity(os.stat(path.parent, follow_symlinks=False)), "directory_component_replaced")
        # O_EXCL refuses any existing leaf, including a dangling symlink. The
        # retained directory, not a second traversal of the full path, owns writes.
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent_fd)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            require(identity(os.fstat(stream.fileno())) == identity(os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)), "output_leaf_replaced")
        canonical(path.parent, directory=True)
        require(parent == parent_identity(os.fstat(parent_fd)) == parent_identity(os.stat(path.parent, follow_symlinks=False)), "directory_component_replaced")
    finally:
        os.close(parent_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--mode", choices=EXPECTED)
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--release-zip", type=Path)
    parser.add_argument("--runtime-gate", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not args.execute:
        print(json.dumps({"status": "prepared_not_run", "production_read": False, "production_modified": False}))
        return 0
    report = {"schema_version": 1, "scope": "devbrain_readonly_release_source_comparison", "status": "failed", "mode": args.mode,
              "production_modified": False, "app_imported": False, "services_or_models_started": False,
              "source_contents_or_actual_digests_published": False, "deployment_authorized": False,
              "external_actual_exit_observation_required": True, "point_in_time_only": True}
    end = time.monotonic() + TIMEOUT_SECONDS
    started = time.monotonic()
    try:
        require(sys.platform == "linux" and sys.flags.isolated == 1 and sys.dont_write_bytecode, "qualified_isolated_linux_required")
        require(args.source_dir == SOURCE and args.mode in EXPECTED and args.release_zip and args.runtime_gate and args.output,
                "explicit_fixed_inputs_required")
        gate_bytes = read_public(args.runtime_gate, end)
        require(hashlib.sha256(gate_bytes).hexdigest() == GATE_SHA, "runtime_gate_mismatch")
        gate = json.loads(gate_bytes);runtime = gate["runtime"]
        require(gate["status"] == "pass" and Path(sys._base_executable).resolve() == Path(runtime["python_executable"])
                and hash_public(Path(runtime["python_executable"]), end) == runtime["python_binary_sha256"], "qualified_python_mismatch")
        expected = release_inventory(args.release_zip, args.mode, end)
        first, first_files, first_dirs = scan_source(expected, end)
        second, second_files, second_dirs = scan_source(expected, end)
        require(first == second and first_files == second_files and first_dirs == second_dirs, "source_changed_between_passes")
        report.update(expected_file_count=len(expected), expected_package_file_count=sum(x.startswith("src/devbrain/") for x in expected),
                      expected_release_zip_sha256=EXPECTED[args.mode][1], comparison=second, two_pass_identity_stable=True)
        require(second["missing"] == second["modified"] == second["unexpected"] == 0, "release_source_mismatch")
        report["status"] = "matched_pending_external_observation"
        report["comparison_passed"] = True
        code = 0
    except Exception as exc:
        report["comparison_passed"] = False
        report["error_code"] = str(exc) if isinstance(exc, Refusal) else "source_comparison_failed"
        code = 1
    report["elapsed_seconds"] = round(time.monotonic() - started, 6)
    if args.output is not None:
        try:
            publish(args.output, report)
        except Exception:
            print(json.dumps({"status": "failed", "error_code": "report_publication_failed", "production_modified": False}))
            return 1
    print(json.dumps({key: report[key] for key in ("status", "production_modified", "comparison_passed")}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
