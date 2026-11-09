"""Copy-only replay must preserve privacy and refuse unsafe claims/paths."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
import types

import pytest


@pytest.fixture
def replay():
    spec = importlib.util.spec_from_file_location("queue_replay_guard", Path(__file__).with_name("replay-failed-queue-080.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("path", [
    "/home/piotr/.local/share/devbrain", "/home/piotr/.cache/devbrain-qualification-080/run/other",
    "/mnt/c/Users/piotr/AppData/Local/DevBrain/queue-copy", "/tmp/queue-copy",
])
def test_copy_replay_rejects_production_and_foreign_roots(replay, path):
    with pytest.raises(ValueError):
        replay.private_root(Path(path), "run")


def test_copy_replay_rejects_reused_root(replay, monkeypatch):
    path = Path("/home/piotr/.cache/devbrain-qualification-080/run/queue-copy")
    monkeypatch.setattr(Path, "exists", lambda self: self == path)
    with pytest.raises(ValueError):
        replay.private_root(path, "run")


def test_copy_replay_rejects_redirected_parent(replay, monkeypatch):
    path = Path("/home/piotr/.cache/devbrain-qualification-080/run/queue-copy")
    monkeypatch.setattr(Path, "exists", lambda self: False)
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == path.parent)
    with pytest.raises(ValueError):
        replay.private_root(path, "run")


@pytest.mark.parametrize("change", [
    {"state": "running"}, {"attempts": 6}, {"worker": "another-worker"}, {"lease_until": 42},
])
def test_selected_claim_does_not_overwrite_stale_or_owned_attempt(replay, monkeypatch, change):
    __import__("devbrain.agent")  # Load the real budget policy before the Store-only shim.
    row = {"id": "selected", "state": "pending", "attempts": 5, "worker": None, "lease_until": None, **change}
    writes = []
    shim = types.ModuleType("devbrain.store")
    shim._one = lambda tx, sql, params, strict: dict(row)
    shim._set = lambda *args: writes.append(args)
    shim._now = lambda: "synthetic-time"
    monkeypatch.setitem(sys.modules, "devbrain.store", shim)

    class FakeStore:
        @contextmanager
        def transaction(self):
            yield object()

    with pytest.raises(ValueError):
        replay.claim_selected(FakeStore(), "selected", 5, "qualification-run")
    assert not writes


@pytest.mark.parametrize("work_budget,expected_lease", [(120.0, 210.0), (180.0, 270.0), (5.0, 120.0)])
def test_selected_claim_uses_current_shared_inference_lease(replay, monkeypatch, work_budget, expected_lease):
    from devbrain import agent

    monkeypatch.setattr(agent, "EXTRACTION_TIMEOUT", work_budget)
    monkeypatch.setattr(replay.time, "time", lambda: 1000.0)
    row = {"id": "selected", "state": "pending", "attempts": 5, "worker": None, "lease_until": None}
    writes = []
    shim = types.ModuleType("devbrain.store")
    shim._one = lambda *args: dict(row)
    shim._set = lambda tx, table, value, where, params: writes.append(deepcopy(value))
    shim._now = lambda: "synthetic-time"
    monkeypatch.setitem(sys.modules, "devbrain.store", shim)

    class FakeStore:
        @contextmanager
        def transaction(self):
            yield object()

    claimed = replay.claim_selected(FakeStore(), "selected", 5, "qualification-run")
    assert claimed["lease_until"] == 1000.0 + expected_lease
    assert claimed["attempts"] == 6 and claimed["worker"] == "qualification-run"
    assert writes == [claimed]


@pytest.mark.parametrize("controller", ["copy", "production"])
@pytest.mark.parametrize("deadline", ["inner", "outer"])
def test_real_extraction_deadline_settles_before_finish_without_following_job_or_late_writes(
        replay, monkeypatch, controller, deadline):
    """Use real extraction and controller code; only model/Store IO are synthetic."""
    from devbrain import agent

    original_agent = agent.KnowledgeAgent
    if controller == "production":
        spec = importlib.util.spec_from_file_location(
            "production_deadline_guard", Path(__file__).with_name("retry-production-queue-080.py"))
        implementation = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(implementation)
    else:
        implementation = replay
    monkeypatch.setattr(agent, "EXTRACTION_TIMEOUT", 0.02 if deadline == "inner" else 10.0)
    original_outer = agent.extraction_outer_timeout
    outer_calls, transactions, writes, model_returns, finishes, claims, publications = [], [], [], [], [], [], []

    def outer_timeout():
        # The watchdog case deliberately makes the caller expire first. The
        # normal case uses the real shared policy, including its five-second gap.
        value = original_outer() if deadline == "inner" else 0.02
        outer_calls.append(value)
        return value

    monkeypatch.setattr(agent, "extraction_outer_timeout", outer_timeout)
    jobs = [{"id": name, "job_key": name, "project_id": "project", "kind": "extract", "payload": {"source_id": "source"},
             "state": "failed", "attempts": 2, "retry_count": 0} for name in ("selected", "following")]
    rows = {job["id"]: deepcopy(job) for job in jobs}
    diagnostic_history = []

    class Model:
        active = False
        calls = 0
        closed = False

        async def chat(self, *args, **kwargs):
            self.calls += 1
            self.active = True
            try:
                await asyncio.sleep(0.1)
                model_returns.append("late model response")
                return {"message": {"content": '{"records":[]}'}}
            finally:
                self.active = False

        async def aclose(self):
            assert not self.active
            self.closed = True

    model = Model()

    class FakeStore:
        def get_source(self, source_id):
            assert source_id == "source"
            return {"id": "source", "project_id": "project", "kind": "chat", "metadata": {},
                    "content": "Synthetic fixture fact.", "sha256": "synthetic-sha", "is_current": True}

        def set_job_diagnostics(self, job_id, value, **kwargs):
            assert not finishes, "No diagnostics may arrive after the job was finished"
            assert kwargs == {"expected_attempts": 3, "worker": "qualification-budget"}
            rows[job_id]["diagnostics"] = deepcopy(value)
            diagnostic_history.append(deepcopy(value))
            return True

        @contextmanager
        def transaction(self):
            transactions.append("unexpected transaction")
            yield self

        def retry_job(self, project_id, job_id, attempts):
            assert job_id == "selected" and project_id == "project" and attempts == 2
            rows[job_id].update(state="pending", retry_count=1)

        def finish_job(self, job_id, error, **kwargs):
            assert not model.active, "The owned inference must settle before finish_job"
            assert kwargs == {"worker": "qualification-budget", "expected_attempts": 3}
            finishes.append((job_id, error))
            rows[job_id].update(state="failed" if error else "completed", error_code="timeout")

        def job_status(self, project_id, job_id):
            return rows[job_id]

    store, settings = FakeStore(), types.SimpleNamespace(answer_tokens=100)

    def build_agent(actual_store, actual_settings):
        assert actual_store is store and actual_settings is settings
        instance = original_agent(store, settings, ollama=model, retriever=object())
        monkeypatch.setattr(instance, "_commit_extraction", lambda *args: writes.append(args))
        return instance

    monkeypatch.setattr(agent, "KnowledgeAgent", build_agent)
    ingest = types.ModuleType("devbrain.ingest")
    ingest.import_history = lambda *args: pytest.fail("No following import or model job may run")
    monkeypatch.setitem(sys.modules, "devbrain.ingest", ingest)

    def claim(actual_store, job_id, attempts, worker):
        assert actual_store is store and worker == "qualification-budget"
        claims.append(job_id)
        rows[job_id].update(state="running", worker=worker, attempts=attempts + 1)
        return rows[job_id]

    monkeypatch.setattr(replay, "claim_selected", claim)
    report = {"run_id": "budget", "jobs": []}
    before_following = deepcopy(rows["following"])

    async def check():
        started = asyncio.get_running_loop().time()
        if controller == "copy":
            with pytest.raises(ValueError, match="Historical failure remains unresolved"):
                await replay.replay(store, settings, jobs, report, lambda: publications.append(deepcopy(report)))
        else:
            claim(store, "selected", 2, "qualification-budget")
            await implementation.execute_selected(store, settings, rows["selected"])
            # The production controller's unchanged readback refuses continuation.
            with pytest.raises(implementation.Refusal, match="selected_job_failed"):
                implementation.verify_attempt_result({"jobs": jobs, "sources": []},
                                                     {"jobs": list(rows.values()), "sources": []}, rows["selected"])
        assert asyncio.get_running_loop().time() - started < 1.0
        settled = deepcopy(diagnostic_history)
        await asyncio.sleep(0.12)  # Past the fake model's original completion time.
        assert diagnostic_history == settled and not model_returns and not model.active

    asyncio.run(check())
    assert outer_calls == [5.02 if deadline == "inner" else 0.02]
    assert len(finishes) == 1 and finishes[0][0] == "selected" and finishes[0][1]
    assert claims == ["selected"] and rows["following"] == before_following
    assert model.calls == 1 and model.closed
    assert not transactions and not writes
    assert len(diagnostic_history) == 1
    assert diagnostic_history[0]["code"] == ("timeout" if deadline == "inner" else "cancelled")
    assert diagnostic_history[0]["counts"]["written_records"] == 0
    if controller == "copy":
        assert len(report["jobs"]) == len(publications) == 1
        assert report["jobs"][0]["state"] == "failed"


def test_evidence_excludes_target_raw_error_and_history(replay):
    class FakeStore:
        def job_status(self, project, job):
            return {"id": job, "state": "failed", "attempts": 2, "error_code": "invalid_jsonl",
                    "error": "PRIVATE original exception", "target": {"path": "PRIVATE transcript"},
                    "attempt_history": [{"error": "PRIVATE old error"}],
                    "diagnostics": {"code": "invalid_jsonl"}}

    result = replay.job_result(FakeStore(), {"id": "job", "project_id": "project"})
    assert result["error_code"] == "invalid_jsonl"
    assert not {"error", "target", "attempt_history"} & result.keys()
    assert "PRIVATE" not in repr(result)


@pytest.mark.parametrize("owned,already_exited", [(False, False), (True, True)])
def test_engine_cleanup_refuses_changed_identity_or_unexpected_exit(replay, monkeypatch, owned, already_exited):
    signals = []

    class Poll:
        def register(self, handle, events):
            pass

        def poll(self, timeout):
            return [(17, 1)] if already_exited else []

    def require(condition, message):
        if not condition:
            raise ValueError(message)

    monkeypatch.setattr(replay.select, "poll", Poll)
    monkeypatch.setattr(replay.signal, "pidfd_send_signal", lambda *args: signals.append(args))
    with pytest.raises(ValueError):
        replay.stop_owned_engine(17, {"pid": 25}, types.SimpleNamespace(owns=lambda row: owned),
                                 types.SimpleNamespace(require=require))
    assert not signals


def test_engine_shutdown_uses_admitted_handle_and_verifies_exit(replay, monkeypatch):
    signals, polls = [], []

    class Poll:
        def register(self, handle, events):
            assert handle == 17

        def poll(self, timeout):
            polls.append(timeout)
            return [] if timeout == 0 else [(17, 1)]

    def require(condition, message):
        if not condition:
            raise ValueError(message)

    monkeypatch.setattr(replay.select, "poll", Poll)
    monkeypatch.setattr(replay.signal, "pidfd_send_signal", lambda *args: signals.append(args))
    monkeypatch.setattr(replay.os, "waitpid", lambda *args: (25, 143 << 8))
    result = replay.stop_owned_engine(17, {"pid": 25}, types.SimpleNamespace(owns=lambda row: True),
                                      types.SimpleNamespace(require=require))
    assert signals == [(17, replay.signal.SIGTERM)]
    assert polls == [0, 30000]
    assert result == {"engine_actual_exit_verified": True, "engine_returncode": 143}
