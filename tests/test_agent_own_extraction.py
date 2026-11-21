"""A built-in agent's ACK and the extraction of its own conversation (#168), on the real ArcadeDB engine.

The engine archives its journal in the middle of a turn (a large tool result, a third-party result), and
the archive queues the turn's messages for extraction. Proposed records extracted from that same session's
messages are its own words restated: they must not make the ACK it just gave stale, and since #256 no
proposal the worker writes makes any session's ACK stale. Everything else that moves the acknowledged state
still does.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from cairnmind import memory as portable
from cairnmind.agent import KnowledgeAgent
from cairnmind.agent_journal import AgentJournal
from cairnmind.memory import MemoryManager
from cairnmind.sessions import SessionManager
from cairnmind.inspector import Inspector
from cairnmind.store import EXTRACTION_MARK, Store

PROMPT = ("In inventory.txt change only line 250. We decided that inventory.txt stays sorted by item name, "
          "and the file must keep exactly 400 lines.")
DECISION = "We decided that inventory.txt stays sorted by item name"
CONSTRAINT = "the file must keep exactly 400 lines"


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


class ScriptedModel:
    """Answers each extraction with the next scripted records; no provider is contacted."""
    def __init__(self, *replies):
        self.replies = list(replies)

    async def chat(self, messages, tools=None, **kwargs):
        return {"message": {"role": "assistant", "content": json.dumps({"records": self.replies.pop(0)})},
                "done_reason": "stop", "prompt_eval_count": 100, "eval_count": 30}


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "own-extraction@example.invalid")
    git(root, "config", "user.name", "Own Extraction")
    (root / "inventory.txt").write_text("".join(f"item {n:03d}\n" for n in range(1, 401)))
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    store = Store(tmp_path / "database")
    project = store.add_project("own-extraction", root)
    return store, project, root, SessionManager(store, store.settings)


def readback(packet):
    return {key: ({"id": packet[field]["id"], "version": packet[field]["version"]}
                  if packet.get(field) else None)
            for key, field in (("plan", "active_plan"), ("task", "active_task"))}


def acknowledged(manager, project, client="api"):
    """A session that has read and acknowledged its packet, with the person's prompt in its journal."""
    prepared = manager.prepare(project["id"], client, timeout=0)
    assert prepared["status"] == "ready", prepared["unknowns"]
    packet = prepared["packet"]
    manager.ack(prepared["session_id"], packet["id"], packet["hash"], readback(packet))
    manager.require_ack(prepared["session_id"])
    return prepared["session_id"]


def mid_turn_archive(store, manager, session_id):
    """What the engine does after a large read_file result: archive the journal, which queues extraction."""
    journal = AgentJournal(store.settings, session_id)
    journal.append("message", {"role": "user", "content": PROMPT})
    journal.append("message", {"role": "assistant", "content": "", "tool_calls": [
        {"id": "read-1", "name": "read_file", "arguments": {"path": "inventory.txt"}}]})
    journal.append("message", {"role": "tool", "name": "read_file", "content": "item 001\n" * 400})
    archive = manager.archive_agent_journal(session_id)
    assert archive["status"] == "complete"
    return archive


async def extract_queued(store, *replies):
    """Run every queued extraction job with the scripted model, as the background worker would."""
    model, written = ScriptedModel(*replies), []
    while job := store.claim_job("own-extraction-test", lease_seconds=120, kinds=["extract"]):
        before = {row["id"] for row in store.list_records(job["project_id"])}
        await KnowledgeAgent(store, store.settings, model).extract(job)
        written += [row for row in store.list_records(job["project_id"]) if row["id"] not in before]
    return written


def decision(quote=DECISION, kind="decision", key="inventory-order"):
    return {"kind": kind, "key": key, "content": quote + ".", "evidence": quote, "metadata": {}}


