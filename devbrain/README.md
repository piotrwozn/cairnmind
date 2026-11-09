# DevBrain

[Kierunki rozwoju i propozycje do omówienia](docs/ROADMAP.md).

Lokalne środowisko programistyczne dla **Codex CLI, Claude Code, Ollamy, API OpenAI/Anthropic i GitHub Copilota** z osobnym agentem wiedzy. Jedna komenda `devbrain` otwiera listę projektów w bieżącym terminalu. Wybierasz projekt, asystenta i jawnie rozpoczynasz pracę. Wspólna pamięć odpowiada na `ask`: co ustalono, gdzie znajduje się kod, od czego zależy i co trzeba zrobić dalej. Inspektor pamięci, sesje oraz graf 3D są dostępne z menu.

Wersja **0.8.0** domyślnie pokazuje jeden widok na całej szerokości terminala (**Focus**) i używa języka angielskiego. **More → Settings → Language → Polski** zmienia język interfejsu. `Ctrl+B`, a następnie `Spacja`, otwiera wyszukiwalne menu poleceń; `Ctrl+B`, a następnie `?`, pokazuje skróty. `Enter` na projekcie otwiera jego podgląd. Dopiero **Start work / Rozpocznij pracę** uruchamia jednego wskazanego asystenta. **Connections / Połączenia** skupia konta, modele i klucze, a **More / Więcej** zawiera sesje, snapshoty i ustawienia. Wielopanelowy układ **Cockpit** pozostaje opcją. [Instrukcja terminala](docs/TERMINAL.md).

DevBrain korzysta z Hybrid RAG, źródłowego grafu zależności i ograniczonej pętli agentowej. Stan projektów jest zapisany poza rozmowami modeli. Wersja 0.3 dodaje sprawdzane wznowienie sesji, atomowe plany, wykonanie nazwanych profili testowych i pomiar przypominania na izolowanych danych.

Wersja 0.3.2 rozwija ograniczoną korektę odpowiedzi z 0.3.1 o opcjonalny kontrakt `answer_schema`: wymagane klucze i typy danych są sprawdzane przed zaakceptowaniem wyniku. Raport ewaluacji zawiera diagnostykę odpowiedzi modelu i błędów Ollama. Próg zaliczenia oraz wymagania dotyczące źródeł pozostają takie same.

Wersja 0.3.3 naprawia przekazywanie identyfikatorów sesji z launchera do MCP, poprawia uprawnienia czterech narzędzi tymczasowego serwera testowego Codexa i dodaje jawne pominięcie Claude’a przy niedostępnym limicie konta. Pominięty etap pozostaje `not_run`.

Wersja 0.3.4 rozpoznaje techniczne wpisy `world_state` i `token_usage_record` z historii Codexa 0.166. Weryfikowane wznowienie oraz importer potrafią ponownie rozpoznać takie wpisy zapisane przez starszy parser, zachowując ich oryginalną treść, identyfikatory i skróty. Nieznane lub uszkodzone wpisy nadal oznaczają niepełny zakres historii.

Wersja 0.3.5 utrwala zakres historii dla danego wznowienia. Ponawianie odczytu pakietu nie przesuwa tego zakresu za bieżącymi wywołaniami narzędzi. Startowy hook odczytuje dokładny pakiet launchera bez zastępowania go innym, a zmiana kodu nadal wymaga odświeżenia i nowego ACK.

Wersja 0.3.6 zachowuje zapisane przez natywnego klienta zgody narzędzi MCP oraz uprawnienia istniejących plików konfiguracji. Ponowny start aktualizuje wyłącznie zarządzane pola połączenia DevBrain. Zapis nowej trwałej zgody przez Codexa nadal zmienia konfigurację i może wymagać odświeżenia pakietu oraz ACK.

Wersja 0.3.7 rozpoznaje 11 dodatkowych typów wpisów pomocniczych Claude Code 2.1.318, w tym tryb sesji, metadane załączników i żądań API. Archiwizuje ich pełną surową treść i naprawia klasyfikację wcześniej zapisanych wpisów bez zmiany identyfikatorów oraz skrótów. Niepoprawne schematy i konflikty pochodzenia nadal blokują gotowość.

