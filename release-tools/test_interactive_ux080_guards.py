"""Pure operator-evidence checks; no clients, databases, windows or subprocesses."""
import ast
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def controller():
    spec = importlib.util.spec_from_file_location("interactive_ux_guard", Path(__file__).with_name("run-interactive-ux080.py"))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


@pytest.mark.parametrize("entrypoint", ["attach", "workload"])
@pytest.mark.parametrize("cleaner_fails", [False, True])
def test_environment_boundary_reads_locale_before_replacing_process_environment(controller, entrypoint, cleaner_fails):
    # Execute the actual three-statement environment boundary from each path,
    # without entering account, manifest, PTY, server or engine lifecycles.
    tree = ast.parse(Path(controller.__file__).read_text())
    function = next(node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == entrypoint)
    boundaries = []
    for node in ast.walk(function):
        for _, body in ast.iter_fields(node):
            if not isinstance(body, list):
                continue
            for index, statement in enumerate(body):
                if isinstance(statement, ast.Expr) and ast.unparse(statement) == "os.environ.clear()":
                    assert index > 0 and index + 1 < len(body)
                    boundaries.append(body[index - 1:index + 2])
    assert len(boundaries) == 1
    boundary = boundaries[0]
    assert isinstance(boundary[0], ast.Assign)
    assert ast.unparse(boundary[0].value.func) == "common.clean_environment"
    incoming = {"LANG": "pl_PL.UTF-8", "LC_ALL": "C.UTF-8", "LC_CTYPE": "C.UTF-8",
                "TZ": "Europe/Warsaw", "PRIVATE_API_KEY": "synthetic-secret"}
    process_environment = dict(incoming)
    calls = []

    def clean(work, runtime, run_id, native_paths):
        calls.append((work, runtime, run_id, native_paths))
        assert process_environment == incoming
        if cleaner_fails:
            raise ValueError("synthetic_environment_failure")
        return {key: incoming[key] for key in ("LANG", "LC_ALL", "LC_CTYPE", "TZ")}

    values = {"os": SimpleNamespace(environ=process_environment),
              "common": SimpleNamespace(clean_environment=clean), "work": Path("/synthetic/private"),
              "runtime": {"qualified": True}, "state": {"run_id": "synthetic", "native_binaries": {}},
              "args": SimpleNamespace(run_id="synthetic"), "binaries": {}}
    code = compile(ast.Module(body=boundary, type_ignores=[]), controller.__file__, "exec")
    if cleaner_fails:
        with pytest.raises(ValueError, match="synthetic_environment_failure"):
            exec(code, values)
        assert process_environment == incoming
    else:
        exec(code, values)
        assert process_environment == {key: incoming[key] for key in ("LANG", "LC_ALL", "LC_CTYPE", "TZ")}
    assert calls == [(values["work"], values["runtime"], "synthetic", {})]


def observations(controller, operator="human"):
    reported = {"operator": operator, "human_reported": operator == "human", "agent_reported": operator == "agent"}
    values = {"task:" + name: {"status": "passed", **reported, "size": "120x36", "actual_size": [120, 36]}
              for name in controller.TASKS}
    values.update({"geometry:" + size: {"status": "passed", **reported, "size": size, "actual_size": list(actual)}
                   for size, actual in controller.SIZES.items()})
    return values


def test_five_human_tasks_once_and_three_visual_sizes(controller):
    assert controller.human_acceptance(observations(controller))
    assert not controller.agent_acceptance(observations(controller))


def test_agent_acceptance_never_becomes_human_acceptance(controller):
    values = observations(controller, "agent")
    assert controller.agent_acceptance(values)
    assert not controller.human_acceptance(values)
    assert all(value["human_reported"] is False for value in values.values())


@pytest.mark.parametrize("change", ["human_claim", "mixed_operator", "missing_flag", "unreported", "wrong_size"])
def test_agent_acceptance_requires_consistent_explicit_observations(controller, change):
    values = observations(controller, "agent")
    row = values["task:start"]
    if change == "human_claim":
        row["human_reported"] = True
    elif change == "mixed_operator":
        row["operator"] = "human"
    elif change == "missing_flag":
        row.pop("agent_reported")
    elif change == "unreported":
        row["agent_reported"] = False
    else:
        row["size"], row["actual_size"] = "80x24", [80, 24]
    assert not controller.agent_acceptance(values)
    assert not controller.human_acceptance(values)


@pytest.mark.parametrize("problem", ["missing_task", "missing_size", "auto_claim", "wrong_actual_size", "failed", "blocked"])
def test_partial_or_unobserved_human_acceptance_is_not_pass(controller, problem):
    values = copy.deepcopy(observations(controller))
    if problem == "missing_task":
        values.pop("task:reconnect")
    elif problem == "missing_size":
        values.pop("geometry:80x24")
    elif problem == "auto_claim":
        values["task:start"]["human_reported"] = False
    elif problem == "wrong_actual_size":
        values["geometry:80x24"]["actual_size"] = [180, 55]
    else:
        values["task:source"]["status"] = problem
    assert not controller.human_acceptance(values)


@pytest.mark.parametrize("failure", ["none", "sync", "release", "cleanup"])
def test_final_auth_sync_always_releases_and_never_publishes_private_rows(controller, failure):
    rows = [{"private_seed": "DO_NOT_PUBLISH", "run_lock": 42}]
    calls = []
    def sync(received):
        assert received is rows
        calls.append("sync")
        if failure == "sync":
            raise RuntimeError("DO_NOT_PUBLISH")
        return {"passed": True, "error_codes": []}
    def release(received, *, close_journal):
        assert received is rows
        calls.append(("release", close_journal))
        if failure == "release":
            raise RuntimeError("DO_NOT_PUBLISH")
        return {"passed": close_journal, "error_codes": [] if close_journal else ["oauth_recovery_required"]}
    report = {"errors": [], "cleanup_complete": failure != "cleanup"}
    controller.finish_native_auth(SimpleNamespace(sync_native_auth=sync, release_native_auth=release), rows, report)
    assert calls == ["sync", ("release", failure not in {"sync", "cleanup"})]
    assert bool(report["errors"]) is (failure != "none")
    assert "DO_NOT_PUBLISH" not in str(report)
    assert "run_lock" not in str(report)


def test_baseline_attach_refuses_any_original_secure_store(controller):
    def require(value, message):
        if not value:
            raise ValueError(message)
    common = SimpleNamespace(require=require)
    assert controller.attach_auth_environment({"baseline_only": True}, common) == {}
    with pytest.raises(ValueError, match="baseline_auth_storage_forbidden"):
        controller.attach_auth_environment({"baseline_only": True, "claude_secure_storage": {}}, common)
