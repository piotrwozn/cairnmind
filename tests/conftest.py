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
import subprocess
import sys
import tempfile
import termios
import threading
import time
from urllib.parse import quote, urlparse
import uuid
import zlib

import pytest

from cairnmind import process_identity
from cairnmind.arcade import ArcadeClient, ArcadeError
from cairnmind.config import SESSION_NAMES, Settings, env_names, env_value
from cairnmind.engine import install_engine, start_engine, stop_engine
import stall_watch

# A time limit for every test, and a stop for what a test left running (#236).
pytest_plugins = ("run_guard",)

# Verified on 2026-11-09: each passes in full with no ArcadeDB engine available.
NO_ARCADE_MODULES = frozenset({
    "test_agent_journal", "test_agent_tools", "test_answer_contract", "test_api_broker",
    "test_api_credentials", "test_api_panels", "test_api_profiles", "test_api_providers",
    "test_api_runtime", "test_api_worker", "test_arcade_transport", "test_client_auth", "test_repository_evaluation",
    "test_client_runtime", "test_cockpit_cli", "test_cockpit_server", "test_cockpit_state",
    "test_copilot_adapter", "test_database_credentials", "test_deployment", "test_environment_names", "test_graph_raster", "test_graph_widget_rendering", "test_memory_models", "test_memory_view", "test_module_alias",
    "test_project_remove_cli", "test_hook_spool_unassigned", "test_published_memory_format",
    "test_published_memory_secrets",
    "test_published_memory_diff", "test_setup_profiles",
    "test_native_evaluation", "test_native_session_environment", "test_ollama", "test_parallel_isolation", "test_parsing", "test_readonly_cli",
    "test_runtime_pane", "test_runtime_selection", "test_sample_memory", "test_terminal_client", "test_terminal_commands",
    "test_terminal_graph", "test_terminal_latency080", "test_terminal_panels", "test_terminal_preview",
    "test_terminal_ux080", "test_topology", "test_usage", "test_workspace","test_workspace_cli", "test_workspace_locale",
    "test_workspace_state", "test_workspace_ux080", "test_workspace_focus_column", "test_shards",
    "test_run_guard", "test_process_identity", "test_machine_identity", "test_socket_base", "test_python_pin",
    "test_ci_macos_job",
    "test_system_links", "test_legacy_groups_workspace",
    "test_install_macos",
    "test_first_start_round2", "test_first_start_round3", "test_engine_docker", "test_tmux_optional", "test_external_mcp_start",
    "test_knowledge_mode", "test_person_proof", "test_person_key_workspace", "test_inspector_plan_confirm",
    "test_inspector_release", "test_inspector_replace", "test_inspector_published_import",
    "test_command_line_confirms_nothing",
    "test_doc_links", "test_stall_watch", "test_restore_claims_units",
    "test_measure_service_spend", "test_compare_model_paths_script",
    "test_upgrade_mark", "test_upgrade_start", "test_service_version", "test_other_version",
    "test_sessions_dialog", "test_command_menu_popup", "test_mcp_unknown_argument", "test_stop_broker_line",
    "test_ended_pane_text",
    "test_short_answers", "test_person_acts_consent", "test_project_forget_workspace",
    "test_server_mode",
})

TMUX_MODULES = frozenset({
    "test_api_worker", "test_api_workspace_e2e", "test_client_runtime", "test_terminal_preview", "test_ended_pane_text",
    "test_terminal_workspace_e2e", "test_workspace", "test_workspace_focus_column",
})

# Modules whose tests measure time against a budget. Under xdist each of their tests runs while no
# other test of the run does (see run_slot); a serial run is unchanged.
EXCLUSIVE_MODULES = frozenset({"test_terminal_latency080"})

# The directory, under the run's temporary root, of the engine that parallel workers share.
SHARED_ENGINE = "arcade-engine-shared"

