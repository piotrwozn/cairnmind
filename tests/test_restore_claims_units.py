"""A restore without the person's workspace keeps no person's act of the backup (#214, restore_claims)."""
from __future__ import annotations

from pathlib import Path

import pytest

from cairnmind import person_proof, record_kinds, restore_claims
from cairnmind.confirmation import (FALLBACK_WORDS, confirmation_of, confirmation_text, confirmed_by, import_mark,
                                    kept_verdict, local_verdict)
from cairnmind.restore_claims import BackupClaims
from cairnmind.runtime_sockets import socket_directory, workspace_namespace
from cairnmind.terminal_i18n import CATALOG, tr
from test_person_proof import KEY, fake_tmux, settings, workspace_socket  # noqa: F401 (fixtures)

AT = "2026-11-22T10:20:31+00:00"
FILE = "cairnmind-20261122T101500-1a2b3c4d.cairnmind.json.gz"
PERSON = {"by": "person", "route": "inspector_review", "source_id": "s1", "version": 3, "actor_name": "Ada",
          "at": "2026-11-20T09:12:00+00:00"}


def record(status="confirmed", mark=None, version=3, **extra):
    metadata = {"topic": "x", **({"confirmation": mark} if mark is not None else {})}
    return {"id": "r1", "kind": "decision", "key": "k", "content": "text", "status": status, "version": version,
            "source_ids": ["s1"], "metadata": metadata, **extra}


def test_a_persons_mark_becomes_the_backups_claim():
    claims = BackupClaims(FILE, AT)
    row = record(mark=PERSON)
    turned = claims.turn("records", row)
    assert turned["metadata"]["confirmation"] == {
        "by": "import", "origin": "import", "package": "backup", "where": FILE, "imported_at": AT, "version": 3,
        "confirmed_by": [{"name": "Ada", "action": "inspector_review", "branch": None, "commit": None,
                          "at": "2026-11-20T09:12:00+00:00"}]}
    assert turned["metadata"]["topic"] == "x"
    assert {k: v for k, v in turned.items() if k != "metadata"} == {k: v for k, v in row.items() if k != "metadata"}
    for stray in (None, {"by": "lapsed", "confirmed_version": 1}):
        again = claims.turn("records", record(mark=stray))
        assert again["metadata"]["confirmation"]["confirmed_by"] == []
        assert again["metadata"]["confirmation"]["package"] == "backup"
    assert claims.confirmations == 3
    untouched = [record(status="proposed"), record(status="disputed"), record(status="superseded"),
                 record(status="proposed", mark={"by": "lapsed", "confirmed_version": 1}),
                 record(mark={"by": "import", "package": "archive", "where": "a", "confirmed_by": []}),
                 record(mark={"by": "seed", "route": "seed"})]
    for row in untouched:
        assert claims.turn("records", row) == row
    assert claims.confirmations == 3
    assert claims.turn("sources", {"id": "s1"}) == {"id": "s1"}
    assert claims.turn("records", record(mark=PERSON, metadata=None))["metadata"]["confirmation"]["by"] == "import"


def test_every_confirmed_revision_carries_the_claim():
    claims = BackupClaims(FILE, AT)
    row = {"id": "v1", "record_id": "r1", "version": 2, "snapshot": record(mark=PERSON, version=2)}
    turned = claims.turn("record_revisions", row)
    mark = turned["snapshot"]["metadata"]["confirmation"]
    assert (mark["by"], mark["package"], mark["version"]) == ("import", "backup", 2)
    assert turned["version"] == 2 and turned["snapshot"]["status"] == "confirmed"
    for snapshot in (record(status="proposed"), record(mark={"by": "import", "package": "published"}), "text", None):
        other = {**row, "snapshot": snapshot}
        assert claims.turn("record_revisions", other) == other
    assert claims.confirmations == 0


