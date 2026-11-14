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

# The status word of an /ask result, as the panel shows it.
ASK_STATUS = {"complete": "Complete", "partial": "Partial", "unavailable": "Unavailable"}

MESSAGES = {**ASK_MESSAGES, ASK_UNSUPPORTED_PREFIX: ASK_UNSUPPORTED_PREFIX_PL, "Complete": "Pełna",
            **PROVIDER_MESSAGES, **LOGIN_MESSAGES}


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
