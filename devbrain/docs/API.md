# DevBrain 0.8.0 — połączenia API, Ollama i Copilot

`devbrain` otwiera terminalowe środowisko projektu. Nazwane połączenie określa klienta, model i limity, a asystent projektu korzysta z jednego z tych połączeń. Możesz przypisać kilka asystentów i jawnie uruchamiać wybranego. Własny agent DevBrain obsługuje bezpośrednie API; Copilot ma osobny adapter oficjalnego Python SDK. Domyślny interfejs jest angielski; **More → Settings → Language → Polski** włącza polski.

## Dostępne połączenia

| Profil | Protokół / logowanie | Dostępne modele |
|---|---|---|
| Codex CLI | Natywny klient i jego logowanie ChatGPT lub konfiguracja klucza | Modele obsługiwane przez zainstalowanego Codexa |
| Claude Code | Natywny klient i jego logowanie / konfiguracja | Modele obsługiwane przez zainstalowanego Claude Code |
| OpenAI API | Responses API, osobny klucz API | Lista dostawcy lub jawny identyfikator modelu |
| Anthropic API | Messages API, osobny klucz API | Lista dostawcy lub jawny identyfikator modelu |
| API zgodne z OpenAI | Chat Completions API i własny adres | Lista serwera lub jawny identyfikator modelu |
| Ollama | Lokalne `/api/chat` i `/api/tags` | Modele zainstalowane na wskazanej Ollamie |
| GitHub Copilot | Oficjalny SDK i konto / uprawnienia Copilot | Modele dostępne dla tego konta |

Abonament ChatGPT, abonament Claude, dostęp GitHub Copilot i bezpośrednie API mają oddzielne logowania i rozliczenia. Wybór modelu Claude w Copilocie oznacza wykorzystanie połączenia i limitu Copilot. Profil OpenAI API nie otrzymuje automatycznie uprawnień abonamentu ChatGPT. GitHub Models jest inną usługą niż adapter Copilot.

Własny agent programistyczny wymaga modelu obsługującego narzędzia. Model embeddingowy nie jest takim agentem. Brak modelu lub narzędzi jest zgłaszany jawnie; DevBrain nie zmienia dostawcy w tle.

## Konfiguracja w terminalu

1. Uruchom `devbrain`, otwórz **Connections / Połączenia** i wybierz **Add connection / Dodaj połączenie**. Menu jest też dostępne przez `Ctrl+B`, potem `Spacja`.
2. Wybierz rodzaj połączenia, nadaj nazwę i podaj model. Własny adres podaj dla API zgodnego z OpenAI. Limity, stawki w USD i odwołania do kluczy znajdują się w **Advanced limits and credentials / Zaawansowane limity i poświadczenia**. Formularze zapisujesz przez `Ctrl+S`; błąd nie usuwa wpisanych wartości.
3. W menu nazwanego połączenia wybierz **Connect account / Połącz konto**, **Connect GitHub account / Połącz konto GitHub** albo **Set API key / Ustaw klucz API**, stosownie do dostawcy. Codex i Claude Code zachowują własne procedury logowania. **Check connection / Sprawdź połączenie** pokazuje dostępny stan lokalny; w Copilocie nie jest potwierdzeniem działającej sesji konta.
4. **Choose model / Wybierz model** może pobrać metadane od dostawcy i zapisać wybrany model. Nie rozpoczyna rozmowy ani edycji projektu.
5. **Test model and tools / Testuj model i narzędzia** jest osobnym, potwierdzanym zapytaniem. Może zużyć płatne tokeny lub limit konta; test nie edytuje projektu.
6. Na liście projektów `Enter` otwiera podgląd. Na ekranie projektu wybierz przypisanego asystenta albo **Add assistant / Dodaj asystenta**, a następnie **Start work / Rozpocznij pracę**. Rozpocznie pracę tylko wskazany asystent. Polecenie Start work w globalnym menu najpierw pokazuje ekran projektu do potwierdzenia.

