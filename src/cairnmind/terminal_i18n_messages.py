"""Fixed messages the backend writes in English, with their Polish translations.

The memory agent and the model providers report what went wrong with fixed sentences. They are
English in the code; a Polish screen shows the translation below. Only an exact fixed sentence is
translated: text from a model, a provider or a source is never looked up here (see fixed_message).
"""
from __future__ import annotations

# Answers and gaps of the memory agent's /ask (agent.py, service.py).
ASK_MESSAGES = {
    "The verification state changed during the answer; read it again.":
        "Stan weryfikacji zmienił się podczas odpowiedzi; odczytaj go ponownie.",
    "The machine verification that was read could not be confirmed as current.":
        "Nie można potwierdzić aktualności odczytanej weryfikacji maszynowej.",
    "The model did not return an answer with valid citations.":
        "Model nie zwrócił odpowiedzi z poprawnymi cytowaniami.",
    "The local model's answer has an invalid format.": "Niepoprawny format odpowiedzi lokalnego modelu.",
    "No verified answer.": "Brak zweryfikowanej odpowiedzi.",
    "The model gave no answer text.": "Model nie podał treści odpowiedzi.",
    "The response has no answer field, which answer_schema requires.":
        "Odpowiedź nie zawiera pola answer wymaganego przez answer_schema.",
    "The answer field holds no valid JSON for answer_schema.":
        "Pole answer nie zawiera poprawnych danych JSON dla answer_schema.",
    "The answer field holds an invalid JSON value.": "Pole answer zawiera niepoprawną wartość JSON.",
    "The JSON answer exceeds the answer budget; narrow the question.":
        "Odpowiedź JSON przekracza budżet odpowiedzi; zawęź pytanie.",
    "The model returned an invalid list of unknowns.": "Model zwrócił niepoprawną listę braków wiedzy.",
    "Invalid citation format.": "Niepoprawny format cytowań.",
    "Some citations are left out because of the answer budget; narrow the question to read the details.":
        "Pomijam część cytowań z powodu budżetu odpowiedzi; zawęź pytanie, aby odczytać szczegóły.",
    "Invalid citation.": "Niepoprawne cytowanie.",
    "The model cited a source it did not read.": "Model wskazał źródło, którego nie odczytał.",
    "The source could not be confirmed as current.": "Nie można potwierdzić aktualności źródła.",
    "The quoted passage does not occur in the checked version of the source.":
        "Cytowany fragment nie występuje w sprawdzonej wersji źródła.",
    "The quoted passage is too short to be evidence.": "Cytowany fragment jest zbyt krótki, aby był dowodem.",
    "The model's answer could not be verified in the sources.":
        "Nie udało się zweryfikować odpowiedzi modelu w źródłach.",
    "There are not enough sources for a confirmed answer.":
        "Brak wystarczających źródeł do potwierdzonej odpowiedzi.",
    "The answer has no verified citations.": "Odpowiedź nie zawiera zweryfikowanych cytowań.",
    "Could not check whether the quotes support the answer.":
        "Nie udało się sprawdzić, czy cytaty potwierdzają odpowiedź.",
    "The analysis reached the context budget; narrow the question.":
        "Analiza osiągnęła budżet kontekstu; zawęź pytanie.",
    "The budget for read sources was exceeded.": "Przekroczono budżet odczytanych źródeł.",
    "No steps were left to correct the answer's format or citations.":
        "Brak pozostałych kroków na poprawę formatu lub cytowań odpowiedzi.",
    "No context budget was left to correct the answer's format or citations.":
        "Brak budżetu kontekstu na poprawę formatu lub cytowań odpowiedzi.",
    "The model returned tool calls instead of a corrected answer.":
        "Model zwrócił narzędzia zamiast poprawionej odpowiedzi.",
    "The model returned an invalid list of tool calls.": "Model zwrócił niepoprawną listę narzędzi.",
    "The model broke the tool protocol.": "Niepoprawny protokół narzędzi modelu.",
    "The analysis reached its step limit; narrow the question.": "Analiza osiągnęła limit kroków; zawęź pytanie.",
    "The agent's step limit was exceeded.": "Przekroczono limit kroków agenta.",
    "The support check of the answer did not finish within the analysis time limit.":
        "Kontrola poparcia odpowiedzi nie zakończyła się w limicie czasu analizy.",
    "Correcting the format or citations did not finish within the analysis time limit.":
        "Poprawa formatu lub cytowań nie zakończyła się w limicie czasu analizy.",
    "The local analysis ran out of time.": "Przekroczono czas lokalnej analizy.",
    "The local model did not finish within the time limit.": "Lokalny model nie zakończył pracy w limicie czasu.",
    "The local model was unavailable while correcting the format or citations.":
        "Lokalny model nie był dostępny podczas poprawy formatu lub cytowań.",
    "The local knowledge model is unavailable.": "Lokalny model wiedzy jest niedostępny.",
    "Waiting for the local agent timed out.": "Przekroczono czas oczekiwania na lokalnego agenta.",
    "The queue or the analysis exceeded the time limit.": "Kolejka lub analiza przekroczyła limit czasu.",
}
# The support check's gap: this fixed prefix, then the judging model's own reason (untrusted, kept as is).
ASK_UNSUPPORTED_PREFIX = "The quotes do not support the answer: "
ASK_UNSUPPORTED_PREFIX_PL = "Cytaty nie potwierdzają odpowiedzi: "