async def test_extraction_of_the_sessions_own_turn_mid_turn_keeps_its_ack(workspace):
    store, project, _, manager = workspace
    sid = acknowledged(manager, project)
    mid_turn_archive(store, manager, sid)
    # Only the person's message has text; the tool request and the tool result are never extracted.
    written = await extract_queued(store, [decision(), decision(CONSTRAINT, "constraint", "inventory-lines")])
    assert sorted(row["kind"] for row in written) == ["constraint", "decision"]
    assert all(row["status"] == "proposed" for row in written)
    manager.require_ack(sid)
    status = manager.status(sid)
    assert status["acknowledged"] is True and status["stale"] is False
    # Another session of the project sees them in its new packet, labelled.
    other = manager.prepare(project["id"], "claude", timeout=0)
    assert {row["id"] for row in written} <= {row["id"] for row in other["packet"]["records"]}
    # Reading the context again hands back the packet it acknowledged, not a new one to acknowledge.
    assert manager.resume_context(sid)["packet"]["id"] == manager._session(sid)["packet_id"]
    manager.require_ack(sid)


@pytest.mark.parametrize("client", ["external", "claude", "api"])
async def test_records_extracted_from_another_sessions_turn_keep_every_ack(workspace, client):
    store, project, _, manager = workspace
    reader = acknowledged(manager, project, client)
    writer = manager.prepare(project["id"], "api", timeout=0)["session_id"]
    mid_turn_archive(store, manager, writer)
    [written] = await extract_queued(store, [decision()])
    manager.require_ack(reader)
    assert manager.status(reader)["acknowledged"] is True
    # Shown to the reader in its next packet, labelled; a person's confirmation then counts (#256).
    Inspector(store).review(project["id"], written["id"], "confirm", 1)
    with pytest.raises(ValueError, match="changed since the acknowledged packet"):
        manager.require_ack(reader)


async def test_a_person_confirming_or_editing_the_sessions_own_proposal_makes_the_ack_stale(workspace):
    store, project, _, manager = workspace
    sid = acknowledged(manager, project)
    mid_turn_archive(store, manager, sid)
    [proposal] = await extract_queued(store, [decision()])
    manager.require_ack(sid)
    # The person confirms it in the inspector (#177): no other write can store that status.
    Inspector(store).review(project["id"], proposal["id"], "confirm", 1)
    with pytest.raises(ValueError, match="resume_required"):
        manager.require_ack(sid)

    second = acknowledged(manager, project)
    mid_turn_archive(store, manager, second)
    [proposal] = await extract_queued(store, [decision(CONSTRAINT, "constraint", "inventory-lines")])
    manager.require_ack(second)
    edited = store.remember(project["id"], {"id": proposal["id"], "expected_version": 1,
                                            "content": "The file keeps 400 lines or more."},
                            proposal["source_ids"])
    assert edited["status"] == "proposed" and "extraction_source_id" not in edited["metadata"]
    with pytest.raises(ValueError, match="resume_required"):
        manager.require_ack(second)


async def test_a_record_citing_the_sessions_transcript_with_a_forged_mark_makes_the_ack_stale(workspace):
    store, project, _, manager = workspace
    sid = acknowledged(manager, project)
    archive = mid_turn_archive(store, manager, sid)
    await extract_queued(store, [])
    manager.require_ack(sid)
    prompt_source = archive["source_ids"][0]
    forged = store.remember(project["id"], {"kind": "decision", "key": "forged", "status": "proposed",
                                            "content": DECISION + ".",
                                            "metadata": {"extraction_source_id": prompt_source,
                                                         "origin": "automatic_extraction"}}, [prompt_source])
    assert "extraction_source_id" not in forged["metadata"]
    with pytest.raises(ValueError, match="resume_required"):
        manager.require_ack(sid)


