"""Real ArcadeDB recall fixtures with controlled local-model/native adapters."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from cairnmind import __version__
from cairnmind.config import Settings
from cairnmind.evaluation import (
    GoldenCase, MeasuredOllama, _case_answer_schema, _native_client, _native_failure,
    run_evaluation, score_answer,
)
from cairnmind.ollama import OllamaUnavailable
from cairnmind.store import Store


class FixtureOllama:
    def __init__(self, *, corrupt=False, tokens=True):
        self.corrupt = corrupt
        self.tokens = tokens

    async def model_identity(self, name):
        return name + "@controlled-fixture"

    async def embed(self, texts):
        return [[1., .2, .1, .3] for _ in texts]

    async def chat(self, messages, **kwargs):
        prompt = json.loads(messages[1]["content"])
        key = re.search(r"recall\d+_\w+", prompt["question"])[0]
        if "initial_evidence" in prompt:
            hits = list(prompt["initial_evidence"])
        else:
            # Compact final repair has no discovery history. Consume only the
            # inspected literal cards the actual model receives in that pass.
            assert kwargs.get("tools") is None
            hits = [{"source_id": item["source_id"], "text": item["quote_excerpt"]}
                    for item in prompt["citation_catalog"]]
        for message in messages[2:]:
            if message.get("role") == "tool":
                hits += json.loads(message["content"]).get("hits", [])
        selected = None
        for hit in hits:
            try:
                card = json.loads(hit.get("text", ""))
            except ValueError:
                continue
            if card.get("case") == key:
                selected = hit, card
                break
        if selected is None:
            response = {"message": {"content": "", "tool_calls": [{"function": {
                "name": "search", "arguments": {"query": key}}}]}}
            if self.tokens:
                response.update(prompt_eval_count=40, eval_count=12)
            return response
        hit, card = selected
        facts = dict(card["facts"])
        if self.corrupt and key == "recall04_task_id":
            facts["id"] = "invented-id"
        response = {"message": {"content": json.dumps({"answer": facts if prompt.get("answer_schema") else json.dumps(facts),
            "citations": [{"source_id": hit["source_id"], "quote": hit["text"]}],
            "unknowns": card.get("unknowns", [])})}}
        if self.tokens:
            response.update(prompt_eval_count=40, eval_count=12)
        return response


class MissingModels:
    async def model_identity(self, name):
        raise OllamaUnavailable("Required fixture model is not installed")


class MissingEmbeddings(FixtureOllama):
    async def embed(self, texts):
        raise OllamaUnavailable("Safe embedding runtime failure", status_code=503,
                                endpoint="/api/embed", error_code="runtime_busy")


def test_twenty_real_storage_cases_measure_usage_and_leave_user_database_untouched(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "user-data", create=True)
    store = Store(settings.data_dir)
    root = tmp_path / "user-project"
    root.mkdir()
    store.add_project("User project", root)
    before = store.export()
    monkeypatch.setenv("DEVBRAIN_ARCADE_DATABASE", settings.arcade_database)
    report = run_evaluation(settings, ollama=FixtureOllama())
    assert report["status"] == "passed"
    assert report["cairnmind_version"] == __version__
    assert report["evaluation_mode"] == "typed_caller_contract"
    assert report["factual_accuracy"] == 1
    assert report["critical_facts_passed"] is True
    assert len(report["scenarios"]) == 20
    assert all(report["deterministic_checks"].values())
    assert all(case["context_bytes_max"] > 0 for case in report["scenarios"])
    assert all(case["prompt_tokens"] >= 40 for case in report["scenarios"])
    assert all(case["answer_schema"]["type"] == "object" for case in report["scenarios"])
    assert all(isinstance(case["answer"]["answer"], str) for case in report["scenarios"])
    after = store.export()
    before.pop("exported_at", None)
    after.pop("exported_at", None)
    assert after == before
    path = Path(report["report_path"])
    assert path.is_file()
    assert path.stat().st_mode & 0o777 == 0o600
    serialized = path.read_text()
    assert settings.arcade_password not in serialized
    assert settings.token not in serialized
    assert not any("cairnmind_eval_" in str(row) for row in store.query("SELECT FROM projects"))


def test_critical_id_error_fails_even_above_ninety_percent(tmp_path):
    settings = Settings.load(tmp_path / "data", create=True)
    report = run_evaluation(settings, ollama=FixtureOllama(corrupt=True, tokens=False))
    assert report["factual_accuracy"] == .95
    assert report["status"] == "failed"
    assert report["critical_facts_passed"] is False
    assert all(case["prompt_tokens"] is None for case in report["scenarios"])
    assert all(case["completion_tokens"] is None for case in report["scenarios"])
    diagnostic = next(case for case in report["scenarios"] if case["id"] == "recall04_task_id")
    assert diagnostic["model_call_count"] == len(diagnostic["model_calls"])
    assert "invented-id" in diagnostic["model_calls"][-1]["candidate"]["content"]
    assert diagnostic["model_calls"][-1]["transport_status"] == "completed"


@pytest.mark.parametrize("unavailable_model", [MissingModels(), MissingEmbeddings()], ids=["model", "embedding"])
def test_missing_models_are_not_run_and_native_clients_are_explicit(tmp_path, unavailable_model):
    settings = Settings.load(tmp_path / "data", create=True)
    report = run_evaluation(settings, ollama=unavailable_model, client_runner=lambda *args: 1 / 0)
    assert report["status"] == "not_run"
    assert report["attempted"] == 0
    assert report["factual_accuracy"] is None
    assert {case["status"] for case in report["scenarios"]} == {"not_run"}
    assert all(case["model_call_count"] == 0 and case["model_calls"] == [] for case in report["scenarios"])
    assert all(case["prompt_tokens"] is None and case["completion_tokens"] is None for case in report["scenarios"])
    assert all(case["error"]["type"] == "OllamaUnavailable" for case in report["scenarios"])
    if isinstance(unavailable_model, MissingEmbeddings):
        assert all(case["error"] == {"type": "OllamaUnavailable", "status_code": 503,
            "endpoint": "/api/embed", "error_code": "runtime_busy"} for case in report["scenarios"])
    assert report["native_clients"][0]["status"] == "not_run"


def test_explicit_client_handoff_uses_fresh_scoped_versions_and_only_temp_worktree(tmp_path):
    settings = Settings.load(tmp_path / "data", create=True)
    calls = []

    def runner(client, root, isolated, project, expected, *, session):
        calls.append((client, root, isolated.arcade_database, expected))
        assert root.name == "worktree"
        assert (root / ".git").is_file()
        assert isolated.arcade_database != settings.arcade_database
        from cairnmind.sessions import SessionManager

        fixture_store = Store(isolated.data_dir, settings=isolated)
        manager = SessionManager(fixture_store, isolated)
        packet = session["packet"]
        assert packet["active_plan"]
        assert packet["active_task"]["id"] == expected["task_id"]
        readback = {key: {"id": packet["active_" + key]["id"], "version": packet["active_" + key]["version"]}
                    if packet.get("active_" + key) else None for key in ("plan", "task")}
        manager.ack(session["session_id"], packet["id"], packet["hash"], readback)
        return {"status": "passed", "answer": expected, "usage": None}

    report = run_evaluation(settings, ollama=MissingModels(), clients=True, client_runner=runner)
    assert [call[0] for call in calls] == ["codex", "claude", "codex"]
    assert [call[3]["version"] for call in calls] == [1, 2, 2]
    assert [call[3]["state"] for call in calls] == ["todo", "needs_test", "needs_test"]
    assert all(case["fresh_process"] for case in report["native_clients"])
    assert [case["previous_ack_invalidated"] for case in report["native_clients"]] == [None, True, True]
    assert {case["status"] for case in report["native_clients"]} == {"passed"}
    assert report["native_mode"] == "full_handoff"
    assert report["full_client_handoff_passed"] is True
    assert [case["handoff_from"] for case in report["native_clients"]] == [None, "codex", "claude"]
    assert [case["compaction_client"] for case in report["native_clients"]] == [None, None, "claude"]
    assert report["status"] == "not_run"  # Vendor success cannot invent local-model results.
    assert not calls[0][1].exists()


def test_unknown_requires_real_scoped_abstention_and_isolation():
    case = GoldenCase("unknown", "?", {"answer": "unknown"}, True, "source", True)
    good = {"answer": '{"answer":"unknown"}', "status": "partial", "unknowns": ["Not stored"],
            "sources": [{"id": "source"}]}
    assert score_answer(case, good, private_marker="SECRET")[0]
    assert not score_answer(case, {**good, "answer": "Generic invalid model result"}, private_marker="SECRET")[0]
    assert not score_answer(case, {**good, "unknowns": ["SECRET"]}, private_marker="SECRET")[0]


def test_case_contract_contains_types_and_field_names_without_expected_fact_values():
    expected = {"id": "private-task-uuid", "version": 90210, "approved": True,
                "state": "private-status", "depends_on": ["private-dependency"],
                "acceptance_criteria": ["private-criterion"], "answer": "private-abstention-value"}
    schema = _case_answer_schema(GoldenCase("test", "?", expected))
    assert schema == {"type": "object", "properties": {
        "id": {"type": "string"}, "version": {"type": "integer"}, "approved": {"type": "boolean"},
        "state": {"type": "string"}, "depends_on": {"type": "array", "items": {"type": "string"}},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}}, "answer": {"type": "string"}},
        "required": list(expected), "additionalProperties": False}
    encoded = json.dumps(schema)
    assert all(value not in encoded for value in (
        "private-task-uuid", "90210", "private-status", "private-dependency",
        "private-criterion", "private-abstention-value", '"const"', '"enum"', '"default"'))
    different = {"id": "other-task", "version": 4, "approved": False, "state": "other-state",
                 "depends_on": [], "acceptance_criteria": ["different"], "answer": "something else"}
    assert _case_answer_schema(GoldenCase("test", "?", different)) == schema
    schema["properties"]["id"]["type"] = "integer"
    assert _case_answer_schema(GoldenCase("test", "?", expected))["properties"]["id"]["type"] == "string"


def test_replay_nested_unknown_answer_repairs_shape_with_real_citation_and_no_golden_value_in_schema(tmp_path):
    class NestedUnknown(FixtureOllama):
        injected = False
        repair_schema = None

        async def chat(self, messages, **kwargs):
            response = await super().chat(messages, **kwargs)
            prompt = json.loads(messages[1]["content"])
            if "recall18_isolation" not in prompt["question"] or not response["message"].get("content"):
                return response
            contract = prompt["answer_schema"]
            assert contract == {"type": "object", "properties": {"answer": {"type": "string"}},
                                "required": ["answer"], "additionalProperties": False}
            output = json.loads(response["message"]["content"])
            if not self.injected:
                self.injected = True
                # Exact decoded shape from the user's failed 0.3.1 report. The
                # source and quote remain genuine current-fixture references.
                output["answer"] = json.dumps({"answer": {"secret_marker": "unknown"}})
            else:
                self.repair_schema = kwargs["format"]
                assert self.repair_schema["properties"]["answer"] == contract
                assert kwargs.get("tools") is None
                assert "initial_evidence" not in prompt
                allowed = self.repair_schema["properties"]["citations"]["items"]["properties"]["source_id"]["enum"]
                assert set(allowed) == {item["source_id"] for item in prompt["citation_catalog"]}
                assert all(message["role"] != "tool" for message in messages)
                assert output["citations"][0]["source_id"] in allowed
                assert any(output["citations"][0]["quote"] == item["quote_excerpt"]
                           for item in prompt["citation_catalog"]
                           if item["source_id"] == output["citations"][0]["source_id"])
                assert isinstance(output["answer"], dict)
            response["message"]["content"] = json.dumps(output)
            return response

    model = NestedUnknown()
    settings = Settings.load(tmp_path / "data", create=True)
    report = run_evaluation(settings, ollama=model)
    assert report["status"] == "passed" and report["factual_accuracy"] == 1
    assert model.injected and model.repair_schema is not None
    case = next(item for item in report["scenarios"] if item["id"] == "recall18_isolation")
    assert case["status"] == "passed" and case["answer"]["status"] == "partial"
    assert isinstance(case["answer"]["answer"], str)
    assert json.loads(case["answer"]["answer"]) == {"answer": "unknown"}
    assert case["answer"]["unknowns"] and case["answer"]["sources"]
    candidates = [json.loads(call["candidate"]["content"]) for call in case["model_calls"]
                  if call.get("candidate", {}).get("content")]
    assert len(candidates) == 2
    assert json.loads(candidates[0]["answer"]) == {"answer": {"secret_marker": "unknown"}}
    assert candidates[-1]["answer"] == {"answer": "unknown"}
    assert case["model_call_count"] == len(case["model_calls"])


def test_native_clients_missing_auth_and_no_api_key_fallback(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data", create=True)
    monkeypatch.setattr("cairnmind.evaluation.shutil.which", lambda name: "/usr/bin/" + name)
    monkeypatch.setenv("OPENAI_API_KEY", "never-forward-this")
    monkeypatch.setenv("CODEX_API_KEY", "never-forward-this")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "never-forward-this")
    calls = []

    def command(argv, **kwargs):
        calls.append((argv, kwargs))
        assert "OPENAI_API_KEY" not in kwargs["env"]
        assert "CODEX_API_KEY" not in kwargs["env"]
        assert "ANTHROPIC_API_KEY" not in kwargs["env"]
        return SimpleNamespace(returncode=1, stdout="", stderr="Login required")

    monkeypatch.setattr("cairnmind.evaluation.subprocess.run", command)
    assert _native_client("codex", tmp_path, settings, "project", {})["status"] == "not_run"
    assert _native_client("claude", tmp_path, settings, "project", {})["status"] == "not_run"
    assert len(calls) == 2


def test_native_subprocesses_use_read_only_controls_and_report_real_usage(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data", create=True)
    settings.arcade_database = "cairnmind_eval_fixture"
    monkeypatch.setattr("cairnmind.evaluation.shutil.which", lambda name: "/usr/bin/" + name)
    expected = {"task_id": "t", "version": 2, "state": "needs_test"}
    calls = []

    def command(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ["login", "status"]:
            return SimpleNamespace(returncode=0, stdout="Logged in using ChatGPT", stderr="")
        if argv[1:3] == ["auth", "status"]:
            return SimpleNamespace(returncode=0, stdout='{"loggedIn":true,"authMethod":"claude.ai"}', stderr="")
        if "exec" in argv:
            assert argv[argv.index("--sandbox") + 1] == "read-only"
            assert "--ephemeral" in argv
            overrides = [argv[index + 1] for index, value in enumerate(argv) if value == "-c"]
            assert 'approval_policy="never"' in overrides
            assert not any(value.startswith("ask_for_approval=") for value in overrides)
            output = '\n'.join([json.dumps({"type": "item.completed", "item": {"type": "agent_message",
                "text": json.dumps(expected)}}), json.dumps({"type": "turn.completed",
                "usage": {"input_tokens": 10, "output_tokens": 5}})])
        else:
            assert "--strict-mcp-config" in argv
            assert argv[argv.index("--tools") + 1] == ""
            assert "--no-session-persistence" in argv
            output = json.dumps({"result": json.dumps(expected), "usage": {"input_tokens": 11}})
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr("cairnmind.evaluation.subprocess.run", command)
    for client in ("codex", "claude"):
        result = _native_client(client, tmp_path, settings, "project", expected)
        assert result["status"] == "passed"
        assert result["usage"]["input_tokens"] > 0
        assert result["context_size"] is None
    assert all("dangerously" not in " ".join(argv) for argv in calls)


def test_native_correct_echo_without_actual_ack_never_passes(tmp_path):
    settings = Settings.load(tmp_path / "data", create=True)
    calls = []

    def echo(client, root, isolated, project, expected, *, session):
        calls.append(client)
        return {"status": "passed", "answer": expected}

    report = run_evaluation(settings, ollama=MissingModels(), clients=True, client_runner=echo)
    assert report["status"] == "failed"
    assert calls == ["codex"]
    assert [phase["status"] for phase in report["native_clients"]] == ["failed", "not_run", "not_run"]
    assert "ACK" in report["native_clients"][0]["reason"]
    assert all(phase["fresh_process"] is False and phase["blocked_by_phase"] == 1
               for phase in report["native_clients"][1:])
    assert report["full_client_handoff_passed"] is False


@pytest.mark.parametrize("returncode,message", [
    (1, "You've hit your limit · resets later"),
    (0, "You've hit your limit · resets later"),
    (1, "You’ve hit your limit"),
    (0, "You have hit your limit"),
    (1, "Usage limit reached"),
    (1, "rate_limit_error"),
])
def test_claude_account_limit_is_not_run_without_raw_error_output(returncode, message):
    stdout = json.dumps({"is_error": True, "result": message + " PRIVATE_DIAGNOSTIC", "usage": {"input_tokens": 17}})
    result = _native_failure("claude", stdout, "", returncode)
    assert result["status"] == "not_run"
    assert result["error_code"] == "usage_limit"
    assert result["usage"]["input_tokens"] == 17
    assert result["process_started"] is True
    assert "PRIVATE_DIAGNOSTIC" not in json.dumps(result)


def test_claude_structured_limit_and_plain_error_are_classified_conservatively():
    structured = json.dumps({"is_error": True, "error": {"type": "rate_limit_error", "message": "private detail"}})
    assert _native_failure("claude", structured, "", 1)["error_code"] == "usage_limit"
    plain = _native_failure("claude", "", "You've hit your limit", 1)
    assert plain["status"] == "not_run" and plain["error_code"] == "usage_limit"
    length = _native_failure("claude", json.dumps({"is_error": True,
        "result": "Maximum context length exceeded; too many input tokens"}), "", 1)
    assert length["status"] == "failed" and length["error_code"] == "context_length"
    unknown = _native_failure("claude", "", "PRIVATE_AUTH_URL unexpected subprocess failure", 1)
    assert unknown["status"] == "failed" and unknown["error_code"] == "native_process_error"
    assert "PRIVATE_AUTH_URL" not in json.dumps(unknown)


def test_attempted_wrong_fact_is_not_hidden_by_quota_looking_text():
    stdout = json.dumps({"is_error": True, "result": json.dumps({"task_id": "wrong", "version": 1,
        "state": "todo", "error": "You've hit your limit"})})
    result = _native_failure("claude", stdout, "", 1)
    assert result["status"] == "failed" and result["error_code"] == "native_process_error"
    assert _native_failure("claude", json.dumps({"is_error": False, "result": "You've hit your limit"}), "", 0) is None


@pytest.mark.parametrize("has_fact,expected_status", [(False, "not_run"), (True, "failed")])
def test_codex_model_reported_approval_block_is_not_a_success(tmp_path, monkeypatch, has_fact, expected_status):
    settings = Settings.load(tmp_path / "data", create=True)
    monkeypatch.setattr("cairnmind.evaluation.shutil.which", lambda name: "/usr/bin/codex")
    expected = {"task_id": "actual-task", "version": 1, "state": "todo"}
    actual = {"task_id": "wrong-task" if has_fact else None, "version": None, "state": None,
              "error": "resume_context requires approval, but approval policy is never PRIVATE_DETAIL"}

    def command(argv, **kwargs):
        if "exec" not in argv:
            return SimpleNamespace(returncode=0, stdout="Logged in using ChatGPT", stderr="")
        return SimpleNamespace(returncode=0, stderr="", stdout="\n".join([
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(actual)}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 20}})]))

    monkeypatch.setattr("cairnmind.evaluation.subprocess.run", command)
    result = _native_client("codex", tmp_path, settings, "project", expected)
    assert result["status"] == expected_status
    if not has_fact:
        assert result["error_code"] == "approval_required"
        assert "PRIVATE_DETAIL" not in json.dumps(result)
        assert result["usage"]["input_tokens"] == 20


def _ack_native_fixture(isolated, session):
    from cairnmind.sessions import SessionManager
    packet = session["packet"]
    readback = {key: {"id": packet["active_" + key]["id"], "version": packet["active_" + key]["version"]}
                if packet.get("active_" + key) else None for key in ("plan", "task")}
    SessionManager(Store(isolated.data_dir, settings=isolated), isolated).ack(
        session["session_id"], packet["id"], packet["hash"], readback)


def test_skip_claude_runs_only_codex_and_never_claims_full_handoff(tmp_path):
    settings = Settings.load(tmp_path / "data", create=True)
    calls = []
    def runner(client, root, isolated, project, expected, *, session):
        assert client == "codex"  # No Claude runner, binary probe or auth can be invoked.
        calls.append((client, expected["version"], expected["state"]))
        _ack_native_fixture(isolated, session)
        return {"status": "passed", "answer": expected}
    report = run_evaluation(settings, clients=True, skip_claude=True, ollama=FixtureOllama(), client_runner=runner)
    assert report["factual_accuracy"] == 1
    assert report["status"] == "not_run"
    assert report["native_mode"] == "codex_only_partial"
    assert report["full_client_handoff_passed"] is False
    assert calls == [("codex", 1, "todo"), ("codex", 2, "needs_test")]
    phases = report["native_clients"]
    assert [phase["status"] for phase in phases] == ["passed", "not_run", "passed"]
    assert phases[1]["fresh_process"] is False and phases[1]["session_id"] is None
    assert phases[1]["error_code"] == "skipped_by_request"
    assert phases[2]["handoff_from"] == "codex"
    assert all(phase["compaction_client"] is None for phase in phases)


def test_native_quota_blocks_later_paid_client_processes(tmp_path):
    settings = Settings.load(tmp_path / "data", create=True)
    calls = []
    def runner(client, root, isolated, project, expected, *, session):
        calls.append(client)
        if client == "claude":
            return {"status": "not_run", "error_code": "usage_limit", "reason": "Account limit reached"}
        _ack_native_fixture(isolated, session)
        return {"status": "passed", "answer": expected}
    report = run_evaluation(settings, clients=True, ollama=MissingModels(), client_runner=runner)
    assert calls == ["codex", "claude"]
    assert [phase["status"] for phase in report["native_clients"]] == ["passed", "not_run", "not_run"]
    assert report["native_clients"][2]["blocked_by_phase"] == 2
    assert report["native_clients"][2]["fresh_process"] is False
    assert report["full_client_handoff_passed"] is False
    assert report["status"] == "not_run"


def test_skip_claude_requires_explicit_native_clients_before_creating_output(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    report_dir = tmp_path / "reports"
    with pytest.raises(ValueError, match="requires"):
        run_evaluation(settings, skip_claude=True, report_dir=report_dir)
    assert not report_dir.exists()


async def test_model_diagnostics_bound_candidates_and_count_response_schema_without_prompt_dump():
    messages = [{"role": "system", "content": "PROMPT_SECRET_DO_NOT_LOG"},
                {"role": "user", "content": json.dumps({"initial_evidence": [
                    {"source_id": "scoped-source", "uri": "fixture://źródło", "text": "SOURCE_TEXT_DO_NOT_LOG"}
                ]}, ensure_ascii=False)}]
    response = {"message": {"content": "ź" * 6000,
        "thinking": "THINKING_DO_NOT_LOG", "tool_calls": [{"function": {
            "name": "read_source", "arguments": {"source_id": "scoped-source", "query": "x" * 2000}}}
            for _ in range(8)]}, "done_reason": "length", "prompt_eval_count": 57, "eval_count": 1200}
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    tools = [{"type": "function", "function": {"name": "read_source"}}]

    class Adapter:
        async def chat(self, actual_messages, **kwargs):
            assert actual_messages is messages
            assert kwargs == {"tools": tools, "format": schema}
            return response

    measured = MeasuredOllama(Adapter())
    assert await measured.chat(messages, tools=tools, format=schema) is response
    call, = measured.calls
    encoded = json.dumps({"messages": messages, "tools": tools, "format": schema}, ensure_ascii=False)
    assert call["context_chars"] == len(encoded)
    assert call["context_bytes"] == len(encoded.encode())
    assert call["context_bytes"] > call["context_chars"]
    assert call["initial_evidence"] == [{"source_id": "scoped-source", "uri": "fixture://źródło"}]
    assert call["initial_evidence_total"] == 1
    assert call["initial_evidence_truncated"] is False
    assert call["transport_status"] == "completed"
    assert call["prompt_eval_count"] == 57 and call["eval_count"] == 1200
    candidate = call["candidate"]
    assert candidate["content"] == response["message"]["content"][:MeasuredOllama.CONTENT_LIMIT]
    assert candidate["content_chars"] == 6000 and candidate["content_truncated"] is True
    assert candidate["tool_calls_total"] == 8 and candidate["tool_calls_truncated"] is True
    assert len(candidate["tool_calls"]) == MeasuredOllama.TOOL_LIMIT
    assert all(item["arguments_truncated"] for item in candidate["tool_calls"])
    assert all(len(item["arguments_json"]) == MeasuredOllama.ARGUMENTS_LIMIT for item in candidate["tool_calls"])
    assert candidate["done_reason"] == "length"
    serialized = json.dumps(call)
    assert all(secret not in serialized for secret in (
        "PROMPT_SECRET_DO_NOT_LOG", "SOURCE_TEXT_DO_NOT_LOG", "THINKING_DO_NOT_LOG"))


async def test_model_diagnostics_record_safe_transport_failure_and_rethrow_without_retry():
    failure = OllamaUnavailable("HTTP_BODY_SECRET_DO_NOT_LOG", status_code=500,
                                endpoint="/api/chat", error_code="runner_failed")
    calls = []

    class Adapter:
        async def chat(self, messages, **kwargs):
            calls.append(messages)
            raise failure

    measured = MeasuredOllama(Adapter())
    with pytest.raises(OllamaUnavailable) as caught:
        await measured.chat([{"role": "user", "content": "PROMPT_SECRET_DO_NOT_LOG"}])
    assert caught.value is failure and len(calls) == 1
    call, = measured.calls
    assert call["transport_status"] == "error"
    assert call["error"] == {"type": "OllamaUnavailable", "status_code": 500,
                             "endpoint": "/api/chat", "error_code": "runner_failed"}
    assert "prompt_eval_count" not in call and "eval_count" not in call and "candidate" not in call
    assert "HTTP_BODY_SECRET_DO_NOT_LOG" not in json.dumps(call)
    assert "PROMPT_SECRET_DO_NOT_LOG" not in json.dumps(call)


async def test_model_diagnostics_record_cancellation_without_invented_usage():
    class Adapter:
        async def chat(self, messages, **kwargs):
            raise asyncio.CancelledError()

    measured = MeasuredOllama(Adapter())
    with pytest.raises(asyncio.CancelledError):
        await measured.chat([])
    call, = measured.calls
    assert call["transport_status"] == "cancelled"
    assert call["error"]["type"] == "CancelledError"
    assert "prompt_eval_count" not in call and "eval_count" not in call


def test_real_evaluation_records_first_http_failure_without_hiding_unattempted_case(tmp_path):
    class FirstRequestFails(FixtureOllama):
        first = True

        async def chat(self, messages, **kwargs):
            if self.first:
                self.first = False
                raise OllamaUnavailable("Safe classified runtime failure", status_code=500,
                                        endpoint="/api/chat", error_code="memory_exhausted")
            return await super().chat(messages, **kwargs)

    settings = Settings.load(tmp_path / "data", create=True)
    report = run_evaluation(settings, ollama=FirstRequestFails())
    assert report["status"] == "not_run" and report["attempted"] == 19
    first = report["scenarios"][0]
    assert first["id"] == "recall01_engine" and first["status"] == "not_run"
    assert first["model_call_count"] == 1
    assert first["prompt_tokens"] is None and first["completion_tokens"] is None
    call, = first["model_calls"]
    assert call["error"]["status_code"] == 500 and call["error"]["error_code"] == "memory_exhausted"
    assert call["initial_evidence"]
    assert all(item["status"] == "passed" for item in report["scenarios"][1:])
    assert all(d["candidate"]["content"] for item in report["scenarios"][1:] for d in item["model_calls"]
               if d["transport_status"] == "completed" and not d["candidate"].get("tool_calls"))