# Errors of the model providers (api_providers.py) and of Copilot (copilot_adapter.py).
PROVIDER_MESSAGES = {
    "The API credential is missing.": "Brakuje poświadczeń API.",
    "The provider rejected the credential.": "Dostawca odrzucił poświadczenia.",
    "The account has no access to the chosen resource.": "Konto nie ma dostępu do wybranego zasobu.",
    "No funds or provider quota are left.": "Brak środków lub dostępnego limitu dostawcy.",
    "The provider limited the request rate.": "Dostawca ograniczył częstotliwość żądań.",
    "Cannot connect to the provider.": "Nie można połączyć się z dostawcą.",
    "Waiting for the provider timed out.": "Upłynął czas oczekiwania na dostawcę.",
    "The model or the API address is unavailable.": "Model lub adres API jest niedostępny.",
    "The request exceeds the context limit. Increase the Ollama profile's window or shorten the context.":
        "Żądanie przekracza limit kontekstu. Zwiększ okno profilu Ollama lub skróć kontekst.",
    "Ollama gave no reliable context limit for the model; check the model and the server version.":
        "Ollama nie podała wiarygodnego limitu kontekstu modelu; sprawdź model i wersję serwera.",
    "The provider cut the answer off at the output limit.": "Dostawca przerwał odpowiedź na limicie wyjścia.",
    "The provider does not support the requested configuration.": "Dostawca nie obsługuje żądanej konfiguracji.",
    "The model did not confirm working tool support.": "Model nie potwierdził poprawnej obsługi narzędzi.",
    "The provider's service is temporarily unavailable.": "Usługa dostawcy jest chwilowo niedostępna.",
    "The API answered with a redirect; check the server address.":
        "API zwróciło przekierowanie; sprawdź adres serwera.",
    "The provider returned an invalid or incomplete response.":
        "Dostawca zwrócił niepoprawną lub niekompletną odpowiedź.",
    "The provider's response exceeded the size limit.": "Odpowiedź dostawcy przekroczyła limit rozmiaru.",
    "The provider refused to carry out the request.": "Dostawca odmówił wykonania żądania.",
    "The provider rejected the request because of a conflict.": "Dostawca odrzucił żądanie z powodu konfliktu.",
    "The provider rejected the request.": "Dostawca odrzucił żądanie.",
    "The provider rejected the tool definitions.": "Dostawca odrzucił definicje narzędzi.",
    "The chosen window exceeds the Ollama model's limit. Lower it or choose a model with a larger context.":
        "Wybrane okno przekracza limit modelu Ollama. Zmniejsz je lub wybierz model z większym kontekstem.",
    "Install Cairnmind's dependencies with Copilot support.": "Zainstaluj zależności Cairnmind z obsługą Copilota.",
    "The pinned Copilot SDK version is required.": "Wymagana jest przypięta wersja SDK Copilota.",
    "Prepare the Copilot runtime in Cairnmind's settings.": "Przygotuj runtime Copilota w ustawieniach Cairnmind.",
    "The Copilot runtime changed; prepare it again.": "Runtime Copilota zmienił się; przygotuj go ponownie.",
    "Could not prepare a verified Copilot runtime.": "Nie udało się przygotować zweryfikowanego runtime Copilota.",
    "Sign in to GitHub Copilot.": "Zaloguj się do GitHub Copilot.",
    "GitHub rejected the Copilot credentials.": "GitHub odrzucił poświadczenia Copilota.",
    "Account or organization policy restricts access to Copilot.":
        "Zasady konta lub organizacji ograniczają dostęp do Copilota.",
    "The available Copilot quota is used up.": "Wyczerpano dostępny limit Copilota.",
    "Copilot limited the request rate.": "Copilot ograniczył częstotliwość żądań.",
    "Cannot connect to the Copilot service.": "Nie można połączyć się z usługą Copilota.",
    "The Copilot request failed; the result may be uncertain.":
        "Żądanie Copilota zakończyło się błędem; wynik może być niepewny.",
    "This turn's tool limit was reached. Continue explicitly.": "Osiągnięto limit narzędzi tej tury. Kontynuuj jawnie.",
    "The Copilot turn time limit was reached.": "Osiągnięto limit czasu tury Copilota.",
    "The Copilot response size limit was reached.": "Osiągnięto limit rozmiaru odpowiedzi Copilota.",
    "The Copilot model did not confirm tool support.": "Model Copilota nie potwierdził obsługi narzędzi.",
    "The previous Copilot turn is still running.": "Poprzednia tura Copilota nadal trwa.",
    "Choose a direct API profile.": "Wybierz profil bezpośredniego API.",
}
# The Copilot sign-in terminal (copilot_adapter._login).
LOGIN_MESSAGES = {
    "Open:": "Otwórz:", "Code:": "Kod:", "Value:": "Wartość:",
    "Copilot login completed.": "Logowanie Copilot zakończone.",
    "Store the credentials locally without encryption? [y/N]:":
        "Zapisać poświadczenia lokalnie bez szyfrowania? [tak/N]:",
    "Login cancelled.": "Anulowano logowanie.",
}
# Gaps the built-in agent's engine (api_agent.py) reports at the end of a turn.
ENGINE_MESSAGES = {
    "A checkpoint is required before context can be shortened.":
        "Przed skróceniem kontekstu wymagany jest punkt kontrolny.",
    "An earlier action has no confirmed outcome. Resolve it explicitly before continuing.":
        "Wcześniejsze działanie nie ma potwierdzonego wyniku. Rozstrzygnij je jawnie przed kontynuacją.",
    "Context renewal limit reached.": "Osiągnięto limit odnowień kontekstu.",
    "Copilot context reset unavailable.": "Reset kontekstu Copilota jest niedostępny.",
    "Copilot did not complete the exact model read/ACK protocol.":
        "Copilot nie ukończył dokładnego protokołu odczytu i ACK.",
    "Host tool limit reached.": "Osiągnięto limit narzędzi hosta.",
    "Model call limit reached; continue explicitly to renew the budget.":
        "Osiągnięto limit wywołań modelu; kontynuuj jawnie, aby odnowić budżet.",
    "No complete tool result was recorded; the tool was not replayed.":
        "Nie zapisano pełnego wyniku narzędzia; narzędzia nie uruchomiono ponownie.",
    "Read and ACK the current ready packet before effects.":
        "Przed działaniami odczytaj i potwierdź (ACK) bieżący pakiet gotowości.",
    "Read the complete ready packet using resume_context before its exact ACK.":
        "Odczytaj cały pakiet gotowości przez resume_context przed jego dokładnym ACK.",
    "Read the current state before another proposal.": "Odczytaj bieżący stan przed kolejną propozycją.",
    "The authoritative packet contains credential material; it was not delivered or acknowledged.":
        "Wiążący pakiet zawiera dane uwierzytelniające; nie został dostarczony ani potwierdzony.",
    "The local memory service is unavailable; effects are paused.":
        "Lokalna usługa pamięci jest niedostępna; działania są wstrzymane.",
    "The model did not complete the current packet's read/ACK protocol.":
        "Model nie ukończył protokołu odczytu i ACK bieżącego pakietu.",
    "The service did not accept the exact ACK.": "Usługa nie przyjęła dokładnego ACK.",
    "The turn stopped. No request or action was automatically repeated.":
        "Tura została przerwana. Żadne żądanie ani działanie nie zostało automatycznie powtórzone.",
    "Turn time limit reached.": "Osiągnięto limit czasu tury.",
    "The request exceeds the configured model context. Increase the profile's explicit context window or "
    "continue with a shorter request restored from archived memory. The request was not automatically retried.":
        "Żądanie przekracza skonfigurowany kontekst modelu. Zwiększ jawne okno kontekstu profilu albo kontynuuj "
        "krótszym żądaniem odtworzonym z zarchiwizowanej pamięci. Żądanie nie zostało automatycznie ponowione.",
    # Turn results without a word of their own in the status line.
    "Context limit": "Limit kontekstu", "Limit reached": "Osiągnięto limit", "Resume required": "Wymagane wznowienie",
    "Uncertain": "Niepewne", "Partial": "Częściowo",
}
AGENT_MESSAGES = {**PROVIDER_MESSAGES, **ENGINE_MESSAGES}