Otwieranie TUI, wybieranie profilu oraz odtwarzanie widoku nie generuje odpowiedzi modelu. Uruchomiony panel API odtwarza lokalną historię; model rozpoczyna pracę po wysłaniu wiadomości. Zmiana profilu obowiązuje następne uruchomienie, a istniejący proces zachowuje swoją konfigurację.

Przy uruchomieniu usługi przez TUI zadania pamięci wymagające lokalnego modelu czekają na **Start work** lub **More → Queue → Process memory**. Akcja włącza kolejkę wybranego projektu. Nie uruchamia kolejki innych projektów. **Process memory** pozwala też wznowić samo przetwarzanie pamięci po błędzie połączenia, bez ponownego uruchamiania asystenta. Działające wcześniej usługi i sesje kontynuują pracę.

Domyślne adresy to `https://api.openai.com/v1`, `https://api.anthropic.com/v1` i `http://127.0.0.1:11434` dla Ollamy. Własny adres nie może zawierać hasła, klucza ani parametrów zapytania. Połączenia zdalne wymagają HTTPS; zwykły HTTP jest dostępny dla adresów loopback/localhost. Nie wyłączaj weryfikacji TLS, aby obejść problem z firmowym certyfikatem.

### Klucze i zmienne środowiskowe

**Set API key / Ustaw klucz API** otwiera maskowane, puste pole. Zapisany klucz nie jest ponownie wyświetlany. Domyślnie klucz pozostaje w pamięci bieżącego procesu brokera i przestaje być dostępny po jego zakończeniu. Zwykłe odłączenie terminala nie kończy brokera.

Opcja **Zapamiętaj lokalnie** jest domyślnie wyłączona. Zapisuje klucz w prywatnym pliku `credentials/api-keys.json` w lokalnym katalogu danych DevBrain. Katalog ma uprawnienia `0700`, a plik `0600`; **plik nie jest szyfrowany**. Nie trafia do bazy wiedzy, administracyjnego eksportu ani projektowego snapshotu. Usunięcie lokalnego klucza jest osobną akcją w menu połączenia.

W ustawieniach zaawansowanych połączenia możesz zamiast zapisu klucza podać odwołanie `env:NAZWA_ZMIENNEJ`, na przykład `env:OPENAI_API_KEY`. Zmienna musi być dostępna procesowi brokera uruchamiającemu środowisko. Zmiana zmiennej w nowej powłoce nie aktualizuje już działającego brokera. Klucze nie są przekazywane do poleceń systemowych wykonywanych przez agenta.

Pełne rozmowy i wyniki poleceń mogą zawierać prywatne dane. Redakcja znanych poświadczeń nie jest obietnicą automatycznego usunięcia wszystkich sekretów wpisanych w treści projektu lub rozmowy. Przed udostępnieniem pamięci sprawdź zakres snapshotu.

### Ollama

Wybierz **Ollama — lokalne modele** i adres działającej lokalnej usługi. Pole klucza może pozostać puste. Lista modeli pochodzi z tej instancji Ollamy. DevBrain nie pobiera brakującego modelu automatycznie.

Profil Ollamy ma w ustawieniach zaawansowanych jawne pole **Ollama context tokens / Okno kontekstu Ollamy**: domyślnie **32768 tokenów**, zakres **4096–262144**. Wartość jest wysyłana do Ollamy; DevBrain nie zgaduje rzeczywistego okna modelu. Większa wartość zwiększa zużycie RAM/VRAM. Zbyt duży kontekst jest odrzucany przed generowaniem zamiast przycinania pakietu pamięci; dopasuj profil albo zmniejsz zakres pracy.

Model kodujący w profilu jest niezależny od ustawionego w DevBrain lokalnego agenta wiedzy i modelu embeddingowego. Wspólna karta GPU ma ograniczoną pamięć; rzeczywiste opóźnienia i możliwości wybranego modelu sprawdź lokalnie. API modeli lokalnych nie ma opłaty dostawcy za tokeny, ale interfejs nie zamienia braku danych cenowych w fikcyjną wycenę.

