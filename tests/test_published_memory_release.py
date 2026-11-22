"""The secrets gate of published memory (#171) on real ArcadeDB and Git: refusals, releases, round trips."""
from __future__ import annotations

from fastapi.testclient import TestClient
import pytest
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.api_credentials import CredentialStore
from cairnmind.memory import MemoryConflict
from cairnmind.memory_published import Publisher
from cairnmind.memory_published_check import check_directory
from cairnmind.memory_published_import import PublishedImporter
from cairnmind.memory_published_release import (
    SecretsRefused, checkout_rules, import_rules, registered_secrets, release,
)
from cairnmind.service import create_app
from cairnmind.store import Store
from test_published_memory import commit, folder_of, git, published_bytes, repository, reviewed, teammate
from test_published_memory_secrets import KEY_BLOCK, SHAPES


@pytest.fixture
def ada(tmp_path):
    root = repository(tmp_path / "ada-repository")
    store = Store(tmp_path / "ada-database")
    return {"store": store, "project": store.add_project("team", root)["id"], "root": root, "tmp": tmp_path}


def test_no_secret_mode_is_needed_any_more_and_a_clean_record_is_written(ada, monkeypatch):
    monkeypatch.delenv("CAIRNMIND_ENABLE_PUBLICATION", raising=False)
    record = reviewed(ada["store"], ada["project"], "decision", "Use cookies.", "auth")
    assert Publisher(ada["store"]).publish(ada["project"])["written"][-1] == f"decision/{record['id']}.md"


@pytest.mark.parametrize("secret", ["token", "arcade", "saved", "environment"])
def test_a_registered_secret_refuses_the_whole_publication_naming_the_record_and_nothing_of_the_secret(
        ada, monkeypatch, secret):
    store, pid, root = ada["store"], ada["project"], ada["root"]
    value = {"token": store.settings.token, "arcade": store.settings.arcade_password,
             "saved": "plain saved key 1", "environment": "plain env key 22"}[secret]
    if secret == "saved":
        CredentialStore(store.settings).set("openai-main", value.replace(" ", "-"), persist=True)
        value = value.replace(" ", "-")
    if secret == "environment":
        monkeypatch.setenv("OPENAI_API_KEY", value)
    clean = reviewed(store, pid, "fact", "The service listens on localhost only.", "listen")
    leaked = reviewed(store, pid, "decision", f"Connect with {value} for now.", "connect")
    for preview in (True, False):
        with pytest.raises(SecretsRefused) as refused:
            Publisher(store).publish(pid, preview=preview)
        message = str(refused.value)
        assert f"decision/{leaked['id']}.md: registered secret in text" in message
        assert value not in message and value[:6] not in message and clean["id"] not in message
    assert not folder_of(root).exists()
    with pytest.raises(SecretsRefused, match="A registered secret is never released"):
        release(store, pid, leaked["id"], "It is only a test value.")


def test_a_private_key_or_a_provider_key_in_a_record_is_not_published(ada):
    store, pid = ada["store"], ada["project"]
    key = reviewed(store, pid, "constraint", f"Deploy with\n{KEY_BLOCK}", "deploy-key")
    token = reviewed(store, pid, "fact", f"CI uses {SHAPES['github_token']}.", "ci-token")
    with pytest.raises(SecretsRefused) as refused:
        Publisher(store).publish(pid)
    message = str(refused.value)
    assert f"constraint/{key['id']}.md: private key in text" in message
    assert f"fact/{token['id']}.md: github token in text" in message
    assert "MIIE" not in message and SHAPES["github_token"][4:12] not in message


def test_a_released_record_is_published_with_its_reason_checked_in_ci_and_carried_by_a_teammate(ada):
    store, pid, root = ada["store"], ada["project"], ada["root"]
    example = SHAPES["aws_access_key"]
    record = reviewed(store, pid, "fact", f"The AWS documentation uses {example} as its example key.", "aws-example")
    with pytest.raises(SecretsRefused, match="aws access key in text"):
        Publisher(store).publish(pid)
    with pytest.raises(ValueError, match="reason"):
        release(store, pid, record["id"], "   ")
    with pytest.raises(SecretsRefused, match="reason itself"):
        release(store, pid, record["id"], f"Same as {example}")
    released = release(store, pid, record["id"], "The AWS documentation example, not a real key.")
    assert released["released"] == [f"fact/{record['id']}.md: aws access key in text"]
    assert example not in str(released)
    first = Publisher(store).publish(pid)
    path = f"fact/{record['id']}.md"
    assert path in first["written"]
    text = (folder_of(root) / path).read_text()
    assert "The AWS documentation example, not a real key." in text and '{"at":' in text and '"kind":"release"}' in text
    assert check_directory(folder_of(root))[1] == []
    assert Publisher(store).publish(pid)["status"] == "unchanged"
    with pytest.raises(ValueError, match="Nothing in this record needs a release"):
        release(store, pid, record["id"], "Again.")
    commit(root, "memory")
    bo = teammate(ada, "bo", "Bo Lee")
    PublishedImporter(bo["store"]).import_published(folder_of(bo["root"]), bo["project"])
    before = published_bytes(bo["root"])
    assert Publisher(bo["store"]).publish(bo["project"])["status"] == "unchanged"
    assert published_bytes(bo["root"]) == before


