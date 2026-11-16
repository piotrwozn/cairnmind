"""A third-party MCP server that did not start, on the sidebar's notice line and in Notices (#209).

The agent worker reports a fixed code and the server's id to the broker, which keeps them with the run;
the sidebar turns the code into a translated reason, once per server per run. Nothing the server said
travels: not its output, not its error output.
"""
from __future__ import annotations

from copy import deepcopy
import json
import logging

from fastapi.testclient import TestClient
import httpx
import pytest

from cairnmind.cockpit_state import CockpitState
from cairnmind.mcp_host import McpHost
from cairnmind.terminal_panels import SidebarApp
from test_api_runtime import local_profile, mcp_server, runtime  # noqa: F401  (runtime is a fixture)
from test_mcp_agent import server as fixture_server, worker
from test_terminal_panels import settle
from test_terminal_ux080 import UIBroker, setup_ui
from test_workspace_stage_scope import notice_of, wait_until

SECRET = "SERVER_SAID_PRIVATE_TEXT"
REASON_EN = "its program did not start or stopped during its start"
REASON_PL = "jego program nie uruchomił się albo zakończył się w trakcie startu"
MISSING_EN = "its program does not exist or cannot be run on this computer"
MISSING_PL = "jego programu nie ma albo nie da się go uruchomić na tym komputerze"


def started_run(runner, project, servers):
    CockpitState(runner.settings).update({"project_mcp_servers": {project["id"]: servers}})
    return runner.start_agent(project, local_profile(), slot_id="slot")


def report(run, **changes):
    return {"run_id": run["id"], "generation": run["generation"], "project_id": run["project_id"],
            "server_id": "gone", "code": "start_failed", **changes}


def broker_for(runner):
    from cairnmind.cockpit import create_cockpit_app
    return create_cockpit_app(runner.settings, runtime=runner,
                              proxy_transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))


def test_the_broker_keeps_one_fixed_report_per_server_with_the_run(runtime):  # noqa: F811
    runner, _, project, _, _ = runtime
    run = started_run(runner, project, [mcp_server("gone", command="/nonexistent/server", args=())])
    assert runner.record_mcp_failure(**report(run)) == {"recorded": True}
    # A second report of the same server in the same run (an interrupted start tried again) is kept once.
    assert runner.record_mcp_failure(**report(run)) == {"recorded": False}
    row = next(row for row in runner.list_runs() if row["id"] == run["id"])
    assert row["mcp_failures"] == [{"server_id": "gone", "code": "start_failed"}]


@pytest.mark.parametrize("changes,refusal", [
    ({"code": "server_said_something"}, "code"),
    ({"code": "Start failed: " + SECRET}, "code"),
    ({"code": None}, "code"),
    ({"server_id": "Gone Server"}, "server"),
    ({"server_id": "x" * 25}, "server"),
    ({"server_id": "other"}, "server"),          # well formed, but not a server of this run
    ({"project_id": "another-project"}, "project"),
    ({"generation": 7}, "generation"),
])
def test_the_broker_refuses_a_report_it_cannot_trust(runtime, changes, refusal):  # noqa: F811
    runner, _, project, _, _ = runtime
    run = started_run(runner, project, [mcp_server("gone", command="/nonexistent/server", args=())])
    with pytest.raises(ValueError, match=refusal):
        runner.record_mcp_failure(**report(run, **changes))
    assert "mcp_failures" not in runner.get_run(run["id"])


def test_an_unknown_run_is_refused(runtime):  # noqa: F811
    from cairnmind.client_runtime import UnknownRun
    runner, _, project, _, _ = runtime
    run = started_run(runner, project, [mcp_server("gone", command="/nonexistent/server", args=())])
    with pytest.raises(UnknownRun):
        runner.record_mcp_failure(**report(run, run_id="f" * 32))


@pytest.mark.parametrize("lifecycle", ["stopping", "exited"])
def test_a_report_racing_a_stop_is_refused(runtime, monkeypatch, lifecycle):  # noqa: F811
    # The run passes the first check, then moves on before the report is written: the write refuses it.
    runner, _, project, _, _ = runtime
    run = started_run(runner, project, [mcp_server("gone", command="/nonexistent/server", args=())])
    checked = runner.agent_configuration

    def check_then_stop(run_id, generation):
        config = checked(run_id, generation)
        with runner._locked():
            rows = runner._read()
            next(row for row in rows if row["id"] == run_id)["lifecycle"] = lifecycle
            runner._save(rows)
        return config
    monkeypatch.setattr(runner, "agent_configuration", check_then_stop)
    with pytest.raises(ValueError, match="no longer running"):
        runner.record_mcp_failure(**report(run))
    assert "mcp_failures" not in runner.registry.read_text()


