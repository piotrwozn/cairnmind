"""The second round of the first start (#313): doctor's exit code, one output rule for the commands a person reads,
a foreign engine on the port, ask without models, and what `install` writes and indexes. No engine is needed."""
import json

import httpx
import pytest
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.cli_cmds import doctor_attention, first_start
from cairnmind.storage import engine, store_writer
from cairnmind.memory import integrations, project_removal
from cairnmind.agents import knowledge_mode
from cairnmind.storage.arcade import ArcadeClient, ArcadeError
from cairnmind.config import Settings
from cairnmind.pal.platform_hints import hint

REPORT = {"python": "3.12.3", "data_dir": "/home/me/.local/share/cairnmind", "environment_notes": [],
          "clients": {"codex": {"installed": False, "verified_resume": False},
                      "claude": {"installed": False, "verified_resume": False}},
          "java": {"on_path": True, "executable": "/usr/bin/java", "compatible": True, "major": 21},
          "tmux": {"code": "tmux_missing"},
          "arcadedb": {"mode": "managed", "engine": "native", "available": True, "database_available": True,
                       "version_matches": True, "url": "http://127.0.0.1:2480", "database": "cairnmind"},
          "knowledge_agent": first_start.KNOWLEDGE_DISABLED,
          "ollama": {"available": False, "url": "http://127.0.0.1:11434"}, "memory_models": {},
          "service": {"available": False}, "service_running": False, "service_url": "http://127.0.0.1:8765",
          "store_writer": {"role": "writer"}}


def healthy(**changes):
    return {**REPORT, **changes}


# --- doctor: the exit code is "something required is missing" ----------------------------------------------------

@pytest.fixture
def doctor(monkeypatch, tmp_path):
    """`cairnmind doctor` over a report the test chooses; no engine, no store, no client."""
    from cairnmind.agents import api_runtime
    directory = tmp_path / "memory"
    directory.mkdir()
    (directory / "config.json").write_text(json.dumps({"port": 1, "knowledge_agent": "disabled"}))
    Settings.load(directory, create=True)
    state = {"report": healthy()}
    monkeypatch.setattr(cli, "service_alive", lambda settings: False)
    monkeypatch.setattr(integrations, "doctor", lambda settings: dict(state["report"]))
    monkeypatch.setattr(store_writer, "inspect", lambda settings: {"role": "writer"})
    monkeypatch.setattr(api_runtime, "coding_profiles_status", lambda settings: [])
    monkeypatch.setattr(cli, "store_for_read", lambda settings: (_ for _ in ()).throw(ValueError("no store")))

    def run(*arguments, terminal=True, **changes):
        state["report"] = healthy(**changes)
        monkeypatch.setattr(cli, "_stdout_is_terminal", lambda: terminal)
        return CliRunner().invoke(cli.app, ["--data-dir", str(directory), "doctor", *arguments])
    run.directory = directory
    return run


def test_doctor_exits_1_only_when_something_required_is_missing(doctor):
    for terminal in (True, False):
        assert doctor(terminal=terminal).exit_code == 0
    missing = {"available": False, "installed": False}
    for terminal in (True, False):
        broken = doctor(terminal=terminal, arcadedb=missing)
        assert broken.exit_code == 1
        if not terminal:
            assert json.loads(broken.output)["arcadedb"] == missing  # the report is still whole, and has projects
            assert json.loads(broken.output)["projects"] is None
    assert doctor("--json", arcadedb=missing).exit_code == 1
    # Models set up and not answering, no tmux, no client: optional, so exit 0.
    (doctor.directory / "config.json").write_text(json.dumps({"port": 1, "knowledge_agent": "enabled"}))
    models = doctor(ollama={"available": False, "url": "http://127.0.0.1:11434"}, knowledge_agent=None)
    assert models.exit_code == 0, models.output
    assert "Optional and not working:" in models.output and "Ollama does not answer" in models.output


def test_optional_parts_that_do_not_work_have_their_own_group():
    report = healthy(knowledge_agent="", clients={
        "claude": {"installed": True, "verified_resume": False}, "codex": {"installed": False}})
    del report["knowledge_agent"]
    lines = doctor_attention.summary(report)
    heading = lines.index(first_start.OPTIONAL_BROKEN)
    assert lines[heading - 1] == first_start.NOTHING_MISSING
    assert lines[heading + 1] == "  - Ollama does not answer; local models need `ollama serve`."
    assert lines[heading + 2].startswith("  - claude is installed but its contract checks fail")
    assert lines[-1] == first_start.NEXT_START
    assert doctor_attention.required_missing(report) == []
    assert doctor_attention.attention(report) == doctor_attention.not_working(report) and len(
        doctor_attention.attention(report)) == 2