def test_accepted_record_kinds_are_withdrawn_by_a_restore():
    accepted = [{"client": "claude", "kind": "kind-a", "note": "n", "accepted_by": "Ada"},
                {"client": "codex", "kind": "kind-b", "note": "n", "accepted_by": "Ada"}]
    old = [{"client": "claude", "kind": f"old-{index}", "withdrawn_at": "t"} for index in range(19)]
    project = {"id": "p", record_kinds.FIELD: accepted, record_kinds.WITHDRAWN_FIELD: old}
    row, count = record_kinds.withdraw_all(project, AT, "restore", "note")
    assert count == 2 and row[record_kinds.FIELD] == []
    withdrawn = row[record_kinds.WITHDRAWN_FIELD]
    assert len(withdrawn) == record_kinds.MAX_WITHDRAWN and withdrawn[0]["kind"] == "old-1"
    assert [entry["kind"] for entry in withdrawn[-2:]] == ["kind-a", "kind-b"]
    last = withdrawn[-1]
    assert (last["withdrawn_route"], last["withdrawn_by"], last["withdrawn_source_id"], last["withdraw_note"],
            last["withdrawn_at"]) == ("restore", None, None, "note", AT)
    assert last["accepted_by"] == "Ada"
    bare = {"id": "p", record_kinds.FIELD: []}
    assert record_kinds.withdraw_all(bare, AT, "restore", "note") == (bare, 0)
    claims = BackupClaims(FILE, AT)
    assert claims.turn("projects", project)[record_kinds.FIELD] == [] and claims.record_kinds == 2


def test_a_restored_claim_reads_as_the_backups_in_english_and_polish():
    claims = BackupClaims(FILE, AT)
    person = claims.turn("records", record(mark=PERSON))
    before = claims.turn("records", record(mark=None))
    english = confirmation_text(person)
    assert english == (f"confirmed by Ada, in the inspector, 2026-11-20, as the backup says; restored from {FILE} "
                       "on 2026-11-22 without the person's workspace; not verified here")
    assert confirmation_text(before) == (
        f"confirmed before only a person could confirm, as the backup says; restored from {FILE} on 2026-11-22 "
        "without the person's workspace; not verified here")
    polish = confirmation_text(person, lambda text: tr(text, "pl"))
    assert polish == (f"potwierdzone przez Ada, w inspektorze, 2026-11-20, według kopii zapasowej; przywrócone z "
                      f"{FILE} dnia 2026-11-22 bez środowiska osoby; niezweryfikowane tutaj")
    assert "confirmed" not in confirmation_text(before, lambda text: tr(text, "pl"))
    bare = {**person, "metadata": {"confirmation": {"by": "import", "package": "backup", "confirmed_by": [{}]}}}
    english = confirmation_text(bare)
    assert "an unnamed person" in english and "time unknown" in english and "a backup" in english
    assert "an unknown day" in english and "by a person's act" in english
    polish = confirmation_text(bare, lambda text: tr(text, "pl"))
    assert "kopii zapasowej" in polish and "osobę bez podanego imienia" in polish
    assert "a backup" in FALLBACK_WORDS and all(word not in polish for word in ("unnamed", "unknown", "a backup"))
    for turned in (person, before):
        assert confirmed_by(turned) == "import"
        assert not kept_verdict(turned) and not local_verdict(turned)
    archive = record(mark=import_mark("archive", "a.json", [{"name": "Bob"}], AT, 3))
    assert confirmation_text(archive).startswith("confirmed elsewhere by Bob")
    assert confirmation_of(person)["package"] == "backup"


def test_the_restore_asks_this_installations_workspace_socket(settings, monkeypatch):  # noqa: F811
    expected = str(socket_directory("ui", workspace_namespace(settings.data_dir)) / "tmux.sock")
    asked = []
    monkeypatch.setenv("TMUX", "/tmp/other,1,0")

    def checked(given, value):
        asked.append(value)
        return Path(value) if given is settings else None

    monkeypatch.setattr(person_proof, "workspace_socket", checked)
    for answer in ("key", "no_key", "unavailable"):
        calls = []
        monkeypatch.setattr(person_proof, "read_key", lambda s, socket, calls=calls, answer=answer:
                            calls.append(socket) or (answer, "k" * 64 if answer == "key" else None))
        assert person_proof.restore_proof(settings) == answer
        assert calls == [Path(expected)]
    assert set(asked) == {expected}
    monkeypatch.setattr(person_proof, "workspace_socket", lambda s, value: None)
    monkeypatch.setattr(person_proof, "read_key", lambda *a: pytest.fail("no socket, nothing is asked"))
    assert person_proof.restore_proof(settings) == "unavailable"


