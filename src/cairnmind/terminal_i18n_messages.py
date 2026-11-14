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

# The status word of an /ask result, as the panel shows it.
ASK_STATUS = {"complete": "Complete", "partial": "Partial", "unavailable": "Unavailable"}

MESSAGES = {**ASK_MESSAGES, ASK_UNSUPPORTED_PREFIX: ASK_UNSUPPORTED_PREFIX_PL, "Complete": "Pełna"}


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