### GitHub Copilot

W **Connections / Połączenia** wybierz nazwane połączenie **GitHub Copilot**:

- **Connect GitHub account / Połącz konto GitHub** otwiera operacyjny terminal logowania. Jeśli brakuje runtime, najpierw proponuje jego przygotowanie z osobnym potwierdzeniem pobrania. Kody urządzenia, adresy logowania i wynik tego terminala nie stają się źródłami pamięci projektu.
- **Prepare Copilot runtime / Przygotuj środowisko Copilota** pozwala osobno pobrać przypięty oficjalny runtime, sprawdzić sumy i utrwalić lokalne potwierdzenie integralności. Samo otwarcie DevBrain nie pobiera runtime.
- **Choose model / Wybierz model** pokazuje modele przyznane kontu. Uprawnienia organizacji i polityki administratora mogą ograniczać korzystanie z Copilota.

Adapter używa oficjalnego SDK Python `github-copilot-sdk`, przypiętego do zgodnej wersji w lockfile. Zestaw narzędzi i zgody kontroluje DevBrain. Narzędzia o niepotwierdzonych uprawnieniach nie otrzymują automatycznej zgody. Informacje o wykorzystaniu są pokazywane tylko wtedy, gdy raportuje je SDK; nie są niezależnym odczytem salda konta.

Logowanie kontem nie wymaga własnego klucza API. Runtime DevBrain ma osobny katalog poświadczeń i może poprosić o zgodę na ich lokalny zapis bez szyfrowania; decyzja należy do użytkownika. Wersja 0.7.2 poprawia przekazanie tej zgody do runtime. Odziedziczone zmienne `COPILOT_PROVIDER_*` oraz `COPILOT_OFFLINE` są usuwane, aby nie przełączały tego profilu na innego dostawcę API. Uprawnienia plików i sumy SHA-256 runtime są sprawdzane również po wcześniejszej udanej weryfikacji.

## Praca agenta i uprawnienia

Panel API/Copilota pokazuje historię, odpowiedź strumieniową, wywołania i wyniki narzędzi oraz stan pamięci. **Wyślij** lub `Ctrl+Enter` rozpoczyna turę. `Ctrl+C` lub **Przerwij** zatrzymuje bieżącą operację. Prefiks workspace `Ctrl+B` i skróty paneli nadal działają; do przekazania natywnego `Ctrl+B` użyj go dwukrotnie.

Agent może przeglądać, tworzyć, edytować i usuwać pliki wybranego worktree. Narzędzia plikowe sprawdzają ścieżki i dowiązania, a zmiana istniejącego pliku wymaga zgodności z hashem jego odczytanej wersji. Bezpośrednie zapisy do `.git/` i zarządzanego `.memory/devbrain/` są blokowane. Zmiana pliku przez drugiego klienta wymaga ponownego odczytu zamiast nadpisania nowszej pracy.

Każde polecenie systemowe, także uruchomienie testów, wymaga **jednorazowej zgody**. Ekran pokazuje dokładne polecenie, katalog, sesję i generację. Zgoda dotyczy tej konkretnej operacji; zmiana parametrów wymaga nowej zgody. Polecenie działa z uprawnieniami użytkownika — ten mechanizm nie jest izolacją systemową. Procesy natywnego Codexa i Claude Code zachowują własne polityki narzędzi.

Domyślne limity własnego silnika to 20 zapytań modelu na turę, 4096 tokenów wyjściowych na zapytanie i 120 sekund na polecenie. Profil Copilota ma domyślnie 20 wywołań narzędzi i 900 sekund na turę. Formularze pozwalają zmienić obsługiwane limity. Liczby wewnętrznych zapytań Copilota nie są deklarowane jako znane, gdy SDK ich nie ujawnia.

Osiągnięcie limitu zatrzymuje turę; kolejną kontynuację rozpoczynasz wiadomością. Tokeny pochodzą z raportu dostawcy. Koszt jest szacunkiem na podstawie opcjonalnych stawek profilu; bez stawek lub danych wykorzystania widnieje **brak danych**. Nie jest to gwarantowany limit kwoty pobranej przez dostawcę.

