"""What Cairnmind says when the workspace cannot open, and when the service could not be started (#273, #258).

tmux 3.4+ is needed only by the terminal workspace: `cairnmind`, `cairnmind tui` and `cairnmind workspace
takeover`. Every other command works without it, and doctor reports the workspace as unavailable instead of
Cairnmind as broken. The English sentences are the source; MESSAGES is their Polish for the terminal catalog.
#274 adds what Cairnmind says without models: the knowledge agent's stored choice (knowledge_mode.py), init's
choice, the answer without models with its full-text matches and doctor's information line.
"""
from __future__ import annotations

from . import platform_hints
from .terminal_safe import safe_text

WORKSPACE_NEEDS = "The terminal workspace needs tmux 3.4 or newer."
TMUX_NOT_FOUND = "tmux was not found."
TMUX_FOUND = "Found {version}."
TMUX_SILENT = "tmux did not report its version."
WITHOUT_WORKSPACE = ("Without it, these commands work: cairnmind serve, cairnmind run, cairnmind mcp and "
                     "cairnmind brief.")
COCKPIT_MISSING = "unavailable (tmux 3.4+ not found)"
COCKPIT_OLD = "unavailable (tmux 3.4+ not found; found {version})"
COCKPIT_OTHER = "unavailable (tmux 3.4+ not found; {code})"
SERVICE_NOT_STARTED = ("The Cairnmind service is not running and could not be started: {reason} Start it with "
                       "`{command}`, then call the tool again.")
KNOWLEDGE_DISABLED = "disabled (deterministic-only mode)"
MODELS_NOT_CONFIGURED = "not configured (deterministic-only mode)"
GET_MODELS = ("To use models: `cairnmind models pull` downloads the local models into Ollama (about 7 GB); "
              "`cairnmind models set` chooses a model server.")
SERVER_DOWN = "the model server {url} does not answer"
MODEL_MISSING = "the model server {url} does not have {model}"
CHOSE_ENABLED = ("Knowledge agent: enabled. The model server answers and has the models {chat} and {embedding}. "
                 "Saved as knowledge_agent in {config}.")
CHOSE_DISABLED = ("Knowledge agent: disabled (deterministic-only mode), because {reason}. Plans, tasks, checks, "
                  "brief, impact, memory and file indexing work without models; cairnmind ask makes no "
                  "answer and lists the passages that contain words of the question. Saved as knowledge_agent in {config}.")
TURNED_ON = ("Knowledge agent: enabled, saved as knowledge_agent in {config}. Restart the knowledge service "
             "(cairnmind stop, then start Cairnmind again) so that it uses the models. To turn it off again, set "
             "knowledge_agent to disabled in that file and restart the service.")
ASK_OFF = ("The knowledge agent is disabled (deterministic-only mode), so no answer was made. The passages listed as "
           "full-text matches contain words of the question: they are search results, not an answer and not "
           "verified evidence for one.")
ASK_NO_MATCH = ("The knowledge agent is disabled (deterministic-only mode), so no answer was made. A full-text "
                "search found no passage that contains words of the question.")
MATCHES_HEADING = "Full-text matches (search results, not an answer)"
MATCHES_HELD_BACK = ("Some matching passages were left out because of their sensitivity class, as answers leave them "
                     "out for the remote model server (memory_remote_classes).")
NEEDS_MODELS = ("{command} needs the knowledge agent's models, and the knowledge agent is disabled "
                "(deterministic-only mode).")
KNOWLEDGE_INVALID = "knowledge_agent in config.json is {value}; it must be enabled or disabled."
RESTART_FOR_MODELS = ("knowledge_agent is enabled in config.json, but this service started without models. Restart "
                      "the knowledge service (cairnmind stop, then start Cairnmind again) so that it uses them.")

