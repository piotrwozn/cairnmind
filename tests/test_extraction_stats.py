"""How extractions fared in review, per extractor version (#193)."""
from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.agents import extraction_stats as stats_module
from cairnmind.memory.confirmation import SEED_ROUTE, remember_as_person
from cairnmind.agents.extraction_stats import extraction_stats, render
from cairnmind.terminal.inspector import Inspector
from cairnmind.terminal.inspector_api import attach_inspector_routes
from cairnmind.store import Store

from conftest import as_person


def stamp(version):
    return {"version": version, "prompt_sha256": "0123456789abcdef", "model": "chat-model@abc123"}


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = Store(tmp_path / "memory")
    project = store.add_project("stats", root)
    return store, project


def hand_source(store, project):
    return store.snapshot(project["id"], "note://hand", "By hand", "note")["id"]


def extracted(store, project, text, version=1):
    source = store.snapshot(project["id"], f"note://{text}", f"Raw {text}", "note",
                            metadata={"normalized_text": text, "role": "user"})
    return store.remember(project["id"], {"kind": "fact", "content": text}, [source["id"]],
                          extracted_from=source["id"], extractor=stamp(version))


def review(store, project, record, action, reason=None):
    return Inspector(store).review(project["id"], record["id"], action, record["version"], reason=reason)["record"]


def row(stats, version):
    return next(item for item in stats["extractors"] if item["extractor_version"] == version)


def test_no_extraction_gives_no_groups_and_a_plain_line(workspace):
    store, project = workspace
    store.remember(project["id"], {"kind": "fact", "content": "Written by hand"}, [hand_source(store, project)])
    result = extraction_stats(store, project["id"])
    assert result["extractors"] == [] and result["records"] == 0 and result["truncated"] is False
    assert "No record was made by automatic extraction" in render(result)


def test_each_extractor_version_counts_its_states_and_the_share_confirmed_of_the_reviewed(workspace):
    store, project = workspace
    pid = project["id"]
    # Version 1: one confirmed by a person, one disputed, one superseded, one still proposed.
    review(store, project, extracted(store, project, "v1 confirmed"), "confirm")
    review(store, project, extracted(store, project, "v1 disputed"), "dispute", "wrong")
    old = extracted(store, project, "v1 replaced")
    store.remember(pid, {"kind": "fact", "content": "v1 replacement", "supersedes": old["id"]}, [hand_source(store, project)])
    extracted(store, project, "v1 proposed")
    # Version 2: two proposed, one confirmed.
    extracted(store, project, "v2 a", 2)
    extracted(store, project, "v2 b", 2)
    review(store, project, extracted(store, project, "v2 c", 2), "confirm")
    result = extraction_stats(store, pid)
    assert [item["extractor_version"] for item in result["extractors"]] == [2, 1]
    one, two = row(result, 1), row(result, 2)
    assert (one["total"], one["proposed"], one["confirmed"], one["disputed"], one["superseded"]) == (4, 1, 1, 1, 1)
    assert (one["reviewed"], one["confirmed_share"]) == (2, 0.5)
    assert (two["total"], two["proposed"], two["confirmed"], two["reviewed"], two["confirmed_share"]) == (3, 2, 1, 1, 1.0)
    assert result["records"] == 7 and result["truncated"] is False


def test_no_review_yet_has_no_share_instead_of_zero(workspace):
    store, project = workspace
    extracted(store, project, "only proposed")
    [item] = extraction_stats(store, project["id"])["extractors"]
    assert item["confirmed_share"] is None and item["reviewed"] == 0
    assert "no review yet" in render(extraction_stats(store, project["id"]))


def test_a_confirmation_that_no_person_gave_here_stays_out_of_the_share(workspace):
    store, project = workspace
    pid = project["id"]
    source = store.snapshot(pid, "note://seeded", "Raw", "note", metadata={"normalized_text": "seeded", "role": "user"})
    seeded = store.remember(pid, {"kind": "fact", "content": "seeded"}, [source["id"]],
                            extracted_from=source["id"], extractor=stamp(1))
    # Only a person's route can confirm; evaluation seed data is the route that is not a person's verdict.
    remember_as_person(store, pid, {"id": seeded["id"], "expected_version": seeded["version"], "status": "confirmed"},
                       [source["id"]], route=SEED_ROUTE)
    assert store.query("SELECT status FROM records WHERE id=:id", {"id": seeded["id"]})[0]["status"] == "confirmed"
    review(store, project, extracted(store, project, "by a person"), "dispute", "no")
    [item] = extraction_stats(store, pid)["extractors"]
    assert (item["confirmed"], item["confirmed_elsewhere"], item["disputed"]) == (0, 1, 1)
    assert (item["reviewed"], item["confirmed_share"]) == (1, 0.0)
    assert "1 more confirmed elsewhere" in render(extraction_stats(store, pid))


