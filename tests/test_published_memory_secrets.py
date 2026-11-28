"""The secrets gate of published memory (#171), without a store: what is found, what is not, and releases."""
from __future__ import annotations

import secrets
from pathlib import Path

import pytest
from test_published_memory_format import (
    AT,
    OTHER,
    PROJECT,
    RECORD,
    REVIEW,
    constraint,
    decision,
    single,
    tree,
)
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.memory.memory_published_check import check_files
from cairnmind.memory.memory_published_format import path_of, render
from cairnmind.memory.memory_published_secrets import (
    REGISTERED,
    Rules,
    SecretRulesError,
    described,
    load_rules,
    random_looking,
    release_problem,
    scan_text,
    value_sha256,
)
from cairnmind.storage.sql import json_text

ROOT = Path(__file__).resolve().parents[1]
RELEASE = "6f7a8b9c-0d1e-4f2a-8b3c-4d5e6f7a8b9c"
# Assembled at run time, so that this file holds no string that a scanner of the repository would flag.
KEY_BLOCK = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0Z3VS5JJcds3xfn\n-----END RSA " + "PRIVATE KEY-----"
SHAPES = {
    "aws_access_key": "AKIA" + "IOSFODNN7EXAMPLE",
    "github_token": "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
    "anthropic_key": "sk-ant-" + "api03-Zx9Yw8Vu7Ts6Rq5Po4Nm3",
    "openai_key": "sk-proj-" + "Ab12Cd34Ef56Gh78Ij90Kl",
    "slack_token": "xoxb-" + "123456789012-abcdefABCDEF",
    "google_api_key": "AIza" + "SyA1234567890abcdefghijklmnopqrstuv",
    "stripe_key": "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc",
    "jwt": "eyJ" + "hbGciOiJIUzI1NiJ9.eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
    "bearer_token": "Authorization: Bearer " + "8f3kD92LmQ0zXa7RwT4u",
    "connection_password": "postgres://app:" + "Hunter2Strong" + "@db.internal:5432/app",
    "private_key": KEY_BLOCK,
}


def kinds(text, rules=None):
    return [kind for kind, _ in scan_text(text, rules or Rules())]


@pytest.mark.parametrize("kind", sorted(SHAPES))
def test_every_well_known_shape_is_found_inside_ordinary_text(kind):
    text = f"The deploy note says:\n{SHAPES[kind]}\nthen restart."
    assert kinds(text) == [kind]
    hit = {"path": f"decision/{RECORD}.md", "field": "text", "kind": kind, "value_sha256": value_sha256(SHAPES[kind])}
    printed = described(hit)
    assert printed == f"decision/{RECORD}.md: {kind.replace('_', ' ')} in text"
    assert not any(piece in printed for piece in SHAPES[kind].split() if len(piece) > 3)


def test_registered_secrets_are_found_whatever_their_shape_and_short_values_are_ignored():
    rules = Rules(registered=["plain words password", "short"])
    assert kinds("we set it to plain words password today", rules) == [REGISTERED]
    assert kinds("a short note", rules) == []
    assert kinds("plain words password", rules.without_registered()) == []


def test_long_random_strings_are_found_and_what_memory_normally_holds_is_not():
    found = sum(random_looking(secrets.token_urlsafe(30)) for _ in range(500))
    assert found >= 495  # 40 random characters; rare unlucky draws aside.
    ordinary = [
        "0898fda2c41e7b9d3f5a6c8e0b2d4f6a8c1e3b5d", "5d0b7e2a91c4f6083e7a1d5c9b2f4e6081a3c5e7d9f1b3a5c7e9d1f3b5a7c9e1",
        "0b6c2f9e-5d1a-4c8e-9a43-2f1d7c9e8a10", "plan:b1c2d3e4-f5a6-4b7c-8d9e-0f1a2b3c4d5e:format",
        "src/cairnmind/memory_published_format.py", "tests/test_published_memory_adversarial.py",
        "objects/d7a74213d2ede295abbfe536064657bb35f4d1ba09bb13f3fe7de567dd01b6e6",
        "DEVBRAIN_PILOT_REPORT_DIR/doctor-before", "test_at_160x45_the_column_is_beside_the_focused_view_in_a_terminal",
        "E2E070_PRIVATE_PROFILE_KEY_MUST_NOT_ENTER_HISTORY", "PublishedMemoryAdversarialTestCase",
        "2026-11-12T09:14:03.120412+00:00", "https://github.com/piotrwozn/cairnmind/releases/tag/v0.10.0",
        "postgres://app@db.internal/app", "postgres://app:${DB_PASSWORD}@db/app", "Bearer authentication is used",
    ]
    for text in ordinary:
        assert kinds(text) == [], text


