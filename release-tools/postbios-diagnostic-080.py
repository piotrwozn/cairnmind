#!/usr/bin/env python3
"""Post-BIOS diagnostic controller. Only run through its Windows observer."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import xml.etree.ElementTree as ET

BASE = Path('/home/piotr/.cache/devbrain-runtime-080')
PYTHON = BASE / 'runtimes/python/bin/python3.12'
JAVA = BASE / 'runtimes/jdk-21.0.13+9/bin/java'
TMUX = BASE / 'runtimes/tmux-3.8/bin/tmux'
UV = Path('/home/piotr/.local/bin/uv')
DIST = Path('/home/piotr/.cache/devbrain-validation-20261107/tooling/arcadedb-26.11.1')
RUNTIME_HASHES = {
    'python': '23bc148d2b48964b621c5561ff2b81c1d9c7b717967a7491efbf12f4304882b5',
    'java': '9cad3f2620d3ef2ed10b26f922af7e773d54fdaa05f615d6d6df014f3fec4155',
    'tmux': 'b4338a07981a2d6e008578059050cf91e43e086929630358937a4df365766669',
}
REQUIREMENTS_SHA = 'da5edd5506fe2ed399445d193aaec9f3ebe021acac4510f888f4474258646d6a'
PERL_SHA = '47bdc8a342556c1d140084417ef6cacd79a3362e4b6cc1e4754df55bdf1e3683'
OPENSSL_SHA = '603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a'
PHASES = ('prepare', 'perl', 'ssl', 'imports', 'unit', 'tmux', 'jvm1', 'jvm2', 'jvm3', 'attest')
FATAL = re.compile(r'segfault|segmentation fault|fatal signal|SIGSEGV|SIGABRT|invalid opcode|general protection fault|oom-kill|Out of memory|Kernel panic', re.I)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write(path, value):
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def read(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024**2:
        raise ValueError('Missing, linked or oversized report: ' + str(path))
    return json.loads(path.read_text(encoding='utf-8-sig'))


def manifest(root):
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('Bundle/source symlink is not accepted: ' + str(path))
        if path.is_file():
            result[path.relative_to(root).as_posix()] = digest(path)
    return result


def runtime_identity():
    result = {}
    for name, path in (('python', PYTHON), ('java', JAVA), ('tmux', TMUX)):
        actual = digest(path)
        if actual != RUNTIME_HASHES[name]:
            raise RuntimeError('Runtime binary SHA mismatch: ' + name)
        result[name + '_executable'] = str(path.resolve())
        result[name + '_binary_sha256'] = actual
    if Path(sys._base_executable).resolve() != PYTHON.resolve():
        raise RuntimeError('Controller must use the exact private CPython3.12.16')
    if Path('/proc/self/exe').resolve() != PYTHON.resolve() or digest('/proc/self/exe') != RUNTIME_HASHES['python']:
        raise RuntimeError('Actual executing Python identity differs from the pinned binary')
    return result


def helper(bundle, name):
    path = bundle / 'release-tools' / name
    spec = importlib.util.spec_from_file_location('postbios_' + name.replace('-', '_'), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def clean_environment():
    # No auth tokens, provider settings or private application configuration.
    allowed = ('HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'TZ', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS')
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update(PATH='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
               PYTHONFAULTHANDLER='1', PYTHONDONTWRITEBYTECODE='1',
               UV_OFFLINE='1', UV_PYTHON_DOWNLOADS='never',
               OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1')
    return env


def run_command(argv, cwd, output, timeout=120, env=None):
    with output.open('w') as stream:
        completed = subprocess.run([str(item) for item in argv], cwd=cwd,
                                   env=env or clean_environment(), stdout=stream,
                                   stderr=stream, timeout=timeout)
    if completed.returncode:
        raise RuntimeError(f'Command returned {completed.returncode}; see {output.name}')


def create_venv(directory, bundle, output):
    if directory.exists():
        raise RuntimeError('Refusing to reuse any historical venv')
    run_command([UV, 'venv', '--python', PYTHON, directory], directory.parent, output / 'venv.log', 30)
    run_command([UV, 'pip', 'install', '--python', directory / 'bin/python', '--only-binary', ':all:',
                 '-r', bundle / 'requirements.txt'], directory.parent, output / 'dependencies.log', 120)


def junit(path, count):
    root = ET.parse(path)
    cases = list(root.iter('testcase'))
    result = {'count': len(cases), 'failure': len(list(root.iter('failure'))),
              'error': len(list(root.iter('error'))), 'skipped': len(list(root.iter('skipped'))),
              'sha256': digest(path), 'path': str(path)}
    if len(cases) != count or any(result[key] for key in ('failure', 'error', 'skipped')):
        raise RuntimeError('Incomplete or unsuccessful JUnit: ' + json.dumps(result))
    return result


def worker(args):
    output, root, bundle = args.artifact_root / args.phase, args.linux_root, args.bundle_root
    expected = read(args.artifact_root / 'bundle-manifest.json')['files']
    if manifest(bundle) != expected:
        raise RuntimeError('Bundle changed after Windows snapshot')
    result = {'phase': args.phase, 'runtime': runtime_identity(), 'status': 'running', 'diagnostic_only': True}
    report = output / 'workload-report.json'
    write(report, result)
    project = root / 'project'
    python = root / 'venv/bin/python'
    if args.phase == 'prepare':
        if root.exists():
            raise RuntimeError('Post-BIOS root already exists; no silent repeat')
        if digest(bundle / 'requirements.txt') != REQUIREMENTS_SHA:
            raise RuntimeError('Dependency pins differ from the pre-BIOS snapshot')
        root.mkdir()
        run_command(['/bin/ps', '-eo', 'pid,ppid,comm,rss'], root, output / 'background-process-metadata.log', 5)
        result['background_process_metadata'] = 'background-process-metadata.log'
        project.mkdir()
        shutil.copytree(bundle / 'devbrain', project / 'devbrain')
        (project / 'evidence').mkdir()
        shutil.copy2(bundle / 'evidence/devbrain-c1-diagnostic-retry-plan-20261107.json', project / 'evidence')
        create_venv(root / 'venv', bundle, output)
        run_command([UV, 'pip', 'install', '--python', python, '--no-deps', '--editable', project / 'devbrain'],
                    root, output / 'editable.log')
        run_command([python, '-I', '-B', '-c',
                     'import devbrain,sys,json,importlib.metadata as m; print(json.dumps({"origin":devbrain.__file__,"prefix":sys.prefix,"base":sys._base_executable,"packages":sorted((d.metadata["Name"],d.version) for d in m.distributions())}))'],
                    root, output / 'installed-identity.json')
        identity = read(output / 'installed-identity.json')
        if (Path(identity['origin']).resolve() != (project / 'devbrain/src/devbrain/__init__.py').resolve()
                or Path(identity['prefix']).resolve() != (root / 'venv').resolve()
                or Path(identity['base']).resolve() != PYTHON.resolve()):
            raise RuntimeError('Isolated import did not resolve the frozen editable source')
        result['installed_identity'] = identity
        result['source_manifest'] = manifest(project / 'devbrain')
        write(root / 'preparation.json', result)
    else:
        prep = read(root / 'preparation.json')
        if manifest(project / 'devbrain') != prep['source_manifest']:
            raise RuntimeError('Frozen Linux source changed')
        import devbrain
        if (Path(devbrain.__file__).resolve() != (project / 'devbrain/src/devbrain/__init__.py').resolve()
                or Path(sys.prefix).resolve() != (root / 'venv').resolve()):
            raise RuntimeError('Worker is not using the fresh isolated editable environment')
        result['worker_environment'] = {'origin': devbrain.__file__, 'prefix': sys.prefix,
                                        'base': str(Path(sys._base_executable).resolve())}
        if args.phase == 'perl':
            if digest('/usr/bin/perl') != PERL_SHA:
                raise RuntimeError('Perl binary changed from the failing baseline')
            archive = BASE / 'gcc31318/downloads/openssl-3.5.9.tar.gz'
            if digest(archive) != OPENSSL_SHA:
                raise RuntimeError('OpenSSL source SHA mismatch')
            trials = root / 'perl'
            trials.mkdir()
            deadline = time.monotonic() + 75
            result['cases'] = []
            for index in range(3):
                fresh = trials / str(index)
                fresh.mkdir()
                with tarfile.open(archive) as stream:
                    stream.extractall(fresh, filter='data')
                source, prefix = fresh / 'openssl-3.5.9', fresh / 'unused-prefix'
                argv = ['/usr/bin/perl', str(source / 'Configure'), 'linux-x86_64', 'shared', 'no-tests',
                        '--prefix=' + str(prefix), '--openssldir=' + str(prefix / 'ssl'), '--libdir=lib', '-O2', '-g']
                env = clean_environment()
                env.update(PATH='/usr/bin:/bin', CC='gcc', CFLAGS='-O2 -g -fno-omit-frame-pointer')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Perl phase workload budget exhausted')
                run_command(argv, source, output / f'configure-{index}.log', min(30, remaining), env)
                result['cases'].append({'index': index, 'argv': argv, 'returncode': 0})
                write(report, result)
            result.update(completed_configures=3, perl_binary_sha256=PERL_SHA, source_sha256=OPENSSL_SHA)
        elif args.phase == 'ssl':
            diagnostics = helper(bundle, 'diagnose-runtime-080.py')
            work = root / 'ssl'
            if work.exists():
                raise RuntimeError('SSL phase already exists')
            if diagnostics.repro(work, python, 'postbios4601-31216'):
                raise RuntimeError('SSL phase failed')
            value = read(work / 'evidence/ssl-postbios4601-31216.json')
            if value['status'] != 'pass' or len(value['cases']) != 20:
                raise RuntimeError('SSL phase incomplete')
            result['cases'] = value['cases']
        elif args.phase == 'imports':
            diagnostics = helper(bundle, 'diagnose-imports-080.py')
            work = root / 'imports'
            if diagnostics.compare(work, python, 'postbios4601-31216'):
                raise RuntimeError('Import/GC/Textual phase failed')
            value = read(work / 'evidence/imports-postbios4601-31216.json')
            if value['status'] != 'pass' or len(value['cases']) != 32:
                raise RuntimeError('Import phase incomplete')
            result['cases'] = value['cases']
        elif args.phase == 'unit':
            xml = output / 'pytest.xml'
            run_command([python, '-I', '-B', '-m', 'pytest', '-q', '-x', '--noconftest', '-p', 'no:cacheprovider',
                         'tests/test_arcade_transport.py', 'tests/test_runtime_selection.py',
                         'tests/test_runtime_pane.py', '--junitxml', xml],
                        project / 'devbrain', output / 'pytest.log', 120)
            result['junit'] = junit(xml, 76)
        elif args.phase == 'tmux':
            work = root / 'tmux'
            work.mkdir()
            (work / 'evidence').mkdir()
            (work / 'runtimes').symlink_to(BASE / 'runtimes', target_is_directory=True)
            helper(bundle, 'diagnose-tmux-080.py').trials(work)
            value = read(work / 'evidence/tmux38-teardown.json')
            if (value['status'] != 'pass' or value['cores'] or len(value['trials']) != 3
                    or sum(row['zoomed_windows_destroyed'] for row in value['trials']) != 63
                    or not all(row['server_stopped'] for row in value['trials'])):
                raise RuntimeError('tmux teardown phase incomplete')
            result.update(trials=value['trials'], cores=value['cores'])
        elif args.phase == 'attest':
            gate = helper(bundle, 'attest-postbios-gate-080.py').build_candidate(args.artifact_root)
            write(args.artifact_root / 'gate-candidate.json', gate)
            result['gate_candidate_sha256'] = digest(args.artifact_root / 'gate-candidate.json')
        else:
            work = root / args.phase
            work.mkdir()
            (work / 'evidence').mkdir()
            (work / 'runtimes').symlink_to(BASE / 'runtimes', target_is_directory=True)
            create_venv(work / 'venv', bundle, output)
            diagnostics = helper(bundle, 'diagnose-runtime-080.py')
            if diagnostics.java_tests(work, project, selection_only=args.phase != 'jvm1', repetitions=1, tmux=TMUX):
                raise RuntimeError('JVM/test phase failed')
            value = read(work / 'evidence/java-tests.json')
            if value['status'] != 'pass' or value['selection_count'] != 56 or len(value['runs']) != 1:
                raise RuntimeError('JVM phase incomplete')
            run = value['runs'][0]
            if (run.get('returncode') != 0 or not run.get('tiered_stop_level_4') or not run.get('c2_compiled')
                    or run.get('stop_error_type') or run.get('fatal_artifacts_after_cleanup')):
                raise RuntimeError('C2 or cleanup evidence failed')
            result.update(junit=junit(Path(run['junit']), 88 if args.phase == 'jvm1' else 56),
                          c2_compiled=True, tiered_stop_level_4=True, java_run=run,
                          source_manifest=value['source_sha256'])
    result['runtime_end'] = runtime_identity()
    if result['runtime_end'] != result['runtime']:
        raise RuntimeError('Runtime bytes or identities changed during workload')
    result['status'] = 'workload_passed_pending_external_watch'
    write(report, result)
    return 0


def controller(args):
    directory = args.artifact_root / args.phase
    report = {'schema_version': 1, 'scope': 'postbios_diagnostic_controller', 'run_id': args.run_id,
              'phase': args.phase, 'status': 'running', 'errors': [], 'worker_returncode': None,
              'pid': os.getpid(), 'birth': Path('/proc/self/stat').read_text().rpartition(')')[2].split()[19],
              'runtime': runtime_identity()}
    group = Path('/proc/self/cgroup').read_text().strip().split('::', 1)[1]
    if not group.endswith('/' + args.scope_unit) or not group.startswith('/user.slice/'):
        raise RuntimeError('Controller is not in the Windows-owned UUID scope')
    cg = Path('/sys/fs/cgroup') / group.lstrip('/')
    report.update(control_group=group, memory_max=(cg / 'memory.max').read_text().strip(),
                  memory_swap_max=(cg / 'memory.swap.max').read_text().strip())
    expected_memory = str(2 * 1024**3 if args.phase == 'perl' else 4 * 1024**3)
    if report['memory_max'] != expected_memory or report['memory_swap_max'] != '0':
        raise RuntimeError('Owned cgroup memory/swap limit readback failed')
    write(directory / 'controller-report.json', report)
    crash_dir = Path('/mnt/c/Users/piotr/AppData/Local/Temp/wsl-crashes')
    diagnostics = helper(args.bundle_root, 'supervise-080.py')
    observation = diagnostics.Observation(crash_dir)
    parent = diagnostics.ParentControl(directory, args.run_id, args.nonce)
    parent.check()
    env = clean_environment()
    launcher = PYTHON if args.phase == 'prepare' else args.linux_root / 'venv/bin/python'
    argv = [str(launcher), '-I', '-B', str(Path(__file__).resolve()), '--worker',
            '--bundle-root', str(args.bundle_root), '--artifact-root', str(args.artifact_root),
            '--linux-root', str(args.linux_root), '--phase', args.phase, '--run-id', args.run_id,
            '--scope-unit', args.scope_unit, '--nonce', args.nonce, '--timeout-seconds', str(args.timeout_seconds)]
    process = None
    deadline = time.monotonic() + args.timeout_seconds
    try:
        with (directory / 'worker.log').open('w') as log:
            process = subprocess.Popen(argv, env=env, stdout=log, stderr=log, start_new_session=True)
            report['worker_pid'] = process.pid
            write(directory / 'controller-report.json', report)
            while True:
                parent.check()
                if observation.poll():
                    raise RuntimeError('New native fatal or boot change')
                code = process.poll()
                if code is not None:
                    report['worker_returncode'] = code
                    if code:
                        raise RuntimeError('Worker exited nonzero: ' + str(code))
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError('Phase deadline exceeded')
                time.sleep(.25)
        value = read(directory / 'workload-report.json')
        if value['status'] != 'workload_passed_pending_external_watch':
            raise RuntimeError('Missing complete workload report')
        report['workload_sha256'] = digest(directory / 'workload-report.json')
        report['status'] = 'controller_passed_pending_external_watch'
    except BaseException as exc:
        report['status'] = 'stopped_on_failure'
        report['errors'].append(type(exc).__name__ + ': ' + str(exc))
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            report['errors'].append('Worker required forced cleanup; Windows must empty the owned scope')
            report['status'] = 'stopped_on_failure'
        report.update(worker_returncode=process.returncode if process is not None else None,
                      initial_boot_id=observation.initial_boot, final_boot_id=observation.final_boot,
                      new_wsl_dumps=sorted(observation.new_files), new_kernel_fatals=sorted(observation.new_kernel))
        try:
            report['runtime_end'] = runtime_identity()
            if report['runtime_end'] != report['runtime']:
                raise RuntimeError('Runtime identity changed before controller exit')
        except Exception as exc:
            report['status'] = 'stopped_on_failure'
            report['errors'].append(type(exc).__name__ + ': ' + str(exc))
        write(directory / 'controller-report.json', report)
    return 0 if report['status'] == 'controller_passed_pending_external_watch' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--bundle-root', type=Path, required=True)
    parser.add_argument('--artifact-root', type=Path, required=True)
    parser.add_argument('--linux-root', type=Path, required=True)
    parser.add_argument('--phase', choices=PHASES, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--scope-unit', required=True)
    parser.add_argument('--nonce', required=True)
    parser.add_argument('--timeout-seconds', type=int, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', args.run_id) or not re.fullmatch(r'devbrain-pb-[a-f0-9]{32}\.scope', args.scope_unit):
        parser.error('Invalid diagnostic ownership identity')
    if args.linux_root != BASE / ('postbios-' + args.run_id):
        parser.error('Linux root must be fresh and run-derived inside the private cache')
    for path in (args.bundle_root, args.artifact_root):
        physical = Path('/mnt/c/Users/piotr/AppData/Local/Packages/OpenAI.Codex_2p2nqsd0c76g0/LocalCache/Local/DevBrain/qualification')
        if not path.is_absolute() or path.resolve() != path or not path.is_relative_to(physical):
            parser.error('Bundle and artifacts must be inside the verified physical MSIX LOCALAPPDATA qualification root')
    if not 1 <= args.timeout_seconds <= 1800:
        parser.error('Invalid bounded timeout')
    return worker(args) if args.worker else controller(args)


if __name__ == '__main__':
    raise SystemExit(main())
