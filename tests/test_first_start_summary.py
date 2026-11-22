"""The first check: what `cairnmind doctor` prints in a terminal, and the installer's copy of it (#287)."""
import json

import pytest

from cairnmind import cli, doctor_attention, first_start

SUMMARY = doctor_attention.summary


def healthy(**changes):
    report = {"python": "3.12.3", "data_dir": "/home/me/.local/share/cairnmind", "environment_notes": [],
              "clients": {"codex": {"installed": False, "verified_resume": False},
                          "claude": {"installed": False, "verified_resume": False}},
              "java": {"on_path": True, "executable": "/usr/bin/java", "compatible": True, "major": 21},
              "tmux": {"code": "tmux_missing"},
              "arcadedb": {"mode": "managed", "engine": "native", "available": True, "database_available": True,
                           "url": "http://127.0.0.1:2480", "database": "cairnmind"},
              "knowledge_agent": first_start.KNOWLEDGE_DISABLED,
              "ollama": {"available": False, "url": "http://127.0.0.1:11434"}, "memory_models": {},
              "service": {"available": False}, "service_running": False, "service_url": "http://127.0.0.1:8765",
              "store_writer": {"role": "writer"}}
    report.update(changes)
    return report


def test_a_healthy_deterministic_report_lists_required_then_optional_then_the_next_step():
    lines = SUMMARY(healthy())
    assert lines[0] == "Memory is kept in /home/me/.local/share/cairnmind."
    assert lines[1] == first_start.REQUIRED_PRESENT
    assert lines[2] == "  - Database engine: answers at http://127.0.0.1:2480; the memory database can be read."  # #326: no internal name
    assert lines[3] == "  - " + first_start.SERVICE_ON_DEMAND
    assert lines[4] == first_start.NOTHING_MISSING
    assert lines[5] == first_start.OPTIONAL_OFF
    optional = [line for line in lines if line.startswith("  - ")][2:]
    assert [line.split(":")[0] for line in optional] == ["  - Local models", "  - Terminal workspace", "  - Codex",
                                                         "  - Claude Code"]
    assert "Install tmux" in optional[1] and "Every other command works without it" in optional[1]
    assert lines[-2] == first_start.FULL_REPORT and lines[-1] == first_start.NEXT_START
    assert first_start.NOTES not in lines  # an empty group is not printed
    assert not any("None" in line or "cockpit" in line for line in lines)


def test_a_missing_java_is_required_and_missing_and_the_next_step_is_to_fix_it():
    broken = healthy(java={"on_path": False, "compatible": False},
                     arcadedb={"mode": "managed", "engine": "native", "available": False, "url": "http://x:1",
                               "error": "ConnectError"})
    lines = SUMMARY(broken)
    heading = lines.index(first_start.REQUIRED_MISSING)
    missing = lines[heading + 1:lines.index(first_start.OPTIONAL_OFF)]
    assert any(line.startswith("  - Java 21 or newer is needed for the engine: not found. ") for line in missing)
    # Java is what stops the engine: the promise that the next command starts it is not made.
    assert not any("does not answer" in line or "starts it" in line for line in missing)
    assert any("sudo apt install -y openjdk-21-jre-headless" in line for line in missing)
    assert lines[-1] == first_start.NEXT_FIX
    assert not any(line.startswith("  - Database engine: answers") for line in lines)


def test_models_that_are_on_but_unreachable_are_optional_and_not_working():
    report = healthy(ollama={"available": False, "url": "http://127.0.0.1:11434"})
    del report["knowledge_agent"]
    lines = SUMMARY(report)
    # Not a reason to exit 1 (#313): under its own heading, after "Required and missing: nothing."
    assert lines[lines.index(first_start.OPTIONAL_BROKEN) - 1] == first_start.NOTHING_MISSING
    assert "  - Ollama does not answer; local models need `ollama serve`." in lines
    assert not any(line.startswith("  - Local models") for line in lines)
    reachable = healthy(ollama={"available": True, "chat_model_installed": True, "embedding_model_installed": True})
    del reachable["knowledge_agent"]
    assert "  - " + first_start.MODELS_ON in SUMMARY(reachable)


