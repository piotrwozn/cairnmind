"""Small explicit terminal catalog. User content and API identifiers are never translated."""
from __future__ import annotations


CATALOG = {
    "Invalid assistant selections": "Niepoprawna lista wyborów asystenta",
    "Invalid assistant selection": "Niepoprawny wybór asystenta",
    "Invalid assistant worktree": "Niepoprawny katalog pracy asystenta",
    "Duplicate assistant selection scope": "Powtórzony wybór asystenta dla tego samego projektu i katalogu",
    "Resume session": "Wróć do sesji",
    "Resume returns to the running process. Model changes apply to its next launch.":
        "Powrót otwiera działający proces. Zmiana modelu dotyczy następnego uruchomienia.",
    "Returned to the running session. No process was restarted.": "Wrócono do działającej sesji. Proces nie został uruchomiony ponownie.",
    "Project opened. Choose Resume session to return.": "Projekt otwarty. Wybierz Wróć do sesji.",
    "Session ended or changed. Review the project before choosing Start work.":
        "Sesja zakończyła się lub zmieniła. Sprawdź projekt przed wybraniem Rozpocznij pracę.",
    "Session": "Sesja", "Connection": "Połączenie", "Control": "Sterowanie",
    "None": "Brak", "Unknown": "Nieznany", "Configured": "Skonfig.",
    "Running": "Działa", "Starting": "Startuje", "Stopping": "Kończy", "Ended": "Koniec",
    "Failed": "Błąd", "Interrupted": "Przerwana", "Stale": "Nieaktualna", "Partial": "Częściowa",
    "ACK ready": "ACK gotowa", "ACK needed": "Brak ACK", "Not loaded": "Niewczytana",
    "Offline": "Offline", "Login needed": "Zaloguj", "Ready(local)": "Gotowe(lok.)",
    "Limited": "Limit",
    "You": "Ty", "Other": "Inny", "Read only": "Tylko odczyt", "UI": "UI",
    "Process memory": "Przetwórz pamięć",
    "Assistant started. Memory processing may still be paused. Use More → Queue → Process memory.":
        "Asystent został uruchomiony. Przetwarzanie pamięci może nadal być odroczone. Wybierz Więcej → Kolejka → Przetwórz pamięć.",
    "Use local models to process pending memory for this project only. No assistant is started.":
        "Użyj lokalnych modeli do przetworzenia oczekującej pamięci tylko tego projektu. Asystent nie zostanie uruchomiony.",
    "Project changed. Open Queue again.": "Projekt się zmienił. Otwórz Kolejkę ponownie.",
    "Memory processing could not be enabled.": "Nie udało się włączyć przetwarzania pamięci.",
    "Memory processing enabled for this project.": "Włączono przetwarzanie pamięci tego projektu.",
    "Background memory processing is disabled in this service.": "Przetwarzanie pamięci w tle jest wyłączone w tej usłudze.",
    "Projects": "Projekty", "Project": "Projekt", "Open project": "Otwórz projekt",
    "Search projects…": "Szukaj projektu…", "Add folder": "Dodaj folder",
    "Start work": "Rozpocznij pracę", "Open": "Otwórz", "Back": "Wróć",
    "Connections": "Połączenia", "Memory": "Pamięć", "More": "Więcej",
    "Ask memory": "Zapytaj pamięć",
    "Save a finding": "Zapisz ustalenie", "Finding": "Ustalenie",
    "Open a project first.": "Najpierw otwórz projekt.",
    "Write the finding first.": "Najpierw wpisz ustalenie.",
    "The finding is longer than 16000 characters.": "Ustalenie ma więcej niż 16000 znaków.",
    "Choose a kind: decision, fact, constraint, question or preference.":
        "Wybierz rodzaj: decision, fact, constraint, question lub preference.",
    "Choose a status: proposed or confirmed.": "Wybierz stan: proposed lub confirmed.",
    "Finding saved to memory: {kind}, {status}.": "Ustalenie zapisane w pamięci: {kind}, {status}.",
    "A finding needs a registered project worktree": "Ustalenie wymaga zarejestrowanego katalogu roboczego projektu",
    "Invalid finding key": "Niepoprawny klucz ustalenia",
    "Could not read the workspace state.": "Nie udało się odczytać stanu środowiska.",
    "This form already saved a different text, kind or status; the first one is in memory. "
    "Open a new form to save the changed one.":
        "Ten formularz zapisał już inny tekst, rodzaj lub stan; pierwszy jest w pamięci. "
        "Otwórz nowy formularz, aby zapisać zmieniony.",
    "This project is no longer registered.": "Ten projekt nie jest już zarejestrowany.",
    "No clear answer from the memory service. The finding may have been saved; saving again "
    "from this form is safe and will not store it twice.":
        "Brak jednoznacznej odpowiedzi usługi pamięci. Ustalenie mogło zostać zapisane; ponowny zapis "
        "z tego formularza jest bezpieczny i nie utworzy go drugi raz.",
    "Question answered from project memory with sources": "Odpowiedź z pamięci projektu ze źródłami",
    "Question": "Pytanie", "Ask": "Zapytaj", "Memory answer": "Odpowiedź pamięci",
    "Answers come only from this project's memory, with cited sources. Nothing is saved.":
        "Odpowiedzi pochodzą wyłącznie z pamięci tego projektu i mają źródła. Nic nie jest zapisywane.",
    "Impact": "Wpływ zmian", "Compare with": "Porównaj z", "Pending only": "Tylko bez oceny",
    "Kind / status": "Rodzaj / stan", "Record": "Ustalenie", "Linked to": "Powiązane z", "Checked": "Sprawdzone",
    "Verdict": "Ocena", "Save verdict": "Zapisz ocenę", "not checked": "nie sprawdzone",
    "still valid": "nadal aktualne", "needs update": "wymaga zmiany", "not related": "niezwiązane",
    "Still valid": "Nadal aktualne", "Not related": "Niezwiązane",
    "Needs update: say what to update": "Wymaga zmiany: napisz, co zmienić",
    "What should be updated": "Co trzeba zmienić", "Note (optional)": "Notatka (opcjonalnie)",
    "Not checked yet.": "Jeszcze nie sprawdzone.", "checked: {verdict} ({day})": "sprawdzone: {verdict} ({day})",
    "Linked to what changed": "Powiązania ze zmianami", "names the file": "wskazuje plik",
    "names {symbol} in": "wskazuje {symbol} w", "(link {status})": "(powiązanie {status})",
    "rests on a snapshot from {day} of": "opiera się na kopii z {day} pliku",
    "rests on a snapshot of": "opiera się na kopii pliku",
    "its definition changed": "jego definicja się zmieniła",
    "it is no longer defined there": "nie jest już tam zdefiniowany",
    "it is newly defined there": "jest tam nowo zdefiniowany",
    "it is now defined in the renamed file": "jest teraz zdefiniowany w pliku o nowej nazwie",
    "its definition could not be compared": "nie można porównać jego definicji",
    "A link means \"worth checking\", not that the record is wrong.":
        "Powiązanie znaczy „warto sprawdzić”, a nie że ustalenie jest błędne.",
    "Compared with {since} ({commit}).": "Porównano z {since} ({commit}).",
    "Comparing the worktree with {since}…": "Porównywanie katalogu roboczego z {since}…",
    "Could not read the impact list.": "Nie udało się odczytać listy wpływu zmian.",
    "Nothing in this worktree differs from {since} ({commit}). Enter another commit to compare with, "
    "for example main or HEAD~3.":
        "Nic w tym katalogu roboczym nie różni się od {since} ({commit}). Wpisz inny commit do porównania, "
        "na przykład main albo HEAD~3.",
    "No entry without a verdict since {since} ({commit}).": "Brak wpisów bez oceny od {since} ({commit}).",
    "No record is linked to what changed since {since} ({commit}).":
        "Żadne ustalenie nie jest powiązane ze zmianami od {since} ({commit}).",
    "{count} records worth re-checking since {since} ({commit}). A link means \"worth checking\", not that the "
    "record is wrong.":
        "Ustalenia warte ponownego sprawdzenia od {since} ({commit}): {count}. Powiązanie znaczy „warto "
        "sprawdzić”, a nie że ustalenie jest błędne.",
    "{count} documentation pages mention a changed definition (cairnmind impact lists them).":
        "Strony dokumentacji wspominające zmienioną definicję: {count} (wypisuje je cairnmind impact).",
    "Verdict saves your judgement of this entry as an audit note. It changes no record, no status and no task.":
        "Ocena zapisuje Twój osąd tego wpisu jako notatkę audytową. Nie zmienia żadnego ustalenia, stanu ani "
        "zadania.",
    "Open an entry of the list first.": "Najpierw otwórz wpis z listy.",
    "This entry cannot be judged as shown. Press r to read the list again.":
        "Tego wpisu nie można ocenić w tej postaci. Naciśnij r, aby wczytać listę ponownie.",
    "Saved as an audit note for this entry as listed. No record, status or task is changed.":
        "Zapisywana jako notatka audytowa dla wpisu w tej postaci. Żadne ustalenie, stan ani zadanie się nie zmienia.",
    "Needs update requires a note on what to update. Nothing was saved.":
        "Ocena „wymaga zmiany” wymaga notatki, co zmienić. Niczego nie zapisano.",
    "The code or the record changed, the list was reloaded. Nothing was saved.":
        "Kod lub ustalenie się zmieniły, wczytano listę ponownie. Niczego nie zapisano.",
    "The verdict was not saved.": "Ocena nie została zapisana.",
    "The impact list is not available here.": "Lista wpływu zmian nie jest tu dostępna.",
    "A verdict is being saved. Wait for its answer.": "Ocena jest zapisywana. Poczekaj na odpowiedź.",
    "This record is no longer in the list, the list was reloaded. Nothing was saved.":
        "Tego ustalenia nie ma już na liście, wczytano listę ponownie. Niczego nie zapisano.",
    "No clear answer from the memory service. The verdict may have been saved; the list was reloaded and shows it "
    "if so. Saving the same verdict again is safe and stores nothing twice.":
        "Brak jednoznacznej odpowiedzi usługi pamięci. Ocena mogła zostać zapisana; wczytano listę ponownie i jeśli "
        "tak, pokazuje ją. Ponowny zapis tej samej oceny jest bezpieczny i niczego nie powtórzy.",
    "This verdict was already saved.": "Ta ocena była już zapisana.",
    "Verdict saved: {verdict}. No record, status or task was changed.":
        "Ocena zapisana: {verdict}. Żadne ustalenie, stan ani zadanie się nie zmieniło.",
    "Conflicts": "Sprzeczności", "Resolve": "Rozstrzygnij", "Resolve conflict": "Rozstrzygnij sprzeczność",
    "Possible conflict": "Możliwa sprzeczność", "new finding": "nowe ustalenie",
    "existing finding": "dotychczasowe ustalenie", "Scope": "Zakres", "whole project": "cały projekt",
    "Keep A: it replaces B and becomes confirmed": "Zostaw A: zastępuje B i zostaje potwierdzone",
    "Keep B: it replaces A and becomes confirmed": "Zostaw B: zastępuje A i zostaje potwierdzone",
    "This conflict is no longer pending.": "Ta sprzeczność nie czeka już na decyzję.",
    "(the text is longer; open the record to read all of it)":
        "(tekst jest dłuższy; otwórz ustalenie, aby przeczytać całość)",
    "Both are valid in different scopes": "Oba są ważne w różnych zakresach",
    "False alarm: they do not contradict": "Fałszywy alarm: nie są sprzeczne",
    "Describe the two scopes": "Opisz oba zakresy", "Save decision": "Zapisz decyzję",
    "Open a conflict first.": "Najpierw otwórz sprzeczność.",
    "Decision saved with an audit note.": "Decyzja zapisana z notatką audytową.",
    "The decision was not saved. The current list was reloaded.":
        "Decyzja nie została zapisana. Wczytano aktualną listę.",
    "The decision is saved with an audit note; every source is kept.":
        "Decyzja jest zapisywana z notatką audytową; wszystkie źródła zostają.",
    "Resolve records your decision with an audit note. No source is removed.":
        "Rozstrzygnięcie zapisuje Twoją decyzję z notatką audytową. Żadne źródło nie jest usuwane.",
    "{letter} has the newer evidence; both times were stated by their sources.":
        "{letter} ma nowsze dowody; oba czasy pochodzą ze źródeł.",
    "{letter} was recorded later, but its evidence time is only an upper bound.":
        "{letter} zapisano później, ale czas jego dowodów to tylko górna granica.",
    "The order of the evidence cannot be compared.": "Kolejności dowodów nie da się porównać.",
    "Evidence": "Dowody",
    "at least one supporting source still stands": "co najmniej jedno źródło nadal obowiązuje",
    "imported file evidence has not been compared with local files yet":
        "zaimportowanych dowodów z plików nie porównano jeszcze z lokalnymi plikami",
    "the source changed; the quoted passage is still there": "źródło się zmieniło; cytowany fragment nadal w nim jest",
    "the source changed; the quoted passage is gone": "źródło się zmieniło; cytowanego fragmentu już w nim nie ma",
    "the source changed; there is no verified quoted passage to compare":
        "źródło się zmieniło; brak zweryfikowanego cytatu do porównania",
    "orphaned: no supporting source has a current version":
        "osierocone: żadne źródło nie ma aktualnej wersji",
    "written by an external client's session, or resting only on one; never verified":
        "zapisane przez sesję zewnętrznego klienta albo oparte tylko na niej; nigdy niezweryfikowane",
    "Brief": "Skrót",
    "Where work stopped: checkpoint, next task, checks and commits":
        "Gdzie przerwano pracę: punkt kontrolny, następne zadanie, testy i commity",
    "The memory service returned no brief.": "Usługa pamięci nie zwróciła skrótu.",
    "Asking memory…": "Pytam pamięć…", "Missing evidence": "Brakujące dowody",
    "No verified answer.": "Brak zweryfikowanej odpowiedzi.",
    "The memory service returned no answer.": "Usługa pamięci nie zwróciła odpowiedzi.",
    "Answer words found in quotes": "Słowa odpowiedzi obecne w cytatach",
    "Settings": "Ustawienia", "Sessions": "Sesje", "Assistant": "Asystent",
    "Choose assistant": "Wybierz asystenta", "Add assistant": "Dodaj asystenta",
    "No projects yet. Add a folder to begin.": "Brak projektów. Dodaj folder, aby zacząć.",
    "No matching projects. Clear the search or add a folder.": "Brak pasujących projektów. Wyczyść wyszukiwanie lub dodaj folder.",
    "Select a visible project first.": "Najpierw wybierz widoczny projekt.",
    "Enter opens a project. Starting work is a separate action.": "Enter otwiera projekt. Rozpoczęcie pracy jest osobną akcją.",
    "Browsing memory does not start an assistant.": "Przeglądanie pamięci nie uruchamia asystenta.",
    "Choose an assistant before starting work.": "Wybierz asystenta przed rozpoczęciem pracy.",
    "Working folder": "Katalog pracy", "Folder": "Folder", "Project name": "Nazwa projektu",
    "Name (optional; defaults to folder name)": "Nazwa (opcjonalnie; domyślnie nazwa folderu)",
    "Browse folders": "Przeglądaj foldery", "Use this folder": "Wybierz ten folder",
    "Parent folder": "Folder nadrzędny", "Folder path (Windows or WSL)": "Ścieżka folderu (Windows lub WSL)",
    "Enter a folder path, then press Enter to browse.": "Wpisz ścieżkę folderu i naciśnij Enter, aby go przeglądać.",
    "This folder does not exist or cannot be read.": "Ten folder nie istnieje lub nie można go odczytać.",
    "No subfolders. You can use this folder.": "Brak podfolderów. Możesz wybrać ten folder.",
    "Cancel": "Anuluj", "Close": "Zamknij", "Save": "Zapisz", "Continue": "Dalej",
    "Confirm": "Potwierdź", "Refresh": "Odśwież", "Details": "Szczegóły",
    "Technical details": "Szczegóły techniczne", "Hide technical details": "Ukryj szczegóły techniczne",
    "Check the highlighted field.": "Sprawdź zaznaczone pole.", "Required field": "Pole wymagane",
    "Saving…": "Zapisywanie…", "Could not save. Your entries were kept.": "Nie udało się zapisać. Wpisane wartości zachowano.",
    "Operation completed.": "Operacja zakończona.", "Operation needs attention.": "Operacja wymaga uwagi.",
    "Status": "Stan", "Ready": "Gotowy", "Unavailable": "Niedostępny",
    "Connected": "Połączono", "Not connected": "Niepołączony", "No data": "Brak danych",
    "Advanced": "Zaawansowane", "Edit connection": "Edytuj połączenie", "Add connection": "Dodaj połączenie",
    "Connection type": "Rodzaj połączenia", "Connection name": "Nazwa połączenia",
    "Model": "Model", "Model (blank = provider default)": "Model (puste = domyślny dostawcy)",
    "Connect account": "Połącz konto", "Connect GitHub account": "Połącz konto GitHub",
    "Prepare Copilot runtime": "Przygotuj środowisko Copilota", "Choose model": "Wybierz model",
    "Check connection": "Sprawdź połączenie", "Test model and tools": "Testuj model i narzędzia",
    "Set API key": "Ustaw klucz API", "Remove saved key": "Usuń zapisany klucz",
    "Changes apply to the next launch. Saving does not start a model.": "Zmiany dotyczą następnego uruchomienia. Zapis nie uruchamia modelu.",
    "Advanced limits and credentials": "Zaawansowane limity i poświadczenia",
    "Connection saved. You can connect it or choose it for your project.": "Połączenie zapisane. Możesz się zalogować lub wybrać je w projekcie.",
    "An assistant was added. Existing assignments were preserved.": "Dodano asystenta. Istniejące przypisania zachowano.",
    "Starting the selected assistant…": "Uruchamianie wybranego asystenta…",
    "Project opened. Choose Start work when ready.": "Projekt otwarty. Gdy będziesz gotowy, wybierz Rozpocznij pracę.",
    "Your existing sessions keep running when you browse another project.": "Istniejące sesje działają dalej podczas przeglądania innego projektu.",
    "Find folders": "Szukaj folderów", "Import memory": "Importuj pamięć",
    "Snapshots and export": "Snapshoty i eksport", "Manage assistants": "Zarządzaj asystentami",
    "Language": "Język", "Layout": "Układ", "Focus": "Skupienie", "Cockpit": "Kokpit",
    "Reduce motion": "Ogranicz ruch", "On": "Włączone", "Off": "Wyłączone",
    "Preferences saved.": "Preferencje zapisane.", "Return to projects": "Wróć do projektów",
    "This workspace is open in another terminal.": "To środowisko jest otwarte w innym terminalu.",
    "Choose read-only observation or explicitly take control. Existing assistants keep running.": "Wybierz podgląd albo jawnie przejmij sterowanie. Istniejący asystenci działają dalej.",
    "Observe (read only)": "Obserwuj (tylko odczyt)", "Take control": "Przejmij sterowanie", "Return to shell": "Wróć do powłoki",
    "Ctrl+B then Space: menu · Tab: next · Esc: back": "Ctrl+B, potem Spacja: menu · Tab: dalej · Esc: wstecz",
    "Use named connections; no identifiers are needed.": "Wybieraj połączenia po nazwie; identyfikatory nie są potrzebne.",
    "New connection": "Nowe połączenie", "New project": "Nowy projekt", "Global memory": "Pamięć globalna",
    "Records": "Ustalenia", "Sources": "Źródła", "Plans": "Plany", "Tasks": "Zadania",
    "Stated": "Dowód z dnia", "Observed no later than": "Zaobserwowano nie później niż",
    "Mixed evidence": "Dowody mieszane", "Warning": "Ostrzeżenie",
    "The superseded finding had newer evidence": "Zastąpione ustalenie miało nowsze dowody",
    "Checks": "Testy", "Queue": "Kolejka", "Unassigned": "Nieprzypisane", "Snapshot": "Snapshot",
    "Filter loaded page…": "Filtruj załadowaną stronę…", "List": "Lista", "Filters": "Filtry",
    "Dispute": "Zakwestionuj", "History": "Historia", "Preview": "Podgląd", "Automatic": "Automat",
    "Import": "Import", "Apply": "Wczytaj", "Type / state": "Typ / stan",
    "Content / source": "Treść / źródło", "Version / attempt": "Wersja / próba",
    "Version": "Wersja", "Graph": "Graf", "Groups": "Grupy", "Pause": "Pauza",
    "Next": "Dalej", "Previous": "Wstecz", "Next node": "Następny węzeł", "Previous node": "Poprzedni węzeł",
    "Reset camera": "Reset kamery", "Camera": "Kamera", "Global": "Globalny", "Parent": "Wyżej",
    "Next groups": "Dalsze grupy", "More groups": "Więcej grup", "Previous groups": "Poprzednie grupy",
    "Zoom in": "Przybliż", "Zoom out": "Oddal", "+ zoom": "+ zoom", "− zoom": "− zoom",
    "Memory groups · Enter selects · n/p pages · Esc returns": "Grupy pamięci · Enter wybiera · n/p strony · Esc wraca",
    "Memory groups · Enter: select · n/p: pages · Esc: back": "Grupy pamięci · Enter: wybierz · n/p: strony · Esc: wróć",
    "Scope changed; return and select the group again.": "Zakres zmienił się; wróć i ponownie wybierz grupę.", "groups": "grup",
    "Local setup only. Use Connect GitHub account to sign in; Choose model checks available models.": "To stan lokalnej konfiguracji. Połącz konto GitHub służy do logowania; Wybierz model sprawdza dostępne modele.",
    "The project changed. Close this form and open it again.": "Projekt zmienił się. Zamknij formularz i otwórz go ponownie.",
    "Enter a whole number from 1 to 10080.": "Wpisz liczbę całkowitą od 1 do 10080.",
    "{count} items · read only": "{count} wpisów · tylko odczyt", "More pages available": "Dalsza strona dostępna",
    "Review changes confidence; it does not complete a task.": "Ocena zmienia pewność; nie kończy zadania.",
    "Review was not retried. The current version was reloaded.": "Ocena nie została ponowiona. Odczytano aktualną wersję.",
    "Memory updates": "Aktualizacje pamięci", "Account login": "Logowanie konta",
    "Memory acknowledged": "Pamięć potwierdzona", "Memory acknowledgement required": "Wymagane potwierdzenie pamięci",
    "Memory graph · Connecting…": "Graf pamięci · Łączenie…", "Memory graph": "Graf pamięci",
    "No nodes in this scope.": "Brak węzłów w tym zakresie.", "nodes": "węzłów", "relationships": "relacji",
    "paused": "pauza", "moving": "ruch", "n: next page": "n: następna strona", "limited view, g: groups": "ograniczony widok, g: grupy",
    "Workspace": "Praca", "Second assistant": "Drugi asystent", "Search projects": "Szukaj projektu",
    "Commands": "Polecenia", "Keyboard help": "Pomoc klawiatury", "Type to find a command…": "Wpisz, aby znaleźć polecenie…",
    "Choose or open a project": "Wybierz lub otwórz projekt", "Return to your assistant": "Wróć do asystenta",
    "Show the second visible assistant": "Pokaż drugiego widocznego asystenta", "Facts, tasks, plans and sources": "Ustalenia, zadania, plany i źródła",
    "Explore relationships in memory": "Przeglądaj powiązania pamięci", "Find a registered project": "Znajdź zapisany projekt",
    "Register a project folder": "Dodaj folder projektu", "Explicitly start the selected assistant": "Jawnie uruchom wybranego asystenta",
    "Review assistant before starting": "Sprawdź asystenta przed rozpoczęciem",
    "Review your assistant before starting": "Sprawdź asystenta przed rozpoczęciem",
    "Accounts, assistants and models": "Konta, asystenci i modele", "Return to or manage an existing session": "Wróć do sesji lub nią zarządzaj",
    "Snapshots, import and advanced tools": "Snapshoty, import i narzędzia zaawansowane", "Language, layout and reduced motion": "Język, układ i ograniczenie ruchu",
    "Focus view": "Widok skupienia", "Cockpit view": "Widok kokpitu", "One view at a time": "Jeden widok naraz",
    "Multiple panes on a large terminal": "Wiele paneli w dużym terminalu", "Navigation and native client shortcuts": "Nawigacja i skróty klientów natywnych",
    "Replace connection": "Zmień połączenie", "Move earlier": "Przesuń wcześniej", "Remove from layout": "Usuń z układu",
    "Remove from layout?": "Usunąć z układu?", "Remove": "Usuń", "Keep at least one assistant in the layout.": "Zostaw co najmniej jednego asystenta w układzie.",
    "This hides the assignment. Existing processes are not stopped.": "Usuwa przypisanie z układu. Istniejące procesy nie są zatrzymywane.",
    "Enter a positive whole number.": "Wpisz dodatnią liczbę całkowitą.",
    "Account login opened. Credentials stay outside project memory.": "Otworzono logowanie konta. Poświadczenia pozostają poza pamięcią projektu.",
    "Snapshots contain complete conversations and sources. Review before sharing.": "Snapshoty zawierają pełne rozmowy i źródła. Sprawdź je przed udostępnieniem.",
    "Import checked. Review the details, then choose Apply.": "Import sprawdzony. Przejrzyj szczegóły, a następnie wybierz Wczytaj.",
    "Imported checks are historical.": "Importowane testy pozostają historyczne.",
    "Codex — ChatGPT account": "Codex — konto ChatGPT", "Claude Code — Claude account": "Claude Code — konto Claude",
    "GitHub Copilot — GitHub account": "GitHub Copilot — konto GitHub", "Ollama — local models": "Ollama — modele lokalne",
    "OpenAI — API key": "OpenAI — klucz API", "Anthropic — API key": "Anthropic — klucz API", "OpenAI-compatible API": "API zgodne z OpenAI",
    "Ctrl+B, then Space: search all commands\n"
    "Ctrl+B, then p / 1 / 2 / i / g: projects / assistant / second assistant / memory / graph\n"
    "Ctrl+B, then Tab: next view\nCtrl+B, then z: expand or restore panes\n"
    "Ctrl+B, then n: save a finding to memory (Ctrl+S saves, Esc closes)\n"
    "Ctrl+B, then [: scroll and copy terminal history (q to leave)\n"
    "Ctrl+B, then d: detach; run cairnmind to return\n"
    "Ctrl+B, then Ctrl+B: send Ctrl+B to the native assistant\n\n"
    "Cairnmind controls: Tab / Shift+Tab move focus; Enter chooses; Esc goes back.\n"
    "Projects: Enter opens; Start work starts only the chosen assistant.\n"
    "Graph: [ / ] select nodes; arrows rotate; Space pauses; Enter opens a node.\n"
    "Native assistants keep their own shortcuts, paste, Ctrl+C and approval dialogs.\n"
    "Memory readiness, session activity and terminal control are separate states.":
    "Ctrl+B, potem Spacja: wyszukaj polecenie\n"
    "Ctrl+B, potem p / 1 / 2 / i / g: projekty / asystent / drugi asystent / pamięć / graf\n"
    "Ctrl+B, potem Tab: następny widok\nCtrl+B, potem z: powiększ lub przywróć panele\n"
    "Ctrl+B, potem n: zapisz ustalenie w pamięci (Ctrl+S zapisuje, Esc zamyka)\n"
    "Ctrl+B, potem [: przewijaj i kopiuj historię terminala (q wraca)\n"
    "Ctrl+B, potem d: odłącz; uruchom cairnmind, aby wrócić\n"
    "Ctrl+B, potem Ctrl+B: przekaż Ctrl+B do natywnego asystenta\n\n"
    "Cairnmind: Tab / Shift+Tab zmienia fokus; Enter wybiera; Esc wraca.\n"
    "Projekty: Enter otwiera; Rozpocznij pracę uruchamia tylko wybranego asystenta.\n"
    "Graf: [ / ] wybiera węzły; strzałki obracają; Spacja wstrzymuje; Enter otwiera węzeł.\n"
    "Natywni asystenci zachowują własne skróty, wklejanie, Ctrl+C i zgody.\n"
    "Gotowość pamięci, aktywność sesji i sterowanie terminalem to oddzielne stany.",
}

