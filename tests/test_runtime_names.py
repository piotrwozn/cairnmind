"""Names of running processes and of whole-database backups after the rename (issue #40).

What a process or a file of an earlier version carries is still understood: the identity a
service announces, the session key of a spooled hook event, and a backup's suffix and format.
"""
from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

from cairnmind import cli, cockpit, sessions
from cairnmind.config import BACKUP_FORMATS, BACKUP_SUFFIXES, BROKER_NAMES, SERVICE_NAMES, Settings
from cairnmind.service import instance_id
from cairnmind.store import Store


class Reply:
    def __init__(self, payload):
        self.payload, self.is_success = payload, True

    def json(self):
        return self.payload


class Client:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, **kwargs):
        return Reply(self.payload)


@pytest.mark.parametrize("name, alive", [("cairnmind", True), ("devbrain", True), ("another-service", False)])
def test_a_service_of_this_or_an_earlier_version_is_recognised_as_ours(tmp_path, monkeypatch, name, alive):
    settings = Settings.load(tmp_path / "data", create=True)
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: Client({"service": name, "instance": instance_id(settings)}))
    assert cli.service_alive(settings) is alive
    assert SERVICE_NAMES[0] == "cairnmind" and BROKER_NAMES[0] == "cairnmind-cockpit"


@pytest.mark.parametrize("key", ["cairnmind_session_id", "devbrain_session_id"])
def test_a_spooled_event_of_this_or_an_earlier_hook_keeps_its_logical_session(tmp_path, key):
    store = Store(tmp_path / "memory")
    root = tmp_path / "repo"
    root.mkdir()
    project = store.add_project("demo", root)
    manager = sessions.SessionManager(store, Settings.load(store.data_dir))
    prepared = manager.prepare(project["id"], "codex", timeout=0)
    manager.note_event({"hook_event_name": "SessionStart", "session_id": "native-one", "cwd": str(root),
                        key: prepared["session_id"], "event_id": "event-one"}, "codex")
    bound = store.query("SELECT native_session_id FROM sessions WHERE id=:id", {"id": prepared["session_id"]})
    assert bound[0]["native_session_id"] == "native-one"