def test_the_next_step_follows_the_registered_projects():
    assert doctor_attention.summary(healthy(projects=2))[-1] == first_start.NEXT_PROJECTS
    assert doctor_attention.summary(healthy(projects=0))[-1] == first_start.NEXT_START
    assert doctor_attention.summary(healthy(projects=None))[-1] == first_start.NEXT_START
    broken = healthy(projects=2, arcadedb={"available": False, "installed": False})
    assert doctor_attention.summary(broken)[-1] == first_start.NEXT_FIX


def test_a_stopped_service_is_not_told_to_be_stopped():
    assert "cairnmind stop" not in first_start.SERVICE_ON_DEMAND
    assert "  - " + first_start.SERVICE_ON_DEMAND in doctor_attention.summary(healthy())


# --- a foreign engine on the port ---------------------------------------------------------------------------------

def refusing(monkeypatch):
    """Every ArcadeClient of the engine module talks to a server that does not know the sign-in."""
    real = ArcadeClient
    transport = httpx.MockTransport(lambda request: httpx.Response(401, json={"error": "User/Password not valid"}))
    monkeypatch.setattr(engine, "ArcadeClient", lambda settings, **kw: real(settings, transport=transport))
    return real, transport


def test_an_engine_that_refuses_the_sign_in_is_named_with_the_way_out(monkeypatch, tmp_path):
    settings = Settings.load(tmp_path / "new", create=True)
    real, transport = refusing(monkeypatch)
    client = real(settings, transport=transport)
    with pytest.raises(ArcadeError) as caught:
        client.server_info()
    text = str(caught.value)
    assert text.startswith("ArcadeDB HTTP 401: User/Password not valid. ")
    assert first_start.FOREIGN_ENGINE.format(url=settings.arcade_url, data_dir=settings.data_dir) in text
    assert "arcade_url" in text and "--data-dir" in text and "arcade_password" not in text
    status = engine.engine_status(settings)
    assert status["sign_in_refused"] is True and status["available"] is False and status["error"] == text
    started = []
    monkeypatch.setattr(engine, "start_engine", lambda settings: started.append(settings))
    with pytest.raises(ArcadeError, match="does not accept the sign-in"):
        engine.ensure_engine(settings)
    assert started == []  # never a second engine on a port that answers
    report = healthy(arcadedb={**status, "installed": True})
    assert doctor_attention.required_missing(report) == [doctor_attention._line(text, limit=900)]
    assert not any("does not answer" in line for line in doctor_attention.summary(report))


def test_start_engine_does_not_start_a_second_engine_on_a_port_that_answers(monkeypatch, tmp_path):
    settings = Settings.load(tmp_path / "new", create=True)
    refusing(monkeypatch)
    popen = []
    monkeypatch.setattr(engine.subprocess, "Popen", lambda *a, **k: popen.append(a))
    with pytest.raises(ArcadeError, match="does not accept the sign-in"):
        engine.start_engine(settings)
    assert popen == []


def test_start_engine_without_java_names_docker_too(monkeypatch, tmp_path):
    settings = Settings.load(tmp_path / "new", create=True)
    (engine.runtime_dir(settings) / "lib").mkdir(parents=True)
    monkeypatch.setattr(engine, "engine_status", lambda settings: {"available": False})
    monkeypatch.setattr(engine, "resolve_java", lambda settings: None)
    with pytest.raises(ArcadeError) as caught:
        engine.start_engine(settings)
    assert str(caught.value) == first_start.JAVA_NEEDED.format(found="not found", hint=hint("java"))
    assert "engine start --docker" in str(caught.value)
    monkeypatch.setattr(engine, "resolve_java", lambda settings: "/usr/bin/java")
    monkeypatch.setattr(engine, "java_major", lambda java: 17)
    with pytest.raises(ArcadeError) as old:
        engine.start_engine(settings)
    assert str(old.value) == first_start.JAVA_NEEDED.format(found="Java 17", hint=hint("java"))


# --- one rule: sentences in a terminal, the full JSON otherwise ----------------------------------------------------