async def test_a_code_change_after_the_sessions_own_extraction_still_makes_the_ack_stale(workspace):
    store, project, root, manager = workspace
    sid = acknowledged(manager, project)
    mid_turn_archive(store, manager, sid)
    await extract_queued(store, [decision()])
    manager.require_ack(sid)
    (root / "inventory.txt").write_text("changed\n")
    git(root, "commit", "-qam", "change")
    with pytest.raises(ValueError, match="resume_required"):
        manager.require_ack(sid)


def test_the_extraction_mark_is_only_for_a_new_record_from_its_one_source(workspace):
    store, project, _, _ = workspace
    first = store.snapshot(project["id"], "note://one", "One note.", "note")
    second = store.snapshot(project["id"], "note://two", "Two note.", "note")
    record = {"kind": "fact", "key": "fact", "content": "A fact."}
    with pytest.raises(ValueError, match="extraction mark"):
        store.remember(project["id"], record, [first["id"], second["id"]], extracted_from=first["id"])
    saved = store.remember(project["id"], record, [first["id"]], extracted_from=first["id"])
    assert saved["metadata"]["extraction_source_id"] == first["id"]
    with pytest.raises(ValueError, match="extraction mark"):
        store.remember(project["id"], {"id": saved["id"], "expected_version": 1, "content": "B."}, [first["id"]],
                       extracted_from=first["id"])


def marked_copy(package, target, mark):
    """The package with the extraction mark written back into its record rows, checksums made consistent."""
    shutil.copytree(package.parent, target)
    data = json.loads((target / "manifest.json").read_text())
    for table in ("records", "record_revisions"):
        for descriptor in data["objects"][table]:
            row = json.loads((target / descriptor["path"]).read_text())
            head = row["snapshot"] if table == "record_revisions" else row
            if head["key"] != "imported-order":
                continue
            head["metadata"][EXTRACTION_MARK] = mark
            blob = portable._bytes(row)
            digest = hashlib.sha256(blob).hexdigest()
            (target / "objects" / f"{digest}.json").write_bytes(blob)
            descriptor.update(sha256=digest, path=f"objects/{digest}.json", bytes=len(blob))
    data["snapshot_id"] = portable._manifest_id(data)
    (target / "manifest.json").write_bytes(portable._bytes(data))
    return target / "manifest.json"


@pytest.mark.parametrize("forged", [False, True])
def test_a_record_from_an_imported_package_makes_the_ack_stale_even_with_the_mark(workspace, tmp_path, forged):
    store, project, root, manager = workspace
    sid = acknowledged(manager, project)
    source = mid_turn_archive(store, manager, sid)["source_ids"][0]
    manager.require_ack(sid)
    baseline = MemoryManager(store).snapshot(project["id"])
    # A collaborator imports the package, adds a record citing this session's transcript, and exports it.
    elsewhere = tmp_path / "elsewhere"
    subprocess.run(["git", "clone", "-q", str(root), str(elsewhere)], check=True, capture_output=True)
    other = Store(tmp_path / "other-database")
    MemoryManager(other).import_snapshot(Path(baseline["manifest_path"]), elsewhere)
    other.remember(project["id"], {"kind": "decision", "key": "imported-order", "status": "proposed",
                                   "content": DECISION + "."}, [source], extracted_from=source)
    package = Path(MemoryManager(other).snapshot(project["id"])["manifest_path"])
    rows = [json.loads((package.parent / item["path"]).read_text())
            for item in json.loads(package.read_text())["objects"]["records"]]
    assert rows and not any(EXTRACTION_MARK in row["metadata"] for row in rows)  # never exported
    if forged:
        package = marked_copy(package, tmp_path / "forged", source)
    assert MemoryManager(store).import_snapshot(package, root)["status"] == "imported"
    [saved] = [row for row in store.list_records(project["id"]) if row["key"] == "imported-order"]
    assert EXTRACTION_MARK not in saved["metadata"]  # never imported
    with pytest.raises(ValueError, match="resume_required"):
        manager.require_ack(sid)
