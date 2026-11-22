"""More, then Settings, then Storage and models: where storage and the memory models run, and how to move them (#74).

The workspace works through the knowledge service, and `cairnmind setup PROFILE` is refused while that service runs,
so this screen changes nothing and tests nothing. It shows the topology the running service loaded (its /status, the
service's own read), what the two marks of the status row abbreviate, and for each profile the command a person runs
after leaving the workspace and stopping the service. setup_profiles has no plan without its tests (_plan_lines reads
the tested candidate), so the exact plan is the command's own dry run; nothing here repeats it. One request, a GET.
"""
from __future__ import annotations

from .setup_profiles import MODEL_OPTIONS, OPTION_NAMES, REMOTE_OPTIONS
from .terminal_safe import safe_text

TITLE = "Storage and models"
NOW = ("Where storage and the memory models run, as the running knowledge service loaded its settings when it "
       "started:")
CHIPS = "In the status row, when the terminal is wide enough, this reads {chips}."
CHIP_PROBLEM = "In the problem colour: {marks}. The lines above say why."
LOADED = ("A change saved in config.json since the service started is not shown here and is not in use; it takes "
          "effect after `cairnmind stop` and the next start. The marks of the status row are what the workspace "
          "loaded when it started; `cairnmind workspace stop`, then `cairnmind`, loads them again.")
WHY_HERE = ("Nothing is changed here. The workspace works through the knowledge service, and `cairnmind setup` "
            "refuses to move storage or the models while that service runs, so a change made from here would either "
            "be refused or stop the service under the workspace. No target is tested here either: `cairnmind setup` "
            "tests it, before it saves anything.")
HOW = ("To change it: leave the workspace with Ctrl+B, then d (it keeps running), and in that terminal stop the "
       "service, then run the command of the profile you want. Without --yes the command only tests the target and "
       "prints what it will change and what it leaves as it is; nothing changes. Run it again with --yes to make "
       "the change, then do what it says next. A coding session cannot run it.")
PLACEHOLDERS = "Words in capitals are yours to fill in; no command here takes a password or a key."
LOCAL_HEAD = "The engine Cairnmind manages on this machine, and the memory models on this computer (local):"
LOCAL_STAYS = "Storage already runs on this machine and stays there."
LOCAL_BACK = (
    "Storage moves from the store at {url} to the engine Cairnmind manages on this machine, which the command "
    "starts; nothing is installed (the native engine needs `cairnmind engine install` first). With --backup, the "
    "store at {url} is first read into FILE, a new file, and nothing is written to that store; the file is checked, "
    "then restored here. With --empty, the store on this machine is used without that data. Either way the store at "
    "{url} is left as it is.")
KEEP_OPEN = ("With --backup, keep the workspace running (leave it with Ctrl+B, then d, rather than closing it): a "
             "person's confirmations in the backup are kept only while it answers with its key.")
MODELS_HOME = ("A memory model on another computer moves to the Ollama on this computer, with this release's model "
               "for its role, once a test shows that Ollama serves it; the model server it stops using is left as it "
               "is. A disabled knowledge agent stays disabled.")
REMOTE_DB_HEAD = ("A database this installation only connects to, and the memory models on this computer "
                  "(remote-db):")
REMOTE_DB = (
    "Storage moves to the ArcadeDB at DB_URL, which this installation then only connects to. The command tests that "
    "it answers with a supported version and that USER signs in to DATABASE with the password in PASSWORD_FILE, a "
    "private file (mode 0600) you created; only the file's path is saved. It reads which installation writes that "
    "store and never takes it over. The store used before is left as it is, with its data; the command names it.")
OPTIONAL_DB = (
    "Optional: --ca-file CA_FILE checks the server's certificate against a private certificate authority; "
    "--admin-password-file ADMIN_PASSWORD_FILE, a private file with the server's root password, makes --yes create "
    "the user and the database first, as `cairnmind db create-user` does, and is never saved.")
