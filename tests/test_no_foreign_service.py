"""The conftest guard that keeps every test away from the default ports of the machine that runs it (#221)."""

import socket
from pathlib import Path
from urllib.parse import urlparse

import pytest

from cairnmind.config import Settings

DEFAULTS = Settings(data_dir=Path("."))
PORTS = {"service": DEFAULTS.port, "cockpit": DEFAULTS.cockpit_port, "ollama": urlparse(DEFAULTS.ollama_url).port}


@pytest.mark.parametrize("port", PORTS.values(), ids=PORTS.keys())
def test_a_default_port_is_refused_even_when_something_listens_there(port):
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            pytest.skip(f"port {port} is taken on this machine, so a real listener cannot be set up")
        listener.listen()
        with socket.socket() as client:
            with pytest.raises(ConnectionRefusedError):
                client.connect(("127.0.0.1", port))
        with socket.socket() as client:
            assert client.connect_ex(("127.0.0.1", port)) == 111


def test_any_other_port_still_connects():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with socket.socket() as client:
            client.connect(listener.getsockname())
