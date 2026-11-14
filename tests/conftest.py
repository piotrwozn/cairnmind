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

A new test database starts as a file copy of an empty one whose types and indexes the real
Store.init built once per process (see SchemaTemplate); Store.init then runs in full on the copy.
CAIRNMIND_TEST_SCHEMA_TEMPLATE=0 builds every database from nothing instead.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
from pathlib import Path
import shutil
import socket
import sys
import threading
from urllib.parse import quote
import uuid

import pytest

from cairnmind.arcade import ArcadeClient, ArcadeError
from cairnmind.config import SESSION_NAMES, Settings, env_names, env_value
from cairnmind.engine import install_engine, start_engine, stop_engine

# Verified on 2026-11-09: each passes in full with no ArcadeDB engine available.
NO_ARCADE_MODULES = frozenset({
    "test_agent_journal", "test_agent_tools", "test_answer_contract", "test_api_broker",
    "test_api_credentials", "test_api_panels", "test_api_profiles", "test_api_providers",
    "test_api_runtime", "test_api_worker", "test_arcade_transport", "test_client_auth", "test_repository_evaluation",
    "test_client_runtime", "test_cockpit_cli", "test_cockpit_server", "test_cockpit_state",
    "test_copilot_adapter", "test_environment_names", "test_graph_raster", "test_graph_widget_rendering", "test_memory_view", "test_module_alias",
    "test_native_evaluation", "test_native_session_environment", "test_ollama", "test_parallel_isolation", "test_parsing", "test_readonly_cli",
    "test_runtime_pane", "test_runtime_selection", "test_sample_memory", "test_terminal_client", "test_terminal_commands",
    "test_terminal_graph", "test_terminal_latency080", "test_terminal_panels", "test_terminal_preview",
    "test_terminal_ux080", "test_usage", "test_workspace","test_workspace_cli", "test_workspace_locale",
    "test_workspace_state", "test_workspace_ux080", "test_workspace_focus_column",
})

TMUX_MODULES = frozenset({
    "test_api_worker", "test_api_workspace_e2e", "test_client_runtime", "test_terminal_preview",
    "test_terminal_workspace_e2e", "test_workspace", "test_workspace_focus_column",
})

# Modules whose tests measure time against a budget. Under xdist each of their tests runs while no
# other test of the run does (see run_slot); a serial run is unchanged.
EXCLUSIVE_MODULES = frozenset({"test_terminal_latency080"})

# The directory, under the run's temporary root, of the engine that parallel workers share.
SHARED_ENGINE = "arcade-engine-shared"


def measuring_coverage() -> bool:
    """Whether coverage is measuring this process (the non-required `coverage` CI job, or `--cov`)."""
    coverage = sys.modules.get("coverage")
    return bool(coverage and coverage.Coverage.current())


def time_budget(seconds: float) -> float:
    """A time limit for work that is bound by this process's own CPU.

    Coverage's tracer slows pure-Python loops by about half, so under coverage the limit is
    tripled; without coverage, as in the required CI job, it is exactly `seconds`. Do not use it
    for a check that work did not wait for a timeout: a larger limit could hide the wait.
    """
    return seconds * 3 if measuring_coverage() else seconds
  
  
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_BOX = re.compile("[\u2500-\u257f]")


def plain_words(text: str) -> str:
    """The words of CLI output, for comparing a message that Typer or Rich may have drawn in a box.

    Colour codes, box-drawing characters and line breaks are dropped and runs of white space become
    one space, so a message wrapped at 60 columns or split across a border still matches. Nothing
    else is changed: the words, digits and punctuation of the message must still be there.
    """
    return " ".join(_BOX.sub(" ", _ANSI.sub("", text)).split())


@pytest.hookimpl(trylast=True)  # after the tmp_path plugin has set up the base directory
def pytest_configure(config):
    global MCP_FIXTURE
    config.addinivalue_line("markers", "arcade: needs the real pinned ArcadeDB engine (Java 21)")
    config.addinivalue_line("markers", "tmux: drives a real tmux server")
    config.addinivalue_line("markers", "no_schema_template: every database of the test is built from nothing")
    config.addinivalue_line("markers", "exclusive: under xdist, runs while no other test of the run runs")
    # A worker's base directory is its own (<run root>/popen-gwN); a serial run's is the run root.
    copy = config._tmp_path_factory.getbasetemp() / "mcp-fixture" / MCP_FIXTURE_SOURCE.name
    copy.parent.mkdir(exist_ok=True)
    shutil.copyfile(MCP_FIXTURE_SOURCE, copy)
    MCP_FIXTURE = str(copy)