REMOTE_HEAD = ("A database this installation only connects to, and the memory models on another computer "
               "(remote):")
REMOTE = (
    "Storage moves as with remote-db, with the same tests. Both memory models move to the model server at "
    "MODELS_URL (HTTPS on another computer); PROVIDER is ollama or openai_compatible, and CHAT_MODEL and "
    "EMBEDDING_MODEL are the models' names there. --allow-remote is your consent that this server receives your "
    "questions and the memory text of the classes in memory_remote_classes. Each model is tested before anything is "
    "saved, a disabled knowledge agent is turned on, and when the embedding model changes the command says that the "
    "vectors must be rebuilt afterwards (`cairnmind rebuild-vectors PROJECT`).")
OPTIONAL_KEY = ("Optional: --credential-ref env:NAME or --credential-ref profile:ID names where the server's key is "
                "kept; the key itself is never typed here, shown or saved.")
UNREADABLE = ("The knowledge service did not say where storage and the models run, so nothing is shown here; "
              "`cairnmind doctor` in a terminal shows it.")

MESSAGES = {
    TITLE: "Baza i modele",
    NOW: "Gdzie działają baza i modele pamięci, według ustawień, które działająca usługa wiedzy wczytała przy "
         "starcie:",
    CHIPS: "W wierszu stanu, gdy terminal jest dość szeroki, wygląda to tak: {chips}.",
    CHIP_PROBLEM: "W kolorze problemu: {marks}. Wiersze powyżej mówią dlaczego.",
    LOADED: "Zmiana zapisana w config.json od startu usługi nie jest tu pokazana i nie jest używana; zaczyna "
            "działać po `cairnmind stop` i następnym starcie. Oznaczenia w wierszu stanu to to, co środowisko "
            "wczytało przy swoim starcie; `cairnmind workspace stop`, a potem `cairnmind`, wczytuje je ponownie.",
    WHY_HERE: "Tutaj nic się nie zmienia. Środowisko działa przez usługę wiedzy, a `cairnmind setup` odmawia "
              "przeniesienia bazy lub modeli, dopóki ta usługa działa, więc zmiana stąd zostałaby odrzucona albo "
              "zatrzymałaby usługę pod środowiskiem. Tutaj nie jest też sprawdzany żaden cel: sprawdza go "
              "`cairnmind setup`, zanim cokolwiek zapisze.",
    HOW: "Aby to zmienić: wyjdź ze środowiska przez Ctrl+B, potem d (działa ono dalej), i w tym terminalu zatrzymaj "
         "usługę, a potem uruchom polecenie wybranego profilu. Bez --yes polecenie tylko sprawdza cel i wypisuje, "
         "co zmieni i co zostawi bez zmian; nic się nie zmienia. Uruchom je ponownie z --yes, aby wprowadzić "
         "zmianę, a potem zrób to, co każe dalej. Sesja programistyczna nie może go uruchomić.",
    PLACEHOLDERS: "Słowa wielkimi literami wypełniasz sam; żadne polecenie tutaj nie przyjmuje hasła ani klucza.",
    LOCAL_HEAD: "Silnik zarządzany przez Cairnmind na tym komputerze i modele pamięci na tym komputerze (local):",
    LOCAL_STAYS: "Baza już działa na tym komputerze i tu zostaje.",
    LOCAL_BACK: "Baza przenosi się z bazy pod adresem {url} na silnik zarządzany przez Cairnmind na tym komputerze, "
                "który polecenie uruchamia; nic nie jest instalowane (silnik natywny wymaga najpierw `cairnmind "
                "engine install`). Z --backup baza pod adresem {url} jest najpierw czytana do FILE, nowego pliku, "
                "i nic nie jest do niej zapisywane; plik jest sprawdzany, a potem odtwarzany tutaj. Z --empty baza "
                "na tym komputerze jest używana bez tych danych. W obu przypadkach baza pod adresem {url} zostaje "
                "bez zmian.",
    KEEP_OPEN: "Przy --backup niech środowisko działa (wyjdź z niego przez Ctrl+B, potem d, zamiast je zamykać): "
               "potwierdzenia osoby w kopii zapasowej są zachowywane tylko wtedy, gdy odpowiada ono swoim kluczem.",
    MODELS_HOME: "Model pamięci na innym komputerze przenosi się do Ollamy na tym komputerze, z modelem tego "
                 "wydania dla swojej roli, gdy test pokaże, że ta Ollama go udostępnia; serwer modeli, którego "
                 "przestaje używać, zostaje bez zmian. Wyłączony agent wiedzy pozostaje wyłączony.",
    REMOTE_DB_HEAD: "Baza, z którą ta instalacja tylko się łączy, i modele pamięci na tym komputerze (remote-db):",
    REMOTE_DB: "Baza przenosi się do ArcadeDB pod adresem DB_URL, z którą ta instalacja odtąd tylko się łączy. "
               "Polecenie sprawdza, że odpowiada ona w obsługiwanej wersji i że USER loguje się do DATABASE hasłem "
               "z PASSWORD_FILE, prywatnego pliku (tryb 0600), który utworzyłeś; zapisywana jest tylko ścieżka do "
               "pliku. Odczytuje, która instalacja zapisuje tę bazę, i nigdy jej nie przejmuje. Poprzednio używana "
               "baza zostaje bez zmian, z danymi; polecenie ją wymienia.",
    OPTIONAL_DB: "Opcjonalnie: --ca-file CA_FILE sprawdza certyfikat serwera względem prywatnego urzędu "
                 "certyfikacji; --admin-password-file ADMIN_PASSWORD_FILE, prywatny plik z hasłem roota serwera, "
                 "sprawia, że --yes najpierw tworzy użytkownika i bazę, tak jak `cairnmind db create-user`, i "
                 "nigdy nie jest zapisywany.",
    REMOTE_HEAD: "Baza, z którą ta instalacja tylko się łączy, i modele pamięci na innym komputerze (remote):",
    REMOTE: "Baza przenosi się tak jak przy remote-db, z tymi samymi testami. Oba modele pamięci przenoszą się na "
            "serwer modeli pod adresem MODELS_URL (HTTPS na innym komputerze); PROVIDER to ollama albo "
            "openai_compatible, a CHAT_MODEL i EMBEDDING_MODEL to nazwy modeli na tym serwerze. --allow-remote to "
            "twoja zgoda, by ten serwer otrzymywał twoje pytania i tekst pamięci z klas w "
            "memory_remote_classes. Każdy model jest sprawdzany, zanim cokolwiek zostanie zapisane, wyłączony "
            "agent wiedzy zostaje włączony, a gdy zmienia się model embeddingu, polecenie mówi, że wektory trzeba "
            "potem przebudować (`cairnmind rebuild-vectors PROJECT`).",
    OPTIONAL_KEY: "Opcjonalnie: --credential-ref env:NAME albo --credential-ref profile:ID wskazuje, gdzie jest "
                  "klucz serwera; samego klucza nigdy się tu nie wpisuje, nie pokazuje ani nie zapisuje.",
    UNREADABLE: "Usługa wiedzy nie podała, gdzie działają baza i modele, więc nic tu nie jest pokazane; pokazuje to "
                "`cairnmind doctor` w terminalu.",
}