def test_the_private_route_takes_only_the_fixed_fields(runtime):  # noqa: F811
    runner, _, project, _, _ = runtime
    run = started_run(runner, project, [mcp_server("gone", command="/nonexistent/server", args=())])
    route = "/_control/agent/mcp_failure"
    with TestClient(broker_for(runner), base_url="http://127.0.0.1:8766") as client:
        bearer = {"Authorization": "Bearer " + runner.settings.token}
        assert client.post(route, json=report(run)).status_code == 401
        # Nothing but the code and the id: a field that could carry the server's words is refused.
        assert client.post(route, headers=bearer, json={**report(run), "message": SECRET}).status_code == 422
        assert client.post(route, headers=bearer, json={**report(run), "code": 3}).status_code == 422
        assert client.post(route, headers=bearer, json=report(run, run_id="f" * 32)).status_code == 404
        assert client.post(route, headers=bearer, json=report(run, code="weird")).status_code == 400
        assert client.post(route, headers=bearer, json=report(run, server_id="../x")).status_code == 400
        assert client.post(route, headers=bearer, json=report(run, project_id="another")).status_code == 400
        assert "mcp_failures" not in runner.get_run(run["id"])
        accepted = client.post(route, headers=bearer, json=report(run))
        assert accepted.status_code == 200 and accepted.json() == {"recorded": True}
    assert runner.get_run(run["id"])["mcp_failures"] == [{"server_id": "gone", "code": "start_failed"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("language,reason", [("en", REASON_EN), ("pl", REASON_PL)])
async def test_the_pane_names_the_reason_in_words_and_reports_each_server_once(tmp_path, language, reason):
    # #169 retrial: the pane row showed "time (start_failed)", the raw code.
    app, _ = worker(tmp_path, servers=[fixture_server()],
                    problems=[{"server_id": "local", "code": "definition_refused"}])
    # Saved, not only set: the app re-reads the saved language every half second and would switch back,
    # which a slow run did in the middle of this test.
    CockpitState(app.settings).update({"language": language})
    sent = []
    app._report = lambda server_id, code: sent.append((server_id, code))
    async with app.run_test(size=(95, 42)) as pilot:
        await pilot.pause()
        app.language = language
        app.refresh_language()
        await app.agent_event({"type": "mcp_server_failed", "data": {"server_id": "time", "code": "start_failed"}})
        await app.agent_event({"type": "mcp_server_failed", "data": {"server_id": "x\x1b[2J", "code": "start"}})
        await app.workers.wait_for_complete()
        row = str(app.query_one("#agent-mcp").render())
        assert f"time ({reason})" in row and "start_failed" not in row
        history = " ".join(" ".join(line.text for line in app.query_one("#agent-history").lines).split())
        assert reason in history
        assert "start_failed" not in history and "\x1b" not in history
    # The launcher's own problems are the broker's already; only a validated failure of this run is sent.
    assert sent == [("time", "start_failed")]


def test_the_worker_sends_only_the_run_the_id_and_the_code_and_never_fails_on_it(tmp_path, monkeypatch):
    from cairnmind import api_runtime
    from cairnmind.config import Settings
    settings = Settings.load(tmp_path / "data", create=True)
    run = {"id": "run-a", "generation": 2, "project_id": "project-a", "worktree": "/w", "session_id": "s"}
    sent, real = [], httpx.Client

    def answer(request):
        sent.append((request.url.path, json.loads(request.content), request.headers["authorization"]))
        return httpx.Response(200, json={"recorded": True})
    monkeypatch.setattr("cairnmind.cockpit.cockpit_status",
                        lambda settings: {"available": True, "url": "http://127.0.0.1:9"})
    monkeypatch.setattr(api_runtime.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(answer), **kw))
    assert api_runtime.report_mcp_failure(settings, run, "gone", "start_failed") is True
    assert sent == [("/_control/agent/mcp_failure", {"run_id": "run-a", "generation": 2, "project_id": "project-a",
                                                     "server_id": "gone", "code": "start_failed"},
                     "Bearer " + settings.token)]
    assert api_runtime.report_mcp_failure(settings, run, "gone", "said: " + SECRET) is False
    assert api_runtime.report_mcp_failure(settings, run, "Gone!", "start_failed") is False
    assert len(sent) == 1

    def down(request):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(api_runtime.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(down), **kw))
    assert api_runtime.report_mcp_failure(settings, run, "gone", "start_failed") is False


