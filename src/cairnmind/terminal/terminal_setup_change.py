"""More, then a change of where storage and models run: the dialogs of the workspace's own change of setup (#339).

The flow: the profile, a form of non-secret values (paths of private files and a credential reference, never a password
or a key), the plan `cairnmind setup` itself produces from a dry run, the consent dialog that says where memory content
will go and what is unavailable for how long, then a progress dialog while the helper of setup_change.py stops the
knowledge service, runs the same setup code and starts the service again. Everything before the helper is started only
reads. The screen that offers the choice of a profile calls `change_setup(app, profile)` from a Textual worker.
The English sentences are the source; MESSAGES is their Polish for the terminal catalog.
"""
from __future__ import annotations

import asyncio
import functools
from urllib.parse import urlsplit

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from cairnmind.agents import knowledge_mode
from cairnmind.config import Settings
from cairnmind.memory import memory_egress
from cairnmind.pal.terminal_safe import plain, safe_text
from cairnmind.storage import database_mode
from cairnmind.terminal import setup_change, setup_mark, setup_profiles
from cairnmind.terminal.setup_profiles import Request, render_one, run

CHOOSE = "Change where storage and models run"
LOCAL = "Everything on this computer (local)"
REMOTE_DB = "Database on another computer, models here (remote-db)"
REMOTE = "Database and models on other computers (remote)"
DATABASE_FORM = "Where storage and models will run"
F_URL = "Database address (https://host:2480)"
F_USER = "Database user"
F_DATABASE = "Database name"
F_PASSWORD_FILE = "Password file (a private file you created, mode 0600)"  # noqa: S105 - UI field label, not a secret
F_CA_FILE = "Certificate authority file (optional)"
F_ADMIN_FILE = "Root password file (optional, never saved)"
F_MODELS_URL = "Model server address (https://...)"
F_PROVIDER = "Provider (ollama or openai_compatible)"
F_CHAT = "Chat model"
F_EMBEDDING = "Embedding model"
F_REFERENCE = "Key reference (env:NAME or profile:ID, optional)"
FORM_NOTE = ("Nothing is saved yet. No field takes a password or a key: name the private file you created, and the "
             "variable or profile that holds the key. The next step signs in to the database and sends one test "
             "request to each model server, with no memory content, then shows the plan.")
TEST = "Test and show the plan"
BACK = "What happens to the data of the store at {url}?"
BACK_BACKUP = "Back it up into a new file and restore it here"
BACK_EMPTY = "Use the store on this machine without it"
BACKUP_FORM = "Backup file"
F_BACKUP = "New backup file (it must not exist)"
REFUSED_TITLE = "The setup was not changed"
CONSENT_TITLE = "Change where storage and models run?"
APPROVE = "Change setup"
WHERE = "Where memory content goes:"
STORE_AT = "Everything in memory is stored in ArcadeDB at {url}."
STORE_HERE = "Everything in memory is stored on this computer, in {path}."
MODELS_HERE = "The memory models run on this computer; no memory text is sent to a model server."
MODELS_OFF = "The knowledge agent stays off (deterministic-only mode); no memory text goes to a model."
DOWNTIME = ("The knowledge service of this workspace is stopped for the change and started again at its end. Until "
            "then memory, Ask, Brief, the memory view and the memory tools of sessions are unavailable: usually under "
            "a minute; a backup and restore take longer, with the size of the store.")
KEEP_OPEN = ("Keep the workspace open until the change has finished: the restore keeps a person's confirmations only "
             "while the workspace answers with its key.")
GOES_ON = ("Once started, the change cannot be stopped halfway; it goes on if the workspace is closed. `cairnmind "
           "status` shows where storage and models run afterwards, and `cairnmind doctor` names a change that did "
           "not finish. How it ended, including a restore that kept confirmations only as claims, is shown once the "
           "next time the workspace opens, if you did not see it here, and stays in setup-change.json in the data "
           "directory.")
