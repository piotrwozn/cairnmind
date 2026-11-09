#!/usr/bin/env bash
# Install this checkout as an isolated uv tool. Model and Copilot runtime downloads are opt-in.
set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: bash scripts/install-wsl.sh [--pull-models]

Installs DevBrain as a uv tool together with ArcadeDB 26.11.1 (about 86 MB,
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

constraints_file="$(mktemp "${TMPDIR:-/tmp}/devbrain-constraints.XXXXXXXX")"
trap 'rm -f -- "$constraints_file"' EXIT

printf 'Preparing dependencies from uv.lock...\n'
uv export --directory "$package_dir" --locked --no-dev --no-emit-project \
    --format requirements-txt --output-file "$constraints_file" >/dev/null

printf 'Installing DevBrain from %s...\n' "$package_dir"
uv tool install --python "${DEVBRAIN_INSTALL_PYTHON:-3.12}" --reinstall --constraints "$constraints_file" "$package_dir"

tool_bin_dir="$(uv tool dir --bin)"
devbrain_bin="$tool_bin_dir/devbrain"
if [[ ! -x "$devbrain_bin" ]]; then
    printf 'Installation did not provide the executable: %s\n' "$devbrain_bin" >&2
    exit 1
fi

printf '\nInstalling the pinned ArcadeDB release (about 86 MB, verified with SHA-256).\n'
if [[ -n "${DEVBRAIN_ARCADE_DIST:-}" ]]; then
    "$devbrain_bin" engine install --distribution "$DEVBRAIN_ARCADE_DIST"
else
    "$devbrain_bin" engine install
fi
"$devbrain_bin" init
"$devbrain_bin" engine status
"$devbrain_bin" doctor

if [[ "$pull_models" == true ]]; then
    if ! "$devbrain_bin" models list >/dev/null; then
        printf 'Cannot reach Ollama. Start ollama serve, then run devbrain models pull.\n' >&2
        exit 1
    fi
    "$devbrain_bin" models pull
else
    printf '\nModels were not downloaded. With Ollama running, run: devbrain models pull\n'
fi

printf '\nDevBrain installed: %s\n' "$devbrain_bin"
case ":$PATH:" in
    *":$tool_bin_dir:"*) ;;
    *) printf 'Add this directory to PATH: %s\n' "$tool_bin_dir" ;;
esac
printf 'Open the terminal workspace: devbrain\n'
printf 'Projects, API/Ollama/Copilot profiles, client panes, memory and the graph are chosen in the terminal.\n'
printf 'Copilot: Connections -> Connect GitHub account; the installer did not download its runtime.\n'
printf 'Status without opening the workspace: devbrain workspace status\n'
