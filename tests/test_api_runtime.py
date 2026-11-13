"""Generation-scoped API worker launch using public config and RAM credentials."""
import json

import httpx
import pytest

from cairnmind.api_credentials import CredentialStore
from cairnmind.api_profiles import normalize_profile
from cairnmind.api_runtime import RuntimeSupervisor, _worker_environment
from cairnmind.config import Settings


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / 'data', create=True)
    root = tmp_path / 'project'
    root.mkdir()
    requests = []
    def request(req):
        requests.append(json.loads(req.content))
        return httpx.Response(200, json={'session_id': 'logical-session', 'status': 'ready', 'packet': {'id': 'packet'}})
    credentials = CredentialStore(settings)
    runner = RuntimeSupervisor(settings, '/tmp/devbrain-tmux-3.4/tmux', credentials=credentials,
                               session_transport=httpx.MockTransport(request))
    monkeypatch.setattr(runner, 'diagnostics', lambda: {'available': True})
    spawned = []
    def spawn(rows, project, client, slot, launch, generation, purpose, *, run_id):
        spawned.append(launch)
        row = {'id': run_id, 'generation': generation, 'session_id': launch['session_id'],
               'project_id': project['id'], 'worktree': launch['cwd'], 'client': client,
               'profile_id': launch['profile_id'], 'slot_id': slot, 'purpose': purpose, 'lifecycle': 'running'}
        rows.append(row)
        runner._save(rows)
        return dict(row)
    monkeypatch.setattr(runner, '_spawn', spawn)
    monkeypatch.setattr(runner, '_refresh', lambda row: row)
    yield runner, credentials, {'id': 'project', 'root': str(root)}, spawned, requests


def test_launch_and_immutable_config_never_contain_key(runtime, monkeypatch):
    runner, credentials, project, spawned, requests = runtime
    secret = 'ONLY_IN_BROKER_MEMORY'
    monkeypatch.setenv('CUSTOM_SECRET_SOURCE', secret)
    profile = normalize_profile({'id': 'api', 'client': 'api', 'label': 'Own API', 'provider': 'openai',
                                 'model': 'model-a', 'credential_ref': 'env:CUSTOM_SECRET_SOURCE'})
    run = runner.start_agent(project, profile, slot_id='slot')
    assert requests[0]['client'] == 'api'
    assert secret not in json.dumps(spawned)
    assert 'CUSTOM_SECRET_SOURCE' not in spawned[0]['env']
    assert secret not in runner.registry.read_text()
    config = runner.agent_configuration(run['id'], run['generation'])
    assert config['profile']['model'] == 'model-a'
    profile['model'] = 'changed'
    assert runner.agent_configuration(run['id'], run['generation'])['profile']['model'] == 'model-a'
    assert runner.agent_credential(run['id'], run['generation']) == secret
    for path in (runner.directory / 'agent-configs').iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
        assert secret not in path.read_text()
    with pytest.raises(ValueError, match='generation'):
        runner.agent_configuration(run['id'], run['generation'] + 1)


def test_live_slot_reused_and_explicit_resume_reuses_logical_session(runtime):
    runner, credentials, project, spawned, requests = runtime
    profile = normalize_profile({'id': 'local', 'client': 'api', 'label': 'Local', 'provider': 'ollama', 'model': 'local-tools'})
    first = runner.start_agent(project, profile, slot_id='slot')
    second = runner.start_agent(project, profile, slot_id='slot')
    assert first['id'] == second['id'] and len(spawned) == 1
    rows = runner._read()
    rows[0]['lifecycle'] = 'exited'
    runner._save(rows)
    from cairnmind.agent_journal import AgentJournal
    AgentJournal(runner.settings, first['session_id']).append('message', {'role': 'user', 'content': 'Archived turn'})
    resumed = runner.start_agent(project, profile, slot_id='slot', resume=True)
    assert resumed['id'] != first['id'] and resumed['generation'] == 2
    assert requests[-1]['session_id'] == first['session_id']
    assert requests[-1]['reason'] == 'resume'


def test_missing_credential_or_unknown_resume_cannot_spawn(runtime):
    runner, credentials, project, spawned, requests = runtime
    profile = normalize_profile({'id': 'api', 'client': 'api', 'label': 'Cloud', 'provider': 'openai', 'model': 'tools'})
    with pytest.raises(ValueError, match='credential_missing'):
        runner.start_agent(project, profile, slot_id='slot')
    assert not spawned and not requests
    profile = normalize_profile({'id': 'local', 'client': 'api', 'label': 'Local', 'provider': 'ollama', 'model': 'local'})
    with pytest.raises(ValueError, match='agent_resume_unavailable'):
        runner.start_agent(project, profile, slot_id='slot', resume=True)
    assert not spawned


