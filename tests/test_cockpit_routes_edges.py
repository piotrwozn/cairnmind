"""Additional fast-tier route coverage for cockpit.py (_control, profiles, credentials, models)."""
from __future__ import annotations

import httpx
from test_cockpit_edges import _FakeRuntime, _make_auth_client

from cairnmind.agents.api_providers import ProviderError
from cairnmind.config import Settings
from cairnmind.terminal import cockpit
from cairnmind.terminal.cockpit_state import CockpitState


def _authed_client(tmp_path, monkeypatch, *, runtime=None, service_handler=None):
    settings = Settings.load(tmp_path / "data", create=True)
    CockpitState(settings).update({
        "profiles": [
            {"id": "codex", "label": "Codex", "client": "codex"},
            {"id": "claude", "label": "Claude", "client": "claude"},
            {"id": "copilot", "label": "Copilot", "client": "copilot"},
            {"id": "openai", "label": "OpenAI", "client": "api", "provider": "openai",
             "model": "gpt-4o", "base_url": "https://api.openai.com/v1"},
        ]
    })

    def default_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/projects":
            if request.method == "POST":
                return httpx.Response(200, json={"project": {"id": "p2", "name": "P2", "root": str(tmp_path)}})
            return httpx.Response(200, json=[{"id": "p1", "name": "P1", "root": str(tmp_path)}])
        if request.url.path == "/project-roots":
            return httpx.Response(200, json={"roots": [str(tmp_path)], "unavailable": []})
        return httpx.Response(200, json={"ok": True})

    rt = runtime or _FakeRuntime()
    app = cockpit.create_cockpit_app(
        settings,
        public_url="http://127.0.0.1:8766",
        runtime=rt,
        proxy_transport=httpx.MockTransport(service_handler or default_handler),
    )
    client, csrf = _make_auth_client(app, settings.token)
    headers = {"x-csrf-token": csrf, "origin": "http://127.0.0.1:8766"}
    ctrl_headers = {"authorization": f"Bearer {settings.token}"}
    return app, client, headers, ctrl_headers, rt


def test_cockpit_control_profiles_credentials_and_models_edges(tmp_path, monkeypatch):
    app, client, headers, ctrl, rt = _authed_client(tmp_path, monkeypatch)

    # _control/agent invalid action (404), invalid identity (422), missing method (409), mcp_failure (422 & 409 & 200)
    assert client.post("/_control/agent/bogus", headers=ctrl, json={}).status_code == 404
    assert client.post("/_control/agent/config", headers=ctrl, json={"run_id": 123, "generation": "bad"}).status_code == 422
    assert client.post("/_control/agent/config", headers=ctrl, json={"run_id": "r1", "generation": 1}).status_code == 409
    assert client.post(
        "/_control/agent/mcp_failure",
        headers=ctrl,
        json={"run_id": "r1", "generation": "bad", "project_id": "p1", "server_id": "s1", "code": "start_failed"},
    ).status_code == 422
    assert client.post(
        "/_control/agent/mcp_failure",
        headers=ctrl,
        json={"run_id": "r1", "generation": 1, "project_id": "p1", "server_id": "s1", "code": "start_failed"},
    ).status_code == 409
    rt.record_mcp_failure = lambda **kw: {"recorded": True}
    assert client.post(
        "/_control/agent/mcp_failure",
        headers=ctrl,
        json={"run_id": "r1", "generation": 1, "project_id": "p1", "server_id": "s1", "code": "start_failed"},
    ).status_code == 200

    # _control/stop validation (422) and stop_clients=True + shutdown callback
    shutdown_called = []
    app.state.shutdown = lambda: shutdown_called.append(True)
    assert client.post("/_control/stop", headers=ctrl, json={"stop_clients": "yes"}).status_code == 422
    res_stop = client.post("/_control/stop", headers=ctrl, json={"stop_clients": True})
    assert res_stop.status_code == 200 and rt.stopped

    # /auth/session, /api/settings, /api/projects POST, /api/profiles PUT
    assert client.get("/auth/session").status_code == 200
    assert client.get("/api/settings").status_code == 200
    proj_dir = tmp_path / "proj2"
    proj_dir.mkdir()
    assert client.post("/api/projects", headers=headers, json={"name": "P2", "root": str(proj_dir)}).status_code == 200
    assert client.put("/api/profiles", headers=headers, json={"profiles": client.get("/api/profiles").json()["profiles"]}).status_code == 200

    # /api/profiles/{id}/status: unknown (404), codex (probe_client), copilot (runtime_status)
    assert client.get("/api/profiles/does-not-exist/status").status_code == 404
    monkeypatch.setattr("cairnmind.agents.client_auth.probe_client", lambda c: {"client": c, "auth": "signed_in"})
    assert client.get("/api/profiles/codex/status").status_code == 200
    monkeypatch.setattr("cairnmind.agents.copilot_adapter.runtime_status", lambda s: {"available": False})
    assert client.get("/api/profiles/copilot/status").json()["runtime"] == {"available": False}

    # DELETE credential on native client (422)
    assert client.delete("/api/profiles/codex/credential", headers=headers).status_code == 422

    # /api/profiles/{id}/models: native (422), ProviderError with context_details, httpx.HTTPError, copilot
    assert client.get("/api/profiles/codex/models").status_code == 422

    class _ErrProvider:
        def __init__(self, exc):
            self.exc = exc

        async def list_models(self):
            raise self.exc

        async def aclose(self):
            pass

    monkeypatch.setattr(
        "cairnmind.agents.api_providers.make_provider",
        lambda p, k: _ErrProvider(ProviderError("context_limit", context_details={"configured_context_window": 4096})),
    )
    res_err = client.get("/api/profiles/openai/models").json()
    assert res_err["status"] == "failed" and res_err["code"] == "context_limit"

    monkeypatch.setattr("cairnmind.agents.api_providers.make_provider", lambda p, k: _ErrProvider(httpx.ConnectError("down")))
    assert client.get("/api/profiles/openai/models").json()["code"] == "provider_unavailable"

    class _OkCopilot:
        def __init__(self, *a, **k):
            pass

        async def list_models(self):
            return {"models": [{"id": "gpt-4o"}]}

        def close(self):
            pass

    monkeypatch.setattr("cairnmind.agents.copilot_adapter.CopilotAdapter", _OkCopilot)
    assert client.get("/api/profiles/copilot/models").json()["models"][0]["id"] == "gpt-4o"
