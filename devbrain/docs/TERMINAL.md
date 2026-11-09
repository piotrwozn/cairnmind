# DevBrain 0.8.0 — środowisko w terminalu

`devbrain` uruchamia środowisko w bieżącym terminalu. `devbrain tui` jest aliasem tego samego interfejsu. tmux zarządza panelami i odłączaniem, a Textual wyświetla projekty, ustawienia, inspektor i graf. Panele Codexa i Claude’a zawierają rzeczywiste natywne CLI, z ich własnym ekranem, klawiaturą, logowaniem i zgodami. Profile Ollamy i API używają własnego agenta DevBrain, a Copilot osobnego adaptera oficjalnego SDK.

Domyślny język to angielski. Zmień go przez **More → Settings → Language → Polski**. Poniższe instrukcje podają najważniejsze nazwy angielskie i polskie. Język dotyczy interfejsu DevBrain; treści projektów i natywne interfejsy klientów pozostają własnymi treściami.

## Uruchomienie

Wymagane są Linux lub Ubuntu WSL2, Java 21+, tmux 3.4+, zainstalowany DevBrain i interaktywny terminal Unicode/UTF-8. Klienty kodujące instaluj w tej samej dystrybucji WSL. Do używania gotowego pakietu nie potrzebujesz Node.js, npm ani budowania frontendu.

W Windows Terminal otwórz Ubuntu albo wejdź do niego z PowerShella:

```powershell
wsl -d Ubuntu
```

Następnie w Ubuntu:

```bash
devbrain
```

Polecenie działa z dowolnego katalogu. Lista projektów pochodzi ze wspólnego lokalnego rejestru DevBrain, a nie z katalogu, w którym otwarto terminal. Osobne `--data-dir` lub `DEVBRAIN_HOME` oznacza osobny magazyn i rejestr.

Przy pierwszej konfiguracji wybierz **Add folder / Dodaj folder**. Wpisz ścieżkę Linux lub Windows, np. `/home/piotr/src/app` albo `C:\Users\piotr\src\app`. **Browse folders / Przeglądaj foldery** otwiera wybór katalogu bez poleceń powłoki: `Enter` otwiera podfolder, `Alt+Up` przechodzi wyżej, a `Ctrl+S` wybiera wyświetlony katalog. Nazwa projektu może pozostać pusta; wtedy pochodzi z nazwy folderu. W formularzu `Ctrl+S` zapisuje, a `Esc` wraca. Możesz też użyć CLI:

```bash
devbrain project add moja-aplikacja ~/src/moja-aplikacja
devbrain install moja-aplikacja
devbrain
```

Dla sklonowanego projektu zawierającego `.memory/devbrain/` zacznij od podglądu i importu pamięci, aby zachować jego istniejącą tożsamość. Samo znalezienie snapshotu nie importuje danych i nie uruchamia zapisanych w nim poleceń.

Uruchomienie bez interaktywnego TTY kończy się komunikatem i kodem `2`, przed utworzeniem konfiguracji albo uruchomieniem usług. Nie przełącza się automatycznie na przeglądarkę. Wersję pakietu można odczytać bez TTY i bez inicjalizacji:

```bash
devbrain version
```

## Układ i rozmiar

Domyślny **Focus** pokazuje jeden widok na całej szerokości. Zacznij od **Projects / Projekty**, otwórz projekt i wybierz **Start work / Rozpocznij pracę**. Menu `Ctrl+B`, potem `Spacja`, przełącza między projektami, asystentem, pamięcią i grafem. Interfejs jest obsługiwany klawiaturą również przy **80 × 24**.

**More → Settings → Layout → Cockpit** włącza wielopanelowy układ na większym terminalu. To samo ustawienie jest dostępne przez polecenia **Focus view / Cockpit view** w menu. Pełny układ może pokazać pięć paneli:

```text
┌────────────────┬──────────────────────┬──────────────────────┐
│ Projekty       │ Codex CLI            │ Claude Code          │
│ Worktree       │                      │                      │
│ Połączenia     ├─────────────────────────────┬───────────────┤
│ Więcej         │ Inspektor pamięci           │ Graf 3D       │
│                │                             │ ASCII XYZ     │
└────────────────┴─────────────────────────────┴───────────────┘
```

Panel boczny zajmuje 24 kolumny. Po prawej górna część mieści wybranych klientów i agentów, a dolna inspektor i graf. Jeden wybrany klient wykorzystuje całą górną część; przy dwóch panel jest dzielony. Jeżeli skonfigurujesz więcej slotów, jawnie wybierasz do dwóch widocznych jednocześnie. Ukrycie slotu nie kończy jego procesu.

