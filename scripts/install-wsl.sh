#!/usr/bin/env bash
# Install this checkout as an isolated uv tool. Model and Copilot runtime downloads are opt-in.
set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: bash scripts/install-wsl.sh [--pull-models]

Installs Cairnmind as a uv tool together with ArcadeDB 26.11.1 (about 86 MB,
archive verified with SHA-256), starts the engine and initialises local memory.
Requires Java 21+ and tmux 3.4+. Does not change Codex or Claude Code configuration.

With CAIRNMIND_ARCADE_CONNECT_URL set, the installation only connects to a database
that runs elsewhere (docs/DEPLOYMENT.md): no ArcadeDB is downloaded, no engine is
started and Java is not needed. The address is saved in config.json of the data
directory, so later commands need no variable to stay connect-only.
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

# --- the checks before installing (kept between these two lines for the tests) ---
# This file runs on macOS too, whose bash is 3.2: no ${x,,}, associative arrays, mapfile or &>> (#232).
system="$(uname -s)"
tmux_hint='sudo apt install tmux'
uv_hint='https://docs.astral.sh/uv/getting-started/installation/'
case "$system" in
    Linux) ;;
    Darwin)
        tmux_hint='brew install tmux'
        uv_hint='brew install uv'
        # hw.optional.arm64 is 1 on Apple silicon, also in a shell that Rosetta translates; an Intel Mac has none.
        # Intel Macs are refused: no one has run Cairnmind there, and the local models would run on the processor.
        if [[ "$(sysctl -n hw.optional.arm64 2>/dev/null || true)" != 1 ]]; then
            printf 'This Mac has an Intel processor. Cairnmind supports macOS on Apple silicon (M1 or later) only.\n' >&2
            exit 1
        fi
        if [[ "$(uname -m)" != arm64 ]]; then
            printf 'This shell runs under Rosetta (x86_64). Open a native arm64 terminal and run the installer again.\n' >&2
            exit 1
        fi
        ;;
    *)
        printf 'Run the installer in Ubuntu on WSL2, another Linux system or macOS on Apple silicon.\n' >&2
        exit 1
        ;;
esac

# Connect-only: the database runs elsewhere and this installation never starts an engine, so Java is
# needed only for the engine Cairnmind manages itself.
connect_url="${CAIRNMIND_ARCADE_CONNECT_URL:-${DEVBRAIN_ARCADE_CONNECT_URL:-}}"

