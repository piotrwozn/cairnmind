"""Build the guarded 0.7.0 -> 0.7.1 release; output-dir supports candidate builds."""
from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import tomllib
import zipfile
import zlib

ROOT = Path('/workspace/devbrain')
BASELINE = Path('/workspace/devbrain-0.7.0.zip')
BASELINE_SHA256 = 'e02ca74807b74942e718d91272659563c4cb17f8dc812df4e7bd63ad1dc0c8e5'
VERSION = '0.7.1'
SUPPORTED = '0.7.0'
SKIP = {'node_modules', '__pycache__', '.git', '.venv', 'dist', 'test-results', 'playwright-report', '.vite', '.pytest_cache', '.ruff_cache'}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def atomic(path, raw):
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=Path('/workspace'))
    args = parser.parse_args()
    output = args.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    if digest(BASELINE.read_bytes()) != BASELINE_SHA256:
        raise ValueError('The immutable 0.7.0 baseline ZIP changed')
    with zipfile.ZipFile(BASELINE) as archive:
        before = {name.removeprefix('devbrain/'): archive.read(name)
                  for name in archive.namelist() if not name.endswith('/')}
    paths = set(before)
    for directory in ('src', 'tests', 'docs', 'scripts', 'pilot', 'frontend'):
        paths.update(path.relative_to(ROOT).as_posix() for path in (ROOT / directory).rglob('*')
                     if path.is_file() and not any(part in SKIP for part in path.relative_to(ROOT).parts)
                     and path.suffix not in {'.pyc', '.tsbuildinfo'})
    current = {}
    for relative in sorted(paths):
        path = ROOT / relative
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(ROOT):
            raise ValueError('Missing or unsafe release source: ' + relative)
        current[relative] = path.read_bytes()
    if tomllib.loads(current['pyproject.toml'].decode())['project']['version'] != VERSION:
        raise ValueError('Set and lock target package version before building')
    changes = []
    for relative, raw in current.items():
        original = before.get(relative)
        if original == raw:
            continue
        try:
            content, encoding = raw.decode('utf-8'), 'utf-8'
        except UnicodeDecodeError:
            content, encoding = base64.b64encode(raw).decode(), 'base64'
        original_hash = digest(original) if original is not None else None
        changes.append({'path': relative, 'before': [original_hash], 'before_by_version': {SUPPORTED: original_hash},
                        'after': digest(raw), 'encoding': encoding, 'content': content})
    package = {'version': VERSION, 'supported_versions': [SUPPORTED], 'files': changes}
    payload = base64.b64encode(zlib.compress(json.dumps(package, ensure_ascii=False).encode(), 9)).decode()
    updater_content = Path('/workspace/devbrain-update071-template.py').read_text().replace('__PAYLOAD__', payload)
    ast.parse(updater_content)
    # Never publish a package assembled across concurrently changing sources.
    for relative, raw in current.items():
        if (ROOT / relative).read_bytes() != raw:
            raise ValueError('Source changed during release build: ' + relative)
    updater = output / 'devbrain-update-0.7.1.py'
    atomic(updater, updater_content.encode())
    archive_path = output / 'devbrain-0.7.1.zip'
    fd, temporary_name = tempfile.mkstemp(prefix='.devbrain-release-', dir=output)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for relative, raw in current.items():
                info = zipfile.ZipInfo('devbrain/' + relative, date_time=(2026, 11, 7, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, raw)
        os.replace(temporary, archive_path)
    finally:
        temporary.unlink(missing_ok=True)
    report = {'version': VERSION, 'supported_versions': [SUPPORTED],
              'baselines': {SUPPORTED: {'file': BASELINE.name, 'sha256': BASELINE_SHA256}},
              'files': [{key: value for key, value in row.items() if key != 'content'} for row in changes],
              'source_files': len(current),
              'updater_bytes': updater.stat().st_size, 'updater_sha256': digest(updater.read_bytes()),
              'zip_bytes': archive_path.stat().st_size, 'zip_sha256': digest(archive_path.read_bytes())}
    atomic(output / 'devbrain-update-0.7.1-manifest.json', json.dumps(report, indent=2).encode())
    atomic(Path(str(archive_path) + '.sha256'), (report['zip_sha256'] + '  ' + archive_path.name + '\n').encode())
    print(json.dumps({**report, 'files': [row['path'] for row in changes]}, indent=2))


if __name__ == '__main__':
    main()
