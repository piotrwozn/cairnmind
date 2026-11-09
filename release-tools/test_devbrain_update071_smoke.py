#!/usr/bin/env python3
"""Exact guarded 0.7.0 -> 0.7.1 install, isolated tools/data and real terminal."""
from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from uuid import uuid4
import zipfile


BASELINES = {version: Path(f"/workspace/devbrain-{version}.zip") for version in ("0.7.0",)}
UPDATER = Path("/workspace/devbrain-update-0.7.1.py")
MANIFEST = Path("/workspace/devbrain-update-0.7.1-manifest.json")
SHARED = Path("/tmp/devbrain-managed-arcade-f80vghvg")
TMUX = Path("/tmp/devbrain-tmux-3.4/tmux")
DIST = Path("/tmp/devbrain-arcade-dist/arcadedb-26.11.1")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def write_json(path, document):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
        json.dump(document, handle, indent=2)
        handle.write("\n")


def inventory(root):
    return {path.relative_to(root).as_posix(): digest(path.read_bytes()) for path in root.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"}


def file_signatures(paths):
    """Detect rewrites even when the resulting bytes are identical."""
    return {str(path): {"sha256": digest(path.read_bytes()), "mode": path.stat().st_mode & 0o777,
                       "mtime_ns": path.stat().st_mtime_ns, "inode": path.stat().st_ino}
            for path in paths if path.is_file()}


def extract(archive_path, destination):
    destination.mkdir(parents=True)
    with zipfile.ZipFile(archive_path) as archive:
        for entry in archive.infolist():
            if entry.is_dir():
                continue
            relative = PurePosixPath(entry.filename.removeprefix("devbrain/"))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Unsafe release ZIP path")
            path = destination.joinpath(*relative.parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.read(entry))


SEED = r'''
import json,sys
from pathlib import Path
import devbrain
from devbrain.config import Settings
from devbrain.store import Store
data,root,neighbor,output=map(Path,sys.argv[1:5]);version=sys.argv[5]
s=Settings.load(data);s.ollama_url='http://127.0.0.1:1';s.port=int(sys.argv[6]);s.save()
store=Store(data);p=store.add_project('update-fixture',root);n=store.add_project('keep-neighbor',neighbor)
source=store.snapshot(p['id'],'manual://canary','Preserve the exact decision content.','manual')
record=store.remember(p['id'],{'kind':'decision','content':'Preserve the exact decision content.','status':'confirmed'},[source['id']])
other=store.snapshot(n['id'],'manual://neighbor','Preserve unrelated project evidence.','manual')
store.remember(n['id'],{'kind':'fact','content':'Preserve unrelated project evidence.'},[other['id']])
output.write_text(json.dumps({'export':store.export(),'project':p,'neighbor':n,'source':source,'record':record}))
assert devbrain.__version__==version
schema=store.query("SELECT value FROM devbrain_meta WHERE name='schema_version'")[0]['value']
assert schema==4
print(json.dumps({'baseline':version,'schema':schema,'projects':2}))
'''


PROBE = r'''
import importlib.metadata,json,sys
from pathlib import Path
import devbrain
from devbrain.config import Settings
from devbrain.store import Store
from devbrain.journal import Journal
data,oldfile,source=map(Path,sys.argv[1:4]);old=json.loads(oldfile.read_text());store=Store(data)
assert devbrain.__version__==importlib.metadata.version('devbrain')=='0.7.1'
assert store.query("SELECT value FROM devbrain_meta WHERE name='schema_version'")[0]['value']==4
current=store.export()
for key,rows in old['export'].items():
    if key not in {'format_version','exported_at'}:assert current[key]==rows,key
assert store.get_source(old['source']['id'])['content']==old['source']['content']
assert store.get_source(old['source']['id'])['sha256']==old['source']['sha256']
assert Journal(store).cursor()['epoch']
package=Path(devbrain.__file__).parent;matched=[]
for path in (source/'src/devbrain').rglob('*'):
    if path.is_file() and '__pycache__' not in path.parts and path.suffix!='.pyc':
        relative=path.relative_to(source/'src/devbrain')
        assert (package/relative).read_bytes()==path.read_bytes(),str(relative);matched.append(str(relative))
for name in ('workspace.py','terminal_panels.py','terminal_inspector.py','terminal_graph.py','terminal_client.py','graph_raster.py'):
    assert (package/name).is_file(),name
for name in ('api_agent.py','api_worker.py','api_runtime.py','api_profiles.py','api_credentials.py','api_providers.py','agent_memory.py','agent_tools.py','agent_journal.py','copilot_adapter.py'):
    assert (package/name).is_file(),name
from devbrain.api_credentials import CredentialStore
from devbrain.copilot_adapter import runtime_status
assert CredentialStore(Settings.load(data)).resolve({'credential_ref':'profile:upgrade-key'})=='API_PRIVATE_CANARY_071'
assert importlib.metadata.version('github-copilot-sdk')=='1.0.29'
observed=runtime_status(Settings.load(data))
assert observed['available'] is False and observed['code']=='copilot_runtime_missing',observed
assert not (data/'runtime/copilot-runtime').exists()
print(json.dumps({'version':'0.7.1','schema':4,'legacy_rows_preserved':True,
    'source_hashes_preserved':True,'packaged_files_match':len(matched),'packaged_terminal_modules':True,'packaged_api_modules':True,'persisted_api_key_preserved':True,'copilot_runtime_not_downloaded':True,
    'database':store.settings.arcade_database}))
'''


GRAPH_PAINT = r'''
import asyncio,json,sys
from dataclasses import replace
from pathlib import Path
from rich.text import Text
from textual.app import App,ComposeResult
from textual.geometry import Region
from textual.strip import Strip
from textual.style import Style
from devbrain.store import Store
from devbrain.inspector import Inspector
from devbrain.terminal_graph import GraphModel,TerminalGraph,render_scene

data,oldfile=map(Path,sys.argv[1:]);old=json.loads(oldfile.read_text())
projection=Inspector(Store(data)).graph(old['project']['id'])
ids={node['id'] for node in projection['nodes']}
assert 'record:'+old['record']['id'] in ids and 'source:'+old['source']['id'] in ids
model=GraphModel(motion=False);assert model.accept(projection)
def failed_conversion(cls,*args,**kwargs):
 raise TypeError("Style.from_rich_style() missing 1 required positional argument: 'rich_style'")
Style.from_rich_style=classmethod(failed_conversion)
class Painter(App):
 def compose(self)->ComposeResult:yield TerminalGraph(model)
 def invalidate_frame(self):pass
async def verify():
 app=Painter();seen=[]
 async with app.run_test(size=(80,24)) as pilot:
  widget=app.query_one(TerminalGraph)
  for _ in range(3):
   frame=await asyncio.to_thread(render_scene,model.nodes,model.edges,model.positions,replace(model.camera),model.selected_id,{},widget.size.width,widget.size.height,animate=False)
   assert frame.plain.strip() and frame.spans
   widget.set_frame(frame);await pilot.pause(.12)
   strips=widget.render_lines(Region(0,0,widget.size.width,widget.size.height))
   assert all(isinstance(strip,Strip) for strip in strips)
   assert any(segment.style and segment.style.color for strip in strips for segment in strip)
   text='\n'.join(strip.text for strip in strips);assert text.strip()
   assert text=='\n'.join(strip.text for strip in app.screen._compositor.render_strips())
   assert 'Span(' not in text and 'TypeError' not in text
   seen.append(text);model.camera.rotate(yaw=.35,pitch=.07)
  assert len(set(seen))==3
  await pilot.resize_terminal(43,12)
  frame=render_scene(model.nodes,model.edges,model.positions,replace(model.camera),model.selected_id,{},widget.size.width,widget.size.height,animate=False)
  widget.set_frame(frame);await pilot.pause(.12)
  resized=widget.render_lines(Region(0,0,widget.size.width,widget.size.height))
  assert len(resized)==12 and all(strip.cell_length==43 for strip in resized)
  assert ''.join(strip.text for strip in resized).strip()
  widget.set_frame(Text(''));await pilot.pause(.12)
  assert all(not strip.text.strip() for strip in widget.render_lines(Region(0,0,widget.size.width,widget.size.height)))
 print(json.dumps({'packaged_graph_actual_memory_nodes':len(ids),'packaged_graph_injected_style_failure_bypassed':True,'packaged_graph_real_painter_rotated_frames':len(set(seen)),'packaged_graph_resize_and_clear':True}))
asyncio.run(verify())
'''


CLEANUP = r'''
import json,sys
from pathlib import Path
from devbrain.config import Settings
from devbrain.arcade import ArcadeClient
settings=Settings.load(Path(sys.argv[1]));prefix=sys.argv[2]
assert settings.arcade_database.startswith(prefix+'_')
client=ArcadeClient(settings)
client.server_command('drop database '+settings.arcade_database)
assert client.server_info()
print(json.dumps({'only_owned_database_removed':True,'shared_engine_kept_running':True}))
'''


OLD_SSE = r'''
import json,sys
from pathlib import Path
from urllib.parse import parse_qs,urlsplit
import httpx
launch,ready=map(Path,sys.argv[1:]);info=json.loads(launch.read_text())
parts=urlsplit(info['url']);origin=parts.scheme+'://'+parts.netloc
ticket=parse_qs(parts.fragment)['ticket'][0]
try:
 with httpx.Client(base_url=origin,trust_env=False,timeout=None) as client:
  exchanged=client.post('/auth/exchange',json={'ticket':ticket},headers={'Origin':origin})
  exchanged.raise_for_status()
  state=client.get('/api/state');state.raise_for_status();assert not state.json()['runs']
  with client.stream('GET','/api/events') as response:
   response.raise_for_status();ready.write_text('ready')
   for _ in response.iter_lines():pass
except httpx.HTTPError as error:
 print(json.dumps({'closed_reason':type(error).__name__}))
else:print(json.dumps({'stream_closed':True}))
'''


REFRESH_OLD = r'''
import json,sys,time
from pathlib import Path
import httpx
from devbrain.config import Settings
from devbrain.store import Store
data,oldfile=map(Path,sys.argv[1:]);s=Settings.load(data);store=Store(data)
deadline=time.monotonic()+45
with httpx.Client(trust_env=False,timeout=5) as client:
 while time.monotonic()<deadline:
  result=client.get(s.url+'/status',headers={'Authorization':'Bearer '+s.token})
  result.raise_for_status()
  if result.json().get('background',{}).get('last_scan_at'):break
  time.sleep(.2)
 else:raise AssertionError('Old service initial scan did not finish')
old=json.loads(oldfile.read_text())
assert store.get_source(old['source']['id'])==old['source']
assert next(row for row in store.list_records(old['project']['id']) if row['id']==old['record']['id'])==old['record']
current=store.export()
for key in ('sources','records','record_revisions'):
 for row in old['export'][key]:assert row in current[key],key
old['export']=current;oldfile.write_text(json.dumps(old))
print(json.dumps({'old_initial_scan_finished':True,'immutable_original_evidence_retained':True}))
'''


FAKE_NATIVE = r'''
import json,os,sys
from pathlib import Path
client=Path(sys.argv[0]).name;args=sys.argv[1:]
path=Path(os.environ['FIXTURE_071_NATIVE_TRACE'])
with path.open('a') as out:out.write(json.dumps({'client':client,'args':args})+'\n')
if args==['--version']:
 print('codex-cli 0.166.0' if client=='codex' else '2.1.319 (Claude Code)');raise SystemExit
if '--help' in args:
 print('[PROMPT] mcp --no-daemon hooks --resume SESSION_ID --model MODEL');raise SystemExit
if args==['features','list']:
 print('hooks stable true');raise SystemExit
if args==['login','status']:
 print('Not logged in');raise SystemExit(1)
if args==['auth','status','--json']:
 print(json.dumps({'loggedIn':False,'authMethod':None}));raise SystemExit
print('UNEXPECTED_NATIVE_CODING_START_071',flush=True)
raise SystemExit(91)
'''


def free_port():
    with socket.socket() as channel:
        channel.bind(("127.0.0.1", 0))
        return channel.getsockname()[1]


def run_baseline(version, directory, manifest, uv_cache):
    started = time.monotonic()
    directory.mkdir(mode=0o700)
    source, rejected, data, state = (directory / name for name in ("source", "rejected", "data", "state"))
    tools, bin_dir, isolated_home = (directory / name for name in ("tools", "bin", "home"))
    bin_dir.mkdir()
    isolated_home.mkdir()
    shared = json.loads((SHARED / "config.json").read_text())
    prefix = "devbrain_update071_" + uuid4().hex[:10]
    env = {key: value for key, value in os.environ.items() if not key.startswith("DEVBRAIN_")
        and key not in {"UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "TMUX", "TMUX_PANE"}}
    env.update(HOME=str(isolated_home), DEVBRAIN_HOME=str(data), UV_TOOL_DIR=str(tools), UV_TOOL_BIN_DIR=str(bin_dir),
        UV_CACHE_DIR=uv_cache, UV_OFFLINE="true", XDG_STATE_HOME=str(state),
        DEVBRAIN_ARCADE_DIST=str(DIST), DEVBRAIN_ARCADE_DB_PREFIX=prefix,
        DEVBRAIN_ARCADE_URL=shared["arcade_url"], DEVBRAIN_ARCADE_USER=shared["arcade_user"],
        DEVBRAIN_ARCADE_PASSWORD=shared["arcade_password"], PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
        TERM="xterm-256color", LANG="C.UTF-8", FIXTURE_071_NATIVE_TRACE=str(directory / "native-trace.jsonl"))
    (bin_dir / "tmux").symlink_to(TMUX)
    for name in ("codex", "claude"):
        path = bin_dir / name
        path.write_text("#!" + sys.executable + "\n" + FAKE_NATIVE)
        path.chmod(0o700)
    browser_log = directory / "browser-trace"
    for name in ("xdg-open", "wslview", "sensible-browser"):
        path = bin_dir / name
        path.write_text("#!" + sys.executable + "\nfrom pathlib import Path\n"
                        + "Path(" + repr(str(browser_log)) + ").write_text('BROWSER_CALLED')\nraise SystemExit(92)\n")
        path.chmod(0o700)
    env["BROWSER"] = str(bin_dir / "xdg-open")
    binary, python = bin_dir / "devbrain", tools / "devbrain/bin/python"
    checks, counter, tty_process, master = {}, 0, None, None
    old_sse, old_sse_log = None, None
    screen, screen_lock = bytearray(), threading.Lock()
    reader_stop, terminal_reader = threading.Event(), None
    cleanup_errors = []
    socket_directory = Path(tempfile.gettempdir()) / ("devbrain-ui-" + str(os.getuid()) + "-"
        + digest((str(data.resolve()) + ":default").encode())[:16])
    engine_identity_file = SHARED / "arcade/process.json"
    engine_before = file_signatures([engine_identity_file])
    summary = {"status": "running", "baseline": version, "workdir": str(directory),
               "baseline_archive_sha256": digest(BASELINES[version].read_bytes()),
               "actual_paid_native_or_model_inference": False, "checks": checks}

    def run(args, label, expected=0, timeout=300, environment=None):
        nonlocal counter
        counter += 1
        result = subprocess.run(list(map(str, args)), cwd=directory, env=environment or env,
                                capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        log = directory / f"{counter:02d}-{label}.log"
        log.write_text(result.stdout + result.stderr)
        log.chmod(0o600)
        if expected is not None:
            assert result.returncode == expected, (label, result.returncode, str(log))
        return result

    def protected_state():
        paths = [data / "cockpit/server.json", data / "workspaces/default/supervisor.json"]
        signatures = {str(path): {key: value for key, value in json.loads(path.read_text()).items()
                                 if key in {"pid", "birth", "instance", "workspace_id"}} for path in paths if path.is_file()}
        expected = ["-m", "devbrain", "--data-dir", str(data), "serve"]
        services = {}
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                command = (proc / "cmdline").read_bytes().decode().split("\0")
                if any(command[index:index + len(expected)] == expected for index in range(len(command))):
                    services[proc.name] = (proc / "stat").read_text().rsplit(")", 1)[1].split()[19]
            except (OSError, UnicodeError):
                continue
        assert len(services) == 1, "Expected one installed memory service process"
        signatures["memory_service"] = services
        return signatures

    def drain():
        if master is None:
            return b""
        output = bytearray()
        for _ in range(40):
            if not select.select([master], [], [], 0)[0]:
                break
            try:
                chunk = os.read(master, 65536)
            except (BlockingIOError, OSError):
                break
            if not chunk:
                break
            output.extend(chunk)
        return bytes(output)

    def collect_terminal():
        # A physical terminal keeps reading while the parent waits. Without
        # this reader, tmux can block flushing its final screen during detach.
        while not reader_stop.is_set():
            try:
                readable = select.select([master], [], [], .1)[0]
            except (OSError, ValueError):
                return
            if not readable:
                continue
            chunk = drain()
            if chunk:
                with screen_lock:
                    screen.extend(chunk)
                    screen[:] = screen[-1024 * 1024:]
            else:
                # PTY EOF/EIO remains readable; avoid spinning after exit.
                reader_stop.wait(.05)

    def terminal_snapshot():
        with screen_lock:
            return bytes(screen)

    def cleanup_step(label, action):
        try:
            return action()
        except Exception as error:
            cleanup_errors.append({"step": label, "type": type(error).__name__, "error": str(error)})
            return None

    def owned_processes():
        found = []
        marker = os.fsencode(str(directory)) + b"/"
        for process in Path("/proc").iterdir():
            if not process.name.isdigit() or int(process.name) == os.getpid():
                continue
            try:
                identity = (process / "stat").read_text().rsplit(")", 1)[1].split()
                if identity[0] != "Z" and marker in (process / "cmdline").read_bytes():
                    found.append({"pid": int(process.name), "birth": identity[19]})
            except (OSError, IndexError):
                continue
        return found

    def verify_owned_processes_stopped():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not owned_processes():
                checks["no_owned_processes_after_cleanup"] = True
                return
            time.sleep(.1)
        raise AssertionError({"remaining_owned_processes": owned_processes()})

    def remove_owned_dead_socket():
        terminal_socket = socket_directory / "tmux.sock"
        # Query only the deterministic socket belonging to this data fixture.
        result = subprocess.run([str(TMUX), "-S", str(terminal_socket), "list-sessions"],
            cwd=directory, env=env, capture_output=True, text=True, timeout=5)
        assert result.returncode != 0, "Fixture terminal server still running"
        if terminal_socket.exists():
            info = terminal_socket.lstat()
            assert stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid()
            terminal_socket.unlink()
        if socket_directory.exists():
            assert socket_directory.stat().st_uid == os.getuid()
            socket_directory.rmdir()
        checks["owned_terminal_socket_removed_after_server_exit"] = True

    try:
        extract(BASELINES[version], source)
        extract(BASELINES[version], rejected)
        project, neighbor = directory / "project", directory / "neighbor"
        project.mkdir()
        neighbor.mkdir()
        protected = {
            project / ".codex/config.toml": b'approval_policy = "on-request"\nsandbox_mode = "workspace-write"\n',
            project / ".claude/settings.local.json": b'{"permissions":{"deny":["Bash(rm:*)"]}}\n',
            project / "AGENTS.md": b"Preserve native permissions.\n",
            isolated_home / ".codex/auth.json": b'{"fixture_auth":"PRIVATE_AUTH_CANARY_071"}\n',
            isolated_home / ".claude/.credentials.json": b'{"fixture_auth":"PRIVATE_AUTH_CANARY_071"}\n',
        }
        for path, raw in protected.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            path.chmod(0o600)
        run(["bash", source / "scripts/install-wsl.sh"], "baseline-install", timeout=600)
        baseline = directory / "baseline.json"
        run([python, "-I", "-B", "-c", SEED, data, project, neighbor, baseline, version, free_port()], "seed-baseline")
        configuration = data / "config.json"
        values = json.loads(configuration.read_text())
        values["cockpit_port"] = free_port()
        values["poll_seconds"] = 3600
        write_json(configuration, values)
        config_before = configuration.read_bytes()
        model_lock = data / "models.lock.json"
        model_lock.write_text('{"canary":"preserve existing models metadata"}\n')
        model_lock.chmod(0o600)
        protected[model_lock] = model_lock.read_bytes()
        api_keys = data / "credentials/api-keys.json"
        write_json(api_keys, {"version": 1, "keys": {"upgrade-key": "API_PRIVATE_CANARY_071"}})
        api_keys.parent.chmod(0o700)
        protected[api_keys] = api_keys.read_bytes()
        preferences = data / "cockpit/preferences.json"
        old_preferences = {"version": 2, "profiles": [
            {"id": "codex", "client": "codex", "label": "Custom Codex", "model": "native-fixture-model"},
            {"id": "claude", "client": "claude", "label": "Custom Claude"},
            {"id": "upgrade-key", "client": "api", "label": "Preserve API", "provider": "openai",
             "model": "fixture-model", "credential_ref": "profile:upgrade-key"},
            {"id": "copilot", "client": "copilot", "label": "Preserve Copilot", "model": "fixture-model",
             "credential_ref": "env:UPGRADE_FIXTURE_GITHUB_TOKEN"}],
            "scan_roots": [], "default_slots": [{"id": "codex", "profile_id": "codex"}, {"id": "claude", "profile_id": "claude"}],
            "project_slots": {}, "last_project": None}
        write_json(preferences, old_preferences)
        old_preferences_raw = preferences.read_bytes()
        before = inventory(source)
        run([sys.executable, UPDATER, "--source-dir", source, "--check-only"], "check-only")
        assert inventory(source) == before and not state.exists()
        checks["check_only_nonmutating"] = True
        changed = next(row for row in manifest["files"] if (rejected / row["path"]).is_file()
                       and digest((rejected / row["path"]).read_bytes()) != row["after"])
        target = rejected / changed["path"]
        target.write_bytes(target.read_bytes() + b"\nLOCAL_CHANGE_CANARY\n")
        before = inventory(rejected)
        run([sys.executable, UPDATER, "--source-dir", rejected], "reject-local-change", expected=1)
        assert inventory(rejected) == before and not state.exists()
        checks["local_edit_guard"] = True
        target.write_bytes((source / changed["path"]).read_bytes())
        target.unlink()
        before = inventory(rejected)
        run([sys.executable, UPDATER, "--source-dir", rejected, "--check-only"], "reject-deleted-baseline-file", expected=1)
        assert inventory(rejected) == before and not state.exists()
        checks["deleted_baseline_file_guard"] = True
        if version == "0.7.0":
            launched = run([binary, "cockpit", "open", "--no-browser"], "old-cockpit-sse-launch")
            launch_file, ready_file = directory / "old-cockpit-launch.json", directory / "old-sse-ready"
            write_json(launch_file, json.loads(launched.stdout))
            old_sse_log = (directory / "old-sse.log").open("wb")
            (directory / "old-sse.log").chmod(0o600)
            old_sse = subprocess.Popen([str(python), "-I", "-B", "-c", OLD_SSE, str(launch_file), str(ready_file)],
                                       cwd=directory, env=env, stdout=old_sse_log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 45
            while not ready_file.exists() and time.monotonic() < deadline:
                assert old_sse.poll() is None, "Old SSE client exited before ready"
                time.sleep(.1)
            assert ready_file.exists(), "Old cockpit SSE never became ready"
            run([python, "-I", "-B", "-c", REFRESH_OLD, data, baseline], "old-cockpit-stable-baseline")
            assert old_sse.poll() is None
        applied = run([sys.executable, UPDATER, "--source-dir", source], "apply-exact-updater", timeout=600)
        if old_sse is not None:
            old_sse.wait(timeout=20)
            assert old_sse.returncode == 0
            checks["old_0_7_active_sse_stops_without_hanging_update"] = True
            checks["verified_host_pidfd_shutdown_fallback_exercised"] = "Zamykanie starego hosta interfejsu" in applied.stdout
        probe = json.loads(run([python, "-I", "-B", "-c", PROBE, data, baseline, source], "probe-preservation").stdout)
        checks.update({key: value for key, value in probe.items() if key != "database"})
        graph_paint = run([python, "-I", "-B", "-c", GRAPH_PAINT, data, baseline], "installed-graph-real-painter", timeout=60)
        checks.update(json.loads(graph_paint.stdout))
        assert probe["database"].startswith(prefix + "_")
        assert configuration.read_bytes() == config_before
        for path, raw in protected.items():
            assert path.read_bytes() == raw and path.stat().st_mode & 0o777 == 0o600, str(path)
        checks["config_models_native_credentials_permissions_preserved"] = True
        assert preferences.read_bytes() == old_preferences_raw
        checks["v2_native_api_copilot_profiles_preserved_nonmutating"] = True
        loaded = run([python, "-I", "-B", "-c", "import json,sys;from pathlib import Path;from devbrain.config import Settings;from devbrain.cockpit_state import CockpitState;s=CockpitState(Settings.load(Path(sys.argv[1])));value=s.load();assert value['version']==2;assert len(value['profiles'])==4;assert {p['client'] for p in value['profiles']}=={'codex','claude','api','copilot'};assert next(p for p in value['profiles'] if p['client']=='api')['credential_ref']=='profile:upgrade-key';print(json.dumps({'profiles_preserved':True}))", data], "profile-format-2-preservation")
        assert json.loads(loaded.stdout)["profiles_preserved"]
        assert preferences.read_bytes() == old_preferences_raw
        assert not list(preferences.parent.glob("preferences-v1-backup-*.json"))
        checks["v2_profiles_need_no_migration_or_new_backup"] = True
        for row in manifest["files"]:
            assert digest((source / row["path"]).read_bytes()) == row["after"], row["path"]
        checks["source_payload_verified"] = len(manifest["files"])
        backups = sorted((state / "devbrain-updates").glob("*/database.devbrain.json.gz"))
        assert len(backups) == 1
        with gzip.open(backups[0], "rt") as handle:
            backup = json.load(handle)
        assert backup["payload"]["format_version"] == 5
        assert backup["sha256"] == digest(json.dumps(backup["payload"], ensure_ascii=False,
            separators=(",", ":"), sort_keys=True).encode())
        old = json.loads(baseline.read_text())["export"]
        for key, rows in old.items():
            if key not in {"format_version", "exported_at"}:
                assert backup["payload"][key] == rows, key
        source_backup = backups[0].parent
        saved_paths = json.loads((source_backup / "manifest.json").read_text())
        with zipfile.ZipFile(BASELINES[version]) as baseline_zip:
            for item in saved_paths:
                path = source_backup / item["path"]
                if item["existed"]:
                    assert path.read_bytes() == baseline_zip.read("devbrain/" + item["path"]), item["path"]
                    assert path.stat().st_mode & 0o777 == 0o600
                else:
                    assert not path.exists()
        checks["explicit_pre_update_source_backup_private_and_exact"] = len(saved_paths)
        checks["explicit_pre_update_database_backup"] = backup["payload"]["format_version"]
        assert "API_PRIVATE_CANARY_071" not in json.dumps(backup)
        assert "PRIVATE_AUTH_CANARY_071" not in json.dumps(backup)
        checks["database_backup_excludes_local_credentials"] = True
        migrations = list((data / "backups").glob("before-schema-3-to-4-*.devbrain.json.gz"))
        assert not migrations
        checks["schema_4_unchanged_no_new_database_migration"] = True
        missing = directory / "never-initialized"
        for arguments in ([], ["tui"]):
            result = run([binary, "--data-dir", missing, *arguments], "no-tty", expected=2)
            assert "terminal" in (result.stdout + result.stderr).lower() and not missing.exists()
        assert not browser_log.exists()
        checks["no_tty_exit_2_without_initialization_or_browser"] = True
        assert json.loads(run([binary, "version"], "version").stdout)["version"] == "0.7.1"
        assert not json.loads(run([binary, "workspace", "status"], "workspace-before").stdout)["available"]

        master, slave = os.openpty()
        tty_name = os.ttyname(slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 55, 180, 0, 0))

        def controlling_terminal():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        tty_process = subprocess.Popen([str(binary)], cwd=directory, env=env, stdin=slave, stdout=slave, stderr=slave,
                                       preexec_fn=controlling_terminal)
        os.close(slave)
        os.set_blocking(master, False)
        terminal_reader = threading.Thread(target=collect_terminal, name="smoke-terminal-reader", daemon=True)
        terminal_reader.start()
        deadline = time.monotonic() + 50
        status = None
        while time.monotonic() < deadline:
            assert tty_process.poll() is None, "Packaged terminal exited before attachment"
            status = json.loads(run([binary, "workspace", "status"], "tty-status", timeout=15).stdout)
            if status.get("available") and len(status.get("state", {}).get("pane_ids", {})) == 5:
                clients = subprocess.run([str(TMUX), "-S", status["socket"], "list-clients", "-F", "#{client_tty}\t#{client_readonly}\t#{client_flags}"],
                                         env=env, cwd=directory, capture_output=True, text=True, timeout=10)
                if clients.returncode == 0 and any(line.startswith(f"{tty_name}\t0\t") and "ignore-size" not in line
                                                  for line in clients.stdout.splitlines()):
                    break
            time.sleep(.25)
        else:
            raise AssertionError("Packaged terminal did not create five panes")
        assert not status["state"].get("run_ids") and not status["state"].get("project_id")
        run([binary, "workspace", "detach"], "detach-real-terminal")
        tty_process.wait(timeout=15)
        assert tty_process.returncode == 0
        (directory / "terminal-screen.ansi").write_bytes(terminal_snapshot())
        (directory / "terminal-screen.ansi").chmod(0o600)
        after_detach = json.loads(run([binary, "workspace", "status"], "workspace-detached").stdout)
        assert after_detach["available"] and len(after_detach["state"]["pane_ids"]) == 5
        assert not browser_log.exists()
        trace_path = Path(env["FIXTURE_071_NATIVE_TRACE"])
        trace = [json.loads(line) for line in trace_path.read_text().splitlines()] if trace_path.exists() else []
        def allowed(args):
            return args in (["--version"], ["features", "list"], ["login", "status"], ["auth", "status", "--json"]) or "--help" in args
        assert all(allowed(row["args"]) for row in trace), "Unexpected native coding invocation"
        checks["packaged_real_tty_180x55_five_panes_detach_0"] = True
        checks["packaged_first_terminal_promoted_to_writer"] = True
        checks["opening_workspace_never_starts_native_coding_or_browser"] = True
        assert not (data / "runtime/copilot-runtime").exists()
        checks["opening_workspace_never_downloads_copilot_runtime"] = True
        signatures = protected_state()
        assert signatures
        before_source = inventory(source)
        before_updates = inventory(state)
        unchanged_files = [*source.rglob("*"), *state.rglob("*"), *protected, configuration]
        before_metadata = file_signatures(unchanged_files)
        reapplied_output = run([sys.executable, UPDATER, "--source-dir", source], "idempotent-reapply", timeout=600)
        assert "pliki i uslugi pozostaly bez zmian" in reapplied_output.stdout
        assert inventory(source) == before_source and inventory(state) == before_updates
        assert file_signatures(unchanged_files) == before_metadata
        assert protected_state() == signatures
        reapplied = json.loads(run([binary, "workspace", "status"], "workspace-after-reapply").stdout)
        assert reapplied["available"] and reapplied["state"]["pane_ids"] == after_detach["state"]["pane_ids"]
        checks["receipt_reapply_preserves_sources_backups_services"] = True
        checks["receipt_reapply_preserves_tracked_file_bytes_modes_mtimes_inodes"] = len(before_metadata)
        summary["status"] = "passed"
    except Exception as error:
        import traceback

        summary.update(status="failed", failure_type=type(error).__name__, failure=str(error))
        (directory / "traceback.txt").write_text(traceback.format_exc())
        if master is not None:
            (directory / "terminal-failure.ansi").write_bytes(terminal_snapshot())
    finally:
        if old_sse is not None and old_sse.poll() is None:
            cleanup_step("old-sse-terminate", old_sse.terminate)
            cleanup_step("old-sse-wait", lambda: old_sse.wait(timeout=10))
        if old_sse_log is not None:
            old_sse_log.close()
        if binary.exists():
            for arguments, label in ((["workspace", "stop", "--clients"], "cleanup-workspace"),
                                     (["cockpit", "stop", "--clients"], "cleanup-broker"),
                                     (["stop"], "cleanup-service")):
                result = cleanup_step(label, lambda arguments=arguments, label=label:
                                      run([binary, *arguments], label, expected=None, timeout=45))
                if result is not None and result.returncode:
                    cleanup_errors.append({"step": label, "returncode": result.returncode})
        if tty_process is not None and tty_process.poll() is None:
            cleanup_step("terminal-terminate", lambda: os.killpg(tty_process.pid, signal.SIGTERM))
            cleanup_step("terminal-wait", lambda: tty_process.wait(timeout=10))
        reader_stop.set()
        if terminal_reader is not None:
            terminal_reader.join(timeout=1)
        if master is not None:
            os.close(master)
        cleanup_step("verify-owned-processes-stopped", verify_owned_processes_stopped)
        cleanup_step("remove-owned-dead-socket", remove_owned_dead_socket)
        if python.exists() and (data / "config.json").exists():
            cleanup = cleanup_step("cleanup-owned-database", lambda:
                run([python, "-I", "-B", "-c", CLEANUP, data, prefix], "cleanup-owned-database", expected=None))
            checks["only_owned_database_removed"] = cleanup is not None and cleanup.returncode == 0
            checks["shared_engine_kept_running"] = cleanup is not None and cleanup.returncode == 0
            if cleanup is None or cleanup.returncode:
                summary["status"] = "failed"
        if file_signatures([engine_identity_file]) != engine_before:
            cleanup_errors.append({"step": "verify-shared-engine-identity", "error": "Engine identity changed"})
        else:
            checks["shared_engine_identity_unchanged"] = True
        if cleanup_errors:
            summary.update(status="failed", cleanup_errors=cleanup_errors)
        summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
        write_json(directory / "summary.json", summary)
    return summary


def main():
    global UPDATER, MANIFEST
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="append", choices=list(BASELINES))
    parser.add_argument("--updater", type=Path, default=UPDATER)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    arguments = parser.parse_args()
    UPDATER, MANIFEST = arguments.updater, arguments.manifest
    manifest = json.loads(MANIFEST.read_text())
    assert manifest["version"] == "0.7.1" and manifest["supported_versions"] == ["0.7.0"]
    for version, archive_path in BASELINES.items():
        assert digest(archive_path.read_bytes()) == manifest["baselines"][version]["sha256"]
    assert digest(UPDATER.read_bytes()) == manifest["updater_sha256"]
    uv_cache = subprocess.check_output(["uv", "cache", "dir"], text=True).strip()
    directory = Path(tempfile.mkdtemp(prefix="devbrain-update071-smoke-"))
    print(json.dumps({"workdir": str(directory), "updater_sha256": manifest["updater_sha256"]}), flush=True)
    reports = []
    for version in arguments.baseline or list(BASELINES):
        report = run_baseline(version, directory / version, manifest, uv_cache)
        reports.append(report)
        print(json.dumps({"baseline": version, "status": report["status"], "report": str(directory / version / "summary.json")}), flush=True)
    summary = {"status": "passed" if all(row["status"] == "passed" for row in reports) else "failed",
        "updater_sha256": manifest["updater_sha256"], "baselines": reports,
        "harness_sha256": digest(Path(__file__).read_bytes()),
        "actual_paid_native_or_model_inference": False, "physical_user_wsl_test": False}
    write_json(directory / "summary.json", summary)
    print(json.dumps({"status": summary["status"], "report": str(directory / "summary.json")}), flush=True)
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
