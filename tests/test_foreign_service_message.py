"""A service that refuses this directory's token is named plainly by every client, not passed on bare (#291)."""
import asyncio

import httpx
import pytest
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.mcp import mcp_server
from cairnmind.config import Settings
from cairnmind.terminal.terminal_client import BrokerClient, BrokerError
from cairnmind.terminal.terminal_i18n import tr
from cairnmind.terminal.terminal_i18n_messages import broker_error_text

BARE = {"detail": "Invalid local service token"}


def _checked(text, settings):
    assert settings.url in text and str(settings.data_dir) in text and "`port`" in text
    assert settings.token not in text


def _workspace_client(settings, *, matched):
    client = BrokerClient(settings, transport=httpx.MockTransport(lambda request: httpx.Response(401, json=BARE)))
    client._url, client._csrf, client._allow_start = "http://127.0.0.1:1", "csrf", matched
    client._client = httpx.Client(base_url=client._url, transport=client._transport)
    return client


def test_the_workspace_names_a_service_of_another_data_directory(tmp_path):
    settings = Settings.load(tmp_path / "second", create=True)
    client = _workspace_client(settings, matched=False)
    client._authenticate = lambda: None
    with pytest.raises(BrokerError) as raised:
        client.request("GET", "/api/status")
    english = broker_error_text(raised.value, lambda text: tr(text, "en"))
    _checked(english, settings)
    assert "most likely belongs to another data directory" in english
    polish = broker_error_text(raised.value, lambda text: tr(text, "pl"))
    _checked(polish, settings)
    assert "należy do innego" in polish
    # a service this workspace started or matched by instance id is this directory's own: its 401 stays as it is
    own = _workspace_client(settings, matched=True)
    own._authenticate = lambda: None
    with pytest.raises(BrokerError) as raised:
        own.request("GET", "/api/status")
    assert "another data directory" not in str(raised.value) and "Invalid local service token" in str(raised.value)


def _foreign_async_client(monkeypatch):
    class Foreign:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, method, url, **kwargs):
            return httpx.Response(401, json=BARE, request=httpx.Request(method, url))
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", Foreign)


def test_the_mcp_bridge_names_a_service_of_another_data_directory(monkeypatch, tmp_path):
    settings = Settings.load(tmp_path / "second", create=True)
    _foreign_async_client(monkeypatch)
    external = mcp_server.ServiceClient(settings, external={"project": "p", "worktree": None})
    with pytest.raises(RuntimeError) as raised:
        asyncio.run(external._send("GET", "/projects", None, {}))
    _checked(str(raised.value), settings)
    assert "most likely belongs to another data directory" in str(raised.value)
    # the native client's service was matched by the launcher, not here: if another directory's service took the port since, it says so too
    native = mcp_server.ServiceClient(settings)
    with pytest.raises(RuntimeError) as raised:
        asyncio.run(native._send("GET", "/projects", None, {}))
    _checked(str(raised.value), settings)
    # a client that started or matched the service itself keeps the service's own 401
    native._matched = True
    with pytest.raises(RuntimeError) as raised:
        asyncio.run(native._send("GET", "/projects", None, {}))
    assert "another data directory" not in str(raised.value) and "Invalid local service token" in str(raised.value)


def test_status_shows_the_url_it_asked(monkeypatch, tmp_path):
    settings = Settings.load(tmp_path / "data", create=True)
    monkeypatch.setattr(cli, "request", lambda *args, **kwargs: {"queue": 0})
    result = CliRunner().invoke(cli.app, ["--data-dir", str(settings.data_dir), "status"])
    assert result.exit_code == 0 and settings.url in result.output and settings.token not in result.output


@pytest.mark.asyncio
@pytest.mark.parametrize("language,words", [("en", "most likely belongs to another data directory"), ("pl", "należy do innego")])
async def test_the_sidebar_names_a_foreign_service_from_the_state_once(tmp_path, monkeypatch, language, words):
    # #308: /api/state stays whole; its refusal flag is what the sidebar names, in the panel's language.
    from test_mcp_failure_notice import RunsBroker
    from cairnmind.terminal.terminal_panels import SidebarApp
    from test_terminal_panels import settle
    from test_terminal_ux080 import setup_ui
    from test_workspace_stage_scope import notice_of, wait_until
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch, language)
    broker = RunsBroker(broker.projects[0], lambda: [])
    real = broker.request
    broker.request = lambda method, path, **kw: {**real(method, path, **kw), "foreign_service": True} if path == "/api/state" \
        else real(method, path, **kw)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        await wait_until(pilot, lambda: words in notice_of(app))
        for _ in range(2):
            await app.load_state()
        entries = [e for e in app.notice_history.entries if words in e.text]
        assert len(entries) == 1 and entries[0].count == 1
        assert settings.url in notice_of(app) and str(settings.data_dir) in notice_of(app)
