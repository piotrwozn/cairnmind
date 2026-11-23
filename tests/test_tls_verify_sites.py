"""A client given an https address verifies certificates; one given a plain http address builds no store (#415).

httpx ignores `verify` when a transport is given, so the sites are driven with a recorder in place of the client
classes: it keeps the `verify` each site chose and gives the site a mock-backed client. What a `verify` value means
is read from a real client: its pool's TLS context.
"""
import contextlib
import ssl

import httpx
import pytest
from typer.testing import CliRunner

from cairnmind import api_runtime, cli, cockpit, integrations, topology
from cairnmind.api_providers import make_provider
from cairnmind.config import ENV_PREFIXES, Settings, tls_verify
from cairnmind.knowledge_mode import missing_models as REAL_PROBE
from cairnmind.memory_models import doctor_verify
from cairnmind.ollama import Ollama
from cairnmind.terminal_client import BrokerClient, BrokerError

HTTPS, HTTP = "https://models.example.com", "http://127.0.0.1:9911"
REAL_CLIENT, REAL_ASYNC = httpx.Client, httpx.AsyncClient


@pytest.fixture
def settings(tmp_path):
    return Settings.load(tmp_path / "data", create=True)


@pytest.fixture
def seen(monkeypatch):
    """The `verify` of every httpx client a site makes, each over a transport that answers an empty object."""
    verifies = []

    def recorder(real):
        def make(*args, **kwargs):
            verifies.append(kwargs.get("verify", "not stated"))
            kwargs["transport"] = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
            return real(*args, **kwargs)
        return make

    monkeypatch.setattr(httpx, "Client", recorder(REAL_CLIENT))
    monkeypatch.setattr(httpx, "AsyncClient", recorder(REAL_ASYNC))
    return verifies


def context(verify):
    return REAL_CLIENT(verify=verify)._transport._pool._ssl_context


def test_verify_true_checks_certificate_and_host_and_false_builds_no_store(monkeypatch):
    built = []
    real = ssl.create_default_context
    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **k: (built.append(1), real(*a, **k))[1])
    off = context(False)
    assert built == [] and off.verify_mode == ssl.CERT_NONE
    on = context(True)
    assert built and on.verify_mode == ssl.CERT_REQUIRED and on.check_hostname


@pytest.mark.parametrize("url,verified", [
    ("http://x", False), ("HTTP://x", False), ("hTtp://x:1/y", False), ("http://user:pw@x", False),
    ("https://x", True), ("HTTPS://x", True), ("https://user:pw@x", True), ("http+unix://", True),
    ("ftp://x", True), ("http:x", True), ("", True), (" http://x", True), ("\thttp://x", True), (None, True)])
def test_tls_verify_is_off_only_for_a_url_httpx_reads_as_plain_http(url, verified):
    assert tls_verify(url) is verified
    if isinstance(url, str) and url.strip() == url and url:
        # httpx connects to what it parses, case aside: plain http exactly when the verdict is False
        with contextlib.suppress(httpx.InvalidURL):
            assert (httpx.URL(url).scheme == "http" and bool(httpx.URL(url).host)) is (not verified)


def test_a_plain_http_address_that_redirects_is_never_followed():
    seen_requests = []

    def handler(request):
        seen_requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://elsewhere.example/"})

    with REAL_CLIENT(transport=httpx.MockTransport(handler), verify=tls_verify("http://x")) as client:
        assert client.get("http://x/").status_code == 302
    assert seen_requests == ["http://x/"]


def test_the_service_address_is_plain_http_whatever_the_host_and_port(settings):
    for host in ("127.0.0.1", "example.com", "[::1]"):
        settings.host = host
        assert settings.url.startswith("http://") and tls_verify(settings.url) is False


# Each site that may be given an address other than the service's: (name, driver(settings, url, tmp_path, monkeypatch)).
def _ollama(settings, url, tmp_path, monkeypatch):
    Ollama(settings, base_url=url)._new_client()


def _provider(settings, url, tmp_path, monkeypatch):
    provider = "ollama" if url.startswith("http:") else "openai_compatible"
    make_provider({"id": "api-fixture", "client": "api", "label": "F", "provider": provider, "model": "m",
                   "base_url": url})._http()


