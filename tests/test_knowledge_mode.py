"""Deterministic-only mode (#274): the knowledge agent's stored choice, what init, `models pull` and `models set` do
with it, and what doctor, the evaluations and `ask` say without models.

No test asks a model server that is not a stand-in: servers are httpx.MockTransport, and the store is a FakeStore (the
fast tier has no database). The conftest fixture models_found_at_init replaces the probe of init; the tests of the real
probe put it back.
"""
import json

import httpx
import pytest
from typer.testing import CliRunner

from cairnmind import cli, first_start, knowledge_mode
from cairnmind.config import Settings
from cairnmind.knowledge_mode import missing_models as REAL_PROBE
from cairnmind.terminal_i18n import tr

NEW_SENTENCES = [first_start.KNOWLEDGE_DISABLED, first_start.MODELS_NOT_CONFIGURED, first_start.GET_MODELS,
                 first_start.SERVER_DOWN, first_start.MODEL_MISSING, first_start.CHOSE_ENABLED,
                 first_start.CHOSE_DISABLED, first_start.TURNED_ON, first_start.ASK_OFF, first_start.NEEDS_MODELS,
                 first_start.KNOWLEDGE_INVALID, first_start.RESTART_FOR_MODELS, first_start.ASK_NO_MATCH,
                 first_start.ASK_LEFT_OUT, first_start.MATCHES_HEADING, first_start.MATCHES_HELD_BACK]


def test_a_config_from_before_0_13_counts_as_enabled_and_a_new_one_waits_for_init(tmp_path):
    old = tmp_path / "old"
    old.mkdir()
    (old / "config.json").write_text(json.dumps({"port": 1, "chat_model": "m"}))  # no knowledge_agent: before 0.13.0
    assert Settings.load(old).knowledge_agent == "enabled"
    new = Settings.load(tmp_path / "new", create=True)
    assert new.knowledge_agent == ""  # not chosen yet: init chooses
    assert Settings.load(tmp_path / "new").knowledge_agent == ""  # saved as it is, not turned into enabled
    assert Settings.load(tmp_path / "nothing").knowledge_agent == ""  # no config.json at all
    new.knowledge_agent = "disabled"
    assert [knowledge_mode.enabled(Settings.load(old)), knowledge_mode.enabled(Settings.load(tmp_path / "new")),
            knowledge_mode.enabled(new)] == [True, True, False]
    new.knowledge_agent = "on"
    with pytest.raises(ValueError, match="knowledge_agent in config.json is 'on'; it must be enabled or disabled"):
        knowledge_mode.enabled(new)


NOTHING_FOUND = {"matches": [], "held_back": None, "left_out": False, "warnings": [], "elapsed_seconds": 0.0}


def test_found_but_left_out_is_never_said_to_be_nothing_found():
    held = {**NOTHING_FOUND, "held_back": {"chunks": 1}, "left_out": True}
    answer = knowledge_mode.unavailable_answer(held)
    assert answer["answer"] == first_start.ASK_LEFT_OUT != first_start.ASK_NO_MATCH
    assert first_start.MATCHES_HELD_BACK in answer["unknowns"] and answer["full_text_matches"] == []
    dropped = knowledge_mode.unavailable_answer({**NOTHING_FOUND, "left_out": True, "warnings": ["a file changed"]})
    assert dropped["answer"] == first_start.ASK_LEFT_OUT and dropped["freshness"]["warnings"] == ["a file changed"]
    assert tr(first_start.ASK_LEFT_OUT, "pl") == first_start.MESSAGES[first_start.ASK_LEFT_OUT]


def a_match(uri="notes.md", excerpt="The ledger stores amounts in cents."):
    return {"id": "chunk-" + uri, "source_id": "source-" + uri, "uri": uri, "kind": "document", "match": "full_text",
            "line_start": 1, "line_end": 1, "excerpt": excerpt}


def test_the_unavailable_answer_and_the_status_fields():
    answer = knowledge_mode.unavailable_answer(NOTHING_FOUND)
    assert answer["status"] == "unavailable" and answer["sources"] == [] and answer["full_text_matches"] == []
    assert answer["answer"] == first_start.ASK_NO_MATCH
    assert answer["unknowns"] == [first_start.GET_MODELS]
    assert answer["diagnostics"]["code"] == "models_not_configured"
    pipeline = {"archive": {"pending": 7, "running": 0, "completed": 0, "failed": 0},
                "indexing": {"pending": 3, "running": 1, "completed": 5, "failed": 2},
                "extraction": {"pending": 2, "running": 0, "completed": 1, "failed": 0},
                "other": {"pending": 9, "running": 9, "completed": 0, "failed": 0}}
    assert knowledge_mode.status_fields(pipeline) == {
        "models": "not configured (deterministic-only mode)", "jobs_held": 6, "get_models": first_start.GET_MODELS}