OUTCOME_TITLE = "How the last change of where storage and models run ended"
PLAN = "The plan, as `cairnmind setup` gives it:"
PROGRESS_TITLE = "Changing where storage and models run"
PROGRESS_HINT = "Wait: the change cannot be stopped halfway."
ENDED_SILENTLY = ("The process that carried out the change ended without saying how (exit status {code}). `cairnmind "
                  "doctor` says whether the change was left unfinished; `cairnmind setup` shows where storage and "
                  "models run now.")

MESSAGES = {
    CHOOSE: "Zmień miejsce działania bazy i modeli",
    LOCAL: "Wszystko na tym komputerze (local)",
    REMOTE_DB: "Baza na innym komputerze, modele tutaj (remote-db)",
    REMOTE: "Baza i modele na innych komputerach (remote)",
    DATABASE_FORM: "Gdzie będą działać baza i modele",
    F_URL: "Adres bazy (https://host:2480)",
    F_USER: "Użytkownik bazy",
    F_DATABASE: "Nazwa bazy",
    F_PASSWORD_FILE: "Plik z hasłem (prywatny plik, który utworzono, tryb 0600)",
    F_CA_FILE: "Plik urzędu certyfikacji (opcjonalnie)",
    F_ADMIN_FILE: "Plik z hasłem roota (opcjonalnie, nigdy nie zapisywany)",
    F_MODELS_URL: "Adres serwera modeli (https://...)",
    F_PROVIDER: "Dostawca (ollama albo openai_compatible)",
    F_CHAT: "Model chat",
    F_EMBEDDING: "Model embedding",
    F_REFERENCE: "Odwołanie do klucza (env:NAZWA albo profile:ID, opcjonalnie)",
    FORM_NOTE: ("Nic nie jest jeszcze zapisane. Żadne pole nie przyjmuje hasła ani klucza: wskaż prywatny plik, który "
                "utworzono, oraz zmienną lub profil z kluczem. Następny krok loguje się do bazy i wysyła jedno "
                "zapytanie testowe do każdego serwera modeli, bez treści pamięci, a potem pokazuje plan."),
    TEST: "Sprawdź i pokaż plan",
    BACK: "Co się stanie z danymi bazy pod adresem {url}?",
    BACK_BACKUP: "Zapisz kopię zapasową do nowego pliku i odtwórz ją tutaj",
    BACK_EMPTY: "Użyj bazy na tym komputerze bez nich",
    BACKUP_FORM: "Plik kopii zapasowej",
    F_BACKUP: "Nowy plik kopii zapasowej (nie może istnieć)",
    REFUSED_TITLE: "Ustawienia nie zostały zmienione",
    CONSENT_TITLE: "Zmienić miejsce działania bazy i modeli?",
    APPROVE: "Zmień ustawienia",
    WHERE: "Dokąd trafia treść pamięci:",
    STORE_AT: "Cała pamięć jest przechowywana w ArcadeDB pod adresem {url}.",
    STORE_HERE: "Cała pamięć jest przechowywana na tym komputerze, w {path}.",
    MODELS_HERE: "Modele pamięci działają na tym komputerze; żaden tekst pamięci nie jest wysyłany na serwer modeli.",
    MODELS_OFF: ("Agent wiedzy pozostaje wyłączony (tryb tylko deterministyczny); żaden tekst pamięci nie trafia do "
                 "modelu."),
    DOWNTIME: ("Usługa wiedzy tego środowiska jest zatrzymana na czas zmiany i uruchamiana ponownie na jej końcu. Do "
               "tego czasu pamięć, Pytanie, Brief, widok pamięci i narzędzia pamięci sesji są niedostępne: zwykle "
               "krócej niż minutę; kopia zapasowa i odtworzenie trwają dłużej, zależnie od rozmiaru bazy."),
    KEEP_OPEN: ("Zostaw środowisko otwarte, aż zmiana się skończy: odtworzenie zachowuje potwierdzenia osoby tylko "
                "wtedy, gdy środowisko odpowiada swoim kluczem."),
    GOES_ON: ("Raz zaczętej zmiany nie można zatrzymać w połowie; trwa dalej także po zamknięciu środowiska. "
              "`cairnmind status` pokazuje potem, gdzie działają baza i modele, a `cairnmind doctor` wskazuje zmianę, "
              "która się nie skończyła. Jak się skończyła, także odtworzenie, które zachowało potwierdzenia tylko jako "
              "twierdzenia, zostanie pokazane raz przy następnym otwarciu środowiska, jeśli nie zobaczysz tego tutaj, "
              "i zostaje w setup-change.json w katalogu danych."),
    OUTCOME_TITLE: "Jak skończyła się ostatnia zmiana miejsca działania bazy i modeli",
    PLAN: "Plan, jaki podaje `cairnmind setup`:",
    PROGRESS_TITLE: "Zmiana miejsca działania bazy i modeli",
    PROGRESS_HINT: "Czekaj: zmiany nie można zatrzymać w połowie.",
    ENDED_SILENTLY: ("Proces, który wykonywał zmianę, skończył bez powiedzenia, jak (kod wyjścia {code}). `cairnmind "
                     "doctor` mówi, czy zmiana została niedokończona; `cairnmind setup` pokazuje, gdzie baza i modele "
                     "działają teraz."),
}