def test_a_ready_workspace_and_an_installed_verified_client_are_optional_and_on():
    lines = SUMMARY(healthy(tmux={"code": "ready"}, clients={
        "codex": {"installed": True, "verified_resume": True}, "claude": {"installed": False}}))
    assert lines[lines.index(first_start.OPTIONAL_OFF) - 1] == first_start.NOTHING_MISSING
    assert "  - " + first_start.WORKSPACE_ON in lines
    assert "  - Codex: installed and verified; start it with cairnmind run NAME codex." in lines
    assert any(line.startswith("  - Claude Code: not installed") for line in lines)
    assert first_start.OPTIONAL_ON in lines


def test_environment_notes_are_notes_not_missing():
    lines = SUMMARY(healthy(environment_notes=["CAIRNMIND_PORT is used by this process."]))
    assert lines[lines.index(first_start.NOTES) + 1] == "  - CAIRNMIND_PORT is used by this process."
    assert first_start.NOTHING_MISSING in lines and lines[-1] == first_start.NEXT_START
    assert "CAIRNMIND_PORT is used by this process." not in doctor_attention.attention(healthy(
        environment_notes=["CAIRNMIND_PORT is used by this process."]))


def test_a_reader_installation_is_present_and_names_who_writes_and_the_takeover():
    # #373: a reader is how a second installation is meant to be, so it is not under Required and missing.
    reader = healthy(store_writer={"role": "reader", "reason": "This installation only reads this store.",
                                   "next": "cairnmind store takeover --yes",
                                   "writer": {"installation": "abc12345", "host": "mac", "since": "2026-11-22T10:00:00Z"}})
    lines = SUMMARY(reader)
    assert first_start.NOTHING_MISSING in lines and lines[-1] == first_start.NEXT_START
    assert "  - " + first_start.READER_ROLE in lines
    mixed = healthy(store_writer={"role": "reader", "problem": "The writer row and the journal head differ."})
    assert lines[-1] != SUMMARY(mixed)[-1] and any("Store writer: reader." in line for line in SUMMARY(mixed))


def test_where_things_run_is_the_topology_lines_and_a_server_that_is_down_names_the_server():
    # #300's three lines are the one wording of where storage and models run; the engine line does not repeat it.
    place = {"lines": ["Storage: ArcadeDB at https://db.example:2480 on db.example, which this installation only "
                       "connects to; it is not installed, started or stopped here.",
                       "Memory models: none in use; the knowledge agent is disabled (deterministic-only mode).",
                       "On this machine: the knowledge service at http://127.0.0.1:8765."]}
    connect = healthy(java={"needed": False, "reason": "connect"}, topology=place,
                      arcadedb={"mode": "connect", "host": "db.example", "available": True, "database_available": True,
                                "url": "https://db.example:2480", "database": "team"})
    lines = SUMMARY(connect)
    assert lines[1:4] == place["lines"] and lines[4] == first_start.REQUIRED_PRESENT
    assert lines[5] == "  - Database engine: answers at https://db.example:2480; the memory database can be read."
    down = healthy(java={"needed": False}, arcadedb={"mode": "connect", "available": False,
                                                      "url": "https://db.example:2480", "error": "ConnectError"})
    assert ("  - The database server at https://db.example:2480 does not answer (ConnectError). Check that it "
            "runs and that this machine reaches it.") in SUMMARY(down)