def test_human_or_json_prints_sentences_only_in_a_terminal(monkeypatch, capsys):
    result = {"stopping": True}
    monkeypatch.setattr(cli, "_stdout_is_terminal", lambda: True)
    cli._human_or_json(result, False, lambda: ["Said \x1b[31mred", "next"])
    said = capsys.readouterr().out
    assert said.splitlines()[1] == "next" and "\x1b" not in said and '"stopping"' not in said
    cli._human_or_json(result, True, lambda: pytest.fail("--json asks for no sentence"))
    assert json.loads(capsys.readouterr().out) == result
    monkeypatch.setattr(cli, "_stdout_is_terminal", lambda: False)
    cli._human_or_json(result, False, lambda: pytest.fail("a pipe asks for no sentence"))
    assert json.loads(capsys.readouterr().out) == result


@pytest.fixture
def run(monkeypatch, tmp_path):
    def invoke(*arguments, terminal=True):
        monkeypatch.setattr(cli, "_stdout_is_terminal", lambda: terminal)
        return CliRunner().invoke(cli.app, ["--data-dir", str(tmp_path / "memory"), *arguments])
    return invoke


def test_project_list_stop_and_engine_stop_speak_in_a_terminal_and_keep_their_json(monkeypatch, run):
    rows = []
    monkeypatch.setattr(cli, "store_for_read", lambda settings: type("S", (), {"list_projects": lambda self: rows})())
    monkeypatch.setattr(project_removal, "removed_rows", lambda reader: [])  # this fake store has no query (#327)
    assert run("project", "list").stdout.strip() == first_start.PROJECTS_NONE
    assert json.loads(run("project", "list", terminal=False).stdout) == []
    rows.extend([{"id": "a", "name": "alpha", "root": "/w/alpha"}, {"id": "b", "name": "beta", "root": "/w/beta"}])
    lines = run("project", "list").stdout.splitlines()
    assert lines == [first_start.PROJECTS_HEADING, "  - alpha: /w/alpha", "  - beta: /w/beta", first_start.PROJECTS_NEXT]
    assert json.loads(run("project", "list", terminal=False).stdout) == rows
    assert json.loads(run("project", "list", "--json").stdout) == rows
    monkeypatch.setattr(cli, "service_alive", lambda settings: False)
    said = run("stop")
    assert said.stdout.strip().startswith("The Cairnmind service at http://") and "was not running" in said.stdout
    assert json.loads(run("stop", terminal=False).stdout) == {"stopping": False, "reason": "The service is not running"}
    monkeypatch.setattr(engine, "stop_engine", lambda settings: {"stopping": True})
    stopped = run("engine", "stop").stdout
    assert stopped.startswith("The database engine of ") and "was stopped" in stopped
    refused = {"stopping": False, "reason": "No managed process for this data directory"}
    monkeypatch.setattr(engine, "stop_engine", lambda settings: refused)
    assert run("engine", "stop").stdout.strip() == first_start.ENGINE_NOT_STOPPED.format(
        reason="No managed process for this data directory")
    assert json.loads(run("engine", "stop", terminal=False).stdout) == refused


def test_stop_names_a_running_service_and_the_engine_it_leaves(monkeypatch, run, tmp_path):
    monkeypatch.setattr(cli, "service_alive", lambda settings: True)
    monkeypatch.setattr(cli, "request", lambda settings, method, path, **kw: {"stopping": True})
    (tmp_path / "memory" / "arcade").mkdir(parents=True)
    (tmp_path / "memory" / "arcade" / "process.json").write_text("{}")
    lines = run("stop").stdout.splitlines()
    assert "is stopping" in lines[0] and lines[1] == first_start.ENGINE_LEFT_RUNNING
    monkeypatch.setattr(cli, "request", lambda settings, method, path, **kw: {"stopping": False})
    assert "answered but did not stop" in run("stop").stdout.splitlines()[0]