def test_status_tells_a_running_disabled_service_to_restart_once_config_says_enabled(tmp_path):
    pipeline = {group: {"pending": 1, "running": 0, "completed": 0, "failed": 0}
                for group in ("indexing", "extraction")}
    settings = Settings.load(tmp_path, create=True)
    settings.knowledge_agent = "disabled"
    settings.save()
    assert knowledge_mode.status_fields(pipeline, settings)["get_models"] == first_start.GET_MODELS
    knowledge_mode.turn_on(settings)  # `models pull` ran while the service kept running
    fields = knowledge_mode.status_fields(pipeline, settings)
    assert fields["get_models"] == first_start.RESTART_FOR_MODELS and fields["models"].startswith("not configured")
    (tmp_path / "config.json").write_text("{ half an edit")  # a hand edit in progress must not take /status down
    assert knowledge_mode.status_fields(pipeline, settings)["get_models"] == first_start.GET_MODELS


def test_the_service_refuses_to_start_on_a_knowledge_agent_value_it_does_not_know(tmp_path):
    from dataclasses import replace
    from cairnmind.service import create_app
    settings = replace(Settings.load(tmp_path, create=True), knowledge_agent="on",
                       arcade_url="http://127.0.0.1:1", ollama_url="http://127.0.0.1:1")
    with pytest.raises(ValueError, match="knowledge_agent in config.json"):
        create_app(settings)  # refused before the store is opened


def test_the_ask_panel_shows_the_answer_in_polish_and_every_new_sentence_has_polish():
    from cairnmind.terminal_commands import ask_summary
    found = {**NOTHING_FOUND, "matches": [a_match()], "held_back": {"classes": ["chat"], "chunks": 1}}
    shown = ask_summary(knowledge_mode.unavailable_answer(found), lambda text: tr(text, "pl"))
    assert first_start.MESSAGES[first_start.ASK_OFF] in shown
    assert first_start.MESSAGES[first_start.GET_MODELS] in shown
    assert first_start.MESSAGES[first_start.MATCHES_HELD_BACK] in shown
    for english in NEW_SENTENCES:
        assert first_start.MESSAGES[english] != english
        assert tr(english, "pl") == first_start.MESSAGES[english]


def test_the_answer_without_models_lists_matches_apart_from_sources_and_says_it_is_no_answer():
    two = [a_match("notes.md"), a_match("other.md", "Cents again.")]
    answer = knowledge_mode.unavailable_answer({**NOTHING_FOUND, "matches": two})
    assert answer["answer"] == first_start.ASK_OFF and answer["sources"] == []
    assert answer["full_text_matches"] == two and "citations" not in answer and answer["cached"] is False
    held = knowledge_mode.unavailable_answer({**NOTHING_FOUND, "matches": two, "held_back": {"chunks": 2}},
                                             ["x" * 500] * 7)
    assert first_start.MATCHES_HELD_BACK in held["unknowns"] and held["diagnostics"]["held_back"] == {"chunks": 2}
    errors = [item for item in held["unknowns"] if item == "x" * 240]
    assert len(errors) == 1 and len(held["unknowns"]) <= 8  # equal errors count once; each is cut to 240 characters
    varied = knowledge_mode.unavailable_answer(NOTHING_FOUND, [f"error {n}" for n in range(7)])
    assert [item for item in varied["unknowns"] if item.startswith("error")] == [f"error {n}" for n in range(5)]
    assert varied["freshness"]["warnings"] == [f"error {n}" for n in range(5)]


class Search:
    """A retriever stand-in that records how it was asked, and which gate was set while it ran."""

    def __init__(self):
        self.calls, self.warnings = [], []

    def search(self, *args, **kwargs):
        from cairnmind.memory_egress import ASK
        self.calls.append((args, kwargs, ASK.get()))
        return ["hit"]