class RunsBroker(UIBroker):
    def __init__(self, project, runs):
        super().__init__(project)
        self.runs = runs

    def request(self, method, path, *, params=None, json=None, timeout=20):
        value = super().request(method, path, params=params, json=json, timeout=timeout)
        if path == "/api/state":
            value["runs"] = deepcopy(self.runs())
        return value


async def sidebar_with(tmp_path, monkeypatch, runs, language="en"):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch, language)
    broker = RunsBroker(broker.projects[0], runs)
    return SidebarApp(settings, broker=broker, workspace=workspace), broker.projects[0]


def api_run(project, failures, *, run_id="run-a", lifecycle="running", generation=1):
    return {"id": run_id, "generation": generation, "project_id": project["id"], "worktree": project["root"],
            "client": "api", "slot_id": "slot", "purpose": "coding", "lifecycle": lifecycle,
            "mcp_failures": failures}


def post_report(runner, run, data):
    with TestClient(broker_for(runner), base_url="http://127.0.0.1:8766") as client:
        return client.post("/_control/agent/mcp_failure", headers={"Authorization": "Bearer " + runner.settings.token},
                           json=report(run, **data))


def as_shown(rows, project):
    """The broker's run rows, moved to the project the sidebar fixture shows."""
    return [{**row, "project_id": project["id"], "worktree": project["root"]} for row in rows]


@pytest.mark.asyncio
@pytest.mark.parametrize("language,reason", [("en", MISSING_EN), ("pl", MISSING_PL)])
async def test_acceptance_a_missing_program_is_one_notice_and_stays_in_notices(tmp_path, monkeypatch, runtime,  # noqa: F811
                                                                               language, reason):
    # The whole way: the host's fixed code, the report through the broker's route, the run record, the
    # sidebar's poll. The program does not exist, and the notice says so (#240).
    runner, _, project, _, _ = runtime
    host = McpHost([{"id": "gone", "command": "/nonexistent/server"}], worktree=tmp_path, reserved=set())
    events = await host.start()
    assert events == [("mcp_server_failed", {"server_id": "gone", "code": "program_unavailable"})]
    run = started_run(runner, project, [mcp_server("gone", command="/nonexistent/server", args=())])
    assert post_report(runner, run, events[0][1]).status_code == 200
    app, shown = await sidebar_with(tmp_path, monkeypatch, lambda: as_shown(runner.list_runs(), shown), language)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        await wait_until(pilot, lambda: "gone" in notice_of(app))
        assert reason in notice_of(app) and "program_unavailable" not in notice_of(app)
        for _ in range(3):  # later polls: still one notice, not one per poll
            await app.load_state()
        entries = [entry for entry in app.notice_history.entries if "gone" in entry.text]
        assert len(entries) == 1 and entries[0].count == 1 and entries[0].kind == "warn"
        assert entries[0].project_id == shown["id"]
        app.notice("A later notice")  # the line moves on; Notices still has it
        assert "gone" not in notice_of(app)
        app.action_notices()
        await wait_until(pilot, lambda: "gone" in str(app.screen.query_one("#message-body").render()))


@pytest.mark.asyncio
async def test_a_failure_in_another_project_goes_to_notices_not_the_line(tmp_path, monkeypatch):
    other = {"id": "project-other", "root": str(tmp_path / "other")}
    failure = [{"server_id": "tracker", "code": "start_timeout"}]
    app, project = await sidebar_with(tmp_path, monkeypatch, lambda: [api_run(other, failure, run_id="run-o")])
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        await wait_until(pilot, lambda: any("tracker" in entry.text for entry in app.notice_history.entries))
        assert "tracker" not in notice_of(app)
        entry = next(entry for entry in app.notice_history.entries if "tracker" in entry.text)
        assert entry.project_id == "project-other" and "did not answer within its start time" in entry.text


@pytest.mark.asyncio
async def test_the_sidebar_shows_only_a_validated_failure_of_a_live_run(tmp_path, monkeypatch):
    forged = [{"server_id": "evil\x1b[31m", "code": "start_failed"}, {"server_id": "a", "code": SECRET},
              {"server_id": "b", "code": "start_failed", "message": SECRET}, "start_failed"]
    def rows(project):
        ended = api_run(project, [{"server_id": "old", "code": "start_failed"}], run_id="run-ended", lifecycle="exited")
        return [api_run(project, forged), ended]
    app, project = await sidebar_with(tmp_path, monkeypatch, lambda: rows(project))
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        await app.load_state()
        texts = [entry.text for entry in app.notice_history.entries]
        assert not any(SECRET in text or "evil" in text or "old" in text for text in texts)
        # The extra field is not shown; the id and the code it came with are.
        assert sum("MCP server b " in text for text in texts) == 1