PATHS = ("password_file", "ca_file", "admin_password_file", "backup")


def _fields(profile: str, t) -> list:
    from cairnmind.terminal.terminal_ui import Field
    fields = [Field("url", t(F_URL), required=True), Field("user", t(F_USER), required=True),
              Field("database", t(F_DATABASE), required=True), Field("password_file", t(F_PASSWORD_FILE), required=True),
              Field("ca_file", t(F_CA_FILE)), Field("admin_password_file", t(F_ADMIN_FILE))]
    if profile == "remote":
        fields += [Field("models_url", t(F_MODELS_URL), required=True), Field("provider", t(F_PROVIDER), required=True),
                   Field("chat_model", t(F_CHAT), required=True), Field("embedding_model", t(F_EMBEDDING), required=True),
                   Field("credential_ref", t(F_REFERENCE))]
    return fields


def _request(profile: str, values: dict, *, empty: bool = False) -> Request:
    """The request of the form: a typed Windows path becomes its WSL path; an empty optional field is not given."""
    from cairnmind.terminal.terminal_published_import import full_path
    given = {name: (full_path(value) if name in PATHS else value) or None for name, value in values.items()}
    return Request(profile, **given, allow_remote=profile == "remote", empty=empty)


def consent_text(result: dict, request: Request, settings, language: str) -> str:
    """What the person agrees to: where memory content goes, what is unavailable, then the plan as setup gives it."""
    from cairnmind.terminal.terminal_i18n import tr

    def t(text, **values):
        return tr(text, language, **{name: safe_text(value) for name, value in values.items()})

    sends = [item for item in result["changes"] if item[0] in (setup_profiles.SENDS, setup_profiles.SENDS_NO_TEXT)]
    if sends:
        models = render_one(sends[0], language)
    elif request.profile == "remote":  # the roles already are where the request puts them
        host = urlsplit(request.models_url).hostname
        classes = memory_egress.describe(settings, "chat")["sends"]
        models = render_one((setup_profiles.SENDS, {"host": host, "classes": ", ".join(classes)}) if classes
                            else (setup_profiles.SENDS_NO_TEXT, {"host": host}), language)
    else:
        models = t(MODELS_HERE if knowledge_mode.enabled(settings) else MODELS_OFF)
    store = (t(STORE_HERE, path=settings.data_dir / "arcade") if request.profile == "local"
             else t(STORE_AT, url=database_mode.shown(request.url)))
    plan = [render_one(item, language) for key in ("tests", "changes", "writer", "rebuild", "left_behind")
            for item in result[key] if item not in sends]
    return "\n".join([t(WHERE), store, models, "", t(DOWNTIME), *([t(KEEP_OPEN)] if request.backup else []),
                      t(GOES_ON), "", t(PLAN), *plan])


