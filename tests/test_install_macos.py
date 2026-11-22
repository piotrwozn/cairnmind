"""The installer's checks on macOS and on Linux, with stand-ins for uname, java_home and the tools (#232).

The checks are cut out of scripts/install-wsl.sh between two marker lines and run with a PATH of stand-ins only, so
a Mac is imitated on Linux. On macOS (the CI job) /bin/bash is the bash 3.2 that macOS ships, and it runs them too.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install-wsl.sh"
JAVA_HOME_TOOL = "/usr/libexec/java_home"
# macOS's own /bin/bash is 3.2; prefer it there, as a person running the documented command gets it.
BASH = "/bin/bash" if Path("/bin/bash").exists() else shutil.which("bash")


def checks() -> str:
    script = SCRIPT.read_text()
    start = script.index("# --- the checks before installing")
    return script[start:script.index("# --- end of the checks ---", start)]


def tool(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)
    return path


def java(path: Path, major: int) -> Path:
    return tool(path, f"echo 'openjdk version \"{major}.0.5\" 2024-10-15' >&2")


def jdk(root: Path, major: int) -> Path:
    """A Java home with bin/java of that version."""
    java(root / "bin" / "java", major)
    return root


@pytest.fixture
def mac(tmp_path):
    """Stand-ins for a Mac with tmux 3.5 and uv and no Java; each test changes what it needs."""
    bin_dir = tmp_path / "bin"
    state = {"system": "Darwin", "release": "25.0.0", "java_home": None}

    def write():
        tool(bin_dir / "uname", f"case \"$1\" in -s) echo {state['system']} ;; -r) echo {state['release']} ;; esac")
        found = state["java_home"]
        tool(tmp_path / "java_home", f"echo '{found}'" if found else
             "echo 'Unable to find any JVMs matching version \"21+\".' >&2; exit 1")

    tool(bin_dir / "tmux", "echo 'tmux 3.5a'")
    tool(bin_dir / "uv", "exit 0")
    for name in ("tr", "mktemp", "sleep", "rm"):  # what the Docker question uses
        os.symlink(shutil.which(name), bin_dir / name)

    def run(show_flag=False, pull_models=False, **environment):
        write()
        text = ("pull_models=true\n" if pull_models else "") + checks().replace(
            JAVA_HOME_TOOL, str(tmp_path / "java_home"))
        flag = ('\necho "engine_in_docker=${engine_in_docker:-unset}"\necho "workspace_ready=${workspace_ready:-unset}"'
                if show_flag else "")
        result = subprocess.run(
            [BASH, "-c", "set -euo pipefail\n" + text + '\necho "passed JAVA_HOME=${JAVA_HOME:-}"' + flag],
            env={"PATH": str(bin_dir), "HOME": str(tmp_path), **environment}, capture_output=True, text=True,
            timeout=30)
        return result

    return run, state, bin_dir, tmp_path


def test_the_checks_accept_a_mac_with_java_through_java_home(mac):
    run, state, bin_dir, tmp_path = mac
    state["java_home"] = str(jdk(tmp_path / "Temurin-21.jdk" / "Contents" / "Home", 21))
    result = run()
    assert result.returncode == 0, result.stderr
    assert f"passed JAVA_HOME={state['java_home']}" in result.stdout
    assert "Detected macOS." in result.stdout
    assert "apt" not in result.stdout + result.stderr


def test_java_home_comes_before_java_home_tool_and_path(mac):
    run, state, bin_dir, tmp_path = mac
    home = jdk(tmp_path / "brew" / "openjdk.jdk" / "Contents" / "Home", 21)
    state["java_home"] = str(jdk(tmp_path / "Old.jdk", 17))
    java(bin_dir / "java", 17)
    result = run(JAVA_HOME=str(home))
    assert result.returncode == 0, result.stderr
    assert f"passed JAVA_HOME={home}" in result.stdout


def test_an_older_java_in_java_home_is_refused_with_its_source(mac):
    run, state, bin_dir, tmp_path = mac
    home = jdk(tmp_path / "jdk17", 17)
    result = run(JAVA_HOME=str(home))
    assert result.returncode == 1 and "passed" not in result.stdout
    assert "Found Java 17" in result.stderr and "JAVA_HOME" in result.stderr and "brew install openjdk@21" in result.stderr


def test_a_java_home_without_java_is_said_and_the_search_goes_on(mac):
    run, state, bin_dir, tmp_path = mac
    state["java_home"] = str(jdk(tmp_path / "Zulu-21.jdk", 21))
    result = run(JAVA_HOME=str(tmp_path / "nothing-here"))
    assert result.returncode == 0, result.stderr
    assert "has no bin/java" in result.stderr
    assert f"passed JAVA_HOME={state['java_home']}" in result.stdout


def test_the_java_stub_of_macos_without_a_jdk_is_not_taken_for_java(mac):
    run, state, bin_dir, tmp_path = mac
    # /usr/bin/java with no JDK installed: prints a notice and fails.
    tool(bin_dir / "java", "echo 'The operation could not be completed. Unable to locate a Java Runtime.' >&2; exit 1")
    result = run()
    assert result.returncode == 1 and "passed" not in result.stdout
    assert "no JDK is installed" in result.stderr and "brew install openjdk@21" in result.stderr


def test_without_any_java_a_mac_gets_the_homebrew_hint(mac):
    run, state, bin_dir, tmp_path = mac
    result = run()
    assert result.returncode == 1
    assert "Java not found" in result.stderr and "brew install openjdk@21" in result.stderr
    assert "apt" not in result.stderr


def test_a_homebrew_jdk_that_nothing_finds_gets_the_exact_link_line(mac):
    # A link in /Library/Java/JavaVirtualMachines, not a JAVA_HOME in ~/.zprofile: a process that a desktop app
    # starts reads no shell profile, and it starts the engine too.
    run, state, bin_dir, tmp_path = mac
    prefix = tmp_path / "homebrew" / "opt" / "openjdk@21"
    jdk(prefix / "libexec" / "openjdk.jdk" / "Contents" / "Home", 21)
    tool(bin_dir / "brew", f"[ \"$1 $2\" = '--prefix openjdk@21' ] && echo '{prefix}'")
    result = run()
    assert result.returncode == 1
    assert (f'sudo ln -sfn "{prefix}/libexec/openjdk.jdk" /Library/Java/JavaVirtualMachines/openjdk-21.jdk'
            in result.stderr)
    assert "JAVA_HOME" not in result.stderr


def test_a_missing_uv_on_a_mac_gets_the_homebrew_hint(mac):
    run, state, bin_dir, tmp_path = mac
    state["java_home"] = str(jdk(tmp_path / "Temurin-21.jdk", 21))
    (bin_dir / "uv").unlink()
    result = run()
    assert result.returncode == 1 and "brew install uv" in result.stderr and "apt" not in result.stderr


def test_an_old_tmux_on_a_mac_gets_the_homebrew_hint_and_the_installer_goes_on(mac):
    run, state, bin_dir, tmp_path = mac
    state["java_home"] = str(jdk(tmp_path / "Temurin-21.jdk", 21))
    tool(bin_dir / "tmux", "echo 'tmux 3.3a'")
    result = run(show_flag=True)
    assert result.returncode == 0 and "passed" in result.stdout, result.stderr
    assert "Found tmux 3.3. Version 3.4+ is required for the terminal workspace" in result.stderr
    assert "brew install tmux" in result.stderr and "apt" not in result.stderr
    assert "workspace_ready=false" in result.stdout


@pytest.mark.parametrize("system,hint", [("Darwin", "brew install tmux"), ("Linux", "sudo apt install tmux")])
def test_without_tmux_the_installer_says_the_workspace_is_unavailable_and_goes_on(mac, system, hint):
    run, state, bin_dir, tmp_path = mac
    state.update(system=system, java_home=str(jdk(tmp_path / "Temurin-21.jdk", 21)))
    java(bin_dir / "java", 21)
    (bin_dir / "tmux").unlink()
    result = run(show_flag=True)
    assert result.returncode == 0, result.stderr
    assert "passed" in result.stdout and "workspace_ready=false" in result.stdout
    assert result.stderr == ("tmux not found, so the terminal workspace is unavailable; the installation goes on. "
                             f"For the workspace, install tmux 3.4+: {hint}\n")


def test_a_tmux_that_reports_no_version_does_not_stop_the_installer(mac):
    run, state, bin_dir, tmp_path = mac
    state.update(system="Linux")
    java(bin_dir / "java", 21)
    tool(bin_dir / "tmux", "exit 1")
    result = run(show_flag=True)
    assert result.returncode == 0, result.stderr
    assert "Cannot detect the tmux version" in result.stderr and "workspace_ready=false" in result.stdout


def test_the_installer_ends_with_the_workspace_lines_only_when_tmux_is_ready_and_then_the_summary():
    script = SCRIPT.read_text()
    ready = script.index('if [[ "$workspace_ready" == true ]]; then\n    printf \'Open the terminal workspace: cairnmind\\n\'')
    ending = script[ready:]
    # #287: no else line any more (the summary says the workspace is optional and off); the summary ends the output.
    assert "\nelse\n" not in ending and "cairnmind serve" not in ending
    assert ending.count("Open the terminal workspace") == 1
    last = script.rstrip().splitlines()[-1]
    assert last == '"$cairnmind_bin" doctor | "$(uv tool dir)/cairnmind/bin/python" -I -m cairnmind.doctor_attention'
    assert "Models were not downloaded" not in script
    assert script.count('engine status >/dev/null') == 2 and 'engine install >/dev/null' in script  # no engine JSON


def test_a_missing_uv_on_linux_is_one_install_line_and_run_again_without_a_second_document(mac):
    run, state, bin_dir, tmp_path = mac
    state.update(system="Linux")
    java(bin_dir / "java", 21)
    (bin_dir / "uv").unlink()
    result = run()
    assert result.returncode == 1
    assert result.stderr.endswith("uv not found on PATH. Install it, open a new terminal and run this installer "
                                  "again: curl -LsSf https://astral.sh/uv/install.sh | sh\n")
    assert "docs/INSTALL.md" not in result.stderr


def test_the_gpu_advice_is_printed_only_with_pull_models(mac):
    run, state, bin_dir, tmp_path = mac
    state.update(system="Linux", release="5.15.167.4-microsoft-standard-WSL2")
    java(bin_dir / "java", 21)
    assert "NVIDIA" not in run().stdout
    assert "Install the NVIDIA driver in Windows, not in Linux." in run(pull_models=True).stdout
    state.update(release="6.8.0-48-generic")
    assert "GPU" not in run().stdout
    assert "check local GPU access" in run(pull_models=True).stdout


def test_a_connect_only_mac_needs_no_java(mac):
    run, state, bin_dir, tmp_path = mac
    result = run(CAIRNMIND_ARCADE_CONNECT_URL="https://db.example.org:2490")
    assert result.returncode == 0, result.stderr


def test_an_upgrade_finds_a_workspace_in_the_macos_socket_folder(tmp_path):
    import socket
    import tempfile
    script = SCRIPT.read_text()
    start = script.index("# --- what an installed version left running")
    # Kept short: a socket path has a limit of about a hundred bytes.
    system_temp = Path(tempfile.mkdtemp(prefix="cm-", dir="/tmp"))
    try:
        functions = script[start:script.index("# --- end ---", start)].replace("/private/tmp", str(system_temp))
        folder = system_temp / f"cairnmind-{os.getuid()}" / "ui-0123456789abcdef"
        folder.mkdir(parents=True)
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(folder / "tmux.sock"))
        tool(tmp_path / "bin" / "tmux", "exit 0")
        result = subprocess.run([BASH, "-c", "set -euo pipefail\n" + functions + "\nlive_terminals"],
                                env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
                                     "TMPDIR": str(tmp_path)}, capture_output=True, text=True, timeout=30)
        server.close()
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [str(folder / "tmux.sock")]
    finally:
        shutil.rmtree(system_temp, ignore_errors=True)


def test_an_upgrade_without_tmux_counts_a_server_it_cannot_ask_as_live(tmp_path):
    import socket
    import tempfile
    script = SCRIPT.read_text()
    start = script.index("# --- what an installed version left running")
    system_temp = Path(tempfile.mkdtemp(prefix="cm-", dir="/tmp"))
    try:
        functions = script[start:script.index("# --- end ---", start)]
        folder = system_temp / f"cairnmind-ui-{os.getuid()}-0123456789abcdef"
        folder.mkdir()
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(folder / "tmux.sock"))
        bin_dir = tmp_path / "bin"  # `id` only: no tmux on PATH
        bin_dir.mkdir()
        (bin_dir / "id").symlink_to(shutil.which("id"))
        result = subprocess.run([BASH, "-c", "set -euo pipefail\n" + functions + "\nlive_terminals"],
                                env={"PATH": str(bin_dir), "TMPDIR": str(system_temp)},
                                capture_output=True, text=True, timeout=30)
        server.close()
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [str(folder / "tmux.sock")]
    finally:
        shutil.rmtree(system_temp, ignore_errors=True)


def test_the_mcp_server_check_and_the_upgrade_mark_run_on_the_bash_of_macos(tmp_path):
    script = SCRIPT.read_text()
    start = script.index("# --- what an installed version left running")
    functions = script[start:script.index("# --- end ---", start)]
    tool(tmp_path / "bin" / "ps", "[ \"$*\" = '-A -ww -o pid= -o args=' ] || exit 64\n"
         "echo '  501 /u/tools/cairnmind/bin/python /u/bin/cairnmind mcp --client external --project /p'\n"
         "echo '  502 /usr/bin/python3 other mcp --client external'\n"
         "echo '  503 /u/tools/cairnmind/bin/python /u/bin/cairnmind mcp'\n"
         "echo '  504 /u/tools/cairnmind/bin/python /u/bin/cairnmind mcp --client=external'")
    mark = tmp_path / ".local" / "state" / "cairnmind" / "upgrade-in-progress"
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "HOME": str(tmp_path)}
    result = subprocess.run(
        [BASH, "-c", "set -euo pipefail\n" + functions + "\nlive_bridges /u/tools/cairnmind/bin/python\n"
         'begin_upgrade\ncat "$upgrade_mark"\necho "pid=$$"\nend_upgrade\n'],
        env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    first, second, held, shown = result.stdout.splitlines()
    assert (first, second) == ("501", "504") and shown == f"pid={held}" and not mark.exists()
    # The refusal joins the ids with the bash 3.2 pattern substitution and ends the installer.
    refused = subprocess.run([BASH, "-c", "set -euo pipefail\n" + functions +
                              "\nstop_installed /u/bin/cairnmind cairnmind /u/tools/cairnmind/bin/python\necho went on"],
                             env=env, capture_output=True, text=True, timeout=30)
    assert refused.returncode == 1 and "went on" not in refused.stdout
    assert "processes: 501 504" in refused.stderr and "kill 501 504" in refused.stderr


def test_an_upgrade_without_tmux_does_not_count_a_socket_nothing_listens_on(tmp_path):
    """#287: with the installed version's Python a leftover socket is told from a live server by a connect."""
    import socket
    import tempfile
    script = SCRIPT.read_text()
    start = script.index("# --- what an installed version left running")
    system_temp = Path(tempfile.mkdtemp(prefix="cm-", dir="/tmp"))
    try:
        functions = script[start:script.index("# --- end ---", start)]
        bin_dir = tmp_path / "bin"  # `id` only: no tmux on PATH
        bin_dir.mkdir()
        (bin_dir / "id").symlink_to(shutil.which("id"))
        servers, expected = [], []
        for suffix, listening in (("0000000000000001", False), ("0000000000000002", True)):
            folder = system_temp / f"cairnmind-ui-{os.getuid()}-{suffix}"
            folder.mkdir()
            server = socket.socket(socket.AF_UNIX)
            server.bind(str(folder / "tmux.sock"))
            if listening:
                server.listen()
                expected.append(str(folder / "tmux.sock"))
            servers.append(server)

        def live(python):
            return subprocess.run([BASH, "-c", "set -euo pipefail\n" + functions + f'\nlive_terminals "{python}"'],
                                  env={"PATH": str(bin_dir), "TMPDIR": str(system_temp)},
                                  capture_output=True, text=True, timeout=30)
        result = live(sys.executable)
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == expected
        # A Python that is not there, or none: as before, a socket nobody can ask counts as live.
        assert len(live(tmp_path / "no-python").stdout.splitlines()) == 2
        for server in servers:
            server.close()
    finally:
        shutil.rmtree(system_temp, ignore_errors=True)