# Server mode (#218): with CAIRNMIND_TEST_ARCADE_SERVER_URL (http://127.0.0.1:PORT of a running ArcadeDB) and
# CAIRNMIND_TEST_ARCADE_SERVER_PASSWORD_FILE (a private file with its root password), the engine tests install, start
# and stop nothing and run against that server through connect-only mode; the `server-tests` CI job does it with the
# published image. Without them nothing here changes. Tests that cannot run against a server this run does not own are
# skipped with the reason below: a module (`test_x`) or one test (`test_x::test_name`). An entry is added only after a
# run showed that test failing, with the reason from that failure. tests/test_server_mode.py keeps the list true.
LEFT_OUT_ON_A_SERVER = {
    "test_database_user::test_a_remote_url_a_dedicated_user_and_a_password_file_index_and_read":
        "sets its own database address, which conflicts with the connect address of server mode",
    "test_platform_hints::test_a_missing_client_and_a_missing_java_ask_for_the_system_they_run_on":
        "starts the engine of a managed installation to see its Java hint, which a connect-only one never starts",
    "test_store_readonly::test_a_stopped_engine_is_refused_without_starting_it":
        "names its own database address, which conflicts with the connect address of server mode",
    "test_store_readonly::test_a_missing_database_is_refused_and_not_created":
        "expects the message of a managed installation for a missing database, which a connect-only one words otherwise",
    "test_evaluation":
        "its evaluation runs create databases of their own name, which a connect-only installation never creates",
    "test_memory_evaluation":
        "its evaluation runs create databases of their own name, which a connect-only installation never creates",
    "test_injection_evaluation":
        "its evaluation runs create and drop databases of their own name, which a connect-only installation never does",
    "test_arcade::test_a_request_the_test_engine_does_not_answer_says_what_it_waited_for":
        "expects the unavailable message of a managed engine, which a connect-only installation words otherwise",
    "test_integrations::test_doctor_reports_arcadedb_without_lifecycle_work_or_credentials":
        "expects the Java report of a managed installation, which a connect-only one answers with no engine needed",
    "test_lookup_indexes::test_a_new_database_has_every_lookup_index":
        "asks the product's own path to create a new database, which a connect-only installation never does",
    "test_cli::test_cli_evaluation_skip_claude_is_explicit_and_remains_not_run":
        "`cairnmind eval run` is refused on a connect-only installation",
    "test_memory_cli_service::test_memory_evaluation_cli_preserves_exit_contract":
        "`cairnmind eval memory` is refused on a connect-only installation",
}


def server_under_test():
    """(address, root password) of the server of server mode, or None when the tests start their own engine."""
    address, password_file = env_value("TEST_ARCADE_SERVER_URL"), env_value("TEST_ARCADE_SERVER_PASSWORD_FILE")
    if not address and not password_file:
        return None
    if not (address and password_file):
        raise pytest.UsageError("Server mode needs both CAIRNMIND_TEST_ARCADE_SERVER_URL and "
                                "CAIRNMIND_TEST_ARCADE_SERVER_PASSWORD_FILE.")
    if urlparse(address).port in default_ports():  # the identity of a server fails closed: never the installed one
        raise pytest.UsageError(f"CAIRNMIND_TEST_ARCADE_SERVER_URL names a default port of Cairnmind ({address}).")
    from cairnmind.database_credentials import read_private_file
    try:
        return address, read_private_file(password_file, "CAIRNMIND_TEST_ARCADE_SERVER_PASSWORD_FILE")
    except ValueError as problem:  # a usage error, not an INTERNALERROR traceback from the collection hook
        raise pytest.UsageError(str(problem)) from None


def default_ports() -> set:
    """The default service, cockpit, engine and Ollama ports: those of the Cairnmind installed on a machine (#221)."""
    defaults = Settings(data_dir=Path("."))
    return {defaults.port, defaults.cockpit_port, urlparse(defaults.arcade_url).port, urlparse(defaults.ollama_url).port}


