"""Persistence tests use the real, pinned ArcadeDB Java engine.

Ollama and coding clients are mocked; storage/FTS/ANN/graph/transactions are not.
An existing isolated developer test engine can be reused with
CAIRNMIND_TEST_ARCADE_HOME. Each pytest process owns a unique database namespace.

Every test module is marked ``arcade`` and starts the engine, unless it is listed in
NO_ARCADE_MODULES. Add a module there only after it passes with no engine running.
Modules in TMUX_MODULES drive a real tmux server and are also marked ``tmux``.
Fast local run: ``uv run pytest -m "not arcade and not tmux"``.

Parallel run: ``uv run pytest -n 4`` (pytest-xdist). All workers share one engine: the first
worker that needs it starts it under a file lock in the run's temporary directory, each worker
keeps its own database namespace, and the controlling process stops the engine at the end.
An engine whose pytest process was killed is stopped by the next run (see reap_orphan_engines).
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import socket
import uuid

import pytest

from cairnmind.arcade import ArcadeClient, ArcadeError
from cairnmind.config import SESSION_NAMES, Settings, env_names, env_value
from cairnmind.engine import install_engine, start_engine, stop_engine

# Verified on 2026-11-09: each passes in full with no ArcadeDB engine available.
NO_ARCADE_MODULES = frozenset({
    "test_agent_journal", "test_agent_tools", "test_answer_contract", "test_api_broker",
    "test_api_credentials", "test_api_panels", "test_api_profiles", "test_api_providers",
    "test_api_runtime", "test_api_worker", "test_arcade_transport", "test_client_auth",
    "test_client_runtime", "test_cockpit_cli", "test_cockpit_server", "test_cockpit_state",
    "test_copilot_adapter", "test_environment_names", "test_graph_raster", "test_graph_widget_rendering", "test_memory_view", "test_module_alias",
    "test_native_evaluation", "test_native_session_environment", "test_ollama", "test_parsing",
    "test_runtime_pane", "test_runtime_selection", "test_sample_memory", "test_terminal_client", "test_terminal_commands",
    "test_terminal_graph", "test_terminal_latency080", "test_terminal_panels", "test_terminal_preview",
    "test_terminal_ux080", "test_usage", "test_workspace","test_workspace_cli", "test_workspace_locale",
    "test_workspace_state", "test_workspace_ux080",
})

TMUX_MODULES = frozenset({
    "test_api_worker", "test_api_workspace_e2e", "test_client_runtime", "test_terminal_preview",
    "test_terminal_workspace_e2e", "test_workspace",
})

# The directory, under the run's temporary root, of the engine that parallel workers share.
SHARED_ENGINE = "arcade-engine-shared"


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


def is_xdist_worker(config) -> bool:
    return hasattr(config, "workerinput")


def is_xdist_controller(config) -> bool:
    return not is_xdist_worker(config) and getattr(config.option, "numprocesses", None) not in (None, 0)


@contextlib.contextmanager
def file_lock(path: Path):
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _pytest_alive(pid: int) -> bool:
    try:
        return b"pytest" in Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False


def reap_orphan_engines(runs_root: Path) -> None:
    """Stop test engines whose owning pytest process is gone (killed, not torn down).

    Only engines this conftest started and recorded in owner.json are touched, and only when that
    process no longer runs pytest; stop_engine itself checks the process belongs to that directory.
    """
    for owner in runs_root.glob("pytest-*/arcade-engine*/owner.json"):
        try:
            pid = int(json.loads(owner.read_text())["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if _pytest_alive(pid) or not (owner.parent / "arcade" / "process.json").exists():
            continue
        with contextlib.suppress(Exception):
            stop_engine(Settings.load(owner.parent))


def record_owner(root: Path, pid: int) -> None:
    (root / "owner.json").write_text(json.dumps({"pid": pid}))


def prepare_engine(root: Path, owner_pid: int) -> Settings:
    """Configure, install and start an owned test engine in ``root`` (idempotent)."""
    settings = Settings.load(root, create=True)
    if not (root / "owner.json").exists():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        settings.arcade_url = f"http://127.0.0.1:{port}"
        settings.save()
        record_owner(root, owner_pid)
    distribution = env_value("ARCADE_DIST")
    install_engine(settings, Path(distribution) if distribution else None)
    start_test_engine(settings)
    return settings


def pytest_unconfigure(config):
    """With xdist the controlling process, not a worker, stops the engine the workers shared."""
    if not is_xdist_controller(config) or env_value("TEST_ARCADE_HOME"):
        return
    factory = getattr(config, "_tmp_path_factory", None)
    if factory is None:
        return
    root = factory.getbasetemp() / SHARED_ENGINE
    if (root / "arcade" / "process.json").exists():
        stop_engine(Settings.load(root))


@pytest.fixture(autouse=True)
def no_inherited_session():
    """No test starts inside the session, data directory or runtime choice of the shell that ran pytest.

    Done by hand and not with monkeypatch: requesting monkeypatch here would create it before
    every other fixture and so undo a test's patches only after those fixtures were torn down.
    """
    removed = {key: os.environ.pop(key) for name in (*SESSION_NAMES, "HOME", "JAVA_EXECUTABLE", "TMUX_EXECUTABLE")
               for key in env_names(name) if key in os.environ}
    yield
    os.environ.update(removed)


def start_test_engine(settings):
    """Start the engine, once more after a failure, so that one slow start does not fail the session.

    When it fails twice the whole run stops with the cause named once: a fixture that raises is raised
    again for every test that uses it, which would be a thousand identical setup errors.
    """
    for attempt in range(2):
        try:
            start_engine(settings)
            return
        except ArcadeError as exc:
            error = exc
    pytest.exit(f"The test engine did not start, twice: {error}", returncode=1)


@pytest.fixture(scope="session")
def arcade_test_engine(tmp_path_factory, request):
    # Both spellings: a developer's exported earlier name must not reach the test engine either.
    runtime_names = tuple(key for name in ("ARCADE_URL", "ARCADE_USER", "ARCADE_PASSWORD", "ARCADE_DATABASE",
                                           "ARCADE_DB_PREFIX") for key in env_names(name))
    original = {name: os.environ.pop(name, None) for name in runtime_names}
    existing = env_value("TEST_ARCADE_HOME")
    worker = is_xdist_worker(request.config)
    # A serial run owns and stops its engine; parallel workers share one that the controller stops.
    owns = not existing and not worker
    if existing:
        settings = Settings.load(Path(existing), create=True)
        start_test_engine(settings)
    else:
        run_root = tmp_path_factory.getbasetemp()
        if worker:
            # A worker's base directory is <run root>/popen-gwN; the run root is shared by all workers.
            run_root = run_root.parent
        reap_orphan_engines(run_root.parent)
        if worker:
            root = run_root / SHARED_ENGINE
            root.mkdir(exist_ok=True)
            with file_lock(run_root / (SHARED_ENGINE + ".lock")):
                settings = prepare_engine(root, os.getppid())
        else:
            settings = prepare_engine(tmp_path_factory.mktemp("arcade-engine"), os.getpid())
    prefix = "devbrain_test_" + uuid.uuid4().hex[:8]
    changes = {"CAIRNMIND_ARCADE_URL": settings.arcade_url,
               "CAIRNMIND_ARCADE_USER": settings.arcade_user,
               "CAIRNMIND_ARCADE_PASSWORD": settings.arcade_password,
               "CAIRNMIND_ARCADE_DB_PREFIX": prefix}
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