def test_a_record_a_person_edited_after_extraction_still_counts_for_its_extractor(workspace):
    store, project = workspace
    record = extracted(store, project, "to edit")
    # A person confirms, then the stamp must still be what the first version said, whatever later versions hold.
    review(store, project, record, "confirm")
    [item] = extraction_stats(store, project["id"])["extractors"]
    assert item["extractor_version"] == 1 and item["confirmed"] == 1


def test_a_session_edit_keeps_the_extractor_group_and_a_dispute_then_confirm_counts_as_confirmed(workspace):
    store, project = workspace
    pid = project["id"]
    record = extracted(store, project, "to rewrite")
    sources = [item["id"] for item in store.query("SELECT id FROM sources WHERE project_id=:p", {"p": pid})]
    edited = store.remember(pid, {"id": record["id"], "expected_version": record["version"],
                                  "kind": "fact", "content": "rewritten by a session"}, sources)
    assert edited["version"] > record["version"]
    [item] = extraction_stats(store, pid)["extractors"]
    assert item["extractor_version"] == 1 and item["proposed"] == 1 and item["total"] == 1
    disputed = review(store, project, edited, "dispute", "wrong")
    [item] = extraction_stats(store, pid)["extractors"]
    assert (item["disputed"], item["confirmed"], item["total"]) == (1, 0, 1)
    review(store, project, disputed, "confirm")
    [item] = extraction_stats(store, pid)["extractors"]
    assert (item["disputed"], item["confirmed"], item["total"]) == (0, 1, 1)


def test_records_written_by_hand_or_another_project_are_not_counted(workspace, tmp_path):
    store, project = workspace
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = store.add_project("other", other_root)
    extracted(store, other, "elsewhere")
    as_person(store, project["id"], {"kind": "fact", "content": "by hand"}, [hand_source(store, project)])
    assert extraction_stats(store, project["id"])["extractors"] == []
    assert extraction_stats(store, other["id"])["records"] == 1


def test_a_project_with_more_extractions_than_a_page_is_read_in_pages_and_a_cap_is_reported(workspace, monkeypatch):
    store, project = workspace
    for index in range(5):
        extracted(store, project, f"page {index}")
    monkeypatch.setattr(stats_module, "PAGE", 2)
    monkeypatch.setattr(stats_module, "CHUNK", 2)
    [item] = extraction_stats(store, project["id"])["extractors"]
    assert item["total"] == 5 and item["proposed"] == 5
    monkeypatch.setattr(stats_module, "MAX_RECORDS", 4)
    capped = extraction_stats(store, project["id"])
    assert capped["truncated"] is True and capped["records"] == 4
    # Exactly as many records as the cap: nothing was dropped, so it is not reported as truncated.
    monkeypatch.setattr(stats_module, "MAX_RECORDS", 5)
    exact = extraction_stats(store, project["id"])
    assert exact["truncated"] is False and exact["records"] == 5


def test_the_command_prints_numbers_and_json_and_never_record_text(workspace, monkeypatch):
    store, project = workspace
    review(store, project, extracted(store, project, "SECRET\x1b[31m text"), "confirm")
    monkeypatch.setattr(cli, "ensure_service", lambda *a, **k: pytest.fail("the command started the service"))
    runner = CliRunner()
    arguments = ["--data-dir", str(store.data_dir), "memory", "extraction-stats", "stats"]
    shown = runner.invoke(cli.app, arguments)
    assert shown.exit_code == 0, shown.output or repr(shown.exception)
    assert "Extractor v1: 1 records, 0 proposed, 1 confirmed" in shown.output
    assert "100% of the reviewed ones confirmed" in shown.output
    assert "SECRET" not in shown.output and "\x1b" not in shown.output
    payload = json.loads(runner.invoke(cli.app, [*arguments, "--json"]).output)
    assert payload["extractors"][0]["confirmed_share"] == 1.0 and "SECRET" not in json.dumps(payload)
    missing = runner.invoke(cli.app, ["--data-dir", str(store.data_dir), "memory", "extraction-stats", "nope"])
    assert missing.exit_code != 0


def test_the_inspector_api_gives_the_same_numbers_and_changes_nothing(workspace):
    store, project = workspace
    extracted(store, project, "api one")
    review(store, project, extracted(store, project, "api two"), "confirm")
    app = FastAPI()
    attach_inspector_routes(app, store, store.settings)
    client = TestClient(app)
    headers = {"Authorization": "Bearer " + store.settings.token}
    before = store.export()
    before.pop("exported_at", None)
    response = client.get("/inspector/extraction-stats", params={"project": project["id"]}, headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["extractors"] == extraction_stats(store, project["id"])["extractors"] and "cursor" in body
    assert client.get("/inspector/extraction-stats", params={"project": "nope"}, headers=headers).status_code == 404
    assert client.get("/inspector/extraction-stats", params={"project": project["id"]}).status_code == 401
    after = store.export()
    after.pop("exported_at", None)
    assert after == before
