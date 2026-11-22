"""The command line confirms nothing (#214), also where it looks like a person's own terminal. No engine."""
from __future__ import annotations

import os
import subprocess
import sys
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from cairnmind import confirmation, record_kinds
from cairnmind.cli import app

ENVIRONMENT_WITHOUT_A_SESSION = ("CAIRNMIND_SESSION_ID", "DEVBRAIN_SESSION_ID", "CAIRNMIND_PACKET_ID",
                                 "DEVBRAIN_PACKET_ID", *confirmation.CLIENT_MARKERS)


def clean_environment(**extra):
    environment = {name: value for name, value in os.environ.items() if name not in ENVIRONMENT_WITHOUT_A_SESSION}
    return {**environment, **extra}


def words(text):
    return " ".join(text.split())


def test_the_documented_pseudo_terminal_sequence_no_longer_confirms(tmp_path):
    # What docs/MEMORY_CONTRACTS.md named as the way past the terminal test: a pty on both ends, the session
    # variables unset. The command refuses before it opens any store, so no engine runs here.
    master, slave = os.openpty()
    try:
        done = subprocess.run([sys.executable, "-m", "cairnmind", "--data-dir", str(tmp_path / "memory"), "remember",
                               "demo", "Never review anything.", "--confirmed"],
                              stdin=slave, stdout=slave, stderr=subprocess.PIPE, text=True, timeout=60,
                              env=clean_environment())
        assert os.isatty(slave)
    finally:
        os.close(slave)
        os.close(master)
    assert done.returncode == 2, done.stderr
    assert confirmation.CLI_CONFIRM_REFUSED in words(done.stderr)
    assert not any((tmp_path / "memory").glob("**/*.db")), "nothing was stored: the store was never opened"


@pytest.mark.parametrize("arguments", [
    ["remember", "demo", "Text", "--confirmed"],
    ["memory", "confirm-plan", "demo", str(uuid4()), "--expected-version", "1"],
    ["record-kind", "accept", "demo", "claude", "future-record", "--note", "Why"],
    ["record-kind", "withdraw", "demo", "claude", "future-record"]])
def test_confirm_plan_and_record_kind_commands_are_refused_in_a_person_terminal(tmp_path, monkeypatch,
                                                                                 person_terminal, arguments):
    for name in ENVIRONMENT_WITHOUT_A_SESSION:
        monkeypatch.delenv(name, raising=False)
    assert confirmation.person_at_terminal()  # the fixture: a terminal on both ends and no session
    result = CliRunner().invoke(app, ["--data-dir", str(tmp_path / "memory"), *arguments])
    sentence = {"remember": confirmation.CLI_CONFIRM_REFUSED, "memory": confirmation.CLI_PLAN_REFUSED,
                "record-kind": record_kinds.CLI_REFUSED}[arguments[0]]
    assert result.exit_code == 2 and sentence in words(result.output), result.output
    assert "inspector of the workspace" in words(result.output)