def left_out_reason(module: str, name: str) -> str | None:
    return LEFT_OUT_ON_A_SERVER.get(module) or LEFT_OUT_ON_A_SERVER.get(f"{module}::{name}")


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
  
  
def terminal_attributes(fd: int) -> list:
    """termios.tcgetattr for comparing a terminal before and after: without PENDIN on macOS, whose kernel sets
    that flag itself when a terminal goes back to canonical mode, so a correct restore still differs there."""
    attributes = termios.tcgetattr(fd)
    if sys.platform == "darwin":
        attributes[3] &= ~termios.PENDIN
    return attributes


# FWASWRITTEN in the <sys/fcntl.h> of macOS: its kernel marks a descriptor once it was written to, shows the mark in
# F_GETFL, and F_SETFL cannot clear it.
DARWIN_WRITTEN = 0x10000


def status_flags(fd: int) -> int:
    """fcntl F_GETFL for comparing a descriptor before and after: without the written mark on macOS (#236)."""
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    return flags & ~DARWIN_WRITTEN if sys.platform == "darwin" else flags


@pytest.fixture
def short_tmp_path():
    """A short private folder of its own, for a path that has a length limit (#236).

    tmp_path on macOS is under /private/var/folders/..., longer than a socket path may be and than some messages
    show a path in full. Resolved, so a path the product resolves (/tmp is /private/tmp there) compares equal.
    """
    path = Path(tempfile.mkdtemp(prefix="cm-", dir="/tmp")).resolve()
    yield path
    shutil.rmtree(path, ignore_errors=True)


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_BOX = re.compile("[\u2500-\u257f]")


def plain_words(text: str) -> str:
    """The words of CLI output, for comparing a message that Typer or Rich may have drawn in a box.

    Colour codes, box-drawing characters and line breaks are dropped and runs of white space become
    one space, so a message wrapped at 60 columns or split across a border still matches. Nothing
    else is changed: the words, digits and punctuation of the message must still be there.
    """
    return " ".join(_BOX.sub(" ", _ANSI.sub("", text)).split())


@contextlib.contextmanager
def person_proven():
    """The test-only door of a person's act (#214): the store counts an open act as the person's while it is set.

    In production the only thing that sets `person_proof._PROVEN` is `person_proof.require`, after the open
    workspace's key was checked. A test sets it itself around a store it built in its own process; a program
    that can import Cairnmind and do the same can equally write the database, which the document names. Tests
    of a route do not use it: they send the proof (`workspace_person`) and the route sets it.
    """
    from cairnmind import person_proof
    token = person_proof._PROVEN.set(True)
    try:
        yield
    finally:
        try:
            person_proof._PROVEN.reset(token)
        except ValueError:  # the token was made in another context (an async test): set the default again
            person_proof._PROVEN.set(False)


@contextlib.contextmanager
def person_unproven():
    """A token holder's request inside a test that is otherwise a person's: the door shut for the block."""
    from cairnmind import person_proof
    token = person_proof._PROVEN.set(False)
    try:
        yield
    finally:
        person_proof._PROVEN.reset(token)


@pytest.fixture(autouse=True)
def person_door(request):
    """Every test is a person's own process unless it is marked `unproven`: the tests of the store's gate."""
    if request.node.get_closest_marker("unproven"):
        yield
        return
    with person_proven():
        yield


TEST_KEY = "ab" * 32
TEST_SOCKET = "/tmp/cairnmind-ui-test/tmux.sock"


@pytest.fixture
def workspace_person(monkeypatch):
    """The open workspace of a test: the proof its panels send, with the tmux check answered as it would be.

    Returns the two headers for a request to a route. Without them the same request is a token holder's.
    """
    from cairnmind import person_proof
    monkeypatch.setattr(person_proof, "_tmux", lambda settings: "tmux")
    monkeypatch.setattr(person_proof, "read_key", lambda *args, **kwargs: ("key", TEST_KEY))
    monkeypatch.setattr(person_proof, "workspace_socket",
                        lambda settings, value: Path(value) if value == TEST_SOCKET else None)
    return {person_proof.KEY_HEADER: TEST_KEY, person_proof.SOCKET_HEADER: TEST_SOCKET}