def test_a_mac_names_its_chip_and_unified_memory_once_and_linux_says_what_it_said():
    from cairnmind.terminal_i18n import tr
    mac = healthy(gpu={"chip": "Apple M3 Pro", "memory_gib": 18, "note": "Apple M3 Pro, 18 GiB unified memory shared "
                                                                         "with the models"})
    lines = SUMMARY(mac)
    assert lines[1] == "This Mac: Apple M3 Pro, 18 GiB of unified memory, shared with the models."
    assert lines[2] == first_start.REQUIRED_PRESENT and sum("Apple M3 Pro" in line for line in lines) == 1
    assert tr(first_start.THIS_MAC, "pl", chip="Apple M3 Pro", gib=18) == (
        "Ten Mac: Apple M3 Pro, 18 GiB pamięci zunifikowanej, współdzielonej z modelami.")
    # A Mac whose chip could not be read, Linux and WSL: the summary is the one without a gpu block.
    for gpu in ({"note": "The GPU is not checked on macOS."},
                {"available": False, "note": "nvidia-smi not found; verify Windows NVIDIA driver and WSL GPU access."},
                {"available": True, "devices": ["RTX 4090, 24564 MiB, 555.42"]}):
        assert SUMMARY(healthy(gpu=gpu)) == SUMMARY(healthy())


STOPPED = "Local ArcadeDB is unavailable (ConnectError); run cairnmind engine status or cairnmind engine start"


def down(engine="native", **changes):
    arcade = {"mode": "managed", "engine": engine, "available": False, "url": "http://127.0.0.1:2480",
              "installed": True, "error": STOPPED, **changes}
    return healthy(arcadedb=arcade, store_writer={"role": "unknown", "reason": "ArcadeDB is not running"})


def test_a_stopped_engine_that_can_start_is_present_on_demand_not_missing():
    lines = SUMMARY(down())
    assert first_start.NOTHING_MISSING in lines
    assert "  - " + first_start.ENGINE_ON_DEMAND.format(url="http://127.0.0.1:2480") in lines
    assert not any("Store writer" in line or "does not answer" in line for line in lines)
    assert lines[-1] == first_start.NEXT_START
    assert SUMMARY(down("docker", installed=None))[-1] == first_start.NEXT_START


def test_a_stopped_engine_that_cannot_start_names_what_stops_it_once():
    docker = healthy(arcadedb={"mode": "managed", "engine": "docker", "available": False, "url": "http://x:1",
                               "error": STOPPED}, docker={"available": False, "error": "no daemon"},
                     store_writer={"role": "unknown"})
    lines = SUMMARY(docker)
    missing = lines[lines.index(first_start.REQUIRED_MISSING) + 1:lines.index(first_start.OPTIONAL_OFF)]
    assert len(missing) == 1 and "no daemon" in missing[0] and "next command that needs it starts it" not in missing[0]
    never = SUMMARY(down(installed=False))
    assert any(first_start.ENGINE_NOT_INSTALLED in line for line in never) and never[-1] == first_start.NEXT_FIX
    busy = SUMMARY(down(error="Local ArcadeDB is unavailable (ReadTimeout); run cairnmind engine status"))
    assert any("ReadTimeout); it may be starting or busy" in line for line in busy)
    assert not any(line.startswith("  - Database engine: not running") for line in busy)


def test_an_engine_of_another_release_is_not_present_and_names_both_releases():
    arcade = {"mode": "managed", "engine": "native", "available": True, "database_available": True,
              "url": "http://127.0.0.1:2480", "database": "cairnmind", "version": "25.9.1",
              "configured_version": "26.11.1", "version_matches": False}
    lines = SUMMARY(healthy(arcadedb=arcade))
    assert not any(line.startswith("  - Database engine: answers") for line in lines)
    assert any("runs release 25.9.1, not the pinned 26.11.1" in line and "cairnmind engine start" in line
               for line in lines)
    assert lines[-1] == first_start.NEXT_FIX
    assert any("answers" in line for line in SUMMARY(healthy(arcadedb={**arcade, "version_matches": True})))


def test_a_shell_whose_commands_are_refused_is_missing_not_a_note():
    refused = "CAIRNMIND_SESSION_ID and DEVBRAIN_SESSION_ID name different sessions; neither is used: commands in this shell are refused."
    informational = "CAIRNMIND_PORT is used by this process."
    lines = SUMMARY(healthy(environment_notes=[refused, informational], environment_blockers=[refused]))
    missing = lines[lines.index(first_start.REQUIRED_MISSING) + 1:lines.index(first_start.OPTIONAL_OFF)]
    assert missing == [f"  - {refused}"]
    notes = lines[lines.index(first_start.NOTES) + 1:lines.index(first_start.FULL_REPORT)]
    assert notes == [f"  - {informational}"] and lines[-1] == first_start.NEXT_FIX