Minimalny pełny rozmiar układu Cockpit to **140 kolumn × 42 wiersze**. Zalecany jest **180 × 55** lub większy. Poniżej minimum aktywny panel jest powiększany; skróty nadal przełączają między widokami, a ukryte procesy pracują dalej. Samo powiększenie okna nie wyłącza wybranego trybu Focus.

Windows Terminal powinien używać czcionki zawierającej polskie znaki i znaki obramowań Unicode. W Ubuntu `locale charmap` powinno wskazywać `UTF-8`. Rozmiar oznacza liczbę znaków terminala, a nie pikseli ekranu.

## Nawigacja

Najpierw naciśnij prefiks `Ctrl+B`, następnie wskazany klawisz:

| Klawisz po `Ctrl+B` | Działanie |
|---|---|
| `Spacja` | Wyszukiwalne menu wszystkich poleceń |
| `?` | Pomoc klawiatury |
| `p` | Panel projektów, profili i ustawień |
| `1` | Pierwszy widoczny klient |
| `2` | Drugi widoczny klient |
| `i` | Inspektor pamięci |
| `g` | Graf pamięci |
| `Tab` | Następny widok |
| `z` | Powiększenie aktywnego panelu / powrót do układu |
| `[` | Przewijanie i kopiowanie historii terminala; `q` wraca |
| `d` | Odłączenie workspace i powrót do powłoki |
| `Ctrl+B` | Przekazanie `Ctrl+B` do natywnego klienta |

Podwójne `Ctrl+B` jest potrzebne, gdy sam Codex, Claude albo program w ich terminalu używa tego skrótu. Pozostałe wejście, wklejanie, `Ctrl+C` i dialogi zgód w aktywnym panelu klienta trafiają do jego rzeczywistego CLI. Przyciski i pola DevBrain obsługujesz przez `Tab`, `Shift+Tab`, strzałki i `Enter`; `Esc` zamyka dialog lub wraca. Formularze zapisujesz przez `Ctrl+S`. Błąd pola lub zapisu pozostawia wpisane wartości i pokazuje problem; ponowienie wymaga świadomego zapisu.

Na liście projektów `Enter` otwiera ekran projektu do inspekcji. Nie rozpoczyna pracy asystenta. Na tym ekranie **Start work / Rozpocznij pracę** uruchamia tylko wskazanego asystenta. Poza polami tekstowymi `w` wybiera tę akcję, `p` otwiera podgląd, `r` odświeża, a `m` otwiera pamięć. Na liście `w` najpierw otwiera ekran projektu. Puste wyniki wyszukiwania pozostają puste; akcje nie używają wcześniej aktywnego projektu. W inspektorze `h` otwiera historię, `m` pamięć, `r` odświeża, a `Esc` wraca do listy. Skróty literowe działają tylko we wskazanym panelu DevBrain.

**Start work** w globalnym menu najpierw pokazuje projekt i ustawia fokus na przycisku uruchomienia. Sprawdź widoczną nazwę asystenta i naciśnij `Enter`, aby rozpocząć. Dzięki temu opóźnione polecenie menu nie uruchamia asystenta po zmianie projektu.

Jeśli TUI uruchamia usługę pamięci, jej kolejka embeddingu i ekstrakcji czeka na jawną akcję. Udane **Start work** włącza przetwarzanie pamięci tylko wybranego projektu. Możesz też wybrać **More → Queue → Process memory / Więcej → Kolejka → Przetwórz pamięć**, aby przetworzyć oczekujące zadania bez uruchamiania asystenta. Samo otwarcie projektu, odczyt pamięci i przełączenie widoku nie włączają tej kolejki. Skanowanie plików i skonfigurowane snapshoty nadal działają. Jeśli uruchomienie asystenta się powiedzie, ale włączenie kolejki nie, komunikat prowadzi do **Process memory**; ponowny start asystenta nie jest potrzebny.

Już działające usługi i sesje kontynuują swoją pracę. Ręczne `devbrain serve` zachowuje automatyczne przetwarzanie kolejki; `devbrain serve --defer-background-inference` pozwala je odroczyć. Jawne zapytanie do modelu pozostaje osobną akcją użytkownika.

Szersze formularze tymczasowo powiększają panel, aby mieścił ścieżki i ustawienia. Po zamknięciu wracają do poprzedniego układu, jeżeli w międzyczasie nie zmieniono ręcznie panelu lub powiększenia.

## Projekt, asystent i połączenie