async def test_text_matches_use_the_lexical_search_of_ask_and_ask_no_model(tmp_path):
    from types import SimpleNamespace
    from cairnmind.agent import INITIAL_HITS
    from cairnmind.memory_egress import ASK, Gate
    from test_deterministic_mode import Untouched  # records every attribute asked of it
    remote = {"provider": "openai_compatible", "base_url": "https://models.example/v1", "allow_remote": True}
    model, seen = Untouched(), []

    def hits(found, project_id, evidence, worktree, question, register, size):
        seen.append((found, register, size))
        shown = []
        for number in range(12):
            evidence[f"s{number}"] = {"uri": f"f{number}.md", "kind": "document"}
            shown.append({"id": f"c{number}", "source_id": f"s{number}", "text": "cents", "line_start": 1})
        shown[0]["refreshed"] = True  # a file changed since indexing: its start is not a passage with the words
        return shown

    def agent_for(settings):
        return SimpleNamespace(settings=settings, store=object(), ollama=model, retriever=Search(), _hits=hits)

    for settings, gate_type in ((Settings(data_dir=tmp_path, memory_chat=remote), Gate), (Settings(data_dir=tmp_path), type(None))):
        agent = agent_for(settings)
        found = await knowledge_mode.text_matches(agent, "p", "ledger cents", worktree="/w")
        (args, kwargs, gate), = agent.retriever.calls
        assert args == ("p", "ledger cents", INITIAL_HITS) and kwargs == {"worktree": "/w"}  # no query_vector
        assert isinstance(gate, gate_type) and ASK.get() is None
        assert seen[-1] == (["hit"], False, 400)
        assert len(found["matches"]) == 8 and "c0" not in {m["id"] for m in found["matches"]}
        assert all(m["match"] == "full_text" and m["excerpt"] == "cents" and "sha256" not in m
                   for m in found["matches"])
        assert found["matches"][0] == {"id": "c1", "source_id": "s1", "uri": "f1.md", "kind": "document",
                                       "match": "full_text", "line_start": 1, "excerpt": "cents"}
    assert model.touched == []


def test_the_ask_panel_lists_matches_under_their_own_heading_and_never_as_sources():
    from cairnmind.terminal_commands import ask_summary

    def polish(text):
        return tr(text, "pl")
    shown = ask_summary(knowledge_mode.unavailable_answer({**NOTHING_FOUND, "matches": [a_match()]}), polish)
    assert first_start.MESSAGES[first_start.ASK_OFF] in shown
    assert polish(first_start.MATCHES_HEADING) + ":" in shown
    assert '- notes.md (1-1): "The ledger stores amounts in cents."' in shown
    assert polish("Sources") + ":" not in shown
    empty = ask_summary(knowledge_mode.unavailable_answer(NOTHING_FOUND), polish)
    assert first_start.MESSAGES[first_start.ASK_NO_MATCH] in empty and polish(first_start.MATCHES_HEADING) not in empty


# --- init, `models pull` and `models set` -----------------------------------------------------------------------

REAL_ASYNC_CLIENT = httpx.AsyncClient  # a test that patches the client twice must not wrap its own first patch
CHAT, EMBEDDING ="qwen3.5:9b", "qwen3-embedding:0.6b"
TAGS = {"models": [{"name": CHAT, "digest": "a" * 64}, {"name": EMBEDDING, "digest": "b" * 64}]}


class FakeStore:
    """The part of the store init and `models pull` touch; the fast tier has no database."""
    not_writer = None
    confirmations_before_rule = None

    def resolve_project(self, project):
        return {"id": "p1", "name": "demo"}


def _server(monkeypatch, handler):
    """Every httpx.Client of the process talks to `handler` (a stand-in model server); returns the requests seen."""
    seen = []
    real = httpx.Client

    def respond(request):
        seen.append((request.method, request.url.path))
        return handler(request)
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(respond), **kw))
    return seen


def _down(request):
    raise httpx.ConnectError("refused", request=request)


def _init(monkeypatch, tmp_path):
    """`cairnmind init` with the real probe, in tmp_path/memory, and no engine or store."""
    from cairnmind import database_mode, engine_docker
    monkeypatch.setattr(engine_docker, "choose_engine_mode", lambda settings: None)
    monkeypatch.setattr(database_mode, "connect_only", lambda settings: False)
    monkeypatch.setattr(database_mode, "engine_mode", lambda settings: "native")
    monkeypatch.setattr(cli, "store_for", lambda settings, **kw: FakeStore())
    monkeypatch.setattr(knowledge_mode, "missing_models", REAL_PROBE)
    data = tmp_path / "memory"
    return data, CliRunner().invoke(cli.app, ["--data-dir", str(data), "init"])


