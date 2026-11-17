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


def test_a_write_refused_for_its_session_hashes_nothing(fixture, transactions, monkeypatch):  # noqa: F811
    from cairnmind.cli import actor_write
    result = prepared(fixture)
    packet, session_id = result["packet"], result["session_id"]
    write = {"project": fixture.project["id"], "sources": [fixture.note["id"]], "session_id": session_id,
             "record": {"kind": "fact", "content": "Refused"}}
    transactions.clear()
    response = fixture.client.post("/remember", json={**write, "packet_id": packet["id"]})  # no ACK yet
    assert response.status_code == 409 and "acknowledge the current ready resume packet" in response.text
    monkeypatch.setenv("CAIRNMIND_SESSION_ID", session_id)
    with pytest.raises(ValueError, match="acknowledge the current ready resume packet"):
        with actor_write(fixture.store, fixture.settings, fixture.project["id"]):
            pass
    assert transactions == []
    fixture.manager.ack(session_id, packet["id"], packet["hash"], readback(packet))
    transactions.clear()
    response = fixture.client.post("/remember", json={**write, "packet_id": "another-packet"})
    assert response.status_code == 409 and "acknowledge the current ready resume packet" in response.text
    response = fixture.client.post("/memory/snapshot", json={"project": fixture.project["id"],
                                                             "session_id": session_id, "packet_id": "another-packet"})
    assert response.status_code == 409 and "resume_required" in response.text
    assert transactions == []
    assert not fixture.store.query("SELECT FROM records WHERE content='Refused'")


def test_a_session_acknowledged_after_its_write_began_is_refused_without_hashing_in_the_transaction(
        fixture, transactions):  # noqa: F811
    result = prepared(fixture)
    packet, session_id = result["packet"], result["session_id"]
    transactions.clear()
    code = fixture.manager.code_scope(session_id)  # not acknowledged yet: nothing is hashed
    assert transactions == []
    fixture.manager.ack(session_id, packet["id"], packet["hash"], readback(packet))
    transactions.clear()
    with pytest.raises(ValueError, match="retry it"):
        with fixture.store.transaction():
            fixture.manager.require_ack(session_id, packet["id"], code)
    assert transactions == []
    fixture.manager.require_ack(session_id, packet["id"])  # the retry


def test_a_root_registered_after_the_code_state_was_taken_refuses_the_check(fixture):  # noqa: F811
    bound = fixture.session("external")
    code = fixture.manager.code_scope(bound["session_id"])
    # Registered roots decide which directories the manifest leaves out, and that read is outside the transaction.
    nested = fixture.root / "services" / "inner"
    nested.mkdir(parents=True)
    fixture.store.add_project("inner", nested)
    with pytest.raises(ValueError, match="registered project roots changed"):
        with fixture.store.transaction():
            fixture.manager.require_ack(bound["session_id"], bound["packet_id"], code)


def test_a_memory_write_rechecks_the_code_as_it_was_when_its_transaction_began(fixture, transactions,
                                                                               monkeypatch):  # noqa: F811
    from cairnmind.memory import MemoryManager
    bound = fixture.session("external")
    lock = MemoryManager._publisher_lock

    @contextmanager
    def edited_meanwhile(self, *args, **kwargs):
        # After the first check, before core's transaction: the code changes while core does its own work.
        with lock(self, *args, **kwargs):
            (fixture.root / "app.py").write_text("VALUE = 3\n")
            yield
    monkeypatch.setattr(MemoryManager, "_publisher_lock", edited_meanwhile)
    transactions.clear()
    response = fixture.client.post("/memory/snapshot", json={"project": fixture.project["id"], **bound})
    assert response.status_code == 409 and "changed since the acknowledged packet" in response.text
    assert len(transactions) == 2 and set(transactions) == {0}, transactions
    assert fixture.store.query("SELECT FROM memory_snapshots") == []


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