Wersja 0.3.8 rozlicza nieudaną pierwszą próbę odczytu rozmiaru historii, gdy późniejsze zdarzenie tej samej sesji dostarcza poprawny zakres i przechodzi on pełną weryfikację archiwum. Zachowuje ślad pierwotnego błędu; zmierzone, sprzeczne lub niezweryfikowane zakresy nadal blokują wznowienie.

Wersja 0.3.9 ogranicza kontekst pojedynczej poprawy odpowiedzi `ask`, dodaje schemat i jedną próbę poprawy ekstrakcji oraz atomowy zapis jej propozycji. Diagnostyka zachowuje przyczyny błędów i rozróżnia historię nieudanych prób od potwierdzonego zakresu archiwum. Zadania można odczytać i ponowić pojedynczo w obrębie projektu, z kontrolą wersji próby i zachowaniem audytu.

Wersja 0.3.10 ogranicza generowanie ekstrakcji do badanego źródła, bez treści wcześniejszych rekordów w danych wejściowych modelu. Schemat generowania ogranicza cytaty do dosłownych fragmentów tego źródła; niezależna walidacja nadal odrzuca nieistniejące cytaty. Raport rozdziela przyczyny pominięcia propozycji. Jawna flaga `jobs retry --include-omissions` pozwala ponowić zakończoną ekstrakcję, jeśli wszystkie propozycje odrzucono i niczego nie zapisano.

Wersja **0.5** wprowadziła zarządzane sesje klientów, profile, wyszukiwanie projektów, inspektor i lokalny kokpit przeglądarkowy. Ten interfejs pozostaje dostępny jawnie przez `devbrain cockpit open`; [jego instrukcja jest archiwalna](docs/COCKPIT.md).

Wersja **0.6** udostępnia te podstawy bezpośrednio w terminalu. `devbrain` i `devbrain tui` otwierają ten sam układ tmux/Textual: panel projektów i ustawień, do dwóch widocznych klientów, inspektor i prawdziwą projekcję XYZ grafu. Codex i Claude zachowują własne interfejsy, logowanie i zgody. [Instrukcja środowiska terminalowego](docs/TERMINAL.md).

Wersja **0.7** dodaje własnego agenta programistycznego dla OpenAI Responses, Anthropic Messages, API zgodnych z OpenAI i lokalnej Ollamy oraz osobny adapter oficjalnego SDK GitHub Copilot. Wybierasz profile i panele w terminalu. Nowe silniki używają wspólnej pamięci, rzeczywistego ACK modelu, dziennika działań i jednorazowych zgód na polecenia. [Konfiguracja połączeń i uprawnień](docs/API.md).

