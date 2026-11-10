"""Private saved credentials never become a registered source-code scope."""
import pytest

from cairnmind.api_credentials import CredentialStore
from cairnmind.memory import MemoryManager
from cairnmind.store import Store


def test_new_project_and_worktree_cannot_enclose_saved_credentials(tmp_path):
    store = Store(tmp_path / 'data')
    project = store.add_project('safe', tmp_path / 'safe')
    CredentialStore(store.settings).set('api', 'SAVED_KEY', persist=True)
    with pytest.raises(ValueError, match='private credential store'):
        store.add_project('unsafe', tmp_path)
    with pytest.raises(ValueError, match='private credential store'):
        store.add_project_root(project['id'], tmp_path)
    assert len(store.list_projects()) == 1
    assert not store.query('SELECT FROM project_roots WHERE path=:path', {'path': str(tmp_path)})


def test_portable_import_and_dry_run_reject_private_credential_scope(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    source = Store(tmp_path / 'source-data')
    project = source.add_project('source-project', root)
    source.snapshot(project['id'], 'manual://evidence', 'Portable source', 'manual')
    package = MemoryManager(source).snapshot(project['id'])
    target = Store(tmp_path / 'target-data')
    CredentialStore(target.settings).set('api', 'SAVED_KEY', persist=True)
    for dry_run in (True, False):
        with pytest.raises(ValueError, match='private credential store'):
            MemoryManager(target).import_snapshot(package['path'], tmp_path, dry_run=dry_run)
    assert not target.list_projects()


def test_broker_refuses_persisting_key_into_existing_project_scope(tmp_path):
    import httpx
    from fastapi.testclient import TestClient
    from cairnmind.cockpit import create_cockpit_app
    from cairnmind.cockpit_state import CockpitState
    from cairnmind.service import create_app
    store = Store(tmp_path / 'data')
    store.add_project('legacy-wide-root', tmp_path)
    state = CockpitState(store.settings)
    state.update({'profiles': [*state.load()['profiles'], {'id': 'api', 'client': 'api', 'provider': 'openai',
                                                         'label': 'Cloud', 'model': 'tool-model'}]})
    memory = create_app(store.settings, start_worker=False)
    app = create_cockpit_app(store.settings, proxy_transport=httpx.ASGITransport(app=memory))
    origin = 'http://127.0.0.1:8766'
    with TestClient(app, base_url=origin) as client:
        ticket = app.state.browser_sessions.ticket()
        login = client.post('/auth/exchange', json={'ticket': ticket}, headers={'Origin': origin})
        headers = {'Origin': origin, 'X-CSRF-Token': login.json()['csrf']}
        response = client.post('/api/profiles/api/credential', json={'key': 'NEVER_SAVED', 'persist': True}, headers=headers)
        assert response.status_code == 409
        assert not CredentialStore(store.settings).path.exists()
        assert 'NEVER_SAVED' not in response.text