def test_the_release_route_and_command_refuse_a_coding_session(ada, monkeypatch, workspace_person):
    store, pid = ada["store"], ada["project"]
    record = reviewed(store, pid, "fact", f"Example {SHAPES['stripe_key']} from the Stripe guide.", "stripe")
    headers = {"Authorization": f"Bearer {store.settings.token}"}
    with TestClient(create_app(store.settings, start_worker=False)) as client:
        body = {"project": "team", "reason": "The Stripe guide's example."}
        assert client.post(f"/inspector/records/{record['id']}/release", json=body).status_code == 401
        # The token alone is not a person (#306); the open workspace's key is.
        assert client.post(f"/inspector/records/{record['id']}/release", headers=headers, json=body).status_code == 403
        answer = client.post(f"/inspector/records/{record['id']}/release", headers={**headers, **workspace_person},
                             json=body)
        assert answer.status_code == 200 and answer.json()["released"] == [f"fact/{record['id']}.md: stripe key in text"]
    monkeypatch.setenv("CAIRNMIND_SESSION_ID", "session-1")
    refused = CliRunner().invoke(cli.app, ["--data-dir", str(store.data_dir), "memory", "release", "team", record["id"],
                                           "--reason", "Anything."])
    assert refused.exit_code != 0 and "A coding session cannot release a record" in refused.output


def test_a_rule_turned_on_after_publishing_never_deadlocks_and_a_teammates_file_is_reported_not_blocking(ada):
    store, pid, root = ada["store"], ada["project"], ada["root"]
    own = reviewed(store, pid, "fact", "Ask ada@example.org about deploys.", "deploy-owner")
    Publisher(store).publish(pid)
    commit(root, "ada memory")
    bo = teammate(ada, "bo", "Bo Lee")
    PublishedImporter(bo["store"]).import_published(folder_of(bo["root"]), bo["project"])
    other = reviewed(bo["store"], bo["project"], "fact", "Ask bo@example.org about releases.", "release-owner")
    Publisher(bo["store"]).publish(bo["project"])
    commit(bo["root"], "bo memory")
    git(root, "fetch", "-q", str(bo["root"]), "main")
    git(root, "merge", "-q", "--no-edit", "FETCH_HEAD")
    (root / ".cairnmind").mkdir()
    (root / ".cairnmind" / "publication.toml").write_text('[personal]\ndetect = ["email"]\n')
    with pytest.raises(SecretsRefused) as refused:
        Publisher(store).publish(pid)
    assert f"fact/{own['id']}.md: email in text" in str(refused.value) and other["id"] not in str(refused.value)
    release(store, pid, own["id"], "The deploy owner's work address, agreed to be shared.")
    result = Publisher(store).publish(pid)
    assert result["status"] == "written" and f"fact/{own['id']}.md" in result["written"]
    assert result["not_ours"]["hits"] == [f"fact/{other['id']}.md: email in text"]
    assert "bo@example.org" not in str(result) and f"fact/{other['id']}.md" not in result["written"]
    assert Publisher(store).publish(pid)["status"] == "unchanged"
    # The import applies the checkout's patterns too: nothing comes in that this store would refuse.
    with pytest.raises(MemoryConflict, match=f"fact/{other['id']}.md: email found in text"):
        PublishedImporter(store).import_published(folder_of(root), pid)
    assert check_directory(folder_of(root), checkout_rules(folder_of(root)))[1] == [
        (f"fact/{other['id']}.md", "email found in text")]


def test_the_import_refuses_this_machines_registered_secrets_and_session_keys_are_not_claimed(ada):
    store = ada["store"]
    rules = import_rules(folder_of(ada["root"]), store.settings)
    assert store.settings.token in rules.registered and store.settings.arcade_password in rules.registered
    other = CredentialStore(store.settings)
    other.set("openai-main", "plain-session-key-1", persist=False)
    assert "plain-session-key-1" in other.registered_values()
    # A new instance, as the memory service builds, holds only the saved keys (documented, not claimed).
    assert "plain-session-key-1" not in registered_secrets(store.settings, environ={})
