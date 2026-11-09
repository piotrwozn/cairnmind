"""Exited tmux panes retain run ownership without inventing a process PID."""
import json
import socket
import subprocess
import sys

import pytest

from devbrain import client_runtime
from devbrain.client_runtime import NativeRuntime
from devbrain.config import Settings


@pytest.fixture
def pane_runtime(tmp_path, monkeypatch):
    runtime = NativeRuntime(Settings(tmp_path / 'home'), tmux_binary=sys.executable)
    runtime.socket_directory = tmp_path / 'socket'
    runtime.socket_directory.mkdir(mode=0o700)
    runtime.socket = runtime.socket_directory / 'tmux.sock'
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(runtime.socket))
    row = {'id': 'owned-run', 'run_id': 'owned-run', 'tmux_session': 'devbrain_owned-run',
           'generation': 3, 'lifecycle': 'running', 'state': 'running', 'purpose': 'coding',
           'pid': 4242, 'process_start': 'original-birth', 'pgid': 4242,
           'project_id': 'project', 'session_id': 'memory-session'}
    metadata = {'session': row['tmux_session'], 'run': row['id'], 'generation': '3',
                'pane': '%7', 'pid': '4242', 'dead': '0', 'exit': '', 'exists': True}
    calls = []

    def tmux(*args, check=True):
        calls.append(args)
        assert args[0] in {'display-message', 'kill-session'}, 'No terminal output capture is permitted'
        if args[0] == 'kill-session':
            assert args[1:] == ('-t', row['tmux_session'])
            metadata['exists'] = False
            return subprocess.CompletedProcess(args, 0, '', '')
        if not metadata['exists']:
            return subprocess.CompletedProcess(args, 1, '', '')
        text = '\t'.join(metadata[key] for key in ('session', 'run', 'generation', 'pane', 'pid', 'dead', 'exit'))
        return subprocess.CompletedProcess(args, 0, text + '\n', '')

    def forbidden(*args, **kwargs):
        pytest.fail('Exited or invalid panes must never probe or signal a process')

    monkeypatch.setattr(runtime, '_tmux', tmux)
    monkeypatch.setattr(client_runtime, 'process_start', forbidden)
    monkeypatch.setattr(client_runtime.os, 'getpgid', forbidden)
    monkeypatch.setattr(client_runtime.os, 'killpg', forbidden)
    with runtime._locked():
        runtime._save([row])
    try:
        yield runtime, row, metadata, calls
    finally:
        listener.close()


@pytest.mark.parametrize(('exit_status', 'lifecycle', 'code'), [('0', 'exited', 0), ('9', 'failed', 9), ('', 'failed', None)])
def test_dead_pane_without_pid_preserves_history_and_observed_exit(pane_runtime, exit_status, lifecycle, code):
    runtime, row, metadata, calls = pane_runtime
    metadata.update(pid='', dead='1', exit=exit_status)
    result = runtime.get_run(row['id'])
    assert result['lifecycle'] == lifecycle and result['exit_code'] == code
    assert result['pid'] == 4242 and result['pgid'] == 4242 and result['process_start'] == 'original-birth'
    assert result['session_id'] == 'memory-session' and result['generation'] == 3
    assert all(call[0] == 'display-message' for call in calls)


def test_dead_auth_pane_without_pid_discards_volatile_terminal(pane_runtime):
    runtime, row, metadata, calls = pane_runtime
    row.update(purpose='auth', project_id=None, session_id=None)
    with runtime._locked():
        runtime._save([row])
    metadata.update(pid='', dead='1', exit='0')
    result = runtime.get_run(row['id'])
    assert result['lifecycle'] == 'exited' and result['project_id'] is None and result['session_id'] is None
    assert [call for call in calls if call[0] == 'kill-session'] == [('kill-session', '-t', row['tmux_session'])]
    assert not metadata['exists']
    assert {path.name for path in runtime.directory.iterdir()} == {'tmux.conf', 'registry.lock', 'runs.json'}
    assert json.loads(runtime.registry.read_text())['runs'][0] == result


def test_stop_dead_pane_without_pid_only_removes_its_owned_session(pane_runtime):
    runtime, row, metadata, calls = pane_runtime
    metadata.update(pid='', dead='1', exit='0')
    result = runtime.stop(row['id'], row['generation'])
    assert result['stopped'] and result['lifecycle'] == 'exited'
    assert result['pid'] == 4242 and result['pgid'] == 4242
    assert [call for call in calls if call[0] == 'kill-session'] == [('kill-session', '-t', row['tmux_session'])]


@pytest.mark.parametrize('pid', ['', '0', '-3', '+42', ' 42', '42 ', 'invalid', '１２', '2.3'])
def test_live_pane_invalid_pid_refuses_refresh_without_registry_or_process_mutation(pane_runtime, pid):
    runtime, row, metadata, calls = pane_runtime
    metadata['pid'] = pid
    before = runtime.registry.read_bytes()
    with pytest.raises(ValueError, match='process identity is invalid'):
        runtime.get_run(row['id'])
    assert runtime.registry.read_bytes() == before
    assert all(call[0] == 'display-message' for call in calls)


@pytest.mark.parametrize('dead', ['', '2', 'true'])
def test_unknown_dead_marker_does_not_authorize_absent_pid(pane_runtime, dead):
    runtime, row, metadata, calls = pane_runtime
    metadata.update(pid='', dead=dead)
    before = runtime.registry.read_bytes()
    with pytest.raises(ValueError, match='pane metadata is invalid'):
        runtime.get_run(row['id'])
    assert runtime.registry.read_bytes() == before
    assert all(call[0] == 'display-message' for call in calls)


@pytest.mark.parametrize('field', ['session', 'run', 'generation'])
def test_dead_pane_still_requires_exact_registry_identity(pane_runtime, field):
    runtime, row, metadata, calls = pane_runtime
    metadata.update(pid='', dead='1', exit='0')
    metadata[field] = 'different-owner'
    before = runtime.registry.read_bytes()
    with pytest.raises(ValueError, match='identity does not match'):
        runtime.stop(row['id'], row['generation'])
    assert runtime.registry.read_bytes() == before
    assert all(call[0] == 'display-message' for call in calls)


def test_live_valid_pid_retains_existing_owned_process_identity(pane_runtime):
    runtime, row, metadata, calls = pane_runtime
    result = runtime.get_run(row['id'])
    assert result['lifecycle'] == 'running' and result['pid'] == 4242 and result['pane_id'] == '%7'
    assert result['process_start'] == 'original-birth' and result['session_id'] == 'memory-session'


def test_dead_pane_with_retained_pid_remains_supported(pane_runtime):
    runtime, row, metadata, calls = pane_runtime
    metadata.update(dead='1', exit='0')
    result = runtime.get_run(row['id'])
    assert result['lifecycle'] == 'exited' and result['exit_code'] == 0 and result['pid'] == 4242


@pytest.mark.parametrize('pane', ['', '7', '%invalid'])
def test_invalid_pane_identity_refuses_actions(pane_runtime, pane):
    runtime, row, metadata, calls = pane_runtime
    metadata.update(pane=pane, pid='', dead='1', exit='0')
    before = runtime.registry.read_bytes()
    with pytest.raises(ValueError, match='pane metadata is invalid'):
        runtime.stop(row['id'], row['generation'])
    assert runtime.registry.read_bytes() == before
    assert all(call[0] == 'display-message' for call in calls)
