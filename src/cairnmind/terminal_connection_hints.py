"""A connection key that is missing or unusable, said as what the person can do instead of as its code (#243).

The codes are the fixed ones a credential check (api_credentials.status) or a launch (api_runtime) reports;
any other code keeps the sidebar's general line, so nothing a service sends is ever shown as advice.
"""
from __future__ import annotations

NOT_STARTED = "The assistant was not started."
HINTS = {
    "credential_missing": "No API key is set for this connection. Open Connections, choose the connection, "
                          "then Set API key.",
    "credential_invalid": "The API key of this connection is not a valid key. Open Connections, choose the "
                          "connection, then Set API key, or correct its environment variable.",
    "credential_unavailable": "The API key of this connection cannot be read. Open Connections, choose the "
                              "connection, then Set API key.",
}
MESSAGES = {
    NOT_STARTED: "Asystent nie został uruchomiony.",
    HINTS["credential_missing"]: "To połączenie nie ma klucza API. Otwórz Połączenia, wybierz połączenie, "
                                 "a potem Ustaw klucz API.",
    HINTS["credential_invalid"]: "Klucz API tego połączenia nie jest poprawnym kluczem. Otwórz Połączenia, wybierz "
                                 "połączenie, a potem Ustaw klucz API, albo popraw jego zmienną środowiskową.",
    HINTS["credential_unavailable"]: "Nie można odczytać klucza API tego połączenia. Otwórz Połączenia, wybierz "
                                     "połączenie, a potem Ustaw klucz API.",
}


def connection_hint(codes, translate):
    """The sentence for the first code that has one, in the panel's language; None when none has."""
    for code in codes:
        if isinstance(code, str) and code in HINTS:
            return translate(HINTS[code])
    return None