def pytest_collection_modifyitems(config, items):
    for item in items:
        module = Path(str(item.fspath)).stem
        if module not in NO_ARCADE_MODULES:
            item.add_marker(pytest.mark.arcade)
        if module in TMUX_MODULES:
            item.add_marker(pytest.mark.tmux)
        if module in EXCLUSIVE_MODULES:
            item.add_marker(pytest.mark.exclusive)


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


# True while this xdist worker's current test holds its slot (set and cleared by run_slot only).
_slot_held = False


def require_slot(config, work: str) -> None:
    """Fail when engine work of a test starts outside its slot under xdist; outside xdist there is none."""
    if is_xdist_worker(config) and not _slot_held:
        raise AssertionError(f"{work} outside the test's slot: the fixture doing it must request run_slot")


@pytest.fixture(autouse=True)
def run_slot(request, tmp_path_factory):
    """Under xdist, an ``exclusive`` test waits until the tests running in other workers end, and
    no other test starts until it ends; every other test holds a shared slot.

    Two locks in the run's shared directory: a new test first passes the gate, which an exclusive
    test holds while it waits, so that a stream of new shared slots cannot keep it waiting forever.
    Being autouse does not put it first: pytest orders a conftest's autouse fixtures by name. Every
    fixture that does engine work therefore requests it, which sets the slot up before that work
    and tears it down after it, and calls require_slot, which fails if the slot is not held.
    """
    global _slot_held
    if not is_xdist_worker(request.config):
        yield
        return
    run_root = tmp_path_factory.getbasetemp().parent
    exclusive = request.node.get_closest_marker("exclusive") is not None
    with (run_root / "test-slot.gate").open("a") as gate, (run_root / "test-slot.lock").open("a") as lock:
        fcntl.flock(gate, fcntl.LOCK_EX)
        if exclusive:
            fcntl.flock(lock, fcntl.LOCK_EX)
        else:
            fcntl.flock(lock, fcntl.LOCK_SH)
            fcntl.flock(gate, fcntl.LOCK_UN)
        _slot_held = True
        try:
            yield
        finally:
            _slot_held = False
            fcntl.flock(lock, fcntl.LOCK_UN)
            fcntl.flock(gate, fcntl.LOCK_UN)


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


MCP_FIXTURE_SOURCE = Path(__file__).parent / "fixtures" / "mcp_fixture_server.py"
# The path every test starts the fixture server by. pytest_configure replaces it with this pytest
# process's own copy, so that the servers and children of one xdist worker can be told from those of
# another by their command line: mcp_fixture_processes, and the leak check that uses it, see only
# their own process's servers, never those another worker has running at the same moment.
MCP_FIXTURE = str(MCP_FIXTURE_SOURCE)
MCP_MODULES = frozenset({"test_mcp_host", "test_mcp_agent", "test_mcp_sources", "test_mcp_servers",
                         "test_workspace_mcp", "test_mcp_review_host", "test_mcp_review_agent",
                         "test_mcp_round2"})


