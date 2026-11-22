#!/usr/bin/env bash
# Install this checkout as an isolated uv tool. Model and Copilot runtime downloads are opt-in.
set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: bash scripts/install-wsl.sh [--pull-models]

Installs Cairnmind as a uv tool together with ArcadeDB 26.11.1 (about 86 MB,
archive verified with SHA-256), starts the engine and initialises local memory.
Requires Java 21+ (or Docker, which then runs the engine). tmux 3.4+ is needed only for the terminal workspace. Does not change Codex or Claude Code configuration.

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
uv_hint='curl -LsSf https://astral.sh/uv/install.sh | sh'
case "$system" in
    Linux) ;;
    Darwin)
        tmux_hint='brew install tmux'
        uv_hint='brew install uv'
        ;;
    *)
        printf 'Run the installer in Ubuntu on WSL2, another Linux system or macOS.\n' >&2
        exit 1
        ;;
esac

# Connect-only: the database runs elsewhere and this installation never starts an engine, so Java is
# needed only for the engine Cairnmind manages itself.
connect_url="${CAIRNMIND_ARCADE_CONNECT_URL:-${DEVBRAIN_ARCADE_CONNECT_URL:-}}"
engine_in_docker=false

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
        return 1
    }

    # The Java the engine runs (engine.resolve_java): on macOS JAVA_HOME's, then the one /usr/libexec/java_home finds
    # (/usr/bin/java starts the newest of the same JDKs, and without any JDK it is a stand-in that only fails), then
    # java on PATH. On Linux only java on PATH. Returns 1 after saying what is wrong.
    check_java() {
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
            refuse_java "Java not found. Install Java 21+: $java_hint" || return 1
        fi
        if ! java_version_output="$("$java_bin" -version 2>&1)"; then
            if [[ "${system:-}" == Darwin ]]; then
                refuse_java "$java_bin did not run: macOS answers so when no JDK is installed. Install Java 21+: $java_hint" || return 1
            fi
            refuse_java "$java_bin -version did not run. Java 21+ is required: $java_hint" || return 1
        fi
        java_version_pattern='version "([0-9]+)'
        if [[ ! "$java_version_output" =~ $java_version_pattern ]]; then
            refuse_java 'Cannot detect the Java version. Check java -version; Java 21+ is required.' || return 1
        fi
        java_major="${BASH_REMATCH[1]}"
        if (( java_major < 21 )); then
            refuse_java "Found Java $java_major$java_source. Java 21+ is required: $java_hint" || return 1
        fi
    }

    # Without Java 21 the engine can run in Docker (#272): said once, and the installer goes on. Without a Docker
    # that answers, the Java message above is the end.
    # Docker's answer in $docker_said; 1 when it fails or has not answered in 5 seconds. Bounded in the shell itself:
    # macOS has no timeout command. shortcut: a docker that starts children of its own may leave them behind.
    docker_asked() {
        local file pid waited=0 status=0
        file="$(mktemp "${TMPDIR:-/tmp}/cairnmind-docker.XXXXXXXX")"
        docker version --format '{{.Server.Version}}' >"$file" 2>&1 &
        pid=$!
        while kill -0 "$pid" 2>/dev/null && ((waited < 50)); do
            sleep 0.1
            waited=$((waited + 1))
        done
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            waited=0 # a docker that ignores SIGTERM gets one more second, then SIGKILL: the limit holds
            while kill -0 "$pid" 2>/dev/null && ((waited < 10)); do
                sleep 0.1
                waited=$((waited + 1))
            done
            kill -KILL "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
            docker_said="no answer within 5 seconds"
            status=1
        else
            wait "$pid" 2>/dev/null || status=1
            docker_said="$(<"$file")"
        fi
        rm -f -- "$file"
        return "$status"
    }

    if ! check_java; then
        if ! command -v docker >/dev/null 2>&1; then
            exit 1
        fi
        if docker_asked; then
            printf 'Java 21 was not found and Docker answers, so cairnmind init can run the engine in Docker; it says which mode it chose.\n'
            engine_in_docker=true
        else
            # What docker said is untrusted text: its first non-blank line, printable characters only, cut short.
            docker_said="${docker_said#"${docker_said%%[![:space:]]*}"}" # real docker starts with an empty line
            docker_said="${docker_said%%$'\n'*}"
            docker_said="${docker_said//[^[:print:]]/}"
            docker_said="${docker_said:0:200}"
            printf 'Docker is on the path but did not answer (it said: %s). Either start Docker and run the installer again, so the engine runs in Docker, or install Java 21+: %s\n' "${docker_said:-nothing}" "$java_hint" >&2
            exit 1
        fi
    fi