def test_this_repositorys_own_documentation_and_example_memory_raise_no_alarm():
    paths = [ROOT / "README.md", ROOT / "CHANGELOG.md", ROOT / "CONTRIBUTING.md", *sorted((ROOT / "docs").glob("*.md")),
             *sorted((ROOT / "examples").rglob("*.json")), *sorted((ROOT / "examples").rglob("*.md"))]
    checked = 0
    for path in paths:
        if path.is_file():
            hits = scan_text(path.read_text(encoding="utf-8"), Rules())
            assert hits == [], (path, [kind for kind, _ in hits])
            checked += 1
    assert checked > 10
    # The format's own example files are clean too: digests, UUIDs, commits, paths and audit passages.
    assert check_files(tree(decision(), constraint()))[1] == []


def test_personal_data_is_off_by_default_and_a_project_turns_it_on_with_its_own_patterns():
    text = "Ask ada.nowak@example.org or call +48 601 234 567 about EMP-123456."
    assert kinds(text) == []
    rules = load_rules('[personal]\ndetect = ["email", "phone"]\n'
                       'patterns = [{name = "employee-id", regex = "EMP-[0-9]{6}"}]\n')
    assert sorted(kinds(text, rules)) == ["email", "personal:employee-id", "phone"]
    assert kinds("Released on 2026-11-14, 1,250 records.", rules) == []
    for broken in ("[personal]\ndetect = [\"ssn\"]", "[other]\n", '[personal]\npatterns = [{name = "x", regex = "("}]',
                   "not toml ="):
        with pytest.raises(SecretRulesError):
            load_rules(broken)


def with_text(text, record_id=RECORD):
    return single(record_id, "deploy", text, REVIEW)


def release_source(record_id, hits, reason="A documented example key, not a real one."):
    passage = json_text({"action": "release", "actor": "local_user", "actor_name": "Ada Nowak", "at": AT,
                         "branch": "main", "commit": "3" * 40, "hits": sorted(hits, key=json_text), "reason": reason,
                         "record_id": record_id})
    return {"ref": {"at": AT, "kind": "release"}, "passage": passage}


def test_a_hand_added_record_with_a_key_is_refused_by_the_check_naming_it_and_printing_nothing_of_it():
    model = with_text(f"Use this key:\n{KEY_BLOCK}")
    result, errors = check_files(tree(model, constraint()))
    assert result is None and errors == [(path_of(model), "private key found in text")]
    assert "MIIE" not in repr(errors)
    quoted = decision()
    quoted["revisions"][0]["quote"]["text"] = "token " + SHAPES["github_token"]
    assert ("github token found in quote") in [reason for _, reason in check_files(tree(quoted))[1]]


def test_a_release_in_the_file_lets_exactly_the_values_it_names_through():
    model = with_text(f"The AWS documentation example key is {SHAPES['aws_access_key']}.")
    hit = {"field": "text", "kind": "aws_access_key", "value_sha256": value_sha256(SHAPES["aws_access_key"])}
    model["sources"][RELEASE] = release_source(RECORD, [hit])
    data = render(model)
    assert "A documented example key, not a real one." in data.decode()
    assert check_files(tree(model))[1] == []
    # Another value of the same kind is not released by it.
    other = with_text(f"The key is {'AKIA' + 'ABCDEFGHIJKLMNOP'}.")
    other["sources"][RELEASE] = release_source(RECORD, [hit])
    assert check_files(tree(other))[1] == [(path_of(other), "aws access key found in text")]