@functools.cache
def _progress_class():
    """Built on first use: terminal_ui reads the catalog, which reads this module's MESSAGES."""
    from cairnmind.terminal.terminal_ui import LocalizedModal

    class ProgressModal(LocalizedModal, ModalScreen[str]):
        """The change as the helper reports it. Esc does nothing before the end: nothing here can stop the helper."""
        BINDINGS = [("escape", "close", "Close")]

        def __init__(self, process, data_dir, language):
            super().__init__()
            self.process, self.data_dir, self.language = process, data_dir, language
            self.final, self.lines = None, []

        def compose(self) -> ComposeResult:
            with Vertical(classes="cm-dialog -wide"):
                yield Static(plain(self.t(PROGRESS_TITLE)), classes="heading")
                with VerticalScroll(classes="cm-dialog-body -message"):
                    yield Static("", id="progress-body")
                with Horizontal(classes="actions"):
                    yield Button(self.t("Close"), id="progress-close", disabled=True)
                yield Static(self.t(PROGRESS_HINT), classes="cm-hint", id="progress-hint", markup=False)

        def on_mount(self):
            self.set_interval(0.5, self.poll)

        def poll(self):
            if self.final is not None:
                return
            found = setup_change.read_progress(self.data_dir, self.process.pid)
            code = self.process.poll()
            if code is not None and (found is None or found["state"] == "running"):
                found = setup_change.read_progress(self.data_dir, self.process.pid)  # its last report, written as it ended
                if found is None or found["state"] == "running":
                    lines = [*(found["lines"] if found else []), self.t(ENDED_SILENTLY, code=code)]
                    return self.show(lines, "ended")
            if found:
                self.show(found["lines"], None if found["state"] == "running" else found["state"])

        def show(self, lines, final):
            self.lines = list(lines)
            self.query_one("#progress-body", Static).update(plain("\n".join(self.lines)))
            self.query_one(VerticalScroll).scroll_end(animate=False)
            if final is not None:
                self.final = final
                if final in setup_change.EXIT:  # a person is looking at the end: the next opening need not show it
                    setup_change.mark_shown(self.data_dir, self.process.pid)
                self.query_one("#progress-close", Button).disabled = False
                self.query_one("#progress-hint", Static).update(self.t("Esc: close"))
                self.set_focus(self.query_one("#progress-close", Button))

        def on_button_pressed(self, event: Button.Pressed) -> None:
            event.stop()
            self.action_close()

        def action_close(self) -> None:
            if self.final is not None:
                self.dismiss(self.final)
    return ProgressModal


def ProgressModal(process, data_dir, language):
    return _progress_class()(process, data_dir, language)


def show_last_outcome(app) -> None:
    """At an opening of the workspace (the sidebar's mount): the end of a change that nobody watched, once (#339)."""
    app.run_worker(_show_last_outcome(app), group="setup-outcome", exit_on_error=False)


async def _show_last_outcome(app) -> None:
    from cairnmind.terminal.terminal_ui import MessageModal
    data_dir, language = app.settings.data_dir, app.language
    found = await asyncio.to_thread(setup_change.unseen_outcome, data_dir, language)
    if found is None:
        return
    kept = await asyncio.to_thread(setup_mark.unchecked, data_dir)  # a copy of config.json that was kept, as doctor names it
    lines = [*found["lines"], *(["", render_one((kept.template, kept.values), language)] if kept else [])]
    setup_change.mark_shown(data_dir, found["pid"])  # shown means put on the screen: at most once, never in the store
    await app.push_screen_wait(MessageModal(app.t(OUTCOME_TITLE), "\n".join(lines)))