def test_init_without_a_model_server_chooses_deterministic_only_and_says_so(monkeypatch, tmp_path):
    seen = _server(monkeypatch, _down)
    data, result = _init(monkeypatch, tmp_path)
    assert result.exit_code == 0, result.output
    said = result.stderr
    assert said.startswith("Cairnmind: Knowledge agent: disabled (deterministic-only mode), because "
                           "the model server http://127.0.0.1:11434 does not answer.")
    assert first_start.GET_MODELS in said and str(data / "config.json") in said
    assert said.count("does not answer") == 1  # both roles use the one server
    assert Settings.load(data).knowledge_agent == "disabled"
    assert json.loads(result.stdout)["knowledge_agent"] == "disabled"
    assert seen and set(seen) == {("GET", "/api/tags")}  # a question, never a download


def test_init_with_a_server_without_the_models_chooses_deterministic_only(monkeypatch, tmp_path):
    seen = _server(monkeypatch, lambda request: httpx.Response(200, json={"models": []}))
    data, result = _init(monkeypatch, tmp_path)
    assert result.exit_code == 0, result.output
    assert f"the model server http://127.0.0.1:11434 does not have {CHAT}; " in result.stderr
    assert f"the model server http://127.0.0.1:11434 does not have {EMBEDDING}." in result.stderr
    assert Settings.load(data).knowledge_agent == "disabled"
    assert set(seen) == {("GET", "/api/tags")}


def test_init_with_both_models_chooses_enabled(monkeypatch, tmp_path):
    _server(monkeypatch, lambda request: httpx.Response(200, json=TAGS))
    data, result = _init(monkeypatch, tmp_path)
    assert result.exit_code == 0, result.output
    assert result.stderr.startswith(f"Cairnmind: Knowledge agent: enabled. The model server answers and has the "
                                    f"models {CHAT} and {EMBEDDING}.")
    assert Settings.load(data).knowledge_agent == "enabled"
    assert json.loads(result.stdout)["knowledge_agent"] == "enabled"


@pytest.mark.parametrize("stored", [None, "disabled", "enabled"])
def test_init_keeps_a_config_from_before_0_13_and_never_asks_again(monkeypatch, tmp_path, stored):
    def must_not_ask(request):
        raise AssertionError("init asked a model server although the choice is stored")
    _server(monkeypatch, must_not_ask)
    data = tmp_path / "memory"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"port": 1} if stored is None
                                                 else {"port": 1, "knowledge_agent": stored}))
    _, result = _init(monkeypatch, tmp_path)
    assert result.exit_code == 0, result.output
    assert "Knowledge agent" not in result.stderr
    assert json.loads(result.stdout)["knowledge_agent"] == (stored or "enabled")
    assert Settings.load(data).knowledge_agent == (stored or "enabled")


def test_init_refuses_a_knowledge_agent_value_it_does_not_know(monkeypatch, tmp_path):
    data = tmp_path / "memory"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"port": 1, "knowledge_agent": "maybe"}))
    _, result = _init(monkeypatch, tmp_path)
    assert isinstance(result.exception, ValueError)
    assert str(result.exception) == "knowledge_agent in config.json is 'maybe'; it must be enabled or disabled."


def test_init_refuses_a_configured_but_invalid_model_profile_instead_of_storing_disabled(monkeypatch, tmp_path):
    seen = _server(monkeypatch, _down)
    data = tmp_path / "memory"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"port": 1, "knowledge_agent": "", "memory_chat": {
        "provider": "openai_compatible", "base_url": "https://models.example.invalid/v1"}}))  # no consent to send out
    path, result = _init(monkeypatch, tmp_path)
    assert isinstance(result.exception, ValueError) and "Invalid profile" in str(result.exception)
    assert Settings.load(data).knowledge_agent == ""  # nothing chosen: the person fixes the profile first
    assert ("POST", "/chat/completions") not in seen and all(call[0] == "GET" for call in seen)  # nothing was sent


def _disabled(tmp_path):
    data = tmp_path / "memory"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"port": 1, "knowledge_agent": "disabled"}))
    return data


