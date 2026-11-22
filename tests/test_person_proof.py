"""The key a person's act needs (#214): the shape of the proof, where it is read, and the sentences. No engine."""
from __future__ import annotations

import json
import shutil
import socket
import sys

import pytest

from cairnmind import person_proof
from cairnmind.config import Settings
from cairnmind.confirmation import NOT_PROVEN
from cairnmind.runtime_sockets import prepare_socket_base, socket_directory, workspace_namespace
from cairnmind.terminal_i18n_messages import BROKER_MESSAGES

KEY = "ab" * 32
OTHER = "cd" * 32


@pytest.fixture
def settings(tmp_path):
    return Settings.load(tmp_path / "data", create=True)


@pytest.fixture
def workspace_socket(settings):
    """A real unix socket where this installation's workspace socket is: a private directory of the user."""
    prepare_socket_base()
    directory = socket_directory("ui", workspace_namespace(settings.data_dir))
    directory.mkdir(mode=0o700, parents=True)
    directory.chmod(0o700)
    path = directory / "tmux.sock"
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    try:
        yield path
    finally:
        server.close()
        shutil.rmtree(directory, ignore_errors=True)


def fake_tmux(tmp_path, key=None, version="3.4"):
    """A tmux that records argv and standard input of every call and keeps the key a `source-file -` sets."""
    state, log = tmp_path / "fake_key", tmp_path / "fake_log.jsonl"
    if key:
        state.write_text(key)
    program = tmp_path / "fake-tmux"
    program.write_text(f"""#!{sys.executable}
import json, re, sys
args = sys.argv[1:]
stdin = sys.stdin.read() if "source-file" in args else None
open({str(log)!r}, "a").write(json.dumps({{"argv": args, "stdin": stdin}}) + "\\n")
if "-V" in args:
    print("tmux {version}")
elif "show-options" in args:
    try:
        print(open({str(state)!r}).read())
    except OSError:
        pass
elif "source-file" in args:
    found = re.search(r"@cairnmind_person_key ([0-9a-f]+)", stdin or "")
    if found:
        open({str(state)!r}, "w").write(found.group(1))
""")
    program.chmod(0o755)

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return str(program), calls


def read(settings, socket_path, binary):
    settings.tmux_executable = binary
    return person_proof.read_key(settings, socket_path)


def test_the_key_is_64_hex_and_set_only_through_standard_input():
    key = person_proof.new_key()
    assert len(key) == 64 and set(key) <= set("0123456789abcdef") and key != person_proof.new_key()
    assert person_proof.set_key_input(key) == f"set-option -g @cairnmind_person_key {key}\n"
    for bad in ("", "abc", KEY.upper(), KEY + "\n", "-" + KEY[1:]):
        with pytest.raises(ValueError):
            person_proof.set_key_input(bad)


def test_the_workspace_socket_is_this_installations_private_socket(settings, workspace_socket, tmp_path):
    assert person_proof.workspace_socket(settings, str(workspace_socket)) == workspace_socket
    # Another data directory has another namespace.
    other = Settings.load(tmp_path / "other", create=True)
    assert person_proof.workspace_socket(other, str(workspace_socket)) is None
    for value in (None, "", 7, "tmux.sock", "\0" + str(workspace_socket), str(workspace_socket.parent),
                  str(workspace_socket.parent / "other.sock"), str(tmp_path / "tmux.sock"),
                  str(workspace_socket) + "x" * 200):
        assert person_proof.workspace_socket(settings, value) is None, value


def test_a_loose_directory_a_link_or_a_plain_file_is_not_the_workspace_socket(settings, workspace_socket, tmp_path):
    directory = workspace_socket.parent
    directory.chmod(0o755)
    assert person_proof.workspace_socket(settings, str(workspace_socket)) is None
    directory.chmod(0o700)
    # A link in the place of the directory.
    real = tmp_path / "real"
    shutil.move(directory, real)
    directory.symlink_to(real, target_is_directory=True)
    assert person_proof.workspace_socket(settings, str(workspace_socket)) is None
    directory.unlink()
    shutil.move(real, directory)
    # A regular file, and a link, in the place of the socket.
    workspace_socket.unlink()
    workspace_socket.write_text("")
    assert person_proof.workspace_socket(settings, str(workspace_socket)) is None
    workspace_socket.unlink()
    workspace_socket.symlink_to(tmp_path)
    assert person_proof.workspace_socket(settings, str(workspace_socket)) is None