def test_the_refusal_to_replace_a_running_workspace_shows_a_count_and_five_paths(tmp_path):
    import socket
    import tempfile
    script = SCRIPT.read_text()
    start = script.index("# --- what an installed version left running")
    system_temp = Path(tempfile.mkdtemp(prefix="cm-", dir="/tmp"))
    try:
        functions = script[start:script.index("# --- end ---", start)]
        bin_dir = tmp_path / "bin"  # a tmux that finds every server alive, and an installed command with no workspace
        tool(bin_dir / "tmux", "exit 0")
        tool(bin_dir / "cairnmind", 'if [ "$1 $2" = "workspace status" ]; then echo \'{"available": false}\'; fi')
        servers = []
        for number in range(7):
            folder = system_temp / f"cairnmind-native-{os.getuid()}-{number:016d}"
            folder.mkdir()
            server = socket.socket(socket.AF_UNIX)
            server.bind(str(folder / "tmux.sock"))
            servers.append(server)
        # The third argument as the installer passes it: without it the MCP server note (#298) comes first.
        python = tmp_path / "tools" / "cairnmind" / "bin" / "python"  # no process runs it: no MCP server is open
        result = subprocess.run(
            [BASH, "-c", "set -euo pipefail\n" + functions +
             f'\nstop_installed "{bin_dir}/cairnmind" cairnmind "{python}"'],
            env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "TMPDIR": str(system_temp)},
            capture_output=True, text=True, timeout=30)
        for server in servers:
            server.close()
        assert result.returncode == 1
        lines = result.stderr.splitlines()
        assert lines[0] == "A workspace or coding sessions of the installed version are running (7):"
        assert [line.endswith("tmux.sock") for line in lines[1:6]] == [True] * 5 and lines[6] == "and 2 more."
        assert "cairnmind workspace stop --clients" in lines[7]
    finally:
        shutil.rmtree(system_temp, ignore_errors=True)