def test_a_backup_is_written_under_the_current_names_and_an_earlier_one_is_restored(tmp_path):
    store = Store(tmp_path / "memory")
    root = tmp_path / "repo"
    root.mkdir()
    project = store.add_project("demo", root)
    note = store.snapshot(project["id"], "note://one", "Kept across a restore.", "note")
    folder = tmp_path / "backups"
    folder.mkdir()
    written = store.backup(folder)
    assert written.name.startswith("cairnmind-") and written.name.endswith(BACKUP_SUFFIXES[0])
    with gzip.open(written, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    assert envelope["format"] == BACKUP_FORMATS[0] == "cairnmind-arcadedb-backup"
    # The same backup as 0.9.0 wrote it: the earlier format name under the earlier suffix.
    earlier = folder / "devbrain-earlier.devbrain.json.gz"
    with gzip.open(earlier, "wt", encoding="utf-8") as handle:
        json.dump({**envelope, "format": BACKUP_FORMATS[1]}, handle)
    store.remember(project["id"], {"kind": "fact", "key": "later", "content": "Written after the backup."}, [note["id"]])
    store.restore(earlier)
    assert store.list_records(project["id"]) == []
    foreign = folder / "foreign.cairnmind.json.gz"
    with gzip.open(foreign, "wt", encoding="utf-8") as handle:
        json.dump({**envelope, "format": "something-else"}, handle)
    with pytest.raises(ValueError, match="not a supported Cairnmind ArcadeDB backup"):
        store.restore(foreign)


def installer_functions():
    script = (Path(__file__).resolve().parents[1] / "scripts" / "install-wsl.sh").read_text()
    start = script.index("# --- what an installed version left running")
    return script[start:script.index("# --- end ---", start)]


@pytest.fixture
def stand_ins(tmp_path):
    """A temporary directory for sockets, and stand-ins for tmux and for the installed command that log their calls."""
    tools, log = tmp_path / "bin", tmp_path / "calls.log"
    tools.mkdir()
    # Kept short: the path of a socket has a hard limit of about a hundred bytes.
    sockets = Path(tempfile.mkdtemp(prefix="cm-", dir="/tmp"))
    (tools / "tmux").write_text(
        "#!/bin/sh\necho \"tmux $3\" >> " + str(log) + "\n"
        "case \"$3\" in list-sessions) [ -e \"$2.alive\" ] ;; kill-server) rm -f \"$2.alive\" ;; esac\n")
    (tools / "cairnmind").write_text(
        "#!/bin/sh\necho \"$*\" >> " + str(log) + "\n"
        "if [ \"$1 $2\" = 'workspace status' ]; then [ \"$STATUS\" = fail ] && exit 3; "
        "printf '{\\n  \"available\": %s\\n}\\n' \"$STATUS\"; fi\n"
        # $STOP_FAILS and $COCKPIT_STOP_FAILS make that stop fail, saying two lines (#319)
        "if [ \"$*\" = stop ] && [ -n \"$STOP_FAILS\" ]; then echo 'first line' >&2; echo 'Error: refused' >&2; exit 2; fi\n"
        "if [ \"$*\" = 'cockpit stop' ] && [ -n \"$COCKPIT_STOP_FAILS\" ]; then echo 'first line' >&2; "
        "echo 'Error: refused' >&2; exit 2; fi\n"
        "if [ \"$*\" = 'workspace stop --clients' ] && [ -n \"$WORKSPACE_STOP_FAILS\" ]; then "
        "echo 'Error: refused' >&2; exit 2; fi\n")
    # `ps -A -ww -o pid= -o args=`: prints the file $PS_LINES (nothing by default) and ends with $PS_STATUS (0).
    # With $PS_TIMES it prints them only for that many calls (a process that ends meanwhile).
    (tools / "ps").write_text(
        "#!/bin/sh\n[ \"$*\" = '-A -ww -o pid= -o args=' ] || exit 64\n"
        "if [ -n \"$PS_TIMES\" ]; then n=$(cat \"$PS_LINES.n\" 2>/dev/null || echo 0); n=$((n + 1)); "
        "echo $n > \"$PS_LINES.n\"; [ \"$n\" -le \"$PS_TIMES\" ] || exit 0; fi\n"
        "[ -n \"$PS_LINES\" ] && cat \"$PS_LINES\"\nexit \"${PS_STATUS:-0}\"\n")
    for tool in tools.iterdir():
        tool.chmod(0o755)

    def terminal(name):
        """A listening socket where a terminal server of that name would have one, marked alive."""
        import socket
        folder = sockets / f"{name}-{os.getuid()}-0123456789abcdef"
        folder.mkdir()
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(folder / "tmux.sock"))
        (folder / "tmux.sock.alive").write_text("")
        return server, folder / "tmux.sock"

    def run(status="false", installed="cairnmind", ps=None, **extra):
        log.write_text("")
        environment = {key: value for key, value in os.environ.items() if key != "CAIRNMIND_INSTALL_STOP_WORKSPACE"}
        environment.update(PATH=f"{tools}:{os.environ['PATH']}", TMPDIR=str(sockets), STATUS=status, **extra)
        if ps is not None:  # lines of `ps`, with {tools} standing for the installed tool's directory
            (tmp_path / "ps.txt").write_text("".join(line.format(tools=tools) + "\n" for line in ps))
            environment["PS_LINES"] = str(tmp_path / "ps.txt")
        result = subprocess.run(
            ["bash", "-c", "set -euo pipefail\n" + installer_functions()
             + f'\nstop_installed "{tools}/{installed}" cairnmind "{tools}/python"\necho continued'],
            env=environment, capture_output=True, text=True, timeout=30)
        return result, [line for line in log.read_text().splitlines() if not line.startswith("tmux list-sessions")]

    yield run, terminal
    shutil.rmtree(sockets, ignore_errors=True)