if [[ -z "$connect_url" ]]; then
    java_hint='sudo apt install openjdk-21-jre-headless'
    if [[ "${system:-}" == Darwin ]]; then
        java_hint='brew install openjdk@21, then link it as docs/INSTALL.md shows for macOS'
    fi

    # $1: what is wrong. macOS finds Homebrew's openjdk@21 only once it is linked into /Library/Java/JavaVirtualMachines
    # (Homebrew's own advice); then every process finds it, also one that a desktop app starts without ~/.zprofile.
    refuse_java() {
        local brew_jdk
        printf '%s\n' "$1" >&2
        if [[ "${system:-}" == Darwin ]] && command -v brew >/dev/null 2>&1 \
            && brew_jdk="$(brew --prefix openjdk@21 2>/dev/null)/libexec/openjdk.jdk" \
            && [[ -x "$brew_jdk/Contents/Home/bin/java" ]]; then
            printf "Homebrew's openjdk@21 is installed, but macOS does not find it. Link it, then run the installer again:\n" >&2
            printf '  sudo ln -sfn "%s" /Library/Java/JavaVirtualMachines/openjdk-21.jdk\n' "$brew_jdk" >&2
        fi
        exit 1
    }

    # The Java the engine runs (engine.resolve_java): on macOS JAVA_HOME's, then the one /usr/libexec/java_home finds
    # (/usr/bin/java starts the newest of the same JDKs, and without any JDK it is a stand-in that only fails), then
    # java on PATH. On Linux only java on PATH.
    java_bin=""
    java_source=""
    if [[ -n "${JAVA_HOME:-}" && "${system:-}" == Darwin ]]; then
        if [[ "$JAVA_HOME" == /* && -f "$JAVA_HOME/bin/java" && -x "$JAVA_HOME/bin/java" ]]; then
            java_bin="$JAVA_HOME/bin/java"
            java_source=" (JAVA_HOME=$JAVA_HOME)"
        else
            printf 'JAVA_HOME is set to %s, which has no bin/java; it is ignored.\n' "$JAVA_HOME" >&2
        fi
    fi
    if [[ -z "$java_bin" && "${system:-}" == Darwin ]] \
        && java_home_found="$(/usr/libexec/java_home -v 21+ 2>/dev/null)" && [[ -x "$java_home_found/bin/java" ]]; then
        java_bin="$java_home_found/bin/java"
        java_source=" ($java_home_found)"
        # The rest of this installation runs exactly the Java checked here.
        export JAVA_HOME="$java_home_found"
    fi
    if [[ -z "$java_bin" ]] && ! java_bin="$(command -v java)"; then
        refuse_java "Java not found. Install Java 21+: $java_hint"
    fi
    if ! java_version_output="$("$java_bin" -version 2>&1)"; then
        if [[ "${system:-}" == Darwin ]]; then
            refuse_java "$java_bin did not run: macOS answers so when no JDK is installed. Install Java 21+: $java_hint"
        fi
        refuse_java "$java_bin -version did not run. Java 21+ is required: $java_hint"
    fi
    java_version_pattern='version "([0-9]+)'
    if [[ ! "$java_version_output" =~ $java_version_pattern ]]; then
        refuse_java 'Cannot detect the Java version. Check java -version; Java 21+ is required.'
    fi
    java_major="${BASH_REMATCH[1]}"
    if (( java_major < 21 )); then
        refuse_java "Found Java $java_major$java_source. Java 21+ is required: $java_hint"
    fi
fi

if ! command -v tmux >/dev/null 2>&1; then
    printf 'tmux not found. Install it: %s (version 3.4+ required).\n' "$tmux_hint" >&2
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
    printf 'Found tmux %s.%s. Version 3.4+ is required: %s\n' "$tmux_major" "$tmux_minor" "$tmux_hint" >&2
    exit 1
fi

if [[ "$system" == Darwin ]]; then
    printf 'Detected macOS on Apple silicon. Ollama runs the models on the built-in GPU.\n'
elif [[ "$(uname -r | tr '[:upper:]' '[:lower:]')" == *microsoft* ]]; then
    printf 'Detected Linux on WSL. Install the NVIDIA driver in Windows, not in Linux.\n'
else
    printf 'Detected Linux outside WSL. The package is supported; check local GPU access.\n'
fi

if ! command -v uv >/dev/null 2>&1; then
    printf 'uv not found on PATH. Install uv: %s\n' "$uv_hint" >&2
    printf 'See docs/INSTALL.md for the full installation guide.\n' >&2
    exit 1
fi
# --- end of the checks ---

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
    # On macOS they live in one folder per user under /private/tmp (runtime_sockets.py, #235).
    for socket in "${TMPDIR:-/tmp}"/{cairnmind,devbrain}-{ui,native}-"$(id -u)"-*/tmux.sock \
        /private/tmp/cairnmind-"$(id -u)"/{ui,native}-*/tmux.sock; do
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

if [[ -n "$connect_url" ]]; then
    # Shown without a user, a password, a query or a fragment, should the address carry one by mistake.
    shown_url="$(printf '%s' "$connect_url" | sed -E 's#^([a-zA-Z]+://)[^/@]*@#\1#; s#[?\#].*$##')"
    printf '\nThis installation only connects to the database at %s; no engine is installed.\n' "$shown_url"
    # The first command below saves the address in config.json (it says so): a later command started
    # without the variable stays connect-only instead of starting a local engine.
    # The database and its user are created once, by a person (cairnmind db create-user): until then
    # these two commands say what is missing, and the installation itself is complete.
    if ! "$cairnmind_bin" engine status || ! "$cairnmind_bin" init; then
        printf '\nThe database is not ready for this installation yet. Follow docs/DEPLOYMENT.md,\n' >&2
        printf '"Connect-only installations", then run: cairnmind init\n' >&2
    fi
else
    printf '\nInstalling the pinned ArcadeDB release (about 86 MB, verified with SHA-256).\n'
    arcade_dist="${CAIRNMIND_ARCADE_DIST:-${DEVBRAIN_ARCADE_DIST:-}}"
    if [[ -n "$arcade_dist" ]]; then
        "$cairnmind_bin" engine install --distribution "$arcade_dist"
    else
        "$cairnmind_bin" engine install
    fi
    "$cairnmind_bin" init
    "$cairnmind_bin" engine status
fi
# Only what needs attention is printed; `cairnmind doctor` shows the full report (#244).
printf '\nChecking the installation...\n'
"$cairnmind_bin" doctor | "$(uv tool dir)/cairnmind/bin/python" -I -m cairnmind.doctor_attention

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