def _model_pull(settings, url, tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_PREFIXES[0] + "OLLAMA_URL", url.replace("models.example.com", "127.0.0.1:9911"))
    CliRunner().invoke(cli.app, ["--data-dir", str(settings.data_dir), "models", "pull"])


def _stop_cockpit(settings, url, tmp_path, monkeypatch):
    monkeypatch.setattr(cockpit, "cockpit_status", lambda s: {"available": True, "url": url})
    cockpit.stop_cockpit(settings)


def _doctor(settings, url, tmp_path, monkeypatch):
    settings.ollama_url = url.replace("models.example.com", "127.0.0.1:9911")
    integrations.doctor(settings)


def _missing_models(settings, url, tmp_path, monkeypatch):
    settings.ollama_url = url.replace("models.example.com", "127.0.0.1:9911")
    REAL_PROBE(settings)  # the autouse fixture replaced knowledge_mode.missing_models


def _probe(settings, url, tmp_path, monkeypatch):
    settings.ollama_url = url.replace("models.example.com", "127.0.0.1:9911")
    with contextlib.suppress(Exception):  # topology reads the store's writer; only the client matters here
        topology.probe(settings)


SITES = [_ollama, _provider, _model_pull, _stop_cockpit, _doctor, _missing_models, _probe]
ALSO_PLAIN = (_model_pull, _doctor, _probe)


@pytest.mark.parametrize("site", SITES, ids=lambda site: site.__name__)
@pytest.mark.parametrize("url", [HTTPS, HTTP], ids=["https", "http"])
def test_a_client_verifies_for_an_https_address_and_builds_no_store_for_a_plain_http_one(
        site, url, settings, seen, tmp_path, monkeypatch):
    with contextlib.suppress(Exception):
        site(settings, url, tmp_path, monkeypatch)
    https = url.startswith("https:")
    assert seen, "the site made no client"
    # These also make clients to the service or the engine (plain http, never verified); the rest make one client.
    assert https in seen and (site in ALSO_PLAIN or all(verify is https for verify in seen)), seen
    for verify in set(seen):
        assert (context(verify).verify_mode == ssl.CERT_REQUIRED) is verify


def test_the_broker_is_never_asked_over_https_so_no_client_is_built_for_it(settings, seen, monkeypatch):
    other = {"available": True, "url": "https://127.0.0.1:9911"}
    with pytest.raises(BrokerError):
        BrokerClient._validated(other)
    monkeypatch.setattr(cockpit, "cockpit_status", lambda s: other)
    with pytest.raises(BrokerError):
        api_runtime.api_worker_config(settings, "run", 1)
    assert api_runtime.report_mcp_failure(settings, {"id": "r", "generation": 1, "project_id": "p"}, "s", "x") is False
    with pytest.raises(BrokerError):
        BrokerClient(settings)._connect(other)
    assert seen == []


# doctor_verify: the one client doctor shares across servers.
def row(url, **more):
    return {"provider": "openai_compatible", "base_url": url, "allow_remote": not url.startswith("http://127."), **more}


def test_doctor_verify_is_off_only_when_every_address_it_may_ask_is_plain_http(settings):
    assert doctor_verify(settings) is False
    settings.memory_chat = row(HTTPS)  # one https server among plain http ones
    assert doctor_verify(settings) is True
    settings.memory_chat, settings.memory_embedding = None, row(HTTPS)
    assert doctor_verify(settings) is True
    settings.memory_embedding = row("http://127.0.0.1:9912")
    assert doctor_verify(settings) is False
    settings.ollama_url = "https://127.0.0.1:11434"  # the local Ollama behind TLS
    assert doctor_verify(settings) is True


@pytest.mark.parametrize("bad", [{"provider": "nobody"}, "text", {"provider": "ollama", "base_url": HTTPS}])
def test_doctor_verify_verifies_when_a_role_cannot_be_read(settings, bad):
    settings.memory_chat = bad  # an invalid profile, or a remote server without its consent
    assert doctor_verify(settings) is True


def test_doctor_verify_verifies_when_ollama_url_is_another_computer(settings):
    settings.ollama_url = "http://models.example.com:11434"  # refused, never asked: still not decided by the scheme
    assert doctor_verify(settings) is True