def test_invalid_private_configuration_refuses_identity_and_symlink(runtime, tmp_path):
    runner, credentials, project, spawned, requests = runtime
    profile = normalize_profile({'id': 'local', 'client': 'api', 'label': 'Local', 'provider': 'ollama', 'model': 'local'})
    run = runner.start_agent(project, profile, slot_id='slot')
    path = runner.directory / 'agent-configs' / (run['id'] + '.json')
    content = json.loads(path.read_text())
    content['project_id'] = 'foreign'
    path.write_text(json.dumps(content))
    with pytest.raises(ValueError, match='configuration_invalid'):
        runner.agent_configuration(run['id'], run['generation'])
    path.unlink()
    foreign = tmp_path / 'foreign.json'
    foreign.write_text('{}')
    path.symlink_to(foreign)
    with pytest.raises(OSError):
        runner.agent_configuration(run['id'], run['generation'])


def test_worker_environment_removes_known_and_configured_credentials(monkeypatch):
    for name in ('OPENAI_API_KEY', 'GH_TOKEN', 'GITHUB_TOKEN', 'PRIVATE_CUSTOM', 'DEVBRAIN_SESSION_ID'):
        monkeypatch.setenv(name, 'SECRET')
    environment = _worker_environment([{'credential_ref': 'env:PRIVATE_CUSTOM'}])
    assert not set(('OPENAI_API_KEY', 'GH_TOKEN', 'GITHUB_TOKEN', 'PRIVATE_CUSTOM', 'DEVBRAIN_SESSION_ID')) & environment.keys()
    assert 'PATH' in environment


def test_agent_data_inside_project_is_rejected_before_session_or_launch(runtime):
    runner, credentials, project, spawned, requests = runtime
    unsafe = {'id': 'project', 'root': str(runner.settings.data_dir.parent)}
    profile = normalize_profile({'id': 'local', 'client': 'api', 'label': 'Local', 'provider': 'ollama', 'model': 'local'})
    with pytest.raises(ValueError, match='agent_storage_in_project'):
        runner.start_agent(unsafe, profile, slot_id='slot')
    assert not spawned and not requests


def test_reopening_live_agent_needs_no_new_credential_or_model_call(runtime):
    runner, credentials, project, spawned, requests = runtime
    profile = normalize_profile({'id': 'api', 'client': 'api', 'label': 'Cloud', 'provider': 'openai', 'model': 'tools'})
    credentials.set('api', 'SESSION_KEY')
    first = runner.start_agent(project, profile, slot_id='slot')
    credentials.delete('api')
    second = runner.start_agent(project, profile, slot_id='slot')
    assert second['id'] == first['id'] and len(requests) == len(spawned) == 1


def test_all_profiles_custom_credentials_are_absent_from_worker_spec(runtime, monkeypatch):
    from cairnmind.cockpit_state import CockpitState
    runner, credentials, project, spawned, requests = runtime
    state = CockpitState(runner.settings)
    row = normalize_profile({'id': 'foreign', 'client': 'api', 'label': 'Other', 'provider': 'openai',
                             'model': 'model', 'credential_ref': 'env:UNRECOGNIZED_VALUE'})
    state.update({'profiles': [*state.load()['profiles'], row]})
    monkeypatch.setenv('UNRECOGNIZED_VALUE', 'FOREIGN_VENDOR_SECRET')
    local = normalize_profile({'id': 'local', 'client': 'api', 'label': 'Local', 'provider': 'ollama', 'model': 'local'})
    runner.start_agent(project, local, slot_id='slot')
    assert 'UNRECOGNIZED_VALUE' not in spawned[0]['env']
    assert 'FOREIGN_VENDOR_SECRET' not in json.dumps(spawned)


def test_empty_slots_or_nonstring_client_cannot_start_default_native_agents(runtime):
    runner, credentials, project, spawned, requests = runtime
    for slots in ([], [{'id': 'slot', 'client': []}]):
        with pytest.raises(ValueError, match='supported client slots'):
            runner.start_project(project, slots=slots)
    assert not spawned


