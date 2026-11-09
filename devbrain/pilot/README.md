# Rzeczywisty pilotaż DevBrain 0.3

Ten zestaw przygotowuje mały projekt do pracy Codex → Claude → świeży Codex.
Nie zawiera gotowej implementacji ani zaliczonych wyników modeli. Do wykonania
potrzebne są Twój WSL2, GPU, Ollama i zalogowane oba klienty. Instalację i modele
przygotuj według [instrukcji DevBrain](../docs/README.md).

## Diagnostyka i ewaluacja

W WSL sprawdź `nvidia-smi`, `codex --version`, `claude --version`, następnie zapisz
diagnostykę i wykonaj pomiar lokalnego modelu:

```bash
export DEVBRAIN_PILOT_REPORT_DIR="$HOME/devbrain-pilot-reports/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$DEVBRAIN_PILOT_REPORT_DIR"
devbrain doctor > "$DEVBRAIN_PILOT_REPORT_DIR/doctor-before.json"
devbrain models pull
devbrain doctor > "$DEVBRAIN_PILOT_REPORT_DIR/doctor.json"
devbrain eval run --report-dir "$DEVBRAIN_PILOT_REPORT_DIR" > "$DEVBRAIN_PILOT_REPORT_DIR/recall-local.json"
```

`models pull` pobiera modele. Ewaluacja używa własnego tymczasowego repozytorium i
bazy. Kod wyjścia ewaluacji to `0` = zaliczona, `1` = niezaliczona, `3` = `not_run`.
Zaliczenie wymaga 20 wykonanych
scenariuszy, >=90% trafności, braku błędów krytycznych oraz zaliczenia etapów obu
klientów. `not_run` oznacza niewykonany pomiar, nie sukces.

Dopiero po zaliczeniu lokalnej ewaluacji sprawdź obu klientów i ich zapisane
logowania. Poniższy blok uruchomi `--clients` tylko po przejściu tych kontroli;
wykorzysta istniejące limity kont. Nie wykonuje logowania ani nie wyświetla
poświadczeń.

```bash
if python3 - "$DEVBRAIN_PILOT_REPORT_DIR" <<'PY'
import json, pathlib, subprocess, sys
folder = pathlib.Path(sys.argv[1])
recall = json.loads((folder / 'recall-local.json').read_text())
doctor = json.loads((folder / 'doctor.json').read_text())
assert recall['status'] == 'passed' and recall['attempted'] == 20, 'Najpierw zalicz lokalną ewaluację.'
assert all(doctor['clients'][name]['verified_resume'] for name in ('codex', 'claude')), 'Sprawdź możliwości CLI w doctor.json.'
codex = subprocess.run(['codex', 'login', 'status'], capture_output=True, text=True, timeout=20)
assert codex.returncode == 0 and 'chatgpt' in (codex.stdout + codex.stderr).lower(), 'Brak logowania Codex ChatGPT.'
claude = subprocess.run(['claude', 'auth', 'status', '--json'], capture_output=True, text=True, timeout=20)
auth = json.loads(claude.stdout) if claude.returncode == 0 else {}
assert auth.get('loggedIn') is True and auth.get('authMethod') in ('claude.ai', 'oauth'), 'Brak logowania Claude.'
PY
then
  devbrain eval run --clients --report-dir "$DEVBRAIN_PILOT_REPORT_DIR" > "$DEVBRAIN_PILOT_REPORT_DIR/recall-clients.json"
else
  printf 'Etap klientów nie został uruchomiony. Rozwiąż wskazaną przyczynę i ponów kontrolę.\n' >&2
fi
```

## Utworzenie jednego świeżego repozytorium

Wykonaj z katalogu rozpakowanego pakietu DevBrain. Blok odmawia użycia istniejącego
`~/src/devbrain-pilot`; nie usuwa go ani nie inicjalizuje ponownie. Przy kolejnym
podejściu kontynuuj istniejący pilotaż lub wybierz nową nazwę katalogu i projektu
oraz zmień ją konsekwentnie w promptach. Projekt w pamięci ma nazwę `devbrain-pilot`.

```bash
(
  set -eu
  kit_dir="$(pwd)/pilot"
  pilot_root="$HOME/src/devbrain-pilot"
  if [ -e "$pilot_root" ]; then
    printf 'Katalog już istnieje: %s. Kontynuuj poprzedni pilotaż.\n' "$pilot_root" >&2
    exit 1
  fi
  test -f "$kit_dir/plan.json"
  mkdir -p "$HOME/src"
  mkdir "$pilot_root"
  mkdir "$pilot_root/.devbrain"
  cp "$kit_dir/templates/.gitignore" "$pilot_root/.gitignore"
  cp "$kit_dir/templates/README.md" "$pilot_root/README.md"
  cp "$kit_dir/templates/checks.toml" "$pilot_root/.devbrain/checks.toml"
  cp "$kit_dir/plan.json" "$pilot_root/pilot-plan.json"
  git -C "$pilot_root" init
  git -C "$pilot_root" add .
  git -C "$pilot_root" -c user.name='DevBrain Pilot' -c user.email='pilot@localhost' commit -m 'Prepare pilot fixture'
  devbrain project add devbrain-pilot "$pilot_root"
  devbrain install devbrain-pilot
)
```

