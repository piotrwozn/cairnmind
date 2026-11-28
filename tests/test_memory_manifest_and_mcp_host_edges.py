"""Edge-case tests for memory.py manifest/snapshot checks and mcp_host.py."""
from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cairnmind import mcp_host, memory


def _write_manifest(base, manifest, *, recompute_id=True):
    manifest = dict(manifest)
    if recompute_id:
        manifest.pop("snapshot_id", None)
        manifest["snapshot_id"] = memory._manifest_id(manifest)
    path = base / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _minimal_manifest(base):
    pid = str(uuid4())
    empty_payload = {t: [] for t in memory.TABLES}
    return {
        "format": memory.FORMAT,
        "format_version": memory.VERSION,
        "project": {"id": pid, "name": "Demo"},
        "origin_root": "/abs/root",
        "root_aliases": ["/abs/root"],
        "ancestors": [],
        "parent_id": None,
        "active_plan_id": None,
        "objects": {t: [] for t in memory.TABLES},
        "counts": {t: 0 for t in memory.TABLES},
        "state_hash": memory._state_hash(empty_payload),
        "created_at": "2026-01-01T00:00:00Z",
    }


def test_memory_manifest_snapshot_named_objects_and_summary_edges(tmp_path):
    base = tmp_path / "snap"
    base.mkdir()
    (base / "objects").mkdir()

    # Valid manifest works with manifest_summary, inspect_snapshot, and _named_objects
    m = _minimal_manifest(base)
    mpath = _write_manifest(base, m)
    assert memory.manifest_summary(mpath)["status"] == "manifest_valid"
    assert memory.inspect_snapshot(mpath)["status"] == "valid"
    assert memory._named_objects(mpath) == set()

    # _check_manifest error branches
    for mutate, expected in [
        (lambda x: x.__setitem__("project", "bad"), "Missing memory project identity"),
        (lambda x: x["project"].__setitem__("id", str(uuid4()).upper()), "Invalid memory project identity"),
        (lambda x: x.__setitem__("origin_root", "relative/path"), "must be absolute"),
        (lambda x: x.__setitem__("root_aliases", ["relative"]), "root bindings"),
        (lambda x: x.__setitem__("parent_id", "a" * 64), "parent snapshot"),
        (lambda x: x.__setitem__("ancestors", ["a" * 64, "a" * 64]), "cycle"),
        (lambda x: x.__setitem__("objects", {}), "object collections"),
    ]:
        bad = json.loads(json.dumps(m))
        mutate(bad)
        p = _write_manifest(base, bad)
        with pytest.raises(ValueError, match=expected):
            memory._check_manifest(p)

    # _load_snapshot error branches: non-list descriptors, non-dict descriptor, bad descriptor shape,
    # identity mismatch, state_hash mismatch, counts mismatch
    bad_list = json.loads(json.dumps(m))
    bad_list["objects"]["records"] = "bad"
    with pytest.raises(ValueError, match="object list"):
        memory._load_snapshot(_write_manifest(base, bad_list))

    bad_item = json.loads(json.dumps(m))
    bad_item["objects"]["records"] = ["not-dict"]
    with pytest.raises(ValueError, match="budget"):
        memory._load_snapshot(_write_manifest(base, bad_item))

    bad_desc = json.loads(json.dumps(m))
    bad_desc["objects"]["records"] = [{"id": "r1", "sha256": "short", "path": "objects/x.json", "bytes": 1}]
    with pytest.raises(ValueError, match="content-addressed object"):
        memory._load_snapshot(_write_manifest(base, bad_desc))

    obj_bytes = json.dumps({"id": "wrong-id"}).encode()
    obj_sha = hashlib.sha256(obj_bytes).hexdigest()
    (base / "objects" / f"{obj_sha}.json").write_bytes(obj_bytes)
    bad_id = json.loads(json.dumps(m))
    bad_id["objects"]["records"] = [{"id": "r1", "sha256": obj_sha, "path": f"objects/{obj_sha}.json", "bytes": len(obj_bytes)}]
    with pytest.raises(ValueError, match="identity mismatch"):
        memory._load_snapshot(_write_manifest(base, bad_id))

    bad_hash = json.loads(json.dumps(m))
    bad_hash["state_hash"] = "0" * 64
    with pytest.raises(ValueError, match="portable-state checksum"):
        memory._load_snapshot(_write_manifest(base, bad_hash))

    bad_counts = json.loads(json.dumps(m))
    bad_counts["counts"]["records"] = 99
    with pytest.raises(ValueError, match="counts do not match"):
        memory._load_snapshot(_write_manifest(base, bad_counts))

    # _named_objects mismatch & KeyError wrapper; _load_snapshot TypeError wrapper
    mpath.write_text(json.dumps({**m, "snapshot_id": "0" * 64}), encoding="utf-8")
    with pytest.raises(ValueError, match="checksum mismatch"):
        memory._named_objects(mpath)
    no_objs = {k: v for k, v in m.items() if k != "objects"}
    no_objs["snapshot_id"] = memory._manifest_id(no_objs)
    mpath.write_text(json.dumps(no_objs), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid or unreadable"):
        memory._named_objects(mpath)
    with pytest.raises(ValueError, match="Invalid or unreadable"):
        memory._load_snapshot(123)
    with pytest.raises(ValueError, match="Invalid or unreadable"):
        memory.manifest_summary(123)


@pytest.mark.asyncio
async def test_mcp_host_and_server_protocol_and_lifecycle_edges(tmp_path, monkeypatch):
    srv = mcp_host.McpServer({"id": "demo", "command": "echo", "args": [], "env": {}})

    # _write without process raises mcp_server_unavailable
    with pytest.raises(mcp_host.McpFailure) as exc:
        await srv._write({"jsonrpc": "2.0"})
    assert exc.value.code == "mcp_server_unavailable"

    # _write with failing drain raises mcp_server_unavailable
    class _BrokenStdin:
        def is_closing(self):
            return False

        def write(self, data):
            pass

        async def drain(self):
            raise BrokenPipeError()

    srv.process = SimpleNamespace(stdin=_BrokenStdin(), stdout=None, pid=1, returncode=0)
    with pytest.raises(mcp_host.McpFailure) as exc:
        await srv._write({"jsonrpc": "2.0"})
    assert exc.value.code == "mcp_server_unavailable"

    # _answer handles ping
    sent = []
    monkeypatch.setattr(srv, "_write", lambda msg: asyncio.sleep(0, result=sent.append(msg)))
    await srv._answer({"id": 7, "method": "ping"})
    assert sent[-1] == {"jsonrpc": "2.0", "id": 7, "result": {}}

    # _read handles exception & sets state to "off"
    class _BoomStdout:
        async def readline(self):
            raise RuntimeError("pipe error")

    srv.state = "running"
    srv.process = SimpleNamespace(stdout=_BoomStdout(), stdin=None, pid=1, returncode=0)
    fut = asyncio.get_running_loop().create_future()
    srv._pending[1] = fut
    await srv._read()
    assert srv.state == "off" and isinstance(fut.exception(), mcp_host.McpFailure)

    # request when state is "off" raises mcp_server_unavailable
    with pytest.raises(mcp_host.McpFailure) as exc:
        await srv.request("tools/list", {}, 1)
    assert exc.value.code == "mcp_server_unavailable"

    # _signal on Windows with _job and without _job (ProcessLookupError swallowed)
    monkeypatch.setattr(mcp_host, "IS_WINDOWS", True)
    term_calls = []
    srv._job = SimpleNamespace(terminate=lambda: term_calls.append("job"), close=lambda: term_calls.append("close"))
    srv._signal(SimpleNamespace(pid=1), 15)
    assert "job" in term_calls
    srv._job = None
    monkeypatch.setattr(mcp_host, "recorded", lambda pid, birth: True)
    srv._signal(SimpleNamespace(pid=1, terminate=lambda: (_ for _ in ()).throw(ProcessLookupError())), 15)

    # _halt closes _job and cancels active _reader
    srv.process = None
    srv._job = SimpleNamespace(close=lambda: term_calls.append("closed"))
    srv._reader = asyncio.create_task(asyncio.sleep(10))
    await srv._halt()
    assert "closed" in term_calls and srv._job is None and srv._reader is None

    # McpHost: _admitted exception -> catalogue_invalid; start() already started -> []; withdraw()
    host = mcp_host.McpHost([], worktree=str(tmp_path))
    stopped = []
    host.servers["demo"] = SimpleNamespace(id="demo", stop=lambda: asyncio.sleep(0, result=stopped.append(True)), state="off")
    monkeypatch.setattr(host, "_admit", lambda sid, rows: (_ for _ in ()).throw(RuntimeError("bad")))
    ev_type, ev_data = await host._admitted({"id": "demo", "command": "echo"}, [])
    assert ev_type == "mcp_server_failed" and ev_data["code"] == "catalogue_invalid" and stopped == [True]

    host.catalogue["mcp_demo_t"] = {"server_id": "demo", "tool": "t", "schema": {}}
    await host.withdraw("demo", "failed")
    assert host.catalogue == {} and len(stopped) == 2

    host.started = True
    assert await host.start() == []
    host.started = False
    assert await host.start() == []
