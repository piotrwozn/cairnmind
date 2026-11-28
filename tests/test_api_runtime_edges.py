"""Fast-tier edge-case coverage tests for cairnmind.api_runtime."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from cairnmind import api_runtime as ar
from cairnmind.config import Settings
from cairnmind.private_files import write_durable_private
from cairnmind.terminal_client import BrokerError, BrokerUnreachable


def test_read_private_and_prepare_session_errors(tmp_path):
    settings = Settings.load(tmp_path / "data", create=True)

    # Non-dict JSON rejected
    arr = tmp_path / "arr.json"
    write_durable_private(arr, json.dumps([1, 2, 3]))
    with pytest.raises(ValueError, match="agent_configuration_invalid"):
        ar._read_private(arr)

    # Oversized file rejected
    big = tmp_path / "big.json"
    write_durable_private(big, '{"a":"' + ("x" * (260 * 1024)) + '"}')
    with pytest.raises(ValueError, match="agent_configuration_invalid"):
        ar._read_private(big)

    # _prepare_session error responses: 500, invalid JSON shape, network error
    proj = {"id": "p1", "root": str(tmp_path)}
    sup_500 = ar.RuntimeSupervisor(
        settings,
        session_transport=httpx.MockTransport(lambda _r: httpx.Response(500, json={"detail": "err"})),
    )
    with pytest.raises(ValueError, match="memory_unavailable"):
        sup_500._prepare_session(proj, "api", str(tmp_path))

    sup_bad = ar.RuntimeSupervisor(
        settings,
        session_transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"status": "bad"})),
    )
    with pytest.raises(ValueError, match="memory_unavailable"):
        sup_bad._prepare_session(proj, "api", str(tmp_path))

    def raise_http(_r):
        raise httpx.ConnectError("down")

    sup_err = ar.RuntimeSupervisor(settings, session_transport=httpx.MockTransport(raise_http))
    with pytest.raises(ValueError, match="memory_unavailable"):
        sup_err._prepare_session(proj, "api", str(tmp_path))


def test_runtime_supervisor_validation_auth_and_worker_helpers(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data", create=True)
    sup = ar.RuntimeSupervisor(settings)

    # start_project refuses when tmux unavailable
    monkeypatch.setattr(sup, "diagnostics", lambda: {"available": False})
    with pytest.raises(ValueError, match="tmux_missing_or_unsupported"):
        sup.start_project({"id": "p1", "root": str(tmp_path)})

    # start_agent validation: bad slot_id, data_dir inside worktree, copilot unavailable
    with pytest.raises(ValueError, match="profile_invalid"):
        sup.start_agent({"id": "p1", "root": str(tmp_path)}, {"id": "a1", "client": "api", "label": "A", "provider": "ollama", "model": "qwen"}, slot_id="bad slot!")

    monkeypatch.setattr("cairnmind.copilot_adapter.runtime_status", lambda _s: {"available": False, "code": "copilot_runtime_missing"})
    work = tmp_path / "repo"
    work.mkdir()
    with pytest.raises(ValueError, match="copilot_runtime_missing"):
        sup.start_agent({"id": "p1", "root": str(work)}, {"id": "cp1", "client": "copilot", "label": "CP"}, slot_id="slot1")

    # start_auth for copilot: spawns & reuses live auth run
    monkeypatch.setattr("cairnmind.copilot_adapter.login_command", lambda _s: ["python", "-m", "cairnmind.copilot_adapter", "--login"])
    spawned = []

    def fake_spawn(rows, project, client, slot_id, launch, generation, purpose, run_id=None):
        row = {"id": "auth1", "client": client, "purpose": purpose, "lifecycle": "running", "generation": generation}
        rows.append(row)
        sup._save(rows)
        spawned.append(row)
        return dict(row)

    monkeypatch.setattr(sup, "_spawn", fake_spawn)
    monkeypatch.setattr(sup, "_refresh", lambda row: row)
    first_auth = sup.start_auth("copilot")
    second_auth = sup.start_auth("copilot")
    assert first_auth["id"] == second_auth["id"] == "auth1" and len(spawned) == 1

    # api_worker_config: happy path, non-str key, and HTTPError
    monkeypatch.setattr("cairnmind.cockpit.cockpit_status", lambda _s: {"available": True, "url": "http://127.0.0.1:8766"})
    monkeypatch.setattr("cairnmind.terminal_client.BrokerClient._validated", staticmethod(lambda _st: "http://127.0.0.1:8766"))

    class FakeHttpClient:
        def __init__(self, responses):
            self._responses = iter(responses)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, _path, json=None):
            item = next(self._responses)
            if isinstance(item, Exception):
                raise item
            return item

    ok_cfg = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"run": {"id": "r1"}, "profile": {}})
    ok_key = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"key": "secret-k"})
    monkeypatch.setattr(ar.httpx, "Client", lambda *a, **k: FakeHttpClient([ok_cfg, ok_key]))
    got = ar.api_worker_config(settings, "r1", 1)
    assert got["key"] == "secret-k"

    bad_key = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"key": 12345})
    monkeypatch.setattr(ar.httpx, "Client", lambda *a, **k: FakeHttpClient([ok_cfg, bad_key]))
    with pytest.raises(ValueError, match="cannot be restored"):
        ar.api_worker_config(settings, "r1", 1)

    monkeypatch.setattr(ar.httpx, "Client", lambda *a, **k: FakeHttpClient([httpx.ConnectError("down")]))
    with pytest.raises(ValueError, match="cannot be restored"):
        ar.api_worker_config(settings, "r1", 1)

    # stage_readiness credential error fallback
    bad_creds = SimpleNamespace(status=lambda _p: (_ for _ in ()).throw(OSError("unreadable")))
    obs = ar.stage_readiness(settings, bad_creds)
    assert obs({"client": "api"}) == {"credential": None, "runtime": None}

    # cli_stage_readiness: non-dict status, BrokerUnreachable, BrokerError
    class FakeBroker:
        def __init__(self, outcome):
            self._outcome = outcome
            self.closed = False

        def connect(self):
            if self._outcome == "connect_err":
                raise BrokerError(503, "no")

        def request(self, *_a, **_k):
            if self._outcome == "unreachable":
                raise BrokerUnreachable(0, "gone")
            if self._outcome == "err":
                raise BrokerError(500, "fail")
            return self._outcome

        def close(self):
            self.closed = True

    monkeypatch.setattr("cairnmind.terminal_client.BrokerClient", lambda _s: FakeBroker("not-a-dict"))
    assert ar.cli_stage_readiness(settings)({"id": "p1", "client": "api"}) == {"credential": None, "runtime": None}
    monkeypatch.setattr("cairnmind.terminal_client.BrokerClient", lambda _s: FakeBroker("unreachable"))
    assert ar.cli_stage_readiness(settings)({"id": "p1", "client": "api"}) == {"credential": "unknown", "runtime": "unknown"}
    monkeypatch.setattr("cairnmind.terminal_client.BrokerClient", lambda _s: FakeBroker("err"))
    assert ar.cli_stage_readiness(settings)({"id": "p1", "client": "api"}) == {"credential": None, "runtime": None}

    # coding_profiles_status: live broker ok & error, plus corrupt preferences fallback
    from cairnmind.cockpit_state import CockpitState
    cs = CockpitState(settings)
    existing = cs.load()["profiles"]
    cs.update({
        "profiles": [
            *existing,
            {"id": "p-api", "client": "api", "label": "API", "provider": "ollama", "model": "qwen"},
            {"id": "p-cp", "client": "copilot", "label": "CP"},
        ]
    })
    monkeypatch.setattr("cairnmind.terminal_client.BrokerClient", lambda _s: FakeBroker({"code": "ok"}))
    st_live = ar.coding_profiles_status(settings)
    assert st_live["observed_only"] is True and st_live["profiles"][0]["status"] == {"code": "ok"}

    monkeypatch.setattr("cairnmind.terminal_client.BrokerClient", lambda _s: FakeBroker("err"))
    st_err = ar.coding_profiles_status(settings)
    assert st_err["profiles"][0]["status"] == {"code": "unavailable"}

    monkeypatch.setattr("cairnmind.cockpit.cockpit_status", lambda _s: {"available": False})
    st_local = ar.coding_profiles_status(settings)
    cp_row = next(r for r in st_local["profiles"] if r["id"] == "p-cp")
    assert "runtime" in cp_row

    (settings.data_dir / "cockpit" / "preferences.json").write_text("{corrupt", encoding="utf-8")
    assert ar.coding_profiles_status(settings)["code"] == "profile_configuration_invalid"