def test_init_says_where_memory_is_in_a_terminal(monkeypatch, run, tmp_path):
    from cairnmind.storage import database_mode, engine_docker
    store = type("S", (), {"not_writer": None, "confirmations_before_rule": {"marked": 2, "before_rule": 1}})()
    monkeypatch.setattr(engine_docker, "choose_engine_mode", lambda settings: None)
    monkeypatch.setattr(database_mode, "connect_only", lambda settings: False)
    monkeypatch.setattr(database_mode, "engine_mode", lambda settings: "native")
    monkeypatch.setattr(cli, "store_for", lambda settings, **kw: store)
    monkeypatch.setattr(knowledge_mode, "choose", lambda settings: None)
    lines = run("init").stdout.splitlines()
    assert lines == [first_start.INIT_READY.format(data_dir=tmp_path / "memory"),
                     first_start.INIT_MARKED.format(marked=2, left=1)]
    piped = json.loads(run("init", terminal=False).stdout)
    assert piped["next"].startswith("cairnmind doctor") and piped["confirmations_before_rule"]["marked_as_a_persons"] == 2


# --- ask -----------------------------------------------------------------------------------------------------------

MATCH = {"id": "c1", "source_id": "s1", "uri": "/w/app.py", "kind": "code", "match": "full_text",
         "line_start": 1, "line_end": 3, "excerpt": "def login(): pass"}


def unavailable(*matches):
    found = {"matches": list(matches), "held_back": None, "left_out": False, "warnings": [], "elapsed_seconds": 0.1}
    return knowledge_mode.unavailable_answer(found)


def test_ask_in_a_terminal_is_a_short_view_and_its_json_is_unchanged(monkeypatch, run):
    answer = unavailable(MATCH)
    monkeypatch.setattr(cli, "service_alive", lambda settings: True)
    monkeypatch.setattr(cli, "request", lambda settings, method, path, **kw: answer)
    shown = run("ask", "demo", "where is login?").stdout
    assert shown.startswith("Status: " + first_start.MODE_STATUS)
    assert first_start.ASK_OFF in shown and first_start.MATCHES_HEADING in shown and '"status"' not in shown
    assert json.loads(run("ask", "demo", "where is login?", terminal=False).stdout) == answer
    assert answer["status"] == "unavailable"  # the JSON keeps saying so; no answer is made up


def test_ask_summary_keeps_unavailable_with_models():
    from cairnmind.terminal.terminal_commands import ask_summary
    configured = {"status": "unavailable", "answer": "The model server did not answer.", "unknowns": []}
    assert ask_summary(configured, str).startswith("Status: Unavailable")
    assert ask_summary(unavailable(MATCH), str).startswith("Status: " + first_start.MODE_STATUS)


def test_a_record_and_the_note_it_came_from_are_one_match():
    note = {**MATCH, "id": "chunk", "source_id": "note", "uri": "manual://1", "excerpt": "Accounts  lock after five."}
    record = {**note, "id": "rec", "status": "proposed", "excerpt": "Accounts lock after five."}
    other = {**note, "id": "elsewhere", "source_id": "readme", "uri": "/w/README.md"}
    kept = knowledge_mode._without_copies([note, record, other])
    assert [match["id"] for match in kept] == ["rec", "elsewhere"]  # the record carries the status; another place stays
    assert knowledge_mode._without_copies([note, other]) == [note, other]


def test_ask_for_an_unknown_project_starts_no_service(monkeypatch, run):
    from cairnmind.storage.store_readonly import StoreNotReady
    started, asked = [], []

    class Store:
        def __init__(self, error):
            self.error = error

        def resolve_project(self, project):
            raise self.error
    monkeypatch.setattr(cli, "service_alive", lambda settings: False)
    monkeypatch.setattr(cli, "_ensure_service_or_refuse", lambda settings: started.append(1))
    monkeypatch.setattr(cli, "store_for_read",
                        lambda settings: Store(ValueError("Unknown project: nope. " + first_start.UNKNOWN_PROJECT_NEXT)))
    refused = run("ask", "nope", "why?")
    assert refused.exit_code == 1 and str(refused.exception).startswith("Unknown project: nope. cairnmind project list")
    assert started == []
    # The store cannot be read yet (the engine is not running): the service starts and decides, as before.
    monkeypatch.setattr(cli, "store_for_read", lambda settings: Store(StoreNotReady("not ready")))
    monkeypatch.setattr(cli, "request", lambda settings, method, path, **kw: asked.append(path) or unavailable())
    assert run("ask", "nope", "why?", terminal=False).exit_code == 0 and asked == ["/ask"]


# --- project add, remember --------------------------------------------------------------------------------------

