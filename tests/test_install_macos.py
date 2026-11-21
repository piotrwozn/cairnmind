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
    os.symlink(shutil.which("tr"), bin_dir / "tr")

    def run(show_flag=False, **environment):
        write()
        text = checks().replace(JAVA_HOME_TOOL, str(tmp_path / "java_home"))
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


def test_the_installer_ends_with_the_workspace_line_only_when_tmux_is_ready():
    script = SCRIPT.read_text()
    ready = script.index('if [[ "$workspace_ready" == true ]]; then\n    printf \'Open the terminal workspace: cairnmind\\n\'')
    ending = script[ready:]
    assert ending.index("\nelse\n") < ending.index("without it these commands work: cairnmind serve, cairnmind run, "
                                                   "cairnmind mcp, cairnmind brief.")
    assert ending.count("Open the terminal workspace") == 1


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


# --- Linux: what it printed before stays as it was -----------------------------------------------------------------

@pytest.mark.parametrize("release,line", [
    ("5.15.167.4-microsoft-standard-WSL2", "Detected Linux on WSL. Install the NVIDIA driver in Windows, not in Linux."),
    ("4.4.0-19041-Microsoft", "Detected Linux on WSL. Install the NVIDIA driver in Windows, not in Linux."),
    ("6.8.0-48-generic", "Detected Linux outside WSL. The package is supported; check local GPU access.")])
def test_linux_output_is_unchanged(mac, release, line):
    run, state, bin_dir, tmp_path = mac
    state.update(system="Linux", release=release)
    java(bin_dir / "java", 21)
    result = run()
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [line, "passed JAVA_HOME="]
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
    # A Docker that does not answer changes nothing: the Java message is the end.
    (bin_dir / "java").unlink()
    tool(bin_dir / "docker", "echo 'Cannot connect to the Docker daemon' >&2; exit 1")
    result = run()
    assert result.returncode == 1 and "Docker" not in result.stdout and "passed" not in result.stdout
    assert result.stderr == f"Java not found. Install Java 21+: {hint}\n"
    # With Docker, the install branch runs init and does not download an engine.
    script = SCRIPT.read_text()
    branch = script.index('elif [[ "$engine_in_docker" == true ]]; then')
    body = script[branch:script.index("\nelse\n", branch)]
    assert '"$cairnmind_bin" init' in body and "engine install" not in body


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
                    "Java 21+ is required"):
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
