"""Published team memory replaces a person's own verdicts only for the list the person was shown (#304).

Real HTTP service, ArcadeDB and Git; no model. The workspace is stood in for by `fixture.person`, the headers its
panels send (#214). A teammate's store is a clone of the project's repository.
"""
from __future__ import annotations

import re

import pytest

from cairnmind.terminal import person_proof
from cairnmind.memory.confirmation import REPLACED_CHANGED
from cairnmind.terminal.inspector import Inspector
from cairnmind.memory.memory_published import PUBLICATION_SWITCH, Publisher
from cairnmind.memory.memory_published_import import PublishedImporter
from cairnmind.store import Store

from test_confirmed_by_person import fixture  # noqa: F401 (the fixture)
from test_confirmed_elsewhere import rows
from test_published_memory import commit, edited, folder_of, git, identify, reviewed

FULL_PATH = "The published memory folder must be given as a full path. Nothing was imported."


class Team:
    """The person's records (one confirmed, one disputed, both published) and a teammate's later versions."""

    def __init__(self, fixture, tmp_path):  # noqa: F811
        self.fixture = fixture
        store, pid, root = fixture.store, fixture.pid, fixture.root
        self.confirmed = reviewed(store, pid, "constraint", "Never push to main without review.", "review")
        self.disputed = reviewed(store, pid, "fact", "Sessions last 24 hours.", "sessions")
        self.disputed = Inspector(store).review(pid, self.disputed["id"], "dispute", self.disputed["version"],
                                                reason="They last 12 hours")["record"]
        Publisher(store).publish(pid)
        commit(root, "Publish")
        self.clone = tmp_path / "bo-repository"
        git(tmp_path, "clone", "-q", str(root), str(self.clone))
        identify(self.clone, "Bo Lee")
        self.bo = Store(tmp_path / "bo-database")
        self.bo_pid = self.bo.add_project("team", self.clone)["id"]
        PublishedImporter(self.bo).import_published(folder_of(self.clone), self.bo_pid)
        edited(self.bo, self.bo_pid, self.confirmed, "Push to main directly; reviews are not needed.")
        edited(self.bo, self.bo_pid, self.disputed, "Sessions last 24 hours, as measured.")
        Publisher(self.bo).publish(self.bo_pid)
        commit(self.clone, "Bo's versions")
        self.paths = [f"constraint/{self.confirmed['id']}.md", f"fact/{self.disputed['id']}.md"]

    @property
    def folder(self):
        return {"project": self.fixture.pid, "path": str(folder_of(self.clone))}

    def pull(self):
        git(self.fixture.root, "pull", "-q", "--ff-only", str(self.clone), "main")

    def post(self, route, body, person=True):
        return self.fixture.client.post(route, json=body, headers=self.fixture.person if person else None)

    def mine(self):
        return {key: {name: row[name] for name in ("content", "status", "version", "metadata")}
                for key, row in rows(self.fixture.store, self.fixture.pid).items()}


