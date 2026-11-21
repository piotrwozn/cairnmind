"""Where storage and the memory models run, and what runs on this machine (#74): a pure read.

`topology(settings)` reads only settings that already exist, through the functions that already compute each part
(database_mode, database_credentials.problem, knowledge_mode, memory_models, memory_egress). It makes no network
call, starts no process and reads and writes no file. What needs a request is handed in by the caller: `models`
(memory_models.doctor_models, which asks each model server) and `writer` (store_writer.inspect, or the running
store's writer_state). A stored value that cannot be used is a named problem, never a default in its place.

Where a value came from is Settings.load's record (`_origin["environment"]`: field -> the variable's spelling).
"config.json" here means "not from a variable": config.json's own value, or the built-in default when an old
config.json lacks the key. The same rule every command applies.

The three lines say where things run by configuration, not whether they answer now: that stays in the `arcadedb`,
`ollama` and `memory_models` blocks of doctor.
"""
from __future__ import annotations

from .terminal_safe import safe_text

STORAGE_NATIVE = "Storage: ArcadeDB on this machine at {url}, started and stopped by Cairnmind (native engine, Java 21)."
STORAGE_DOCKER = "Storage: ArcadeDB on this machine at {url}, started and stopped by Cairnmind (engine in Docker)."
STORAGE_ENGINE_UNKNOWN = ("Storage: ArcadeDB on this machine at {url}, managed by Cairnmind, but its engine mode "
                          "cannot be used: {problem}")
STORAGE_CONNECT_HERE = ("Storage: ArcadeDB at {url} on this machine, which this installation only connects to; it is "
                        "not installed, started or stopped here.")
STORAGE_CONNECT_REMOTE = ("Storage: ArcadeDB at {url} on {host}, which this installation only connects to; it is not "
                          "installed, started or stopped here.")
FROM_VARIABLE = "The address comes from {variable}, for this process only."
STORAGE_PROBLEM = "It cannot be used: {problem}"
WRITER_YES = "This installation writes it."
WRITER_UNKNOWN = "Whether this installation writes it is unknown: {reason}"

MODELS_DISABLED = "Memory models: none in use; the knowledge agent is disabled (deterministic-only mode)."
MODELS_INVALID = "Memory models: they cannot be used: {problem}"
MODELS_BOTH = "Memory models: chat {chat} and embedding {embedding}, both on {place}."
MODELS_SPLIT = "Memory models: chat {chat} on {chat_place}; embedding {embedding} on {embedding_place}."
PLACE_HERE = "this machine ({provider} at {url})"
PLACE_REMOTE = ("{host}, another computer ({provider} at {url}), which receives questions and memory text of the "
                "classes {classes}")
PLACE_REMOTE_NO_TEXT = "{host}, another computer ({provider} at {url}), which receives questions and no memory text"
OPENAI_COMPATIBLE = "an OpenAI-compatible server"
OLLAMA = "Ollama"  # a name, not translated
OLLAMA_FROM_VARIABLE = "The Ollama address comes from {variable}, for this process only."
EVALUATED_RELEASE = "These are the release's models, evaluated for this release."
EVALUATED_PASSED = "Evaluated with these models: cairnmind eval run and cairnmind eval memory passed."
EVALUATED_NOT = "Not evaluated with these models: run cairnmind eval run and cairnmind eval memory."
EVALUATED_UNKNOWN = "Whether they were evaluated is unknown: a model server did not name its model."
EVALUATED_NOT_CHECKED = "Whether they were evaluated is not checked here; cairnmind doctor checks it."

THIS_MACHINE = "On this machine: {items}."
ITEM_SERVICE = "the knowledge service at {url}"
ITEM_SERVICE_VARIABLE = "the knowledge service at {url} (port from {variable}, for this process only)"
ITEM_NO_SERVICE = "no knowledge service, because this installation only reads the store"
ITEM_ENGINE_NATIVE = "the ArcadeDB engine (native, Java 21)"
ITEM_ENGINE_DOCKER = "the ArcadeDB engine in Docker"
ITEM_ENGINE_UNKNOWN = "the ArcadeDB engine, in an engine mode that cannot be used"
ITEM_DATABASE_HERE = "ArcadeDB at {url}, which this installation does not manage"
ITEM_MODELS_BOTH = "{provider} at {url} for chat and embedding"
ITEM_MODELS_CHAT = "{provider} at {url} for chat"
ITEM_MODELS_EMBEDDING = "{provider} at {url} for embedding"

