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
def test_default_transport_skips_ca_loading_is_kept_between_requests_and_closes(tmp_path, monkeypatch, url):
    requests, closed, opened = [], [], []

    def forbidden(*args, **kwargs):
        raise AssertionError("A loopback HTTP request loaded TLS CA certificates")

    def handle(self, request):
        requests.append(request)
        return httpx.Response(200, json={"result": []}, headers={"arcadedb-session-id": "tx"})

    original_exit = httpx.HTTPTransport.__exit__
    def exiting(self, *args):
        original_exit(self, *args)
        closed.append(self)

    monkeypatch.setattr(ssl, "create_default_context", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    original_close = httpx.HTTPTransport.close
    def closing(self):
        original_close(self)
        closed.append(self)
    original_init = httpx.HTTPTransport.__init__
    def starting(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        opened.append(self)
    monkeypatch.setattr(httpx.HTTPTransport, "__exit__", exiting)
    monkeypatch.setattr(httpx.HTTPTransport, "close", closing)
    monkeypatch.setattr(httpx.HTTPTransport, "__init__", starting)
    client = ArcadeClient(settings(tmp_path, url))
    assert client.query("SELECT 1") == []
    client.server_info()
    with client.transaction() as tx:
        tx.query("SELECT 1")
    # One transport served every request, those of the transaction included, and nothing was closed meanwhile.
    assert len(requests) == 5 and len(opened) == 1 and closed == []
    client.close()
    assert closed == opened
    assert client.query("SELECT 1") == [] and len(opened) == 2  # still usable: a new one is opened
    client.close()
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


def test_a_failed_request_leaves_the_client_usable(tmp_path, monkeypatch):
    answers = [httpx.ConnectError("synthetic"), httpx.Response(200, json={"result": [{"ok": 1}]})]
    def handle(self, request):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    client = ArcadeClient(settings(tmp_path))
    with pytest.raises(ArcadeError, match="ConnectError"):
        client.query("SELECT 1")
    assert client.query("SELECT 1") == [{"ok": 1}]
    client.close()


def test_only_a_read_outside_a_transaction_is_sent_again_when_a_kept_connection_is_dead(tmp_path):
    seen = []
    def handle(request):
        seen.append(request.url.path.split("/")[3])
        if len(seen) % 2:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=request)
        return httpx.Response(200, json={"result": []}, headers={"arcadedb-session-id": "tx"})
    client = ArcadeClient(settings(tmp_path), transport=httpx.MockTransport(handle))
    assert client.query("SELECT 1") == [] and seen == ["query", "query"]
    seen.clear()
    with pytest.raises(ArcadeError, match="RemoteProtocolError"):
        client.command("UPDATE x SET y=1")  # it may have been applied: never repeated
    assert seen == ["command"]
    seen[:] = ["skip"]  # the next request succeeds, the one after it fails
    with pytest.raises(ArcadeError, match="RemoteProtocolError"):
        with client.transaction() as tx:
            tx.query("SELECT 1")  # inside a transaction nothing is repeated either
    assert seen == ["skip", "begin", "query", "rollback"]


def test_a_forked_process_does_not_use_the_connections_of_its_parent(tmp_path, monkeypatch):
    import cairnmind.arcade as arcade

    client = ArcadeClient(settings(tmp_path), transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"result": []})))
    client.query("SELECT 1")
    first = client._connections.client()
    assert client._connections.client() is first
    monkeypatch.setattr(arcade.os, "getpid", lambda: -1)
    assert client._connections.client() is not first


def test_threads_share_the_kept_connections_of_one_client(tmp_path):
    seen = []
    def handle(request):
        seen.append(request.headers.get("arcadedb-session-id"))
        return httpx.Response(200, json={"result": []})
    client = ArcadeClient(settings(tmp_path), transport=httpx.MockTransport(handle))
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda n: (client.query("SELECT 1"), client._connections.client()), range(64)))
    assert [rows for rows, _ in results] == [[]] * 64 and seen == [None] * 64
    assert len({id(kept) for _, kept in results}) == 1  # one pool for every thread


def test_threads_that_start_together_make_one_pool(tmp_path, monkeypatch):
    import threading

    made = []
    original = httpx.Client.__init__

    def counting(self, *args, **kwargs):
        made.append(self)
        original(self, *args, **kwargs)
    monkeypatch.setattr(httpx.Client, "__init__", counting)
    client = ArcadeClient(settings(tmp_path), transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"result": []})))
    barrier = threading.Barrier(8)

    def run(_):
        barrier.wait(10)
        return client.query("SELECT 1")
    with ThreadPoolExecutor(max_workers=8) as executor:
        assert list(executor.map(run, range(8))) == [[]] * 8
    assert len(made) == 1


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