def test_with_nothing_running_the_installer_stops_only_the_background_processes(stand_ins):
    run, terminal = stand_ins
    result, calls = run()
    assert result.returncode == 0 and "continued" in result.stdout
    assert calls == ["workspace status", "cockpit stop", "stop"]


@pytest.mark.parametrize("name", ["cairnmind-ui", "devbrain-ui", "cairnmind-native", "devbrain-native"])
def test_the_installer_refuses_while_a_workspace_or_a_coding_session_runs(stand_ins, name):
    run, terminal = stand_ins
    server, socket_path = terminal(name)
    # The installed version itself reports no workspace: the terminal belongs to another data directory.
    result, calls = run(status="false")
    assert result.returncode == 1 and "continued" not in result.stdout
    assert str(socket_path) in result.stderr and "cairnmind workspace stop --clients" in result.stderr
    assert calls == ["workspace status"]  # nothing was stopped behind the user's back
    server.close()


def test_the_installer_refuses_on_a_workspace_that_only_the_installed_version_reports(stand_ins):
    run, terminal = stand_ins
    result, calls = run(status="true")
    assert result.returncode == 1 and "workspace reported by cairnmind workspace status" in result.stderr
    assert calls == ["workspace status"]


def test_told_to_the_installer_closes_everything_including_terminals_of_other_data_directories(stand_ins):
    run, terminal = stand_ins
    server, socket_path = terminal("devbrain-native")
    result, calls = run(status="true", CAIRNMIND_INSTALL_STOP_WORKSPACE="1")
    assert result.returncode == 0 and "continued" in result.stdout
    assert calls == ["workspace status", "workspace stop --clients", "cockpit stop --clients", "tmux kill-server",
                     "cockpit stop", "stop"]
    assert not Path(str(socket_path) + ".alive").exists()
    server.close()


def test_a_status_that_cannot_be_read_is_said_and_not_taken_for_no_workspace(stand_ins):
    run, terminal = stand_ins
    result, calls = run(status="fail")
    assert result.returncode == 0 and "could not be ruled out" in result.stderr


def test_the_installer_names_open_mcp_servers_of_the_installed_version_and_stops_nothing(stand_ins):
    run, terminal = stand_ins
    result, calls = run(ps=[
        "  101 {tools}/python /h/.local/bin/cairnmind mcp --client external --project /p",
        "  102 {tools}/python3.12 /h/.local/bin/cairnmind --data-dir /d mcp --client=external",
        "  103 /usr/bin/vim notes.txt"])
    assert result.returncode == 1 and "continued" not in result.stdout
    assert "processes: 101 102" in result.stderr and "kill 101 102" in result.stderr
    assert "/p" not in result.stderr.replace("/python", "") and "/d" not in result.stderr
    assert calls == []  # not even the workspace was asked


def test_mcp_servers_of_other_pythons_and_native_ones_are_not_named(stand_ins):
    run, terminal = stand_ins
    result, calls = run(ps=[
        "101 /usr/bin/python3 /h/.local/bin/cairnmind mcp --client external",
        "102 {tools}/python /h/.local/bin/cairnmind mcp",
        "103 grep mcp --client external"])
    assert result.returncode == 0 and "continued" in result.stdout
    assert calls == ["workspace status", "cockpit stop", "stop"]


def test_a_ps_that_fails_is_said_and_the_installer_goes_on(stand_ins):
    run, terminal = stand_ins
    result, calls = run(PS_STATUS="1")
    assert result.returncode == 0 and "continued" in result.stdout
    assert "Note: ps failed" in result.stderr and "could not be ruled out" in result.stderr


def test_open_mcp_servers_are_named_even_when_the_installed_command_is_missing(stand_ins):
    run, terminal = stand_ins
    result, calls = run(installed="not-installed", ps=["101 {tools}/python /x/cairnmind mcp --client external"])
    assert result.returncode == 1 and "processes: 101" in result.stderr and calls == []