def test_a_running_service_says_where():
    lines = SUMMARY(healthy(service_running=True))
    assert "  - Local service: running at http://127.0.0.1:8765." in lines


def test_the_installer_pipe_prints_the_same_summary(monkeypatch, capsys):
    import io
    report = healthy()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(report)))
    assert doctor_attention.main() == 0
    assert capsys.readouterr().out.splitlines() == SUMMARY(report)
    for unreadable in ({}, [1], {"environment_notes": "abc"}):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(unreadable)))
        assert doctor_attention.main() == 0
        assert capsys.readouterr().out == f"{first_start.REPORT_UNREADABLE} {first_start.FULL_REPORT}\n"


# --- cairnmind doctor: a summary in a terminal, the JSON everywhere else -----------------------------------------

@pytest.fixture
def quiet(monkeypatch, tmp_path):
    from cairnmind import api_runtime, database_credentials, engine, integrations, store_writer
    import httpx
    from cairnmind.config import Settings
    directory = tmp_path / "memory"
    directory.mkdir()
    (directory / "config.json").write_text(json.dumps({"port": 1, "arcade_url": "http://127.0.0.1:1"}))
    Settings.load(directory, create=True)
    monkeypatch.setattr(cli, "service_alive", lambda settings: False)
    monkeypatch.setattr(integrations, "_probe_cli", lambda name: {"installed": False})
    monkeypatch.setattr(database_credentials, "doctor_block", lambda settings: {"available": False})
    monkeypatch.setattr(engine, "resolve_java", lambda settings: None)
    monkeypatch.setattr(store_writer, "inspect", lambda settings: {"role": "writer"})
    monkeypatch.setattr(api_runtime, "coding_profiles_status", lambda settings: [])
    monkeypatch.setattr(cli, "store_for_read", lambda settings: (_ for _ in ()).throw(ValueError("no store")))
    real = httpx.Client
    monkeypatch.setattr(integrations.httpx, "Client", lambda **kw: real(
        transport=httpx.MockTransport(lambda request: httpx.Response(503)), **kw))
    return directory


def run_doctor(directory, *arguments):
    from typer.testing import CliRunner
    return CliRunner().invoke(cli.app, ["--data-dir", str(directory), "doctor", *arguments])


def test_doctor_prints_the_summary_in_a_terminal_and_json_otherwise(monkeypatch, quiet):
    monkeypatch.setattr(cli, "_stdout_is_terminal", lambda: True)
    summary = run_doctor(quiet)
    assert summary.exit_code == 1  # its engine is missing: something required (#313)
    assert summary.output.startswith(f"Memory is kept in {quiet}.")
    assert summary.output.splitlines()[1].startswith("Storage: ArcadeDB")  # the report's topology lines
    assert first_start.REQUIRED_MISSING in summary.output and first_start.FULL_REPORT in summary.output
    forced = run_doctor(quiet, "--json")
    assert json.loads(forced.output)["data_dir"] == str(quiet)
    monkeypatch.setattr(cli, "_stdout_is_terminal", lambda: False)
    assert json.loads(run_doctor(quiet).output) == json.loads(forced.output)


def test_an_environment_note_says_not_set_instead_of_none(monkeypatch, quiet):
    monkeypatch.setenv("CAIRNMIND_PORT", "8719")
    (quiet / "config.json").write_text(json.dumps({"arcade_url": "http://127.0.0.1:1"}))
    notes = json.loads(run_doctor(quiet, "--json").output)["environment_notes"]
    port = [note for note in notes if note.startswith("CAIRNMIND_PORT is used by this process")]
    assert port and "None" not in port[0] and port[0].endswith("and not set in config.json.")