Poprawka **0.7.1** naprawia błąd wyświetlania grafu i wprowadza oświetlone węzły ASCII 3D z perspektywą i zasłanianiem powierzchni. Obraca się rzeczywisty graf pamięci, podobnie jak klasyczny terminalowy donut; nie są to dane demonstracyjne. [Aktualizacja z 0.7.0](docs/README.md#aktualizacja-070-do-071).

Poprawka **0.7.2** naprawia logowanie GitHub Copilot kontem w WSL: zgoda na lokalny zapis poświadczeń jest przekazywana w formacie wymaganym przez runtime. Profil Copilota ignoruje odziedziczone ustawienia BYOK i trybu offline, a integralność runtime jest sprawdzana według zawartości plików. Narzędzie potwierdzania pamięci ma dokładny schemat pól planu i zadania, aby model nie musiał zgadywać formatu ACK. Poprawiono także konfigurację testu natywnego Codexa, diagnostykę błędów zapisu ekstrakcji i zależności narzędzi budowania opcjonalnego kokpitu WWW. [Aktualizacja z 0.7.1](docs/README.md#aktualizacja-071-do-072).

## Architektura

```text
Terminal: tmux + Textual ──► Codex CLI / Claude Code / agent API / Copilot SDK
                           │ MCP: resume / ACK / ask / plany
                           ▼
                 Lokalna usługa DevBrain
                  │                   │
          agent wiedzy Ollama     niezależna archiwizacja
                  │                   │
          Hybrid RAG + GraphRAG ◄─────┘
                  │
       ArcadeDB: źródła, wektory, graf, decyzje, zadania
```

- **Agent wiedzy:** `qwen3.5:9b` przez Ollama, kontekst roboczy 16 tys. tokenów. Model kodujący nowego profilu jest wybierany niezależnie.
- **Embeddingi:** `qwen3-embedding:0.6b`.
- **Wyszukiwanie:** natywny indeks pełnotekstowy ArcadeDB + `LSM_VECTOR`/HNSW + przejścia po grafie.
- **Pamięć:** wersjonowane rekordy i niezależne snapshoty źródeł, także niezacommitowanego kodu.
- **Kolejka:** wspólna dla klientów; pytania mają pierwszeństwo przed oczekującą ekstrakcją i embeddingami.
- **Plany:** cały plan i zależności zapisane atomowo, stabilne identyfikatory kroków i deterministyczny następny krok dla projektu/worktree.
- **Sesje:** launcher czeka na gotowy pakiet wznowienia; zapis z powiązanej sesji wymaga rzeczywistego ACK modelu z odczytanymi identyfikatorami i wersjami.
- **Weryfikacja:** nazwane profile testowe wiążą wynik z definicją zadania, profilem i odciskiem bieżącego kodu. Postęp `done` i świeżość dowodu są osobne.

Inspiracją jest sposób organizacji pamięci i agentów w SynapCores. To własna implementacja warstwy wiedzy; nie kopia całego produktu ani jego silnika bazodanowego.

Jedynym silnikiem danych jest **[ArcadeDB](https://github.com/ArcadeData/arcadedb), Apache 2.0**. DevBrain zarządza przypiętą wersją `26.11.1` jako lokalnym procesem Java. Dokumenty, graf, FTS i indeks wektorowy znajdują się w tym samym silniku.

## Instalacja na Windowsie

Docelowy układ: Windows + WSL2 Ubuntu, **Java 21+, tmux 3.4+**, Ollama i wybrane klienty w tej samej dystrybucji WSL. Codex i Claude Code są wymagane tylko dla ich natywnych profili. RTX 4080 Super ma 16 GB VRAM; rzeczywiste wykorzystanie pamięci i opóźnienia sprawdzisz po instalacji. Docker nie jest potrzebny.

1. W Windowsie zainstaluj aktualny sterownik NVIDIA i WSL2. W Ubuntu sprawdź `nvidia-smi`.
2. W WSL zainstaluj Java 21 (`sudo apt install openjdk-21-jre-headless tmux`), [uv](https://docs.astral.sh/uv/getting-started/installation/), [Ollama](https://docs.ollama.com/linux), [Codex CLI](https://developers.openai.com/codex/cli) i [Claude Code](https://code.claude.com/docs/en/setup). Zaloguj oba CLI ich własnymi mechanizmami.
3. Skopiuj katalog projektu do WSL, np. `~/src/devbrain`, i uruchom:

```bash
cd ~/src/devbrain
bash scripts/install-wsl.sh
```

Instalator tworzy izolowaną instalację narzędzia przez `uv tool`, pobiera przypięty ArcadeDB (około 86 MB, z weryfikacją SHA-256), uruchamia silnik i inicjalizuje bazę. Nie pobiera modeli ani runtime Copilota i nie zmienia konfiguracji Codexa/Claude’a. Runtime Copilota przygotowujesz osobną akcją w TUI. Jeśli polecenie `devbrain` nie jest jeszcze w `PATH`, dodaj katalog pokazany przez instalator.

Przy instalacji ręcznej po `uv tool install --python 3.12 ~/src/devbrain` wykonaj:

```bash
devbrain engine install
devbrain init
devbrain engine status
```

Uruchom Ollama w osobnym terminalu, jeśli nie działa jako usługa:

```bash
ollama serve
```

Następnie pobierz modele — to jawny krok pobierający kilka GB:

```bash
devbrain models pull
devbrain doctor
```

Instrukcja pracy: **[docs/TERMINAL.md](docs/TERMINAL.md)**. Pełna instalacja, pamięć i diagnostyka: **[docs/README.md](docs/README.md)**. Gotowy pakiet terminalowy nie wymaga Node.js ani budowania frontendu.

## Pierwszy projekt

```bash
devbrain project add moja-aplikacja ~/src/moja-aplikacja
devbrain install moja-aplikacja
devbrain
```

Polecenie `devbrain` działa z dowolnego katalogu i korzysta z jednego rejestru projektów w lokalnym magazynie DevBrain. Projekt możesz też dodać przez **Add folder / Dodaj folder**: wpisz ścieżkę Linux lub Windows albo wybierz katalog klawiaturą. Nazwa jest opcjonalna. `Enter` na liście otwiera projekt do inspekcji. Na ekranie projektu wybierz nazwanego asystenta i użyj **Start work / Rozpocznij pracę**. **Add assistant / Dodaj asystenta** dodaje kolejne połączenie do projektu, zachowując istniejące przydziały.

W **Connections / Połączenia** dodaj połączenie, zaloguj konto albo ustaw klucz, wybierz model i opcjonalnie wykonaj jawny test. Copilot korzysta z konta GitHub bez własnego klucza API. Limity i odwołania do poświadczeń są w **Advanced limits and credentials / Zaawansowane limity i poświadczenia**. Sam zapis połączenia nie generuje odpowiedzi. Otwarcie środowiska dołącza istniejące procesy; nie uruchamia wszystkich przypisanych asystentów. [Pełna instrukcja API](docs/API.md).

Pełny układ wymaga co najmniej **140 kolumn × 42 wierszy**; zalecane jest **180 × 55**. W mniejszym terminalu aktywny panel jest powiększany, a ukryte procesy nadal działają. `Ctrl+B`, potem `p`, `1`, `2`, `i` lub `g` wybiera projekty, klientów, inspektor lub graf; `z` powiększa panel, a `d` odłącza środowisko. Podwójne `Ctrl+B` przekazuje ten skrót do natywnego klienta. Brak interaktywnego terminala daje komunikat z instrukcją, bez otwierania przeglądarki i inicjalizowania usług.

```bash
devbrain workspace status
devbrain workspace detach
devbrain workspace takeover
devbrain workspace stop
devbrain workspace stop --clients
```

Odłączenie zachowuje środowisko i procesy. `workspace stop` zamyka zewnętrzny układ; opcja `--clients` jawnie zatrzymuje również klientów przejętych przez ten workspace. Pozostałe uruchomienia pozostają niezależne. Wyłączenie WSL kończy procesy, a zapisana pamięć pozostaje dostępna do jawnego wznowienia.

Jeden terminal steruje workspace. Drugi terminal otwiera podgląd; `devbrain workspace takeover` jawnie przejmuje sterowanie i przełącza poprzedni terminal w podgląd, bez uruchamiania nowych klientów.

Launcher i `devbrain run` odmawiają startu, jeśli klient nie ma zweryfikowanej obsługi MCP, hooków, przekazania pakietu i powiązania sesji. Wynik wykrycia pokazuje `devbrain doctor`. Projekt z istniejącym kodem wymaga repozytorium Git i pierwszego commita; wyjątek dla pustego projektu jawnie oznacza brak zweryfikowanego kodu.

Można pracować również bez launchera:

```bash
devbrain run moja-aplikacja codex
devbrain run moja-aplikacja claude
devbrain ask moja-aplikacja "Dlaczego zmieniliśmy logowanie i co od niego zależy?"
devbrain context moja-aplikacja
devbrain status --project moja-aplikacja
```

`ask` zwraca odpowiedź, źródła, braki wiedzy i aktualność. Przy niedostępnym modelu pokazuje `unavailable`; nie udaje, że samo wyszukiwanie jest odpowiedzią agenta.

## Plany, zadania i dowody

Zapis planu obejmuje nagłówek, kroki i finalny graf zależności w jednej transakcji. Codex i Claude widzą te same wersje. Aktywny plan jest przypisany do konkretnego zarejestrowanego worktree. Launcher pokazuje plany, następny krok, blokady, świeżość dowodów i gotowość pakietu sesji.

```bash
devbrain plan save moja-aplikacja ./plan.json --idempotency-key login-plan-1 --activate
devbrain plan next moja-aplikacja
devbrain plan list moja-aplikacja
devbrain task list moja-aplikacja
```

Stany postępu to `todo`, `in_progress`, `needs_test`, `needs_review`, `done`, `blocked`. Osobny status `proposed`/`confirmed` opisuje pewność ustalenia. Wynik `done` pozostaje historią wykonania; późniejsza zmiana kodu może zmienić jego weryfikację na `stale`.

Profile deklarujesz w `.devbrain/checks.toml`, np. `[checks.unit]` z `argv = ["uv", "run", "pytest"]`. Uruchomienie jest jawną operacją CLI:

```bash
devbrain check run moja-aplikacja unit --task ID_ZADANIA
devbrain check list moja-aplikacja --task ID_ZADANIA
devbrain eval run
devbrain eval run --clients
devbrain eval run --clients --skip-claude
```

Test działa w bieżącym worktree. Kod wyjścia 0 potwierdza wykonanie wskazanego polecenia; review i akceptacja nadal wymagają oddzielnego sprawdzenia. Dawne flagi testów są deklaracjami `reported`, nigdy automatycznie utworzonymi wynikami uruchomienia. [Format planu, profile, MCP i ograniczenia](docs/README.md#zadania-plany-i-ukończenie).

`acceptance_passed` opisuje udokumentowaną ocenę kryteriów zadania; samo pole nie dowodzi akceptacji człowieka. W pilotażu ten sam checkpoint może zawierać review i ocenę wszystkich kryteriów. Wersja planu rośnie również przy zmianie postępu zadania, więc review zapisane przed przejściem do `done` może odnosić się do wcześniejszej wersji planu; aktualność testów sprawdza `check_status`.

Odcisk obejmuje także nieignorowane pliki nieśledzone. Umieść generowane cache i wyniki testów w `.gitignore`, aby ich powstanie podczas sprawdzenia nie oznaczało zmiany wejściowego kodu; [szczegóły konfiguracji](docs/README.md#nazwane-profile-sprawdzeń).

`eval run` sprawdza 20 scenariuszy z rzeczywistą Ollama w tymczasowym repozytorium i osobnej bazie ArcadeDB. Opcjonalne `--clients` uruchamia świeżego Codexa, Claude’a i ponownie Codexa z istniejącymi logowaniami i zużywa ich limity. Brak modelu, klienta lub logowania daje `not_run`. Raport podaje trafność, błędy stanu, opóźnienia, rozmiar kontekstu lokalnego modelu i tokeny tylko wtedy, gdy dostawca rzeczywiście je raportuje.

`--clients --skip-claude` sprawdza wyłącznie dwie świeże sesje Codexa; nie uruchamia procesu Claude’a. Raport opisuje to jako test częściowy i nie zalicza pełnego przekazania między klientami. Zgody narzędzi Codexa dotyczą wyłącznie czterech operacji serwera `devbrain_eval` w tymczasowej bazie; sandbox pozostaje `read-only`.

## Przenośna pamięć projektu

Pełny snapshot w `.memory/devbrain/` przenosi pamięć jednego projektu razem z repozytorium: rozmowy i dokładną treść źródeł, ustalenia, plany, zadania, checkpointy, rewizje i powiązania. Zapis i import nie wymagają Ollama. Panel pamięci udostępnia podgląd, zapis, sprawdzenie importu, import i ustawienia automatu. Te same operacje są dostępne przez CLI. Samo wykrycie plików niczego nie wczytuje.

```bash
devbrain memory snapshot moja-aplikacja --preview
devbrain memory snapshot moja-aplikacja
devbrain memory auto moja-aplikacja --enable --interval-minutes 15
devbrain memory status moja-aplikacja
devbrain memory auto moja-aplikacja --disable
```

Automat jest domyślnie wyłączony. Po włączeniu zapisuje zmienioną pamięć podczas pracy usługi, także po zamknięciu TUI. Zachowuje pięć ostatnich snapshotów, współdzieląc identyczne obiekty. Nie uruchamia modeli, nie wykonuje commitów i niczego nie wysyła na GitHub. Pełne rozmowy mogą zawierać prywatne informacje: sprawdź zakres eksportu przed dodaniem `.memory/devbrain/` do repozytorium. Konfiguracja komputera, pliki uwierzytelnienia i klucze z konfiguracji nie są eksportowane; treść zapisanych rozmów nie jest automatycznie anonimizowana.

Na drugim komputerze, po zainstalowaniu DevBrain i sklonowaniu repozytorium:

```bash
devbrain memory inspect ~/src/moja-aplikacja/.memory/devbrain
devbrain memory import ~/src/moja-aplikacja/.memory/devbrain --root ~/src/moja-aplikacja --dry-run
devbrain memory import ~/src/moja-aplikacja/.memory/devbrain --root ~/src/moja-aplikacja
```

Import zachowuje tożsamość projektu i nie zastępuje innych projektów w bazie. Ponowne wczytanie tego samego stanu niczego nie powiela; rozbieżne zmiany wymagają rozwiązania konfliktu. Indeksy wyszukiwania odbudowują się lokalnie. Historyczne `done` pozostaje zachowane, ale testy przeniesione z innego komputera nie są lokalnym dowodem `fresh`. Logowania, konfigurację klientów i nowe sesje przygotowujesz lokalnie. [Pełna instrukcja przenoszenia](docs/README.md#przenośna-pamięć-projektu). [Kontrakt formatu, limity i zasady odtwarzania](docs/MEMORY_CONTRACTS.md).

```bash
devbrain eval memory
```

To jawny test z rzeczywistą Ollama: ekstrakcja faktu, decyzji i zadania, przeniesienie między dwiema izolowanymi bazami oraz odtworzenie odpowiedzi ze źródłami. Wymaga wszystkich oczekiwanych ustaleń; pusty wynik ekstrakcji nie zalicza scenariusza. Nie uruchamia Codexa ani Claude’a. Kody wyjścia: `0` — sukces, `1` — niepowodzenie, `3` — brak wymaganych modeli.

## Dane i działanie

Domyślny katalog to `~/.local/share/devbrain`; można go zmienić przez `DEVBRAIN_HOME` albo globalne `--data-dir`. Dane ArcadeDB są w `arcade/databases`. Usługa wiedzy i silnik działają w tle tak długo, jak działa WSL. `devbrain stop` zatrzymuje usługę wiedzy; po zamknięciu klientów `devbrain engine stop` zatrzymuje bazę bez kasowania danych.

```bash
devbrain export ~/pamiec.json --project moja-aplikacja
devbrain export ~/pamiec.md --project moja-aplikacja --markdown
devbrain backup ~/devbrain-backup.devbrain.json.gz
```

Aktualizator **0.7.0 → 0.7.1** sprawdza zmieniane pliki, wykonuje kopię źródeł i bazy oraz zachowuje dane, modele, klucze API, poświadczenia i natywne uprawnienia. Nie wykonuje płatnych zapytań i nie pobiera modeli ani runtime Copilota. [Instrukcja aktualizacji](docs/README.md#aktualizacja-070-do-071) zawiera także drogę ze starszych wersji. Schemat ArcadeDB pozostaje 4, format profili 2, a administracyjny backup 5 z obsługą wcześniejszych formatów 2–4. Stare grupy planów można jawnie przejąć przez `plan adopt`. Do przeniesienia pojedynczego projektu używaj `memory import`; `restore` zastępuje cały magazyn.

Archiwum nie czeka na GPU. Status pokazuje osobno archiwizację, indeksowanie, opracowanie przez model i kolejkę. Przenośny backup zawiera spójny eksport źródeł i stanu bazy; nie zawiera poświadczeń ani wag modeli. Automatyczny backup działa podczas pracy usługi; zachowywane jest siedem ostatnich kopii.

## Sprawdzenie i ograniczenia

```bash
uv sync --locked --extra dev
uv run pytest
uv run ruff check src tests
devbrain benchmark moja-aplikacja "Gdzie obsługujemy błędy logowania?"
```

Testy magazynu, pełnotekstowego wyszukiwania, grafu i indeksu wektorowego używają rzeczywistego silnika ArcadeDB. Wymagają Java 21; domyślna sesja testowa przygotowuje izolowany silnik. Automatyczne testy odpowiedzi Ollama i sesji klientów korzystają z kontrolowanych adapterów; rzeczywiste przekazanie pracy między klientami wymaga pomiaru na docelowym środowisku.

Pierwsza wersja analizuje składnię Python, JavaScript i TypeScript. Inne języki pozostają przeszukiwalne tekstowo. Analiza statyczna nie gwarantuje pełnego grafu dynamicznych wywołań. Skaner indeksuje pliki tekstowe UTF-8 do 2 MiB, respektuje `.gitignore` i pomija m.in. katalogi zależności oraz pliki `.env`/kluczy. Import historii zachowuje jej źródłową treść.

Odpowiedź `ask` i kontekst startowy nadal zajmują tokeny Codexa lub Claude’a. Lokalna pamięć zmniejsza potrzebę przenoszenia całej historii; nie usuwa limitów kontekstu ani kosztu pracy modeli. Połączenia API i Copilot korzystają z własnych rozliczeń i uprawnień kont.

Launcher blokuje start klienta, gdy pakiet wznowienia jest częściowy lub niedostępny. ACK potwierdza odbiór i odczyt konkretnych wersji, a nie rozumienie przez model. Kontrole zapisu DevBrain nie zastępują natywnych uprawnień narzędzi Codexa i Claude’a.