@pytest.mark.parametrize(("variable", "command", "after"), [
    ("STOP_FAILS", "stop", "The new version is asked to stop it once it is installed."),
    ("COCKPIT_STOP_FAILS", "cockpit stop", "`cairnmind cockpit stop` stops it later.")])
def test_a_failed_stop_is_said_with_its_last_line_and_the_installer_goes_on(stand_ins, variable, command, after):
    run, terminal = stand_ins
    result, calls = run(**{variable: "1"})
    assert result.returncode == 0 and "continued" in result.stdout
    assert f"Note: cairnmind {command} failed (it said: Error: refused)" in result.stderr
    assert after in result.stderr
    assert "first line" not in result.stderr
    assert calls == ["workspace status", "cockpit stop", "stop"]  # the other stop was still tried


def test_a_failed_stop_of_the_workspace_is_said_before_the_terminal_servers_are_ended(stand_ins):
    run, terminal = stand_ins
    server, socket_path = terminal("devbrain-native")
    result, calls = run(status="true", CAIRNMIND_INSTALL_STOP_WORKSPACE="1", WORKSPACE_STOP_FAILS="1")
    assert result.returncode == 0 and "continued" in result.stdout
    assert ("Note: cairnmind workspace stop --clients failed (it said: Error: refused), so the workspace and its "
            "coding sessions are ended at their terminal servers instead.") in result.stderr
    assert "stops it later" not in result.stderr  # nothing is left for later on this path (#319 recheck)
    assert "tmux kill-server" in calls  # what then closes them
    server.close()


def after_install(tmp_path, fails, ps_times=None, known=True):
    """stop_failed_service with a stand-in for the new command, which fails or succeeds at `stop`, and a stand-in
    `ps` that shows the replaced version's service of the data directory /d (for $ps_times looks, or always)."""
    command, tools = tmp_path / "cairnmind", tmp_path / "bin"
    tools.mkdir(exist_ok=True)
    command.write_text("#!/bin/sh\n" + ("echo first >&2; echo 'Error: refused' >&2; exit 2\n" if fails else "exit 0\n"))
    lines = tmp_path / "ps.txt"
    lines.write_text("501 /t/python -m cairnmind --data-dir /d serve\n502 /t/python -m cairnmind --data-dir /e serve\n"
                     "503 /t/python -m cairnmind --data-dir /d/x serve\n")
    count = tmp_path / "ps.n"
    count.unlink(missing_ok=True)
    limit = "" if ps_times is None else f'n=$(cat {count} 2>/dev/null || echo 0); n=$((n + 1)); echo $n > {count}; ' \
                                        f'[ "$n" -le {ps_times} ] || {{ grep -v " /d serve" {lines}; exit 0; }}\n'
    (tools / "ps").write_text(f"#!/bin/sh\n[ \"$*\" = '-A -ww -o pid= -o args=' ] || exit 64\n{limit}cat {lines}\n")
    for tool in (command, tools / "ps"):
        tool.chmod(0o755)
    arguments = '/t/python cairnmind /d' if known else '"" cairnmind ""'
    return subprocess.run(["bash", "-c", "set -euo pipefail\n" + installer_functions()
                           + f'\nstop_failed_service "{command}" {arguments} && echo written || echo refused'],
                          env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}"},
                          capture_output=True, text=True, timeout=60)


def test_a_service_that_still_answers_the_new_stop_ends_the_installer_before_the_store_is_written(tmp_path):
    refused = after_install(tmp_path, fails=True)
    assert refused.stdout.strip() == "refused" and "the installer stops here" in refused.stderr
    assert "first" not in refused.stderr and "(its stop said: Error: refused)" in refused.stderr
    # The process of that data directory is named, not the services of /e or /d/x; the way out is a rerun (#319 recheck).
    assert "processes: 501" in refused.stderr and "kill 501)" in refused.stderr and "run this installer again" in refused.stderr
    assert "502" not in refused.stderr and "503" not in refused.stderr and "cairnmind init" not in refused.stderr
    stopped = after_install(tmp_path, fails=False, ps_times=2)  # it ends while the installer looks
    assert stopped.stdout.strip() == "written" and stopped.stderr == ""