def test_the_run_and_service_checks_and_a_failed_stop_run_on_the_bash_of_macos(tmp_path):
    script = SCRIPT.read_text()
    start = script.index("# --- what an installed version left running")
    functions = script[start:script.index("# --- end ---", start)]
    tool(tmp_path / "bin" / "ps", "[ \"$*\" = '-A -ww -o pid= -o args=' ] || exit 64\n"
         "echo '  601 /u/tools/cairnmind/bin/python /u/bin/cairnmind run demo claude'\n"
         "echo '  602 /u/tools/cairnmind/bin/python /u/bin/cairnmind check run unit'\n"
         "echo '  603 /u/tools/cairnmind/bin/python -m cairnmind --data-dir /d serve'\n"
         "echo '  604 /u/tools/cairnmind/bin/python -m cairnmind --data-dir /d cockpit serve'")
    failing = tool(tmp_path / "cairnmind", "echo 'first' >&2; echo 'Error: refused' >&2; exit 2")
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "HOME": str(tmp_path)}
    result = subprocess.run(
        [BASH, "-c", "set -euo pipefail\n" + functions + "\nlive_runs /u/tools/cairnmind/bin/python /u/bin/cairnmind\n"
         "live_services /u/tools/cairnmind/bin/python cairnmind\n"
         f"stop_or_say {failing} cairnmind 'the knowledge service' stop || echo \"returned $?\"\necho went on"],
        env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["601", "603", "returned 1", "went on"]
    assert "Note: cairnmind stop failed (it said: Error: refused)" in result.stderr and "first" not in result.stderr


@pytest.mark.parametrize("stop_workspace", [{}, {"CAIRNMIND_INSTALL_STOP_WORKSPACE": "1"}])
def test_a_socket_without_tmux_is_named_with_both_ways_out_and_nothing_is_removed(tmp_path, stop_workspace):
    """#290: `workspace stop --clients` cannot reach a server without a tmux client, so it is not the advice.
    #307: asking the installer to close the workspace does not change that: it could close nothing."""
    import socket
    import tempfile
    script = SCRIPT.read_text()
    start = script.index("# --- what an installed version left running")
    system_temp = Path(tempfile.mkdtemp(prefix="cm-", dir="/tmp"))
    try:
        functions = script[start:script.index("# --- end ---", start)]
        folder = system_temp / f"cairnmind-ui-{os.getuid()}-0123456789abcdef"
        folder.mkdir()
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(folder / "tmux.sock"))
        bin_dir = tmp_path / "bin"  # `id` only: no tmux on PATH
        bin_dir.mkdir()
        (bin_dir / "id").symlink_to(shutil.which("id"))
        installed = tool(tmp_path / "cairnmind", "exit 1")
        result = subprocess.run([BASH, "-c", f"set -euo pipefail\n{functions}\nstop_installed {installed} cairnmind"],
                                env={"PATH": str(bin_dir), "TMPDIR": str(system_temp), **stop_workspace},
                                capture_output=True, text=True, timeout=30)
        server.close()
        assert result.returncode == 1
        assert str(folder / "tmux.sock") in result.stderr
        assert "tmux is not on the PATH here" in result.stderr and "Install tmux" in result.stderr
        assert "could not be checked or closed" in result.stderr
        assert "remove each socket file" in result.stderr and "workspace stop" not in result.stderr
        assert "Closing" not in result.stdout and (folder / "tmux.sock").exists()
    finally:
        shutil.rmtree(system_temp, ignore_errors=True)


