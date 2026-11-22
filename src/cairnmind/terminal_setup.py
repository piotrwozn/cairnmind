"""More, then Settings, then Storage and models: where storage and the memory models run, and how to move them (#74, #339).

The read side shows the topology the running service loaded (its /status, the service's own read: the only request
this module sends is that GET), what the two marks of the status row abbreviate, and for each profile the command a
person runs in a terminal after stopping the service. The buttons carry a change out from the workspace instead: each
hands its profile to terminal_setup_change.change_setup, which alone tests the target, asks for the person's
agreement in its consent dialog and starts the helper that stops the service, changes the setup and starts it again.
Nothing in this module changes or tests anything itself. Reopening the screen reads the service that was restarted.
"""
from __future__ import annotations

import functools

from .setup_profiles import MODEL_OPTIONS, OPTION_NAMES, REMOTE_OPTIONS
from .terminal_safe import plain, safe_text

TITLE = "Storage and models"
NOW = ("Where storage and the memory models run, as the running knowledge service loaded its settings when it "
       "started:")
CHIPS = "In the status row, when the terminal is wide enough, this reads {chips}."
CHIP_PROBLEM = "In the problem colour: {marks}. The lines above say why."
LOADED = ("A change saved in config.json since the service started is not shown here and is not in use; it takes "
          "effect when the service starts again. The marks of the status row follow config.json, so they can show a "
          "change that this screen does not show yet.")
HERE = ("To change it here, use a button below: the workspace asks for the values (paths of private files, never a "
        "password or a key), tests the target, and shows the plan and where memory content will go; nothing changes "
        "until you agree. Then it stops the knowledge service, makes the change with the same code as `cairnmind "
        "setup`, and starts the service again; until then memory, Ask, Brief and the memory tools of sessions are "
        "unavailable. It refuses while a session or a job runs. This screen shows the new place when you open it "
        "again.")
BUTTON_LOCAL = "Change to local"
BUTTON_REMOTE_DB = "Change to remote-db"
BUTTON_REMOTE = "Change to remote"
HOW = ("To change it in a terminal instead: leave the workspace with Ctrl+B, then d (it keeps running), and in that "
       "terminal stop the service, then run the command of the profile you want. Without --yes the command only "
       "tests the target and prints what it will change and what it leaves as it is; nothing changes. Run it again "
       "with --yes to make the change, then do what it says next. A coding session cannot run it.")
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
    "This installation switches to the ArcadeDB at DB_URL, which it then only connects to. The command tests that "
    "it answers with a supported version and that USER signs in to DATABASE with the password in PASSWORD_FILE, a "
    "private file (mode 0600) you created; only the file's path is saved. It reads which installation writes that "
    "store and never takes it over. The store used before is left as it is, with its data; the command names it. "
    "Its data is not copied to the new store: run `cairnmind backup FILE` before the command, and `cairnmind init` "
    "and `cairnmind restore FILE` after it.")
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
NO_ANSWER = "Nothing is shown here; `cairnmind doctor` in a terminal shows where storage and the models run."