def test_a_stop_that_reports_success_while_the_old_service_still_runs_ends_the_installer(tmp_path):
    """A stop that was only asked, or a service that did not answer within 0.7 s ("not running", exit 0): its
    process still runs after ten seconds, so nothing writes the store (#319 recheck)."""
    result = after_install(tmp_path, fails=False)
    assert result.stdout.strip() == "refused" and "processes: 501" in result.stderr and "its stop said" not in result.stderr
    unknown = after_install(tmp_path, fails=False, known=False)  # without the data directory: said, and trusted
    assert unknown.stdout.strip() == "written" and "could not be confirmed" in unknown.stderr


def test_nothing_writes_the_store_between_the_new_install_and_the_guard_on_a_failed_stop():
    script = (Path(__file__).resolve().parents[1] / "scripts" / "install-wsl.sh").read_text()
    guard = script.index('stop_failed_service "$cairnmind_bin"')
    assert script.index("end_upgrade  # the new version") < guard
    assert guard < script.index('"$cairnmind_bin" engine install') and guard < script.index('"$cairnmind_bin" init')
    assert "service_stop_failed=1" in script


def test_services_of_other_data_directories_are_named_and_none_is_stopped(stand_ins):
    run, terminal = stand_ins
    result, calls = run(ps=[
        "201 {tools}/python -m cairnmind --data-dir /d serve",
        "202 {tools}/python -m cairnmind --data-dir /e f serve --defer-background-inference",
        "203 {tools}/python -I -m cairnmind --data-dir /d cockpit serve",
        "204 {tools}/python {tools}/cairnmind ask p how to serve",
        "205 /usr/bin/python3 -m cairnmind --data-dir /d serve"])
    assert result.returncode == 0 and "continued" in result.stdout
    assert "Knowledge services of the installed version still run, processes: 201 202" in result.stderr
    assert not any(number in result.stderr for number in ("203", "204", "205"))
    assert "/d" not in result.stderr.replace("/default", "") and "/e" not in result.stderr
    assert calls == ["workspace status", "cockpit stop", "stop"]  # nothing was stopped by process or by directory


def test_a_service_that_ends_after_its_stop_is_not_named(stand_ins):
    run, terminal = stand_ins
    # ps is read for bridges, for run sessions and for the first look: the service has ended by the second.
    result, calls = run(ps=["201 {tools}/python -m cairnmind --data-dir /d serve"], PS_TIMES="3")
    assert result.returncode == 0 and "continued" in result.stdout
    assert "still run" not in result.stderr


def test_coding_sessions_started_with_run_are_named_and_the_installer_stops(stand_ins):
    run, terminal = stand_ins
    result, calls = run(ps=[
        "301 {tools}/python {tools}/cairnmind run demo claude",
        "302 {tools}/python {tools}/cairnmind check run unit",
        "303 {tools}/python {tools}/cairnmind --data-dir /d run demo codex"])
    assert result.returncode == 1 and "continued" not in result.stdout
    assert "of the installed version are open, processes: 301" in result.stderr
    assert "302" not in result.stderr and "303" not in result.stderr
    assert calls == []


def test_the_new_installer_pieces_are_in_the_section_the_tests_cut():
    functions = installer_functions()
    assert "the knowledge service may still run the version being replaced" in functions
    assert 'service_stop_failed=1 failed_python="$python" failed_name="$name"' in functions
    assert "stop >/dev/null 2>&1 || true" not in functions.replace("cockpit stop --clients >/dev/null 2>&1 || true", "")
    assert "live_runs()" in functions and "live_services()" in functions


def mark_script(tmp_path, body):
    return subprocess.run(["bash", "-c", "set -euo pipefail\n" + installer_functions() + "\n" + body],
                          env={**os.environ, "HOME": str(tmp_path)}, capture_output=True, text=True, timeout=30)


