"""The impact list as the workspace reads it: GET /inspector/impact, directly and through the broker (issue #8)."""
from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from cairnmind.cli import app
from cairnmind.inspector_api import attach_inspector_routes

from test_cockpit_server import cockpit as cockpit, login
from test_impact import workspace as workspace
from test_impact_verdicts import changed as changed, give


def state(store, pid):
    """Everything a read must leave alone: records with versions and times, revisions, sources, edges."""
    return {table: sorted(json.dumps(row, sort_keys=True, default=str) for row in store.query(
        f"SELECT * FROM {table} WHERE project_id=:project", {"project": pid}))
        for table in ("records", "record_revisions", "sources", "edges", "jobs")}


def test_the_inspector_lists_what_impact_json_prints_and_writes_nothing(changed):
    store, pid, root, records = changed
    give(store, pid, records["rule"], "not_related", note="only the name matches")
    service = FastAPI()
    attach_inspector_routes(service, store, store.settings)
    client = TestClient(service)
    headers = {"Authorization": "Bearer " + store.settings.token}
    before = state(store, pid)

    def command(*extra):
        result = CliRunner().invoke(app, ["--data-dir", str(store.data_dir), "impact", "impact", "--since", "HEAD",
                                          "--json", *extra])
        assert result.exit_code == 0, result.output
        return json.loads(result.output)

    listed = client.get("/inspector/impact", params={"project": pid, "since": "HEAD"}, headers=headers)
    assert listed.status_code == 200, listed.text
    assert listed.json() == command() and len(listed.json()["records"]) == 2
    pending = client.get("/inspector/impact", headers=headers,
                         params={"project": "impact", "since": "HEAD", "worktree": str(root), "pending": "true"})
    assert pending.json() == command("--pending") and [row["id"] for row in pending.json()["records"]] == [
        records["task"]["id"]]
    assert state(store, pid) == before

    def status(params, auth=True):
        return client.get("/inspector/impact", params=params, headers=headers if auth else {}).status_code

    assert status({"project": pid, "since": "HEAD"}, auth=False) == 401
    assert status({"project": pid}) == 422 and status({"project": pid, "since": ""}) == 422
    assert status({"project": pid, "since": "x" * 201}) == 422
    assert status({"project": "no-such-project", "since": "HEAD"}) == 404
    # Mapped by type: a commit whose name says "conflict" is a bad request, not a version conflict.
    assert status({"project": pid, "since": "no-conflict-here"}) == 400
    assert status({"project": pid, "since": "HEAD", "worktree": str(root.parent)}) == 400
    assert state(store, pid) == before


def test_the_broker_passes_the_impact_list_only_to_a_workspace_session(cockpit):
    settings, service, client, runtime, project, requests, _ = cockpit
    sent = len(requests)
    params = {"project": project["id"], "since": "HEAD", "pending": "true"}
    assert client.get("/api/inspector/impact", params=params).status_code == 401
    assert len(requests) == sent
    login(service, client)
    assert client.get("/api/inspector/impact", params=params).status_code == 200
    assert requests[-1][:2] == ("GET", "/inspector/impact") and len(requests) == sent + 1
    assert client.post("/api/inspector/impact", json=params, headers=login(service, client)).status_code == 405
    assert len(requests) == sent + 1
