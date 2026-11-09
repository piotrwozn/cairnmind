"""Pure parent-loss oracle predicates; no processes, signals or services."""
import importlib.util
import errno
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest


@pytest.fixture
def oracle():
    spec = importlib.util.spec_from_file_location("parent_loss_oracle_guards", Path(__file__).with_name("watch-parent-loss080.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def readiness_os(monkeypatch, oracle, *, content=b'{"ready": true}', open_error=None, mode=stat.S_IFREG, size=None):
    state = {"closed": [], "flags": None, "reads": 0}
    def open_file(path, flags):
        state["flags"] = flags
        if open_error:
            raise open_error
        return 19
    def read_file(descriptor, length):
        assert descriptor == 19
        state["reads"] += 1
        return content if state["reads"] == 1 else b""
    monkeypatch.setattr(oracle, "os", SimpleNamespace(
        O_RDONLY=0, O_NOFOLLOW=32, O_CLOEXEC=64, O_NONBLOCK=128, open=open_file,
        fstat=lambda descriptor: SimpleNamespace(st_mode=mode, st_size=len(content) if size is None else size),
        read=read_file, close=lambda descriptor: state["closed"].append(descriptor)))
    return state


def test_readiness_replacement_after_open_reads_retained_publication(oracle, monkeypatch):
    class ReplacedPath:
        metadata_reads = 0
        def exists(self):
            return True
        def is_file(self):
            self.metadata_reads += 1
            return False  # directory entry replaced after the successful exists()
    path = ReplacedPath()
    assert path.exists() and not path.is_file()  # reproduces the old read_json rejection
    state = readiness_os(monkeypatch, oracle)
    assert oracle.read_ready_json(path) == {"ready": True}
    assert path.metadata_reads == 1  # new reader never repeats pathname metadata
    assert state["flags"] == 224 and state["closed"] == [19]


def test_readiness_enoent_is_only_wait_state(oracle, monkeypatch):
    state = readiness_os(monkeypatch, oracle, open_error=FileNotFoundError(errno.ENOENT, "publication gap"))
    assert oracle.read_ready_json("report") is None
    assert state["closed"] == []


@pytest.mark.parametrize("failure", ["permission", "symlink", "other_errno", "malformed", "nonregular", "oversized", "not_object"])
def test_readiness_other_failures_remain_closed(oracle, monkeypatch, failure):
    kwargs = {
        "permission": {"open_error": PermissionError(errno.EACCES, "denied")},
        "symlink": {"open_error": OSError(errno.ELOOP, "no-follow")},
        "other_errno": {"open_error": FileNotFoundError(errno.EIO, "not ENOENT")},
        "malformed": {"content": b"{"},
        "nonregular": {"mode": stat.S_IFIFO},
        "oversized": {"size": 8 * 1024 * 1024 + 1},
        "not_object": {"content": b"[]"},
    }[failure]
    state = readiness_os(monkeypatch, oracle, **kwargs)
    with pytest.raises((RuntimeError, ValueError, OSError)):
        oracle.read_ready_json("report")
    assert state["closed"] == ([] if "open_error" in kwargs else [19])


@pytest.fixture
def cgroup(tmp_path):
    path = tmp_path / "scope"
    path.mkdir()
    (path / "cgroup.events").write_text("populated 0\n")
    stat = path.stat()
    return path, (stat.st_dev, stat.st_ino), "/user.slice/test.scope"


def test_readable_matching_scope_is_empty(oracle, cgroup):
    path, identity, group = cgroup
    assert oracle.scope_empty(path, identity, group, {"LoadState": "loaded", "ControlGroup": group}, 0)
    (path / "cgroup.events").write_text("populated 1\n")
    assert not oracle.scope_empty(path, identity, group, {"LoadState": "loaded", "ControlGroup": group}, 0)


@pytest.mark.parametrize("problem", ["inode", "missing_events", "missing_population", "foreign_group", "missing_group", "probe_failure"])
def test_unknown_scope_evidence_is_not_empty(oracle, cgroup, problem):
    path, identity, group = cgroup
    properties, returncode = {"LoadState": "loaded", "ControlGroup": group}, 0
    if problem == "inode":
        identity = (0, 0)
    elif problem == "missing_events":
        (path / "cgroup.events").unlink()
    elif problem == "missing_population":
        (path / "cgroup.events").write_text("frozen 0\n")
    elif problem == "foreign_group":
        properties["ControlGroup"] = "/user.slice/foreign.scope"
    elif problem == "missing_group":
        properties["ControlGroup"] = ""
    else:
        returncode = 1
    with pytest.raises((RuntimeError, FileNotFoundError)):
        oracle.scope_empty(path, identity, group, properties, returncode)


def test_gone_cgroup_requires_confirmed_gone_unit(oracle, tmp_path):
    path = tmp_path / "missing"
    assert oracle.scope_empty(path, (1, 2), "/scope", {"LoadState": "not-found", "ControlGroup": ""}, 1)
    with pytest.raises(RuntimeError):
        oracle.scope_empty(path, (1, 2), "/scope", {"LoadState": "loaded", "ControlGroup": "/scope"}, 0)


def negative_result():
    return {"run_id": "fixture", "passed": False, "forced_cleanup": True, "cleanup_complete": True,
            "controller_exit_verified": True, "child_returncode": -15, "remaining_scope_processes": [],
            "remaining_tagged_processes": [], "native_failure": False, "errors": ["Windows heartbeat is stale"]}


def test_expected_negative_result_is_counterfactual_success_only(oracle):
    assert oracle.verify_negative_result(negative_result(), "fixture") is None


@pytest.mark.parametrize("problem", ["null_returncode", "bool_returncode", "zero_returncode", "missing_remaining", "remaining_child", "missing_heartbeat", "native_failure", "positive_verdict"])
def test_incomplete_or_unrelated_failure_is_rejected(oracle, problem):
    result = negative_result()
    if problem == "null_returncode":
        result["child_returncode"] = None
    elif problem == "bool_returncode":
        result["child_returncode"] = True
    elif problem == "zero_returncode":
        result["child_returncode"] = 0
    elif problem == "missing_remaining":
        result.pop("remaining_scope_processes")
    elif problem == "remaining_child":
        result["remaining_scope_processes"] = [{"pid": 123}]
    elif problem == "missing_heartbeat":
        result["errors"] = ["Unrelated startup error"]
    elif problem == "native_failure":
        result["native_failure"] = True
    else:
        result["passed"] = True
    with pytest.raises(RuntimeError):
        oracle.verify_negative_result(result, "fixture")
