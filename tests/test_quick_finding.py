"""Ctrl+B n: a finding typed in the workspace, saved through the broker and the service (#14)."""
from __future__ import annotations

import json
from pathlib import Path
import shlex
import subprocess
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest
from textual.widgets import Select, Static, TextArea

from cairnmind.cockpit import create_cockpit_app
from cairnmind.cockpit_state import CockpitState
from cairnmind.config import Settings
from cairnmind.findings import FINDING_KINDS, FINDING_STATUSES, MAX_FINDING_CHARS, finding_error
from cairnmind.service import create_app
from cairnmind.store import Store
from cairnmind.terminal_client import BrokerClient, BrokerError
from cairnmind.terminal_commands import CommandPaletteApp
from cairnmind.workspace import WorkspaceManager
from cairnmind.workspace_finding import ERROR_LIMIT, FindingApp, confirmation
from cairnmind.workspace_state import WorkspaceState
from test_cockpit_server import ORIGIN, Runtime, cockpit as cockpit, login
from test_service import LocalModelFixture

ROOT = Path(__file__).resolve().parents[1]


def text_of(app, selector):
    return str(app.query_one(selector, Static).render())


def recorded_manager(tmp_path, monkeypatch):
    manager = WorkspaceManager(Settings(data_dir=tmp_path / "data"))
    calls = []
    monkeypatch.setattr(manager, "tmux", lambda *args, **kwargs: calls.append((args, kwargs)))
    return manager, calls


def test_prefix_n_opens_the_finding_popup_and_collides_with_no_other_key(tmp_path, monkeypatch):
    manager, calls = recorded_manager(tmp_path, monkeypatch)
    manager._bindings()
    keys = [args[1] for args, _ in calls if args[0] == "bind-key"]
    assert "n" in keys and len(keys) == len(set(keys))
    bound = next(args for args, _ in calls if args[:2] == ("bind-key", "n"))
    assert bound[2] == "run-shell"
    words = shlex.split(bound[3])
    assert words[words.index("--finding"):] == ["--finding", "--client-pid", "#{client_pid}",
                                                "--client-tty", "#{client_tty}"]


def test_finding_popup_targets_the_initiating_writer_and_carries_no_typed_text(tmp_path, monkeypatch):
    manager, calls = recorded_manager(tmp_path, monkeypatch)
    monkeypatch.setattr(manager, "exists", lambda: True)
    monkeypatch.setattr(manager, "clients", lambda: [{"pid": 101, "tty": "/dev/pts/1", "readonly": False},
                                                     {"pid": 202, "tty": "/dev/pts/2", "readonly": True}])
    manager.open_finding(client_pid=101, client_tty="/dev/pts/1")
    args, kwargs = calls[0]
    assert args[:4] == ("display-popup", "-c", "/dev/pts/1", "-E") and kwargs["timeout"] is None
    words = shlex.split(args[-1])
    assert words[words.index("--finding-ui"):] == ["--finding-ui", "--client-tty", "/dev/pts/1"]
    for pid, tty in ((202, "/dev/pts/2"), (101, "/dev/pts/2"), (None, None)):
        with pytest.raises(ValueError):
            manager.open_finding(client_pid=pid, client_tty=tty)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("language", "line"), [("en", "Ctrl+B, then n: save a finding to memory"),
                                                ("pl", "Ctrl+B, potem n: zapisz ustalenie w pamięci")])