Panel boczny pokazuje wszystkie zarejestrowane projekty. Możesz wybrać ich zarejestrowany worktree oraz skanować jawnie wskazane katalogi Ubuntu lub dysków Windows dostępnych przez WSL. Skanowanie nie oznacza zgody na uruchamianie klientów ani automatyczny import pamięci.

1. **Projects / Projekty**: wyszukaj projekt i naciśnij `Enter`.
2. **Project / Projekt**: sprawdź folder i widoczną nazwę asystenta. Możesz wybrać innego przypisanego asystenta albo użyć **Add assistant / Dodaj asystenta**.
3. **Connections / Połączenia**: dodaj nazwane połączenie, zaloguj konto lub ustaw klucz i wybierz model. Limity oraz odwołania do poświadczeń są w ustawieniach zaawansowanych połączenia.
4. Wróć do projektu i wybierz **Start work / Rozpocznij pracę**. Rozpocznie pracę tylko jeden wskazany asystent.

Połączenia obejmują natywnego Codexa, Claude Code, agenta OpenAI/Anthropic, API zgodne z OpenAI, lokalną Ollamę i GitHub Copilot. **More → Manage assistants / Więcej → Zarządzaj asystentami** pozwala zmieniać kolejność i przydziały. Dodanie asystenta zachowuje istniejące przydziały. Wszystkie klienty pracują we wskazanym katalogu; DevBrain nie tworzy sam osobnych worktree ani nie przydziela automatycznie wspólnego zadania. [Konfiguracja kont, kluczy i ograniczeń](API.md).

Otwarcie workspace albo przywrócenie jego układu dołącza istniejące procesy. Nową pracę uruchamiasz jawnie z panelu bocznego. Wybór danych do inspekcji, także przejście z globalnego grafu do projektu, nie uruchamia klienta. Zmiana profilu obowiązuje przy następnym uruchomieniu procesu.

Stan klienta rozdziela brak programu, konfigurację, deklarowane logowanie, łączność i zaobserwowany limit. Zalogowany klient może mieć wyczerpany limit; błąd sieci nie oznacza wylogowania. Sprawdzenie stanu nie wykonuje płatnego zapytania do modelu.

**More / Więcej** zawiera sesje, zarządzanie asystentami, wyszukiwanie folderów, snapshoty, import pamięci i ustawienia. Podstawowe widoki pokazują czytelne podsumowania; pełne dane diagnostyczne otwiera **Technical details / Szczegóły techniczne**. Stan pamięci, aktywność sesji i prawo sterowania terminalem są oddzielnymi informacjami.

Logowanie uruchamia oryginalną procedurę wybranego klienta w osobnym terminalu operacyjnym. Kody urządzenia i wynik tego terminala nie stają się pamięcią projektu ani węzłami grafu. Po zakończeniu odśwież stan. Projekt, MCP, hooki i narzędzia natywnych klientów zatwierdzasz w mechanizmach Codexa lub Claude’a. Własny agent i adapter Copilota pokazują jednorazową zgodę na dokładne polecenie. Klucze API ustawiasz w maskowanym polu; zapis lokalny jest opcjonalny i nie jest szyfrowany.

## Panel własnego agenta i Copilota

Po uruchomieniu wybranego profilu panel odtwarza historię bez zapytania do modelu. Wiadomość wysyłasz przez **Wyślij** lub `Ctrl+Enter`. Odpowiedzi, wywołania i wyniki narzędzi są wyświetlane dosłownie; sekwencje sterujące ze źródeł nie sterują terminalem. Tokeny i wykorzystanie są oznaczane jako obserwowane, a ceny jako szacunek według stawek profilu lub **brak danych**.

`Ctrl+C` lub **Interrupt / Przerwij** zatrzymuje bieżącą operację. Działanie bez potwierdzonego wyniku wymaga **Resolve / Rozstrzygnij** (`Ctrl+R`) i notatki po własnym sprawdzeniu. Zapisz notatkę przez `Ctrl+S`. Ta akcja nie wykonuje ponownie polecenia. Nowa wiadomość jest osobną kontynuacją. Model musi odczytać aktualną pamięć i wykonać dokładny ACK przed działaniami wymagającymi jej odtworzenia.

Szczegóły limitów, dostępu do plików, logowania Copilota i pamięci: [API.md](API.md).

## Inspektor i przenośna pamięć

Inspektor pokazuje rekordy, pełne źródła, surowe i znormalizowane rozmowy, metadane, historię wersji, plany, zadania, wyniki testów i kolejkę. Duże listy oraz źródła mają kolejne strony lub fragmenty. Samo przeglądanie nie wywołuje modelu, nie uruchamia testów i nie ponawia zadań.

