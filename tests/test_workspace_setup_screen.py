"""More, then Settings, then Storage and models: the text of the screen and its one read (#74).

No engine, tmux, Docker or model: the service's answer is the topology of settings in a temporary directory, as it
crosses HTTP. Every test runs the same on macOS.
"""
from __future__ import annotations

import ast
import json
import string
from pathlib import Path

import pytest
import typer.main
from textual.widgets import Button, Static

from cairnmind import cli, setup_profiles, terminal_setup, topology as topology_module
from cairnmind.config import Settings
from cairnmind.terminal_i18n import CATALOG, tr
from cairnmind.terminal_setup import (
    CHIP_PROBLEM, CHIPS, HOW, KEEP_OPEN, LOADED, LOCAL_BACK, MESSAGES, NO_ANSWER, OPTIONAL_KEY, PLACEHOLDERS_OF, UNREADABLE,
    commands, describe, shown)
from cairnmind.terminal_safe import safe_text
from cairnmind.topology import lines, topology
from conftest import plain_words
from cairnmind.terminal_client import BrokerError
from cairnmind.terminal_panels import SidebarApp
from cairnmind.terminal_ui import ChoiceModal, MessageModal
from test_terminal_panels import settle
from test_terminal_ux080 import UIBroker, screen_text, setup_ui
from test_workspace_stage_scope import wait_until
from test_topology import REMOTE, WRITER, clean_environment, make, remote_store  # noqa: F401 - clean_environment is an autouse fixture


def sub(tmp_path, name):
    (tmp_path / name).mkdir()
    return tmp_path / name


def served(settings, writer=WRITER):
    """The service's /status answer as it crosses HTTP."""
    return json.loads(json.dumps({"topology": topology(settings, writer=writer), "writer": writer}))


def case_managed(tmp_path, monkeypatch):
    return make(tmp_path), WRITER


def case_docker(tmp_path, monkeypatch):
    return make(tmp_path, engine_mode="docker"), WRITER


def case_invalid_engine(tmp_path, monkeypatch):
    return make(tmp_path, engine_mode="dokker"), WRITER


def case_connect_remote(tmp_path, monkeypatch):
    return remote_store(tmp_path), WRITER


def case_connect_here(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRNMIND_ARCADE_PASSWORD", "a-database-password")
    return make(tmp_path, arcade_connect_url="http://127.0.0.1:2999"), WRITER


def case_remote_models(tmp_path, monkeypatch):
    return make(tmp_path, memory_chat=REMOTE, memory_embedding=REMOTE), WRITER


def case_split(tmp_path, monkeypatch):
    return make(tmp_path, memory_chat=REMOTE), WRITER


def case_disabled(tmp_path, monkeypatch):
    return make(tmp_path, knowledge_agent="disabled"), WRITER


def case_storage_problem(tmp_path, monkeypatch):
    return make(tmp_path, arcade_url="https://db.example", arcade_user="root"), WRITER


def case_invalid_profile(tmp_path, monkeypatch):
    return make(tmp_path, memory_chat={"provider": "x"}), WRITER


def case_writer_contradicted(tmp_path, monkeypatch):
    return make(tmp_path), {"role": "writer", "problem": "the journal head names another installation"}


CASES = [case_managed, case_docker, case_invalid_engine, case_connect_remote, case_connect_here, case_remote_models,
         case_split, case_disabled, case_storage_problem, case_invalid_profile, case_writer_contradicted]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.__name__[5:])
def test_the_screen_shows_the_three_lines_and_each_problem(case, tmp_path, monkeypatch):
    settings, writer = case(tmp_path, monkeypatch)
    status = served(settings, writer)
    place = status["topology"]
    text = plain_words(describe(status, "en"))
    for line in lines(place, "en"):
        assert plain_words(line) in text
    problems = [place["storage"].get("problem"), place["storage"].get("engine_problem"),
                place["storage"].get("writer", {}).get("problem"), place["models"].get("problem")]
    for problem in filter(None, problems):
        assert plain_words(safe_text(problem, limit=400)) in text
    assert any(problems) == (case in (case_invalid_engine, case_storage_problem, case_invalid_profile,
                                      case_writer_contradicted))


