"""Controlled Copilot transport tests; no GitHub login or paid inference."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import json
import logging
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace as NS

import pytest

from devbrain.config import Settings
from devbrain import copilot_adapter as module
from devbrain.copilot_adapter import CopilotAdapter, CopilotError


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: object
    skip_permission: bool
    defer: str


class Approve:
    kind = "approve-once"


class Reject:
    kind = "reject"

    def __init__(self, feedback=None):
        self.feedback = feedback


@dataclass
class Result:
    text_result_for_llm: str
    result_type: str = "success"
    error: str | None = None


SDK = NS(Tool=Tool, Result=Result, Approve=Approve, Reject=Reject)
TOOL = {"name": "read_file", "description": "Read a project file.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"], "additionalProperties": False}}


class Session:
    def __init__(self, client, session_id, options):
        self.client, self.session_id, self.options = client, session_id, options
        self.aborted = self.disconnected = 0

    async def send_and_wait(self, prompt, **kwargs):
        self.client.prompts.append((prompt, kwargs))
        if self.client.scenario:
            return await self.client.scenario(self, prompt, kwargs)
        return NS(data=NS(content="Finished"))

    async def abort(self):
        self.aborted += 1
        if hasattr(self, "stop_event"):
            self.stop_event.set()

    async def disconnect(self):
        self.disconnected += 1


class Client:
    def __init__(self, **kwargs):
        self.options = kwargs
        self.scenario = None
        self.created, self.resumed, self.prompts = [], [], []
        self.started = self.stopped = 0
        self.existing = []
        self.auth = True

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1

    async def list_sessions(self):
        return self.existing

    async def create_session(self, **kwargs):
        row = Session(self, kwargs.pop("session_id"), kwargs)
        self.created.append(row)
        return row

    async def resume_session(self, session_id, **kwargs):
        row = Session(self, session_id, kwargs)
        self.resumed.append(row)
        return row

    async def get_auth_status(self):
        return NS(isAuthenticated=self.auth, login="private-account@example.invalid", statusMessage="private")

    async def list_models(self):
        return [NS(id="model-a", name="Model A", policy=NS(state="enabled")),
                NS(id="model-b", name="Model B", policy=NS(state="disabled"))]


def adapter(tmp_path, scenario=None, *, limits=None, key=None, existing=None):
    clients = []

    def factory(**kwargs):
        client = Client(**kwargs)
        client.scenario = scenario
        client.existing = existing or []
        clients.append(client)
        return client

    profile = {"id": "copilot", "client": "copilot", "model": "model-a", "limits": limits or {}}
    settings = Settings(data_dir=tmp_path / "data")
    result = CopilotAdapter(profile, key, settings=settings, session_id="logical-123", client_factory=factory, sdk=SDK)
    return result, clients


async def noop(name, args):
    return {"ok": True}


@pytest.mark.asyncio
async def test_uses_only_host_custom_tools_and_disables_ambient_authority(tmp_path, monkeypatch):
    monkeypatch.setenv("COPILOT_SKIP_CLI_DOWNLOAD", "0")
    monkeypatch.setenv("OPENAI_API_KEY", "secret-openai")
    monkeypatch.setenv("COPILOT_CLI_DOWNLOAD_BASE_URL", "https://untrusted.invalid")
    subject, clients = adapter(tmp_path, key="sensitive-github-token")
    assert await subject.run_turn("hello", [TOOL], noop, system="Exact Cairnmind instructions") == {
        "text": "Finished", "tool_calls": [], "usage": {}, "copilot_session_id": "devbrain-logical-123"}
    options = clients[0].options
    assert options["mode"] == "empty"
    assert options["github_token"] == "sensitive-github-token"
    assert options["use_logged_in_user"] is False
    assert options["env"]["COPILOT_SKIP_CLI_DOWNLOAD"] == "1"
    assert "OPENAI_API_KEY" not in options["env"]
    assert "COPILOT_CLI_DOWNLOAD_BASE_URL" not in options["env"]
    assert os.environ["COPILOT_SKIP_CLI_DOWNLOAD"] == "0"
    conf = clients[0].created[0].options
    assert conf["available_tools"] == ["custom:read_file"]
    assert conf["excluded_tools"] == ["builtin:*", "mcp:*"]
    assert conf["mcp_servers"] == {}
    assert conf["custom_agents"] == []
    for name in ("enable_skills", "enable_config_discovery", "enable_file_hooks", "enable_host_git_operations",
                 "enable_on_demand_instruction_discovery", "enable_session_store", "enable_session_telemetry"):
        assert conf[name] is False
    assert conf["autopilot"] == {"continuation_enabled": False, "max_continues": 0}
    assert conf["infinite_sessions"] == {"enabled": False}
    assert conf["memory"] == {"enabled": False}
    assert conf["system_message"] == {"mode": "replace", "content": "Exact Cairnmind instructions"}
    await subject.close()
    assert clients[0].stopped == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [None, "explicit-github-token"])
async def test_account_connection_ignores_ambient_byok_configuration(tmp_path, monkeypatch, key):
    ambient = {
        "COPILOT_PROVIDER_BASE_URL": "https://unrelated-provider.invalid/v1",
        "COPILOT_PROVIDER_API_KEY": "unrelated-provider-secret",
        "COPILOT_PROVIDER_BEARER_TOKEN": "unrelated-bearer-secret",
        "COPILOT_PROVIDER_TYPE": "openai",
        "COPILOT_PROVIDER_WIRE_API": "responses",
        "COPILOT_PROVIDER_AZURE_API_VERSION": "unrelated-version",
        "COPILOT_OFFLINE": "true",
    }
    for name, value in ambient.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("GH_TOKEN", "ambient-github-token")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8080")
    subject, clients = adapter(tmp_path, key=key)
    try:
        assert (await subject.auth_status())["authenticated"] is True
        options = clients[0].options
        assert options["use_logged_in_user"] is (key is None)
        assert options.get("github_token") == key
        assert not set(ambient).intersection(options["env"])
        assert options["env"]["HTTPS_PROXY"] == "http://127.0.0.1:8080"
        assert options["env"].get("GH_TOKEN") == ("ambient-github-token" if key is None else None)
        assert {name: os.environ[name] for name in ambient} == ambient
        assert clients[0].created == clients[0].prompts == []
    finally:
        await subject.close()


@pytest.mark.asyncio
async def test_permission_is_exact_scoped_and_never_approves_builtins_or_managed_requests(tmp_path):
    async def scenario(session, prompt, kwargs):
        own = {"session_id": subject.session_id}
        allowed = NS(kind="custom-tool", tool_name="read_file", managed_approval_required=False)
        assert (await subject._permission(allowed, own)).kind == "approve-once"
        requests = [NS(kind="shell", tool_name="read_file"), NS(kind="custom-tool", tool_name="bash"),
                    NS(kind="custom-tool", tool_name="read_file", managed_approval_required=True)]
        for request in requests:
            assert (await subject._permission(request, own)).kind == "reject"
        assert (await subject._permission(allowed, {"session_id": "another-session"})).kind == "reject"
        assert (await subject._pre_tool({"toolName": "bash"}, own))["permissionDecision"] == "deny"
        assert (await subject._block_subagent({}, own))["decision"] == "block"
        return NS(data=NS(content="ok"))

    subject, _ = adapter(tmp_path, scenario)
    await subject.run_turn("hello", [TOOL], noop)
    assert (await subject._permission(NS(kind="custom-tool", tool_name="read_file"),
                                     {"session_id": subject.session_id})).kind == "reject"
    await subject.close()


@pytest.mark.asyncio
async def test_handler_receives_model_arguments_and_real_call_id_once(tmp_path):
    received, results = [], []

    async def scenario(session, prompt, kwargs):
        tool = session.options["tools"][0]
        good = NS(session_id=session.session_id, tool_call_id="call-1", agent_id=None, arguments={"path": "code.py"})
        invocations = [good, good, NS(session_id="foreign", tool_call_id="call-2", arguments={"path": "x"}),
                       NS(session_id=session.session_id, tool_call_id="call-3", arguments={"wrong": 1}),
                       NS(session_id=session.session_id, tool_call_id="call-4", agent_id="sub", arguments={"path": "x"})]
        for invocation in invocations:
            results.append(json.loads((await tool.handler(invocation)).text_result_for_llm))
        return NS(data=NS(content="Read"))

    async def handler(name, args, call_id=None):
        received.append((name, args, call_id))
        return {"sha256": "actual-file-digest"}

    subject, _ = adapter(tmp_path, scenario)
    await subject.run_turn("read", [TOOL], handler)
    assert received == [("read_file", {"path": "code.py"}, "call-1")]
    assert results[0] == {"sha256": "actual-file-digest"}
    assert [r["error"] for r in results[1:]] == ["copilot_duplicate_or_missing_tool_call", "copilot_tool_scope_rejected",
                                                   "copilot_invalid_tool_arguments", "copilot_subagent_rejected"]
    await subject.close()


@pytest.mark.asyncio
async def test_handler_typeerror_does_not_repeat_effect(tmp_path):
    effects = []
    observed = []

    async def scenario(session, prompt, kwargs):
        tool = session.options["tools"][0]
        observed.append(await tool.handler(NS(session_id=session.session_id, tool_call_id="effect", arguments={"path": "x"})))
        return NS(data=NS(content="Tool failed"))

    async def handler(name, args):
        effects.append(args)
        raise TypeError("private token from unsafe backend")

    subject, _ = adapter(tmp_path, scenario)
    await subject.run_turn("read", [TOOL], handler)
    assert effects == [{"path": "x"}]
    assert observed[0].result_type == "failure"
    assert json.loads(observed[0].text_result_for_llm) == {"error": "devbrain_tool_failed"}
    assert "private token" not in observed[0].text_result_for_llm
    await subject.close()


@pytest.mark.asyncio
async def test_tool_budget_aborts_and_requires_explicit_continuation(tmp_path):
    received = []

    async def scenario(session, prompt, kwargs):
        tool = session.options["tools"][0]
        for index in range(3):
            await tool.handler(NS(session_id=session.session_id, tool_call_id=f"call-{index}", arguments={"path": "x"}))
        return NS(data=NS(content="Never accepted as successful"))

    async def handler(name, args):
        received.append(args)
        return {"ok": True}

    subject, clients = adapter(tmp_path, scenario, limits={"max_tool_calls": 2})
    with pytest.raises(CopilotError, match="copilot_tool_limit"):
        await subject.run_turn("read", [TOOL], handler)
    assert len(received) == 2
    assert clients[0].created[0].aborted >= 1
    assert len(clients[0].prompts) == 1
    await subject.close()


@pytest.mark.asyncio
async def test_timeout_and_cancel_abort_without_retry(tmp_path):
    async def timeout(session, prompt, kwargs):
        raise TimeoutError

    subject, clients = adapter(tmp_path, timeout)
    with pytest.raises(CopilotError, match="copilot_turn_timeout"):
        await subject.run_turn("work", [], noop)
    assert clients[0].created[0].aborted == 1
    assert len(clients[0].prompts) == 1
    await subject.close()

    began = asyncio.Event()

    async def wait(session, prompt, kwargs):
        began.set()
        await asyncio.Event().wait()

    subject, clients = adapter(tmp_path / "cancel", wait)
    task = asyncio.create_task(subject.run_turn("work", [], noop))
    await began.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert clients[0].created[0].aborted == 1
    assert len(clients[0].prompts) == 1
    await subject.close()


@pytest.mark.asyncio
async def test_cold_resume_reapplies_full_restrictions_and_never_continues_pending_work(tmp_path):
    subject, clients = adapter(tmp_path, existing=[NS(session_id="devbrain-logical-123")])
    await subject.run_turn("resume", [TOOL], noop)
    assert clients[0].created == []
    resumed = clients[0].resumed[0]
    assert resumed.options["continue_pending_work"] is False
    assert resumed.options["allow_transcript_recovery"] is False
    assert resumed.options["available_tools"] == ["custom:read_file"]
    assert resumed.options["enable_file_hooks"] is False
    assert resumed.options["autopilot"]["max_continues"] == 0
    old_id = subject.session_id
    await subject.reset_context()
    assert resumed.disconnected == 1
    assert subject.session_id != old_id
    await subject.run_turn("fresh bounded context", [TOOL], noop)
    assert len(clients[0].created) == 1
    await subject.close()


@pytest.mark.asyncio
async def test_changed_schema_or_system_cannot_reuse_old_allowlist(tmp_path):
    subject, clients = adapter(tmp_path)
    await subject.run_turn("first", [TOOL], noop, system="a")
    first = clients[0].created[0]
    tool = {**TOOL, "description": "Read with a newly narrowed contract."}
    await subject.run_turn("second", [tool], noop, system="a")
    assert first.disconnected == 1
    second = clients[0].created[1]
    await subject.run_turn("third", [tool], noop, system="b")
    assert second.disconnected == 1
    assert len(clients[0].created) == 3
    await subject.close()


@pytest.mark.asyncio
async def test_streaming_usage_is_bounded_public_and_redacts_split_credential(tmp_path):
    key = "github-secret-value-12345678"
    deltas, events = [], []

    async def scenario(session, prompt, kwargs):
        emit = session.options["on_event"]
        for part in ("Before ", key[:10], key[10:], " after"):
            emit(NS(type="assistant.message_delta", data=NS(delta_content=part)))
        emit(NS(type="assistant.usage", data=NS(input_tokens=31, output_tokens=11, cost=0.02, private_token=key)))
        emit(NS(type="assistant.usage", data=NS(input_tokens=float("nan"), output_tokens=-1, cost=None)))
        return NS(data=NS(content="Before " + key + " after"))

    subject, _ = adapter(tmp_path, scenario, key=key)
    reply = await subject.run_turn("stream", [], noop, on_delta=deltas.append, on_event=events.append)
    assert "".join(deltas) == "Before [REDACTED] after"
    assert reply["text"] == "Before [REDACTED] after"
    assert reply["usage"] == {"input_tokens": 31, "output_tokens": 11, "cost": 0.02,
                              "copilot": {"unit": "nano_aiu", "observed_calls": 2,
                                          "reported_calls": 0, "status": "unavailable"}}
    assert key not in json.dumps(events)
    await subject.close()


@pytest.mark.asyncio
async def test_oversized_stream_and_sdk_errors_are_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_MAX_TEXT", 10)

    async def scenario(session, prompt, kwargs):
        session.options["on_event"](NS(type="assistant.message_delta", data=NS(delta_content="x" * 11)))
        return NS(data=NS(content=""))

    subject, _ = adapter(tmp_path, scenario)
    with pytest.raises(CopilotError, match="copilot_output_limit"):
        await subject.run_turn("x", [], noop)
    await subject.close()

    async def error(session, prompt, kwargs):
        raise RuntimeError("HTTP 401 invalid API key gh-secret-value")

    subject, _ = adapter(tmp_path / "err", error)
    with pytest.raises(CopilotError) as observed:
        await subject.run_turn("x", [], noop)
    assert observed.value.code == "copilot_auth_rejected"
    assert "gh-secret-value" not in str(observed.value)
    await subject.close()


@pytest.mark.asyncio
async def test_auth_and_model_list_never_send_inference_or_reveal_account(tmp_path):
    subject, clients = adapter(tmp_path)
    assert await subject.auth_status() == {"authenticated": True, "auth": "signed_in", "code": "ready"}
    assert await subject.list_models() == [{"id": "model-a", "name": "Model A", "enabled": True},
                                            {"id": "model-b", "name": "Model B", "enabled": False}]
    assert clients[0].created == clients[0].prompts == []
    clients[0].auth = False
    assert (await subject.auth_status())["code"] == "copilot_auth_required"
    await subject.close()


@pytest.mark.asyncio
async def test_probe_requires_actual_model_tool_call(tmp_path):
    async def valid(session, prompt, kwargs):
        tool = session.options["tools"][0]
        nonce = tool.parameters["properties"]["nonce"]["enum"][0]
        result = await tool.handler(NS(session_id=session.session_id, tool_call_id="probe", arguments={"nonce": nonce}))
        assert json.loads(result.text_result_for_llm) == {"ok": True}
        return NS(data=NS(content="ok"))

    subject, _ = adapter(tmp_path, valid)
    assert (await subject.probe())["tool_call_verified"] is True
    await subject.close()
    subject, _ = adapter(tmp_path / "empty")
    result = await subject.probe()
    assert result["status"] == "unsupported"
    assert result["tool_call_verified"] is False
    await subject.close()


def make_receipt(settings):
    directory = module._runtime_root(settings)
    bundle = directory / module.RUNTIME_VERSION
    binary = bundle / "prebuilds/linux-x64/copilot"
    binary.parent.mkdir(parents=True, mode=0o700)
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    receipt = directory / "receipt.json"
    receipt.write_text(json.dumps({"sdk_version": module.SDK_VERSION, "runtime_version": module.RUNTIME_VERSION,
                                  "entrypoint": binary.relative_to(bundle).as_posix(), "files": module._tree_manifest(bundle)}))
    receipt.chmod(0o600)
    return binary, receipt


def test_status_and_login_command_are_nonmutating_and_do_not_download(tmp_path, monkeypatch):
    monkeypatch.setattr(module.importlib.metadata, "version", lambda name: module.SDK_VERSION)
    settings = Settings(data_dir=tmp_path / "data")
    assert module.runtime_status(settings)["code"] == "copilot_runtime_missing"
    assert not settings.data_dir.exists()
    binary, receipt = make_receipt(settings)
    before = (receipt.read_bytes(), receipt.stat().st_mtime_ns)
    assert module.runtime_status(settings)["available"] is True
    monkeypatch.setattr(module, "_sdk", lambda: SDK)
    command = module.login_command(settings)
    assert command[-3:] == ["--login", "--data-dir", str(settings.data_dir)]
    assert command[1:4] == ["-I", "-m", "devbrain.copilot_adapter"]
    assert before == (receipt.read_bytes(), receipt.stat().st_mtime_ns)
    binary.write_text("tampered")
    assert module.runtime_status(settings)["code"] == "copilot_runtime_invalid"


def test_runtime_receipt_rejects_symlinks_and_escaping_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(module.importlib.metadata, "version", lambda name: module.SDK_VERSION)
    settings = Settings(data_dir=tmp_path / "data")
    binary, receipt = make_receipt(settings)
    outside = tmp_path / "outside"
    outside.write_text("fake")
    binary.unlink()
    binary.symlink_to(outside)
    assert module.runtime_status(settings)["code"] == "copilot_runtime_invalid"
    binary.unlink()
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    value = json.loads(receipt.read_text())
    value["files"] = module._tree_manifest(module._runtime_root(settings) / module.RUNTIME_VERSION)
    value["entrypoint"] = str(outside)
    receipt.write_text(json.dumps(value))
    assert module.runtime_status(settings)["code"] == "copilot_runtime_invalid"


def test_runtime_verification_detects_changed_bytes_even_when_metadata_collides(tmp_path, monkeypatch):
    monkeypatch.setattr(module.importlib.metadata, "version", lambda name: module.SDK_VERSION)
    settings = Settings(data_dir=tmp_path / "data")
    binary, _ = make_receipt(settings)
    assert module.runtime_status(settings)["available"] is True
    before = binary.stat()
    binary.write_bytes(binary.read_bytes().replace(b"exit 0", b"exit 1"))
    os.utime(binary, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert binary.stat().st_size == before.st_size
    assert binary.stat().st_mtime_ns == before.st_mtime_ns
    # WSL may also retain the same ctime for writes within one clock tick.
    # Reproduce that collision deterministically, independent of test speed.
    real_lstat = type(binary).lstat

    def colliding_lstat(path, *args, **kwargs):
        return before if path == binary else real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(type(binary), "lstat", colliding_lstat)
    assert module.runtime_status(settings)["code"] == "copilot_runtime_invalid"


def test_preparation_is_explicit_pinned_and_child_cannot_override_download_host(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data")
    monkeypatch.setattr(module, "_sdk", lambda: SDK)
    monkeypatch.setattr(module.importlib.metadata, "version", lambda name: module.SDK_VERSION)
    monkeypatch.setenv("COPILOT_CLI_DOWNLOAD_BASE_URL", "https://untrusted.invalid")
    monkeypatch.setenv("COPILOT_CLI_PATH", "/untrusted/runtime")
    observed = []

    def run(command, **kwargs):
        observed.append((command, kwargs))
        assert "--prepare-worker" in command
        assert kwargs["env"]["COPILOT_SKIP_CLI_DOWNLOAD"] == "0"
        assert "COPILOT_CLI_DOWNLOAD_BASE_URL" not in kwargs["env"]
        assert "COPILOT_CLI_PATH" not in kwargs["env"]
        make_receipt(settings)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.prepare_runtime(settings)["prepared"] is True
    assert module.prepare_runtime(settings)["prepared"] is False
    assert len(observed) == 1


def test_concurrent_prepare_returns_already_preparing_without_second_download(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data")
    monkeypatch.setattr(module, "_sdk", lambda: SDK)
    monkeypatch.setattr(module.importlib.metadata, "version", lambda name: module.SDK_VERSION)
    began, release = threading.Event(), threading.Event()
    downloads = []

    def run(command, **kwargs):
        downloads.append(command)
        began.set()
        assert release.wait(5)
        make_receipt(settings)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(module.subprocess, "run", run)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(module.prepare_runtime, settings)
        assert began.wait(5)
        try:
            second = module.prepare_runtime(settings)
            assert second["code"] == "already_preparing"
            assert second["preparing"] is True
            assert second["prepared"] is False
            assert len(downloads) == 1
        finally:
            release.set()
        assert first.result(timeout=5)["prepared"] is True
    assert module.prepare_runtime(settings)["prepared"] is False
    assert len(downloads) == 1


def test_real_pinned_sdk_accepts_restricted_session_configuration(tmp_path):
    # Validate against the installed official dependency, not only our fakes.
    # This creates no client process, login, network request or inference.
    sdk = module._sdk()
    assert module.SDK_VERSION == "1.0.29"
    subject = CopilotAdapter({"client": "copilot"}, settings=Settings(data_dir=tmp_path))
    subject._sdk_api = sdk
    kwargs = subject._session_options([TOOL], "instructions")
    inspect.signature(sdk.Client.create_session).bind(None, session_id="test", **kwargs)
    inspect.signature(sdk.Client.resume_session).bind(None, "test", continue_pending_work=False,
                                                    allow_transcript_recovery=False, **kwargs)
    assert kwargs["tools"][0].name == "read_file"
    assert kwargs["tools"][0].skip_permission is False
    assert kwargs["tools"][0].defer == "never"
    assert sdk.Reject(feedback="denied").kind == "reject"
    assert sdk.Approve().kind == "approve-once"


@pytest.mark.asyncio
async def test_real_sdk_dispatch_encodes_host_result_as_successful_tool_result(tmp_path):
    # Execute the real SDK dispatch/serialization path with a controlled RPC
    # peer. No child process, GitHub login, model prompt or network is used.
    from copilot.session import CopilotSession

    received = []

    class Rpc:
        async def request(self, name, body, **kwargs):
            received.append((name, body))
            return {}

    subject = CopilotAdapter({"client": "copilot"}, settings=Settings(data_dir=tmp_path))
    subject._sdk_api = module._sdk()
    subject._active = subject._accept_tools = True
    subject._allowed = {TOOL["name"]: TOOL}
    effects = []

    async def handler(name, arguments, call_id):
        effects.append((name, arguments, call_id))
        return {"sha256": "digest-of-code", "content": "actual file text"}

    subject._handler = handler
    tool = subject._tool(TOOL)
    session = CopilotSession(subject.session_id, Rpc())
    task = asyncio.create_task(session._execute_tool_and_respond(
        "pending-request-1", "read_file", "real-call-id", {"path": "code.py"}, tool.handler))
    session._pending_external_tools["pending-request-1"] = task
    await task
    assert effects == [("read_file", {"path": "code.py"}, "real-call-id")]
    assert len(received) == 1
    name, body = received[0]
    assert name == "session.tools.handlePendingToolCall"
    assert "error" not in body
    assert body["result"]["resultType"] == "success"
    assert json.loads(body["result"]["textResultForLlm"]) == {"sha256": "digest-of-code", "content": "actual file text"}
    assert not subject._tool_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["interrupt", "close"])
async def test_cancel_and_close_cancel_pending_host_approval_callback(tmp_path, operation):
    began = asyncio.Event()
    cancelled = []
    pending = []

    async def scenario(session, prompt, kwargs):
        session.stop_event = asyncio.Event()
        tool = session.options["tools"][0]
        pending.append(asyncio.create_task(tool.handler(NS(session_id=session.session_id,
                              tool_call_id="waiting-for-user", arguments={"path": "code.py"}))))
        await session.stop_event.wait()
        return NS(data=NS(content=""))

    async def handler(name, arguments, call_id):
        began.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(call_id)

    subject, _ = adapter(tmp_path, scenario)
    turn = asyncio.create_task(subject.run_turn("work", [TOOL], handler))
    await asyncio.wait_for(began.wait(), 2)
    await getattr(subject, operation)()
    await asyncio.wait_for(turn, 2)
    assert cancelled == ["waiting-for-user"]
    assert len(pending) == 1 and pending[0].cancelled()
    assert not subject._tool_tasks
    assert subject._accept_tools is False
    await subject.close()


@pytest.mark.asyncio
async def test_copilot_usage_reports_native_units_partial_coverage_and_deduplicates(tmp_path):
    async def scenario(session, prompt, kwargs):
        emit = session.options["on_event"]
        event = NS(type="assistant.usage", data=NS(input_tokens=17, output_tokens=4,
                   accounting=NS(source_session_id=session.session_id, usage_id="usage-1"),
                   copilot_usage=NS(total_nano_aiu=2500)))
        emit(event)
        emit(event)
        emit(NS(type="assistant.usage", data=NS(input_tokens=3, output_tokens=1)))
        return NS(data=NS(content="ok"))

    subject, _ = adapter(tmp_path, scenario)
    reply = await subject.run_turn("x", [], noop)
    assert reply["usage"]["input_tokens"] == 20
    assert reply["usage"]["output_tokens"] == 5
    assert reply["usage"]["copilot"] == {"total_nano_aiu": 2500, "unit": "nano_aiu",
                                            "observed_calls": 2, "reported_calls": 1, "status": "partial"}
    await subject.close()


def test_sdk_raw_exception_logging_cannot_escape_to_product_log(caplog):
    module._sdk()
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("copilot._jsonrpc").warning("Secret HTTP body: SECRET_API_KEY", exc_info=True)
    assert "SECRET_API_KEY" not in caplog.text


@pytest.mark.asyncio
async def test_device_code_login_uses_only_auth_rpc_and_cancels_unfinished_flow(tmp_path, monkeypatch, capsys):
    class Login:
        def __init__(self):
            self.cancelled = []
            self.advanced = []

        async def begin(self, params, **kwargs):
            assert params.kind.value == "githubDotCom"
            assert params.method.value == "device-code"
            return NS(flow_id="opaque-flow", step=NS(kind="device-code", verification_uri="https://github.com/login/device",
                                                      user_code="ABCD-EFGH"))

        async def advance(self, params, **kwargs):
            self.advanced.append(params)
            return NS(kind="completed", result=NS(status="completed"))

        async def cancel(self, params, **kwargs):
            self.cancelled.append(params.flow_id)

    login = Login()

    class AuthAdapter:
        def __init__(self, *args, **kwargs):
            self._session = NS(rpc=NS(accounts=NS(login=login)))

        async def _ensure_client(self):
            pass

        async def _ensure_session(self, tools, system):
            assert tools == []
            assert "No model turn" in system

        async def close(self):
            self.closed = True

    monkeypatch.setattr(module, "CopilotAdapter", AuthAdapter)
    await module._login(Settings(data_dir=tmp_path))
    assert len(login.advanced) == 1
    assert login.cancelled == []
    text = capsys.readouterr().out
    assert "ABCD-EFGH" in text
    assert "Logowanie Copilot zakończone" in text

    async def timeout(params, **kwargs):
        raise TimeoutError

    login.advance = timeout
    with pytest.raises(TimeoutError):
        await module._login(Settings(data_dir=tmp_path))
    assert login.cancelled == ["opaque-flow"]


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,decision,completed", [("tak", "allow", True),
                                                      ("no", "deny", False), ("", "deny", False)])
async def test_login_plaintext_consent_uses_runtime_wire_decision(tmp_path, monkeypatch, capsys,
                                                                 answer, decision, completed):
    requests, cancelled, closed = [], [], []

    class Login:
        async def begin(self, params, **kwargs):
            return NS(flow_id="consent-flow", step=NS(kind="completed",
                                                       result=NS(status="needs-plaintext-consent")))

        async def advance(self, params, **kwargs):
            # Exercise the real pinned SDK request's wire serialization. The
            # runtime treats only "allow" as consent, not "yes" or "true".
            wire = params.to_dict()
            requests.append(wire)
            status = "completed" if wire["input"] == "allow" else "declined"
            return NS(kind="completed", result=NS(status=status))

        async def cancel(self, params, **kwargs):
            cancelled.append(params.flow_id)

    class AuthAdapter:
        def __init__(self, *args, **kwargs):
            self._session = NS(rpc=NS(accounts=NS(login=Login())))

        async def _ensure_client(self):
            pass

        async def _ensure_session(self, tools, system):
            assert tools == []
            assert "No model turn" in system

        async def close(self):
            closed.append(True)

    monkeypatch.setattr(module, "CopilotAdapter", AuthAdapter)
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    if completed:
        await module._login(Settings(data_dir=tmp_path))
    else:
        with pytest.raises(CopilotError, match="copilot_auth_required"):
            await module._login(Settings(data_dir=tmp_path))
    assert requests == [{"flowId": "consent-flow", "input": decision}]
    assert closed == [True]
    assert cancelled == ([] if completed else ["consent-flow"])
    assert ("Logowanie Copilot zakończone" in capsys.readouterr().out) is completed


@pytest.mark.parametrize("details,expected", [
    ("You're out of usage credits: SECRET", "copilot_quota_exhausted"),
    ("organization policy denies operation", "copilot_policy_restricted"),
    ("HTTP 429 too many requests", "copilot_rate_limited"),
    ("Network is unreachable", "copilot_network_unavailable"),
    ("unrecognized unexpected SECRET", "copilot_failed"),
])
def test_fixed_error_codes_never_return_backend_text(details, expected):
    error = CopilotError(module._error_code(details))
    assert error.code == expected
    assert "SECRET" not in str(error)