CHANGE_HINT = ("To change it: `cairnmind engine start --native` or `cairnmind engine start --docker` (where the engine "
               "Cairnmind manages runs), `cairnmind models set` (the memory models), and arcade_connect_url in "
               "config.json for a database Cairnmind only connects to (docs/DEPLOYMENT.md).")

# The words of the workspace status row.
STATUS_STORAGE = "Storage"
STATUS_MODELS = "Models"
WORD_LOCAL = "local"
WORD_LOCAL_DOCKER = "local (Docker)"
WORD_CONNECT_HERE = "local (connect-only)"
WORD_REMOTE = "remote"
WORD_MIXED = "mixed"
WORD_OFF = "off"
WORD_INVALID = "invalid"

MESSAGES = {
    STORAGE_NATIVE: "Baza: ArcadeDB na tym komputerze pod adresem {url}, uruchamiana i zatrzymywana przez Cairnmind (silnik natywny, Java 21).",
    STORAGE_DOCKER: "Baza: ArcadeDB na tym komputerze pod adresem {url}, uruchamiana i zatrzymywana przez Cairnmind (silnik w Dockerze).",
    STORAGE_ENGINE_UNKNOWN: "Baza: ArcadeDB na tym komputerze pod adresem {url}, zarządzana przez Cairnmind, ale jej trybu silnika nie można użyć: {problem}",
    STORAGE_CONNECT_HERE: "Baza: ArcadeDB pod adresem {url} na tym komputerze; ta instalacja tylko się z nią łączy, nie instaluje jej, nie uruchamia ani nie zatrzymuje.",
    STORAGE_CONNECT_REMOTE: "Baza: ArcadeDB pod adresem {url} na {host}; ta instalacja tylko się z nią łączy, nie instaluje jej, nie uruchamia ani nie zatrzymuje.",
    FROM_VARIABLE: "Adres pochodzi z {variable}, tylko dla tego procesu.",
    STORAGE_PROBLEM: "Nie można jej użyć: {problem}",
    WRITER_YES: "Ta instalacja do niej zapisuje.",
    WRITER_UNKNOWN: "Nie wiadomo, czy ta instalacja do niej zapisuje: {reason}",
    MODELS_DISABLED: "Modele pamięci: żadne nie są używane; agent wiedzy jest wyłączony (tryb tylko deterministyczny).",
    MODELS_INVALID: "Modele pamięci: nie można ich użyć: {problem}",
    MODELS_BOTH: "Modele pamięci: czat {chat} i embedding {embedding}, oba na {place}.",
    MODELS_SPLIT: "Modele pamięci: czat {chat} na {chat_place}; embedding {embedding} na {embedding_place}.",
    PLACE_HERE: "tym komputerze ({provider} pod adresem {url})",
    PLACE_REMOTE: "{host}, innym komputerze ({provider} pod adresem {url}), który otrzymuje pytania i tekst pamięci z klas {classes}",
    PLACE_REMOTE_NO_TEXT: "{host}, innym komputerze ({provider} pod adresem {url}), który otrzymuje pytania, ale żadnego tekstu pamięci",
    OPENAI_COMPATIBLE: "serwer zgodny z OpenAI",
    OLLAMA_FROM_VARIABLE: "Adres Ollamy pochodzi z {variable}, tylko dla tego procesu.",
    EVALUATED_RELEASE: "To modele tego wydania, ocenione dla tego wydania.",
    EVALUATED_PASSED: "Ocenione z tymi modelami: cairnmind eval run i cairnmind eval memory przeszły.",
    EVALUATED_NOT: "Nieocenione z tymi modelami: uruchom cairnmind eval run i cairnmind eval memory.",
    EVALUATED_UNKNOWN: "Nie wiadomo, czy zostały ocenione: serwer modeli nie podał nazwy swojego modelu.",
    EVALUATED_NOT_CHECKED: "Tutaj nie sprawdzono, czy zostały ocenione; sprawdza to cairnmind doctor.",
    THIS_MACHINE: "Na tym komputerze: {items}.",
    ITEM_SERVICE: "usługa wiedzy pod adresem {url}",
    ITEM_SERVICE_VARIABLE: "usługa wiedzy pod adresem {url} (port z {variable}, tylko dla tego procesu)",
    ITEM_NO_SERVICE: "brak usługi wiedzy, bo ta instalacja tylko czyta bazę",
    ITEM_ENGINE_NATIVE: "silnik ArcadeDB (natywny, Java 21)",
    ITEM_ENGINE_DOCKER: "silnik ArcadeDB w Dockerze",
    ITEM_ENGINE_UNKNOWN: "silnik ArcadeDB w trybie, którego nie można użyć",
    ITEM_DATABASE_HERE: "ArcadeDB pod adresem {url}, którą ta instalacja nie zarządza",
    ITEM_MODELS_BOTH: "{provider} pod adresem {url} dla czatu i embeddingu",
    ITEM_MODELS_CHAT: "{provider} pod adresem {url} dla czatu",
    ITEM_MODELS_EMBEDDING: "{provider} pod adresem {url} dla embeddingu",
    CHANGE_HINT: "Aby to zmienić: `cairnmind engine start --native` lub `cairnmind engine start --docker` (gdzie działa silnik zarządzany przez Cairnmind), `cairnmind models set` (modele pamięci) oraz arcade_connect_url w config.json dla bazy, z którą Cairnmind tylko się łączy (docs/DEPLOYMENT.md).",
    STATUS_STORAGE: "Baza",
    STATUS_MODELS: "Modele",
    WORD_LOCAL: "lokalnie",
    WORD_LOCAL_DOCKER: "lokalnie (Docker)",
    WORD_CONNECT_HERE: "lokalnie (tylko połączenie)",
    WORD_REMOTE: "zdalnie",
    WORD_MIXED: "mieszane",
    WORD_OFF: "wyłączone",
    WORD_INVALID: "nieprawidłowe",
}