@pytest.mark.asyncio
async def test_the_sidebar_forgets_a_run_that_left_the_live_list(tmp_path, monkeypatch):
    runs = []
    app, project = await sidebar_with(tmp_path, monkeypatch, lambda: runs)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        runs[:] = [api_run(project, [{"server_id": "gone", "code": "start_failed"}])]
        await app.load_state()
        assert app._mcp_failures_shown == {("run-a", 1, "gone")}
        runs[:] = [api_run(project, [{"server_id": "gone", "code": "start_failed"}], lifecycle="exited")]
        await app.load_state()
        assert app._mcp_failures_shown == set()
        runs[:] = []
        await app.load_state()
        assert app._mcp_failures_shown == set()


class NoticeSink:
    """What announce() needs of the sidebar, recording where each notice went."""

    def __init__(self, project):
        self.workspace_data = {"project_id": project["id"], "worktree": project["root"]}
        self.projects = {project["id"]: project}
        self.posted = []

    def t(self, value, **values):
        return value.format(**values)

    def notice_for(self, subject, text, kind):
        self.posted.append(("line", text, kind))

    def notice_elsewhere(self, subject, text, kind):
        self.posted.append(("notices", text, kind))


def test_the_cockpit_state_carries_the_report_to_the_project_shown(runtime):  # noqa: F811
    # No rewriting of the rows: the real /api/state, its enrich() step, and the run's own project and folder.
    from cairnmind.mcp_failure_notice import announce
    runner, _, project, _, _ = runtime
    run = started_run(runner, project, [mcp_server("gone", command="/nonexistent/server", args=())])
    app = broker_for(runner)
    origin = "http://127.0.0.1:8766"
    with TestClient(app, base_url=origin) as client:
        bearer = {"Authorization": "Bearer " + runner.settings.token}
        assert client.post("/_control/agent/mcp_failure", headers=bearer, json=report(run)).status_code == 200
        client.post("/auth/exchange", json={"ticket": app.state.browser_sessions.ticket()}, headers={"Origin": origin})
        state = client.get("/api/state")
    assert state.status_code == 200
    row = next(row for row in state.json()["runs"] if row["id"] == run["id"])
    assert row["mcp_failures"] == [{"server_id": "gone", "code": "start_failed"}]
    sink = NoticeSink(project)
    announce(sink, state.json()["runs"])
    assert [(where, kind) for where, _, kind in sink.posted] == [("line", "warn")]
    assert "gone" in sink.posted[0][1] and REASON_EN in sink.posted[0][1]


@pytest.mark.asyncio
async def test_nothing_the_server_printed_reaches_the_broker_the_notice_or_a_log(tmp_path, monkeypatch, caplog, runtime):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    runner, _, project, _, _ = runtime
    noisy = {"id": "noisy", "command": "/bin/sh",
             "args": ["-c", f"echo {SECRET}; echo {SECRET} >&2; echo '{{\"jsonrpc\": \"2.0\", \"error\": \"{SECRET}\"}}'; exit 3"]}
    host = McpHost([noisy], worktree=tmp_path / "w", reserved=set())
    (tmp_path / "w").mkdir()
    events = await host.start()
    assert events == [("mcp_server_failed", {"server_id": "noisy", "code": "start_failed"})]
    run = started_run(runner, project, [mcp_server("noisy", command="/bin/sh", args=())])
    assert post_report(runner, run, events[0][1]).status_code == 200
    assert SECRET not in runner.registry.read_text()
    app, shown = await sidebar_with(tmp_path, monkeypatch, lambda: as_shown(runner.list_runs(), shown))
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        await wait_until(pilot, lambda: "noisy" in notice_of(app))
        assert SECRET not in json.dumps([entry.text for entry in app.notice_history.entries])
    assert SECRET not in caplog.text


def test_every_code_the_host_reports_has_a_reason_in_both_languages():
    from cairnmind import mcp_host
    from cairnmind.mcp_failure_notice import REASONS, START_FAILURE_CODES, notice_text
    from cairnmind.terminal_agent_i18n import tr as agent_tr
    from cairnmind.terminal_i18n import tr
    produced = set(mcp_host._START_CODES.values()) | {"start_failed", "start_timeout", "protocol_unsupported",
                                                      "catalogue_invalid", "definition_refused",
                                                      "program_unavailable"}
    assert produced == START_FAILURE_CODES and START_FAILURE_CODES <= set(REASONS)
    for code in REASONS:
        for translate in (tr, agent_tr):
            assert translate(REASONS[code], "pl") != REASONS[code]
    text = notice_text("gone", "start_failed", lambda value, **values: tr(value, "pl", **values))
    assert "gone" in text and REASON_PL in text and "start_failed" not in text
