"""Persistence tests use the real, pinned ArcadeDB Java engine.

Ollama and coding clients are mocked; storage/FTS/ANN/graph/transactions are not.
An existing isolated developer test engine can be reused with
DEVBRAIN_TEST_ARCADE_HOME. Each pytest process owns a unique database namespace.

Every test module is marked ``arcade`` and starts the engine, unless it is listed in
NO_ARCADE_MODULES. Add a module there only after it passes with no engine running.
Modules in TMUX_MODULES drive a real tmux server and are also marked ``tmux``.
Fast local run: ``uv run pytest -m "not arcade and not tmux"``.
"""
from __future__ import annotations

import os
from pathlib import Path
import socket
import uuid

import pytest

from devbrain.arcade import ArcadeClient, ArcadeError
from devbrain.config import Settings
from devbrain.engine import install_engine, start_engine, stop_engine

# Verified on 2026-11-09: each passes in full with no ArcadeDB engine available.
NO_ARCADE_MODULES = frozenset({
    "test_agent_journal", "test_agent_tools", "test_answer_contract", "test_api_broker",
    "test_api_credentials", "test_api_panels", "test_api_profiles", "test_api_providers",
    "test_api_runtime", "test_api_worker", "test_arcade_transport", "test_client_auth",
    "test_client_runtime", "test_cockpit_cli", "test_cockpit_server", "test_cockpit_state",
    "test_copilot_adapter", "test_graph_raster", "test_graph_widget_rendering",
    "test_native_evaluation", "test_native_session_environment", "test_ollama", "test_parsing",
    "test_runtime_pane", "test_runtime_selection", "test_terminal_client", "test_terminal_commands",
    "test_terminal_graph", "test_terminal_latency080", "test_terminal_panels", "test_terminal_preview",
    "test_terminal_ux080", "test_workspace", "test_workspace_cli", "test_workspace_locale",
    "test_workspace_state", "test_workspace_ux080",
})

TMUX_MODULES = frozenset({
    "test_api_worker", "test_api_workspace_e2e", "test_client_runtime", "test_terminal_preview",
    "test_terminal_workspace_e2e", "test_workspace",
})


def pytest_configure(config):
    config.addinivalue_line("markers", "arcade: needs the real pinned ArcadeDB engine (Java 21)")
    config.addinivalue_line("markers", "tmux: drives a real tmux server")


def pytest_collection_modifyitems(config, items):
    for item in items:
        module = Path(str(item.fspath)).stem
        if module not in NO_ARCADE_MODULES:
            item.add_marker(pytest.mark.arcade)
        if module in TMUX_MODULES:
            item.add_marker(pytest.mark.tmux)


@pytest.fixture(scope="session")
def arcade_test_engine(tmp_path_factory):
    runtime_names = ("DEVBRAIN_ARCADE_URL", "DEVBRAIN_ARCADE_USER", "DEVBRAIN_ARCADE_PASSWORD",
                     "DEVBRAIN_ARCADE_DATABASE", "DEVBRAIN_ARCADE_DB_PREFIX")
    original = {name: os.environ.pop(name, None) for name in runtime_names}
    existing = os.environ.get("DEVBRAIN_TEST_ARCADE_HOME")
    owns = not existing
    root = Path(existing) if existing else tmp_path_factory.mktemp("arcade-engine")
    settings = Settings.load(root, create=True)
    if owns:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        settings.arcade_url = f"http://127.0.0.1:{port}"
        settings.save()
        distribution = os.environ.get("DEVBRAIN_ARCADE_DIST")
        install_engine(settings, Path(distribution) if distribution else None)
    start_engine(settings)
    prefix = "devbrain_test_" + uuid.uuid4().hex[:8]
    changes = {"DEVBRAIN_ARCADE_URL": settings.arcade_url,
               "DEVBRAIN_ARCADE_USER": settings.arcade_user,
               "DEVBRAIN_ARCADE_PASSWORD": settings.arcade_password,
               "DEVBRAIN_ARCADE_DB_PREFIX": prefix}
    os.environ.update(changes)
    try:
        yield settings, prefix
    finally:
        if owns:
            stop_engine(settings)
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.fixture(autouse=True)
def release_test_databases(request):
    """Start the engine for ``arcade`` tests and drop their databases afterwards."""
    if request.node.get_closest_marker("arcade") is None:
        yield
        return
    settings, prefix = request.getfixturevalue("arcade_test_engine")
    yield
    client = ArcadeClient(settings)
    try:
        names = client._request("GET", "/api/v1/databases").json().get("result", [])
        for name in names:
            if isinstance(name, str) and name.startswith(prefix + "_"):
                client.server_command(f"drop database {name}")
    except ArcadeError:
        pass
