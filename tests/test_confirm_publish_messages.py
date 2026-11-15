"""What a person reads when confirming or publishing is refused (#239): the condition that failed, and a way out that works."""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from cairnmind.inspector import Inspector
from cairnmind.inspector_api import attach_inspector_routes
from cairnmind.terminal_i18n import tr
from cairnmind.terminal_i18n_messages import broker_error_text

from test_published_memory_acts import workspace  # noqa: F401 (a fixture)


def test_confirming_a_record_already_confirmed_here_says_so_and_not_version_conflict(workspace):  # noqa: F811
    store, pid, _, _ = workspace
    source = store.snapshot(pid, "note://twice", "Use cookies.", "note")
    record = store.remember(pid, {"kind": "decision", "content": "Use cookies."}, [source["id"]])
    inspector = Inspector(store)
    confirmed = inspector.review(pid, record["id"], "confirm", 1)["record"]
    with pytest.raises(ValueError, match="^Review version conflict"):
        inspector.review(pid, record["id"], "confirm", 1)  # A stale version is still a version conflict.
    with pytest.raises(ValueError, match="^Already confirmed by a person here at this version; nothing to do.$"):
        inspector.review(pid, record["id"], "confirm", confirmed["version"])
    disputed = inspector.review(pid, record["id"], "dispute", confirmed["version"], reason="Wrong")["record"]
    with pytest.raises(ValueError, match="^Already disputed at this version; nothing to do.$"):
        inspector.review(pid, record["id"], "dispute", disputed["version"], reason="Still wrong")
    other = store.snapshot(pid, "note://other", "Use tokens.", "note")
    replacement = store.remember(pid, {"kind": "decision", "key": "auth-2", "content": "Use tokens.",
                                       "supersedes": record["id"]}, [other["id"]])
    historical = next(r for r in store.list_records(pid, include_history=True) if r["id"] == record["id"])
    assert replacement["supersedes"] == record["id"] and historical["status"] == "superseded"
    with pytest.raises(ValueError, match="^A historical version cannot be reviewed.$"):
        inspector.review(pid, record["id"], "confirm", historical["version"])
    # Over HTTP it is a refusal (400), not a conflict to read again and retry (409); the panel shows it in Polish.
    app = FastAPI()
    attach_inspector_routes(app, store, store.settings)
    response = TestClient(app).post(f"/inspector/records/{replacement['id']}/review",
                                    headers={"Authorization": "Bearer " + store.settings.token},
                                    json={"project": pid, "action": "confirm", "expected_version": 1})
    assert response.status_code == 200
    again = TestClient(app).post(f"/inspector/records/{replacement['id']}/review",
                                 headers={"Authorization": "Bearer " + store.settings.token},
                                 json={"project": pid, "action": "confirm", "expected_version": 2})
    assert again.status_code == 400 and again.json()["detail"].startswith("Already confirmed by a person here")
    error = SimpleNamespace(detail=again.json()["detail"], status_code=400)
    polish = broker_error_text(error, lambda text: tr(text, "pl"))
    assert polish == "Broker 400: Już potwierdzone przez osobę na tym komputerze w tej wersji; nie ma nic do zrobienia."
    for sentence in ("A historical version cannot be reviewed.", "Already disputed at this version; nothing to do."):
        assert tr(sentence, "pl") != sentence


def test_a_refusal_for_a_command_without_a_terminal_names_piped_or_redirected_input_or_output(workspace,  # noqa: F811
                                                                                             monkeypatch):
    from typer.testing import CliRunner

    from cairnmind import cli, confirmation, record_kinds
    store, pid, _, _ = workspace
    for name in ("CAIRNMIND_SESSION_ID", "CAIRNMIND_PACKET_ID", *confirmation.CLIENT_MARKERS):
        monkeypatch.delenv(name, raising=False)
    # CliRunner gives the command no terminal: as `... | head` or `... > file` in a person's own terminal.
    run = CliRunner()
    plan = run.invoke(cli.app, ["--data-dir", str(store.data_dir), "memory", "confirm-plan", pid,
                                "00000000-0000-4000-8000-000000000000", "--expected-version", "1"])
    remember = run.invoke(cli.app, ["--data-dir", str(store.data_dir), "remember", pid, "x", "--confirmed"])
    for result in (plan, remember):
        assert result.exit_code == 2 and "piped or redirected" in " ".join(result.output.split()), result.output
    assert "piped or redirected" in record_kinds.CLI_REFUSED and "piped or redirected" in confirmation.CLI_REFUSED