def test_project_add_of_a_missing_folder_says_how_to_go_on(monkeypatch, run, tmp_path):
    monkeypatch.setattr(cli, "store_for", lambda *a, **k: pytest.fail("the store is not opened for a missing folder"))
    missing = tmp_path / "no folder"
    result = run("project", "add", "x", str(missing))
    assert result.exit_code == 1
    text = str(result.exception)
    from cairnmind.pal.process_control import quote_arg
    assert text == first_start.PROJECT_FOLDER_MISSING.format(root=missing, quoted=quote_arg(str(missing)))
    assert "mkdir -p" in text


def test_point_six_sentences_say_nothing_more_is_needed():
    for sentence in (first_start.REMEMBER_PROPOSED, first_start.BRIEF_REVIEW):
        assert "othing more is needed to go on" in sentence and "already use" in sentence
        assert "--confirmed" not in sentence
        assert "Ctrl+B i" in sentence and "later" in sentence  # a person can still confirm, in the workspace


def test_every_new_sentence_has_polish():
    for name in ("OPTIONAL_BROKEN", "NEXT_PROJECTS", "PROJECT_EXISTED", "PROJECT_FOLDER_MISSING", "REMEMBER_STORED",
                 "STOP_STOPPING", "STOP_NOT_RUNNING", "STOP_REFUSED", "ENGINE_LEFT_RUNNING", "ENGINE_STOPPED",
                 "ENGINE_STOPPED_CONNECT", "ENGINE_NOT_STOPPED", "PROJECTS_NONE", "PROJECTS_HEADING", "PROJECTS_NEXT",
                 "INIT_READY", "INIT_MARKED", "MODE_STATUS", "FOREIGN_ENGINE", "SERVICE_ON_DEMAND",
                 "REMEMBER_PROPOSED", "BRIEF_REVIEW", "INSTALL_NO_CLIENT", "CLIENT_SKIPPED"):
        english = getattr(first_start, name)
        assert first_start.MESSAGES[english] != english, name


# --- install writes nothing for an absent client, and the managed block is not indexed -------------------------------

def test_install_writes_nothing_for_a_client_that_is_not_installed(monkeypatch, tmp_path):
    settings = Settings.load(tmp_path / "memory", create=True)
    root = tmp_path / "app"
    root.mkdir()
    project = {"id": "p", "name": "app", "root": str(root)}
    monkeypatch.setattr(integrations, "_probe_cli", lambda *a, **k: {"installed": False, "mcp": False, "hooks": False,
                                                                     "notes": []})
    absent = integrations.install_integrations(settings, project)
    assert absent["changed"] == [] and not (root / "AGENTS.md").exists() and not (root / "CLAUDE.md").exists()
    assert absent["warnings"] == [first_start.CLIENT_SKIPPED.format(client=c) for c in ("codex", "claude")]
    assert first_start.TRUST_NOTE not in absent["warnings"]
    monkeypatch.setattr(integrations, "_probe_cli", lambda client, *a, **k: {
        "installed": client == "codex", "mcp": False, "hooks": False, "notes": []})
    present = integrations.install_integrations(settings, project)
    assert (root / "AGENTS.md").exists() and not (root / "CLAUDE.md").exists()
    assert first_start.TRUST_NOTE in present["warnings"]


def test_the_managed_block_is_not_chunked():
    from cairnmind.scip import ingest
    block = f"{integrations.BEGIN}\nCall resume_context first.\n{integrations.END}"
    text = f"My rules\n\n{block}\n\nAfter the block\nlast line\n"
    chunks = list(ingest._chunks(text, "/p/AGENTS.md"))
    joined = "\n".join(content for content, _, _ in chunks)
    assert "BEGIN CAIRNMIND" not in joined and "resume_context" not in joined
    assert chunks[0][1:] == (1, 2) and chunks[1][1:] == (6, 8)  # the file's own line numbers
    assert "After the block" in chunks[1][0]
    assert any("resume_context" in content for content, _, _ in ingest._chunks(text, "/p/README.md"))
    earlier = text.replace(integrations.BEGIN, integrations.EARLIER_BEGIN).replace(integrations.END,
                                                                                    integrations.EARLIER_END)
    assert "resume_context" not in "\n".join(c for c, _, _ in ingest._chunks(earlier, "CLAUDE.md"))
    unclosed = f"{integrations.BEGIN}\nCall resume_context first.\n"
    assert any("resume_context" in c for c, _, _ in ingest._chunks(unclosed, "AGENTS.md"))  # an unclosed block stays
    prose = f"Do not edit between {integrations.BEGIN} and {integrations.END} by hand.\n"
    assert list(ingest._chunks(prose, "AGENTS.md"))[0][0].startswith("Do not edit between")
    sample = "a\nb\nc\n" * 40
    assert list(ingest._chunks(sample)) == list(ingest._chunks(sample, "")) == list(ingest._chunks(sample, "/p/x.py"))
    crlf = text.replace("\n", "\r\n")
    assert "resume_context" not in "\n".join(c for c, _, _ in ingest._chunks(crlf, "AGENTS.md"))


