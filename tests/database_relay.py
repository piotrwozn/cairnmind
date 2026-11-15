"""A relay in front of an ArcadeDB server, for tests and measurements of a database reached over a network (#68).

It forwards HTTP/1.1 requests one at a time per connection, so it can do three things a plain TCP
relay cannot: count the requests, delay each of them by a fixed time, as a network would, and cut the
connection at one chosen request, either before the server sees it or after the server answered but
before the answer reaches the client (the case in which a client cannot know whether its commit was
applied). After a cut the relay is down, as a dropped network is: every connection is closed and new
ones are closed at once, until ``heal()``.

With ``tls`` (a folder holding server.pem and server.key) it ends TLS itself, so a server that
listens on loopback over plain http can be reached as an https host that is not this machine.

    python tests/database_relay.py --upstream-port 2480 --address 192.0.2.7 --tls FOLDER --delay-ms 20

runs it alone and prints its address and, when stopped (Ctrl+C or SIGTERM), the number of requests.
"""
from __future__ import annotations

from contextlib import suppress
import socket
import ssl
import threading
import time


class _Closed(Exception):
    pass


def _chunked_complete(body: bytes) -> bool:
    """Whether a chunked body is whole: each chunk is a size line, the data and CRLF; size 0 ends it."""
    position = 0
    while True:
        end = body.find(b"\r\n", position)
        if end < 0:
            return False
        size = int(body[position:end].split(b";")[0], 16)
        if size == 0:
            return body.find(b"\r\n\r\n", end) >= 0
        position = end + 2 + size + 2
        if position > len(body):
            return False


def _read_message(sock, *, response_to: bytes | None = None) -> bytes:
    """One whole HTTP/1.1 message from ``sock``. ``response_to`` is the request method when reading a response."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(65536)
        if not chunk:
            raise _Closed()
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        headers[name.strip().lower()] = value.strip()
    if response_to is not None:
        status = int(lines[0].split()[1])
        if response_to == b"HEAD" or status in (204, 304) or 100 <= status < 200:
            return data
    if headers.get(b"transfer-encoding", b"").lower() == b"chunked":
        while not _chunked_complete(body):
            chunk = sock.recv(65536)
            if not chunk:
                raise _Closed()
            body += chunk
        return head + b"\r\n\r\n" + body
    length = int(headers.get(b"content-length", b"0"))
    while len(body) < length:
        chunk = sock.recv(65536)
        if not chunk:
            raise _Closed()
        body += chunk
    return head + b"\r\n\r\n" + body


class Relay:
    def __init__(self, upstream_port: int, *, address: str = "127.0.0.1", tls=None, delay: float = 0.0,
                 upstream_host: str = "127.0.0.1"):
        self.upstream = (upstream_host, upstream_port)
        self.delay = delay
        self.requests = 0
        self.down = False
        self.seen: list[bytes] = []  # the request line of every request, in order
        self._cut = None
        self._lock = threading.Lock()
        self._sockets: set = set()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._context = None
        if tls is not None:
            self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self._context.load_cert_chain(f"{tls}/server.pem", f"{tls}/server.key")
        self._listener = socket.create_server((address, 0))
        self._listener.settimeout(0.2)
        self.address, self.port = address, self._listener.getsockname()[1]
        self.url = f"{'https' if tls is not None else 'http'}://{address}:{self.port}"
        self._acceptor = threading.Thread(target=self._accept, daemon=True)
        self._acceptor.start()

    # --- what a test asks for ---------------------------------------------------------------------

    def cut_at(self, number: int = 1, *, matching: bytes = b"", after_server: bool = False) -> None:
        """Cut at the ``number``-th request from now whose head or body contains ``matching``.

        ``after_server=False``: the request never reaches the server. ``after_server=True``: the server
        receives and answers it, and the answer is dropped.
        """
        left = [number]

        def when(request: bytes) -> bool:
            if matching not in request:
                return False
            left[0] -= 1
            return left[0] <= 0
        self.cut_when(when, after_server=after_server)

    def cut_when(self, when, *, after_server: bool = False) -> None:
        """Cut at the first request, from now, for which ``when(request bytes)`` is true."""
        with self._lock:
            self._cut = {"when": when, "after_server": after_server}

    def heal(self) -> None:
        with self._lock:
            self.down, self._cut = False, None

    def close(self) -> None:
        self._stop.set()
        self._drop_all()
        self._acceptor.join(5)
        self._listener.close()
        for thread in self._threads:
            thread.join(5)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- the relay ---------------------------------------------------------------------------------

    def _drop_all(self) -> None:
        with self._lock:
            sockets, self._sockets = self._sockets, set()
        for sock in sockets:
            with suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with suppress(OSError):
                sock.close()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                raw, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            thread = threading.Thread(target=self._serve, args=(raw,), daemon=True)
            thread.start()
            self._threads.append(thread)

    def _decide(self, request: bytes) -> str:
        """forward, drop (before the server) or lose (the server's answer), for this request."""
        with self._lock:
            self.requests += 1
            self.seen.append(request.split(b"\r\n", 1)[0])
            cut = self._cut
            if self.down:
                return "drop"
            if cut is None or not cut["when"](request):
                return "forward"
            self._cut, self.down = None, True
            return "lose" if cut["after_server"] else "drop"

    def _serve(self, raw) -> None:
        client = upstream = None
        try:
            raw.settimeout(180)
            if self.down:
                raw.close()
                return
            client = self._context.wrap_socket(raw, server_side=True) if self._context else raw
            upstream = socket.create_connection(self.upstream, timeout=180)
            with self._lock:
                self._sockets.update((client, upstream))
            while not self._stop.is_set():
                request = _read_message(client)
                decision = self._decide(request)
                if decision == "drop":
                    self._drop_all()
                    return
                if self.delay:
                    time.sleep(self.delay)
                upstream.sendall(request)
                response = _read_message(upstream, response_to=request.split(b" ", 1)[0])
                if decision == "lose":
                    self._drop_all()
                    return
                client.sendall(response)
        except (_Closed, OSError, ssl.SSLError, ValueError, IndexError):
            pass
        finally:
            for sock in (client or raw, upstream):
                if sock is not None:
                    with self._lock:
                        self._sockets.discard(sock)
                    with suppress(OSError):
                        sock.close()


def main() -> None:
    import argparse
    import signal
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--upstream-port", type=int, required=True)
    parser.add_argument("--address", default="127.0.0.1")
    parser.add_argument("--tls", help="a folder holding server.pem and server.key")
    parser.add_argument("--delay-ms", type=float, default=0.0)
    args = parser.parse_args()
    relay = Relay(args.upstream_port, address=args.address, tls=args.tls, delay=args.delay_ms / 1000)
    print(relay.url, flush=True)

    def stop(*_):
        raise KeyboardInterrupt

    # A shell starts a background command with SIGINT ignored, so SIGTERM stops it as well.
    signal.signal(signal.SIGTERM, stop)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        relay.close()
        print(f"requests {relay.requests}", flush=True)


if __name__ == "__main__":
    main()