def _role(settings, role: str, environment: dict):
    """One memory model role: (the block, None) or (the block, the reason it cannot be used)."""
    from urllib.parse import urlsplit
    from . import memory_egress
    from .database_mode import shown
    from .memory_models import MODEL_FIELDS, is_loopback, profile
    model = getattr(settings, MODEL_FIELDS[role])
    try:
        row = profile(settings, role)
    except ValueError as exc:
        return {"model": model, "problem": f"Invalid profile: {exc}"}, str(exc)
    unprofiled = row is None
    row = row or {"provider": "ollama", "base_url": settings.ollama_url}
    try:
        local, host = is_loopback(row["base_url"]), urlsplit(row["base_url"]).hostname
    except (ValueError, TypeError, AttributeError):  # an ollama_url that is not an address
        message = "ollama_url is not a valid address."
        return {"model": model, "problem": message}, message
    block = {"model": model, "provider": row["provider"], "url": shown(row["base_url"]), "host": host,
             "on_this_machine": local, "source": "config.json"}
    if unprofiled and "ollama_url" in environment:  # the role has no profile: it uses settings.ollama_url
        block["source"], block["variable"] = "environment", environment["ollama_url"]
    if not local:
        block["sends"] = memory_egress.describe(settings, role)["sends"]
    return block, None


def _evaluated(settings, models: dict | None, roles: dict) -> str:
    from .memory_models import is_default
    if is_default(settings):
        return "release"
    if models is None:
        return "not checked"
    if models.get("evaluated") is True:
        return "passed"
    if all(isinstance(models.get(role), dict) and models[role].get("identity") for role in roles):
        return "not evaluated"
    return "unknown"


