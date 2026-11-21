"""Extract sources again after the extractor changed (#194): the plan, the queue, the supersession, the failure."""
from __future__ import annotations

import asyncio
import json

import pytest
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.agent import KnowledgeAgent
from cairnmind.inspector import Inspector
from cairnmind.reextract import plan, queue
from cairnmind.retrieval import json_value

from test_external_session_writes import fixture as fixture  # noqa: F401 - a fixture
from test_knowledge_mode import _store_choice

TEXT = "We decided that the cache stays off.\nThe build takes a minute."
NEW = {"kind": "decision", "key": "cache", "content": "We decided that the cache stays off.",
       "evidence": "We decided that the cache stays off", "metadata": {}}
VERSION = 2


def stamp(version):
    return {"version": version, "prompt_sha256": "0123456789abcdef", "model": "chat-model@abc123"}


class Model:
    def __init__(self, records):
        self.records = records

    async def chat(self, messages, tools=None, **kwargs):
        return {"message": {"role": "assistant", "content": json.dumps({"records": self.records})},
                "done_reason": "stop"}


class Broken:
    async def chat(self, messages, tools=None, **kwargs):
        return {"message": {"role": "assistant", "content": "not json"}, "done_reason": "stop"}


def source(f, name="a"):
    return f.store.snapshot(f.project["id"], f"note://{name}", "Raw " + name, "note",
                            metadata={"normalized_text": TEXT, "role": "user"})


def old(f, src, content, key, version=1):
    return f.store.remember(f.project["id"], {"kind": "decision", "content": content, "key": key}, [src["id"]],
                            extracted_from=src["id"], extractor=stamp(version) if version else None)


def run(f, records, model=None):
    job = f.store.claim_job("reextract-worker", lease_seconds=120, kinds=["extract"])
    assert job
    asyncio.run(KnowledgeAgent(f.store, f.settings, model or Model(records)).extract(job))
    return job


def record(f, record_id):
    return f.store.query("SELECT FROM records WHERE id=:id", {"id": record_id})[0]


def test_the_plan_selects_only_untouched_older_proposals_and_the_dry_run_matches_the_queue(fixture):  # noqa: F811
    f, pid = fixture, fixture.project["id"]
    a, b = source(f, "a"), source(f, "b")
    old(f, a, "one", "k1", 1)
    old(f, a, "none", "k0", 0)  # no stamp counts as version 0
    confirmed = old(f, a, "confirmed", "k2", 1)
    Inspector(f.store).review(pid, confirmed["id"], "confirm", 1)
    disputed = old(f, a, "disputed", "k3", 1)
    Inspector(f.store).review(pid, disputed["id"], "dispute", 1, reason="wrong")
    old(f, b, "current", "k4", VERSION)
    shown = plan(f.store, pid, VERSION, VERSION)
    assert [(v["extractor_version"], v["records"], v["sources"]) for v in shown["versions"]] == [(0, 1, 1), (1, 1, 1)]
    assert shown["would_queue"] == [a["id"]] and shown["already_queued"] == []
    assert f.store.query("SELECT FROM jobs WHERE kind='extract'") == []  # a plan queues nothing
    first = queue(f.store, pid, VERSION, VERSION)
    assert first["would_queue"] == shown["would_queue"] and first["queued"] == 1
    [job] = f.store.query("SELECT FROM jobs WHERE kind='extract'")
    assert job["dedupe_key"] == f"extract:{a['id']}:v{VERSION}"
    again = queue(f.store, pid, VERSION, VERSION)
    assert again["queued"] == 0 and again["already_queued"] == [a["id"]]
    assert len(f.store.query("SELECT FROM jobs WHERE kind='extract'")) == 1