PLACEHOLDERS_OF = {"url": "DB_URL", "user": "USER", "database": "DATABASE", "password_file": "PASSWORD_FILE",
                   "models_url": "MODELS_URL", "provider": "PROVIDER", "chat_model": "CHAT_MODEL",
                   "embedding_model": "EMBEDDING_MODEL"}


def commands(profile: str, connect_only: bool) -> list[list[str]]:
    """The argument lists of `cairnmind setup PROFILE` without --yes, with capitalised placeholders for the values."""
    base = ["cairnmind", "setup", profile]
    if profile == "local":
        return [base + ["--backup", "FILE"], base + ["--empty"]] if connect_only else [base]
    names = REMOTE_OPTIONS if profile == "remote-db" else REMOTE_OPTIONS + MODEL_OPTIONS
    argv = base + [word for name in names for word in (OPTION_NAMES[name], PLACEHOLDERS_OF[name])]
    return [argv + ([OPTION_NAMES["allow_remote"]] if profile == "remote" else [])]


def shown(argv: list[str]) -> list[str]:
    """One command as screen lines: a short one on one line; a long one with each option (and its value) on a
    continuation line ending in ` \\`, so it reads at 80 columns and pastes into a shell."""
    if len(argv) <= 5:
        return ["  " + " ".join(argv)]
    parts, rest = [" ".join(argv[:3])], argv[3:]
    while rest:
        take = 2 if len(rest) > 1 and not rest[1].startswith("--") else 1
        parts.append(" ".join(rest[:take]))
        rest = rest[take:]
    return (["  " + parts[0] + " \\"]
            + ["      " + part + (" \\" if index < len(parts) - 2 else "") for index, part in enumerate(parts[1:])])