fi

# tmux 3.4+ is needed only by the terminal workspace (#273): without it the installer says so and goes on.
# Returns 1 after saying what is missing.
check_tmux() {
    local version_output version_pattern='tmux ([0-9]+)\.([0-9]+)' major minor
    if ! command -v tmux >/dev/null 2>&1; then
        printf 'tmux not found, so the terminal workspace is unavailable; the installation goes on. For the workspace, install tmux 3.4+: %s\n' "$tmux_hint" >&2
        return 1
    fi
    version_output="$(tmux -V 2>/dev/null)" || version_output=""
    if [[ ! "$version_output" =~ $version_pattern ]]; then
        printf 'Cannot detect the tmux version, so the terminal workspace may be unavailable (it needs 3.4+); the installation goes on.\n' >&2
        return 1
    fi
    major="${BASH_REMATCH[1]}"
    minor="${BASH_REMATCH[2]}"
    if (( major < 3 || (major == 3 && minor < 4) )); then
        printf 'Found tmux %s.%s. Version 3.4+ is required for the terminal workspace, so it is unavailable; the installation goes on. For the workspace, install tmux 3.4+: %s\n' "$major" "$minor" "$tmux_hint" >&2
        return 1
    fi
}
workspace_ready=true
check_tmux || workspace_ready=false

# GPU advice only with --pull-models: nothing else here uses a GPU (#287). The checks run alone in the tests, under set -u.
if [[ "$system" == Darwin ]]; then
    printf 'Detected macOS.\n'
elif [[ "$(uname -r | tr '[:upper:]' '[:lower:]')" == *microsoft* ]]; then
    if [[ "${pull_models:-false}" == true ]]; then
        printf 'Detected Linux on WSL. Install the NVIDIA driver in Windows, not in Linux.\n'
    else
        printf 'Detected Linux on WSL.\n'
    fi
elif [[ "${pull_models:-false}" == true ]]; then
    printf 'Detected Linux outside WSL. The package is supported; check local GPU access.\n'
else
    printf 'Detected Linux outside WSL.\n'
fi

if ! command -v uv >/dev/null 2>&1; then
    printf 'uv not found on PATH. Install it, open a new terminal and run this installer again: %s\n' "$uv_hint" >&2
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
# $1 (optional): the installed version's Python, which can ask a socket when there is no tmux client here.
live_terminals() {
    local socket have_tmux=1 python="${1:-}"
    command -v tmux >/dev/null 2>&1 || have_tmux=0
    # On macOS they live in one folder per user under /private/tmp (runtime_sockets.py, #235).
    for socket in "${TMPDIR:-/tmp}"/{cairnmind,devbrain}-{ui,native}-"$(id -u)"-*/tmux.sock \
        /private/tmp/cairnmind-"$(id -u)"/{ui,native}-*/tmux.sock; do
        [[ -S "$socket" && -O "$socket" ]] || continue
        if ((have_tmux)); then
            tmux -S "$socket" list-sessions >/dev/null 2>&1 || continue
        elif [[ -x "$python" ]]; then
            # A socket nothing listens on is left over from a server that is gone. A connect only asks.
            "$python" -I -c 'import socket, sys; socket.socket(socket.AF_UNIX).connect(sys.argv[1])' "$socket" \
                >/dev/null 2>&1 || continue
        fi
        # Without a tmux client or that Python here a server cannot be asked, so one that owns a socket counts as live.
        printf '%s\n' "$socket"
    done
}

# External MCP servers (cairnmind mcp --client external) running the installed version's Python: an editor keeps
# one open, and its next tool call would start the service again, from the version being replaced (#298).
# $1: that Python, "<uv tool dir>/<tool>/bin/python". Prints their process ids, one per line; fails when ps does.
live_bridges() {
    local python="$1" pid args
    command -v ps >/dev/null 2>&1 || return 1  # not found must not read as "none open", pipefail or not
    ps -A -ww -o pid= -o args= | while read -r pid args; do
        case "$args" in
            "$python"*" mcp "*"--client external"*|"$python"*" mcp "*"--client=external"*) printf '%s\n' "$pid" ;;
        esac
    done
}