def test_models_pull_turns_the_agent_on_and_says_how_to_turn_it_off(monkeypatch, tmp_path):
    from cairnmind import service

    def server(request):
        if request.url.path == "/api/pull":
            return httpx.Response(200, content=b'{"status":"pulling"}\n{"status":"success"}\n')
        return httpx.Response(200, json=TAGS)
    seen = _server(monkeypatch, server)
    monkeypatch.setattr(cli, "store_for", lambda settings, **kw: FakeStore())
    monkeypatch.setattr(service, "schedule_embeddings", lambda *a, **k: 0)
    data = _disabled(tmp_path)
    result = CliRunner().invoke(cli.app, ["--data-dir", str(data), "models", "pull"])
    assert result.exit_code == 0, result.output
    said = json.loads(result.stdout)["knowledge_agent"]
    assert said == first_start.TURNED_ON.format(config=data / "config.json")
    assert "set knowledge_agent to disabled in that file and restart the service" in said
    assert Settings.load(data).knowledge_agent == "enabled"
    assert [call for call in seen if call[1] == "/api/pull"] == [("POST", "/api/pull")] * 2


def test_models_pull_says_the_agent_is_on_even_when_the_store_step_fails(monkeypatch, tmp_path):
    _server(monkeypatch, lambda request: httpx.Response(200, content=b'{"status":"success"}\n')
            if request.url.path == "/api/pull" else httpx.Response(200, json=TAGS))

    def broken(settings, **kw):
        raise ValueError("the engine cannot be started")
    monkeypatch.setattr(cli, "store_for", broken)
    data = _disabled(tmp_path)
    result = CliRunner().invoke(cli.app, ["--data-dir", str(data), "models", "pull"])
    assert result.exit_code != 0
    assert Settings.load(data).knowledge_agent == "enabled"  # the models are here, so it is on ...
    assert first_start.TURNED_ON.format(config=data / "config.json") in " ".join(result.stderr.split())  # ... and said


def test_a_failed_models_pull_leaves_the_agent_disabled(monkeypatch, tmp_path):
    _server(monkeypatch, _down)
    monkeypatch.setattr(cli, "store_for", lambda settings, **kw: FakeStore())
    data = _disabled(tmp_path)
    result = CliRunner().invoke(cli.app, ["--data-dir", str(data), "models", "pull"])
    assert result.exit_code != 0
    assert Settings.load(data).knowledge_agent == "disabled"


def _model_server(monkeypatch, reply):
    """An OpenAI-compatible server on 127.0.0.1:1234 that lists the model m and answers every chat with `reply`."""
    real = REAL_ASYNC_CLIENT

    def server(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "m"}]})
        return httpx.Response(200, json={"id": "x", "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                                         "choices": [{"index": 0, "finish_reason": "stop",
                                                      "message": {"role": "assistant", "content": reply}}]})
    monkeypatch.setattr("cairnmind.ollama.httpx.AsyncClient",
                        lambda **kwargs: real(**kwargs, transport=httpx.MockTransport(server)))


SET_CHAT = ["models", "set", "--role", "chat", "--provider", "openai_compatible",
            "--base-url", "http://127.0.0.1:1234/v1", "--model", "m"]


def test_models_set_turns_the_agent_on_only_after_its_check(monkeypatch, tmp_path):
    data = _disabled(tmp_path)
    _model_server(monkeypatch, "not json")
    failed = CliRunner().invoke(cli.app, ["--data-dir", str(data), *SET_CHAT])
    assert failed.exit_code != 0
    assert Settings.load(data).knowledge_agent == "disabled"
    _model_server(monkeypatch, '{"ok": true}')
    passed = CliRunner().invoke(cli.app, ["--data-dir", str(data), *SET_CHAT])
    assert passed.exit_code == 0, passed.output
    assert json.loads(passed.stdout)["knowledge_agent"] == first_start.TURNED_ON.format(config=data / "config.json")
    saved = Settings.load(data)
    assert saved.knowledge_agent == "enabled" and saved.memory_chat["base_url"] == "http://127.0.0.1:1234/v1"


# --- doctor, the installer's view, the evaluations and rebuild-vectors ------------------------------------------

def _quiet_doctor(monkeypatch):
    from test_tmux_optional import _quiet_doctor as quiet
    quiet(monkeypatch)


def _store_choice(tmp_path, value):
    data = tmp_path / "memory"
    data.mkdir(exist_ok=True)
    document = {"port": 1, "arcade_url": "http://127.0.0.1:1"}
    if value is not None:
        document["knowledge_agent"] = value
    (data / "config.json").write_text(json.dumps(document))
    return data


