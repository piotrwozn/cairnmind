"""HTTP-only ArcadeDB requests must never relax any other client's TLS policy."""
from concurrent.futures import ThreadPoolExecutor
import ssl

import httpx
import pytest

from cairnmind.arcade import ArcadeClient, ArcadeError
from cairnmind.config import Settings


def settings(tmp_path, url="http://127.0.0.1:2480"):
    return Settings(data_dir=tmp_path, arcade_url=url, arcade_database="transport_test",
                    arcade_password="synthetic-local-password")


@pytest.mark.parametrize("url", ["http://127.0.0.1:2480", "http://localhost:2480", "http://[::1]:2480"])
def test_default_transport_skips_ca_loading_and_closes(tmp_path, monkeypatch, url):
    requests, closed = [], []

    def forbidden(*args, **kwargs):
        raise AssertionError("A loopback HTTP request loaded TLS CA certificates")

    def handle(self, request):
        requests.append(request)
        return httpx.Response(200, json={"result": []})

    original_exit = httpx.HTTPTransport.__exit__
    def exiting(self, *args):
        original_exit(self, *args)
        closed.append(self)

    monkeypatch.setattr(ssl, "create_default_context", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    monkeypatch.setattr(httpx.HTTPTransport, "__exit__", exiting)
    client = ArcadeClient(settings(tmp_path, url))
    assert client.query("SELECT 1") == []
    client.server_info()
    assert len(requests) == len(closed) == 2
    assert requests[0].extensions["timeout"]["read"] == 30
    assert requests[1].extensions["timeout"]["read"] == 2
    assert all(request.url.scheme == "http" for request in requests)


def test_external_settings_changes_cannot_redirect_client_or_transaction(tmp_path):
    original = settings(tmp_path)
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"result": []}, headers={"arcadedb-session-id": "tx-bound"})

    client = ArcadeClient(original, transport=httpx.MockTransport(handle))
    original.arcade_url = "https://external.invalid"
    original.arcade_password = "different-password"
    with client.transaction() as tx:
        tx.query("SELECT 1")
    assert len(seen) == 3
    assert all(request.url.host == "127.0.0.1" and request.url.scheme == "http" for request in seen)
    assert len({request.headers["authorization"] for request in seen}) == 1


@pytest.mark.parametrize("url", ["https://127.0.0.1", "http://example.com", "https://example.com",
                                "http://user:secret@127.0.0.1", "http://127.0.0.1?x=y",
                                "http://127.0.0.1#fragment"])
def test_invalid_origins_fail_before_transport(tmp_path, url):
    with pytest.raises(ValueError):
        ArcadeClient(settings(tmp_path, url))


@pytest.mark.parametrize("path", ["https://example.com/data", "http://127.0.0.1:9999/data",
                                 "//example.com/data", "api/v1/server", "/\\example.com",
                                 "/api#fragment", "/api\n"])
def test_nonrelative_or_ambiguous_paths_fail_before_transport(tmp_path, path):
    def forbidden(request):
        raise AssertionError("Invalid endpoint reached transport")
    client = ArcadeClient(settings(tmp_path), transport=httpx.MockTransport(forbidden))
    with pytest.raises(ValueError):
        client._request("GET", path)


@pytest.mark.parametrize("status", [302, 307])
def test_redirect_is_not_followed_and_cannot_be_enabled(tmp_path, status):
    seen = []
    def handle(request):
        seen.append(request)
        return httpx.Response(status, headers={"location": "https://external.invalid/private"})
    client = ArcadeClient(settings(tmp_path), transport=httpx.MockTransport(handle))
    with pytest.raises(ArcadeError):
        client.query("SELECT 1")
    with pytest.raises(ValueError, match="redirects"):
        client._request("GET", "/api/v1/server", follow_redirects=True)
    assert len(seen) == 1


def test_transport_closes_when_request_raises(tmp_path, monkeypatch):
    closed = []
    original_exit = httpx.HTTPTransport.__exit__
    def exiting(self, *args):
        original_exit(self, *args)
        closed.append(self)
    def fail(self, request):
        raise httpx.ConnectError("synthetic", request=request)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fail)
    monkeypatch.setattr(httpx.HTTPTransport, "__exit__", exiting)
    with pytest.raises(ArcadeError, match="ConnectError"):
        ArcadeClient(settings(tmp_path)).query("SELECT 1")
    assert len(closed) == 1


def test_transaction_headers_are_scoped_and_nested_transaction_reuses_session(tmp_path):
    seen = []
    def handle(request):
        seen.append((request.url.path, request.headers.get("arcadedb-session-id")))
        return httpx.Response(200, json={"result": []}, headers={"arcadedb-session-id": "session-one"})
    client = ArcadeClient(settings(tmp_path), transport=httpx.MockTransport(handle))
    with client.transaction() as tx:
        with tx.transaction() as nested:
            assert nested is tx
            nested.query("SELECT 1")
    client.query("SELECT 1")
    assert [session for _, session in seen] == [None, "session-one", "session-one", None]
    assert [path.split("/")[-2] for path, _ in seen] == ["begin", "query", "commit", "query"]
    seen.clear()
    with pytest.raises(RuntimeError, match="abort"):
        with client.transaction():
            raise RuntimeError("abort")
    assert [path.split("/")[-2] for path, _ in seen] == ["begin", "rollback"]


def test_parallel_clients_do_not_share_session_headers(tmp_path):
    def run(session):
        seen = []
        def handle(request):
            seen.append(request.headers.get("arcadedb-session-id"))
            return httpx.Response(200, json={"result": []})
        client = ArcadeClient(settings(tmp_path), session_id=session, transport=httpx.MockTransport(handle))
        client.query("SELECT 1")
        return seen
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(run, ["first", "second"])) == [["first"], ["second"]]


def test_regular_https_transport_still_uses_verified_context(tmp_path, monkeypatch):
    contexts = []
    def context(*args, **kwargs):
        value = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        contexts.append(value)
        return value
    monkeypatch.setattr(ssl, "create_default_context", context)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request",
                        lambda self, request: httpx.Response(200, json={"result": []}))
    ArcadeClient(settings(tmp_path)).query("SELECT 1")
    assert not contexts
    with httpx.HTTPTransport():
        pass
    assert len(contexts) == 1
    assert contexts[0].verify_mode == ssl.CERT_REQUIRED
    assert contexts[0].check_hostname is True
