"""The third round of the first start (#326): the small things two more newcomers met. No engine is needed.

One test for each point, on the printed text; the JSON of the touched commands in a pipe is what it was."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import plain_words

from cairnmind import cli, doctor_attention, first_start, ingest, knowledge_mode, project_removal
from cairnmind.terminal_commands import ask_summary
from cairnmind.terminal_i18n import tr
from cairnmind.topology import FROM_VARIABLE, WRITER_YES, _storage_line
from test_first_start_round2 import healthy

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install-wsl.sh"
BASH = "/bin/bash" if Path("/bin/bash").exists() else shutil.which("bash")


@pytest.fixture
def run(monkeypatch, tmp_path):
    def invoke(*arguments, terminal=True):
        monkeypatch.setattr(cli, "_stdout_is_terminal", lambda: terminal)
        return CliRunner().invoke(cli.app, ["--data-dir", str(tmp_path / "memory"), *arguments])
    return invoke


def dumped(value) -> str:
    """What `output` prints: the bytes a pipe got before this round."""
    return json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n"


# --- 1: a project name comes first ----------------------------------------------------------------------------

def test_a_question_given_without_a_project_says_the_project_comes_first_and_starts_nothing(monkeypatch, run):
    def forbidden(*args, **kwargs):
        raise AssertionError("nothing is opened or started for a wrong command line")
    for name in ("store_for", "store_for_read", "service_alive", "ensure_service", "_ensure_service_or_refuse", "request"):
        monkeypatch.setattr(cli, name, forbidden)
    asked = run("ask", "where is the login?")
    said = plain_words(asked.output)
    assert asked.exit_code == 2 and "Missing argument 'QUESTION'" in said
    assert first_start.PROJECT_FIRST.format(missing="QUESTION") in said and "cairnmind project list" in said
    # The same for every command whose first argument is the project, found where the error is made.
    assert first_start.PROJECT_FIRST.format(missing="CONTENT") in plain_words(run("remember", "a finding").output)
    assert first_start.PROJECT_FIRST.format(missing="PATH") in plain_words(run("project", "root", "demo").output)
    # Three positional arguments: every argument after the project is named, in order, also after a right project.
    for given in (("task", "update", "demo", "t1"), ("task", "update", "t1", "in_progress")):
        said = plain_words(run(*given).output)
        assert first_start.PROJECT_FIRST.format(missing="TASK_ID STATE") in said
    assert first_start.PROJECT_FIRST.format(missing="CLIENT KIND") in plain_words(
        run("record-kind", "accept", "demo", "claude").output)
    # Not when the first argument is not a project, or when the project itself is missing.
    assert "project name comes first" not in plain_words(run("project", "add", "demo").output)
    assert "project name comes first" not in plain_words(run("ask").output)
    assert first_start.MESSAGES[first_start.PROJECT_FIRST] != first_start.PROJECT_FIRST


# --- 2 and 3: the installer ---------------------------------------------------------------------------------------

def stand_in(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)
    return path


def test_the_installer_shows_one_line_for_a_good_install_and_uvs_own_output_for_a_bad_one(tmp_path):
    script = SCRIPT.read_text()
    start = script.index("# uv's list of every package is shown only")
    marker = "printf 'Installed Cairnmind and its dependencies.\\n'"
    block = script[start:script.index("\n", script.index(marker, start)) + 1]
    bin_dir = tmp_path / "bin"
    for name in ("mktemp", "rm"):
        bin_dir.mkdir(exist_ok=True)
        os.symlink(shutil.which(name), bin_dir / name)

    def install(uv_body):
        stand_in(bin_dir / "uv", uv_body)
        # end_upgrade is defined earlier in the script; its EXIT trap calls it (#319).
        prelude = (f'set -euo pipefail\npackage_dir=/p\nconstraints_file="{tmp_path}/constraints"\n: >"$constraints_file"\n'
                   'end_upgrade() { :; }\n')
        return subprocess.run([BASH, "-c", prelude + block + '\necho continued'],
                              env={"PATH": str(bin_dir), "TMPDIR": str(tmp_path)}, capture_output=True, text=True,
                              timeout=30)
    packages = "; ".join(f"echo ' + package{n}==1.0'" for n in range(53))
    good = install(packages)
    assert good.returncode == 0 and good.stderr == ""
    assert good.stdout == "Installed Cairnmind and its dependencies.\ncontinued\n"  # not uv's 53 package lines
    bad = install(f"{packages}; echo 'error: No solution found when resolving dependencies' >&2; exit 3")
    assert bad.returncode == 3 and "continued" not in bad.stdout and "Installed" not in bad.stdout
    assert "error: No solution found when resolving dependencies" in bad.stderr  # uv's own error, whole
    assert not list(tmp_path.glob("cairnmind-install.*"))


def test_the_installer_prints_no_init_block_and_keeps_the_one_time_marks(tmp_path):
    script = SCRIPT.read_text()
    assert script.count('init_said="$("$cairnmind_bin" init --json)"') == 3  # connect-only, Docker and native
    assert 'engine status >/dev/null && init_said="$("$cairnmind_bin" init --json)"' in script
    assert "\n    \"$cairnmind_bin\" init\n" not in script  # init's own sentence ended with a second "Next"
    start = script.index("show_init_marks() {")
    function = script[start:script.index("\n}\n", start) + 3]
    tool_dir = tmp_path / "uvtool"
    stand_in(tool_dir / "cairnmind" / "bin" / "python", f'exec "{sys.executable}" "$@"')
    uv = stand_in(tmp_path / "bin" / "uv", f'echo "{tool_dir}"')

    def shown(init_json):
        quoted = json.dumps(init_json).replace("'", "'\\''")
        result = subprocess.run([BASH, "-c", f"set -euo pipefail\n{function}\nshow_init_marks '{quoted}'"],
                                env={**os.environ, "PATH": f"{uv.parent}:{os.environ['PATH']}"}, capture_output=True,
                                text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        return result.stdout
    plain = {"data_dir": "/d", "next": "cairnmind doctor, then cairnmind project add NAME FOLDER"}
    assert shown(plain) == ""
    marked = {**plain, "confirmations_before_rule": {"marked_as_a_persons": 2, "still_before_the_rule": 1}}
    assert shown(marked) == first_start.INIT_MARKED.format(marked=2, left=1) + "\n"
    # An init result that cannot be read is said on stderr with what init said, never dropped.
    result = subprocess.run([BASH, "-c", f"set -euo pipefail\n{function}\nshow_init_marks 'not json'"],
                            env={**os.environ, "PATH": f"{uv.parent}:{os.environ['PATH']}"}, capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 0 and result.stdout == ""
    assert "could not be read" in result.stderr and "not json" in result.stderr


# --- 4: the stop without Java and Docker ----------------------------------------------------------------------------

def test_the_stop_without_java_and_docker_says_nothing_is_installed_and_names_the_simple_choice():
    script = SCRIPT.read_text()
    assert script.count("Nothing has been installed yet. Java is the simpler choice.") == 2  # both stops
    assert "Java 21** is the simplest" in (SCRIPT.parent.parent / "README.md").read_text()


# --- 5 and 6: doctor and the check ---------------------------------------------------------------------------------

def test_the_human_lines_leave_the_internal_database_name_out_and_the_json_keeps_it():
    arcade = {"mode": "managed", "engine": "native", "available": True, "database_available": True,
              "version_matches": True, "url": "http://127.0.0.1:2480", "database": "devbrain_b7c7a571edc197e2"}
    lines = doctor_attention.summary(healthy(arcadedb=arcade))
    assert "  - " + first_start.ENGINE_READY.format(url="http://127.0.0.1:2480") in lines
    assert "devbrain" not in " ".join(lines)
    unreadable = doctor_attention.summary(healthy(arcadedb={**arcade, "database_available": False}))
    assert "devbrain" not in " ".join(unreadable) and any("cannot be read" in line for line in unreadable)
    assert first_start.MESSAGES[first_start.ENGINE_READY] != first_start.ENGINE_READY
    assert "{database}" not in first_start.MESSAGES[first_start.ENGINE_READY]
    assert arcade["database"] == "devbrain_b7c7a571edc197e2"  # the report that --json prints is not touched here


def test_the_storage_line_says_who_sets_the_address_and_who_writes_to_the_store():
    place = {"storage": {"mode": "managed", "engine": "native", "url": "http://127.0.0.1:2480", "on_this_machine": True,
                         "variable": "CAIRNMIND_ARCADE_URL", "writer": {"role": "writer"}}}
    line = _storage_line(place, "en")
    assert line.endswith("The address is set by the environment variable CAIRNMIND_ARCADE_URL, for this process only. "
                         "This installation is the one that writes to the store.")
    assert "comes from" not in line
    polish = _storage_line(place, "pl")
    assert "zmienna środowiskowa CAIRNMIND_ARCADE_URL" in polish and "To ta instalacja zapisuje do tej bazy." in polish and "magazyn" not in polish
    assert FROM_VARIABLE.format(variable="CAIRNMIND_ARCADE_URL") in line and WRITER_YES in line


# --- 7: the brief's legend ----------------------------------------------------------------------------------------

def test_the_brief_legend_names_only_the_marks_and_titles_the_lines_use(monkeypatch):
    from cairnmind import brief as brief_module

    class Store:
        def resolve_project(self, project):
            return {"id": "p", "name": "demo"}

    monkeypatch.setattr("cairnmind.verification._root", lambda store, pid, worktree: Path("/w"))
    monkeypatch.setattr(brief_module, "_scoped_records", lambda *a: [])
    rows = {"lines": []}
    monkeypatch.setattr(brief_module, "_checkpoint", lambda *a: (None, rows["lines"]))
    monkeypatch.setattr(brief_module, "_plan", lambda *a: [])
    monkeypatch.setattr(brief_module, "_verification", lambda *a: [])
    monkeypatch.setattr(brief_module, "_commits", lambda *a: [])
    monkeypatch.setattr(brief_module, "_proposed", lambda records: [])

    def legend(*lines):
        rows["lines"] = list(lines)
        return brief_module.build_brief(Store(), "p")["text"].splitlines()[1]
    machine = ("machine", "No checkpoint has been saved for this project.")
    assert legend(machine) == "[machine] = measured now by Cairnmind"
    assert legend(machine, ("reported", "Checkpoint summary: half done")) == (
        "[machine] = measured now by Cairnmind; [reported] = stored by an assistant or a person, not verified")
    assert legend(("reported", "Checkpoint summary: half done")) == "[reported] = stored by an assistant or a person, not verified"
    assert legend(machine, ("machine", "Next task is ready | task: Add the limit")) == (
        "[machine] = measured now by Cairnmind; text after | is a stored title")
    # A " | " inside stored or Git text is not a title.
    assert legend(machine, ("reported", "Checkpoint summary: a | b")) == (
        "[machine] = measured now by Cairnmind; [reported] = stored by an assistant or a person, not verified")


# --- 8: the only place a person confirms ---------------------------------------------------------------------------

def test_the_next_steps_say_the_inspector_is_the_only_place_and_offer_no_command_line_confirmation():
    for sentence in (first_start.REMEMBER_PROPOSED, first_start.BRIEF_REVIEW):
        assert "only in the memory inspector" in sentence and "the command line confirms nothing" in sentence
        assert "--confirmed" not in sentence
        polish = first_start.MESSAGES[sentence]
        assert "tylko w inspektorze pamięci" in polish and "wiersz poleceń niczego nie potwierdza" in polish


# --- 9: project --help ---------------------------------------------------------------------------------------------

def test_project_help_describes_add_and_list(run):
    said = plain_words(run("project", "--help").output)
    assert "add Register a project" in said and "list List the registered projects" in said


# --- 10: run is named only when a coding assistant is installed ---------------------------------------------------

def test_the_workspace_refusal_names_run_only_with_a_coding_assistant(monkeypatch):
    found = {"available": False, "code": "tmux_missing"}
    monkeypatch.setattr(first_start.shutil, "which", lambda name, *a, **k: None)
    without = first_start.workspace_refusal(found)
    assert without.endswith(first_start.WITHOUT_WORKSPACE_NO_CLIENT) and "cairnmind run" not in without
    for installed in ("codex", "claude"):
        monkeypatch.setattr(first_start.shutil, "which", lambda name, installed=installed, **k: "/bin/x" if name == installed else None)
        assert first_start.workspace_refusal(found).endswith(first_start.WITHOUT_WORKSPACE)
    assert first_start.MESSAGES[first_start.WITHOUT_WORKSPACE_NO_CLIENT] != first_start.WITHOUT_WORKSPACE_NO_CLIENT


# --- 11: ask without models ------------------------------------------------------------------------------------------

MATCH = {"id": "rec", "source_id": "s1", "uri": "manual://dab5ecce-849d-41fe-9740-d9271baafeb5", "kind": "fact",
         "match": "full_text", "status": "proposed", "excerpt": "Accounts lock after five failed logins."}


def answer(*matches):
    found = {"matches": list(matches), "held_back": None, "left_out": False, "warnings": [], "elapsed_seconds": 0.1}
    return knowledge_mode.unavailable_answer(found)


def test_ask_without_models_names_a_stored_finding_drops_the_lone_heading_and_ends_with_a_next_step(monkeypatch, run):
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)  # no coding assistant installed
    shown = ask_summary(answer(MATCH), str)
    lines = shown.splitlines()
    assert '- stored finding (unconfirmed): "Accounts lock after five failed logins."' in lines
    assert "manual://" not in shown and "Deterministic-only mode" not in shown
    assert lines[-2:] == [first_start.GET_MODELS, first_start.ASK_NEXT] and lines[-3] == ""
    pl = ask_summary(answer(MATCH), lambda text: tr(text, "pl"))
    assert "zapisane ustalenie (niepotwierdzone)" in pl and pl.splitlines()[-1] == tr(first_start.ASK_NEXT, "pl")
    # With an assistant installed, installing one is not the next step: the models are.
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "/bin/x" if name == "claude" else None)
    installed = ask_summary(answer(MATCH), str)
    assert installed.splitlines()[-1] == first_start.GET_MODELS and first_start.ASK_NEXT not in installed
    # A file match keeps its address; the JSON in a pipe is the answer as it was, byte for byte (status unavailable).
    file_match = {**MATCH, "id": "c1", "uri": "/w/app.py", "kind": "code"}
    assert "- /w/app.py (unconfirmed)" in ask_summary(answer(file_match), str)
    result = answer(MATCH, file_match)
    monkeypatch.setattr(cli, "service_alive", lambda settings: True)
    monkeypatch.setattr(cli, "request", lambda settings, method, path, **kw: result)
    assert run("ask", "demo", "where?", terminal=False).stdout == dumped(result)
    assert result["status"] == "unavailable" and result["full_text_matches"][0]["uri"] == MATCH["uri"]
    assert "stored finding" in run("ask", "demo", "where?").stdout


# --- 12: project add, the warning before the next step -------------------------------------------------------------

def test_project_add_prints_the_warning_before_the_next_step_and_its_json_is_unchanged(monkeypatch, run, tmp_path):
    class Store:
        def list_projects(self):
            return []

        def add_project(self, name, root):
            return {"id": "p1", "name": name, "root": str(root)}

        def job_summary(self, project_id):
            return {group: {"pending": 0, "running": 0} for group in ("indexing", "extraction")}
    monkeypatch.setattr(cli, "store_for", lambda *a, **k: Store())
    monkeypatch.setattr(project_removal, "removed_rows", lambda reader: [])  # this fake store has no query (#327)
    monkeypatch.setattr(ingest, "index_project", lambda store, pid: {"files": 1})
    monkeypatch.setattr(ingest, "register_default_histories", lambda *a: {})
    folder = tmp_path / "plain"
    folder.mkdir()
    (folder / "a.txt").write_text("x")
    said = run("project", "add", "plain", str(folder)).stdout.splitlines()
    assert said[0].startswith("Registered plain in ") and said[1] == first_start.NOT_GIT.format(root=folder.resolve())
    assert said[2].startswith("Next: cairnmind remember plain") and len(said) == 3
    piped = run("project", "add", "plain", str(folder), terminal=False).stdout
    assert piped == dumped({"project": {"id": "p1", "name": "plain", "root": str(folder.resolve())},
                            "indexed": {"files": 1}, "history": {},
                            "jobs": {"queued": 0, "service_running": False, "waiting_because": None}})


def test_every_new_sentence_has_polish():
    for name in ("PROJECT_FIRST", "PROJECT_ADDED_NEXT", "PROJECT_EXISTED_NEXT", "PROJECT_ADDED", "PROJECT_EXISTED",
                 "STORED_FINDING", "ASK_NEXT", "WITHOUT_WORKSPACE_NO_CLIENT"):
        english = getattr(first_start, name)
        assert first_start.MESSAGES[english] != english, name