def mcp_fixture_processes() -> list[int]:
    """Live processes of this checkout's fixture MCP server, and the children it marks with its path."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            arguments = (entry / "cmdline").read_bytes().split(b"\0")
            state = (entry / "stat").read_text().rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            continue
        if state != "Z" and any(MCP_FIXTURE.encode() in argument for argument in arguments):
            found.append(int(entry.name))
    return found


@pytest.fixture(autouse=True)
def no_mcp_server_outlives_its_test(request):
    """In the MCP modules a fixture server, or a child in its group, still alive after a test fails the run.

    It is killed first, so one leak cannot hide the next.
    """
    module = Path(str(request.node.fspath)).stem
    yield
    if module not in MCP_MODULES or not Path("/proc").is_dir():
        return
    import signal
    import time
    deadline = time.monotonic() + 2
    left = mcp_fixture_processes()
    while left and time.monotonic() < deadline:
        time.sleep(.05)
        left = mcp_fixture_processes()
    for pid in left:
        for kill in (lambda: os.killpg(pid, signal.SIGKILL), lambda: os.kill(pid, signal.SIGKILL)):
            try:
                kill()
            except OSError:
                pass
    if left:
        pytest.fail(f"A fixture MCP server outlived its test and was killed: {left}")


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


class SchemaTemplate:
    """Give each new test database the schema by copying files instead of some 200 DDL statements.

    Store.init sends about 200 schema statements to a new database, each saved to disk by the engine:
    some 3 s for every Store a test opens, three quarters of the suite's time. Once per process the
    real Store.init builds a database; its two rows (devbrain_meta, journal_state) are deleted, so
    only types and indexes remain, and the closed database is kept as a template. When a Store of
    this process asks for a database that does not exist, the template's files are copied under the
    new name and opened. Store.init then runs exactly as on a new database: the same statements
    (each "IF NOT EXISTS" now finds its type or index), and it writes the schema version, the index
    set and a new journal epoch itself. Built by the code under test in every run, the template
    cannot fall behind a change to the schema. Only databases of this process's prefix are copied.
    """

    def __init__(self, settings: Settings, prefix: str, scratch: Path):
        self.databases = settings.data_dir / "arcade" / "databases"
        self.prefix = prefix
        self.name = None
        self.paused = False  # set for a test marked no_schema_template
        self.lock = threading.Lock()
        if env_value("TEST_SCHEMA_TEMPLATE") == "0" or not self.databases.is_dir():
            return
        from cairnmind.store import Store
        template_prefix = "devbrain_tpl_" + uuid.uuid4().hex[:8]
        os.environ["CAIRNMIND_ARCADE_DB_PREFIX"] = template_prefix
        try:
            store = Store(scratch)
        finally:
            os.environ["CAIRNMIND_ARCADE_DB_PREFIX"] = prefix
        client = store.client
        client.command("DELETE FROM devbrain_meta")
        client.command("DELETE FROM journal_state")
        types = [row["name"] for row in client.query("SELECT name FROM schema:types")]
        rows = sum(client.query(f"SELECT count(*) AS n FROM `{name}`")[0]["n"] for name in types)
        client.server_command(f"close database {client.database}")
        if rows:  # Store.init wrote more than the two rows above: build every database from nothing.
            shutil.rmtree(self.databases / client.database, ignore_errors=True)
            return
        self.name = client.database

    def ensure_database(self, original):
        template = self

        def ensure_database(client):
            name = client.database
            if template.name is None or template.paused or not name.startswith(template.prefix + "_"):
                return original(client)
            with template.lock:
                if client._request("GET", f"/api/v1/exists/{quote(name)}").json().get("result") is True:
                    return
                staging = template.databases / f".copy-{name}"
                shutil.copytree(template.databases / template.name, staging)
                staging.rename(template.databases / name)
                try:
                    client.server_command(f"open database {name}")
                except ArcadeError:
                    shutil.rmtree(template.databases / name, ignore_errors=True)
                    return original(client)
        return ensure_database

    def remove(self):
        if self.name:
            shutil.rmtree(self.databases / self.name, ignore_errors=True)


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
    original_ensure = ArcadeClient.ensure_database
    template = None
    try:
        # Built inside the try: a schema that fails to initialise must still stop the engine and restore the environment.
        template = SchemaTemplate(settings, prefix, tmp_path_factory.mktemp("schema-template"))
        ArcadeClient.ensure_database = template.ensure_database(original_ensure)
        yield settings, prefix, template
    finally:
        ArcadeClient.ensure_database = original_ensure
        if template is not None:
            template.remove()
        for name in changes:
            os.environ.pop(name, None)
        if owns:
            stop_engine(settings)
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.fixture(autouse=True)
def release_test_databases(request, run_slot):
    """Start the engine for ``arcade`` tests and drop their databases afterwards, inside the slot."""
    if request.node.get_closest_marker("arcade") is None:
        yield
        return
    require_slot(request.config, "Starting the engine")
    settings, prefix, template = request.getfixturevalue("arcade_test_engine")
    template.paused = request.node.get_closest_marker("no_schema_template") is not None
    try:
        yield
    finally:
        template.paused = False
    require_slot(request.config, "Dropping the test's databases")
    client = ArcadeClient(settings)
    try:
        names = client._request("GET", "/api/v1/databases").json().get("result", [])
        for name in names:
            if isinstance(name, str) and name.startswith(prefix + "_"):
                client.server_command(f"drop database {name}")
    except ArcadeError:
        pass