def as_person(store, project_id, record, sources=None):
    """Store `record` as confirmed under a person's act, in a test process that stands for the person (#177).

    The command line no longer does this (#214). A test that needs a confirmed record and is not about how it
    was confirmed uses this instead of asking the store for the status, which only a person's route may do.
    """
    from cairnmind.confirmation import remember_as_person
    with person_proven():
        return remember_as_person(store, project_id, {"status": "confirmed", **record}, sources)


def person_edit(store, project_id, record, sources):
    """Write `record` as a person's own act that confirms nothing (#177).

    Once a person confirmed a record, only a person changes or replaces it. This helper stands for that
    person (the act a person's route opens). A changed record is stored as proposed, without a lapsed mark,
    until the person confirms it again.
    """
    from cairnmind.confirmation import person_source
    with person_proven(), store.transaction():
        note = person_source(store, project_id, "edit", action="person_edit")
        with store.person_act("cli", note["id"]):
            return store.remember(project_id, record, sources)


def confirmed_before_rule(store, record_id):
    """Turn a stored record into one that was confirmed before only a person could confirm (#177).

    Writes the rows directly, as an old store holds them: status confirmed and no confirmation mark.
    """
    from cairnmind.sql import clean_row, update_document
    with store.transaction() as tx:
        row = clean_row(tx.query("SELECT FROM records WHERE id=:id", {"id": record_id})[0])
        row["status"] = "confirmed"
        row["metadata"] = {key: value for key, value in row["metadata"].items() if key != "confirmation"}
        update_document(tx, "records", {"status": "confirmed", "metadata": row["metadata"]},
                        "id=:id", {"id": record_id})
        update_document(tx, "record_revisions", {"snapshot": row}, "id=:id",
                        {"id": f"{record_id}:{row['version']}"})
    return row


@pytest.fixture
def person_terminal(monkeypatch):
    """The command line as a person runs it: a terminal on both ends and no coding session (#177).

    CliRunner has no terminal, so without this fixture a CLI test is what an agent's command is.
    """
    import cairnmind.confirmation as confirmation
    monkeypatch.setattr(confirmation, "person_at_terminal", lambda *args, **kwargs: True)


@pytest.hookimpl(trylast=True)  # after the tmp_path plugin has set up the base directory
def pytest_configure(config):
    global MCP_FIXTURE
    config.addinivalue_line("markers", "arcade: needs the real pinned ArcadeDB engine (Java 21)")
    config.addinivalue_line("markers", "tmux: drives a real tmux server")
    config.addinivalue_line("markers", "unproven: a person's act is not proven, as a token holder's request is (#214)")
    config.addinivalue_line("markers", "no_schema_template: every database of the test is built from nothing")
    config.addinivalue_line("markers", "exclusive: under xdist, runs while no other test of the run runs")
    # A worker's base directory is its own (<run root>/popen-gwN); a serial run's is the run root.
    copy = config._tmp_path_factory.getbasetemp() / "mcp-fixture" / MCP_FIXTURE_SOURCE.name
    copy.parent.mkdir(exist_ok=True)
    shutil.copyfile(MCP_FIXTURE_SOURCE, copy)
    MCP_FIXTURE = str(copy)


def pytest_addoption(parser):
    parser.addoption("--shard", default="", metavar="I/N",
                     help="Run only part I of N of the suite. The N parts are disjoint and together are the whole suite.")


def shard_of(value: str) -> tuple[int, int]:
    """`I/N` as (I, N); an empty value is the whole suite, (1, 1)."""
    if not value:
        return 1, 1
    index, _, count = value.partition("/")
    if not (index.isdecimal() and count.isdecimal() and 1 <= int(index) <= int(count) <= 64):
        raise pytest.UsageError("--shard takes I/N with 1 <= I <= N <= 64, for example 2/4")
    return int(index), int(count)


def in_shard(nodeid: str, index: int, count: int) -> bool:
    """Whether a test belongs to part `index` of `count`: decided by its id alone, so every process of a
    run (the xdist controller and each worker) and every job of a CI run agree without talking."""
    return zlib.crc32(nodeid.encode()) % count == index - 1


