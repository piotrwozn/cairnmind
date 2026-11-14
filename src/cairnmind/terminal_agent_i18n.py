"""English/Polish labels for the agent terminal, independent of provider data."""

from .terminal_i18n_messages import AGENT_MESSAGES

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
    "The provider refused the request with third-party MCP tools ({code}); they are withdrawn for this run "
    "and the agent works without them.":
        "Dostawca odrzucił żądanie z narzędziami zewnętrznych serwerów MCP ({code}); zostały wycofane do końca "
        "tego uruchomienia, a agent pracuje bez nich.",
    "The provider refused the request ({code}) also without the third-party MCP tools; they stay offered.":
        "Dostawca odrzucił żądanie ({code}) także bez narzędzi zewnętrznych serwerów MCP; pozostają dostępne.",
    "Third-party MCP servers off in this run: {servers}":
        "Zewnętrzne serwery MCP wyłączone w tym uruchomieniu: {servers}",
    "A third-party MCP tool took too long to check its arguments and is withdrawn for this run.":
        "Sprawdzenie argumentów narzędzia zewnętrznego serwera MCP trwało za długo; narzędzie wycofano do końca "
        "tego uruchomienia.",
    # Turn and memory states shown in the status line (#157)
    "Memory acknowledged": "Pamięć potwierdzona (ACK)", "Memory waits for the model's ACK": "Pamięć czeka na ACK modelu",
    "Memory must be read and acknowledged again": "Pamięć trzeba ponownie odczytać i potwierdzić",
    "Partial result": "Wynik niepełny", "Context limit reached": "Osiągnięto limit kontekstu",
    "Limit reached": "Osiągnięto limit", "Outcome uncertain": "Wynik niepewny", "Busy": "Zajęty",
    "Closed": "Zamknięto", "Invalid input": "Nieprawidłowe dane wejściowe", "Status updated": "Zaktualizowano stan",
    "Context compacted": "Kontekst skrócony",
    "Ctrl+Q does not end the agent. Ctrl+B, then d: leave the workspace; it keeps running. Ctrl+C interrupts a turn.":
        "Ctrl+Q nie kończy agenta. Ctrl+B, potem d: wyjście ze środowiska; działa ono dalej. Ctrl+C przerywa turę.",
}
CATALOG.update(AGENT_MESSAGES)  # fixed provider and engine messages (#153)
LEGACY = {polish: english for english, polish in CATALOG.items()}
STATUS = {"complete": "Completed", "completed": "Completed", "running": "Working", "working": "Working",
          "interrupted": "Interrupted", "failed": "Failed", "error": "Failed", "blocked": "Blocked",
          "ready": "Ready", "approval_required": "Awaiting approval",
          "acknowledged": "Memory acknowledged", "requires_model_ack": "Memory waits for the model's ACK",
          "resume_required": "Memory must be read and acknowledged again", "partial": "Partial result",
          "context_limit": "Context limit reached", "limit_reached": "Limit reached",
          "uncertain": "Outcome uncertain", "busy": "Busy", "closed": "Closed", "invalid_input": "Invalid input"}


def tr(text, language="en", **values):
    english = LEGACY.get(str(text), str(text))
    result = CATALOG.get(english, english) if language == "pl" else english
    return result.format(**values) if values else result


def status_text(value, language="en"):
    return tr(STATUS.get(str(value), str(value)), language)
