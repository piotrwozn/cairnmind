"""An impact verdict and the release of a record need the open workspace's key (#306), on a real ArcadeDB."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from cairnmind import person_proof
from cairnmind.confirmation import ConfirmationRefused
from cairnmind.impact import build_impact
from cairnmind.impact_verdicts import StaleVerdict, record_verdict
from cairnmind.inspector_api import attach_inspector_routes
from cairnmind.memory_published import Publisher
from cairnmind.memory_published_release import SecretsRefused, release
from cairnmind.service import create_app
from conftest import person_proven, person_unproven
from test_impact import workspace as workspace
from test_impact_verdicts import changed as changed, entry, verdicts
from test_published_memory import reviewed
from test_published_memory_release import ada as ada
from test_published_memory_secrets import SHAPES

WRONG_KEY = {person_proof.KEY_HEADER: "cd" * 32}


def release_sources(store, pid):
    return store.query("SELECT id FROM sources WHERE project_id=:project AND uri LIKE 'release://%'", {"project": pid})


def test_a_verdict_needs_the_workspace_key(changed, workspace_person):
    store, pid, root, records = changed
    service = FastAPI()
    attach_inspector_routes(service, store, store.settings)
    client = TestClient(service)
    token = {"Authorization": "Bearer " + store.settings.token}
    shown = entry(build_impact(store, pid, "HEAD"), records["rule"])
    body = {"project": pid, "worktree": str(root), "since": "HEAD", "record_id": shown["id"],
            "fingerprint": shown["fingerprint"], "verdict": "still_valid"}
    with person_unproven():  # the saved path proves itself through the route, not through the test's own door
        refused = client.post("/inspector/impact/verdict", json=body, headers=token)
        assert refused.status_code == 403 and refused.json()["detail"] == person_proof.PROOF_MISSING
        wrong = client.post("/inspector/impact/verdict", json=body, headers={**token, **workspace_person, **WRONG_KEY})
        assert wrong.status_code == 403 and wrong.json()["detail"] == person_proof.PROOF_REFUSED
        assert verdicts(store, pid) == []
        saved = client.post("/inspector/impact/verdict", json=body, headers={**token, **workspace_person})
    assert saved.status_code == 200, saved.text
    assert saved.json()["verdict"]["verdict"] == "still_valid" and len(verdicts(store, pid)) == 1


def test_a_release_needs_the_workspace_key(ada, workspace_person):
    store, pid = ada["store"], ada["project"]
    record = reviewed(store, pid, "fact", f"Example {SHAPES['stripe_key']} from the Stripe guide.", "stripe")
    token = {"Authorization": f"Bearer {store.settings.token}"}
    url, body = f"/inspector/records/{record['id']}/release", {"project": "team", "reason": "The Stripe guide's example."}
    with person_unproven(), TestClient(create_app(store.settings, start_worker=False)) as client:
        refused = client.post(url, headers=token, json=body)
        assert refused.status_code == 403 and refused.json()["detail"] == person_proof.PROOF_MISSING
        wrong = client.post(url, headers={**token, **workspace_person, **WRONG_KEY}, json=body)
        assert wrong.status_code == 403 and wrong.json()["detail"] == person_proof.PROOF_REFUSED
        assert release_sources(store, pid) == []
        saved = client.post(url, headers={**token, **workspace_person}, json=body)
    assert saved.status_code == 200 and saved.json()["released"] == [f"fact/{record['id']}.md: stripe key in text"]
    assert len(release_sources(store, pid)) == 1
    assert f"fact/{record['id']}.md" in Publisher(store).publish(pid)["written"]


def test_the_verdict_and_release_functions_refuse_without_the_proof(changed, ada):
    store, pid, root, records = changed
    shown = entry(build_impact(store, pid, "HEAD"), records["rule"])
    args = (store, pid, records["rule"]["id"], shown["fingerprint"], "still_valid", "HEAD")
    sto, apid = ada["store"], ada["project"]
    example = reviewed(sto, apid, "fact", f"Example {SHAPES['aws_access_key']} from a guide.", "aws")
    secret = reviewed(sto, apid, "fact", f"Connect with {sto.settings.token} now.", "tok")
    with person_unproven():
        with pytest.raises(ConfirmationRefused) as refused:
            record_verdict(*args)
        assert str(refused.value) == person_proof.PROOF_MISSING and verdicts(store, pid) == []
        with pytest.raises(ConfirmationRefused) as refused:
            release(sto, apid, example["id"], "A documented example.")
        assert str(refused.value) == person_proof.PROOF_MISSING and release_sources(sto, apid) == []
        # What is wrong with the input or the state is still said first.
        with pytest.raises(StaleVerdict):
            record_verdict(store, pid, records["rule"]["id"], "0" * 64, "still_valid", "HEAD")
        with pytest.raises(SecretsRefused, match="registered secret"):
            release(sto, apid, secret["id"], "Anything.")
    with person_proven():
        record_verdict(*args)
        release(sto, apid, example["id"], "A documented example.")
    assert len(verdicts(store, pid)) == 1 and len(release_sources(sto, apid)) == 1


def test_what_is_not_a_persons_act_needs_no_key(changed, workspace_person):
    store, pid, root, records = changed
    service = FastAPI()
    attach_inspector_routes(service, store, store.settings)
    client = TestClient(service)
    token = {"Authorization": "Bearer " + store.settings.token}
    listed = client.get("/inspector/impact", params={"project": pid, "worktree": str(root), "since": "HEAD"},
                        headers=token)
    assert listed.status_code == 200
    assert any(row["id"] == records["rule"]["id"] and row["fingerprint"] for row in listed.json()["records"])
    bad = {"project": pid, "since": "HEAD", "record_id": "x", "fingerprint": "0" * 64, "verdict": "wrong-word"}
    assert client.post("/inspector/impact/verdict", json=bad, headers={**token, **workspace_person}).status_code == 422


def test_publishing_and_its_preview_need_no_key(ada):
    store, pid = ada["store"], ada["project"]
    record = reviewed(store, pid, "decision", "Use cookies.", "auth")
    token = {"Authorization": f"Bearer {store.settings.token}"}
    with TestClient(create_app(store.settings, start_worker=False)) as client:
        preview = client.post("/memory/publish", headers=token, json={"project": "team", "preview": True})
    assert preview.status_code == 200, preview.text
    assert f"decision/{record['id']}.md" in str(preview.json())
