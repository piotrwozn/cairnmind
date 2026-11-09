"""Synthetic ownership regressions: no subprocesses, JVM, tmux or native signals."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def scope(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("scope080_guards", Path(__file__).with_name("supervise-080.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    owner = module.ScopeOwnership.__new__(module.ScopeOwnership)
    owner.unit = "devbrain-q-" + "a" * 32 + ".scope"
    owner.group = "/user.slice/user-1000.slice/" + owner.unit
    owner.path = tmp_path / "cgroup"
    owner.path.mkdir()
    stat = owner.path.stat()
    owner.inode = f"{stat.st_dev}:{stat.st_ino}"
    child = {"pid": 424242, "birth": "1234", "state": "S", "uid": os.getuid(),
             "executable": "/qualified/python", "parent_pid": 1}
    rows = {child["pid"]: child}
    groups = {os.getpid(): owner.group, child["pid"]: owner.group}
    (owner.path / "cgroup.procs").write_text(f"{os.getpid()}\n{child['pid']}\n")
    monkeypatch.setattr(module, "identity", lambda pid: rows.get(pid))
    monkeypatch.setattr(module, "process_cgroup", groups.get)
    monkeypatch.setattr(module, "tagged_processes", lambda *_: pytest.fail("Environment markers must not establish scope ownership"))
    return module, owner, rows, groups, child


def test_orphan_without_environment_marker_is_found_by_cgroup(scope):
    _, owner, _, _, child = scope
    # There is deliberately no environ file/marker and PPID has become init.
    assert owner.members() == [child]


def test_environment_cleared_orphan_cleanup_uses_checked_pidfd(scope, monkeypatch):
    module, owner, rows, _, child = scope
    opened, sent, closed = [], [], []
    monkeypatch.setattr(module.os, "pidfd_open", lambda pid: opened.append(pid) or 42)
    monkeypatch.setattr(module.os, "close", closed.append)
    def send(handle, number):
        sent.append((handle, number))
        rows.pop(child["pid"])
    monkeypatch.setattr(module.signal, "pidfd_send_signal", send)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    assert owner.stop() == (True, [])
    assert opened == [child["pid"]] and sent == [(42, module.signal.SIGTERM)] and closed == [42]


def test_reused_pid_after_open_is_not_signalled(scope, monkeypatch):
    module, owner, rows, groups, child = scope
    def opened(pid):
        rows[pid] = {**child, "birth": "reused"}
        groups[pid] = "/user.slice/foreign.scope"
        return 42
    sent, closed = [], []
    monkeypatch.setattr(module.os, "pidfd_open", opened)
    monkeypatch.setattr(module.os, "close", closed.append)
    monkeypatch.setattr(module.signal, "pidfd_send_signal", lambda *args: sent.append(args))
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    assert owner.stop() == (True, [])
    assert not sent and closed == [42]


def test_process_from_other_cgroup_is_never_owned(scope):
    _, owner, _, groups, child = scope
    groups[child["pid"]] = "/user.slice/another.scope"
    assert owner.members() == [] and not owner.owns(child)


def test_foreign_uid_in_scope_is_refused(scope):
    _, owner, rows, _, child = scope
    rows[child["pid"]] = {**child, "uid": os.getuid() + 1}
    with pytest.raises(RuntimeError, match="foreign UID"):
        owner.members()


def test_recreated_cgroup_identity_cannot_authorize_cleanup(scope, monkeypatch):
    module, owner, _, _, _ = scope
    owner.inode = "0:0"
    monkeypatch.setattr(module.os, "pidfd_open", lambda _: pytest.fail("Replaced cgroup must not open process handles"))
    with pytest.raises(RuntimeError, match="identity changed"):
        owner.stop()


def test_delegated_subgroup_descendant_remains_owned(scope):
    _, owner, _, groups, child = scope
    nested = owner.path / "child"
    nested.mkdir()
    (owner.path / "cgroup.procs").write_text(str(os.getpid()))
    (nested / "cgroup.procs").write_text(str(child["pid"]))
    groups[child["pid"]] = owner.group + "/child"
    assert owner.members() == [child]


def test_missing_cgroup_evidence_is_not_empty_success(scope):
    _, owner, _, _, _ = scope
    (owner.path / "cgroup.procs").unlink()
    with pytest.raises(FileNotFoundError):
        owner.members()


@pytest.fixture
def discovery(monkeypatch):
    spec = importlib.util.spec_from_file_location("discovery080_guards", Path(__file__).with_name("supervise-080.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    pid = os.getpid() + 100000
    own = "/user.slice/user-1000.slice/devbrain-q-" + "a" * 32 + ".scope"
    state = {"uid": os.getuid(), "own": own, "group": "/user.slice/unrelated.scope", "denied": False,
             "marker": b"DEVBRAIN_QUALIFICATION_RUN_ID=fixture", "reads": 0, "identities": 0}
    row = {"pid": pid, "birth": "123", "uid": os.getuid(), "state": "S", "executable": "/qualified/python"}
    class Entry:
        name = str(pid)
        def stat(self):
            return SimpleNamespace(st_uid=state["uid"])
        def __truediv__(self, name):
            assert name == "environ"
            return self
        def read_bytes(self):
            state["reads"] += 1
            if state["denied"]:
                raise PermissionError("synthetic unreadable environment")
            return state["marker"]
    entry = Entry()
    monkeypatch.setattr(module, "Path", lambda value: SimpleNamespace(iterdir=lambda: iter([entry])) if value == "/proc" else pytest.fail("Unexpected filesystem access"))
    monkeypatch.setattr(module, "process_cgroup", lambda value: state["own"] if value == os.getpid() else state["group"])
    def identify(value):
        assert value == pid
        state["identities"] += 1
        return row
    monkeypatch.setattr(module, "identity", identify)
    monkeypatch.setattr(module, "same_process", lambda value: value == row)
    return module, state, row


def test_global_scan_skips_foreign_uid_before_exe_or_environment(discovery):
    module, state, _ = discovery
    state.update(uid=os.getuid() + 1, denied=True)
    assert module.tagged_processes("fixture") == []
    assert state["reads"] == state["identities"] == 0


def test_global_scan_reports_inaccessible_same_uid_outside_owned_scope(discovery):
    module, state, row = discovery
    state["denied"] = True
    skipped = []
    assert module.tagged_processes("fixture", skipped=skipped) == []
    assert skipped == [{"pid": row["pid"], "control_group": state["group"], "reason": "inaccessible_same_uid_outside_owned_scope"}]
    assert state["identities"] == 0


@pytest.mark.parametrize("boundary", ["owned", "unknown", "unbounded_caller"])
def test_unreadable_owned_or_unknown_boundary_fails_closed(discovery, monkeypatch, boundary):
    module, state, _ = discovery
    state["denied"] = True
    if boundary == "owned":
        state["group"] = state["own"]
    elif boundary == "unbounded_caller":
        state["own"] = "/user.slice/unrelated.scope"
    else:
        original = module.process_cgroup
        def unreadable(pid):
            if pid != os.getpid():
                raise PermissionError("synthetic unreadable cgroup")
            return original(pid)
        monkeypatch.setattr(module, "process_cgroup", unreadable)
    with pytest.raises(PermissionError):
        module.tagged_processes("fixture")


def test_matching_marker_outside_scope_does_not_hide_unreadable_identity(discovery, monkeypatch):
    module, _, _ = discovery
    def denied(_):
        raise PermissionError("synthetic unreadable executable")
    monkeypatch.setattr(module, "identity", denied)
    with pytest.raises(PermissionError):
        module.tagged_processes("fixture")


def test_global_discovery_rechecks_marker_after_identity(discovery, monkeypatch):
    module, state, row = discovery
    def changed(_):
        state["marker"] = b""
        return row
    monkeypatch.setattr(module, "identity", changed)
    assert module.tagged_processes("fixture") == []
    assert state["reads"] == 2


def test_observable_matching_marker_retains_identity(discovery):
    module, state, row = discovery
    assert module.tagged_processes("fixture") == [row]
    assert state["reads"] == 2 and state["identities"] == 1


def test_owned_scope_unreadable_member_is_never_empty_success(scope, monkeypatch):
    module, owner, _, _, _ = scope
    def denied(_):
        raise PermissionError("synthetic unreadable owned process")
    monkeypatch.setattr(module, "identity", denied)
    with pytest.raises(PermissionError):
        owner.members()
