"""English/Polish labels for the agent terminal, independent of provider data."""

CATALOG = {
    "input": "wejście", "output": "wyjście", "unavailable": "brak danych",
    "token report incomplete": "raport tokenów niepełny", "cost: unavailable": "koszt: brak danych",
    "estimated cost {amount:.6f} USD (profile rates)": "koszt szacunkowy {amount:.6f} USD (według stawek profilu)",
    "complete": "pełny", "partial": "niepełny", "observed": "obserwowany",
    "Copilot: {amount} nano_aiu · report {state}": "Copilot: {amount} nano_aiu · raport {state}",
    "Copilot: usage unavailable": "Copilot: brak danych o wykorzystaniu",
    "Copilot reports {reported}/{observed}": "raporty Copilot {reported}/{observed}",
    "provider report (provider units): ": "raport dostawcy (jednostki dostawcy): ",
    "Interrupt": "Przerwij", "Send": "Wyślij", "Resolve": "Rozstrzygnij",
    "default model": "model domyślny",
    "Restoring the local journal; no model is running…": "Odtwarzanie lokalnego dziennika — bez uruchamiania modelu…",
    "usage: unavailable · cost: unavailable": "wykorzystanie: brak danych · koszt: brak danych",
    " (visible excerpt limited)": " (widoczny fragment ograniczony)",
    "Incomplete response": "Niedokończona odpowiedź",
    "Request {request_id} — this output is not a confirmed response.\n": "Żądanie {request_id} — wynik nie jest potwierdzoną odpowiedzią.\n",
    "Ready. Send a message to start work and restore memory.": "Gotowy. Wyślij wiadomość, aby rozpocząć pracę i protokół odtworzenia pamięci.",
    "Cannot prepare the agent. Check the profile and services in Connections.": "Nie można przygotować agenta. Sprawdź stan profilu i usług w panelu Połączenia.",
    "event": "zdarzenie", "You": "Ty", "Agent": "Agent", "Tool": "Narzędzie", "System": "System",
    "Tool started": "Narzędzie — start", "Tool result": "Narzędzie — wynik",
    "Approve one command": "Zgoda na jedno polecenie",
    "This command runs with your user account permissions.": "Polecenie działa z uprawnieniami Twojego użytkownika.",
    "Run once": "Wykonaj raz", "Agent is working…": "Agent pracuje…",
    "Status: ": "Stan: ", "Completed": "Zakończono",
    "Missing evidence / blockers:\n": "Braki / blokady:\n",
    "Incomplete response — unconfirmed output:\n": "Niedokończona odpowiedź — wynik niepotwierdzony:\n",
    "Interrupted. Check uncertain actions; they will not be retried automatically.": "Przerwano. Niepewne działania wymagają sprawdzenia; nie będą ponawiane automatycznie.",
    "The operation did not finish. Check the journal and connection status.": "Operacja nie została ukończona. Szczegóły stanu są w dzienniku i panelu połączenia.",
    "Message submission cancelled before the turn started.": "Anulowano wysłanie wiadomości przed rozpoczęciem tury.",
    "Interrupting the current operation…": "Przerywanie bieżącej operacji…",
    "Uncertain actions: {count}. Check their outcome and use Resolve before continuing.": "Niepewne działania: {count}. Sprawdź stan i użyj Rozstrzygnij przed kontynuacją.",
    "action": "działanie", "Action with an unconfirmed outcome": "Działanie bez potwierdzonego wyniku",
    "Resolve after checking the outcome": "Rozstrzygnij po własnym sprawdzeniu",
    "What did you check, and what actually happened?": "Co sprawdziłeś i jaki jest faktyczny wynik?",
    "This saves your finding. It does not repeat the command or edit.": "Ta operacja zapisuje Twoje ustalenie. Nie ponawia polecenia ani edycji.",
    "Save finding": "Zapisz ustalenie",
    "Your finding was saved. Send a separate message to continue work.": "Zapisano ustalenie człowieka. Dalszą pracę uruchomisz osobną wiadomością.",
    "A terminal is required. Start cairnmind.": "Agent wymaga interaktywnego terminala. Uruchom cairnmind.",
    "Cannot restore this run. Check the profile status in Cairnmind.": "Nie można odtworzyć tego uruchomienia. Sprawdź stan profilu w Cairnmind.",
    "Working": "Praca w toku", "Interrupted": "Przerwano", "Failed": "Niepowodzenie",
    "Blocked": "Zablokowano", "Ready": "Gotowy", "Awaiting approval": "Oczekuje na zgodę",
    # Third-party MCP tools (#126)
    "Approve one third-party tool call": "Zgoda na jedno wywołanie narzędzia zewnętrznego",
    "This sends exactly these arguments to a program you configured. Its answer is untrusted third-party text.":
        "To wysyła dokładnie te argumenty do programu, który skonfigurowałeś. Jego odpowiedź jest niezaufanym "
        "tekstem z zewnątrz.",
    "Call once": "Wywołaj raz",
    "Third-party MCP server {server} is off ({code}); the agent works without its tools.":
        "Serwer MCP {server} jest wyłączony ({code}); agent pracuje bez jego narzędzi.",
}
LEGACY = {polish: english for english, polish in CATALOG.items()}
STATUS = {"complete": "Completed", "completed": "Completed", "running": "Working", "working": "Working",
          "interrupted": "Interrupted", "failed": "Failed", "error": "Failed", "blocked": "Blocked",
          "ready": "Ready", "approval_required": "Awaiting approval"}


def tr(text, language="en", **values):
    english = LEGACY.get(str(text), str(text))
    result = CATALOG.get(english, english) if language == "pl" else english
    return result.format(**values) if values else result


def status_text(value, language="en"):
    return tr(STATUS.get(str(value), str(value)), language)