## Pamięć i powrót po przerwaniu

Lokalny katalog danych DevBrain musi znajdować się poza katalogami projektów, np. w `~/.local/share/devbrain`. Nowy agent odmawia startu, gdy jego magazyn znajduje się wewnątrz wybranego projektu; zapis trwałego klucza sprawdza również pozostałe zarejestrowane katalogi. Dzięki temu pliki konfiguracji i lokalne poświadczenia nie stają się plikami roboczymi agenta.

Każdy silnik używa wspólnej pamięci projektu, z rzeczywistym odczytem `resume_context` i dokładnym ACK modelu. Dostarczenie kontekstu przez UI lub SDK nie jest ACK. Częściowy pakiet, niezgodny odczyt lub zmiana istotnego stanu blokuje zapisy i działania wymagające odtworzonego kontekstu.

Schemat `ack_resume` wymaga `readback` z polami `plan` i `task`: dokładne `id` oraz `version` z odpowiedniego aktywnego elementu pakietu albo `null`, gdy elementu nie ma. Wersja 0.7.2 podaje ten kształt wprost modelom API i Copilota; potwierdzenie nadal sprawdza rzeczywisty serwer pamięci.

Wiadomości, kompletne wywołania narzędzi i wyniki są utrwalane w prywatnym dzienniku z kontrolą integralności. Rozpoczęcie działania jest zapisywane przed wykonaniem, a wynik przed kolejnym krokiem modelu. Rozmowy i dowody są archiwizowane jako źródła DevBrain i dostępne w inspektorze oraz przenośnej pamięci.

`Ctrl+B d` odłącza interfejs i zachowuje działający proces. Wyłączenie WSL kończy procesy. Późniejsze **Sesje → Wznów zapisaną sesję** odtwarza pamięć i wymaga nowego protokołu ACK. Dziennik nie przyznaje dawnych zgód na polecenia.

Działanie rozpoczęte bez potwierdzonego wyniku ma stan niepewny. **Resolve / Rozstrzygnij** (`Ctrl+R`) pokazuje takie działania i wymaga notatki po własnym sprawdzeniu, np. czy plik powstał lub test się zakończył. `Ctrl+S` zapisuje notatkę; błąd zapisu pozostawia wpisany tekst. Zapis tej notatki nie uruchamia ponownie polecenia. Dopiero osobna wiadomość kontynuuje pracę. Nie powtarzaj płatnego zapytania ani polecenia wyłącznie dlatego, że połączenie zostało zerwane — poprzednia operacja mogła dojść do skutku.

Przy skracaniu kontekstu silnik zachowuje źródła i checkpoint, a nowy kontekst buduje z aktualnej pamięci i potrzebnych źródeł. Pamięć nie usuwa ograniczeń kontekstu ani kosztów modelu. Snapshot przenosi zapisane rozmowy, ustalenia i dowody; nie przenosi kluczy, profili połączeń, aktywnych procesów i zgód. Importowane testy pozostają historyczne.

## Sprawdzenie po instalacji

Najpierw odtwórz znany projekt i sprawdź istniejące panele Codexa/Claude’a. Następnie skonfiguruj wybrany nowy profil, sprawdź stan, wykonaj jawny test modelu i krótką turę z odczytem pliku oraz odrzuconym poleceniem. Sprawdź, czy drugi klient odtwarza zapisane ustalenie ze źródłem. Odłącz i ponownie otwórz `devbrain`.

Testy automatyczne używają kontrolowanych odpowiedzi HTTP/SDK, prawdziwego ArcadeDB i tmux. Nie zużywają płatnych limitów. Rzeczywiste połączenie, modele na konkretnej karcie GPU oraz konta Copilot/OpenAI/Anthropic wymagają osobnego jawnego sprawdzenia użytkownika. Brak konta, modelu lub dostępnego limitu jest wynikiem **not_run**, a nie zaliczonym testem.