# Coding sessions started with `<name> run` of the installed version (#319): the arguments begin with its Python, the
# installed command and `run`, nothing between them, so no other command is taken for one. A session started as
# `<name> --data-dir DIR run` or `python -m cairnmind run` is not seen; docs/UPGRADING.md asks to end those first.
# $1: that Python, $2: the installed command. Prints their process ids, one per line; fails when ps does.
live_runs() {
    local prefix="$1 $2 run " pid args
    command -v ps >/dev/null 2>&1 || return 1
    ps -A -ww -o pid= -o args= | while read -r pid args; do
        case "$args" in
            "$prefix"*) printf '%s\n' "$pid" ;;
        esac
    done
}

# Knowledge services the installed version started by itself, of any data directory (#319): cli.ensure_service runs
# "<its Python> -m <name> --data-dir DIR serve", ending in --defer-background-inference for the workspace.
# $1: that Python, $2: the module, the same as the name, $3 (optional): only the service of that data directory.
# Prints their process ids, one per line; fails when ps does.
live_services() {
    local prefix="$1 -m $2 --data-dir " dir="${3:-}" pid args
    command -v ps >/dev/null 2>&1 || return 1
    ps -A -ww -o pid= -o args= | while read -r pid args; do
        if [[ -n "$dir" ]]; then
            case "$args" in
                "$prefix$dir serve"|"$prefix$dir serve --defer-background-inference") printf '%s\n' "$pid" ;;
            esac
            continue
        fi
        case "$args" in
            "$prefix"*" cockpit serve") ;;  # the interface broker, not a knowledge service
            "$prefix"*" serve"|"$prefix"*" serve --defer-background-inference") printf '%s\n' "$pid" ;;
        esac
    done
}