**Potwierdź** i **Zakwestionuj** zapisują jawną ocenę z audytem oraz kontrolą wersji rekordu. Zakwestionowanie wymaga uzasadnienia; zarządzane zadanie wymaga też aktualnej wersji planu. Konflikt oznacza konieczność ponownego odczytu przed kolejną świadomą oceną. Pewność ustalenia, postęp zadania i świeżość testów są pokazywane oddzielnie: potwierdzenie ustalenia nie ustawia `done` ani nie tworzy dowodu testów.

Globalna skrzynka nieprzypisanych wpisów zachowuje źródła bez pewnego projektu. Odczyt ani wybór w grafie nie przypisuje ich do aktualnego projektu.

**Memory / Pamięć** otwiera przegląd rekordów. **More → Snapshots and export / Więcej → Snapshoty i eksport** otwiera podgląd snapshotu, zapis, sprawdzenie importu, import i ustawienia automatu. Działają także istniejące polecenia:

```bash
devbrain memory snapshot moja-aplikacja --preview
devbrain memory snapshot moja-aplikacja
devbrain memory auto moja-aplikacja --enable --interval-minutes 15
devbrain memory status moja-aplikacja
devbrain memory inspect ~/src/moja-aplikacja/.memory/devbrain
devbrain memory import ~/src/moja-aplikacja/.memory/devbrain --root ~/src/moja-aplikacja --dry-run
```

