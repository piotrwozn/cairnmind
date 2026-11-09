#!/usr/bin/env bash
# Install this checkout as an isolated uv tool. Model and Copilot runtime downloads are opt-in.
set -euo pipefail

usage() {
    cat <<'USAGE'
Użycie: bash scripts/install-wsl.sh [--pull-models]

Instaluje DevBrain przez uv tool oraz ArcadeDB 26.11.1 (około 86 MB,
archiwum weryfikowane SHA-256), uruchamia silnik i inicjalizuje lokalną pamięć.
Wymaga Java 21+ oraz tmux 3.4+. Nie zmienia konfiguracji Codexa/Claude'a.
Nie pobiera modeli ani runtime Copilota domyślnie; Copilota przygotujesz jawnie w TUI.

  --pull-models  Pobierz oba modele po instalacji (kilka GB; Ollama musi działać).
  --help        Pokaż tę pomoc.
USAGE
}

# SDK runtime is prepared explicitly from TUI, never as an installation side effect.
export COPILOT_SKIP_CLI_DOWNLOAD=1

pull_models=false
for argument in "$@"; do
    case "$argument" in
        --pull-models) pull_models=true ;;
        --help|-h) usage; exit 0 ;;
        *) printf 'Nieznany argument: %s\n' "$argument" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ "$(uname -s)" != Linux ]]; then
    printf 'Uruchom instalator w Ubuntu/WSL2 lub innym systemie Linux.\n' >&2
    exit 1
fi

if ! command -v java >/dev/null 2>&1; then
    printf 'Brak Java. Zainstaluj Java 21+: sudo apt install openjdk-21-jre-headless\n' >&2
    exit 1
fi
java_version_output="$(java -version 2>&1)"
java_version_pattern='version "([0-9]+)'
if [[ ! "$java_version_output" =~ $java_version_pattern ]]; then
    printf 'Nie można rozpoznać wersji Java. Sprawdź java -version; wymagane Java 21+.\n' >&2
    exit 1
fi
java_major="${BASH_REMATCH[1]}"
if (( java_major < 21 )); then
    printf 'Wykryto Java %s. Wymagane Java 21+: sudo apt install openjdk-21-jre-headless\n' "$java_major" >&2
    exit 1
fi

if ! command -v tmux >/dev/null 2>&1; then
    printf 'Brak tmux. Zainstaluj: sudo apt install tmux (wymagana wersja 3.4+).\n' >&2
    exit 1
fi
tmux_version_output="$(tmux -V)"
tmux_version_pattern='tmux ([0-9]+)\.([0-9]+)'
if [[ ! "$tmux_version_output" =~ $tmux_version_pattern ]]; then
    printf 'Nie można rozpoznać wersji tmux; wymagana 3.4+.\n' >&2
    exit 1
fi
tmux_major="${BASH_REMATCH[1]}"
tmux_minor="${BASH_REMATCH[2]}"
if (( tmux_major < 3 || (tmux_major == 3 && tmux_minor < 4) )); then
    printf 'Wykryto tmux %s.%s. Wymagane 3.4+: sudo apt install tmux\n' "$tmux_major" "$tmux_minor" >&2
    exit 1
fi

kernel_release="$(uname -r)"
if [[ "${kernel_release,,}" == *microsoft* ]]; then
    printf 'Wykryto Linux w WSL. Sterownik NVIDIA powinien być zainstalowany w Windowsie.\n'
else
    printf 'Wykryto Linux poza WSL. Instalacja pakietu jest obsługiwana; sprawdź lokalny dostęp do GPU.\n'
fi

if ! command -v uv >/dev/null 2>&1; then
    printf 'Brak uv w PATH. Zainstaluj uv: https://docs.astral.sh/uv/getting-started/installation/\n' >&2
    printf 'W Ubuntu możesz użyć pipx; instrukcja jest w docs/README.md.\n' >&2
    exit 1
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
package_dir="$(cd -- "$script_dir/.." && pwd)"
if [[ ! -f "$package_dir/pyproject.toml" || ! -f "$package_dir/uv.lock" ]]; then
    printf 'Brak pyproject.toml lub uv.lock obok katalogu scripts. Skopiuj cały projekt.\n' >&2
    exit 1
fi

constraints_file="$(mktemp "${TMPDIR:-/tmp}/devbrain-constraints.XXXXXXXX")"
trap 'rm -f -- "$constraints_file"' EXIT

printf 'Przygotowanie zależności z uv.lock…\n'
uv export --directory "$package_dir" --locked --no-dev --no-emit-project \
    --format requirements-txt --output-file "$constraints_file" >/dev/null

printf 'Instalowanie DevBrain z %s…\n' "$package_dir"
uv tool install --python "${DEVBRAIN_INSTALL_PYTHON:-3.12}" --reinstall --constraints "$constraints_file" "$package_dir"

tool_bin_dir="$(uv tool dir --bin)"
devbrain_bin="$tool_bin_dir/devbrain"
if [[ ! -x "$devbrain_bin" ]]; then
    printf 'Instalacja nie udostępniła pliku wykonywalnego: %s\n' "$devbrain_bin" >&2
    exit 1
fi

printf '\nInstalowanie przypiętego ArcadeDB; oficjalny download ma około 86 MB i weryfikację SHA-256.\n'
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
        printf 'Nie można połączyć się z Ollama. Uruchom ollama serve, a potem devbrain models pull.\n' >&2
        exit 1
    fi
    "$devbrain_bin" models pull
else
    printf '\nModele nie zostały pobrane. Przy działającej Ollama uruchom: devbrain models pull\n'
fi

printf '\nDevBrain zainstalowany: %s\n' "$devbrain_bin"
case ":$PATH:" in
    *":$tool_bin_dir:"*) ;;
    *) printf 'Dodaj do PATH katalog: %s\n' "$tool_bin_dir" ;;
esac
printf 'Otwórz terminalowe środowisko pracy: devbrain\n'
printf 'Projekty, profile API/Ollama/Copilot, panele klientów, pamięć i graf wybierzesz w terminalu.\n'
printf 'Copilot: Connections -> Connect GitHub account; runtime nie został pobrany przez instalator.\n'
printf 'Stan bez uruchamiania środowiska: devbrain workspace status\n'