@pytest.fixture
def team(fixture, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv(PUBLICATION_SWITCH, "1")
    return Team(fixture, tmp_path)


def test_a_folder_import_replaces_a_confirmed_and_a_disputed_verdict_only_with_the_key(team):
    route, paths = "/memory/import-published", team.paths
    dry = team.post(route, {**team.folder, "dry_run": True}, person=False)
    assert dry.status_code == 200 and dry.json()["replaces_confirmed_here"]["records"] == paths, dry.text
    before = team.mine()
    asked = {**team.folder, "replace_confirmed": True, "expected_replaced": paths}
    refused = team.post(route, asked, person=False)
    assert refused.status_code == 403 and refused.json()["detail"] == person_proof.IMPORT_REPLACE, refused.text
    assert team.mine() == before
    done = team.post(route, asked)
    assert done.status_code == 200 and done.json()["status"] == "imported", done.text
    now = rows(team.fixture.store, team.fixture.pid)
    assert now[team.confirmed["id"]]["content"] == "Push to main directly; reviews are not needed."
    assert now[team.disputed["id"]]["content"] == "Sessions last 24 hours, as measured."
    for record in (team.confirmed, team.disputed):
        assert now[record["id"]]["metadata"]["confirmation"]["by"] == "import"


def test_a_commit_import_replaces_a_verdict_only_with_the_key_at_the_commit_read(team):
    team.pull()
    route, body = "/memory/import-commit", {"project": team.fixture.pid}
    dry = team.post(route, {**body, "revision": "HEAD", "dry_run": True}, person=False)
    assert dry.status_code == 200, dry.text
    at = dry.json()["commit"]
    assert re.fullmatch(r"[0-9a-f]{40}", at) and dry.json()["replaces_confirmed_here"]["records"] == team.paths
    before = team.mine()
    asked = {**body, "revision": at, "replace_confirmed": True, "expected_replaced": team.paths}
    refused = team.post(route, asked, person=False)
    assert refused.status_code == 403 and refused.json()["detail"] == person_proof.IMPORT_REPLACE, refused.text
    assert team.mine() == before
    done = team.post(route, asked)
    assert done.status_code == 200 and done.json()["commit"] == at, done.text
    now = rows(team.fixture.store, team.fixture.pid)
    assert now[team.confirmed["id"]]["content"] == "Push to main directly; reviews are not needed."
    assert now[team.confirmed["id"]]["metadata"]["confirmation"]["by"] == "import"
    assert now[team.disputed["id"]]["content"] == "Sessions last 24 hours, as measured."
    assert now[team.disputed["id"]]["metadata"]["confirmation"]["by"] == "import"


@pytest.mark.parametrize("carrier", ["folder", "commit"])
def test_an_import_whose_replacements_changed_since_the_check_is_refused(team, carrier):
    if carrier == "folder":
        route, body = "/memory/import-published", team.folder
    else:
        team.pull()
        route, body = "/memory/import-commit", {"project": team.fixture.pid, "revision": "HEAD"}
    before = team.mine()
    for listed in ([], team.paths[:1], [*team.paths, "decision/00000000-0000-4000-8000-000000000000.md"]):
        response = team.post(route, {**body, "replace_confirmed": True, "expected_replaced": listed})
        assert response.status_code == 409 and REPLACED_CHANGED in response.json()["detail"], response.text
        assert team.mine() == before
    # The list as shown is accepted, in any order.
    ok = team.post(route, {**body, "replace_confirmed": True, "expected_replaced": team.paths[::-1]})
    assert ok.status_code == 200, ok.text


def test_taking_the_published_side_over_a_local_dispute_needs_the_key(team):
    store, pid = team.fixture.store, team.fixture.pid
    mine = reviewed(store, pid, "decision", "Use cookies.", "auth")
    Publisher(store).publish(pid)
    commit(team.fixture.root, "Publish the decision")
    PublishedImporter(team.bo).import_published(folder_of(team.fixture.root), team.bo_pid)
    edited(team.bo, team.bo_pid, mine, "Use signed cookies.")
    Publisher(team.bo).publish(team.bo_pid)
    current = next(r for r in store.list_records(pid) if r["id"] == mine["id"])
    Inspector(store).review(pid, mine["id"], "dispute", current["version"], reason="Not any more")
    path, route = f"decision/{mine['id']}.md", "/memory/import-published"
    refused = team.post(route, {**team.folder, "dry_run": True})
    assert refused.status_code == 409 and path in refused.json()["detail"], refused.text
    take = {**team.folder, "take_published": [mine["id"]]}
    dry = team.post(route, {**take, "dry_run": True})
    assert dry.status_code == 200 and dry.json()["taken"] == [path], dry.text
    assert path in dry.json()["replaces_confirmed_here"]["records"]
    listed = dry.json()["replaces_confirmed_here"]["records"]
    versions = len(store.record_history(mine["id"]))
    # With the key, taking without replace_confirmed is the 409 that names Team memory, and writes nothing.
    plain = team.post(route, {**take, "expected_replaced": listed})
    assert plain.status_code == 409 and "Team memory" in plain.json()["detail"], plain.text
    assert REPLACED_CHANGED not in plain.json()["detail"] and len(store.record_history(mine["id"])) == versions
    asked = {**take, "replace_confirmed": True, "expected_replaced": listed}
    assert team.post(route, asked, person=False).status_code == 403
    assert len(store.record_history(mine["id"])) == versions
    done = team.post(route, asked)
    assert done.status_code == 200 and done.json()["taken"] == [path], done.text
    head = next(r for r in store.list_records(pid) if r["id"] == mine["id"])
    assert head["content"] == "Use signed cookies." and len(store.record_history(mine["id"])) == versions + 1


@pytest.mark.parametrize("dry_run", [True, False])
def test_a_relative_published_path_is_refused_before_anything_is_read(team, dry_run):
    count = lambda: team.fixture.store.query("SELECT count(*) AS n FROM sources")[0]["n"]  # noqa: E731
    before, state = count(), team.mine()
    response = team.post("/memory/import-published", {"project": team.fixture.pid, "path": "relative/folder",
                                                       "dry_run": dry_run})
    assert response.status_code == 400 and response.json()["detail"] == FULL_PATH, response.text
    assert count() == before and team.mine() == state


@pytest.mark.parametrize("carrier", ["folder", "commit"])
def test_replacing_verdicts_without_the_shown_list_is_refused_even_with_the_key(team, carrier):
    if carrier == "folder":
        route, body = "/memory/import-published", team.folder
    else:
        team.pull()
        route, body = "/memory/import-commit", {"project": team.fixture.pid, "revision": "HEAD"}
    before = team.mine()
    asked = {**body, "replace_confirmed": True}
    # The command line has no key: it is refused for that first.
    assert team.post(route, asked, person=False).status_code == 403
    kept = team.post(route, asked)
    assert kept.status_code == 400 and "expected_replaced" in kept.json()["detail"], kept.text
    assert team.mine() == before
