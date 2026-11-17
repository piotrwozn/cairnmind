"""No server transaction stays open while a session's worktree is hashed (#255).

ArcadeDB ends a remote transaction that sees no request for about five seconds: the next request in it
answers 404 "Remote transaction ... not found or expired". Hashing a checkout on /mnt/c under WSL takes
that long, more while the queue indexes the same files, and it ran inside the writer transaction of
ack_resume, of an exact resume read and of every session-scoped write. Real ArcadeDB, Git and HTTP service.
"""
from contextlib import contextmanager
import threading
import time

import pytest

from cairnmind import verification
from cairnmind.arcade import ArcadeClient

from test_external_session_writes import fixture as fixture  # noqa: F401 - a fixture
from test_external_session_writes import post, readback

# Longer than ArcadeDB 26.11.1 keeps an idle remote transaction: its default timeout is five seconds and
# expired sessions are swept every five, so an idle transaction ends after five to ten seconds.
LONGER_THAN_EXPIRY = 11


@pytest.fixture
def transactions(monkeypatch):
    """Count the server transactions open in each thread, and what was open whenever the worktree was hashed."""
    depth = threading.local()
    seen = []
    begin = ArcadeClient.transaction
    capture = verification.capture_code_state

    @contextmanager
    def counted(self, *, commit=True):
        with begin(self, commit=commit) as tx:
            before = getattr(depth, "value", 0)
            depth.value = before + 1
            try:
                yield tx
            finally:
                depth.value = before

    def observed(*args, **kwargs):
        seen.append(getattr(depth, "value", 0))
        return capture(*args, **kwargs)
    monkeypatch.setattr(ArcadeClient, "transaction", counted)
    monkeypatch.setattr(verification, "capture_code_state", observed)
    return seen


def prepared(value):
    result = value.manager.prepare(value.project["id"], "external", timeout=0)
    assert result["status"] == "ready", result
    return result


def test_ack_succeeds_when_hashing_the_worktree_outlasts_a_server_transaction(fixture, monkeypatch):  # noqa: F811
    result = prepared(fixture)
    packet = result["packet"]
    capture = verification.capture_code_state

    def slow(*args, **kwargs):
        # What a large checkout on a slow file system costs; before the fix the ACK's transaction expired here.
        time.sleep(LONGER_THAN_EXPIRY)
        return capture(*args, **kwargs)
    monkeypatch.setattr(verification, "capture_code_state", slow)
    started = time.monotonic()
    acknowledgment = fixture.manager.ack(result["session_id"], packet["id"], packet["hash"], readback(packet))
    assert acknowledgment["packet_id"] == packet["id"] and acknowledgment["status"] == "acknowledged"
    assert time.monotonic() - started >= LONGER_THAN_EXPIRY
    monkeypatch.setattr(verification, "capture_code_state", capture)
    fixture.manager.require_ack(result["session_id"], packet["id"])


def test_ack_still_refuses_a_wrong_hash_a_wrong_readback_and_a_stale_packet(fixture, transactions):  # noqa: F811
    result = prepared(fixture)
    packet, session_id = result["packet"], result["session_id"]
    transactions.clear()
    with pytest.raises(ValueError, match="ACK does not match a ready current packet"):
        fixture.manager.ack(session_id, packet["id"], "0" * 64, readback(packet))
    with pytest.raises(ValueError, match="ACK does not match a ready current packet"):
        fixture.manager.ack(session_id, "another-packet", packet["hash"], readback(packet))
    with pytest.raises(ValueError, match="readback must match"):
        fixture.manager.ack(session_id, packet["id"], packet["hash"], {"plan": {"id": "x", "version": 1}, "task": None})
    # Refused before the worktree is hashed.
    assert transactions == []
    (fixture.root / "app.py").write_text("VALUE = 2\n")
    with pytest.raises(ValueError, match="state or code changed before ACK"):
        fixture.manager.ack(session_id, packet["id"], packet["hash"], readback(packet))
    assert fixture.store.query("SELECT FROM resume_acks WHERE session_id=:id", {"id": session_id}) == []
    assert transactions and set(transactions) == {0}


def test_resume_reads_and_ack_hash_the_worktree_outside_any_transaction(fixture, transactions):  # noqa: F811
    result = prepared(fixture)
    packet, session_id = result["packet"], result["session_id"]
    fixture.manager.exact_context(session_id, packet["id"])
    fixture.manager.resume_context(session_id)
    fixture.manager.ack(session_id, packet["id"], packet["hash"], readback(packet))
    fixture.manager.require_ack(session_id, packet["id"])
    fixture.manager.status(session_id)
    assert len(transactions) >= 6 and set(transactions) == {0}, transactions


def test_service_endpoints_hash_the_worktree_outside_any_transaction(fixture, transactions):  # noqa: F811
    project = fixture.project["id"]
    started = post(fixture, "/sessions/prepare", {"project": project, "client": "external", "reason": "startup"})
    session_id = started["session_id"]
    exact = post(fixture, "/sessions/context", {"session_id": session_id, "packet_id": started["packet"]["id"]})
    packet = exact["packet"]
    post(fixture, "/sessions/ack", {"session_id": session_id, "packet_id": packet["id"],
                                    "packet_hash": packet["hash"], "readback": readback(packet)})
    bound = {"session_id": session_id, "packet_id": packet["id"]}

    def acknowledged_again():
        # A write changes the state the ACK was given for; the next one needs a new packet and ACK.
        fresh = post(fixture, "/sessions/context", {"session_id": session_id})["packet"]
        post(fixture, "/sessions/ack", {"session_id": session_id, "packet_id": fresh["id"],
                                        "packet_hash": fresh["hash"], "readback": readback(fresh)})
        return {"session_id": session_id, "packet_id": fresh["id"]}

    post(fixture, "/remember", {"project": project, "sources": [fixture.note["id"]], **bound,
                                "record": {"kind": "decision", "content": "Hash the worktree first"}})
    bound = acknowledged_again()
    post(fixture, "/checkpoint", {"project": project, **bound, "state": {"summary": "Hashed outside"}})
    bound = acknowledged_again()
    plan = post(fixture, "/plans/save", {"project": project, **bound, "idempotency_key": "expiry",
                                         "plan": {"title": "Expiry plan", "goal": "Keep transactions short"},
                                         "steps": [{"key": "one", "content": "First step"}]})
    bound = acknowledged_again()
    post(fixture, "/plans/activate", {"project": project, **bound, "plan_id": plan["plan"]["id"]})
    bound = acknowledged_again()
    post(fixture, "/memory/snapshot", {"project": project, **bound})
    assert len(transactions) >= 15 and set(transactions) == {0}, transactions


def test_native_cli_write_hashes_the_worktree_before_its_transaction(fixture, transactions, monkeypatch):  # noqa: F811
    from cairnmind.cli import actor_write
    result = prepared(fixture)
    packet = result["packet"]
    fixture.manager.ack(result["session_id"], packet["id"], packet["hash"], readback(packet))
    transactions.clear()
    monkeypatch.setenv("CAIRNMIND_SESSION_ID", result["session_id"])
    with actor_write(fixture.store, fixture.settings, fixture.project["id"]):
        pass
    assert transactions == [0]