# Existing memory workflows retain their labels as aliases while sharing the
# same catalog. This only translates explicit UI text, never record content.
LEGACY = {
    "stale_workspace_revision: odczytaj bieżący stan i ponów operację.":
        "Workspace changed. Refresh its current state and try again.",
    "Pracuj": "Start work", "Podgląd": "Preview", "Dodaj": "Add folder", "Loguj": "Connections",
    "PROJEKTY · lokalne": "Projects", "Wybierz projekt": "Select a visible project first.",
    "Pamięć globalna": "Global memory", "Wybierz projekt z listy.": "Select a visible project first.",
    "Wybierz projekt w panelu projektów.": "Select a project in Projects.",
    "Klucz": "API key", "Ustaw klucz": "Set API key", "Nowy klucz (zapisany klucz nie jest wyświetlany)": "New key (saved keys are never displayed)",
    "Zapamiętaj lokalnie": "Remember on this computer", "Wpisz nowy klucz.": "Enter a new key.",
    "Klucz pozostaje poza pamięcią projektu. Domyślnie działa tylko podczas bieżącego działania silnika.": "The key stays outside project memory. By default it lasts only for this engine session.",
    "Opcja zapisu tworzy prywatny plik 0600 poza projektem. Plik nie jest szyfrowany i nie trafia do snapshotu.": "Remembering creates a private, unencrypted file outside the project. It is not included in snapshots.",
    "Nazwa profilu": "Connection name", "Klient: codex / claude": "Client: codex / claude",
    "Model (puste = domyślny klienta)": "Model (blank = provider default)",
    "Model (puste = domyślny Copilota)": "Model (blank = provider default)", "Adres API": "API address",
    "Odwołanie do klucza: env:NAZWA lub profile:ID (puste = bez klucza)": "Credential reference: env:NAME or profile:ID (blank = no key)",
    "Maksymalna liczba zapytań modelu w turze": "Maximum model calls per turn",
    "Maksymalna liczba tokenów odpowiedzi": "Maximum output tokens", "Okno kontekstu Ollamy (tokeny; zużywa RAM/VRAM)": "Ollama context tokens (uses RAM/VRAM)",
    "Maksymalna liczba narzędzi w turze": "Maximum tool calls per turn", "Maksymalny czas tury (sekundy)": "Maximum turn duration (seconds)",
    "Limit czasu polecenia (sekundy)": "Command timeout (seconds)", "Cena wejścia USD za milion tokenów (opcjonalnie)": "Input USD per million tokens (optional)",
    "Cena wyjścia USD za milion tokenów (opcjonalnie)": "Output USD per million tokens (optional)",
    "Projekty z wybranych katalogów": "Projects in selected folders", "Skanowanie…": "Scanning…", "Wybierz": "Select",
    "Anuluj skan": "Cancel scan", "Katalog / pamięć": "Folder / memory", "Więcej": "More",
    "Uzupełnij: ": "Required: ", "Operacja nie została ukończona: ": "Operation did not complete: ",
    "Katalog pracy": "Working folder", "Filtry listy": "List filters",
    "Potwierdź ustalenie": "Confirm record", "Zakwestionuj ustalenie": "Dispute record",
    "Widok źródła": "Source view", "Źródła ustalenia": "Record sources",
    "Automatyczny snapshot": "Automatic snapshot", "Sprawdź import pamięci": "Check memory import",
    "Katalog snapshotu lub manifest.json": "Snapshot folder or manifest.json", "Jawny katalog docelowy": "Destination folder",
    "Lokalna nazwa (opcjonalnie)": "Local name (optional)", "Nazwa lokalna (opcjonalnie)": "Local name (optional)",
    "Sprawdź import": "Check import", "Wczytaj pamięć": "Import memory", "Sprawdź katalog": "Check folder",
    "Włączony: tak / nie": "Enabled: yes / no", "Interwał w minutach": "Interval in minutes",
    "Wybierz projekt. Nieprzypisane wpisy i import są dostępne globalnie.": "Choose a project. Unassigned items and import are available globally.",
    "Brak wybranego projektu. Import pozostaje dostępny z jawnym katalogiem docelowym.": "No project selected. Import is available with an explicit destination folder.",
    "Historia / Źródła otwierają zachowane dowody. Ocena zmienia tylko pewność ustalenia.": "History and Sources show saved evidence. Review changes confidence only.",
    "Źródło literalne · v=tekst/surowy/metadane · Więcej pobiera kolejny fragment": "Literal source · choose Sources for text/raw/metadata · More loads the next part",
    "Najpierw otwórz ustalenie.": "Open a record first.", "Otwórz rekord lub plan, aby odczytać historię.": "Open a record or plan to see its history.",
    "Projekt zmienił się podczas oceny. Niczego nie zapisano.": "The project changed during review. Nothing was saved.",
    "Potwierdź ocenę": "Confirm review", "Historia jest tylko do odczytu. Enter otwiera dokładny snapshot wersji.": "History is read-only. Enter opens the exact saved version.",
    "Węzeł wersji nie zawiera identyfikatora właściciela.": "This version has no owner identity.",
    "Brak bezpośrednich źródeł w tym wpisie.": "No direct sources for this item.",
    "Najpierw przeczytaj podgląd bieżącego projektu.": "Review the current project's preview first.",
    "Podgląd gotowy. Zapis i publikacja w Git pozostają jawną operacją.": "Preview ready. Saving and publishing to Git remain explicit actions.",
    "Pełny snapshot obejmuje rozmowy i źródła. Automat jest lokalny; nie wykonuje commitów ani wysyłania.": "A snapshot includes conversations and sources. Automatic snapshots stay local; they do not commit or upload.",
    "Import nie uruchamia modeli ani klientów. Zachowuje historię; testy z innego komputera pozostają historyczne.": "Import does not start models or clients. It preserves history; checks from another computer remain historical.",
    "Najpierw sprawdź import z konkretną ścieżką i katalogiem.": "Check the import with its source and destination folders first.",
    "Pamięć wczytana; żaden klient nie został automatycznie uruchomiony.": "Memory imported. No client was started automatically.",
    "Wybierz projekt do zapisania.": "Choose a project to save.", "Wybierz projekt do ustawienia automatu.": "Choose a project for automatic snapshots.",
    "Wpisz tak albo nie.": "Enter yes or no.", "Snapshot zapisany.": "Snapshot saved.",
    "Pewność (proposed / confirmed / disputed / superseded)": "Confidence (proposed / confirmed / disputed / superseded)",
    "Typ (np. fact, task, extract)": "Type (for example fact, task, extract)",
    "Identyfikatory dowodów, po jednym w wierszu": "Evidence identifiers, one per line", "Komentarz": "Comment",
    "Dowody (identyfikatory źródeł, po jednym w wierszu)": "Evidence (source identifiers, one per line)",
    "Szukaj projektów": "Find folders", "Jawne katalogi, po jednym w wierszu": "Folders to scan, one per line",
    "Skanowane są tylko wybrane katalogi. Nie uruchamia to klientów.": "Only selected folders are scanned. No client is started.",
    "Szukaj": "Search", "Wykryto przenośną pamięć": "Project memory found",
    "Sprawdź i wczytaj — zachowaj tożsamość i historię": "Review and import — keep identity and history",
    "Nowy projekt — osobna tożsamość, bez wczytywania historii": "New project — separate identity, without existing history",
    "Wczytanie zachowuje tożsamość projektu. Testy z drugiego komputera pozostają historyczne.": "Import preserves project identity. Checks from another computer remain historical.",
    "Wczytać tę pamięć projektu?": "Import this project memory?", "Snapshot lub manifest.json": "Snapshot or manifest.json",
    "Pamięć wczytana do podglądu; bez uruchamiania klientów.": "Memory imported for viewing. No clients started.",
    "Klucz ustawiony. Jego treść nie jest ponownie wyświetlana.": "Key saved. Its value will not be displayed again.",
    "Usunąć klucz?": "Remove saved key?", "Dotyczy tylko lokalnego klucza tego profilu.": "Only the local key for this connection will be removed.",
    "Usuń klucz": "Remove saved key", "Klucz usunięty.": "Key removed.", "Wykonać test modelu?": "Test this model?",
    "To rzeczywiste zapytanie do wybranego dostawcy. Może zużyć płatne tokeny lub limit Copilota. Test nie edytuje projektu.": "This sends a real request to the provider and can use paid tokens or Copilot quota. It does not edit your project.",
    "Uruchom test": "Run test", "Trwa jawny test modelu i obsługi narzędzi…": "Testing the model and tools…",
    "Wynik testu połączenia": "Connection test result", "Przygotować Copilota?": "Prepare Copilot?",
    "Pobierze przypięty oficjalny runtime i sprawdzi integralność. Nie uruchamia płatnej inferencji.": "Downloads and verifies the pinned official runtime. No model request is made.",
    "Przygotuj": "Prepare", "Trwa przygotowanie oficjalnego runtime Copilota…": "Preparing the official Copilot runtime…",
    "Przygotowanie Copilota": "Copilot setup", "Logowanie Copilota": "Copilot login",
    "Otworzono operacyjny terminal logowania Copilota. Dane logowania nie trafiają do pamięci.": "Copilot login opened. Credentials stay outside project memory.",
    "Modele": "Models", "Wybierz model — zapis dotyczy następnego uruchomienia": "Choose model — applies to the next launch",
    "Model zapisany; nie uruchomiono generowania odpowiedzi.": "Model saved. No generation started.",
    "Brak uruchomionych sesji.": "No sessions yet.", "Sesje na tym komputerze": "Sessions on this computer",
    "Operacja na sesji": "Session actions", "Pokaż / dołącz bez nowego procesu": "Show / attach without a new process",
    "Przejmij sterowanie": "Take control", "Przerwij bieżącą operację": "Interrupt current operation",
    "Zatrzymaj proces": "Stop process", "Wznów zapisaną sesję": "Resume saved session",
    "Nowa sesja z pamięcią projektu": "New session with project memory",
    " · Nowa sesja jest osobną jawną operacją.": " · Starting a new session is a separate action.",
    "Widok": "View", "Automat": "Automatic", "Stan": "Status", "Uzasadnienie": "Reason",
    "Historia rewizji": "Revision history", "Surowy": "Raw", "Tekst": "Text", "Metadane": "Metadata",
    "Dalsze grupy": "Next groups", "Poprzednie": "Previous", "Kamera": "Camera",
    "Globalny": "Global", "Wyżej": "Parent",
}
CATALOG.update({english: polish for polish, english in LEGACY.items() if english not in CATALOG})
CATALOG.update({
    "{client} failed.": "{client}: awaria.",
    "{client} was interrupted.": "{client}: przerwano pracę.",
    "Exit code: {code}.": "Kod zakończenia: {code}.",
    "The session changed.": "Sesja uległa zmianie.",
    "Open Projects with Ctrl+B p to review the session. Choose Start work only when you want to try again.":
        "Otwórz Projekty przez Ctrl+B p, aby sprawdzić sesję. Wybierz Rozpocznij pracę, gdy chcesz spróbować ponownie.",
    "Open Connections from the menu (Ctrl+B Space) to review account sign-in.":
        "Otwórz Połączenia z menu (Ctrl+B Spacja), aby sprawdzić logowanie do konta.",
    "Open the menu with Ctrl+B Space to review the current session.":
        "Otwórz menu przez Ctrl+B Spacja, aby sprawdzić bieżącą sesję.",
    "Assistant": "Asystent",
    "Assistant started.": "Asystent uruchomiony.",
    "Start requested. Review the workspace.": "Zlecono rozpoczęcie pracy. Sprawdź przestrzeń pracy.",
    # Plan stages (#10)
    "Run next stage": "Uruchom następny etap",
    "Show the project, then start what is mapped to the next plan step":
        "Pokaż projekt, potem uruchom to, co przypisano do następnego kroku planu",
    "Plan stages": "Etapy planu", "implement": "implementacja", "review": "przegląd", "test": "test",
    "Plan": "Plan", "Step": "Krok", "Stage": "Etap", "Check profile": "Profil testu",
    "Required checks": "Wymagane testy", "Not set": "Nieustawione", "Unknown assistant": "Nieznany asystent",
    "There is no next plan step to route.": "Brak następnego kroku planu do przekazania.",
    "This step's state has no stage.": "Stan tego kroku nie ma etapu.",
    "No assistant or check is mapped to this stage.": "Do tego etapu nie przypisano asystenta ani testu.",
    "The mapped assistant is not configured for this project.":
        "Przypisany asystent nie jest skonfigurowany w tym projekcie.",
    "The mapped assistant cannot be launched for a plan step.":
        "Przypisanego asystenta nie można uruchomić dla kroku planu.",
    # Plan stages for the built-in API agent and Copilot (#125)
    "default model": "model domyślny",
    "The assistant of this stage changed since it was mapped. Confirm it in More → Plan stages; nothing was started.":
        "Asystent tego etapu zmienił się od przypisania. Potwierdź go w Więcej → Etapy planu; "
        "nic nie zostało uruchomione.",
    "This stage was mapped before it could name the built-in agent or Copilot. Confirm it in More → Plan stages; "
    "nothing was started.":
        "Ten etap przypisano, zanim mógł wskazywać wbudowanego agenta lub Copilota. Potwierdź go w "
        "Więcej → Etapy planu; nic nie zostało uruchomione.",
    "not confirmed; save it to confirm": "niepotwierdzony; zapisz, aby potwierdzić",
    "The mapped assistant's key is set but is not a valid key. Correct it in Connections or in its environment "
    "variable; nothing was started.":
        "Klucz przypisanego asystenta jest ustawiony, ale nie jest poprawnym kluczem. Popraw go w Połączeniach "
        "albo w jego zmiennej środowiskowej; nic nie zostało uruchomione.",
    "This Copilot profile names a credential that is not available. Provide it, or clear the credential "
    "reference in the profile so the stage uses the signed-in GitHub account; nothing was started.":
        "Ten profil Copilota wskazuje poświadczenia, które nie są dostępne. Udostępnij je albo usuń odwołanie "
        "do poświadczeń z profilu, aby etap użył zalogowanego konta GitHub; nic nie zostało uruchomione.",
    "The mapped assistant has no credential. Add its key in Connections; nothing was started.":
        "Przypisany asystent nie ma poświadczeń. Dodaj jego klucz w Połączeniach; nic nie zostało uruchomione.",
    "The mapped assistant's credential cannot be read. Check it in Connections; nothing was started.":
        "Nie można odczytać poświadczeń przypisanego asystenta. Sprawdź je w Połączeniach; "
        "nic nie zostało uruchomione.",
    "GitHub Copilot is not prepared on this computer. Prepare it in Connections; nothing was started.":
        "GitHub Copilot nie jest przygotowany na tym komputerze. Przygotuj go w Połączeniach; "
        "nic nie zostało uruchomione.",
    "Stage started. Nothing is sent to the model until you write to the assistant in its panel; "
    "it must read and acknowledge its resume packet before it changes anything.":
        "Etap rozpoczęty. Nic nie trafia do modelu, dopóki nie napiszesz do asystenta w jego panelu; "
        "zanim cokolwiek zmieni, musi odczytać i potwierdzić swój pakiet wznowienia.",
    "No active plan in this folder.": "Brak aktywnego planu w tym katalogu.",
    "Every step of the plan is done.": "Wszystkie kroki planu są wykonane.",
    "Every remaining step is blocked.": "Wszystkie pozostałe kroki są zablokowane.",
    "Check passed.": "Test zaliczony.", "Check did not pass.": "Test niezaliczony.",
    "Outcome": "Wynik", "Error": "Błąd", "Check run": "Uruchomienie testu", "Check result": "Wynik testu",
    "The task state is unchanged. Update it after you review the result.":
        "Stan zadania się nie zmienił. Zaktualizuj go po sprawdzeniu wyniku.",
    "A check is still running": "Test nadal trwa",
    "The check keeps running; its result will appear here when it ends.":
        "Test nadal trwa; jego wynik pojawi się tutaj, gdy się zakończy.",
    "The check could not run": "Nie udało się uruchomić testu", "Check finished": "Test zakończony",
    "Project changed. Run next stage was not carried out; press it again for this project.":
        "Projekt się zmienił. Następny etap nie został uruchomiony; naciśnij ponownie dla tego projektu.",
    "Project changed while the check ran. Its result was recorded for the earlier project and is not shown here.":
        "Projekt zmienił się w trakcie testu. Wynik zapisano dla poprzedniego projektu i nie jest tu pokazany.",
    "This assistant is already running here. No process was restarted and it received no new resume packet. "
    "For the next stage, end that session or map another assistant to the stage.":
        "Ten asystent już tu działa. Proces nie został uruchomiony ponownie i nie dostał nowego pakietu "
        "wznowienia. Aby przejść do następnego etapu, zakończ tę sesję albo przypisz do etapu innego asystenta.",
    # Messages of stages.validate_stage_map / validate_project_stages
    "Unknown plan stage; use implement, review or test.": "Nieznany etap planu; użyj implement, review lub test.",
    "A stage must name an assistant slot.": "Etap musi wskazywać asystenta.",
    "The test stage must name a check profile.": "Etap test musi wskazywać profil testu.",
    "Invalid plan stages.": "Niepoprawne etapy planu.",
    "Invalid project identifier.": "Niepoprawny identyfikator projektu.",
    "Start this stage from the workspace with Run next stage.":
        "Uruchom ten etap w przestrzeni pracy przyciskiem Uruchom następny etap.",
    "The assistant did not start": "Asystent nie wystartował",
    "Stage started. The assistant must acknowledge its resume packet.":
        "Etap rozpoczęty. Asystent musi potwierdzić swój pakiet wznowienia.",
    "Running check": "Trwa test",
    "Map stages in More → Plan stages.": "Przypisz etapy w Więcej → Etapy planu.",
    "Check profile for the test stage (empty: not set)": "Profil testu dla etapu test (puste: nieustawione)",
    "The profile is read from this folder's checks.toml when it runs.":
        "Profil jest odczytywany z checks.toml tego katalogu podczas uruchomienia.",
    "Plan stages saved. Nothing was started.": "Etapy planu zapisane. Nic nie zostało uruchomione.",
})
for _en, _pl in CATALOG.items():
    LEGACY.setdefault(_pl, _en)