# Runs the installed command with the arguments after $3 and says so when it fails, with the last line it printed,
# instead of going on in silence (#319); returns 1 then, and the caller decides what follows.
# $1: the installed command, $2: the name a user types for it, $3: what follows from the failure, a whole clause.
stop_or_say() {
    local installed="$1" name="$2" after="$3" said
    shift 3
    if said="$("$installed" "$@" 2>&1 >/dev/null)"; then
        return 0
    fi
    said="${said%"${said##*[![:space:]]}"}"  # without trailing blank lines
    said="${said##*$'\n'}"                    # its last line
    said="${said//[^[:print:]]/}"
    said="${said:0:200}"
    printf 'Note: %s %s failed (it said: %s), so %s\n' "$name" "$*" "${said:-nothing}" "$after" >&2
    return 1
}

# After the new version is installed, a knowledge service whose stop failed is asked to stop again, by the new command
# (it reaches a service of another version, service_version.py): while it still runs, nothing writes the store from
# here on, because the two versions may not agree on what the store allows (#319). The stop's exit code says only that
# a stop was asked or that nothing answered in time, so the process itself is looked for, for up to ten seconds.
# $1: the new command, $2: the Python of the version that was replaced, $3: its name, $4: the data directory.
# Fails when it still runs; the installer then ends, and running it again does the remaining steps.
stop_failed_service() {
    local said="" left="" waited=0
    if said="$("$1" stop 2>&1 >/dev/null)"; then
        said=""
        if [[ -z "$2" || -z "$4" ]]; then
            printf 'Note: the data directory or the Python of the replaced version is not known, so it could not be confirmed that its knowledge service has ended.\n' >&2
            return 0
        fi
        while :; do
            if ! left="$(live_services "$2" "$3" "$4")"; then
                printf 'Note: ps failed, so it could not be confirmed that the knowledge service of the replaced version has ended.\n' >&2
                return 0
            fi
            [[ -n "$left" ]] && ((waited < 100)) || break
            sleep 0.1 2>/dev/null || break
            waited=$((waited + 1))
        done
        [[ -n "$left" ]] || return 0
    else
        said="${said%"${said##*[![:space:]]}"}"
        said="${said##*$'\n'}"
        said="${said//[^[:print:]]/}"
        said="${said:0:200}"
        if [[ -n "$2" && -n "$4" ]]; then
            left="$(live_services "$2" "$3" "$4")" || left=""
        fi
    fi
    left="${left//$'\n'/ }"
    printf '%s\n' \
        "The knowledge service of the version that was replaced still runs${left:+, processes: $left}${said:+ (its stop said: $said)}." \
        'The new version is installed, but the installer stops here: it would write the store beside that service.' \
        "End that service${left:+ (kill $left)}, then run this installer again; it does the remaining steps." >&2
    return 1
}

# While the file upgrade_mark exists, Cairnmind starts no service (upgrade_mark.py); it holds this installer's
# process id. Set where it is used, not where it is defined.
begin_upgrade() {
    upgrade_mark="$HOME/.local/state/cairnmind/upgrade-in-progress"
    if ! { mkdir -p -- "${upgrade_mark%/*}" && printf '%s\n' "$$" >"$upgrade_mark.$$" \
        && mv -f -- "$upgrade_mark.$$" "$upgrade_mark"; }; then
        printf 'Cannot write %s, which tells running Cairnmind processes that an upgrade is in progress.\n' \
            "$upgrade_mark" >&2
        exit 1
    fi
    # A signal ends the installer through exit, so the EXIT trap removes the mark.
    trap 'exit 130' INT
    trap 'exit 143' TERM
    trap 'exit 129' HUP
}

# Removes the mark only when this installer wrote it; safe to call more than once.
end_upgrade() {
    local owner
    upgrade_mark="$HOME/.local/state/cairnmind/upgrade-in-progress"
    rm -f -- "$upgrade_mark.$$"
    owner="$(cat -- "$upgrade_mark" 2>/dev/null)" || return 0
    if [[ "$owner" == "$$" ]]; then
        rm -f -- "$upgrade_mark"
    fi
}

# $1: the executable of the installed version, $2: the name a user types for it,
# $3: its Python (live_bridges, and live_terminals to ask a socket), may be absent.
# The workspace and coding sessions hold interactive work: the installer stops here unless it is
# told to close them. The interface broker and the knowledge service hold none and are stopped.
stop_installed() {
    local installed="$1" name="$2" python="${3:-}" terminals status socket total shown bridges="" runs=""
    # First, and also when the installed command is broken: nothing has been stopped yet. Before the terminals:
    # it is a cheap read that ends the run with nothing asked of the installed command, and a refusal about
    # terminals (#290) then never hides an open MCP server that would restart the service.
    if [[ -z "$python" ]]; then
        printf 'Note: the Python of the installed version is not known, so an open MCP server of it could not be ruled out.\n' >&2
    elif ! bridges="$(live_bridges "$python")"; then
        printf 'Note: ps failed, so an open MCP server of the installed version could not be ruled out.\n' >&2
    elif [[ -n "$bridges" ]]; then
        bridges="${bridges//$'\n'/ }"
        printf '%s\n' \
            "Editors have MCP servers of the installed version open ($name mcp --client external), processes: $bridges" \
            'Their next tool call would start the knowledge service again, from the version being replaced.' \
            "Close them in the editor, or end them with: kill $bridges" \
            'then run this installer again.' >&2
        exit 1
    elif ! runs="$(live_runs "$python" "$installed")"; then
        printf 'Note: ps failed, so an open %s run session of the installed version could not be ruled out.\n' "$name" >&2
    elif [[ -n "$runs" ]]; then
        runs="${runs//$'\n'/ }"
        printf '%s\n' \
            "Coding sessions started with $name run of the installed version are open, processes: $runs" \
            'They run the version being replaced: after the upgrade their MCP server refuses every call until the session starts again.' \
            'Finish there and exit the coding client, then run this installer again.' >&2
        exit 1
    fi
    [[ -x "$installed" ]] || return 0
    terminals="$(live_terminals "$python")"
    # Asked as well, read whole and not through a pipe: an answer cut short must not read as "none".
    if status="$("$installed" workspace status 2>/dev/null)"; then
        if [[ "$status" == *'"available": true'* && -z "$terminals" ]]; then
            terminals="the workspace reported by $name workspace status"
        fi
    elif [[ -z "$terminals" ]]; then
        printf 'Note: %s workspace status failed, so a running workspace could not be ruled out.\n' "$name" >&2
    fi
    if [[ -n "$terminals" ]]; then
        # A count and at most five of them: a machine can hold dozens of leftover sockets (#287).
        # Counted in the shell: a PATH without grep or sed still gets the refusal.
        total=0
        shown=""
        while IFS= read -r socket; do
            total=$((total + 1))
            if ((total > 1 && total <= 5)); then
                shown="$shown"$'\n'
            fi
            if ((total <= 5)); then
                shown="$shown$socket"
            fi
        done <<< "$terminals"
        if ((total > 5)); then
            shown="$shown"$'\n'"and $((total - 5)) more."
        fi
        # Sockets without a tmux client here: nothing can ask or close them, so `workspace stop` is no way out,
        # and neither is the permission to close them (#307).
        if [[ "$terminals" == /* ]] && ! command -v tmux >/dev/null 2>&1; then
            printf '%s\n' \
                'Terminal server sockets of the installed version were found, but tmux is not on the PATH here, so they could not be checked or closed:' \
                "$shown" \
                'Install tmux so they can be checked, or, when no terminal server runs, remove each socket file named above.' \
                'Then run this installer again. The installer removes nothing itself.' >&2
            exit 1
        fi
        if [[ "${CAIRNMIND_INSTALL_STOP_WORKSPACE:-}" != 1 ]]; then
            printf '%s\n' \
                "A workspace or coding sessions of the installed version are running ($total):" \
                "$shown" \
                "Finish there and close them with: $name workspace stop --clients" \
                'then run this installer again. The new version would not find them.' \
                'To let the installer close them, set CAIRNMIND_INSTALL_STOP_WORKSPACE=1.' >&2
            exit 1
        fi
        printf 'Closing the running workspace and coding sessions, as CAIRNMIND_INSTALL_STOP_WORKSPACE=1 asks.\n'
        # A failure is said before the terminal servers are ended below, which is what closes the sessions then.
        stop_or_say "$installed" "$name" 'the workspace and its coding sessions are ended at their terminal servers instead.' \
            workspace stop --clients || true
        stop_or_say "$installed" "$name" 'its coding sessions are ended at their terminal servers instead.' \
            cockpit stop --clients || true
        # Whatever belongs to another data directory, or did not answer, is closed at its terminal server.
        while IFS= read -r socket; do
            [[ -n "$socket" ]] && tmux -S "$socket" kill-server >/dev/null 2>&1 || true
        done <<<"$(live_terminals "$python")"
    fi
    stop_or_say "$installed" "$name" \
        "the interface broker may still run the version being replaced. \`$name cockpit stop\` stops it later." \
        cockpit stop || true
    if ! stop_or_say "$installed" "$name" \
        'the knowledge service may still run the version being replaced. The new version is asked to stop it once it is installed.' \
        stop; then
        service_stop_failed=1 failed_python="$python" failed_name="$name"  # read by the main flow
    fi
    [[ -n "$python" ]] || return 0  # said above: without the Python nothing of the installed version can be looked for
    # A service told to stop takes a moment to end: what still runs after five seconds is named; nothing is stopped.
    local waited=0 services
    while :; do
        if ! services="$(live_services "$python" "$name")"; then
            printf 'Note: ps failed, so knowledge services of the installed version in other data directories could not be looked for.\n' >&2
            return 0
        fi
        [[ -n "$services" ]] && ((waited < 50)) || break
        sleep 0.1 2>/dev/null || break
        waited=$((waited + 1))
    done
    if [[ -n "$services" ]]; then
        services="${services//$'\n'/ }"
        printf '%s\n' \
            "Knowledge services of the installed version still run, processes: $services" \
            "This installer stops only the default data directory's service, so they belong to other data directories (or one is the default one, if it is still ending or its stop failed above)." \
            'Each keeps running the version being replaced, and the new version does not use it until it is stopped:' \
            '  cairnmind --data-dir <its data directory> stop    (ps -o args= -p <process> shows the directory)' >&2
    fi
}
# --- end ---

# From here until the new version is installed, nothing starts the service from the version being replaced (#298).
trap 'rm -f -- "$constraints_file"; end_upgrade' EXIT
begin_upgrade

# An earlier release was installed as the uv tool "devbrain", which owns the devbrain
# executable that this package also provides as an alias. Remove the tool, not the data.
upgraded_from_devbrain=false
if uv tool list 2>/dev/null | grep '^devbrain ' >/dev/null; then
    printf 'Removing the earlier DevBrain uv tool; your memory in %s is kept.\n' \
        "${CAIRNMIND_HOME:-${DEVBRAIN_HOME:-$HOME/.local/share/devbrain}}"
    # Stopped while the tool that started them still exists: afterwards nothing could stop them.
    stop_installed "$(uv tool dir --bin)/devbrain" devbrain "$(uv tool dir)/devbrain/bin/python"
    uv tool uninstall devbrain >/dev/null
    upgraded_from_devbrain=true
fi

# An installed Cairnmind is being replaced: the same care, with the installed version's own command.
if uv tool list 2>/dev/null | grep '^cairnmind ' >/dev/null; then
    stop_installed "$(uv tool dir --bin)/cairnmind" cairnmind "$(uv tool dir)/cairnmind/bin/python"
fi

printf 'Installing Cairnmind from %s...\n' "$package_dir"
uv tool install --python "${CAIRNMIND_INSTALL_PYTHON:-${DEVBRAIN_INSTALL_PYTHON:-3.12}}" --reinstall --constraints "$constraints_file" "$package_dir"

tool_bin_dir="$(uv tool dir --bin)"
cairnmind_bin="$tool_bin_dir/cairnmind"
if [[ ! -x "$cairnmind_bin" ]]; then
    printf 'Installation did not provide the executable: %s\n' "$cairnmind_bin" >&2
    exit 1
fi
end_upgrade  # the new version is in place: a service started from now on runs it
if [[ "${service_stop_failed:-0}" == 1 ]]; then
    # The default data directory as the new version reads it: the one whose service the installer stops.
    data_dir="$("$(uv tool dir)/cairnmind/bin/python" -I -c '
from cairnmind.config import Settings
print(Settings.load().data_dir)' 2>/dev/null || true)"
    stop_failed_service "$cairnmind_bin" "${failed_python:-}" "${failed_name:-}" "$data_dir" || exit 1
fi

# The engine mode this data directory stores (cairnmind init saves it), read by the installed version itself;
# empty when it cannot say: the native engine is then installed as before, and init names what is wrong.
stored_engine_mode() {
    "$(uv tool dir)/cairnmind/bin/python" -I -c '
from cairnmind.config import Settings
from cairnmind.database_mode import engine_mode
print(engine_mode(Settings.load()))' 2>/dev/null || true
}

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
elif [[ "$engine_in_docker" == true || "$(stored_engine_mode)" == docker ]]; then
    printf '\nThe engine runs in Docker: no ArcadeDB is downloaded here; cairnmind init downloads its image.\n'
    "$cairnmind_bin" init
    "$cairnmind_bin" engine status >/dev/null  # the summary at the end reports the engine
else
    printf '\nInstalling the pinned ArcadeDB release (about 86 MB, verified with SHA-256).\n'
    arcade_dist="${CAIRNMIND_ARCADE_DIST:-${DEVBRAIN_ARCADE_DIST:-}}"
    if [[ -n "$arcade_dist" ]]; then
        "$cairnmind_bin" engine install --distribution "$arcade_dist" >/dev/null
    else
        "$cairnmind_bin" engine install >/dev/null
    fi
    "$cairnmind_bin" init
    "$cairnmind_bin" engine status >/dev/null  # the summary at the end reports the engine
fi

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
fi

printf '\nCairnmind installed: %s\n' "$cairnmind_bin"
case ":$PATH:" in
    *":$tool_bin_dir:"*) ;;
    *) printf 'Add this directory to PATH: %s (or run: uv tool update-shell), then open a new terminal.\n' "$tool_bin_dir" ;;
esac
if [[ "$workspace_ready" == true ]]; then
    printf 'Open the terminal workspace: cairnmind\n'
    printf 'Projects, API/Ollama/Copilot profiles, client panes, memory and the graph are chosen in the terminal.\n'
    printf 'Copilot: Connections -> Connect GitHub account; the installer did not download its runtime.\n'
    printf 'Status without opening the workspace: cairnmind workspace status\n'
fi

# The same summary as `cairnmind doctor` in a terminal: required, optional, the next step (#244, #287).
printf '\nChecking the installation...\n'
"$cairnmind_bin" doctor | "$(uv tool dir)/cairnmind/bin/python" -I -m cairnmind.doctor_attention