def pytest_collection_modifyitems(config, items):
    server = server_under_test()
    for item in items:
        module = Path(str(item.fspath)).stem
        reason = server and left_out_reason(module, getattr(item, "originalname", item.name))
        if reason:
            item.add_marker(pytest.mark.skip(reason=f"left out on a server: {reason}"))
        if module not in NO_ARCADE_MODULES:
            item.add_marker(pytest.mark.arcade)
        if module in TMUX_MODULES:
            item.add_marker(pytest.mark.tmux)
        if module in EXCLUSIVE_MODULES:
            item.add_marker(pytest.mark.exclusive)
    index, count = shard_of(config.getoption("--shard"))
    if count > 1:
        dropped = [item for item in items if not in_shard(item.nodeid, index, count)]
        if dropped:
            items[:] = [item for item in items if in_shard(item.nodeid, index, count)]
            config.hook.pytest_deselected(items=dropped)


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
    argv = process_identity.command_line(pid)
    # An owner whose command line cannot be read keeps its engine while it lives: never reap a live run.
    return process_identity.alive(pid) if argv is None else any("pytest" in argument for argument in argv)


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
    import run_guard  # loaded as a plugin already (pytest_plugins); imported here so pytest can rewrite it
    if not is_xdist_worker(request.config):
        yield
        return
    run_root = tmp_path_factory.getbasetemp().parent
    exclusive = request.node.get_closest_marker("exclusive") is not None
    with (run_root / "test-slot.gate").open("a") as gate, (run_root / "test-slot.lock").open("a") as lock:
        # Bounded (#236): a test that hangs elsewhere must not keep this one, and every one behind it, waiting.
        with run_guard.waiting(run_guard.slot_wait(), "waiting for the test slot"):
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
    # The engine looks for Java in JAVA_HOME first (#232); a test chooses its Java itself.
    if "JAVA_HOME" in os.environ:
        removed["JAVA_HOME"] = os.environ.pop("JAVA_HOME")
    yield
    os.environ.update(removed)


@pytest.fixture(autouse=True)
def no_upgrade_mark(tmp_path_factory):
    """No test sees an upgrade the person who runs the tests may have in progress (#298); a test that wants a mark
    points mark_path elsewhere. By hand, not monkeypatch, for the reason given at no_inherited_session."""
    from cairnmind import upgrade_mark
    original = upgrade_mark.mark_path
    missing = tmp_path_factory.getbasetemp() / "no-upgrade-mark" / "upgrade-in-progress"
    upgrade_mark.mark_path = lambda: missing
    yield
    upgrade_mark.mark_path = original


@pytest.fixture(autouse=True)
def models_found_at_init():
    """`cairnmind init` asks the model server whether it has the models (#274). No test asks a real one: here the
    models are found, as every installation before 0.13.0 counted them, unless a test installs another answer.

    Done by hand and not with monkeypatch, for the reason given at no_inherited_session.
    """
    from cairnmind import knowledge_mode
    original = knowledge_mode.missing_models
    knowledge_mode.missing_models = lambda settings: []
    yield
    knowledge_mode.missing_models = original


@pytest.fixture(autouse=True)
def no_foreign_service():
    """No test in this process reaches whatever listens on the default service, cockpit, engine or Ollama port (#221).

    A test whose data directory names no port of its own would otherwise ask the Cairnmind installed on the
    machine that runs the tests (its own token is refused: "Invalid local service token") or its Ollama. Such a
    connection now fails as it does when nothing listens there. By hand, not monkeypatch, for the reason above.
    """
    # The engine's default port too: a command that reads the store before the service starts met the engine of
    # the Cairnmind installed on the machine and its refusal of a foreign password (#313).
    foreign = default_ports()
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex

    def refused(original):
        def guarded(self, address):
            if isinstance(address, tuple) and len(address) > 1 and address[1] in foreign:
                if original is connect_ex:
                    return 111  # ECONNREFUSED
                raise ConnectionRefusedError(111, f"the test never connects to the default port {address[1]}")
            return original(self, address)
        return guarded

    socket.socket.connect, socket.socket.connect_ex = refused(connect), refused(connect_ex)
    yield
    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex


