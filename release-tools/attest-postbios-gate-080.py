#!/usr/bin/env python3
"""Build a pending gate; only the Windows observer can publish it after our exit."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

PHASES = ('prepare', 'perl', 'ssl', 'imports', 'unit', 'tmux', 'jvm1', 'jvm2', 'jvm3')


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024**2:
        raise ValueError('Missing, linked or oversized evidence')
    return json.loads(path.read_text(encoding='utf-8-sig'))


def build_candidate(root):
    binding = digest(root / 'bundle-manifest.json')
    global_crash_binding = digest(root / 'global-crash-baseline.json')
    global_kernel_binding = digest(root / 'global-kernel-baseline.json')
    global_kernel = read(root / 'global-kernel-baseline.json')
    receipts, workloads = {}, {}
    baseline = None
    for phase in PHASES:
        directory = root / phase
        receipt = read(directory / 'receipt.json')
        report = read(directory / 'controller-report.json')
        workload = read(directory / 'workload-report.json')
        if (receipt.get('schema_version') != 1 or receipt.get('scope') != 'postbios_external_diagnostic_watch'
                or receipt.get('passed') is not True or receipt.get('phase') != phase
                or receipt.get('bundle_manifest_sha256') != binding
                or receipt.get('global_crash_baseline_sha256') != global_crash_binding
                or receipt.get('global_kernel_baseline_sha256') != global_kernel_binding
                or receipt.get('controller_report_sha256') != digest(directory / 'controller-report.json')
                or receipt.get('workload_report_sha256') != digest(directory / 'workload-report.json')
                or receipt.get('controller_exit_verified') is not True or receipt.get('cleanup_complete') is not True
                or receipt.get('forced_cleanup') is not False or receipt.get('proxy_exit_code') != 0
                or receipt.get('cleanup_grace_seconds', 0) < 30
                or any(receipt.get(key) != [] for key in ('errors', 'new_wsl_dumps', 'new_kernel_fatals'))
                or receipt.get('initial_boot_id') != receipt.get('final_boot_id')):
            raise ValueError('Incomplete independent phase evidence: ' + phase)
        if (report.get('status') != 'controller_passed_pending_external_watch'
                or workload.get('status') != 'workload_passed_pending_external_watch'
                or workload.get('runtime') != receipt.get('runtime')
                or workload.get('runtime_end') != receipt.get('runtime')
                or report.get('runtime_end') != receipt.get('runtime')
                or receipt.get('runtime_verified_after_cleanup') is not True):
            raise ValueError('Controller/workload binding failed: ' + phase)
        if baseline is None:
            baseline = receipt
        if global_kernel.get('boot_id') != receipt.get('initial_boot_id'):
            raise ValueError('Global kernel baseline boot identity differs')
        if any(receipt.get(key) != baseline.get(key) for key in ('run_id', 'runtime', 'power', 'initial_boot_id', 'final_boot_id')):
            raise ValueError('Runtime, boot, power or run identity changed between phases')
        if (receipt.get('power_profile_start') != receipt['power'] or receipt.get('power_profile_end') != receipt['power']
                or receipt.get('sleep_inhibition', {}).get('restored') is not True
                or receipt.get('power_samples_sha256') != digest(directory / 'power-samples.jsonl')):
            raise ValueError('Power observation or sleep-state restoration is unverified')
        samples = [json.loads(line) for line in (directory / 'power-samples.jsonl').read_text().splitlines()]
        previous_time = 0
        for sample in samples:
            if sample['state'] != receipt['power'] or not 0 <= sample['elapsed_seconds'] - previous_time <= 15:
                raise ValueError('Power sampling gap or changed AC/DC settings')
            previous_time = sample['elapsed_seconds']
        if not samples or previous_time < receipt['cleanup_confirmed_elapsed_seconds'] + 30:
            raise ValueError('Power sampling ended before the complete post-exit grace')
        receipts[phase] = digest(directory / 'receipt.json')
        workloads[phase] = workload
    for name in ('python', 'java', 'tmux'):
        path = Path(baseline['runtime'][name + '_executable'])
        if not path.is_absolute() or path.resolve() != path or digest(path) != baseline['runtime'][name + '_binary_sha256']:
            raise ValueError('Runtime identity no longer matches the completed qualification')
    if workloads['perl'].get('completed_configures') != 3:
        raise ValueError('Perl qualification incomplete')
    for name, count in (('ssl', 20), ('imports', 32)):
        cases = workloads[name].get('cases', [])
        if len(cases) != count or any(case.get('returncode') != 0 for case in cases):
            raise ValueError(name + ' qualification incomplete')
    def valid_junit(value, count):
        row = value.get('junit', {})
        return (row.get('count') == count and all(row.get(key) == 0 for key in ('failure', 'error', 'skipped'))
                and digest(Path(row['path'])) == row.get('sha256'))
    if not valid_junit(workloads['unit'], 76):
        raise ValueError('Unit qualification incomplete')
    tmux = workloads['tmux']
    if (tmux.get('cores') != [] or len(tmux.get('trials', [])) != 3
            or sum(row.get('zoomed_windows_destroyed', 0) for row in tmux['trials']) != 63
            or not all(row.get('server_stopped') is True for row in tmux['trials'])):
        raise ValueError('tmux qualification incomplete')
    source = workloads['jvm1'].get('source_manifest')
    runs = []
    for phase, count in (('jvm1', 88), ('jvm2', 56), ('jvm3', 56)):
        work = workloads[phase]
        if (not valid_junit(work, count) or work.get('c2_compiled') is not True
                or work.get('tiered_stop_level_4') is not True or work.get('source_manifest') != source):
            raise ValueError('Fresh C2/test qualification incomplete: ' + phase)
        run = work['java_run']
        compilation = Path(run['c2_compilation_log'])
        if not compilation.is_file() or "compiler='c2'" not in compilation.read_text(errors='replace'):
            raise ValueError('Actual C2 compilation log is missing')
        runs.append({'phase': phase, 'junit': work['junit'], 'c2_compilation_sha256': digest(compilation)})
    checks = {
        'prepare': {'pass': True}, 'perl': {'pass': True, 'configures': 3},
        'ssl_candidate': {'pass': True, 'cases': 20}, 'imports': {'pass': True, 'cases': 32},
        'unit': {'pass': True, 'cases': 76}, 'tmux_teardown': {'pass': True, 'servers': 3, 'windows': 63},
        'native_evaluation': {'pass': True, 'cases': 32, 'run': 'jvm1'},
        'c2_repetitions': {'pass': True, 'selected_cases': 56, 'fresh_runs': runs},
        'native_children': {'pass': True, 'new_fatals': [], 'independent_windows_watch': True},
        'engines_stopped': {'pass': True, 'remaining_owned_processes': []},
    }
    return {'schema_version': 1, 'scope': 'bounded_runtime_qualification', 'status': 'pending_external_attestation',
            'run_id': baseline['run_id'], 'runtime': baseline['runtime'], 'power': baseline['power'], 'power_profile': baseline['power'],
            'initial_boot_id': baseline['initial_boot_id'], 'final_boot_id': baseline['final_boot_id'],
            'checks': checks, 'phase_receipt_sha256': receipts, 'bundle_manifest_sha256': binding,
            'global_crash_baseline_sha256': global_crash_binding,
            'global_kernel_baseline_sha256': global_kernel_binding,
            'source_manifest': source,
            'source_manifest_sha256': hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest(),
            'root_cause_established': False, 'production_or_soak_authorized': False,
            'limitations': ['Post-BIOS bounded evidence is not proof of the cause of earlier crashes.',
                            'Full current-source suite, upgrade smoke and live/soak work require their separate gates.',
                            'Unrelated pre-existing host/model processes were not terminated or qualified.']}


if __name__ == '__main__':
    raise SystemExit('Use the allowlisted attest phase through run-postbios-diagnostic-080.ps1')