MESSAGES = {
    WORKSPACE_NEEDS: "Środowisko terminalowe wymaga tmux 3.4 lub nowszego.",
    TMUX_NOT_FOUND: "Nie znaleziono tmux.",
    TMUX_FOUND: "Znaleziono {version}.",
    TMUX_SILENT: "tmux nie podał swojej wersji.",
    WITHOUT_WORKSPACE: ("Bez niego działają polecenia: cairnmind serve, cairnmind run, cairnmind mcp i "
                        "cairnmind brief."),
    COCKPIT_MISSING: "niedostępny (nie znaleziono tmux 3.4+)",
    COCKPIT_OLD: "niedostępny (nie znaleziono tmux 3.4+; znaleziono {version})",
    COCKPIT_OTHER: "niedostępny (nie znaleziono tmux 3.4+; {code})",
    SERVICE_NOT_STARTED: ("Usługa Cairnmind nie działa i nie udało się jej uruchomić: {reason} Uruchom ją "
                          "poleceniem `{command}`, a potem wywołaj narzędzie ponownie."),
    KNOWLEDGE_DISABLED: "wyłączony (tryb tylko deterministyczny)",
    MODELS_NOT_CONFIGURED: "nieskonfigurowane (tryb tylko deterministyczny)",
    GET_MODELS: ("Aby używać modeli: `cairnmind models pull` pobiera lokalne modele do Ollamy (około 7 GB); "
                 "`cairnmind models set` wybiera serwer modeli."),
    SERVER_DOWN: "serwer modeli {url} nie odpowiada",
    MODEL_MISSING: "serwer modeli {url} nie ma modelu {model}",
    CHOSE_ENABLED: ("Agent wiedzy: włączony. Serwer modeli odpowiada i ma modele {chat} i {embedding}. "
                    "Zapisano jako knowledge_agent w {config}."),
    CHOSE_DISABLED: ("Agent wiedzy: wyłączony (tryb tylko deterministyczny), ponieważ {reason}. Plany, zadania, "
                     "kontrole, brief, impact, pamięć i indeksowanie plików działają bez modeli; cairnmind ask "
                     "nie tworzy odpowiedzi i wymienia fragmenty zawierające słowa pytania. Zapisano jako knowledge_agent w {config}."),
    TURNED_ON: ("Agent wiedzy: włączony, zapisano jako knowledge_agent w {config}. Uruchom ponownie usługę wiedzy "
                "(cairnmind stop, a potem ponownie uruchom Cairnmind), aby używała modeli. Aby go znowu wyłączyć, "
                "ustaw knowledge_agent na disabled w tym pliku i uruchom usługę ponownie."),
    ASK_OFF: ("Agent wiedzy jest wyłączony (tryb tylko deterministyczny), więc nie powstała żadna odpowiedź. "
              "Fragmenty wymienione jako dopasowania pełnotekstowe zawierają słowa pytania: to wyniki wyszukiwania, "
              "a nie odpowiedź ani zweryfikowane dowody na nią."),
    ASK_NO_MATCH: ("Agent wiedzy jest wyłączony (tryb tylko deterministyczny), więc nie powstała żadna odpowiedź. "
                   "Wyszukiwanie pełnotekstowe nie znalazło żadnego fragmentu ze słowami pytania."),
    MATCHES_HEADING: "Dopasowania pełnotekstowe (wyniki wyszukiwania, nie odpowiedź)",
    MATCHES_HELD_BACK: ("Część pasujących fragmentów pominięto ze względu na ich klasę wrażliwości, tak jak "
                        "odpowiedzi pomijają je dla zdalnego serwera modeli (memory_remote_classes)."),
    NEEDS_MODELS: ("{command} wymaga modeli agenta wiedzy, a agent wiedzy jest wyłączony "
                   "(tryb tylko deterministyczny)."),
    KNOWLEDGE_INVALID: "knowledge_agent w config.json ma wartość {value}; musi to być enabled albo disabled.",
    RESTART_FOR_MODELS: ("knowledge_agent jest włączony w config.json, ale ta usługa wystartowała bez modeli. "
                         "Uruchom ponownie usługę wiedzy (cairnmind stop, a potem ponownie uruchom Cairnmind), aby "
                         "używała modeli."),
}


def _version(found: dict) -> str:
    """The first line `tmux -V` printed, on one line and short (it is another program's output)."""
    return " ".join(str(found.get("version") or "").split())[:80]


def cockpit_status(tmux: dict) -> str | None:
    """What doctor says about the workspace for a tmux block of NativeRuntime.diagnostics; None when ready."""
    if tmux.get("code") == "ready":
        return None
    version = _version(tmux)
    if version:
        return COCKPIT_OLD.format(version=version)
    if tmux.get("code") == "tmux_missing":
        return COCKPIT_MISSING
    return COCKPIT_OTHER.format(code=tmux.get("code") or "unknown")


def workspace_refusal(found: dict) -> str:
    """The one message opening the workspace prints without tmux 3.4+ (found: WorkspaceManager.diagnostics)."""
    version = safe_text(_version(found))
    if version:
        state = TMUX_FOUND.format(version=version)
    elif found.get("code") in {"tmux_unavailable", "tmux_unsupported"}:
        state = TMUX_SILENT
    else:
        state = TMUX_NOT_FOUND
    hint = platform_hints.hint("tmux_launch").rstrip(".") + "."
    return " ".join((WORKSPACE_NEEDS, state, hint, WITHOUT_WORKSPACE))