MCP_FIXTURE_SOURCE = Path(__file__).parent / "fixtures" / "mcp_fixture_server.py"
# The path every test starts the fixture server by. pytest_configure replaces it with this pytest
# process's own copy, so that the servers and children of one xdist worker can be told from those of
# another by their command line: mcp_fixture_processes, and the leak check that uses it, see only
# their own process's servers, never those another worker has running at the same moment.
MCP_FIXTURE = str(MCP_FIXTURE_SOURCE)
MCP_MODULES = frozenset({"test_mcp_host", "test_mcp_agent", "test_mcp_sources", "test_mcp_servers",
                         "test_workspace_mcp", "test_mcp_review_host", "test_mcp_review_agent",
                         "test_mcp_round2"})


def process_ids() -> list[int]:
    """Every process id of the machine: procfs on Linux, ps(1) where there is none (macOS)."""
    if sys.platform.startswith("linux"):
        return [int(name) for name in os.listdir("/proc") if name.isdigit()]
    listed = subprocess.run(["ps", "-A", "-o", "pid="], capture_output=True, text=True, check=True, timeout=30)
    return [int(pid) for pid in listed.stdout.split()]


def mcp_fixture_processes() -> list[int]:
    """Live processes of this checkout's fixture MCP server, and the children it marks with its path."""
    found = []
    for pid in process_ids():
        if pid == os.getpid() or process_identity.start(pid, running=True) is None:
            continue  # gone, a zombie, or unreadable
        if any(MCP_FIXTURE in argument for argument in process_identity.command_line(pid) or []):
            found.append(pid)
    return found


@pytest.fixture(autouse=True)
def no_mcp_server_outlives_its_test(request):
    """In the MCP modules a fixture server, or a child in its group, still alive after a test fails the run.

    It is killed first, so one leak cannot hide the next.
    """
    module = Path(str(request.node.fspath)).stem
    yield
    if module not in MCP_MODULES:
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


def created_as_root(original, prefix: str, template):
    """Server mode: a connect-only installation creates no database (#68), so the run's own are created here as root.

    Not for a test marked no_schema_template: it asks for the product's own path, which refuses to create one."""
    def ensure_database(client):
        if template.paused or not client.database.startswith(prefix + "_"):
            return original(client)
        if not client.database_exists():
            try:
                client.server_command(f"create database {client.database}")
            except ArcadeError:  # as the product's ensure_database: a client that created it at the same time won
                if not client.database_exists():
                    raise
    return ensure_database


def explained_when_unavailable(settings: Settings, original):
    """A request the test engine does not answer says, in the failing test's captured stderr, what it waited for (#221).

    One CREATE PROPERTY once got no answer in 30 s and nothing in the report showed whether the engine was busy,
    stuck or gone. Costs nothing unless a request fails with "unavailable" after more than 5 s (a refused
    connection is instant and says nothing here); the error itself is raised unchanged. A request still pending
    after 20 s is recorded by stall_watch while it waits, which includes the thread dump.
    """
    original = stall_watch.watch(settings, original)

    def request(client, method, path, **kwargs):
        started = time.monotonic()
        try:
            return original(client, method, path, **kwargs)
        except ArcadeError as exc:
            if "unavailable" in str(exc) and time.monotonic() - started > 5:  # a stall: not a refused connection
                waited = time.monotonic() - started
                try:  # a sick engine may answer badly too: nothing here may replace the error being raised
                    statement = str((kwargs.get("json") or {}).get("command", ""))[:200]
                    try:
                        open_databases = len(original(client, "GET", "/api/v1/databases", timeout=2).json().get("result", []))
                    except Exception as probe:
                        open_databases = f"no usable answer to the next request either ({probe!r})"
                    log = settings.data_dir / "arcade" / "engine.log"
                    tail = log.read_text(errors="replace").splitlines()[-15:] if log.exists() else []
                    print(f"ENGINE UNAVAILABLE after {waited:.1f}s: {method} {path} {statement!r}; "
                          f"load average {os.getloadavg()}, databases of the engine: {open_databases}, "
                          f"engine.log ends:\n" + "\n".join(tail), file=sys.stderr)
                    if waited < stall_watch.STALL_AFTER:  # later, the watch has printed the threads, while it waited
                        print("engine threads:\n" + stall_watch.engine_threads(log), file=sys.stderr)
                except Exception as problem:
                    print(f"ENGINE UNAVAILABLE after {waited:.1f}s: {method} {path}; the report failed: {problem!r}",
                          file=sys.stderr)
            raise
    return request