def topology(settings, *, models: dict | None = None, writer: dict | None = None) -> dict:
    """The topology of ``settings``. ``models``: memory_models.doctor_models' result; ``writer``: the writer role."""
    from . import database_credentials, database_mode, knowledge_mode
    from .memory_models import ROLES
    environment = (getattr(settings, "_origin", None) or {}).get("environment", {})

    named = database_mode.CONNECT_SETTING if settings.arcade_connect_url else "arcade_url"
    storage = {**database_mode.describe(settings), "url": database_mode.address(settings),
               "on_this_machine": database_mode.on_this_machine(settings), "setting": named,
               "source": "environment" if named in environment else "config.json"}
    if named in environment:
        storage["variable"] = environment[named]
    reason = database_credentials.problem(settings)
    if reason:
        storage["problem"] = reason
    if writer is not None:
        storage["writer"] = {key: writer[key] for key in ("role", "reason") if key in writer}

    chosen: dict = {}
    try:
        on = knowledge_mode.enabled(settings)
    except ValueError as exc:
        chosen = {"knowledge_agent": "invalid", "problem": str(exc), "evaluated": None}
    else:
        if not on:
            chosen = {"knowledge_agent": "disabled", "evaluated": None}
        else:
            blocks = {role: _role(settings, role, environment) for role in ROLES}
            chosen = {"knowledge_agent": "enabled", **{role: blocks[role][0] for role in ROLES}}
            failed = next(((role, why) for role in ROLES if (why := blocks[role][1])), None)
            if failed:
                chosen = {"knowledge_agent": "enabled", "problem": f"{failed[0]}: {failed[1]}", **chosen, "evaluated": None}
            else:
                chosen["evaluated"] = _evaluated(settings, models, {role: blocks[role][0] for role in ROLES})

    items: list[dict] = []
    if (writer or {}).get("role") == "reader":  # a reader never runs the service (#70)
        items.append({"kind": "no_service"})
    else:
        service = {"kind": "service", "url": settings.url}
        if "port" in environment:
            service["variable"] = environment["port"]
        items.append(service)
    if storage["mode"] == "managed":
        items.append({"kind": "engine", "engine": storage["engine"]})
    elif storage["on_this_machine"]:
        items.append({"kind": "database", "url": storage["url"]})
    if chosen["knowledge_agent"] == "enabled" and "problem" not in chosen:
        servers: dict = {}
        for role in ROLES:
            if chosen[role]["on_this_machine"]:
                servers.setdefault((chosen[role]["provider"], chosen[role]["url"]), []).append(role)
        items += [{"kind": "model_server", "provider": provider, "url": url, "roles": roles}
                  for (provider, url), roles in servers.items()]

    place = {"lines": [], "storage": storage, "models": chosen, "this_machine": items}
    place["lines"] = lines(place, "en")
    return place


def probe(settings) -> dict:
    """What `cairnmind setup` shows: the requests doctor makes (read-only), then the topology."""
    import httpx
    from .memory_models import doctor_models
    from .store_writer import inspect
    with httpx.Client(timeout=1.5, trust_env=False) as http:
        models = doctor_models(settings, http)
    return topology(settings, models=models, writer=inspect(settings))


# --- The sentences -------------------------------------------------------------------------------------------------

def _say(template: str, language: str, **values) -> str:
    # terminal_i18n merges MESSAGES of this module: it is imported here, not at the top, to avoid a cycle.
    if language == "en":
        return template.format(**values)
    from .terminal_i18n import tr
    return tr(template, language, **values)


def _safe(value, limit: int = 200) -> str:
    return safe_text(value, limit=limit)


def _storage_line(place: dict, language: str) -> str:
    storage = place["storage"]
    url = _safe(storage["url"])
    if storage["mode"] == "managed":
        if storage["engine"] == "native":
            parts = [_say(STORAGE_NATIVE, language, url=url)]
        elif storage["engine"] == "docker":
            parts = [_say(STORAGE_DOCKER, language, url=url)]
        else:
            parts = [_say(STORAGE_ENGINE_UNKNOWN, language, url=url, problem=_safe(storage.get("engine_problem", ""), 400))]
    elif storage["on_this_machine"]:
        parts = [_say(STORAGE_CONNECT_HERE, language, url=url)]
    else:
        parts = [_say(STORAGE_CONNECT_REMOTE, language, url=url, host=_safe(storage["host"]))]
    if "variable" in storage:
        parts.append(_say(FROM_VARIABLE, language, variable=_safe(storage["variable"])))
    if "problem" in storage:
        parts.append(_say(STORAGE_PROBLEM, language, problem=_safe(storage["problem"], 400)))
    writer = storage.get("writer")
    if writer:
        reason = _safe(writer.get("reason", ""), 400)
        if writer["role"] == "writer":
            parts.append(_say(WRITER_YES, language))
        elif writer["role"] == "unknown":
            parts.append(_say(WRITER_UNKNOWN, language, reason=reason))
        elif reason:  # reader, lost, none: the store's own sentence
            parts.append(reason)
    return " ".join(parts)


def _provider(block: dict, language: str) -> str:
    return OLLAMA if block["provider"] == "ollama" else _say(OPENAI_COMPATIBLE, language)


