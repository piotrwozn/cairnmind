"""Production engine + protocol adapters + memory service + real ArcadeDB.

The HTTP provider is controlled and never spends a remote model's allowance.
Its next tool call is derived from the actual prior service/file tool result.
"""
import json
import subprocess
from uuid import uuid4

import httpx
import pytest

from devbrain.agent_memory import AgentMemoryClient
from devbrain.api_agent import AgentEngine
from devbrain.api_profiles import normalize_profile
from devbrain.api_providers import make_provider
from devbrain.service import create_app
from devbrain.sessions import SessionManager
from devbrain.store import Store


def last_result(provider, request):
    if provider == 'openai':
        return json.loads(next(row['output'] for row in reversed(request['input']) if row.get('type') == 'function_call_output'))
    if provider == 'anthropic':
        for message in reversed(request['messages']):
            for block in reversed(message['content']):
                if block.get('type') == 'tool_result':
                    return json.loads(block['content'])
    return json.loads(next(row['content'] for row in reversed(request['messages']) if row['role'] == 'tool'))


def response(provider, index, name=None, arguments=None):
    identifier = 'call-' + str(index)
    text = '' if name else 'Implemented and checkpointed with source evidence.'
    if provider == 'openai':
        output = [{'type': 'function_call', 'call_id': identifier, 'name': name, 'arguments': json.dumps(arguments)}] if name else [
            {'type': 'message', 'content': [{'type': 'output_text', 'text': text}]}]
        return httpx.Response(200, json={'status': 'completed', 'output': output,
                                        'usage': {'input_tokens': 100, 'output_tokens': 10}})
    if provider == 'anthropic':
        content = [{'type': 'tool_use', 'id': identifier, 'name': name, 'input': arguments}] if name else [{'type': 'text', 'text': text}]
        return httpx.Response(200, json={'content': content, 'stop_reason': 'tool_use' if name else 'end_turn',
                                        'usage': {'input_tokens': 100, 'output_tokens': 10}})
    if provider == 'ollama':
        message = {'role': 'assistant', 'content': text}
        if name:
            message['tool_calls'] = [{'function': {'name': name, 'arguments': arguments}}]
        return httpx.Response(200, json={'message': message, 'done': True, 'done_reason': 'stop',
                                        'prompt_eval_count': 100, 'eval_count': 10})
    message = {'role': 'assistant', 'content': text}
    if name:
        message['tool_calls'] = [{'id': identifier, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}]
    return httpx.Response(200, json={'choices': [{'index': 0, 'message': message, 'finish_reason': 'tool_calls' if name else 'stop'}],
                                    'usage': {'prompt_tokens': 100, 'completion_tokens': 10}})


@pytest.mark.parametrize('provider', ['openai', 'anthropic', 'openai_compatible', 'ollama'])
@pytest.mark.asyncio
async def test_actual_ack_file_edit_refresh_checkpoint_and_native_handoff(tmp_path, provider):
    root = tmp_path / 'project'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    (root / 'app.py').write_text('VALUE = 1\n')
    subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
                    'commit', '-qm', 'Initial'], check=True)
    store = Store(tmp_path / 'data')
    project = store.add_project('mixed-agent-project', root)
    manager = SessionManager(store, store.settings)
    sid = uuid4().hex
    service = create_app(store.settings, start_worker=False)
    memory = AgentMemoryClient(store.settings, project['id'], root, sid, transport=httpx.ASGITransport(app=service))
    calls = []
    observed_packets = []
    def model(request):
        if request.url.path.endswith('/api/show'):
            return httpx.Response(200, json={'model_info': {'general.architecture': 'qwen3',
                                                          'qwen3.context_length': 131072}})
        payload = json.loads(request.content)
        index = len(calls)
        calls.append(payload)
        if index in {0, 4}:
            return response(provider, index, 'resume_context', {})
        if index in {1, 5}:
            prior = last_result(provider, payload)
            packet = prior['packet']
            assert prior['status'] == packet['status'] == 'ready'
            observed_packets.append(packet)
            readback = {name: ({'id': packet[field]['id'], 'version': packet[field]['version']} if packet.get(field) else None)
                        for name, field in [('plan', 'active_plan'), ('task', 'active_task')]}
            return response(provider, index, 'ack_resume', {'packet_id': packet['id'], 'packet_hash': packet['hash'], 'readback': readback})
        if index == 2:
            assert last_result(provider, payload)['status'] == 'acknowledged'
            return response(provider, index, 'read_file', {'path': 'app.py'})
        if index == 3:
            prior = last_result(provider, payload)
            assert prior['ok'] is True
            return response(provider, index, 'write_file', {'path': 'app.py', 'content': 'VALUE = 2\n',
                                                           'expected_sha256': prior['sha256']})
        if index == 6:
            assert last_result(provider, payload)['status'] == 'acknowledged'
            return response(provider, index, 'checkpoint', {'state': {'goal': 'Shared API work',
                                                                     'results': ['VALUE updated to 2'],
                                                                     'next_step': 'Independent native review'}})
        if index == 7:
            return response(provider, index)
        raise AssertionError('No blind model retries')
    profile = normalize_profile({'id': 'api', 'client': 'api', 'label': 'Controlled API', 'provider': provider,
                                 'model': 'tool-model', 'base_url': 'http://127.0.0.1:12345'})
    model_http = httpx.AsyncClient(transport=httpx.MockTransport(model))
    adapter = make_provider(profile, 'FIXTURE_KEY', client=model_http)
    run = {'id': uuid4().hex, 'generation': 1, 'project_id': project['id'], 'worktree': str(root),
           'session_id': sid, 'client': 'api'}
    engine = AgentEngine(store.settings, run, profile, key='FIXTURE_KEY', provider=adapter, memory=memory)
    try:
        assert not store.query('SELECT FROM resume_acks')
        result = await engine.turn('Change VALUE to 2 and leave the review checkpoint.')
        assert result['status'] == 'completed', result
        assert (root / 'app.py').read_text() == 'VALUE = 2\n'
        assert len(calls) == 8
        assert len(observed_packets) == 2
        assert observed_packets[0]['code']['fingerprint'] != observed_packets[1]['code']['fingerprint']
        assert len(store.query('SELECT FROM resume_acks WHERE session_id=:id', {'id': sid})) == 2
        sources = store.query("SELECT FROM sources WHERE project_id=:id AND kind='agent_transcript'", {'id': project['id']})
        assert sources
        assert all('FIXTURE_KEY' not in row['content'] for row in sources)
        restored = manager.prepare(project['id'], 'claude', str(root), timeout=0)
        assert restored['status'] == 'ready', restored['unknowns']
        assert not manager.status(restored['session_id'])['acknowledged']
        checkpoint = next(row for row in restored['packet']['records'] if row['kind'] == 'checkpoint')
        assert 'Independent native review' in json.dumps(checkpoint)
        assert checkpoint['source_ids']
        assert manager._session(sid)['native_session_id'] is None
    finally:
        await engine.close()
        await model_http.aclose()
