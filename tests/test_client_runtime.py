"""Real tmux and PTY fixtures; native coding models are never invoked."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import shlex
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from cairnmind import client_auth, integrations
from cairnmind.client_runtime import NativeRuntime
from cairnmind.config import Settings


FAKE = r'''
import fcntl, json, os, signal, struct, sys, termios, tty
from pathlib import Path
client = Path(sys.argv[0]).name
args = sys.argv[1:]
if args == ['--version']:
    print('codex-cli 0.166.0' if client == 'codex' else '2.1.319 (Claude Code)')
    raise SystemExit
if args == ['--help'] or args == ['resume', '--help']:
    print('[PROMPT] mcp --no-daemon --resume SESSION_ID --model MODEL')
    raise SystemExit
if args == ['features', 'list']:
    print('hooks stable true')
    raise SystemExit
if args == ['login', 'status']:
    print('Logged in using ChatGPT')
    raise SystemExit
if args == ['auth', 'status', '--json']:
    print(json.dumps({'loggedIn':True,'authMethod':'claude.ai'}))
    raise SystemExit
if args and args[0] in {'login', 'auth'}:
    print('PRIVATE_DEVICE_CODE', flush=True)
    if os.environ.get('FIXTURE_AUTH_HOLD'):
        sys.stdin.readline()
    raise SystemExit
capture = os.environ.get('FIXTURE_CAPTURE')
if capture:
    Path(capture).write_text(json.dumps({'argv':args,'cwd':os.getcwd(),
        'session_id':os.environ.get('DEVBRAIN_SESSION_ID'),'packet_id':os.environ.get('DEVBRAIN_PACKET_ID')}))
signal.signal(signal.SIGINT, lambda *a: print('INTERRUPTED', flush=True))
signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
tty.setcbreak(0)
print('NATIVE_READY ' + str(os.getpid()), flush=True)
for line in sys.stdin:
    line = line.rstrip('\n')
    if line == 'quit':
        break
    if line == 'size':
        rows, cols, _, _ = struct.unpack('HHHH', fcntl.ioctl(0, termios.TIOCGWINSZ, b'\0'*8))
        print('SIZE '+str(cols)+'x'+str(rows), flush=True)
    elif line == 'child':
        import subprocess
        p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
        print('CHILD '+str(p.pid), flush=True)
    else:
        print('ECHO '+repr(line), flush=True)
'''


@pytest.fixture
def native(tmp_path, monkeypatch):
    binary = os.environ.get('DEVBRAIN_TEST_TMUX') or shutil.which('tmux')
    if not binary:
        pytest.skip('Real terminal tests require tmux >=3.4 (set DEVBRAIN_TEST_TMUX)')
    root = tmp_path / 'project'
    root.mkdir()
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    for name in ('codex', 'claude'):
        path = bindir / name
        path.write_text('#!' + sys.executable + '\n' + FAKE)
        path.chmod(0o700)
    monkeypatch.setenv('PATH', str(bindir) + os.pathsep + os.environ.get('PATH', ''))
    client_auth.clear_probe_cache()
    events = []

    def install(settings, project, *, clients=('codex', 'claude')):
        events.append(('install', clients))
        return {'warnings': [], 'clients': {name: {'verified_resume': True, 'mcp_configured': True,
            'hooks_configured': True, 'instructions_configured': True} for name in clients}}

    def prepare(settings, project, client, root, args):
        events.append(('prepare', client))
        sid = uuid4().hex
        return {'status': 'ready', 'session_id': sid, 'packet': {'id': uuid4().hex,
            'hash': 'f'*64, 'session_id': sid, 'project_id': project['id'], 'client': client,
            'worktree': str(root), 'active_plan': None, 'active_task': None}}

    monkeypatch.setattr(integrations, 'install_integrations', install)
    monkeypatch.setattr(integrations, '_prepare_session', prepare)
    settings = Settings.load(tmp_path / 'data', create=True)
    runtime = NativeRuntime(settings, tmux_binary=binary)
    if not runtime.diagnostics()['available']:
        pytest.skip('tmux version is older than3.4')
    project = {'id': uuid4().hex, 'name': 'terminal-test', 'root': str(root)}
    try:
        yield runtime, project, events
    finally:
        for row in runtime.list_runs():
            if row['lifecycle'] in {'starting','running','stopping'}:
                runtime.stop(row['id'], row['generation'])
        runtime._tmux('kill-server', check=False)
        client_auth.clear_probe_cache()


async def output(attachment, expected, timeout=5):
    data = b''
    deadline = asyncio.get_running_loop().time()+timeout
    while expected not in data:
        event = await asyncio.wait_for(attachment.queue.get(), deadline-asyncio.get_running_loop().time())
        if event['type'] == 'output':
            data += event['data']
        if event['type'] == 'gap':
            pytest.fail('Unexpected terminal backpressure')
    return data


def test_missing_tmux_is_lazy_and_actionable(tmp_path):
    settings = Settings.load(tmp_path/'data', create=True)
    runtime = NativeRuntime(settings, tmux_binary='/does-not-exist/tmux')
    assert not runtime.diagnostics()['available']
    assert runtime.list_runs() == []
    assert not runtime.directory.exists()
    with pytest.raises(ValueError, match='tmux_'):
        runtime.start({'id':'p','root':str(tmp_path)},'codex')
    assert not runtime.directory.exists()


def test_batch_installs_before_any_packet_and_keeps_independent_same_root(native):
    runtime, project, events = native
    result = runtime.start_project(project)
    assert result['errors'] == []
    first, second = result['runs']
    assert events == [('install', ('codex','claude')), ('prepare','codex'), ('prepare','claude')]
    assert first['worktree'] == second['worktree'] == project['root']
    assert first['session_id'] != second['session_id']
    assert first['packet_id'] != second['packet_id']
    assert all(row['lifecycle'] == 'running' for row in result['runs'])
    assert len(runtime.start_project(project)['runs']) == 2
    assert len(runtime.list_runs()) == 2
    assert (runtime.registry.stat().st_mode & 0o777) == 0o600
    assert (runtime.socket.stat().st_mode & 0o777) == 0o600
    assert (runtime.directory.stat().st_mode & 0o777) == 0o700
    assert not list(runtime.directory.glob('*.launch.json'))


def test_native_model_flags_keep_exact_bootstrap_and_environment(native, tmp_path, monkeypatch):
    runtime, project, _ = native
    capture = tmp_path/'capture.json'
    monkeypatch.setenv('FIXTURE_CAPTURE', str(capture))
    row = runtime.start(project, 'codex', model='gpt-fixture')
    deadline = time.monotonic()+3
    while not capture.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    seen = json.loads(capture.read_text())
    assert seen['argv'][:3] == ['--no-daemon','--model','gpt-fixture']
    assert 'resume_context' in seen['argv'][-1] and 'ack_resume' in seen['argv'][-1]
    assert row['session_id'] == seen['session_id']
    assert row['packet_id'] == seen['packet_id']
    assert seen['cwd'] == project['root']
    assert not any('dangerously' in a for a in seen['argv'])


@pytest.mark.asyncio
async def test_pty_unicode_resize_prefix_and_reconnect_without_restart(native):
    runtime, project, _ = native
    row = runtime.start(project, 'codex')
    attachment = await runtime.subscribe(row['id'], row['generation'], 'browser1')
    try:
        await output(attachment, b'NATIVE_READY')
        await attachment.write('zażółć\n'.encode())
        await output(attachment, 'zażółć'.encode())
        attachment.resize(101,33)
        await asyncio.sleep(.1)
        await attachment.write(b'size\n')
        await output(attachment, b'SIZE 101x33')
        await attachment.write(b'\x02\n')
        await output(attachment, b'\\x02')
        await attachment.close()
        assert runtime.get_run(row['id'])['pid'] == row['pid']
        second = await runtime.subscribe(row['id'], row['generation'], 'browser2')
        try:
            await output(second, b'NATIVE_READY')
            await second.write(b'after reconnect\n')
            await output(second, b'after reconnect')
        finally:
            await second.close()
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_two_browsers_have_one_controller_and_bounded_slow_output(native):
    runtime, project, _ = native
    row = runtime.start(project,'claude')
    first = await runtime.subscribe(row['id'],row['generation'],'first')
    second = await runtime.subscribe(row['id'],row['generation'],'second')
    try:
        assert first.controller and not second.controller
        with pytest.raises(ValueError,match='lease'):
            await second.write(b'not-authorized\n')
        second.take_control()
        assert second.controller and not first.controller
        with pytest.raises(ValueError,match='lease'):
            first.resize(90,30)
        for _ in range(200):
            first.channel.publish({'type':'output','data':b'x'*32768})
        assert first.overflowed and first.queue.qsize() == 1
        assert (await first.queue.get())['type'] == 'gap'
        assert runtime.get_run(row['id'])['lifecycle'] == 'running'
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_backend_recreation_discovers_existing_native_process(native):
    runtime, project, _ = native
    row = runtime.start(project,'codex')
    attached = await runtime.subscribe(row['id'],row['generation'],'oldbrowser')
    await output(attached,b'NATIVE_READY')
    await runtime.aclose()
    recovered = NativeRuntime(runtime.settings,tmux_binary=runtime.binary)
    assert recovered.get_run(row['id'])['pid'] == row['pid']
    assert recovered.start(project,'codex')['id'] == row['id']
    connection = await recovered.subscribe(row['id'],row['generation'],'newbrowser')
    try:
        await output(connection,b'NATIVE_READY')
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_interrupt_and_stale_generation_do_not_stop_other_native_client(native):
    runtime, project, _ = native
    first, second = runtime.start_project(project)['runs']
    attached = await runtime.subscribe(first['id'],first['generation'],'browser')
    try:
        await output(attached,b'NATIVE_READY')
        with pytest.raises(ValueError,match='generation'):
            runtime.stop(first['id'],first['generation']+1)
        runtime.interrupt(first['id'],first['generation'])
        await output(attached,b'INTERRUPTED')
        runtime.stop(first['id'],first['generation'])
        assert runtime.get_run(second['id'])['lifecycle'] == 'running'
        assert not attached.controller
    finally:
        await attached.close()


def test_private_server_loss_marks_interrupted_and_requires_explicit_supported_resume(native):
    runtime, project, _ = native
    row = runtime.start(project,'codex')
    runtime._tmux('kill-server')
    assert runtime.get_run(row['id'])['lifecycle'] == 'interrupted'
    with pytest.raises(ValueError,match='native_resume_unavailable'):
        runtime.start(project,'codex',resume=True)
    fresh = runtime.start(project,'codex')
    assert fresh['id'] != row['id'] and fresh['generation'] == row['generation']+1


def test_native_stop_refuses_changed_process_identity(native):
    runtime, project, _ = native
    row = runtime.start(project,'codex')
    with runtime._locked():
        rows = runtime._read()
        rows[0]['process_start'] = 'wrong-start-identity'
        runtime._save(rows)
    with pytest.raises(ValueError,match='identity'):
        runtime.stop(row['id'],row['generation'])
    with runtime._locked():
        rows[0]['process_start'] = row['process_start']
        runtime._save(rows)
    assert runtime.get_run(row['id'])['lifecycle'] == 'running'


def test_login_is_operational_and_not_project_memory(native,monkeypatch):
    runtime, project, _ = native
    monkeypatch.setenv('DEVBRAIN_SESSION_ID','must-not-inherit')
    monkeypatch.setenv('DEVBRAIN_PACKET_ID','must-not-inherit')
    monkeypatch.setenv('FIXTURE_AUTH_HOLD','1')
    row = runtime.start_auth('codex')
    assert row['purpose'] == 'auth' and row['session_id'] is None and row['project_id'] is None
    assert row['worktree'] != project['root']
    assert 'PRIVATE_DEVICE_CODE' not in runtime.registry.read_text()
    assert not list(runtime.directory.glob('*.launch.json'))


@pytest.mark.parametrize('state',['signed_out','expired_or_rejected'])
def test_rejected_auth_never_prepares_native_model_turn(native,monkeypatch,state):
    runtime,project,events=native
    monkeypatch.setattr(client_auth,'probe_client',lambda client:{'auth':state,'installation':'installed'})
    with pytest.raises(ValueError,match='login_required'):
        runtime.start(project,'codex')
    assert events == [] and runtime.list_runs() == []


def test_native_environment_auth_hint_does_not_require_subscription_login(native,monkeypatch):
    runtime,project,_=native
    monkeypatch.setattr(client_auth,'probe_client',lambda client:{'auth':'signed_out','installation':'installed',
        'credential_configuration':'environment_present','ready_to_launch':True})
    row=runtime.start(project,'codex')
    assert row['lifecycle']=='running' and row['session_id']


@pytest.mark.parametrize('client',['codex','claude'])
@pytest.mark.parametrize('purpose',['coding','auth'])
def test_independent_launch_removes_only_parent_invocation_markers(native,monkeypatch,client,purpose):
    runtime,project,_=native
    monkeypatch.setenv('CLAUDECODE','1')
    monkeypatch.setenv('CODEX_THREAD_ID','parent-native-thread')
    preserved={'CODEX_HOME':'/fixture/codex-home','CLAUDE_CONFIG_DIR':'/fixture/claude-home',
        'OPENAI_API_KEY':'fictional-test-value','ANTHROPIC_API_KEY':'fictional-test-value'}
    for key,value in preserved.items():
        monkeypatch.setenv(key,value)
    spawn=runtime._spawn
    seen=[]

    def inspect_environment(rows,project,client,slot_id,launch,generation,purpose):
        env=launch['env']
        assert 'CLAUDECODE' not in env and 'CODEX_THREAD_ID' not in env
        assert {key:env.get(key) for key in preserved}==preserved
        if purpose=='auth':
            assert not any(key.startswith('DEVBRAIN_') for key in env)
        else:
            assert env['DEVBRAIN_SESSION_ID']==launch['session_id']
        seen.append(purpose)
        return spawn(rows,project,client,slot_id,launch,generation,purpose)

    monkeypatch.setattr(runtime,'_spawn',inspect_environment)
    if purpose=='auth':
        monkeypatch.setenv('FIXTURE_AUTH_HOLD','1')
        runtime.start_auth(client)
    else:
        runtime.start(project,client)
    assert seen==[purpose]


def test_unsafe_model_identifiers_are_rejected_before_preparation(native):
    runtime,project,events=native
    with pytest.raises(ValueError,match='model identifier'):
        runtime.start(project,'codex',model='--disable-approvals')
    assert not events


def test_alternate_data_directory_has_private_supervisor_namespace(native,tmp_path):
    runtime,_,_=native
    other=NativeRuntime(Settings.load(tmp_path/'other-data',create=True),tmux_binary=runtime.binary)
    assert other.socket != runtime.socket
    assert other.list_runs() == []


def test_starting_intent_recovers_exec_identity_after_host_restart(native):
    runtime,project,_=native
    row=runtime.start(project,'codex')
    with runtime._locked():
        rows=runtime._read()
        rows[0].update(lifecycle='starting',state='starting')
        for field in ('pid','pgid','process_start'):
            rows[0].pop(field,None)
        runtime._save(rows)
    recovered=NativeRuntime(runtime.settings,tmux_binary=runtime.binary)
    current=recovered.get_run(row['id'])
    assert current['lifecycle']=='running' and current['pid']==row['pid']
    assert current['process_start']==row['process_start']
    assert recovered.start(project,'codex')['id']==row['id']


def test_project_cannot_shadow_private_launcher_module(native):
    runtime,project,_=native
    from pathlib import Path
    (Path(project['root'])/'cairnmind.py').write_text('raise RuntimeError("project shadow executed")')
    row=runtime.start(project,'codex')
    assert row['lifecycle']=='running'


def test_explicit_resume_uses_scoped_native_id_and_new_verified_prompt(native,tmp_path,monkeypatch):
    runtime,project,_=native
    capture=tmp_path/'resume.json'
    monkeypatch.setenv('FIXTURE_CAPTURE',str(capture))
    first=runtime.start(project,'codex')
    runtime.stop(first['id'],first['generation'])
    with runtime._locked():
        rows=runtime._read()
        rows[0]['native_session_id']='known-project-native-session'
        runtime._save(rows)
    capture.unlink(missing_ok=True)
    resumed=runtime.start(project,'codex',resume=True,model='gpt-fixture')
    deadline=time.monotonic()+3
    while not capture.exists() and time.monotonic()<deadline:
        time.sleep(.01)
    seen=json.loads(capture.read_text())
    assert seen['argv'][:5]==['--no-daemon','resume','known-project-native-session','--model','gpt-fixture']
    assert 'ack_resume' in seen['argv'][-1]
    assert resumed['session_id']!=first['session_id']
    assert resumed['generation']==first['generation']+1


def test_generated_mcp_and_hook_commands_ignore_project_python_shadow(native,monkeypatch):
    from pathlib import Path
    import tomllib
    runtime,project,_=native
    root=Path(project['root'])
    marker=root/'shadow-executed'
    (root/'cairnmind.py').write_text('from pathlib import Path\nPath('+repr(str(marker))+').write_text("unsafe")\nraise RuntimeError("shadow")')
    monkeypatch.setenv('PYTHONPATH',str(root))
    config=root/'.codex/config.toml'
    config.parent.mkdir()
    integrations._merge_toml_mcp(config,runtime.settings)
    mcp=tomllib.loads(config.read_text())['mcp_servers']['devbrain']
    assert mcp['args'][:3]==['-I','-m','cairnmind']
    completed=subprocess.run([mcp['command'],*mcp['args'],'--help'],cwd=root,capture_output=True,text=True,timeout=10)
    assert completed.returncode==0,completed.stderr
    hookpath=root/'.codex/hooks.json'
    integrations._merge_hooks(hookpath,runtime.settings,'codex')
    command=json.loads(hookpath.read_text())['hooks']['SessionStart'][0]['hooks'][0]['command']
    assert shlex.split(command)[1:4]==['-I','-m','cairnmind']
    completed=subprocess.run(shlex.split(command),cwd=root,input='{}',capture_output=True,text=True,timeout=10)
    assert completed.returncode==0 and not marker.exists(),completed.stderr