def native_failure_notice(run, language="en"):
    """Fixed public feedback; never reflect native output or arbitrary fields."""
    state = run.get("lifecycle")
    if not isinstance(state, str):
        return ""
    client_id = run.get("client")
    client = {"codex": "Codex", "claude": "Claude"}.get(
        client_id if isinstance(client_id, str) else None, tr("Assistant", language))
    if state == "stale":
        message = tr("The session changed.", language)
    elif state in {"failed", "interrupted"}:
        message = tr("{client} failed." if state == "failed" else "{client} was interrupted.", language, client=client)
        code = run.get("exit_code")
        if type(code) is int and 0 <= code <= 255:
            message += " " + tr("Exit code: {code}.", language, code=code)
    else:
        return ""
    recovery = ("Open Connections from the menu (Ctrl+B Space) to review account sign-in."
                if run.get("purpose") == "auth" else
                "Open the menu with Ctrl+B Space to review the current session." if state == "stale" else
                "Open Projects with Ctrl+B p to review the session. Choose Start work only when you want to try again.")
    return message + " " + tr(recovery, language)


def tr(text, language="en", **values):
    text = str(text)
    english = LEGACY.get(text, text)
    translated = CATALOG.get(english, text if text in LEGACY else english) if language == "pl" else english
    return translated.format(**values) if values else translated


def language_for(settings):
    try:
        from .cockpit_state import CockpitState
        value = CockpitState(settings).load().get("language", "en")
        return value if value in {"en", "pl"} else "en"
    except (OSError, ValueError):
        return "en"


def localize_bindings(node, language="en"):
    """Translate labels while preserving exact key routing in pinned Textual 8.

    Textual exposes refresh_bindings but has no public description setter.
    Keep this small compatibility boundary separate from action dispatch;
    App.bind would append bindings and discard priority/id/group metadata.
    """
    from dataclasses import replace

    labels = getattr(node, "_cairnmind_binding_labels", None)
    if labels is None:
        labels = node._cairnmind_binding_labels = {}
    for key, bindings in node._bindings.key_to_bindings.items():
        translated = []
        for index, binding in enumerate(bindings):
            description, tooltip, group = labels.setdefault((key, index, binding.action), (
                binding.description, binding.tooltip, binding.group.description if binding.group else ""))
            translated.append(replace(
                binding, description=tr(description, language), tooltip=tr(tooltip, language),
                group=replace(binding.group, description=tr(group, language)) if binding.group else None,
            ))
        node._bindings.key_to_bindings[key] = translated
    if node.is_mounted:
        node.refresh_bindings()