async def change_setup(app, profile: str | None = None) -> None:
    """Change where storage and models run, from a worker of ``app`` (a panel). ``profile`` is local, remote-db or
    remote, or None to ask. Raises OSError, ValueError or RuntimeError for the caller to show with app.notice."""
    from cairnmind.terminal.terminal_person_act import agreed
    from cairnmind.terminal.terminal_ui import ChoiceModal, Field, FormModal, MessageModal
    t, language, data_dir = app.t, app.language, app.settings.data_dir
    settings = Settings.load(data_dir)

    async def say(title, item):
        await app.push_screen_wait(MessageModal(t(title), render_one(item, language) if isinstance(item, tuple) else item))

    item = await asyncio.to_thread(setup_change.refusal, settings)
    if item:
        return await say(REFUSED_TITLE, item)
    if profile is None:
        profile = await app.push_screen_wait(ChoiceModal(t(CHOOSE), [(t(LOCAL), "local"), (t(REMOTE_DB), "remote-db"),
                                                                     (t(REMOTE), "remote")]))
        if profile is None:
            return
    if profile not in setup_profiles.PROFILES:
        raise ValueError(render_one((setup_profiles.UNKNOWN_PROFILE, {"profile": profile}), language))
    found: dict = {}

    async def check(values, empty=False):
        """The request, validated and dry-run (nothing is written); a refusal is a ValueError in the panel's language."""
        request = _request(profile, values, empty=empty)
        problem = setup_profiles.option_problem(request)
        try:
            if problem:
                raise problem
            result = await asyncio.to_thread(run, Settings.load(data_dir), request, yes=False, say=lambda text: None)
        except setup_profiles.Said as exc:
            raise ValueError(render_one((exc.template, exc.values), language)) from None
        found["request"], found["result"] = request, result

    if profile == "local":
        values, empty = {}, False
        if database_mode.connect_only(settings):
            choice = await app.push_screen_wait(ChoiceModal(
                t(BACK, url=safe_text(database_mode.address(settings))),
                [(t(BACK_BACKUP), "backup"), (t(BACK_EMPTY), "empty")]))
            if choice is None:
                return
            empty = choice == "empty"
            if not empty:
                values = await app.push_screen_wait(FormModal(
                    t(BACKUP_FORM), [Field("backup", t(F_BACKUP), required=True)], note=t(FORM_NOTE), submit=t(TEST),
                    on_submit=check))
                if values is None:
                    return
        if not found:
            try:
                await check(values, empty=empty)
            except (OSError, ValueError) as exc:
                return await say(REFUSED_TITLE, str(exc))
    else:
        values = await app.push_screen_wait(FormModal(t(DATABASE_FORM), _fields(profile, t), note=t(FORM_NOTE),
                                                      submit=t(TEST), on_submit=check))
        if values is None:
            return
    request, result = found["request"], found["result"]
    item = setup_change.target_refusal(result, request)
    if item:
        return await say(REFUSED_TITLE, item)
    if not result["changes"]:  # setup's own sentence: it already runs as that profile says
        return await say(CHOOSE, result["done"][0])
    if not await agreed(app, t(CONSENT_TITLE), consent_text(result, request, settings, language), t(APPROVE)):
        return
    process = await asyncio.to_thread(setup_change.begin, settings, request, setup_change.agreed_plan(result), language)
    modal = ProgressModal(process, data_dir, language)
    await app.push_screen_wait(modal)
    try:
        await asyncio.to_thread(app.broker.reconnect)
    except (OSError, ValueError, RuntimeError) as exc:
        app.notice(exc)
    refresh = getattr(app, "refresh_sidebar", None)  # a panel with a sidebar reads it again (a worker: not awaited)
    if refresh is not None:
        refresh()
    if modal.lines:
        app.notice(modal.lines[-1])