def test_doctor_says_disabled_as_information_and_exits_0(monkeypatch, tmp_path):
    _quiet_doctor(monkeypatch)
    reports = {}
    for value in ("disabled", "enabled", "", None, "maybe"):
        data = _store_choice(tmp_path, value)
        result = CliRunner().invoke(cli.app, ["--data-dir", str(data), "doctor"])
        assert result.exit_code == 0, result.output
        reports[value] = json.loads(result.stdout)
    assert reports["disabled"]["knowledge_agent"] == "disabled (deterministic-only mode)"
    assert reports["disabled"]["get_models"] == first_start.GET_MODELS
    assert reports["maybe"]["knowledge_agent"].startswith("knowledge_agent in config.json is 'maybe'")
    for configured in ("enabled", "", None):  # the output of a configured installation is what it was
        assert "knowledge_agent" not in reports[configured] and "get_models" not in reports[configured]
    assert reports["enabled"] == reports[""] == reports[None]


def test_the_installer_view_lists_no_model_line_in_deterministic_mode_and_keeps_the_fault():
    from cairnmind import doctor_attention
    healthy = {"python": "3.12.3", "java": {"on_path": True, "executable": "/usr/bin/java", "compatible": True},
               "arcadedb": {"available": True, "database_available": True},
               "ollama": {"available": False, "url": "http://127.0.0.1:11434"},
               "memory_models": {"chat": {"reachable": False, "base_url": "http://127.0.0.1:11434"},
                                 "embedding": {"reachable": False, "base_url": "http://127.0.0.1:1234/v1"}},
               "service": {"available": False}, "environment_notes": [], "data_dir": "/d",
               "store_writer": {"role": "writer"}}
    attention = doctor_attention.attention
    off = {"knowledge_agent": first_start.KNOWLEDGE_DISABLED, "get_models": first_start.GET_MODELS}
    assert attention({**healthy, **off}) == []  # information, said by init and by doctor itself
    assert attention(healthy)[0] == "Ollama does not answer; local models need `ollama serve`."  # a fault, as before
    assert len(attention(healthy)) == 2
    invalid = "knowledge_agent in config.json is 'maybe'; it must be enabled or disabled."
    assert attention({**healthy, "knowledge_agent": invalid})[0] == invalid


EVALUATIONS = [(["eval", "run"], "cairnmind eval run", "cairnmind.evaluation.run_evaluation"),
               (["eval", "memory"], "cairnmind eval memory", "cairnmind.memory_evaluation.run_memory_evaluation"),
               (["eval", "extraction"], "cairnmind eval extraction",
                "cairnmind.extraction_evaluation.run_extraction_evaluation"),
               (["eval", "repository", "."], "cairnmind eval repository",
                "cairnmind.repository_evaluation.run_repository_evaluation"),
               (["eval", "replay", "demo"], "cairnmind eval replay", "cairnmind.answer_log.replay")]


@pytest.mark.parametrize("arguments, command, target", EVALUATIONS)
def test_evaluations_report_not_run_without_models(monkeypatch, tmp_path, arguments, command, target):
    def asked(*args, **kwargs):
        raise AssertionError("an evaluation asked a model or the service in deterministic-only mode")
    monkeypatch.setattr(target, asked)
    monkeypatch.setattr(cli, "request", asked)
    monkeypatch.setattr(cli, "store_for_read", asked)
    data = _store_choice(tmp_path, "disabled")
    result = CliRunner().invoke(cli.app, ["--data-dir", str(data), *arguments])
    assert result.exit_code == 3, result.output or repr(result.exception)
    report = json.loads(result.stdout)
    assert report["status"] == "not_run"
    assert report["reason"] == first_start.NEEDS_MODELS.format(command=command) + " " + first_start.GET_MODELS


def test_rebuild_vectors_refuses_without_models(monkeypatch, tmp_path):
    def asked(*args, **kwargs):
        raise AssertionError("rebuild-vectors touched the store or a model in deterministic-only mode")
    monkeypatch.setattr(cli, "store_for", asked)
    data = _store_choice(tmp_path, "disabled")
    result = CliRunner().invoke(cli.app, ["--data-dir", str(data), "rebuild-vectors", "demo"])
    assert result.exit_code == 2, result.output
    assert " ".join(result.stderr.split()) == "Error: " + first_start.NEEDS_MODELS.format(
        command="cairnmind rebuild-vectors") + " " + first_start.GET_MODELS
