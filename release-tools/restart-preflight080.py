"""Read-only restart metadata probe; no app import, service, JVM or model start."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import ssl
import subprocess
import sys
import time


def sha(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for part in iter(lambda: stream.read(1048576), b''):
            value.update(part)
    return value.hexdigest()


def require(value, message):
    if not value:
        raise RuntimeError(message)


def command(argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=5, check=False)
    require(result.returncode == 0, 'Metadata command failed: ' + argv[0])
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    args = parser.parse_args()
    report = {'scope': 'restart080_metadata_only', 'passed': False, 'errors': [],
              'application_started': False, 'model_requests': 0, 'jvm_started': False}
    started = time.monotonic()
    try:
        initial_input_sha = sha(args.input)
        initial_script_sha = sha(__file__)
        expected = json.loads(args.input.read_text())
        report['run_id'] = expected['run_id']
        report['initial_boot_id'] = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        report['kernel'] = platform.release()
        kernel = command(['/usr/bin/dmesg', '--raw'])
        fatal = r'segfault|segmentation fault|fatal signal|SIGSEGV|SIGABRT|invalid opcode|general protection fault|oom-kill|Out of memory|Kernel panic'
        report['kernel_fatals_start'] = sorted({line for line in kernel.splitlines() if re.search(fatal, line, re.I)})
        require(not report['kernel_fatals_start'], 'Native fatal already present in the new boot')
        runtime = expected['gate']['runtime']
        observed_runtime = {}
        for name in ('python', 'java', 'tmux'):
            path = Path(runtime[name + '_executable'])
            require(path.is_absolute() and path.resolve(strict=True) == path and os.access(path, os.X_OK), 'Noncanonical runtime: ' + name)
            observed_runtime[name + '_executable'] = str(path)
            observed_runtime[name + '_binary_sha256'] = sha(path)
            require(observed_runtime[name + '_binary_sha256'] == runtime[name + '_binary_sha256'], 'Runtime bytes changed: ' + name)
        report['runtime'] = observed_runtime
        require(str(Path('/proc/self/exe').resolve()) == runtime['python_executable'], 'Actual Python executable changed')
        require(str(Path(sys._base_executable).resolve()) == runtime['python_executable'], 'Python base changed')
        require(sys.version_info[:3] == (3, 12, 16), 'Python version changed')
        require(sys.flags.isolated == 1 and sys.dont_write_bytecode, 'Isolated read-only interpreter flags required')
        require(sys.executable == expected['python_launcher'] and sys.prefix == expected['preparation']['installed_identity']['prefix'], 'Literal editable venv identity changed')
        packages = sorted([d.metadata['Name'], d.version] for d in importlib.metadata.distributions())
        require(packages == expected['preparation']['installed_identity']['packages'], 'Installed package pins changed')
        report['packages'] = packages
        report['package_count'] = len(packages)
        origin = importlib.util.find_spec('devbrain').origin
        require(origin == expected['preparation']['installed_identity']['origin'], 'Editable source origin changed')
        project = Path(expected['source_root'])
        expected_source = expected['preparation']['source_manifest']
        observed_source = {}
        for relative in expected_source:
            path = project / relative
            require(path.resolve(strict=True) == path, 'Source path is not canonical: ' + relative)
            observed_source[relative] = sha(path)
        require(observed_source == expected_source, 'Frozen source bytes changed')
        allowed_generated = {'__pycache__', '.pytest_cache', '.ruff_cache'}
        require(not any(p.is_symlink() for p in project.rglob('*')), 'Frozen source tree contains a symlink')
        actual_names = {str(p.relative_to(project)) for p in project.rglob('*') if p.is_file()
                        and not any(part in allowed_generated or part.endswith('.egg-info') for part in p.relative_to(project).parts)
                        and p.suffix != '.pyc'}
        require(actual_names == set(expected_source), 'Frozen source inventory changed')
        require(all(observed_source.get(p) == value for p, value in expected['gate']['source_manifest'].items()), 'Gate source manifest changed')
        report.update(source_files_verified=len(observed_source), gate_source_files_verified=len(expected['gate']['source_manifest']),
                      source_manifest_sha256=hashlib.sha256(json.dumps(observed_source, sort_keys=True).encode()).hexdigest(),
                      python_version=sys.version, python_launcher=sys.executable, editable_origin=origin)
        report['openssl'] = ssl.OPENSSL_VERSION
        ssl_spec = importlib.util.find_spec('_ssl')
        report['ssl_module'] = ssl_spec.origin
        require(report['openssl'] == expected['ssl_metadata']['openssl'], 'Actual OpenSSL version changed')
        require(report['ssl_module'] == expected['ssl_metadata']['ssl_module'], 'SSL module origin changed')
        report['ssl_linkage'] = command(['/usr/bin/ldd', runtime['python_executable']])
        java_release = Path(runtime['java_executable']).parent.parent / 'release'
        java_data = dict(line.split('=', 1) for line in java_release.read_text().splitlines() if '=' in line)
        report['java_release'] = {key: java_data.get(key) for key in ('JAVA_VERSION', 'JAVA_RUNTIME_VERSION', 'IMPLEMENTOR')}
        require('21.0.13' in java_data.get('JAVA_VERSION', '') and '+9' in java_data.get('JAVA_RUNTIME_VERSION', ''), 'Temurin release metadata changed')
        report['tmux_version'] = command([runtime['tmux_executable'], '-V']).strip()
        require(report['tmux_version'] == 'tmux 3.8', 'tmux version changed')
        report['process_metadata'] = command(['/bin/ps', '-eo', 'pid,ppid,comm,rss'])
        suspected, unavailable = [], []
        for directory in Path('/proc').iterdir():
            if not directory.name.isascii() or not directory.name.isdecimal() or int(directory.name) == os.getpid():
                continue
            comm = '(unreadable)'
            try:
                comm = (directory / 'comm').read_text().strip()
                if not re.search(r'python|java|devbrain|uvicorn', comm, re.I):
                    continue
                arguments = (directory / 'cmdline').read_bytes().split(b'\0')
                if comm == 'java' or any(b'devbrain' in arg.lower() or b'uvicorn' in arg.lower() for arg in arguments):
                    suspected.append({'pid': int(directory.name), 'comm': comm})
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                unavailable.append({'pid': int(directory.name), 'comm': comm})
        report['production_candidates'] = suspected
        report['unreadable_candidate_processes'] = unavailable
        require(not suspected and not unavailable, 'Production/process-off evidence is unavailable or not empty')
        report['production_off_verified'] = True
        report['final_boot_id'] = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        require(report['initial_boot_id'] == report['final_boot_id'], 'Boot changed during metadata probe')
        report['runtime_end'] = {key: sha(value) for key, value in runtime.items() if key.endswith('_executable')}
        require(all(report['runtime_end'][name + '_executable'] == runtime[name + '_binary_sha256'] for name in ('python', 'java', 'tmux')), 'Runtime changed during probe')
        require(sha(args.input) == initial_input_sha and sha(__file__) == initial_script_sha, 'Probe inputs changed')
        report['passed'] = True
    except Exception as exc:
        report['errors'].append(type(exc).__name__ + ': ' + str(exc))
    report['elapsed_seconds'] = time.monotonic() - started
    print(json.dumps(report, indent=2), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