@pytest.fixture(scope="session")
def arcade_test_engine(tmp_path_factory, request):
    # Both spellings: a developer's exported earlier name must not reach the test engine either.
    runtime_names = tuple(key for name in ("ARCADE_URL", "ARCADE_USER", "ARCADE_PASSWORD", "ARCADE_DATABASE",
                                           "ARCADE_DB_PREFIX") for key in env_names(name))
    server = server_under_test()
    if server:  # a developer's own connect address must not reach the run either
        runtime_names += tuple(env_names("ARCADE_CONNECT_URL"))
    original = {name: os.environ.pop(name, None) for name in runtime_names}
    existing = env_value("TEST_ARCADE_HOME")
    worker = is_xdist_worker(request.config)
    # A serial run owns and stops its engine; parallel workers share one that the controller stops.
    owns = not existing and not worker and not server
    prefix = "devbrain_test_" + uuid.uuid4().hex[:8]
    if server:
        # Nothing is installed, started or stopped; the installation of the tests only connects (#68).
        changes = {"CAIRNMIND_ARCADE_CONNECT_URL": server[0], "CAIRNMIND_ARCADE_USER": "root",
                   "CAIRNMIND_ARCADE_PASSWORD": server[1], "CAIRNMIND_ARCADE_DB_PREFIX": prefix}
        os.environ.update(changes)
        settings = Settings.load(tmp_path_factory.mktemp("server-installation"), create=True)
    elif existing:
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
    if not server:
        changes = {"CAIRNMIND_ARCADE_URL": settings.arcade_url,
                   "CAIRNMIND_ARCADE_USER": settings.arcade_user,
                   "CAIRNMIND_ARCADE_PASSWORD": settings.arcade_password,
                   "CAIRNMIND_ARCADE_DB_PREFIX": prefix}
        os.environ.update(changes)
    original_ensure, original_request = ArcadeClient.ensure_database, ArcadeClient._request
    ArcadeClient._request = explained_when_unavailable(settings, original_request)
    template = None
    try:
        # Built inside the try: a schema that fails to initialise must still stop the engine and restore the environment.
        template = SchemaTemplate(settings, prefix, tmp_path_factory.mktemp("schema-template"))
        ArcadeClient.ensure_database = (created_as_root(original_ensure, prefix, template) if server
                                        else template.ensure_database(original_ensure))
        yield settings, prefix, template
    finally:
        ArcadeClient.ensure_database, ArcadeClient._request = original_ensure, original_request
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
def server_mode_keeps_the_address_to_the_engine_tests(request, monkeypatch):
    """Server mode exports the connect address, once the engine fixture has run, to every later test of the worker.

    A test that is not marked arcade never uses the server: it builds its own managed or Docker installation with
    stand-ins, which the address would turn connect-only. It gets no address (#218). An arcade test would print the
    notice that it saved the address, into the output of a command whose output it parses: only test_connect_only,
    which checks the notice, sees it."""
    if not server_under_test():
        yield
        return
    if request.node.get_closest_marker("arcade") is None:
        for name in env_names("ARCADE_CONNECT_URL"):
            monkeypatch.delenv(name, raising=False)
        yield
        return
    if request.module.__name__ == "test_connect_only":
        yield
        return
    original = Settings.load.__func__

    def load(cls, *args, **kwargs):
        settings = original(cls, *args, **kwargs)
        settings._origin.pop("connect_saved", None)
        return settings
    Settings.load = classmethod(load)
    yield
    Settings.load = classmethod(original)


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