def test_a_release_must_be_for_its_own_record_never_for_a_registered_secret_and_never_in_a_plan_file():
    hit = {"field": "text", "kind": "aws_access_key", "value_sha256": "a" * 64}
    assert release_problem(release_source(RECORD, [hit])["passage"], RECORD) is None
    assert release_problem(release_source(OTHER, [hit])["passage"], RECORD) == "releases another record"
    registered = {**hit, "kind": REGISTERED}
    assert "registered secret" in release_problem(release_source(RECORD, [registered])["passage"], RECORD)
    assert "reason" in release_problem(release_source(RECORD, [hit], reason=" ")["passage"], RECORD)
    model = with_text("Plain text.")
    model["sources"][RELEASE] = release_source(OTHER, [hit])
    assert "releases another record" in check_files(tree(model))[1][0][1]


def test_the_example_workflow_checks_the_published_directory_of_a_pull_request():
    text = (ROOT / "docs" / "examples" / "published-memory-check.yml").read_text(encoding="utf-8")
    assert "\non:\n  pull_request:\n" in text and "permissions:\n  contents: read\n" in text
    assert 'memory check-published "$MEMORY_DIR"' in text and "MEMORY_DIR: .memory/cairnmind-published" in text
    assert "secrets." not in text  # It needs no secret of the repository.


def test_check_published_reads_the_projects_patterns_and_exits_1_without_printing_the_value(tmp_path):
    root = tmp_path / "checkout"
    folder = root / ".memory" / "cairnmind-published"
    model = with_text("Escalate to ada.nowak@example.org when the import fails.")
    for path, data in tree(model).items():
        (folder / path).parent.mkdir(parents=True, exist_ok=True)
        (folder / path).write_bytes(data)
    runner = CliRunner()
    unused = str(tmp_path / "unused")
    assert runner.invoke(cli.app, ["--data-dir", unused, "memory", "check-published", str(folder)]).exit_code == 0
    (root / ".cairnmind").mkdir()
    (root / ".cairnmind" / "publication.toml").write_text('[personal]\ndetect = ["email"]\n')
    refused = runner.invoke(cli.app, ["--data-dir", unused, "memory", "check-published", str(folder)])
    assert refused.exit_code == 1 and refused.output == f"{path_of(model)}: email found in text\n"
    assert "ada.nowak" not in refused.output and not (tmp_path / "unused").exists()
    elsewhere = tmp_path / "rules.toml"
    elsewhere.write_text("[personal]\n")
    passed = runner.invoke(cli.app, ["--data-dir", unused, "memory", "check-published", str(folder),
                                     "--config", str(elsewhere)])
    assert passed.exit_code == 0 and f"of {PROJECT}" in passed.output


def test_a_registered_secret_split_by_an_invisible_character_is_still_found():
    key = secrets.token_urlsafe(32)
    rules = Rules(registered=[key])
    for invisible in ("​", "‍", "⁠", "­", "﻿"):
        assert kinds(f"value {key[:20]}{invisible}{key[20:]} end", rules) == [REGISTERED]
    assert kinds("café ​naïve", rules) == []


def test_check_published_never_prints_a_secret_held_in_a_file_name_and_lists_every_release(tmp_path):
    folder = tmp_path / "published"
    model = with_text(f"The AWS documentation example key is {SHAPES['aws_access_key']}.")
    hit = {"field": "text", "kind": "aws_access_key", "value_sha256": value_sha256(SHAPES["aws_access_key"])}
    model["sources"][RELEASE] = release_source(RECORD, [hit])
    for path, data in tree(model).items():
        (folder / path).parent.mkdir(parents=True, exist_ok=True)
        (folder / path).write_bytes(data)
    runner, unused = CliRunner(), str(tmp_path / "unused")
    passed = runner.invoke(cli.app, ["--data-dir", unused, "memory", "check-published", str(folder)])
    assert passed.exit_code == 0
    assert f"{path_of(model)}: release {RELEASE} by Ada Nowak lets through aws access key in text\n" in passed.output
    assert SHAPES["aws_access_key"] not in passed.output
    token = SHAPES["github_token"]
    (folder / "decision" / f"{token}.md").write_text("x\n")
    (folder / token).write_text("x\n")
    (folder / "notes.txt").write_text("x\n")
    refused = runner.invoke(cli.app, ["--data-dir", unused, "memory", "check-published", str(folder)])
    assert refused.exit_code == 1 and token not in refused.output and token[4:16] not in refused.output
    assert "(an entry in decision whose name holds a secret): unknown entry" in refused.output
    assert "(an entry whose name holds a secret): unknown entry" in refused.output
    assert "notes.txt: unknown entry" in refused.output