# Answers of the local broker (cockpit.py) and of its client (terminal_client.py). A panel shows them as
# "Broker <status>: <detail>"; the detail is translated when it is one of these exact sentences.
BROKER_MESSAGES = {
    "The local broker is unavailable or needs a restart.":
        "Lokalny broker jest niedostępny lub wymaga ponownego uruchomienia.",
    "The local broker does not answer.": "Lokalny broker nie odpowiada.",
    "The local broker address is invalid.": "Niepoprawny adres lokalnego brokera.",
    "The local broker gave an invalid answer.": "Niepoprawna odpowiedź lokalnego brokera.",
    "The request was refused.": "Żądanie zostało odrzucone.",
    "The local session could not be authenticated.": "Nie można uwierzytelnić lokalnej sesji.",
    "The local session ticket is invalid.": "Niepoprawny bilet lokalnej sesji.",
    "The local session is invalid.": "Niepoprawna lokalna sesja.",
    "The connection to the local broker was interrupted.": "Połączenie z lokalnym brokerem zostało przerwane.",
    "The terminal refused the local session or generation.": "Terminal odrzucił lokalną sesję lub generację.",
    "The event stream is unavailable.": "Strumień zdarzeń jest niedostępny.",
    "The broker session expired. Run cairnmind.": "Sesja kokpitu wygasła. Uruchom cairnmind.",
    "Invalid Host.": "Niepoprawny Host.", "Invalid Origin.": "Niepoprawny Origin.",
    "Invalid CSRF token.": "Niepoprawny token CSRF.",
    "The request is too large.": "Żądanie przekracza limit.",
    "Invalid JSON.": "Niepoprawny JSON.", "A JSON object was expected.": "Oczekiwano obiektu JSON.",
    "Invalid request fields.": "Niepoprawne pola żądania.",
    "The memory service is unavailable. Restart cairnmind.":
        "Usługa pamięci jest niedostępna. Uruchom ponownie cairnmind.",
    "The memory service gave an invalid answer.": "Usługa pamięci zwróciła niepoprawną odpowiedź.",
    "The memory service gave an invalid response.": "Niepoprawna odpowiedź usługi pamięci.",
    "Invalid graph projection format.": "Niepoprawny format projekcji grafu.",
    "The project of the chosen entry is unknown.": "Nieznany projekt wskazanego wpisu.",
    "Too many pending cockpit openings.": "Zbyt wiele oczekujących otwarć kokpitu.",
    "Invalid launch ticket.": "Niepoprawny bilet uruchomienia.",
    "The ticket expired or was already used. Run cairnmind again.":
        "Bilet wygasł lub został już użyty. Uruchom ponownie cairnmind.",
    "The cockpit must use a local HTTP address.": "Kokpit musi używać lokalnego adresu HTTP.",
    "Invalid local service token.": "Niepoprawny token lokalnej usługi.",
    "The projects could not be read.": "Nie można odczytać projektów.",
    "Unsupported agent operation.": "Nieobsługiwana operacja agenta.",
    "Invalid agent identity.": "Niepoprawna tożsamość agenta.",
    "The agent engine is unavailable.": "Silnik agenta jest niedostępny.",
    "stop_clients must be a boolean.": "stop_clients musi być wartością logiczną.",
    "Initialize Cairnmind first.": "Najpierw zainicjalizuj Cairnmind.",
    "Unknown project.": "Nieznany projekt.", "Unknown profile.": "Nieznany profil.",
    "The project folder does not exist.": "Katalog projektu nie istnieje.",
    "Give the path of an existing project folder.": "Podaj ścieżkę istniejącego katalogu projektu.",
    "Choose a registered folder of this project.": "Wybierz zarejestrowany katalog tego projektu.",
    "Use this client's own sign-in.": "Użyj natywnego logowania tego klienta.",
    "Invalid credential configuration.": "Niepoprawna konfiguracja poświadczeń.",
    "The location of the key store could not be checked.": "Nie można sprawdzić lokalizacji magazynu kluczy.",
    "The key store must be outside project folders.": "Magazyn kluczy musi znajdować się poza katalogami projektów.",
    "A native client's models are chosen in its profile or terminal.":
        "Modele natywnego klienta wybierasz w jego profilu lub terminalu.",
    "Unsupported sign-in client.": "Nieobsługiwany klient logowania.",
    "Invalid run identifier.": "Niepoprawny identyfikator uruchomienia.",
    "The run identifier was used with other parameters.":
        "Identyfikator uruchomienia został użyty dla innych parametrów.",
    "Controlling a process needs the local CLI.": "Sterowanie procesem wymaga lokalnego CLI.",
    "Unsupported terminal action.": "Nieobsługiwana akcja terminala.",
    "Invalid terminal generation.": "Niepoprawna generacja terminala.",
    "The terminal frame is too large.": "Zbyt duża ramka terminala.",
    "Invalid terminal frame.": "Niepoprawna ramka terminala.",
    "Too much keyboard data.": "Zbyt dużo danych klawiatury.",
    "Invalid keyboard data.": "Niepoprawne dane klawiatury.",
    "Invalid terminal size.": "Niepoprawny rozmiar terminala.",
    "Unsupported terminal frame.": "Nieobsługiwana ramka terminala.",
    "A question has 1 to 4000 characters.": "Pytanie musi mieć od 1 do 4000 znaków.",
    "Invalid event cursor.": "Niepoprawny kursor zdarzeń.",
    "The event exceeds the size limit.": "Zdarzenie przekracza limit rozmiaru.",
    "The cockpit did not start; check the local file cockpit/server.log.":
        "Kokpit nie uruchomił się; sprawdź lokalny plik cockpit/server.log.",
    "The running cockpit has another version. Stop it and open it again.":
        "Uruchomiony kokpit ma inną wersję. Zatrzymaj go i otwórz ponownie.",
    "The cockpit is already running for this store.": "Kokpit już działa dla tego magazynu.",
    # The graph panel's own notices (terminal_graph.py).
    "Incomplete identity of a historical version.": "Niepełna tożsamość wersji historycznej.",
    "An ambiguous memory event; the view was read again.": "Niejednoznaczne zdarzenie pamięci; odświeżono projekcję.",
    "The connection to memory was interrupted; the view and camera were kept.":
        "Połączenie z pamięcią przerwane; zachowano widok i kamerę.",
    "The project is no longer registered.": "Projekt nie jest już zarejestrowany.",
}
# Checks of profiles, keys, settings and the workspace's own state (api_profiles.py, api_credentials.py,
# api_runtime.py, cockpit_state.py, workspace_state.py, workspace.py, terminal_panels.py). A form or a
# notice shows them through the catalog.
STATE_MESSAGES = {
    "Give a valid API address.": "Podaj poprawny adres API.",
    "The API address must not hold a key, a password or query parameters.":
        "Adres API nie może zawierać klucza, hasła ani parametrów zapytania.",
    "Plain HTTP is allowed only for localhost; use HTTPS.":
        "Połączenie HTTP jest dostępne wyłącznie dla localhost; użyj HTTPS.",
    "Invalid API path.": "Niepoprawna ścieżka API.", "Invalid profile limits.": "Niepoprawne limity profilu.",
    "A profile limit is outside the allowed range.": "Limit profilu jest poza dozwolonym zakresem.",
    "Give a credential reference, not an API key.": "Podaj odwołanie do poświadczeń, nie klucz API.",
    "A credential reference is env:NAME or profile:IDENTIFIER.":
        "Odwołanie poświadczeń ma postać env:NAZWA lub profile:IDENTYFIKATOR.",
    "Invalid client profile.": "Niepoprawny profil klienta.",
    "Choose a supported client or API provider.": "Wybierz obsługiwanego klienta lub dostawcę API.",
    "The profile has unsupported fields; save keys separately.":
        "Profil zawiera nieobsługiwane pola; klucze zapisuj osobno.",
    "Invalid model identifier.": "Niepoprawny identyfikator modelu.",
    "An API profile needs a model identifier.": "Profil API wymaga identyfikatora modelu.",
    "Choose an API provider.": "Wybierz dostawcę API.",
    "The protocol does not match the chosen provider.": "Protokół nie pasuje do wybranego dostawcy.",
    "Give both rates or leave both empty.": "Podaj obie stawki lub pozostaw obie puste.",
    "Give both non-negative rates per million tokens.": "Podaj obie nieujemne stawki za milion tokenów.",
    "Give whole-number limits and both rates as numbers, or leave the prices empty.":
        "Podaj poprawne limity całkowite i obie stawki liczbowe albo pozostaw ceny puste.",
    "Invalid credential identifier.": "Niepoprawny identyfikator poświadczeń.",
    "Invalid API key.": "Niepoprawny klucz API.",
    "The credential store must be a private folder of the user.":
        "Magazyn poświadczeń musi być prywatnym katalogiem użytkownika.",
    "The credential lock must be private.": "Blokada poświadczeń musi być prywatna.",
    "The credential store cannot be read safely.": "Nie można bezpiecznie odczytać magazynu poświadczeń.",
    "The credential file must be a private file of the user.":
        "Plik poświadczeń musi być prywatnym plikiem użytkownika.",
    "Invalid credential store.": "Niepoprawny magazyn poświadczeń.",
    "Unsupported credential store.": "Nieobsługiwany magazyn poświadczeń.",
    "The credential store exceeds its size limit.": "Magazyn poświadczeń przekroczył limit rozmiaru.",
    "Choose how the key is stored.": "Wybierz sposób przechowywania klucza.",
    "Invalid credential profile.": "Niepoprawny profil poświadczeń.",
    "Invalid credential reference.": "Niepoprawne odwołanie do poświadczeń.",
    "The agent's private configuration cannot be restored. Start it again from Cairnmind.":
        "Nie można odtworzyć prywatnej konfiguracji agenta. Uruchom go ponownie z Cairnmind.",
    "Give a valid folder path.": "Podaj poprawną ścieżkę katalogu.",
    "The cockpit folder must belong to the user and have permissions 0700.":
        "Katalog kokpitu musi należeć do użytkownika i mieć uprawnienia 0700.",
    "The cockpit lock must be a private file of the user.": "Blokada kokpitu musi być prywatnym plikiem użytkownika.",
    "The cockpit settings file must not be a link.": "Plik ustawień kokpitu nie może być dowiązaniem.",
    "Unsupported version of the cockpit settings.": "Nieobsługiwana wersja ustawień kokpitu.",
    "Invalid list of profiles.": "Niepoprawna lista profili.",
    "Profile identifiers must be unique.": "Identyfikatory profili muszą być unikalne.",
    "A set of panes has 1 to 32 panes.": "Zestaw paneli musi zawierać od 1 do 32 paneli.",
    "Invalid client pane configuration.": "Niepoprawna konfiguracja panelu klienta.",
    "Unsupported field of the cockpit settings.": "Nieobsługiwane pole ustawień kokpitu.",
    "Give 1 to 32 profiles.": "Podaj od 1 do 32 profili.",
    "Invalid list of folders to scan.": "Niepoprawna lista katalogów skanowania.",
    "Folders to scan must exist and be unique.": "Katalogi skanowania muszą istnieć i być unikalne.",
    "Invalid sets of project panes.": "Niepoprawne zestawy paneli projektów.",
    "Invalid project identifier.": "Niepoprawny identyfikator projektu.",
    "Choose folders to scan.": "Wybierz katalogi skanowania.",
    "The chosen folders must exist and be unique.": "Wybrane katalogi muszą istnieć i być unikalne.",
    "A scan is already running; cancel it first.": "Skanowanie już trwa; najpierw je anuluj.",
    "Invalid workspace item identifier.": "Niepoprawny identyfikator elementu środowiska.",
    "Invalid workspace identifier.": "Niepoprawny identyfikator środowiska.",
    "The workspace state must be a private file of the user.":
        "Stan środowiska musi być prywatnym plikiem użytkownika.",
    "Unsupported or damaged workspace state.": "Nieobsługiwany lub uszkodzony stan środowiska.",
    "The working folder must be an absolute path.": "Katalog pracy musi być ścieżką bezwzględną.",
    "Invalid mode or chosen pane of the workspace.": "Niepoprawny tryb lub wybrany panel środowiska.",
    "Invalid pane layout.": "Niepoprawny układ paneli.",
    "Invalid choice of a memory entry.": "Niepoprawny wybór wpisu pamięci.",
    "The entry belongs to another project than the chosen pane.":
        "Wpis należy do innego projektu niż wybrany panel.",
    "Invalid historical version.": "Niepoprawna wersja historyczna.",
    "Invalid set of client panes.": "Niepoprawny zestaw paneli klientów.",
    "Invalid client pane.": "Niepoprawny panel klienta.",
    "A client pane appears more than once.": "Panel klienta występuje więcej niż raz.",
    "Invalid list of workspace panes.": "Niepoprawna lista paneli środowiska.",
    "A pane appears more than once.": "Panel występuje więcej niż raz.",
    "The chosen pane is not in the current set of clients.": "Wybrany panel nie należy do bieżącego zestawu klientów.",
    "Invalid terminal pane identity.": "Niepoprawna tożsamość panelu terminala.",
    "Invalid list of projects.": "Niepoprawna lista projektów.",
    "Invalid list of client errors.": "Niepoprawna lista błędów klientów.",
    "Invalid workspace notice.": "Niepoprawny komunikat środowiska.",
    "Invalid state of the client service.": "Niepoprawny stan usługi klientów.",
    "The workspace state exceeds its limit.": "Stan środowiska przekracza limit.",
    "Unsupported field of the workspace state.": "Nieobsługiwane pole stanu środowiska.",
    "Unsupported list of workspace sessions.": "Nieobsługiwana lista sesji środowiska.",
    "Choose a registered project.": "Wybierz zarejestrowany projekt.",
    "Work needs an explicitly chosen project.": "Praca wymaga jawnie wybranego projektu.",
    "stale_workspace_revision: the workspace state is changing; choose again.":
        "stale_workspace_revision: stan środowiska zmienia się, ponów wybór.",
    "Install tmux >= 3.4 in the same WSL distribution as Cairnmind.":
        "Zainstaluj tmux >= 3.4 w tej samej dystrybucji WSL co Cairnmind.",
    "The workspace socket path is too long.": "Ścieżka gniazda środowiska jest zbyt długa.",
    "The workspace configuration must not be a link.": "Konfiguracja środowiska nie może być dowiązaniem.",
    "The workspace configuration must be a private file of the user.":
        "Konfiguracja środowiska musi być prywatnym plikiem użytkownika.",
    "tmux is unavailable.": "tmux jest niedostępny.",
    "Attaching needs an interactive terminal.": "Dołączenie wymaga interaktywnego terminala.",
    "The terminal workspace is not running.": "Środowisko terminalowe nie działa.",
    "The identity of the attaching terminal was not confirmed.":
        "Nie potwierdzono tożsamości dołączanego terminala.",
    "Start work again explicitly after the workspace resumes.":
        "Ponów jawne rozpoczęcie pracy po wznowieniu środowiska.",
    "The client service is unavailable; running sessions are kept.":
        "Usługa klientów jest niedostępna; działające sesje pozostają zachowane.",
    "Invalid workspace pane.": "Niepoprawny panel środowiska.",
    "Choose a supported client in the open workspace.": "Wybierz obsługiwanego klienta w otwartym środowisku.",
    "Invalid sign-in terminal.": "Niepoprawny terminal logowania.",
    "Cairnmind needs an interactive terminal. Use `cairnmind workspace status` to diagnose.":
        "Cairnmind wymaga interaktywnego terminala. Użyj `cairnmind workspace status` do diagnostyki.",
    "Install tmux >= 3.4 in Ubuntu: sudo apt install tmux": "Zainstaluj tmux >= 3.4 w Ubuntu: sudo apt install tmux",
    "Detach this view with C-b d and run `cairnmind workspace takeover` in another terminal.":
        "Odłącz widok C-b d i uruchom `cairnmind workspace takeover` w osobnym terminalu.",
    "Private panel of the Cairnmind terminal workspace": "Prywatny panel terminalowego środowiska Cairnmind",
    "The chosen version was not found.": "Nie znaleziono wskazanej wersji.",
}
# The fixed sentences a broker may send as a detail: its own, and the checks of settings, profiles, keys,
# workspace state and providers that it relays (a ValueError's text). A broker started from an earlier
# version still answers in Polish; those sentences are known too. Only an exact sentence is translated.
_BROKER_FIXED = frozenset(sentence for catalog in (BROKER_MESSAGES, STATE_MESSAGES, PROVIDER_MESSAGES)
                          for pair in catalog.items() for sentence in pair)


