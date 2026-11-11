#!/usr/bin/env bash
# Install this checkout as an isolated uv tool. Model and Copilot runtime downloads are opt-in.
set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: bash scripts/install-wsl.sh [--pull-models]

Installs Cairnmind as a uv tool together with ArcadeDB 26.11.1 (about 86 MB,
archive verified with SHA-256), starts the engine and initialises local memory.
Requires Java 21+ and tmux 3.4+. Does not change Codex or Claude Code configuration.
Does not download models or the Copilot runtime by default; prepare Copilot in the terminal UI.

  --pull-models  Download both models after installation (several GB; Ollama must be running).
  --help         Show this help.
USAGE
}

# SDK runtime is prepared explicitly from TUI, never as an installation side effect.
export COPILOT_SKIP_CLI_DOWNLOAD=1

pull_models=false
for argument in "$@"; do
    case "$argument" in
        --pull-models) pull_models=true ;;
        --help|-h) usage; exit 0 ;;
        *) printf 'Unknown argument: %s\n' "$argument" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ "$(uname -s)" != Linux ]]; then
    printf 'Run the installer in Ubuntu on WSL2 or another Linux system.\n' >&2
    exit 1
fi

if ! command -v java >/dev/null 2>&1; then
    printf 'Java not found. Install Java 21+: sudo apt install openjdk-21-jre-headless\n' >&2
    exit 1
fi
java_version_output="$(java -version 2>&1)"
java_version_pattern='version "([0-9]+)'
if [[ ! "$java_version_output" =~ $java_version_pattern ]]; then
    printf 'Cannot detect the Java version. Check java -version; Java 21+ is required.\n' >&2
    exit 1
fi
java_major="${BASH_REMATCH[1]}"
if (( java_major < 21 )); then
    printf 'Found Java %s. Java 21+ is required: sudo apt install openjdk-21-jre-headless\n' "$java_major" >&2
    exit 1
fi

if ! command -v tmux >/dev/null 2>&1; then
    printf 'tmux not found. Install it: sudo apt install tmux (version 3.4+ required).\n' >&2
    exit 1
fi
tmux_version_output="$(tmux -V)"
tmux_version_pattern='tmux ([0-9]+)\.([0-9]+)'
if [[ ! "$tmux_version_output" =~ $tmux_version_pattern ]]; then
    printf 'Cannot detect the tmux version; 3.4+ is required.\n' >&2
    exit 1
fi
tmux_major="${BASH_REMATCH[1]}"
tmux_minor="${BASH_REMATCH[2]}"
if (( tmux_major < 3 || (tmux_major == 3 && tmux_minor < 4) )); then
    printf 'Found tmux %s.%s. Version 3.4+ is required: sudo apt install tmux\n' "$tmux_major" "$tmux_minor" >&2
    exit 1
fi

kernel_release="$(uname -r)"
if [[ "${kernel_release,,}" == *microsoft* ]]; then
    printf 'Detected Linux on WSL. Install the NVIDIA driver in Windows, not in Linux.\n'
else
    printf 'Detected Linux outside WSL. The package is supported; check local GPU access.\n'
fi

if ! command -v uv >/dev/null 2>&1; then
    printf 'uv not found on PATH. Install uv: https://docs.astral.sh/uv/getting-started/installation/\n' >&2
    printf 'See docs/INSTALL.md for the full installation guide.\n' >&2
    exit 1
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
package_dir="$(cd -- "$script_dir/.." && pwd)"
if [[ ! -f "$package_dir/pyproject.toml" || ! -f "$package_dir/uv.lock" ]]; then
    printf 'pyproject.toml or uv.lock is missing next to scripts/. Copy the whole project.\n' >&2
    exit 1
fi

constraints_file="$(mktemp "${TMPDIR:-/tmp}/cairnmind-constraints.XXXXXXXX")"
trap 'rm -f -- "$constraints_file"' EXIT

printf 'Preparing dependencies from uv.lock...\n'
uv export --directory "$package_dir" --locked --no-dev --no-emit-project \
    --format requirements-txt --output-file "$constraints_file" >/dev/null

# --- what an installed version left running (kept between these two lines for the tests) ---
# Terminal servers of an installed version, whatever data directory they belong to: the workspace
# and the coding sessions started from it, under the current and the earlier names. A later
# version does not find what an earlier one started, so nothing of it may be left behind unseen.
live_terminals() {
    local socket
    command -v tmux >/dev/null 2>&1 || return 0
    for socket in "${TMPDIR:-/tmp}"/{cairnmind,devbrain}-{ui,native}-"$(id -u)"-*/tmux.sock; do
        if [[ -S "$socket" && -O "$socket" ]] && tmux -S "$socket" list-sessions >/dev/null 2>&1; then
            printf '%s\n' "$socket"
        fi
    done
}