def test_the_upgrade_mark_holds_the_installer_pid_and_only_its_installer_removes_it(tmp_path):
    mark = tmp_path / ".local" / "state" / "cairnmind" / "upgrade-in-progress"
    result = mark_script(tmp_path, f'begin_upgrade\ncat "{mark}"\necho "pid=$$"\nend_upgrade\n[ ! -e "{mark}" ] && echo gone\n'
                                   f'echo 99999 > "{mark}"\nend_upgrade\n[ -e "{mark}" ] && echo kept')
    assert result.returncode == 0, result.stderr
    held, shown = result.stdout.split("\n")[:2]
    assert shown == f"pid={held}"
    assert result.stdout.split("\n")[2:4] == ["gone", "kept"]
    assert not list(mark.parent.glob("upgrade-in-progress.*"))  # no temporary file is left


@pytest.mark.parametrize(("name", "code"), [("SIGINT", 130), ("SIGTERM", 143), ("SIGHUP", 129)])
def test_an_installer_ended_by_a_signal_or_an_error_removes_its_mark(tmp_path, name, code):
    import signal
    mark = tmp_path / ".local" / "state" / "cairnmind" / "upgrade-in-progress"
    failed = mark_script(tmp_path, "trap end_upgrade EXIT\nbegin_upgrade\nexit 1")
    assert failed.returncode == 1 and not mark.exists()
    script = "set -euo pipefail\n" + installer_functions() + "\ntrap end_upgrade EXIT\nbegin_upgrade\necho ready\nsleep 30 &\nwait $!\n"
    process = subprocess.Popen(["bash", "-c", script], env={**os.environ, "HOME": str(tmp_path)}, text=True,
                               stdout=subprocess.PIPE, start_new_session=True)
    try:
        assert process.stdout.readline().strip() == "ready" and mark.read_text().strip() == str(process.pid)
        process.send_signal(getattr(signal, name))
        assert process.wait(timeout=20) == code
        assert not mark.exists()
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def test_the_installer_marks_the_upgrade_around_the_replacement():
    script = (Path(__file__).resolve().parents[1] / "scripts" / "install-wsl.sh").read_text()
    after = script.index("# --- end ---")
    trap = script.index("trap 'rm -f -- \"$constraints_file\"; end_upgrade' EXIT")
    begin = script.index("\nbegin_upgrade\n", after)
    first_stop = script.index('stop_installed "$(uv tool dir --bin)', after)
    assert after < trap < begin < first_stop
    for tool in ("devbrain", "cairnmind"):
        assert f'stop_installed "$(uv tool dir --bin)/{tool}" {tool} "$(uv tool dir)/{tool}/bin/python"' in script
    finished = script.index("end_upgrade  #", first_stop)
    assert script.index("Installation did not provide the executable") < finished < script.index('if [[ -n "$connect_url" ]]', first_stop)
    assert script.index("uv tool install --python") < finished
    # The trap set for the install log still ends the upgrade: a failed install leaves no mark behind.
    log_trap = script.index("trap 'rm -f -- \"$constraints_file\" \"$install_log\"; end_upgrade' EXIT")
    assert begin < log_trap < script.index("uv tool install --python")


def test_scheduled_backups_of_both_suffixes_are_ordered_by_the_time_in_their_name(tmp_path):
    from cairnmind.service import daily_backups

    folder = tmp_path / "backups"
    assert daily_backups(folder) == []
    folder.mkdir()
    names = [f"{1794000000000000000 + index}{BACKUP_SUFFIXES[index % 2]}" for index in range(10)]
    for index, name in enumerate(names):
        (folder / name).write_bytes(b"x")
        # A copied directory: every file has the same, unrelated modification time.
        os.utime(folder / name, (1000, 1000))
    for other in ("before-restore-abc.cairnmind.json.gz", "before-schema-9-to-10-abc.devbrain.json.gz",
                  "cairnmind-20261111T000000-abcd1234.cairnmind.json.gz", "2026-notes.cairnmind.json.gz"):
        (folder / other).write_bytes(b"x")  # safety copies and user backups are not scheduled ones
    assert [path.name for path in daily_backups(folder)] == names
    assert cockpit.BROKER_NAMES == BROKER_NAMES