MESSAGES = {
    NO_ANSWER: "Nic tu nie jest pokazane; `cairnmind doctor` w terminalu pokazuje, gdzie działają baza i modele.",
    TITLE: "Baza i modele",
    NOW: "Gdzie działają baza i modele pamięci, według ustawień, które działająca usługa wiedzy wczytała przy "
         "starcie:",
    CHIPS: "W wierszu stanu, gdy terminal jest dość szeroki, wygląda to tak: {chips}.",
    CHIP_PROBLEM: "W kolorze problemu: {marks}. Wiersze powyżej mówią dlaczego.",
    LOADED: "Zmiana zapisana w config.json od startu usługi nie jest tu pokazana i nie jest używana; zaczyna "
            "działać, gdy usługa uruchomi się ponownie. Oznaczenia w wierszu stanu podążają za config.json, więc "
            "mogą już pokazywać zmianę, której ten ekran jeszcze nie pokazuje.",
    HERE: "Aby to zmienić tutaj, użyj przycisku poniżej: środowisko zapyta o wartości (ścieżki prywatnych plików, "
          "nigdy hasło ani klucz), sprawdzi cel i pokaże plan oraz dokąd trafi treść pamięci; nic się nie zmieni, "
          "dopóki się nie zgodzisz. Potem zatrzyma usługę wiedzy, wprowadzi zmianę tym samym kodem co `cairnmind "
          "setup` i uruchomi usługę ponownie; do tego czasu pamięć, Pytanie, Brief i narzędzia pamięci sesji są "
          "niedostępne. Odmawia, gdy działa sesja lub zadanie. Ten ekran pokaże nowe miejsce, gdy otworzysz go "
          "ponownie.",
    BUTTON_LOCAL: "Zmień na local",
    BUTTON_REMOTE_DB: "Zmień na remote-db",
    BUTTON_REMOTE: "Zmień na remote",
    HOW: "Aby zmienić to w terminalu: wyjdź ze środowiska przez Ctrl+B, potem d (działa ono dalej), i w tym "
         "terminalu zatrzymaj usługę, a potem uruchom polecenie wybranego profilu. Bez --yes polecenie tylko "
         "sprawdza cel i wypisuje, co zmieni i co zostawi bez zmian; nic się nie zmienia. Uruchom je ponownie z "
         "--yes, aby wprowadzić zmianę, a potem zrób to, co każe dalej. Sesja programistyczna nie może go "
         "uruchomić.",
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
    REMOTE_DB: "Instalacja przełącza się na ArcadeDB pod adresem DB_URL, z którą odtąd tylko się łączy. "
               "Polecenie sprawdza, że odpowiada ona w obsługiwanej wersji i że USER loguje się do DATABASE hasłem "
               "z PASSWORD_FILE, prywatnego pliku (tryb 0600), który utworzyłeś; zapisywana jest tylko ścieżka do "
               "pliku. Odczytuje, która instalacja zapisuje tę bazę, i nigdy jej nie przejmuje. Poprzednio używana "
               "baza zostaje bez zmian, z danymi; polecenie ją wymienia. Jej dane nie są kopiowane do nowej bazy: "
               "uruchom `cairnmind backup FILE` przed poleceniem, a `cairnmind init` i `cairnmind restore FILE` po "
               "nim.",
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
        # the status row is computed without the writer role, so its Storage mark never carries that problem
        marks = topology.status_words({**place, "storage": {k: v for k, v in place["storage"].items() if k != "writer"}})
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
    paragraphs += [tr(LOADED, language), tr(HERE, language), tr(HOW, language) + "\n\n  cairnmind stop",
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


PROFILE_BUTTONS = (("local", BUTTON_LOCAL), ("remote-db", BUTTON_REMOTE_DB), ("remote", BUTTON_REMOTE))


@functools.cache
def _screen_class():
    """Built on first use: terminal_ui reads the catalog, which reads this module's MESSAGES. The frame of
    MessageModal (a scrolling body, then buttons), with a button per profile; it dismisses with the profile or None."""
    from textual.app import ComposeResult
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import ModalScreen
    from textual.widgets import Button, Static

    from .terminal_ui import LocalizedModal

    class SetupScreen(LocalizedModal, ModalScreen[str | None]):
        BINDINGS = [("escape", "close", "Close")]
        heading = TITLE

        def __init__(self, text: str):
            super().__init__()
            self.value = text

        def compose(self) -> ComposeResult:
            with Vertical(classes="cm-dialog -wide"):
                yield Static(plain(self.t(TITLE)), classes="heading")
                with VerticalScroll(classes="cm-dialog-body -message"):
                    yield Static(plain(self.value), id="setup-body")
                with Horizontal(classes="actions"):
                    for profile, label in PROFILE_BUTTONS:
                        yield Button(self.t(label), id="change-" + profile)
                    yield Button(self.t("Close"), id="setup-close")
                yield Static(self.t("PgUp/PgDn: scroll · Esc: close"), classes="cm-hint", markup=False)

        def on_button_pressed(self, event: Button.Pressed) -> None:
            event.stop()
            self.dismiss(event.button.id.removeprefix("change-") if event.button.id != "setup-close" else None)

        def action_close(self) -> None:
            self.dismiss(None)
    return SetupScreen


async def open_view(app) -> None:
    """Read the running service's topology and show it (one GET); a button hands its profile to change_setup, which
    asks for the person's agreement before anything starts. After a change the service runs on the new settings and
    the screen is not shown again by itself: opening it reads the new place."""
    from .terminal_client import BrokerError
    from .terminal_i18n import tr
    from .terminal_setup_change import change_setup
    language = getattr(app, "language", "en")
    try:
        status = await app.request("GET", "/api/status")
    except BrokerError as exc:  # /status reads the store too: when it fails, the service's reason is named, not replaced
        raise BrokerError(exc.status_code, f"{exc.detail} {tr(NO_ANSWER, language)}") from exc
    profile = await app.push_screen_wait(_screen_class()(describe(status, language)))
    if profile is not None:
        await change_setup(app, profile)
