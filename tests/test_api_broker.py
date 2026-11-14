"""Authenticated configuration boundary: no paid requests and no public key readback."""
from copy import deepcopy

from fastapi.testclient import TestClient
import httpx
import pytest

from cairnmind.api_credentials import CredentialStore
from cairnmind.cockpit import create_cockpit_app
from cairnmind.cockpit_state import CockpitState
from cairnmind.config import Settings

ORIGIN = 'http://127.0.0.1:8766'


class Runtime:
    def __init__(self, settings):
        self.credentials = CredentialStore(settings)
        self.runs = []
        self.starts = []
    def list_runs(self):
        return deepcopy(self.runs)
    def diagnostics(self):
        return {'available': True}
    def agent_configuration(self, run_id, generation):
        if run_id != 'owned' or generation != 1:
            raise ValueError('Agent generation changed')
        return {'run': {'id': run_id, 'generation': generation}, 'profile': {'id': 'api'}}
    def agent_credential(self, run_id, generation):
        self.agent_configuration(run_id, generation)
        return self.credentials.resolve({'credential_ref': 'profile:api'})
    def start_project(self, project, *, worktree, slots, resume):
        self.starts.append(deepcopy(slots))
        return {'runs': [], 'errors': []}
    def start_auth(self, client):
        return {'id': 'auth-' + client, 'client': client, 'generation': 1, 'purpose': 'auth'}
    async def aclose(self):
        pass


@pytest.fixture
def broker(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / 'data', create=True)
    root = tmp_path / 'project'
    root.mkdir()
    project = {'id': 'project', 'name': 'Project', 'root': str(root)}
    runtime = Runtime(settings)
    state = CockpitState(settings)
    profile = {'id': 'api', 'client': 'api', 'label': 'API', 'provider': 'openai', 'model': 'test-model'}
    state.update({'profiles': [*state.load()['profiles'], profile]})
    def upstream(request):
        if request.url.path == '/projects':
            return httpx.Response(200, json=[project])
        if request.url.path == '/project-roots':
            return httpx.Response(200, json={'roots': [str(root)]})
        return httpx.Response(200, json={})
    monkeypatch.setattr('cairnmind.client_auth.probe_client', lambda name, **kw: {'client': name, 'auth': 'unknown'})
    app = create_cockpit_app(settings, runtime=runtime, proxy_transport=httpx.MockTransport(upstream))
    with TestClient(app, base_url=ORIGIN) as client:
        ticket = app.state.browser_sessions.ticket()
        login = client.post('/auth/exchange', json={'ticket': ticket}, headers={'Origin': ORIGIN})
        headers = {'Origin': ORIGIN, 'X-CSRF-Token': login.json()['csrf']}
        yield settings, state, runtime, client, headers


def test_credentials_are_write_only_and_private_worker_scoped(broker):
    settings, state, runtime, client, headers = broker
    secret = 'UNIQUE_API_KEY_DO_NOT_EXPORT'
    response = client.post('/api/profiles/api/credential', headers=headers, json={'key': secret, 'persist': False})
    assert response.status_code == 200
    assert secret not in response.text
    assert not runtime.credentials.path.exists()
    for path in ('/api/profiles', '/api/profiles/api/status', '/api/state'):
        response = client.get(path)
        assert response.status_code == 200
        assert secret not in response.text
    assert secret not in state.path.read_text()
    private = '/_control/agent/credential'
    payload = {'run_id': 'owned', 'generation': 1}
    assert client.post(private, json=payload).status_code == 401
    bearer = {'Authorization': 'Bearer ' + settings.token}
    assert client.post(private, headers={**bearer, 'Origin': ORIGIN}, json=payload).status_code == 403
    assert client.post(private, headers=bearer, json={**payload, 'generation': 2}).status_code == 409
    read = client.post(private, headers=bearer, json=payload)
    assert read.json() == {'key': secret}
    assert read.headers['cache-control'] == 'no-store'
    assert secret not in client.post('/_control/agent/config', headers=bearer, json=payload).text