def broker_error_text(exc, translate) -> str:
    """One line for an error of the local broker, with its fixed detail in the panel's language."""
    detail, status = getattr(exc, "detail", None), getattr(exc, "status_code", None)
    if isinstance(detail, str) and type(status) is int:
        text = " ".join(detail.split())
        return f"Broker {status}: " + (translate(text) if text in _BROKER_FIXED else text)
    return str(exc)


# Stored kinds, states and outcomes, translated where a panel shows them. The stored value itself is
# never changed: display_word is only for the text on screen (colours keep reading the raw word).
WORDS = {
    "decision": "decyzja", "fact": "fakt", "constraint": "ograniczenie", "question": "pytanie",
    "preference": "preferencja", "task": "zadanie", "checkpoint": "punkt kontrolny", "plan": "plan",
    "group": "grupa", "project": "projekt", "unassigned": "nieprzypisane", "source": "źródło",
    "proposed": "proponowane", "confirmed": "potwierdzone", "disputed": "zakwestionowane",
    "superseded": "zastąpione",
    "todo": "do zrobienia", "in_progress": "w toku", "needs_test": "do testu", "needs_review": "do przeglądu",
    "done": "zrobione", "blocked": "zablokowane",
    "pending": "oczekuje", "running": "trwa", "completed": "zakończone", "failed": "nieudane",
    "timeout": "przekroczony czas", "output_limit": "limit wyjścia", "cancelled": "anulowane",
    "interrupted": "przerwane",
}


