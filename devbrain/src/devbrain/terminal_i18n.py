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
    "Ctrl+B, then [: scroll and copy terminal history (q to leave)\n"
    "Ctrl+B, then d: detach; run devbrain to return\n"
    "Ctrl+B, then Ctrl+B: send Ctrl+B to the native assistant\n\n"
    "DevBrain controls: Tab / Shift+Tab move focus; Enter chooses; Esc goes back.\n"
    "Projects: Enter opens; Start work starts only the chosen assistant.\n"
    "Graph: [ / ] select nodes; arrows rotate; Space pauses; Enter opens a node.\n"
    "Native assistants keep their own shortcuts, paste, Ctrl+C and approval dialogs.\n"
    "Memory readiness, session activity and terminal control are separate states.":
    "Ctrl+B, potem Spacja: wyszukaj polecenie\n"
    "Ctrl+B, potem p / 1 / 2 / i / g: projekty / asystent / drugi asystent / pamięć / graf\n"
    "Ctrl+B, potem Tab: następny widok\nCtrl+B, potem z: powiększ lub przywróć panele\n"
    "Ctrl+B, potem [: przewijaj i kopiuj historię terminala (q wraca)\n"
    "Ctrl+B, potem d: odłącz; uruchom devbrain, aby wrócić\n"
    "Ctrl+B, potem Ctrl+B: przekaż Ctrl+B do natywnego asystenta\n\n"
    "DevBrain: Tab / Shift+Tab zmienia fokus; Enter wybiera; Esc wraca.\n"
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

    labels = getattr(node, "_devbrain_binding_labels", None)
    if labels is None:
        labels = node._devbrain_binding_labels = {}
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