def test_credential_mutations_require_native_consent_session_and_csrf(broker):
    settings, state, runtime, client, headers = broker
    assert client.post('/api/profiles/api/credential', json={'key': 'test-key'}).status_code == 403
    assert client.post('/api/profiles/codex/credential', headers=headers, json={'key': 'test-key'}).status_code == 422
    assert client.post('/api/profiles/api/credential', headers=headers, json={'key': 'test-key', 'persist': 'yes'}).status_code == 422
    assert client.post('/api/profiles/api/credential', headers=headers, json={'key': 'test-key', 'persist': True}).status_code == 200
    assert runtime.credentials.path.stat().st_mode & 0o777 == 0o600
    assert client.delete('/api/profiles/api/credential', headers=headers).status_code == 200
    assert client.get('/api/profiles/api/status').json()['configured'] is False


def test_models_and_paid_probe_are_explicit_and_profile_bound(broker, monkeypatch):
    settings, state, runtime, client, headers = broker
    calls = []
    class Provider:
        async def list_models(self):
            calls.append('models')
            return [{'id': 'model-a'}]
        async def probe(self):
            calls.append('probe')
            return {'status': 'ready', 'tools': True}
        async def close(self):
            calls.append('close')
    monkeypatch.setattr('cairnmind.api_providers.make_provider', lambda *a, **kw: Provider())
    assert client.get('/api/state').status_code == 200
    assert client.get('/api/profiles/api/status').status_code == 200
    assert calls == []
    assert client.get('/api/profiles/api/models').json()['models'] == [{'id': 'model-a'}]
    assert calls == ['models', 'close']
    assert client.get('/api/profiles/api/probe').status_code in {404, 405}
    assert calls == ['models', 'close']
    result = client.post('/api/profiles/api/probe', headers=headers, json={})
    assert result.json()['status'] == 'ready'
    assert calls == ['models', 'close', 'probe', 'close']
    assert client.get('/api/profiles/api/status').json()['last_probe']['tools'] is True
    assert client.post('/api/profiles/api/probe', headers=headers, json={'silent': True}).status_code == 422


def test_profile_changed_with_same_launch_key_is_conflict(broker):
    settings, state, runtime, client, headers = broker
    request = {'project': 'project', 'profile_id': 'api', 'idempotency_key': 'same-launch'}
    assert client.post('/api/client-runs', headers=headers, json=request).status_code == 200
    assert client.post('/api/client-runs', headers=headers, json=request).status_code == 200
    assert len(runtime.starts) == 1
    rows = state.load()['profiles']
    rows[-1]['model'] = 'changed-model'
    state.update({'profiles': rows})
    assert client.post('/api/client-runs', headers=headers, json=request).status_code == 409


def test_copilot_preparation_and_login_are_explicit(broker, monkeypatch):
    settings, state, runtime, client, headers = broker
    calls = []
    monkeypatch.setattr('cairnmind.copilot_adapter.prepare_runtime', lambda s: calls.append('prepare') or {'available': True})
    assert client.get('/api/state').status_code == 200
    assert calls == []
    assert client.post('/api/copilot/prepare', json={}).status_code == 403
    assert client.post('/api/copilot/prepare', headers=headers, json={}).json()['available']
    assert calls == ['prepare']
    assert client.post('/api/copilot/login', headers=headers, json={}).json()['run']['purpose'] == 'auth'


def test_provider_diagnostics_never_echo_mutated_upstream_hint(broker, monkeypatch):
    from cairnmind.api_providers import ProviderError
    settings, state, runtime, client, headers = broker
    class Provider:
        async def probe(self):
            error = ProviderError('context_limit', context_details={'configured_context_window': 32768,
                                                                   'model_context_window': 8192})
            error.hint = 'RAW_PRIVATE_KEY_OR_PROVIDER_BODY'
            raise error
        async def close(self):
            pass
    monkeypatch.setattr('cairnmind.api_providers.make_provider', lambda *a, **kw: Provider())
    response = client.post('/api/profiles/api/probe', headers=headers, json={})
    assert response.json()['code'] == 'context_limit'
    assert response.json()['context_details']['model_context_window'] == 8192
    assert 'Lower it' in response.json()['hint']
    assert 'RAW_PRIVATE_KEY_OR_PROVIDER_BODY' not in response.text