def test_the_screen_names_what_the_marks_abbreviate(tmp_path, monkeypatch):
    def words(settings, writer=WRITER):
        return describe(served(settings, writer), "en")
    assert tr(CHIPS, "en", chips="Storage:local | Models:local") in words(make(tmp_path))
    assert CHIP_PROBLEM.split("{")[0] not in words(make(tmp_path))
    remote = remote_store(sub(tmp_path, "r"), memory_chat=REMOTE, memory_embedding=REMOTE)
    assert "Storage:db.example | Models:remote" in words(remote)
    assert tr(CHIP_PROBLEM, "en", marks="Storage") in words(make(sub(tmp_path, "e"), engine_mode="dokker"))
    assert tr(CHIP_PROBLEM, "en", marks="Models") in words(make(sub(tmp_path, "m"), memory_chat={"provider": "x"}))


def test_the_status_row_mark_ignores_the_writer_problem(tmp_path):
    # the real row is computed without the writer role, so the screen must not promise a problem colour there
    contradicted = {"role": "writer", "problem": "the journal head names another installation"}
    text = describe(served(make(tmp_path), contradicted), "en")
    assert CHIP_PROBLEM.split("{")[0] not in text and contradicted["problem"] in plain_words(text)


def test_the_screen_says_it_shows_what_the_service_loaded(tmp_path, monkeypatch):
    status = served(make(sub(tmp_path, "a"), chat_model="served:1"))
    make(tmp_path / "a", chat_model="on-disk:2")  # config.json changed after the service started

    def forbidden(*args, **kwargs):
        raise AssertionError("describe read settings")
    monkeypatch.setattr(Settings, "load", forbidden)
    text = describe(status, "en")
    assert "served:1" in text and LOADED in text and "on-disk:2" not in text


COMMANDS = {
    "stop": ["  cairnmind stop"],
    "local": ["  cairnmind setup local"],
    "connect": ["  cairnmind setup local --backup FILE", "  cairnmind setup local --empty"],
    "remote-db": ["  cairnmind setup remote-db \\", "      --url DB_URL \\", "      --user USER \\",
                  "      --database DATABASE \\", "      --password-file PASSWORD_FILE"],
    "remote": ["  cairnmind setup remote \\", "      --url DB_URL \\", "      --user USER \\",
               "      --database DATABASE \\", "      --password-file PASSWORD_FILE \\",
               "      --models-url MODELS_URL \\", "      --provider PROVIDER \\", "      --chat-model CHAT_MODEL \\",
               "      --embedding-model EMBEDDING_MODEL \\", "      --allow-remote"],
}


def has_block(text, block):
    rows = text.split("\n")
    return any(rows[at:at + len(block)] == block for at in range(len(rows)))


def test_remote_db_says_the_data_is_not_copied(tmp_path):
    for language in ("en", "pl"):
        text = plain_words(describe(served(make(tmp_path)), language))
        assert plain_words(tr(terminal_setup.REMOTE_DB, language)) in text
        assert "cairnmind backup FILE" in text and "cairnmind restore FILE" in text


def test_each_profile_names_its_exact_command(tmp_path, monkeypatch):
    managed = describe(served(make(sub(tmp_path, "m"))), "en")
    for name in ("stop", "local", "remote-db", "remote"):
        assert has_block(managed, COMMANDS[name]), name
    assert "--backup" not in managed.split(COMMANDS["remote-db"][0])[0].split("cairnmind stop")[1]
    assert KEEP_OPEN not in managed

    connect = describe(served(remote_store(sub(tmp_path, "c"))), "en")
    assert has_block(connect, COMMANDS["connect"]) and has_block(connect, COMMANDS["remote"])
    assert not has_block(connect, COMMANDS["local"])
    assert LOCAL_BACK.format(url="https://db.example:2490") in connect and KEEP_OPEN in connect
    assert "--yes" not in "\n".join(row for row in connect.split("\n") if row.startswith("  cairnmind"))
    assert HOW in connect


def option_fields(argv):
    """The Request fields of a shown command, through the inverse of OPTION_NAMES."""
    names = {option: field for field, option in setup_profiles.OPTION_NAMES.items()}
    fields, rest = {}, argv[3:]
    while rest:
        field = names[rest[0]]
        value = True if field in {"allow_remote", "empty"} else rest[1]
        fields[field] = value
        rest = rest[1 if value is True else 2:]
    return fields