def test_a_commit_supersedes_the_untouched_older_proposals_and_nothing_else(fixture):  # noqa: F811
    f, pid = fixture, fixture.project["id"]
    a = source(f)
    twin = old(f, a, NEW["content"], "old-cache")
    other = old(f, a, "The build takes an hour.", "build")
    confirmed = old(f, a, "We decided something else.", "else")
    Inspector(f.store).review(pid, confirmed["id"], "confirm", 1)
    disputed = old(f, a, "Disputed claim.", "claim")
    Inspector(f.store).review(pid, disputed["id"], "dispute", 1, reason="wrong")
    confirmed, disputed = record(f, confirmed["id"]), record(f, disputed["id"])
    queue(f.store, pid, VERSION, VERSION)
    run(f, [NEW])
    [new] = [r for r in f.store.list_records(pid) if r["metadata"].get("extractor") == stamp_of(f)]
    assert new["status"] == "proposed" and new["supersedes"] == twin["id"] and new["key"] == "cache"
    for gone in (twin, other):
        assert record(f, gone["id"])["status"] == "superseded"
        history = f.store.record_history(gone["id"])
        assert [h["status"] for h in history] == ["proposed", "superseded"]  # the proposal stays readable
        assert history[0]["content"] == gone["content"]
    assert record(f, confirmed["id"]) == confirmed and record(f, disputed["id"]) == disputed
    assert plan(f.store, pid, VERSION, VERSION)["would_queue"] == []


def stamp_of(f):
    return f.store.query("SELECT metadata.extractor AS e FROM records WHERE metadata.extractor.version=:v",
                         {"v": VERSION})[0]["e"]


def test_a_failed_reextraction_leaves_the_old_proposals_as_they_are(fixture):  # noqa: F811
    f, pid = fixture, fixture.project["id"]
    a = source(f)
    kept = old(f, a, "The build takes an hour.", "build")
    queue(f.store, pid, VERSION, VERSION)
    with pytest.raises(ValueError):
        run(f, None, Broken())
    assert record(f, kept["id"]) == f.store.query("SELECT FROM records WHERE id=:id", {"id": kept["id"]})[0]
    assert record(f, kept["id"])["status"] == "proposed" and record(f, kept["id"])["version"] == 1
    assert record(f, kept["id"])["metadata"].get("extraction_source_id") == a["id"]
    assert len(f.store.record_history(kept["id"])) == 1


def test_an_extraction_that_proposes_nothing_supersedes_nothing(fixture):  # noqa: F811
    f, pid = fixture, fixture.project["id"]
    a = source(f)
    kept = old(f, a, "The build takes an hour.", "build")
    queue(f.store, pid, VERSION, VERSION)
    run(f, [])
    assert record(f, kept["id"])["status"] == "proposed"


def test_superseding_untouched_proposals_does_not_make_an_acknowledgement_stale(fixture):  # noqa: F811
    f, pid = fixture, fixture.project["id"]
    a = source(f)
    old(f, a, "The build takes an hour.", "build")
    bound = f.session("external")  # the packet shows the proposal; the ACK does not promise it (#256)
    queue(f.store, pid, VERSION, VERSION)
    run(f, [NEW])
    view = f.client.get(f"/sessions/{bound['session_id']}").json()
    assert view["acknowledged"] is True and view["stale"] is False, view


def test_the_command_dry_runs_queues_once_and_refuses_without_models(fixture, tmp_path):  # noqa: F811
    f = fixture
    a = source(f)
    old(f, a, "The build takes an hour.", "build")
    base =["--data-dir", str(f.settings.data_dir), "memory", "reextract", "external-writes"]
    runner = CliRunner()
    dry = runner.invoke(cli.app, [*base, "--dry-run"])
    assert dry.exit_code == 0, dry.output or repr(dry.exception)
    assert json.loads(dry.stdout)["would_queue"] == [a["id"]]
    assert f.store.query("SELECT FROM jobs WHERE kind='extract'") == []
    assert runner.invoke(cli.app, [*base, "--below-version", str(VERSION + 1)]).exit_code == 2
    first = runner.invoke(cli.app, base)
    assert first.exit_code == 0, first.output or repr(first.exception)
    assert json.loads(first.stdout)["queued"] == 1
    assert json.loads(runner.invoke(cli.app, base).stdout)["queued"] == 0
    assert json_value(f.store.query("SELECT FROM jobs WHERE kind='extract'")[0]["payload"], {})["source_id"] == a["id"]
    (tmp_path / "off").mkdir()
    disabled = _store_choice(tmp_path / "off", "disabled")
    refused = runner.invoke(cli.app, ["--data-dir", str(disabled), "memory", "reextract", "demo"])
    assert refused.exit_code == 2 and "cairnmind memory reextract" in " ".join(refused.stderr.split())