def _place(block: dict, language: str) -> str:
    values = {"provider": _provider(block, language), "url": _safe(block["url"])}
    if block["on_this_machine"]:
        return _say(PLACE_HERE, language, **values)
    values["host"] = _safe(block["host"])
    if block.get("sends"):
        return _say(PLACE_REMOTE, language, classes=", ".join(block["sends"]), **values)
    return _say(PLACE_REMOTE_NO_TEXT, language, **values)


EVALUATION_SENTENCES = {"release": EVALUATED_RELEASE, "passed": EVALUATED_PASSED, "not evaluated": EVALUATED_NOT,
                        "unknown": EVALUATED_UNKNOWN, "not checked": EVALUATED_NOT_CHECKED}


def _models_line(place: dict, language: str) -> str:
    models = place["models"]
    if models["knowledge_agent"] == "disabled":
        return _say(MODELS_DISABLED, language)
    if "problem" in models:
        return _say(MODELS_INVALID, language, problem=_safe(models["problem"], 400))
    chat, embedding = models["chat"], models["embedding"]
    names = {"chat": _safe(chat["model"]), "embedding": _safe(embedding["model"])}
    if (chat["provider"], chat["url"]) == (embedding["provider"], embedding["url"]):
        parts = [_say(MODELS_BOTH, language, place=_place(chat, language), **names)]
    else:
        parts = [_say(MODELS_SPLIT, language, chat_place=_place(chat, language),
                      embedding_place=_place(embedding, language), **names)]
    variable = next((block["variable"] for block in (chat, embedding) if "variable" in block), None)
    if variable:
        parts.append(_say(OLLAMA_FROM_VARIABLE, language, variable=_safe(variable)))
    parts.append(_say(EVALUATION_SENTENCES[models["evaluated"]], language))
    return " ".join(parts)


def _item(item: dict, language: str) -> str:
    kind = item["kind"]
    if kind == "service":
        if "variable" in item:
            return _say(ITEM_SERVICE_VARIABLE, language, url=_safe(item["url"]), variable=_safe(item["variable"]))
        return _say(ITEM_SERVICE, language, url=_safe(item["url"]))
    if kind == "no_service":
        return _say(ITEM_NO_SERVICE, language)
    if kind == "engine":
        return _say({"native": ITEM_ENGINE_NATIVE, "docker": ITEM_ENGINE_DOCKER}.get(item["engine"], ITEM_ENGINE_UNKNOWN), language)
    if kind == "database":
        return _say(ITEM_DATABASE_HERE, language, url=_safe(item["url"]))
    template = (ITEM_MODELS_BOTH if len(item["roles"]) == 2 else
                ITEM_MODELS_CHAT if item["roles"] == ["chat"] else ITEM_MODELS_EMBEDDING)
    provider = OLLAMA if item["provider"] == "ollama" else _say(OPENAI_COMPATIBLE, language)
    return _say(template, language, provider=provider, url=_safe(item["url"]))


def lines(place: dict, language: str = "en") -> list[str]:
    """The three sentences, in ``language`` ("en" or "pl"); every value from settings is literal text."""
    items = "; ".join(_item(item, language) for item in place["this_machine"])
    return [_storage_line(place, language), _models_line(place, language), _say(THIS_MACHINE, language, items=items)]


def status_words(place: dict) -> tuple[tuple[str, bool], tuple[str, bool]]:
    """The two chips of the workspace status row: (word, is a problem) for storage, then for models."""
    storage = place["storage"]
    if storage["mode"] == "managed":
        word = {"native": WORD_LOCAL, "docker": WORD_LOCAL_DOCKER}.get(storage["engine"], WORD_INVALID)
    else:
        word = WORD_CONNECT_HERE if storage["on_this_machine"] else storage["host"]  # a host is not translated
    stored = (word, word == WORD_INVALID or "problem" in storage)

    models = place["models"]
    if models["knowledge_agent"] == "disabled":
        return stored, (WORD_OFF, False)
    if "problem" in models:
        return stored, (WORD_INVALID, True)
    here = [models[role]["on_this_machine"] for role in ("chat", "embedding")]
    return stored, (WORD_LOCAL if all(here) else WORD_REMOTE if not any(here) else WORD_MIXED, False)