@pytest.mark.parametrize("connect_only", [False, True])
@pytest.mark.parametrize("profile", ["local", "remote-db", "remote"])
def test_every_shown_command_is_accepted_by_setup_as_written(profile, connect_only):
    setup = typer.main.get_command(cli.app).get_command(None, "setup")
    for argv in commands(profile, connect_only):
        assert argv[:2] == ["cairnmind", "setup"] and "--yes" not in argv
        context = setup.make_context("setup", argv[2:])  # every option exists and takes the value shown
        assert context.params["profile"] == profile
        request = setup_profiles.Request(profile, **option_fields(argv))
        assert setup_profiles.option_problem(request) is None, argv
        assert " ".join(line.strip().rstrip("\\").strip() for line in shown(argv)).split() == argv


def test_no_secret_or_credential_file_content_reaches_the_screen(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRNMIND_ARCADE_PASSWORD", "PRIVATE_DB_MARKER")
    monkeypatch.setenv("MODEL_KEY", "PRIVATE_KEY_MARKER")
    settings = remote_store(tmp_path, ollama_url="http://user:PRIVATE_URL_MARKER@127.0.0.1:11434",
                            memory_chat=dict(REMOTE, credential_ref="env:MODEL_KEY"))
    status = served(settings)
    for language in ("en", "pl"):
        text = describe(status, language)
        assert not any(marker in text for marker in (
            "PRIVATE_DB_MARKER", "PRIVATE_URL_MARKER", "PRIVATE_KEY_MARKER", "a-database-password"))


@pytest.mark.parametrize("answer", [None, {}, {"topology": []}, {"topology": {"storage": {"mode": "other"}}}, "text",
                                    ["topology"]])
def test_an_answer_of_another_shape_is_named_not_shown(answer):
    with pytest.raises(ValueError) as caught:
        describe(answer, "en")
    assert str(caught.value) == UNREADABLE


def test_a_place_without_this_machine_is_named_not_shown(tmp_path):
    status = served(make(tmp_path))
    del status["topology"]["this_machine"]
    with pytest.raises(ValueError) as caught:
        describe(status, "en")
    assert str(caught.value) == UNREADABLE


def test_the_screen_in_polish(tmp_path):
    status = served(remote_store(tmp_path, memory_chat=REMOTE, memory_embedding=REMOTE))
    text = describe(status, "pl")
    for line in lines(status["topology"], "pl"):
        assert line in text
    assert "Baza:db.example | Modele:zdalnie" in text
    for english in MESSAGES:
        if english != terminal_setup.TITLE:
            assert english[:40] not in text, english
    for name in ("LOCAL_BACK", "KEEP_OPEN", "HOW", "WHY_HERE", "LOADED", "REMOTE", "OPTIONAL_KEY"):
        assert tr(getattr(terminal_setup, name), "pl", url="https://db.example:2490")[:30] in text
    english = describe(status, "en")
    for rows in COMMANDS.values():
        assert has_block(text, rows) == has_block(english, rows)


def test_every_sentence_has_polish_with_the_same_placeholders():
    names = {name for name, value in vars(terminal_setup).items()
             if isinstance(value, str) and name.isupper() and (" " in value or name == "TITLE")}
    sentences = {getattr(terminal_setup, name) for name in names}
    assert len(names) >= 20 and sentences == set(MESSAGES)
    for english in sentences:
        polish = MESSAGES[english]

        def holes(text):
            return {name for _, name, _, _ in string.Formatter().parse(text) if name}
        assert polish != english and holes(polish) == holes(english), english
        assert tr(english, "pl") == polish and CATALOG[english] == polish and tr(english, "en") == english
    assert set(PLACEHOLDERS_OF.values()) <= set(" ".join(MESSAGES).replace(",", " ").replace(";", " ").split())
    assert topology_module.STATUS_STORAGE in CATALOG  # the marks' labels have Polish already


def test_the_module_sends_nothing_that_writes():
    tree = ast.parse(Path(terminal_setup.__file__).read_text(encoding="utf-8"))
    strings = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    assert not strings & {"POST", "PUT", "PATCH", "DELETE"}
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and getattr(node.func, "attr", getattr(node.func, "id", "")) == "request"]
    assert [[arg.value for arg in call.args[:2]] for call in calls] == [["GET", "/api/status"]]