def test_read_key_answers_key_no_key_or_unavailable(settings, workspace_socket, tmp_path):
    binary, calls = fake_tmux(tmp_path, KEY)
    assert read(settings, workspace_socket, binary) == ("key", KEY)
    argv = calls()[-1]["argv"]
    assert argv == ["-S", str(workspace_socket), "show-options", "-gqv", "@cairnmind_person_key"]
    (tmp_path / "fake_key").write_text("")
    assert read(settings, workspace_socket, binary) == ("no_key", None)
    (tmp_path / "fake_key").write_text("not a key")
    assert read(settings, workspace_socket, binary) == ("unavailable", None)
    (tmp_path / "fake_key").write_text(KEY.upper())
    assert read(settings, workspace_socket, binary) == ("unavailable", None)
    assert read(settings, workspace_socket, "/nonexistent/tmux")[0] == "unavailable"
    failing = tmp_path / "failing"
    failing.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(1)\n")
    failing.chmod(0o755)
    assert read(settings, workspace_socket, str(failing)) == ("unavailable", None)


def headers(socket_path=None, key=None):
    return {name: value for name, value in ((person_proof.SOCKET_HEADER, socket_path and str(socket_path)),
                                            (person_proof.KEY_HEADER, key)) if value is not None}


def refusal(settings, sent, **kwargs):
    with pytest.raises(person_proof.PersonProofRefused) as caught:
        person_proof.check(settings, sent, **kwargs)
    return caught.value.message


def test_a_wrong_key_another_socket_or_an_old_workspace_is_refused(settings, workspace_socket, tmp_path):
    binary, _ = fake_tmux(tmp_path, KEY)
    settings.tmux_executable = binary
    person_proof.check(settings, headers(workspace_socket, KEY))
    assert refusal(settings, {}) == person_proof.PROOF_MISSING
    assert refusal(settings, {}, refusal=person_proof.IMPORT_REPLACE) == person_proof.IMPORT_REPLACE
    assert refusal(settings, headers(workspace_socket, OTHER)) == person_proof.PROOF_REFUSED
    assert refusal(settings, headers(workspace_socket, None)) == person_proof.PROOF_REFUSED
    assert refusal(settings, headers(None, KEY)) == person_proof.PROOF_REFUSED
    assert refusal(settings, headers(tmp_path / "tmux.sock", KEY)) == person_proof.PROOF_REFUSED
    assert refusal(settings, headers(workspace_socket, "Ă©" + KEY)) == person_proof.PROOF_REFUSED
    (tmp_path / "fake_key").write_text("")
    assert refusal(settings, headers(workspace_socket, KEY)) == person_proof.NO_WORKSPACE_KEY
    settings.tmux_executable = "/nonexistent/tmux"
    assert refusal(settings, headers(workspace_socket, KEY)) == person_proof.TMUX_MISSING
    failing = tmp_path / "failing"
    failing.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(1)\n")
    failing.chmod(0o755)
    settings.tmux_executable = str(failing)
    assert refusal(settings, headers(workspace_socket, KEY)) == person_proof.PROOF_REFUSED


def test_no_tmux_at_all_is_a_refusal_that_names_tmux_executable(settings, workspace_socket, monkeypatch):
    monkeypatch.setattr(person_proof, "resolve_tmux", lambda settings: None)
    message = refusal(settings, headers(workspace_socket, KEY))
    assert message == person_proof.TMUX_MISSING and "tmux_executable" in message
    monkeypatch.setattr(person_proof, "resolve_tmux", lambda settings: (_ for _ in ()).throw(ValueError("bad")))
    assert refusal(settings, headers(workspace_socket, KEY)) == person_proof.TMUX_MISSING


def test_a_panel_sends_the_proof_only_from_this_workspace(settings, workspace_socket, tmp_path):
    binary, _ = fake_tmux(tmp_path, KEY)
    settings.tmux_executable = binary
    inside = {"TMUX": f"{workspace_socket},123,0"}
    assert person_proof.workspace_headers(settings, inside) == headers(workspace_socket, KEY)
    # A coding session's shell: the native server's socket, or none.
    assert person_proof.workspace_headers(settings, {"TMUX": f"{tmp_path / 'native' / 'tmux.sock'},1,0"}) == {}
    assert person_proof.workspace_headers(settings, {}) == {}
    (tmp_path / "fake_key").write_text("")
    assert person_proof.workspace_headers(settings, inside) == headers(workspace_socket)
    settings.tmux_executable = "/nonexistent/tmux"
    assert person_proof.workspace_headers(settings, inside) == {}