def describe(status, language: str) -> str:
    """The screen's text from the service's /status answer. Any other shape raises ValueError(UNREADABLE): nothing
    is shown in its place, never the settings of this process."""
    from . import topology
    from .terminal_i18n import tr  # imported here: terminal_i18n merges MESSAGES of this module
    place = status.get("topology") if isinstance(status, dict) else None
    try:
        if not isinstance(place, dict) or place["storage"]["mode"] not in ("managed", "connect"):
            raise ValueError
        three = topology.lines(place, language)
        marks = topology.status_words(place)
        connect = place["storage"]["mode"] == "connect"
        url = safe_text(place["storage"]["url"], limit=200)
    except (KeyError, TypeError, AttributeError, ValueError, IndexError):
        raise ValueError(UNREADABLE) from None
    labels = (tr(topology.STATUS_STORAGE, language), tr(topology.STATUS_MODELS, language))
    chips = " | ".join(f"{label}:{safe_text(tr(word, language), limit=80)}"
                       for label, (word, _) in zip(labels, marks))
    paragraphs = [tr(NOW, language), *three, tr(CHIPS, language, chips=chips)]
    bad = [label for label, (_, problem) in zip(labels, marks) if problem]
    if bad:
        paragraphs[-1] += " " + tr(CHIP_PROBLEM, language, marks=", ".join(bad))
    paragraphs += [tr(LOADED, language), tr(WHY_HERE, language), tr(HOW, language) + "\n\n  cairnmind stop",
                   tr(PLACEHOLDERS, language)]

    def block(head, profile, *sentences):
        lines = [tr(head, language)] + [line for argv in commands(profile, connect) for line in shown(argv)]
        return "\n".join(lines) + "\n" + " ".join(sentences)

    local = ((tr(LOCAL_BACK, language, url=url), tr(MODELS_HOME, language), tr(KEEP_OPEN, language)) if connect
             else (tr(LOCAL_STAYS, language), tr(MODELS_HOME, language)))
    paragraphs += [block(LOCAL_HEAD, "local", *local),
                   block(REMOTE_DB_HEAD, "remote-db", tr(REMOTE_DB, language), tr(MODELS_HOME, language),
                         tr(OPTIONAL_DB, language)),
                   block(REMOTE_HEAD, "remote", tr(REMOTE, language), tr(OPTIONAL_KEY, language))]
    return "\n\n".join(paragraphs)


async def open_view(app) -> None:
    """Read the running service's topology and show it; one GET, nothing written."""
    from .terminal_ui import MessageModal
    status = await app.request("GET", "/api/status")
    await app.push_screen_wait(MessageModal(TITLE, describe(status, getattr(app, "language", "en"))))