@pytest.mark.parametrize("arguments", ["", " /u/tools/cairnmind/bin/python"])
def test_stop_installed_says_it_could_not_look_for_mcp_servers_without_a_python_or_a_ps(tmp_path, arguments):
    """No third argument, or no ps on the PATH: one note, never a claim that none is open, and the stop goes on (#298)."""
    script = SCRIPT.read_text()
    start = script.index("# --- what an installed version left running")
    functions = script[start:script.index("# --- end ---", start)]
    bin_dir = tmp_path / "bin"  # `id` only: no tmux, no ps
    bin_dir.mkdir()
    (bin_dir / "id").symlink_to(shutil.which("id"))
    installed = tool(tmp_path / "cairnmind", "exit 1")
    result = subprocess.run(
        [BASH, "-c", f"set -euo pipefail\n{functions}\nstop_installed {installed} cairnmind{arguments}\necho went on"],
        env={"PATH": str(bin_dir), "TMPDIR": str(tmp_path / "none")}, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0 and "went on" in result.stdout, result.stderr
    assert "could not be ruled out" in result.stderr and "unbound" not in result.stderr


# --- Linux: what it printed before stays as it was -----------------------------------------------------------------

@pytest.mark.parametrize("release,short,line", [
    ("5.15.167.4-microsoft-standard-WSL2", "Detected Linux on WSL.",
     "Detected Linux on WSL. Install the NVIDIA driver in Windows, not in Linux."),
    ("4.4.0-19041-Microsoft", "Detected Linux on WSL.",
     "Detected Linux on WSL. Install the NVIDIA driver in Windows, not in Linux."),
    ("6.8.0-48-generic", "Detected Linux outside WSL.",
     "Detected Linux outside WSL. The package is supported; check local GPU access.")])
def test_linux_output_is_short_and_has_the_old_sentences_with_pull_models(mac, release, short, line):
    # #287: the platform line carries GPU advice only on the path that downloads models.
    run, state, bin_dir, tmp_path = mac
    state.update(system="Linux", release=release)
    java(bin_dir / "java", 21)
    for result, expected in ((run(), short), (run(pull_models=True), line)):
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [expected, "passed JAVA_HOME="]
        assert result.stderr == ""


def test_linux_keeps_its_apt_hints(mac):
    run, state, bin_dir, tmp_path = mac
    state.update(system="Linux")
    result = run()
    assert result.returncode == 1
    assert result.stderr == "Java not found. Install Java 21+: sudo apt install openjdk-21-jre-headless\n"
    java(bin_dir / "java", 17)
    result = run()
    assert result.stderr == "Found Java 17. Java 21+ is required: sudo apt install openjdk-21-jre-headless\n"


def test_the_installer_runs_the_engine_in_docker_when_java_is_missing_and_docker_answers(mac):
    """Without Java 21 the installer goes on when Docker answers (#272), says so, and still names the Java problem."""
    run, state, bin_dir, tmp_path = mac
    state.update(system="Linux")
    hint = "sudo apt install openjdk-21-jre-headless"
    tool(bin_dir / "docker", 'case "$1" in version) echo 27.3.1 ;; *) exit 9 ;; esac')
    result = run(show_flag=True)
    assert result.returncode == 0, result.stderr
    # It promises nothing `init` may not keep (an installation that holds a native engine stays native).
    assert "so cairnmind init can run the engine in Docker; it says which mode it chose." in result.stdout
    assert "Java is not needed" not in result.stdout and "will run in Docker" not in result.stdout
    assert result.stderr == f"Java not found. Install Java 21+: {hint}\n" and "passed" in result.stdout
    assert "engine_in_docker=true" in result.stdout  # the flag the install branch below is chosen by
    java(bin_dir / "java", 17)
    result = run(show_flag=True)
    assert result.returncode == 0 and result.stderr == f"Found Java 17. Java 21+ is required: {hint}\n"
    assert "engine_in_docker=true" in result.stdout
    java(bin_dir / "java", 21)  # Java 21 is used when there is one: no word of Docker
    result = run(show_flag=True)
    assert result.returncode == 0 and "Docker" not in result.stdout + result.stderr
    assert "engine_in_docker=false" in result.stdout
    # A Docker that does not answer is not an engine: the Java message stays and names both ways (#285).
    (bin_dir / "java").unlink()
    # Like docker/cli: an empty line on stdout, then the error on stderr. Like the Docker Desktop stub in WSL: a bare echo first.
    tool(bin_dir / "docker", "echo; echo 'Cannot connect to the Docker daemon' >&2; echo second line >&2; exit 1")
    result = run()
    assert result.returncode == 1 and "Docker" not in result.stdout and "passed" not in result.stdout
    assert result.stderr == (f"Java not found. Install Java 21+: {hint}\n"
                             "Docker is on the path but did not answer (it said: Cannot connect to the Docker daemon). "
                             f"Either start Docker and run the installer again, so the engine runs in Docker, "
                             f"or install Java 21+: {hint}\n")
    tool(bin_dir / "docker", "echo; echo 'The command docker could not be found in this WSL 2 distro.'; exit 1")
    result = run()
    assert "(it said: The command docker could not be found in this WSL 2 distro.)" in result.stderr
    # No docker on the path: exactly the old message.
    (bin_dir / "docker").unlink()
    result = run()
    assert result.returncode == 1 and result.stderr == f"Java not found. Install Java 21+: {hint}\n"
    # With Docker, the install branch runs init and does not download an engine.
    script = SCRIPT.read_text()
    branch = script.index('elif [[ "$engine_in_docker" == true || "$(stored_engine_mode)" == docker ]]; then')
    body = script[branch:script.index("\nelse\n", branch)]
    assert '"$cairnmind_bin" init' in body and "engine install" not in body


def test_a_docker_that_does_not_answer_does_not_hold_the_installer(mac):
    """#290: the question to Docker is bounded in the shell (no timeout command on macOS); bash 3.2 runs it too."""
    run, state, bin_dir, tmp_path = mac
    state.update(system="Linux")
    hint = "sudo apt install openjdk-21-jre-headless"
    tool(bin_dir / "docker", "trap '' TERM; exec sleep 60")  # ignores SIGTERM: only SIGKILL ends it
    started = time.monotonic()
    result = run(TMPDIR=str(tmp_path))
    assert time.monotonic() - started < 15
    assert result.returncode == 1 and "passed" not in result.stdout
    assert result.stderr == (f"Java not found. Install Java 21+: {hint}\n"
                             "Docker is on the path but did not answer (it said: no answer within 5 seconds). "
                             f"Either start Docker and run the installer again, so the engine runs in Docker, "
                             f"or install Java 21+: {hint}\n")
    assert list(tmp_path.glob("cairnmind-docker.*")) == []  # the answer file is removed too


def test_a_stored_docker_mode_is_read_from_the_installed_version(tmp_path, monkeypatch):
    """#290: with engine_mode docker stored the installer takes the Docker branch, so no engine archive is downloaded."""
    script = SCRIPT.read_text()
    start = script.index("stored_engine_mode() {")
    function = script[start:script.index("\n}\n", start) + 3]
    tool_dir = tmp_path / "uvtool"
    python = tool(tool_dir / "cairnmind" / "bin" / "python", f'exec "{sys.executable}" "$@"')
    uv = tool(tmp_path / "bin" / "uv", f'echo "{tool_dir}"')

    def stored(config: str | None) -> str:
        home = tmp_path / "home"
        shutil.rmtree(home, ignore_errors=True)
        home.mkdir()
        if config is not None:
            (home / "config.json").write_text(config)
        result = subprocess.run(
            [BASH, "-c", "set -euo pipefail\n" + function + '\nstored_engine_mode'],
            env={**os.environ, "PATH": f"{uv.parent}:{os.environ['PATH']}", "CAIRNMIND_HOME": str(home)},
            capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    assert python.exists()
    assert stored('{"engine_mode": "docker"}') == "docker"
    assert stored('{"engine_mode": "native"}') == "native"
    assert stored(None) == "native"  # no stored mode: as before
    assert stored('{"engine_mode": "banana"}') == ""  # unreadable: as before, init names the problem


def test_on_linux_java_home_does_not_replace_the_java_on_path(mac):
    """On Linux the engine runs java on PATH whatever JAVA_HOME says, so that is the one the installer checks."""
    run, state, bin_dir, tmp_path = mac
    state.update(system="Linux")
    java(bin_dir / "java", 21)
    result = run(JAVA_HOME=str(jdk(tmp_path / "jdk17", 17)))
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_a_java_that_does_not_run_is_said_not_left_silent(mac):
    run, state, bin_dir, tmp_path = mac
    state.update(system="Linux")
    tool(bin_dir / "java", "exit 1")
    result = run()
    assert result.returncode == 1 and "did not run" in result.stderr


def test_other_systems_are_refused(mac):
    run, state, bin_dir, tmp_path = mac
    state.update(system="FreeBSD")
    result = run()
    assert result.returncode == 1 and "or macOS." in result.stderr


def test_the_macos_guide_says_what_the_script_does():
    guide, script = (ROOT / "docs" / "INSTALL.md").read_text(), SCRIPT.read_text()
    section = guide[guide.index("### macOS"):guide.index("## 3. Get Cairnmind")]
    assert "brew install openjdk@21 tmux uv ollama" in section
    # The link of the guide is the one the script prints for Homebrew's openjdk@21.
    assert ('sudo ln -sfn "$(brew --prefix openjdk@21)/libexec/openjdk.jdk" '
            '/Library/Java/JavaVirtualMachines/openjdk-21.jdk') in section
    assert 'brew --prefix openjdk@21 2>/dev/null)/libexec/openjdk.jdk"' in script
    assert "sudo ln -sfn \"%s\" /Library/Java/JavaVirtualMachines/openjdk-21.jdk" in script
    assert "~/.zprofile" not in section
    for hint in ("brew install openjdk@21", "brew install tmux", "brew install uv", "/usr/libexec/java_home -v 21+"):
        assert hint in script
    assert "/usr/libexec/java_home -v 21+" in guide
    # Every message the troubleshooting table quotes is one the script prints.
    table = guide[guide.index("## Troubleshooting"):]
    for message in ("Java not found", "macOS answers so when no JDK is installed", "Version 3.4+ is required",
                    "Java 21+ is required", "Docker is on the path but did not answer"):
        assert message in table and message in script, message


# --- bash 3.2 ------------------------------------------------------------------------------------------------------

# What bash 3.2, the bash of macOS, does not have: case changes, associative arrays and namerefs, mapfile and
# readarray, appending redirections of both streams, |&, case fall-through, coproc, ${x@Q}, negative indexes,
# globstar, read -t with a fraction, stepped ranges and the EPOCH variables.
BASH4_ONLY = [r"\$\{[^}]*(,,|\^\^|,|\^)\}", r"declare\s+-[a-zA-Z]*[An]", r"local\s+-[a-zA-Z]*n", r"\bmapfile\b",
              r"\breadarray\b", r"&>>", r"\|&", r";;&", r";&", r"\bcoproc\b", r"\$\{[^}]*@[QEPAa]\}",
              r"\$\{[a-zA-Z_]+\[-[0-9]+\]\}", r"\bglobstar\b", r"read\s+[^;\n]*-t\s*[0-9]*\.[0-9]",
              r"\{[0-9]+\.\.[0-9]+\.\.[0-9]+\}", r"\bEPOCH(SECONDS|REALTIME)\b", r"\bwait\s+-n\b"]


def test_the_installer_uses_nothing_that_bash_3_2_lacks():
    code = "\n".join(line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#"))
    found = [(pattern, match.group(0)) for pattern in BASH4_ONLY for match in re.finditer(pattern, code)]
    assert found == []
    subprocess.run([BASH, "-n", str(SCRIPT)], check=True)


@pytest.mark.skipif(sys.platform != "darwin", reason="only macOS has the bash 3.2 of macOS")
def test_on_macos_the_checks_ran_under_bash_3_2():
    version = subprocess.run([BASH, "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True).stdout
    assert version.strip() == "3"