# $1: the executable of the installed version, $2: the name a user types for it.
# The workspace and coding sessions hold interactive work: the installer stops here unless it is
# told to close them. The interface broker and the knowledge service hold none and are stopped.
stop_installed() {
    local installed="$1" name="$2" terminals status socket
    [[ -x "$installed" ]] || return 0
    terminals="$(live_terminals)"
    # Asked as well, read whole and not through a pipe: an answer cut short must not read as "none".
    if status="$("$installed" workspace status 2>/dev/null)"; then
        if [[ "$status" == *'"available": true'* && -z "$terminals" ]]; then
            terminals="the workspace reported by $name workspace status"
        fi
    elif [[ -z "$terminals" ]]; then
        printf 'Note: %s workspace status failed, so a running workspace could not be ruled out.\n' "$name" >&2
    fi
    if [[ -n "$terminals" ]]; then
        if [[ "${CAIRNMIND_INSTALL_STOP_WORKSPACE:-}" != 1 ]]; then
            printf '%s\n' \
                'A workspace or coding sessions of the installed version are running:' \
                "$terminals" \
                "Finish there and close them with: $name workspace stop --clients" \
                'then run this installer again. The new version would not find them.' \
                'To let the installer close them, set CAIRNMIND_INSTALL_STOP_WORKSPACE=1.' >&2
            exit 1
        fi
        printf 'Closing the running workspace and coding sessions, as CAIRNMIND_INSTALL_STOP_WORKSPACE=1 asks.\n'
        "$installed" workspace stop --clients >/dev/null 2>&1 || true
        "$installed" cockpit stop --clients >/dev/null 2>&1 || true
        # Whatever belongs to another data directory, or did not answer, is closed at its terminal server.
        while IFS= read -r socket; do
            [[ -n "$socket" ]] && tmux -S "$socket" kill-server >/dev/null 2>&1 || true
        done <<<"$(live_terminals)"
    fi
    "$installed" cockpit stop >/dev/null 2>&1 || true
    "$installed" stop >/dev/null 2>&1 || true
}
# --- end ---

# An earlier release was installed as the uv tool "devbrain", which owns the devbrain
# executable that this package also provides as an alias. Remove the tool, not the data.
upgraded_from_devbrain=false
if uv tool list 2>/dev/null | grep '^devbrain ' >/dev/null; then
    printf 'Removing the earlier DevBrain uv tool; your memory in %s is kept.\n' \
        "${CAIRNMIND_HOME:-${DEVBRAIN_HOME:-$HOME/.local/share/devbrain}}"
    # Stopped while the tool that started them still exists: afterwards nothing could stop them.
    stop_installed "$(uv tool dir --bin)/devbrain" devbrain
    uv tool uninstall devbrain >/dev/null
    upgraded_from_devbrain=true
fi

# An installed Cairnmind is being replaced: the same care, with the installed version's own command.
if uv tool list 2>/dev/null | grep '^cairnmind ' >/dev/null; then
    stop_installed "$(uv tool dir --bin)/cairnmind" cairnmind
fi

printf 'Installing Cairnmind from %s...\n' "$package_dir"
uv tool install --python "${CAIRNMIND_INSTALL_PYTHON:-${DEVBRAIN_INSTALL_PYTHON:-3.12}}" --reinstall --constraints "$constraints_file" "$package_dir"

tool_bin_dir="$(uv tool dir --bin)"
cairnmind_bin="$tool_bin_dir/cairnmind"
if [[ ! -x "$cairnmind_bin" ]]; then
    printf 'Installation did not provide the executable: %s\n' "$cairnmind_bin" >&2
    exit 1
fi

printf '\nInstalling the pinned ArcadeDB release (about 86 MB, verified with SHA-256).\n'
arcade_dist="${CAIRNMIND_ARCADE_DIST:-${DEVBRAIN_ARCADE_DIST:-}}"
if [[ -n "$arcade_dist" ]]; then
    "$cairnmind_bin" engine install --distribution "$arcade_dist"
else
    "$cairnmind_bin" engine install
fi
"$cairnmind_bin" init
"$cairnmind_bin" engine status
"$cairnmind_bin" doctor

if [[ "$upgraded_from_devbrain" == true ]]; then
    # Hooks and MCP entries in existing projects name the removed interpreter.
    printf '\nProjects set up by DevBrain still point at its removed Python. Reconnect each one:\n'
    "$cairnmind_bin" project list | "$(uv tool dir)/cairnmind/bin/python" -I -c '
import json, shlex, sys
projects = json.load(sys.stdin)
for project in projects:
    print("  cairnmind install " + shlex.quote(project["name"]))
if not projects:
    print("  (no registered projects)")
'
fi

if [[ "$pull_models" == true ]]; then
    if ! "$cairnmind_bin" models list >/dev/null; then
        printf 'Cannot reach Ollama. Start ollama serve, then run cairnmind models pull.\n' >&2
        exit 1
    fi
    "$cairnmind_bin" models pull
else
    printf '\nModels were not downloaded. With Ollama running, run: cairnmind models pull\n'
fi

printf '\nCairnmind installed: %s\n' "$cairnmind_bin"
case ":$PATH:" in
    *":$tool_bin_dir:"*) ;;
    *) printf 'Add this directory to PATH: %s\n' "$tool_bin_dir" ;;
esac
printf 'Open the terminal workspace: cairnmind\n'
printf 'Projects, API/Ollama/Copilot profiles, client panes, memory and the graph are chosen in the terminal.\n'
printf 'Copilot: Connections -> Connect GitHub account; the installer did not download its runtime.\n'
printf 'Status without opening the workspace: cairnmind workspace status\n'
