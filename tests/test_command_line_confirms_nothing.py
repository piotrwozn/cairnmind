"""The command line confirms nothing (#214), also where it looks like a person's own terminal. No engine."""
from __future__ import annotations

import os
import subprocess
import sys
from uuid import uuid4

import pytest
from conftest import plain_words
from typer.testing import CliRunner

from cairnmind import cli, confirmation, impact_verdicts, memory_published_release, record_kinds
from cairnmind.cli import app

ENVIRONMENT_WITHOUT_A_SESSION = ("CAIRNMIND_SESSION_ID", "DEVBRAIN_SESSION_ID", "CAIRNMIND_PACKET_ID",
                                 "DEVBRAIN_PACKET_ID", *confirmation.CLIENT_MARKERS)


def clean_environment(**extra):
    environment = {name: value for name, value in os.environ.items() if name not in ENVIRONMENT_WITHOUT_A_SESSION}
    return {**environment, **extra}


def words(text):
    return plain_words(text)  # also drops the colour codes a forced-colour run puts inside option names


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


@pytest.mark.parametrize("in_a_session", [False, True])
@pytest.mark.parametrize("arguments, sentence", [
    (["impact-verdict", "demo", str(uuid4()), "--fingerprint", "0" * 64, "--verdict", "still_valid", "--since", "HEAD"],
     impact_verdicts.CLI_REFUSED),
    (["memory", "release", "demo", str(uuid4()), "--reason", "An example key."],
     memory_published_release.CLI_REFUSED)])
def test_impact_verdict_and_release_commands_are_refused_in_a_person_terminal(
        tmp_path, monkeypatch, person_terminal, arguments, sentence, in_a_session):
    for name in ENVIRONMENT_WITHOUT_A_SESSION:
        monkeypatch.delenv(name, raising=False)
    if in_a_session:
        monkeypatch.setenv("CAIRNMIND_SESSION_ID", "logical-session")
    def no_store(*_args, **_kwargs):
        raise AssertionError("the store was opened")

    monkeypatch.setattr(cli, "store_for", no_store)
    monkeypatch.setattr(cli, "store_for_read", no_store)
    result = CliRunner().invoke(app, ["--data-dir", str(tmp_path / "memory"), *arguments])
    assert result.exit_code == 2 and sentence in words(result.output), result.output
    assert "inspector of the workspace" in words(result.output)


@pytest.mark.parametrize("in_a_session", [False, True])
@pytest.mark.parametrize("arguments", [["forget", "demo", "--delete-project"], ["forget", "demo"],
                                       ["forget", "0a1b2c3d", "--delete-project"]])
def test_forget_is_refused_and_opens_no_store(tmp_path, monkeypatch, arguments, in_a_session):
    from cairnmind import project_forget
    for name in ENVIRONMENT_WITHOUT_A_SESSION:
        monkeypatch.delenv(name, raising=False)
    if in_a_session:
        monkeypatch.setenv("CAIRNMIND_SESSION_ID", "logical-session")

    def no_store(*_args, **_kwargs):
        raise AssertionError("the store was opened")

    monkeypatch.setattr(cli, "store_for", no_store)
    monkeypatch.setattr(cli, "store_for_read", no_store)
    result = CliRunner().invoke(app, ["--data-dir", str(tmp_path / "memory"), *arguments])
    assert result.exit_code == 2 and project_forget.CLI_REFUSED in words(result.output), result.output
    assert "More, then Delete project memory" in words(result.output) and not (tmp_path / "memory").exists()


def test_the_command_line_refusals_name_team_memory():
    from cairnmind import person_proof
    for sentence in (confirmation.replaces_refusal(["fact/x.md"]), confirmation.REPLACES_NOTE,
                     person_proof.IMPORT_REPLACE):
        assert "Team memory" in sentence and "memory view of the workspace" in sentence, sentence
        assert "cannot replace" not in sentence and "yet" not in sentence.split(), sentence
    environment = {"COLUMNS": "200"}
    for command, named in ((["memory", "import-published"], "Team memory"), (["memory", "import-commit"], "Team memory"),
                           (["memory", "import"], "Import in the memory view")):
        output = words(CliRunner().invoke(app, [*command, "--help"], env=environment).output)
        assert named in output and "cannot" not in output.split("--replace-confirmed")[1][:300], (command, output)
    assert "Team memory" in words(CliRunner().invoke(app, ["memory", "import-published", "--help"],
                                                     env=environment).output).split("--take-published")[1][:300]