class StatusBroker(UIBroker):
    """Answers /api/status with `status`, or raises `refuse`; records every request."""

    def __init__(self, project, status):
        super().__init__(project)
        self.status, self.refuse, self.seen = status, None, []

    def request(self, method, path, *, params=None, json=None, timeout=20):
        self.seen.append((method, path, params, json))
        if path == "/api/status":
            if self.refuse:
                raise self.refuse
            return self.status
        return super().request(method, path, params=params, json=json, timeout=timeout)


def ui(tmp_path, monkeypatch, status, language="en"):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch, language)
    broker = StatusBroker(broker.projects[0], status)
    return SidebarApp(settings, broker=broker, workspace=workspace), broker


async def choose_the_entry(app, pilot):
    app.action_settings()
    await wait_until(pilot, lambda: isinstance(app.screen, ChoiceModal))
    assert app.screen.choices[-1][1] == "topology" and len(app.screen.choices) == 5
    if getattr(app, "language", "en") == "pl":
        assert "Baza i modele" in screen_text(app)
    await pilot.press("down", "down", "down", "down", "enter")


def notice_of(app):
    return str(app.query_one("#panel-notice", Static).render())


@pytest.mark.asyncio
@pytest.mark.parametrize("language,size", [("en", (80, 24)), ("en", (120, 35)), ("pl", (80, 24))])
async def test_settings_opens_the_screen_with_one_read(tmp_path, monkeypatch, language, size):
    status = served(make(sub(tmp_path, "service")))
    app, broker = ui(tmp_path, monkeypatch, status, language)
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        before = len(broker.seen)
        await choose_the_entry(app, pilot)
        await wait_until(pilot, lambda: isinstance(app.screen, MessageModal))
        assert app.screen.heading == "Storage and models"
        await pilot.pause()
        if language == "pl":
            assert tr(terminal_setup.TITLE, "pl") == "Baza i modele" and "Baza i modele" in screen_text(app)
        assert plain_words(lines(status["topology"], language)[0])[:30] in plain_words(screen_text(app))
        await pilot.press("escape")
        await wait_until(pilot, lambda: not isinstance(app.screen, MessageModal))
    assert broker.seen[before:] == [("GET", "/api/status", None, None)]


@pytest.mark.asyncio
async def test_the_screen_fits_and_scrolls_at_80_by_24(tmp_path, monkeypatch):
    app, _ = ui(tmp_path, monkeypatch, served(make(sub(tmp_path, "service"))))
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await choose_the_entry(app, pilot)
        await wait_until(pilot, lambda: isinstance(app.screen, MessageModal))
        await pilot.pause()
        region = app.screen.query_one("#message-close", Button).region
        assert region.width > 0 and region.x >= 0 and region.right <= 80 and region.bottom <= 24
        body = app.screen.query_one(".cm-dialog-body")
        assert body.max_scroll_y > 0
        body.scroll_end(animate=False)
        await pilot.pause()
        assert body.scroll_y == body.max_scroll_y and plain_words(OPTIONAL_KEY).endswith("or saved.")
        assert "or saved." in screen_text(app)  # the last words of the last paragraph; the rows are wrapped inside the frame
        for page in range(body.max_scroll_y + 1):  # nothing is cut: no rendered row of the body ends in an ellipsis
            body.scroll_to(y=page, animate=False)
            await pilot.pause()
            assert not any("…" in row for row in screen_text(app).split("\n")), page


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_a_service_that_does_not_answer_is_said_and_nothing_is_shown(tmp_path, monkeypatch, language):
    app, broker = ui(tmp_path, monkeypatch, {"topology": "no"}, language)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        broker.refuse = BrokerError(503, "The client service is unavailable; running sessions are kept.")
        await choose_the_entry(app, pilot)
        await wait_until(pilot, lambda: "running sessions are kept" in notice_of(app) or "sesje" in notice_of(app))
        assert not isinstance(app.screen, MessageModal)
        assert tr(NO_ANSWER, language)[:30] in notice_of(app)  # and where to look instead
        broker.refuse = None  # an answer of another shape
        await choose_the_entry(app, pilot)
        await wait_until(pilot, lambda: tr(UNREADABLE, language)[:40] in notice_of(app))
        assert not isinstance(app.screen, MessageModal)