Automat pozostaje opt-in dla projektu. Snapshot zawiera jego zapisaną pamięć i historię, z pominięciem operacyjnych poświadczeń i wag modeli. Surowe rozmowy mogą zawierać dane prywatne. Import nie nadpisuje innych projektów, a przeniesione testy pozostają historyczne do czasu lokalnego sprawdzenia. [Pełne zasady pamięci przenośnej](README.md#przenośna-pamięć-projektu).

## Graf 3D w terminalu

Graf używa współrzędnych **X/Y/Z**, obrotu w przestrzeni i projekcji perspektywicznej. Wersja **0.7.1** rysuje węzły jako oświetlone bryły znakami ASCII: jasność pokazuje kształt, a bliższe powierzchnie zasłaniają dalsze. Bufor głębokości oraz korekta proporcji komórki terminala zachowują czytelność podczas obrotu, podobnie jak w klasycznej animacji terminalowego donuta. Scena przedstawia rzeczywiste węzły i połączenia pamięci; nie zastępuje ich donatem ani danymi demonstracyjnymi. Limit sceny wynosi **300 węzłów i 800 krawędzi**, a limit odświeżania **10 klatek na sekundę**. To ograniczenie sceny, nie rozmiaru pamięci.

Grupy obejmują projekty, sesje i typy danych. Można przejść do grupy, obejrzeć dalsze strony i wybrać konkretny wpis albo rewizję. Wszystkie zapisane wiadomości i historia pozostają dostępne przez grupy oraz inspektor. Surowe źródło, techniczny wpis klienta, propozycja modelu i potwierdzone ustalenie mają różne znaczenia. Strukturalne grupowanie projektów nie tworzy domniemanych relacji semantycznych między ich pamięcią.

Aktualizacje korzystają z zatwierdzonych zmian bazy; identyfikatory i kamera są zachowywane podczas odświeżania. Pobieranie danych i model wiedzy nie obsługują klawiatury natywnych klientów. Wybór węzła otwiera jego dane w inspektorze. Gdy usługa pamięci jest niedostępna, widoki danych pokazują problem, a natywne procesy mogą nadal działać.

Po przejściu do grafu przez `Ctrl+B g`:

| Klawisz w grafie | Działanie |
|---|---|
| Strzałki | Obrót widoku XYZ |
| `+` / `-` | Przybliżenie / oddalenie |
| `Spacja` | Pauza / wznowienie ruchu |
| `r` | Reset kamery |
| `]` / `[` | Wybór kolejnego / poprzedniego węzła |
| `Tab` / `Shift+Tab` | Przejście między elementami interfejsu |
| `Enter` | Inspekcja wpisu lub rozwinięcie grupy |
| `g` | Lista grup; w niej `n`/`p` przełącza strony, `Esc` zamyka |
| `n` / `p` | Kolejna / poprzednia strona sceny |
| `a` | Przełączenie widoku globalnego i projektowego |
| `Backspace` | Powrót do nadrzędnego zakresu |

Ruch jest wstrzymywany, gdy tmux jest odłączony albo graf jest ukryty przez powiększenie innego panelu lub widok kompaktowy. Po powrocie kamera i wybrany zakres pozostają zachowane.

**More → Settings → Reduce motion / Więcej → Ustawienia → Ogranicz ruch** wyłącza automatyczny obrót grafu. Możesz nadal ręcznie obracać widok i wybierać węzły.

## Odłączanie, zatrzymywanie i wznowienie

```bash
devbrain workspace status
devbrain workspace detach
devbrain workspace takeover
devbrain workspace stop
devbrain workspace stop --clients
```

`detach` lub `Ctrl+B d` odłącza terminal, zachowując workspace i działające procesy. Kolejne `devbrain` dołącza do istniejącego środowiska. `workspace stop` kończy zewnętrzny układ terminalowy; `--clients` dodatkowo zatrzymuje tylko identyfikatory klientów przejęte przez ten workspace. Nie zatrzymuje wszystkich klientów w komputerze ani dowolnych sesji spoza jego rejestru.

Jednocześnie pisze jeden terminal. Gdy środowisko jest już otwarte w innym terminalu, `devbrain` pokazuje wybór: **Observe (read only) / Obserwuj (tylko odczyt)**, **Take control / Przejmij sterowanie** albo **Return to shell / Wróć do powłoki**. Domyślne `Enter` wybiera obserwowanie. Przejęcie sterowania jawnie przekazuje klawiaturę nowemu terminalowi, a dotychczasowy przełącza w podgląd. Nie uruchamia to drugiej kopii klientów ani nowej sesji kodowania. Z podglądu możesz też odłączyć się przez `Ctrl+B d` i wykonać `devbrain workspace takeover`.

Usługa wiedzy i baza mają własne polecenia `devbrain stop` oraz `devbrain engine stop`. Zatrzymanie UI nie usuwa danych. Wyłączenie całej dystrybucji WSL kończy procesy, w tym tmux i klientów. Po ponownym uruchomieniu wybierasz w panelu bocznym pracę, wznowienie zapisanej sesji lub nową sesję. Wznowienie natywnego klienta wymaga jego zweryfikowanego identyfikatora; brak identyfikatora jest pokazywany jawnie. Własne sesje API i Copilota korzystają z dziennika i normalnego protokołu odtworzenia pamięci, bez ponownego przyznawania dawnych zgód.

Dołączenie terminala i wyświetlenie pamięci nie są ACK. Nowa lub wznowiona sesja zachowuje normalne przygotowanie pakietu, `resume_context`, sprawdzenie gotowości i dokładny `ack_resume`. `partial` lub `unavailable` blokuje zapis wspólnej pamięci. Zmiana kodu przez drugi panel może wymagać nowego pakietu i ACK. Żadna z tych operacji nie zastępuje natywnych zgód narzędzi.

## Aktualizacja i diagnostyka

Wersja **0.8.0** upraszcza nawigację i oddziela otwarcie projektu od uruchomienia asystenta. Zmiana interfejsu nie przyznaje dawnych zgód na narzędzia i nie zastępuje protokołu pamięci. [Instalacja, aktualizacja i diagnostyka](README.md).

```bash
devbrain version
devbrain workspace status
devbrain doctor
tmux -V
locale charmap
```

Jeżeli brakuje tmux, zainstaluj go w Ubuntu przez `sudo apt install tmux`. Jeżeli ekran jest zbyt mały lub graf ma nieczytelne znaki, sprawdź rozmiar okna, czcionkę i UTF-8. Błędy pamięci, modeli, zakresów archiwum oraz logowania odczytuj oddzielnie; sam komunikat o wyczerpanym limicie klienta nie oznacza utraty pamięci.

Testy deweloperskie mogą korzystać z prawdziwego tmux i ArcadeDB oraz kontrolowanych odpowiedników CLI. Nie są pomiarem płatnych API ani kont Codexa/Claude’a/Copilota i nie potwierdzają działania na fizycznym komputerze WSL użytkownika. Takie wyniki wymagają osobnego uruchomienia w docelowym środowisku.

Warstwa tłumaczenia skrótów jest dopasowana do przypiętego Textual 8. Textual udostępnia odświeżenie stopki, ale nie publiczny setter opisów istniejących bindingów; [dokumentacja opisuje ograniczenia dynamicznych bindingów](https://textual.textualize.io/guide/input/#dynamic-bindings). Mały adapter `localize_bindings` zmienia tylko ich opisy, zachowując klawisze, akcje, priorytety i identyfikatory. Przy aktualizacji Textual uruchom regresję języka i routingu klawiatury.

Przeglądarkowy interfejs 0.5 pozostaje opcją zgodności przez `devbrain cockpit open`; jego [instrukcja archiwalna](COCKPIT.md) dotyczy wyłącznie tego jawnego polecenia.