def display_word(word, translate) -> str:
    """A stored kind, state or outcome in the panel's language; any other value as it is."""
    word = str(word)
    return translate(word) if word in WORDS else word


# The status word of an /ask result, as the panel shows it.
ASK_STATUS = {"complete": "Complete", "partial": "Partial", "unavailable": "Unavailable"}

MESSAGES = {**ASK_MESSAGES, ASK_UNSUPPORTED_PREFIX: ASK_UNSUPPORTED_PREFIX_PL, "Complete": "Pełna",
            **PROVIDER_MESSAGES, **LOGIN_MESSAGES, **WORDS, **BROKER_MESSAGES, **STATE_MESSAGES,
            "Cairnmind · Memory": "Cairnmind · Pamięć"}


def fixed_message(text, translate, known=ASK_MESSAGES) -> str:
    """Translate `text` only when it is one of the `known` fixed sentences, or the support check's
    prefix followed by a model's reason (the reason stays as it was). Anything else is returned
    unchanged, so model or source text is never swapped for a catalog sentence."""
    text = str(text)
    if text in known:
        return translate(text)
    if text.startswith(ASK_UNSUPPORTED_PREFIX) and known is ASK_MESSAGES:
        return translate(ASK_UNSUPPORTED_PREFIX) + text[len(ASK_UNSUPPORTED_PREFIX):]
    return text