def test_deleted_private_journal_requires_new_session_not_sequence_reuse(runtime):
    runner, credentials, project, spawned, requests = runtime
    profile = normalize_profile({'id': 'local', 'client': 'api', 'label': 'Local', 'provider': 'ollama', 'model': 'local'})
    first = runner.start_agent(project, profile, slot_id='slot')
    rows = runner._read()
    rows[0]['lifecycle'] = 'exited'
    runner._save(rows)
    with pytest.raises(ValueError, match='local journal is missing'):
        runner.start_agent(project, profile, slot_id='slot', resume=True)
    assert len(requests) == len(spawned) == 1
    assert first['session_id'] == 'logical-session'


def mcp_server(identifier='tracker', command='/usr/bin/env', args=('python3', '-m', 'tracker')):
    return {'id': identifier, 'command': command, 'args': list(args), 'env': {}}


def local_profile():
    return normalize_profile({'id': 'local', 'client': 'api', 'label': 'Local', 'provider': 'ollama', 'model': 'm'})


def test_project_mcp_servers_are_frozen_into_the_api_run_only(runtime, tmp_path, monkeypatch):
    from cairnmind.cockpit_state import CockpitState
    runner, credentials, project, spawned, requests = runtime
    other = tmp_path / 'other'
    other.mkdir()
    state = CockpitState(runner.settings)
    state.update({'project_mcp_servers': {'project': [mcp_server()]}})
    run = runner.start_agent(project, local_profile(), slot_id='slot')
    config = runner.agent_configuration(run['id'], run['generation'])
    assert config['mcp_servers'] == [mcp_server()] and config['mcp_problems'] == []
    # Edits apply to the next run only.
    state.update({'project_mcp_servers': {'project': [mcp_server('changed')]}})
    assert runner.agent_configuration(run['id'], run['generation'])['mcp_servers'] == [mcp_server()]
    second = runner.start_agent({'id': 'other', 'root': str(other)}, local_profile(), slot_id='slot')
    assert runner.agent_configuration(second['id'], second['generation'])['mcp_servers'] == []
    monkeypatch.setattr('cairnmind.copilot_adapter.runtime_status', lambda settings: {'available': True})
    copilot = normalize_profile({'id': 'copilot', 'client': 'copilot', 'label': 'Copilot'})
    third = runner.start_agent(project, copilot, slot_id='copilot-slot')
    assert runner.agent_configuration(third['id'], third['generation'])['mcp_servers'] == []
    path = runner.directory / 'agent-configs' / (third['id'] + '.json')
    assert json.loads(path.read_text())['mcp_servers'] == []


def test_a_worktree_program_or_an_invalid_list_gives_no_server_and_the_run_starts(runtime):
    from cairnmind.cockpit_state import CockpitState
    runner, credentials, project, spawned, requests = runtime
    state = CockpitState(runner.settings)
    inside = mcp_server('local', command=project['root'] + '/bin/server', args=())
    state.update({'project_mcp_servers': {'project': [inside, mcp_server()]}})
    run = runner.start_agent(project, local_profile(), slot_id='slot')
    config = runner.agent_configuration(run['id'], run['generation'])
    assert config['mcp_servers'] == [mcp_server()]
    assert config['mcp_problems'] == [{'server_id': 'local', 'code': 'definition_refused'}]
    stored = json.loads(state.path.read_text())
    stored['project_mcp_servers'] = {'project': [{'id': 'x', 'command': 'relative'}]}
    state.path.write_text(json.dumps(stored))
    rows = runner._read()
    rows[0]['lifecycle'] = 'exited'
    runner._save(rows)
    again = runner.start_agent(project, local_profile(), slot_id='slot')
    assert again['id'] != run['id']
    config = runner.agent_configuration(again['id'], again['generation'])
    assert config['mcp_servers'] == [] and config['mcp_problems'] == [{'code': 'configuration_invalid'}]


def test_the_largest_valid_list_still_fits_the_private_configuration(runtime):
    from cairnmind.cockpit_state import CockpitState
    runner, credentials, project, spawned, requests = runtime
    state = CockpitState(runner.settings)
    big = [mcp_server(f's{i}', args=['a' * 4000] * 4) for i in range(4)]
    assert state.update({'project_mcp_servers': {'project': big}})
    with pytest.raises(ValueError, match='too large'):
        state.update({'project_mcp_servers': {'project': [mcp_server(f's{i}', args=['a' * 4096] * 32)
                                                          for i in range(4)]}})
    run = runner.start_agent(project, local_profile(), slot_id='slot')
    assert len(runner.agent_configuration(run['id'], run['generation'])['mcp_servers']) == 4