# --- review of the second round: what the first tests did not pin ------------------------------------------------------

def test_text_matches_lists_a_record_and_its_note_once(monkeypatch):
    import asyncio
    from cairnmind.memory import memory_egress
    monkeypatch.setattr(memory_egress, "ask_gate", lambda settings, store, also_held=frozenset(): None)  # #183's run classes
    shown = [{"id": "chunk", "source_id": "s1", "uri": "manual://note/1", "kind": "note", "text": "We deploy blue green."},
             {"id": "rec", "source_id": "s1", "uri": "manual://note/1", "kind": "decision", "status": "proposed",
              "text": "We deploy blue green."}]

    class Retriever:
        warnings: list = []

        def search(self, *args, **kwargs):
            return shown

    class Agent:
        settings = store = None
        retriever = Retriever()

        def _hits(self, hits, project_id, evidence, *args):
            evidence["s1"] = {"uri": "manual://note/1", "kind": "note"}
            return shown
    found = asyncio.run(knowledge_mode.text_matches(Agent(), "p", "how do we deploy?"))
    assert [match["id"] for match in found["matches"]] == ["rec"]


def test_doctor_lists_an_engine_that_refuses_the_sign_in_once(monkeypatch, tmp_path):
    from cairnmind.storage import arcade, database_credentials
    settings = Settings.load(tmp_path / "new", create=True)
    real = ArcadeClient
    transport = httpx.MockTransport(lambda request: httpx.Response(401, json={"error": "User/Password not valid"}))
    monkeypatch.setattr(arcade, "ArcadeClient", lambda settings, **kw: real(settings, transport=transport))
    block = database_credentials.doctor_block(settings)
    assert block["sign_in_refused"] is True and block["available"] is False
    lines = doctor_attention.summary(healthy(arcadedb={**block, "installed": True}))
    assert sum("does not accept the sign-in" in line for line in lines) == 1
    assert not any("does not answer" in line for line in lines)


def test_the_storage_line_says_the_cause_only_and_doctor_says_the_way_out_once(tmp_path):
    from cairnmind.cli_cmds.topology import _storage_line, topology
    settings = Settings.load(tmp_path / "new", create=True)
    reason = ("ArcadeDB HTTP 401: User/Password not valid. "
              + first_start.FOREIGN_ENGINE.format(url=settings.arcade_url, data_dir=settings.data_dir))
    place = topology(settings, writer={"role": "unknown", "reason": reason})
    line = _storage_line(place, "en")
    assert line.endswith("unknown: ArcadeDB HTTP 401: User/Password not valid")
    assert "arcade_url" not in line


def test_the_engine_of_this_data_directory_is_not_called_foreign(monkeypatch, tmp_path):
    settings = Settings.load(tmp_path / "new", create=True)
    transport = httpx.MockTransport(lambda request: httpx.Response(
        401, json={"error": "Too many failed authentication attempts. Try again later."}))
    client = lambda: ArcadeClient(settings, transport=transport)  # noqa: E731
    with pytest.raises(ArcadeError) as nobody:
        client().server_info()
    assert first_start.FOREIGN_ENGINE.format(url=settings.arcade_url, data_dir=settings.data_dir) in str(nobody.value)
    state = settings.data_dir / "arcade"
    state.mkdir(parents=True)
    (state / "process.json").write_text(json.dumps({"pid": 4242, "url": settings.arcade_url}))
    monkeypatch.setattr(engine, "native_engine_pid", lambda settings: 4242)
    with pytest.raises(ArcadeError) as own:
        client().server_info()
    text = str(own.value)
    assert first_start.OWN_ENGINE_REFUSED.format(url=settings.arcade_url, data_dir=settings.data_dir) in text
    assert "another data directory" not in text and "arcade_url" not in text
    (state / "process.json").write_text(json.dumps({"pid": 4242, "url": "http://127.0.0.1:1"}))
    with pytest.raises(ArcadeError, match="another data directory"):  # a record of another address proves nothing
        client().server_info()
    monkeypatch.setattr(engine, "native_engine_pid", lambda settings: None)  # the recorded process is not running
    (state / "process.json").write_text(json.dumps({"pid": 4242, "url": settings.arcade_url}))
    with pytest.raises(ArcadeError, match="another data directory"):
        client().server_info()
    assert first_start.MESSAGES[first_start.OWN_ENGINE_REFUSED] != first_start.OWN_ENGINE_REFUSED