def test_restore_proof_with_a_real_socket_and_a_fake_tmux(tmp_path, settings, workspace_socket):  # noqa: F811
    binary, calls = fake_tmux(tmp_path, KEY)
    settings.tmux_executable = binary
    assert person_proof.restore_proof(settings) == "key"
    assert calls()[-1]["argv"][:2] == ["-S", str(workspace_socket)]
    (tmp_path / "fake_key").write_text("")
    assert person_proof.restore_proof(settings) == "no_key"
    (tmp_path / "fake_key").write_text(KEY)
    settings.tmux_executable = "/nonexistent/tmux"
    assert person_proof.restore_proof(settings) == "unavailable"
    settings.tmux_executable = binary
    workspace_socket.parent.chmod(0o755)  # a socket the checks refuse fails closed
    asked = len(calls())
    assert person_proof.restore_proof(settings) == "unavailable" and len(calls()) == asked
    workspace_socket.parent.chmod(0o700)
    assert person_proof.restore_proof(settings) == "key"


def test_the_restore_sentences_have_polish():
    claims = BackupClaims(FILE, AT)
    claims.confirmations, claims.record_kinds = 2, 1
    for sentence in (restore_claims.RESTORED_WITHOUT_WORKSPACE, restore_claims.RESTORED_WITH_AN_OLD_WORKSPACE):
        assert sentence in restore_claims.MESSAGES and CATALOG[sentence] == restore_claims.MESSAGES[sentence]
        polish = tr(sentence, "pl", confirmations=2, kinds=1)
        assert polish != tr(sentence, "en", confirmations=2, kinds=1) and "{" not in polish
    # The second way names the setting that may be wrong, in both languages (#214 review).
    assert "tmux_executable" in restore_claims.RESTORED_WITHOUT_WORKSPACE
    assert "tmux_executable" in restore_claims.MESSAGES[restore_claims.RESTORED_WITHOUT_WORKSPACE]
    old = restore_claims.restore_notice("no_key", claims)
    assert "cairnmind workspace stop" in old and "2 confirmed" in old and "1 accepted" in old
    for proof in ("unavailable", "key"):
        assert "cairnmind workspace stop" not in restore_claims.restore_notice(proof, claims)
    assert "tmux 3.4" in restore_claims.restore_notice("unavailable", claims)
    # With nothing to keep, no redo is advised (it could change nothing), in both languages.
    empty = BackupClaims(FILE, AT)
    for proof in ("unavailable", "no_key"):
        template, values = restore_claims.notice_item(proof, empty)
        assert template == restore_claims.RESTORED_NOTHING_TO_KEEP and "run the same restore again" not in template
        assert CATALOG[template] == restore_claims.MESSAGES[template] and "ponownie" not in tr(template, "pl", **values)
    # Not "nothing to keep": a person's confirmation only in a revision's snapshot (a redo with the workspace keeps it).
    revised = BackupClaims(FILE, AT)
    revised.turn("record_revisions", {"snapshot": record(mark=PERSON)})
    assert revised.revisions == 1 and revised.confirmations == revised.record_kinds == 0
    assert restore_claims.notice_item("unavailable", revised)[0] == restore_claims.RESTORED_WITHOUT_WORKSPACE
    # Confirmed records that already carry an import mark are no person's act: nothing to keep, and the sentence says so.
    imported = BackupClaims(FILE, AT)
    imported.turn("records", record(mark=import_mark("memory", "pkg", [], AT, 3)))
    assert restore_claims.notice_item("unavailable", imported)[0] == restore_claims.RESTORED_NOTHING_TO_KEEP
