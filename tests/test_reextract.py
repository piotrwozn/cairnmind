"""Extract sources again after the extractor changed (#194): the plan, the queue, the supersession, the failure."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import itertools
import json
import uuid

import pytest
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind import store as store_module
from cairnmind.agent import KnowledgeAgent
from cairnmind.inspector import Inspector
from cairnmind.memory_egress import release_held_extractions
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


def run(f, records, model=None, agent=None):
    job = f.job = f.store.claim_job("reextract-worker", lease_seconds=120, kinds=["extract"])
    assert job
    asyncio.run((agent or KnowledgeAgent(f.store, f.settings, model or Model(records))).extract(job))
    return job


def counts(f):
    return f.store.job_status(f.project["id"], f.job["id"])["diagnostics"]["counts"]


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
    assert counts(f)["superseded_records"] == 2


def test_the_holder_of_the_key_is_replaced_first_whatever_the_order_of_ids(fixture, monkeypatch):  # noqa: F811
    f, pid = fixture, fixture.project["id"]
    a = source(f)
    ids = itertools.count(1)
    with monkeypatch.context() as patched:
        patched.setattr(store_module, "uuid4", lambda: uuid.UUID(int=next(ids)))
        same_text = old(f, a, NEW["content"], "cache-policy")  # sorts first and says what the new record says
        holder = old(f, a, "The build takes an hour.", "cache")  # holds the new record's key
    assert same_text["id"] < holder["id"]
    queue(f.store, pid, VERSION, VERSION)
    run(f, [NEW])
    [new] = [r for r in f.store.list_records(pid) if r["metadata"].get("extractor") == stamp_of(f)]
    assert new["key"] == "cache" and new["supersedes"] == holder["id"]
    assert record(f, holder["id"])["status"] == record(f, same_text["id"])["status"] == "superseded"
    assert counts(f)["superseded_records"] == 2


def test_a_failure_after_the_supersession_rolls_all_of_it_back(fixture):  # noqa: F811
    f, pid = fixture, fixture.project["id"]
    a = source(f)
    twin = old(f, a, NEW["content"], "old-cache")
    other = old(f, a, "The build takes an hour.", "build")
    queue(f.store, pid, VERSION, VERSION)
    agent = KnowledgeAgent(f.store, f.settings, Model([NEW]))
    committed, real = [], agent._commit_extraction

    def commit(*args):
        real(*args)
        committed.append(True)

    def deadline(_):
        if committed:  # the commit wrote and superseded everything; now the time is up
            raise TimeoutError

    agent._commit_extraction, agent._extraction_deadline = commit, deadline
    with pytest.raises(TimeoutError):
        run(f, None, agent=agent)
    assert committed
    for kept in (twin, other):
        assert record(f, kept["id"])["status"] == "proposed" and record(f, kept["id"])["version"] == 1
        assert len(f.store.record_history(kept["id"])) == 1
    assert not [r for r in f.store.list_records(pid) if r["metadata"].get("extractor", {}).get("version") == VERSION]
    assert counts(f)["written_records"] == 0 and "superseded_records" not in counts(f)


def test_an_answer_that_writes_nothing_supersedes_nothing(fixture):  # noqa: F811
    f, pid = fixture, fixture.project["id"]
    a = source(f)
    kept = old(f, a, "The build takes an hour.", "build")
    confirmed = old(f, a, NEW["content"], "said", VERSION)
    Inspector(f.store).review(pid, confirmed["id"], "confirm", 1)
    queue(f.store, pid, VERSION, VERSION)
    run(f, [NEW])  # its only record says what the confirmed one says: skipped
    assert counts(f)["written_records"] == 0 and "superseded_records" not in counts(f)
    assert record(f, kept["id"])["status"] == "proposed"


def test_an_old_task_whose_dependency_was_retired_is_still_superseded(fixture):  # noqa: F811
    f, pid = fixture, fixture.project["id"]
    a = source(f)
    needed = f.store.remember(pid, {"kind": "task", "content": "Prepare the build.", "key": "prepare"}, [a["id"]])
    task = f.store.remember(pid, {"kind": "task", "content": "Run the build.", "key": "run",
                                  "metadata": {"depends_on": [needed["id"]]}}, [a["id"]],
                            extracted_from=a["id"], extractor=stamp(1))
    f.store.remember(pid, {"id": needed["id"], "expected_version": 1, "status": "superseded"}, [a["id"]])
    queue(f.store, pid, VERSION, VERSION)
    run(f, [NEW])
    assert record(f, task["id"])["status"] == "superseded" and counts(f)["superseded_records"] == 1


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
    gone = old(f, a, "The build takes an hour.", "build")
    bound = f.session("external")  # the packet shows the proposal; the ACK does not promise it (#256)
    queue(f.store, pid, VERSION, VERSION)
    run(f, [NEW])
    assert record(f, gone["id"])["status"] == "superseded"
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


def own_block_source(f):
    from cairnmind.integrations import BEGIN, END
    return f.store.snapshot(f.project["id"], "file:///repo/AGENTS.md", f"{BEGIN}\nFIRST call resume_context.\n{END}\n",
                            "document", metadata={"path": "/repo/AGENTS.md"})


def test_a_reextraction_of_a_source_the_extractor_skips_supersedes_its_untouched_old_proposals(fixture):  # noqa: F811
    f, pid = fixture, fixture.project["id"]
    a = own_block_source(f)
    first, second = old(f, a, "Call resume_context first.", "resume", 1), old(f, a, "Another.", "other", 0)
    confirmed = old(f, a, "Confirmed one.", "kept", 1)
    Inspector(f.store).review(pid, confirmed["id"], "confirm", 1)
    disputed = old(f, a, "Disputed one.", "wrong", 1)
    Inspector(f.store).review(pid, disputed["id"], "dispute", 1, reason="wrong")
    current = old(f, a, "Written by this extractor.", "now", VERSION)
    confirmed, disputed, current = (record(f, r["id"]) for r in (confirmed, disputed, current))
    shown = plan(f.store, pid, VERSION, VERSION)
    assert shown["skipped_sources"] == [a["id"]] and shown["skipped_records"] == 2
    bound = f.session("external")
    queue(f.store, pid, VERSION, VERSION)
    run(f, [NEW])
    for gone in (first, second):
        assert record(f, gone["id"])["status"] == "superseded"
        assert "no longer extracted" in record(f, gone["id"])["metadata"]["superseded_reason"]
        assert [h["status"] for h in f.store.record_history(gone["id"])] == ["proposed", "superseded"]
    assert [record(f, r["id"]) for r in (confirmed, disputed, current)] == [confirmed, disputed, current]
    assert counts(f)["superseded_records"] == 2 and counts(f)["written_records"] == 0
    assert not [r for r in f.store.list_records(pid) if r["metadata"].get("extractor", {}).get("version") == VERSION
                and r["id"] != current["id"]]
    view = f.client.get(f"/sessions/{bound['session_id']}").json()
    assert view["acknowledged"] is True and view["stale"] is False, view


def test_an_ordinary_extraction_of_a_skipped_source_supersedes_nothing(fixture):  # noqa: F811
    f, pid = fixture, fixture.project["id"]
    a = own_block_source(f)
    kept = old(f, a, "Call resume_context first.", "resume", 1)
    f.store.enqueue("extract", pid, {"source_id": a["id"]}, "extract:ordinary")
    run(f, [NEW])
    assert record(f, kept["id"])["status"] == "proposed"


def test_a_failure_while_superseding_for_a_skipped_source_changes_nothing(fixture, monkeypatch):  # noqa: F811
    f, pid = fixture, fixture.project["id"]
    a = own_block_source(f)
    kept = [old(f, a, "One.", "one"), old(f, a, "Two.", "two")]
    queue(f.store, pid, VERSION, VERSION)
    real, calls = f.store.remember, []

    def remember(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise ValueError("disk full")
        return real(*args, **kwargs)

    monkeypatch.setattr(f.store, "remember", remember)
    with pytest.raises(ValueError):
        run(f, [NEW])
    assert len(calls) == 2
    for r in kept:
        assert record(f, r["id"])["status"] == "proposed" and record(f, r["id"])["version"] == 1
        assert len(f.store.record_history(r["id"])) == 1
    diagnostics = f.store.job_status(pid, f.job["id"])["diagnostics"]  # #311: the failure names its stage
    assert diagnostics["code"] == "persistence_failed" and diagnostics["persistence_stage"] == "supersede_records"


def test_the_dry_run_counts_a_masked_source_as_read_because_only_the_job_can_decide(fixture):  # noqa: F811
    f, pid = fixture, fixture.project["id"]
    plain = own_block_source(f)
    masked = f.store.snapshot(pid, "file:///repo/CLAUDE.md", own_block_source(f)["content"], "document",
                              metadata={"path": "/repo/CLAUDE.md", "masked": True})
    assert masked["id"] != plain["id"]
    old(f, plain, "One.", "one")
    old(f, masked, "Two.", "two")
    shown = plan(f.store, pid, VERSION, VERSION)
    assert shown["skipped_sources"] == [plain["id"]]  # the masked one is counted as read, the job decides
    assert shown["skipped_records"] == 1 and masked["id"] in shown["would_queue"]


def test_a_held_back_reextract_job_keeps_its_flag_when_released_and_supersedes_then(fixture):  # noqa: F811
    f, pid = fixture, fixture.project["id"]
    a = f.store.snapshot(pid, "conversation://1", "<environment_context>cwd</environment_context>", "transcript",
                         metadata={"role": "user"})  # the extractor skips it; its class (conversation) is held
    kept = old(f, a, "The environment.", "env")
    queue(f.store, pid, VERSION, VERSION)
    remote = replace(f.settings, memory_chat={"provider": "openai_compatible", "base_url": "https://models.example/v1",
                                              "allow_remote": True})
    held = run(f, [NEW], agent=KnowledgeAgent(f.store, remote, Model([NEW])))
    assert f.store.job_status(pid, held["id"])["diagnostics"]["code"] == "remote_not_allowed"
    f.store.finish_job(held["id"], worker=held["worker"], expected_attempts=held["attempts"])
    assert record(f, kept["id"])["status"] == "proposed"
    allowed = replace(remote, memory_remote_classes=["project", "conversation"])
    assert release_held_extractions(f.store, allowed) == 1
    released = run(f, [NEW], agent=KnowledgeAgent(f.store, allowed, Model([NEW])))
    assert json_value(released["payload"], {})["reextract"] is True
    assert f.store.job_status(pid, released["id"])["diagnostics"]["code"] == "source_skipped"
    assert record(f, kept["id"])["status"] == "superseded" and counts(f)["superseded_records"] == 1