Jeżeli nazwa projektu jest już zajęta, nie nadpisuj jego rejestracji. Sprawdź
`devbrain project list` i kontynuuj właściwe repozytorium. Kopie plików, pomiary
i raporty zapisuj w `DEVBRAIN_PILOT_REPORT_DIR`, aby nie zmieniały odcisku kodu.

## Trzy rzeczywiste sesje

Uruchamiaj klienty kolejno z tego samego terminala WSL, aby odziedziczyły katalog
raportów. Nie używaj `resume` i nie przenoś historii rozmowy między klientami.
Wklej jedynie odpowiedni krótki prompt z katalogu `pilot/prompts` pakietu:

1. `devbrain run devbrain-pilot codex` — [01-codex.txt](prompts/01-codex.txt).
   Model zapisuje plan przez MCP, tworzy kod i testy, zapisuje checkpoint i
   pozostawia `needs_test`. Zakończ natywnego klienta.
2. `devbrain run devbrain-pilot claude` — [02-claude.txt](prompts/02-claude.txt).
   Nowa sesja odtwarza stan, uruchamia profil `unit` i odczytuje rzeczywiste logi
   stdout/stderr. Wykonuje review i aktualizuje zadanie przez MCP. Zakończ klienta.
3. `devbrain run devbrain-pilot codex` — [03-fresh-codex.txt](prompts/03-fresh-codex.txt).
   Trzecia sesja odtwarza stan, źródła, testy i pozostałą pracę.

Przy `partial`/`unavailable` launcher zatrzymuje start: sprawdź
`devbrain status --project devbrain-pilot`, przyczynę archiwizacji i `devbrain doctor`, a
następnie ponów po rozwiązaniu przyczyny. Nie omijaj ACK ani wymagań profilu.
Odtworzenie identyfikatorów jest odczytem stanu; ACK nie dowodzi rozumienia modelu.

## Kompresja, restart i nieaktualny wynik

- W trzeciej sesji wywołaj natywną kompresję rozmowy. Poproś o ponowne odtworzenie
  celu, wersji i źródeł. Model ma ponownie odczytać pakiet i wykonać ACK.
- Zakończ klienta, wykonaj `devbrain stop`, potem `devbrain context devbrain-pilot`.
  To ponownie uruchomi usługę. Uruchom jeszcze jedną świeżą sesję Codexa i użyj
  promptu odczytowego; stan powinien być zachowany.
- Dopiero po uzyskaniu `done` i `fresh` zmień kod funkcji w pilotażowym
  `token_auth.py` tak, aby odrzucał poprawny token. Nie zmieniaj zadania ani jego
  dowodów. `devbrain task list devbrain-pilot` i
  `devbrain check list devbrain-pilot --task RZECZYWISTE_ID_ZADANIA`
  mają pokazać historyczne `done`, ale `stale` dla jego weryfikacji. Odczytaj stan
  także w świeżej sesji; model ma wskazać rozbieżność. Nie kasuj tego wyniku z raportu.
- Jeżeli przywracasz poprawny kod, wykonaj nowy profil `unit` i zanotuj nowe ID
  runa. Raport musi rozróżniać poszczególne obserwacje zamiast uznawać całą próbę
  za zaliczoną wyłącznie na podstawie ostatniego wyniku.

## Pomiary i warunki zaliczenia

Zapisz `devbrain doctor`, wersje klientów i listę modeli, oba raporty ewaluacji,
ID planu/zadania/runów, review i źródła oraz obserwacje przed/po kompresji,
restarcie i zmianie kodu. Raporty mogą zawierać treść rozmów — przekazuj tylko
potrzebne dane diagnostyczne, bez konfiguracji poświadczeń.

Dla trzech pytań użyj istniejącego `devbrain benchmark devbrain-pilot "PYTANIE"`:

- „Jaki jest cel i ograniczenia pilotażu?”
- „Jaki stan ma zadanie walidacji tokenu i jakie ma dowody?”
- „Gdzie znajduje się walidacja tokenu i jakie przypadki sprawdzają testy?”

Zapisz wyniki w katalogu raportów. Benchmark porównuje pierwsze żądanie z
powtórzeniem przez cache; nie oznacza zimnego startu modelu. Podczas pracy
obserwuj GPU przez `nvidia-smi --query-gpu=timestamp,memory.used,memory.total --format=csv -l 1`
w drugim terminalu i zapisz obserwowane zużycie VRAM poza repozytorium.

Pilotaż zalicza się dopiero, gdy rzeczywiste modele przejdą ewaluację i trzy sesje
odtworzą zgodne stany ze źródłami, prawdziwe testy oraz review zostaną zapisane,
a sprawdzenia kompresji, restartu i `stale` dadzą oczekiwane wyniki. Braki,
przekroczenia czasu i błędne odpowiedzi zapisuj jako takie. Rzeczywistą latencję
i VRAM raportujemy jako pomiar; testy techniczne ani gotowy zestaw plików ich nie
zastępują.