async def test_keyboard_help_and_docs_list_the_finding_key(tmp_path, language, line):
    settings = Settings.load(tmp_path / "data", create=True)
    CockpitState(settings).update({"language": language})
    app = CommandPaletteApp(SimpleNamespace(settings=settings), help_only=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert line in text_of(app, "#command-help")
    assert "| `n` |" in (ROOT / "docs" / "TERMINAL.md").read_text(encoding="utf-8")


def test_validation_refuses_empty_text_and_kinds_or_statuses_outside_the_form():
    for kind in FINDING_KINDS:
        for status in FINDING_STATUSES:
            assert finding_error("We use ArcadeDB.", kind, status) is None
    for content in ("", " \n\t ", None, 7):
        assert finding_error(content, "decision", "proposed") == "Write the finding first."
    for kind in ("task", "checkpoint", "Decision", "", None):
        assert finding_error("x", kind, "proposed").startswith("Choose a kind")
    for status in ("disputed", "superseded", "", None):
        assert finding_error("x", "fact", status).startswith("Choose a status")
    assert finding_error("x" * MAX_FINDING_CHARS, "fact", "proposed") is None
    assert finding_error("x" * (MAX_FINDING_CHARS + 1), "fact", "proposed")


class FakeBroker:
    def __init__(self, error=None):
        self.error, self.calls = error, []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if self.error:
            raise self.error
        return {"record": {"id": "record-1"}}


def selected_workspace(tmp_path, project_id="project-a", root=None):
    settings = Settings.load(tmp_path / "data", create=True)
    state = WorkspaceState(settings)
    state.select_project({"id": project_id, "name": "Project A", "root": str(root or tmp_path)})
    return settings, state


@pytest.mark.asyncio
async def test_form_refuses_empty_text_and_a_failed_save_keeps_everything_typed(tmp_path):
    settings, state = selected_workspace(tmp_path)
    broker = FakeBroker(BrokerError(409, "\x1b[31mresume_required‮ " + "conflict " * 400))
    app = FindingApp(settings, broker=broker, workspace=state)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press("ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert text_of(app, "#finding-error") == "Write the finding first." and broker.calls == []
        await pilot.press(*"Use", "space", *"WAL")
        app.query_one("#finding-kind", Select).value = "constraint"
        app.query_one("#finding-status", Select).value = "confirmed"
        await pilot.press("ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.is_running and len(broker.calls) == 1
        assert app.query_one("#finding-content", TextArea).text == "Use WAL"
        assert app.values() == {"content": "Use WAL", "kind": "constraint", "status": "confirmed"}
        error = text_of(app, "#finding-error")
        assert error.startswith("Could not save. Your entries were kept. \\x1b[31mresume_required\\u202e")
        assert "\x1b" not in error and "‮" not in error
        assert len(error) <= len("Could not save. Your entries were kept. ") + ERROR_LIMIT + 1
        await pilot.press("escape")
    assert app.return_value is None and len(broker.calls) == 1


@pytest.mark.asyncio
async def test_escape_closes_without_saving_and_no_project_cannot_save(tmp_path):
    settings, state = selected_workspace(tmp_path)
    broker = FakeBroker()
    app = FindingApp(settings, broker=broker, workspace=state)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press(*"draft", "escape")
    assert app.return_value is None and broker.calls == []
    empty = WorkspaceState(Settings.load(tmp_path / "other", create=True))
    app = FindingApp(settings, broker=broker, workspace=empty)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press(*"orphan", "ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert text_of(app, "#finding-error") == "Open a project first." and app.is_running
    assert broker.calls == []


def test_confirmation_is_one_literal_line_in_both_languages():
    english = confirmation({"kind": "decision", "status": "proposed"})
    assert english == "Finding saved to memory: decision, proposed."
    polish = confirmation({"kind": "fact", "status": "confirmed"}, "pl")
    assert polish == "Ustalenie zapisane w pamięci: fact, confirmed."
    assert confirmation({"kind": "#(rm -rf ~)", "status": "#{pid}"}) == "Finding saved to memory: ?, ?."


@pytest.fixture
def stack(tmp_path, monkeypatch):
    """The real chain of the popup: BrokerClient -> cockpit broker -> memory service -> ArcadeDB."""
    settings = Settings.load(tmp_path / "memory", create=True)
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("demo\n")
    for args in (("init",), ("config", "user.email", "fixture@example.invalid"),
                 ("config", "user.name", "Fixture"), ("add", "."), ("commit", "-m", "fixture")):
        subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)
    store = Store(settings.data_dir)
    project = store.add_project("demo", root)
    monkeypatch.setattr("cairnmind.client_auth.probe_client", lambda client, **kwargs: {"client": client})
    with TestClient(create_app(settings, start_worker=False, ollama=LocalModelFixture())) as service:
        def upstream(request):
            reply = service.request(request.method, request.url.path, params=dict(request.url.params),
                                    content=request.content, headers={
                                        "Authorization": request.headers["authorization"],
                                        "Content-Type": "application/json"})
            return httpx.Response(reply.status_code, content=reply.content,
                                  headers={"Content-Type": "application/json"})
        app = create_cockpit_app(settings, runtime=Runtime(), proxy_transport=httpx.MockTransport(upstream),
                                 public_url=ORIGIN)
        with TestClient(app, base_url=ORIGIN) as browser:
            monkeypatch.setattr("cairnmind.cockpit.cockpit_status",
                                lambda settings: {"available": True, "url": ORIGIN})
            broker = BrokerClient(settings, transport=browser._transport)
            yield settings, store, project, root, broker, service
            broker.close()


@pytest.mark.asyncio
async def test_saved_finding_reaches_store_byte_for_byte_and_is_visible_in_the_inspector(stack, tmp_path):
    settings, store, project, root, broker, _ = stack
    state = WorkspaceState(settings)
    state.select_project(project)
    marker, other = tmp_path / "executed", tmp_path / "executed-too"
    content = (f"-rf --help \"quoted\" 'single' $(touch {marker}) `touch {other}` $HOME ; | & \\\n"
               "second line\n\n  indented # not a comment\n")
    app = FindingApp(settings, broker=broker, workspace=state)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one("#finding-content", TextArea).insert(content)
        app.query_one("#finding-kind", Select).value = "constraint"
        app.query_one("#finding-status", Select).value = "confirmed"
        await pilot.press("ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()
    result = app.return_value
    assert result["kind"] == "constraint" and result["status"] == "confirmed"
    assert not marker.exists() and not other.exists()
    [record] = store.list_records(project["id"])
    assert (record["id"], record["kind"], record["status"], record["content"]) == (
        result["id"], "constraint", "confirmed", content)
    assert record["metadata"]["origin"] == "workspace_form"
    assert record["metadata"]["worktree"] == str(root.resolve())
    [source_id] = record["source_ids"]
    source = store.get_source(source_id)
    assert source["content"] == content and source["kind"] == "manual"
    assert source["uri"].startswith("workspace://finding/") and source["project_id"] == project["id"]
    assert {key: source["metadata"][key] for key in ("role", "origin", "worktree")} == {
        "role": "user", "origin": "workspace_form", "worktree": str(root.resolve())}
    assert source["metadata"]["source_timestamp"]
    seen = broker.request("GET", "/api/inspector/records",
                          params={"project": project["id"], "worktree": str(root)})
    assert [item["id"] for item in seen["items"]] == [record["id"]]


def test_service_keeps_the_resume_gate_and_its_own_validation(stack, tmp_path):
    from cairnmind.sessions import SessionManager
    settings, store, project, root, _, service = stack
    headers = {"Authorization": f"Bearer {settings.token}"}
    body = {"project": project["id"], "content": "Agents must not skip ACK.", "kind": "fact"}
    session = SessionManager(store, settings).prepare(project["id"], "codex", timeout=0)
    gated = service.post("/findings", json={**body, "session_id": session["session_id"]}, headers=headers)
    assert gated.status_code == 409 and "resume_required" in gated.json()["detail"]
    for change in ({"kind": "task"}, {"status": "superseded"}, {"content": "   "},
                   {"worktree": str(tmp_path)}):
        assert service.post("/findings", json={**body, **change}, headers=headers).status_code == 400
    assert service.post("/findings", json=body).status_code == 401
    assert not [row for row in store.list_records(project["id"]) if row["kind"] != "checkpoint"]
    saved = service.post("/findings", json=body, headers=headers)
    assert saved.status_code == 200 and saved.json()["record"]["status"] == "proposed"


def test_broker_forwards_only_the_form_fields_and_never_a_session(cockpit):
    settings, app, client, runtime, project, requests, _ = cockpit
    headers = login(app, client)
    body = {"project": project["id"], "worktree": "/repo", "content": "Line one\n$(id)", "kind": "decision",
            "status": "proposed"}
    assert client.post("/api/findings", json=body, headers=headers).status_code == 200
    method, path, content = requests[-1]
    assert (method, path) == ("POST", "/findings") and json.loads(content) == body
    sent = len(requests)
    for change in ({"session_id": "forged"}, {"packet_id": "forged"}, {"kind": "task"},
                   {"status": "disputed"}, {"content": "  "}, {"project": 5}, {"worktree": ["/repo"]}):
        assert client.post("/api/findings", json={**body, **change}, headers=headers).status_code == 422
    assert client.post("/api/findings", json=body, headers={"Origin": ORIGIN}).status_code in {401, 403}
    assert len(requests) == sent