def test_in_docker_mode_only_a_running_container_of_ours_is_the_own_engine(monkeypatch, tmp_path):
    from cairnmind.storage import engine_docker
    settings = Settings.load(tmp_path / "new", create=True)
    monkeypatch.setattr(engine, "engine_mode", lambda settings: "docker")
    asked = []

    def inspect(answer):
        def fake(settings, **_):
            asked.append(True)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return fake
    ours = {"Name": "/" + engine_docker.container_name(settings),
            "Config": {"Labels": engine_docker.labels(settings)}, "State": {"Running": True}}
    monkeypatch.setattr(engine_docker, "inspect_container", inspect(ours))
    assert engine.own_engine_recorded(settings) is False and asked == []  # no record, no Docker call
    state = settings.data_dir / "arcade"
    state.mkdir(parents=True)
    (state / "container.json").write_text(json.dumps({"name": engine_docker.container_name(settings)}))
    assert engine.own_engine_recorded(settings) is True
    for stale in (None, {**ours, "State": {"Running": False}}, {**ours, "Config": {"Labels": {}}},
                  ArcadeError("Docker does not answer")):  # gone, stopped, not ours, cannot be asked
        monkeypatch.setattr(engine_docker, "inspect_container", inspect(stale))
        assert engine.own_engine_recorded(settings) is False


def test_ask_names_a_proposed_finding_and_the_warnings_of_the_json():
    from cairnmind.terminal.terminal_commands import ask_summary
    from cairnmind.terminal.terminal_i18n import tr
    record = {**MATCH, "id": "rec", "uri": "manual://note/1", "status": "proposed", "excerpt": "We deploy blue green."}
    answer = unavailable(record, {**MATCH, "id": "kept", "uri": "/w/b.py", "status": "confirmed"})
    answer["freshness"]["warnings"] = ["Current source changed since it was indexed: /w/a.py"]
    shown = ask_summary(answer, str)
    # #326: a note saved with remember is a stored finding, not an opaque address.
    assert 'stored finding (1-3) (unconfirmed): "We deploy blue green."' in shown and "manual://" not in shown
    assert "/w/b.py (1-3):" in shown  # a confirmed record carries no mark
    assert shown.splitlines()[shown.splitlines().index("Warnings:") + 1] == (
        "- Current source changed since it was indexed: /w/a.py")
    assert "zapisane ustalenie (1-3) (niepotwierdzone)" in ask_summary(answer, lambda text: tr(text, "pl"))
    assert "Ostrzeżenia:" in ask_summary(answer, lambda text: tr(text, "pl"))


def test_ask_for_a_project_on_an_engine_that_refuses_this_directory_starts_no_service(monkeypatch, run):
    started = []
    refusal = ArcadeError("ArcadeDB HTTP 401: User/Password not valid. " + first_start.FOREIGN_ENGINE.format(
        url="http://127.0.0.1:2480", data_dir="/d"), status_code=401)

    class Store:
        def resolve_project(self, project):
            raise refusal
    monkeypatch.setattr(cli, "service_alive", lambda settings: False)
    monkeypatch.setattr(cli, "_ensure_service_or_refuse", lambda settings: started.append(1))
    monkeypatch.setattr(cli, "store_for_read", lambda settings: Store())
    result = run("ask", "p", "why?")
    assert result.exit_code == 1 and result.exception is refusal and started == []


def test_doctor_counts_the_registered_projects_and_ends_with_the_next_step(monkeypatch, doctor):
    from cairnmind.memory import record_kinds
    store = type("S", (), {"list_projects": lambda self: [{"id": "a"}]})()
    monkeypatch.setattr(cli, "store_for_read", lambda settings: store)
    monkeypatch.setattr(record_kinds, "doctor_view", lambda store: {})
    assert json.loads(doctor(terminal=False).output)["projects"] == 1
    assert doctor().output.strip().splitlines()[-1] == first_start.NEXT_PROJECTS