def test_the_broker_passes_on_only_well_formed_headers():
    sent = headers("/tmp/cairnmind-ui-1-x/tmux.sock", KEY)
    assert person_proof.forwardable(sent) == sent
    assert person_proof.forwardable({}) == {}
    assert person_proof.forwardable({person_proof.KEY_HEADER: "x" * 201}) == {}
    assert person_proof.forwardable({person_proof.SOCKET_HEADER: "/" + "x" * 4096}) == {}
    assert person_proof.forwardable({person_proof.KEY_HEADER: "Ă©"}) == {}
    assert person_proof.forwardable({person_proof.KEY_HEADER: "a\nb"}) == {}


def test_only_a_persons_post_paths_carry_the_proof():
    for path in ("/api/inspector/records/r1/review", "/api/inspector/conflicts/resolve",
                 "/api/inspector/plans/p1/confirm", "/api/inspector/record-kinds/accept",
                 "/api/inspector/record-kinds/withdraw", "/api/findings", "/api/memory/import",
                 "/api/inspector/records/r1/release", "/api/inspector/impact/verdict",
                 "/api/memory/import-published", "/api/memory/import-commit",
                 "/api/inspector/legacy-groups/adopt"):
        assert person_proof.PERSON_BROKER_PATH.fullmatch(path), path
    for path in ("/api/ask", "/api/client-runs", "/api/inspector/records", "/api/inspector/records/r1/release/x",
                 "/api/inspector/impact", "/api/inspector/impact/verdicts", "/api/memory/import-publishedx",
                 "/api/memory/import/x", "/api/memory/import-commit/x", "/api/inspector/records/a/b/review",
                 "/api/inspector/legacy-groups", "/api/inspector/legacy-groups/adopt/x"):
        assert not person_proof.PERSON_BROKER_PATH.fullmatch(path), path


@pytest.mark.unproven
def test_the_proof_is_not_present_until_require_passes(settings, workspace_socket, tmp_path):
    import asyncio
    from types import SimpleNamespace
    from fastapi import HTTPException
    binary, _ = fake_tmux(tmp_path, KEY)
    settings.tmux_executable = binary

    async def attempt(sent):
        try:
            await person_proof.require(SimpleNamespace(headers=sent), settings)
        except HTTPException as exc:
            return exc.status_code, exc.detail, person_proof.present()
        return 200, None, person_proof.present()

    async def scenario():
        return (await asyncio.create_task(attempt({})), await asyncio.create_task(attempt(headers(workspace_socket, KEY))),
                await asyncio.create_task(attempt(headers(workspace_socket, OTHER))), person_proof.present())
    refused, proven, wrong, outside = asyncio.run(scenario())
    assert refused == (403, person_proof.PROOF_MISSING, False)
    assert proven == (200, None, True)
    assert wrong == (403, person_proof.PROOF_REFUSED, False)
    assert outside is False  # a task's proof does not reach the code that started it, nor another request


def test_every_new_sentence_has_polish():
    from cairnmind.terminal_i18n import CATALOG, tr
    sentences = (person_proof.PROOF_MISSING, person_proof.PROOF_REFUSED, person_proof.NO_WORKSPACE_KEY,
                 person_proof.TMUX_MISSING, person_proof.IMPORT_REPLACE, NOT_PROVEN)
    for sentence in sentences:
        assert BROKER_MESSAGES[sentence] != sentence and BROKER_MESSAGES[sentence].strip()
        assert CATALOG[sentence] == BROKER_MESSAGES[sentence] == tr(sentence, "pl")


def test_no_sentence_holds_a_key():
    for sentence in (person_proof.PROOF_MISSING, person_proof.PROOF_REFUSED, person_proof.NO_WORKSPACE_KEY,
                     person_proof.TMUX_MISSING, person_proof.IMPORT_REPLACE, NOT_PROVEN):
        assert not __import__("re").search(r"[0-9a-f]{64}", sentence)
