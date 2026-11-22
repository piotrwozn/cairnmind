"""What Cairnmind says when the workspace cannot open, and when the service could not be started (#273, #258).

tmux 3.4+ is needed only by the terminal workspace: `cairnmind`, `cairnmind tui` and `cairnmind workspace
takeover`. Every other command works without it, and doctor reports the workspace as unavailable instead of
Cairnmind as broken. The English sentences are the source; MESSAGES is their Polish for the terminal catalog.
#274 adds what Cairnmind says without models: the knowledge agent's stored choice (knowledge_mode.py), init's
choice, the answer without models with its full-text matches and doctor's information line.
#287 adds the first start's summary, next steps and stops.
#313 adds the second round: the sentences of the commands that print JSON only outside a terminal, the foreign
engine, the optional parts that do not work.
#326 adds the third round: a project name that comes first, the brief's legend, the only place a person confirms.
"""
from __future__ import annotations

import shutil

from . import platform_hints
from .terminal_safe import safe_text

WORKSPACE_NEEDS = "The terminal workspace needs tmux 3.4 or newer."
TMUX_NOT_FOUND = "tmux was not found."
TMUX_FOUND = "Found {version}."
TMUX_SILENT = "tmux did not report its version."
WITHOUT_WORKSPACE = ("Without it, every other command works, for example cairnmind brief, cairnmind remember, "
                     "cairnmind ask and cairnmind run.")
# cairnmind run starts a coding assistant: it is named only when one is installed (#326).
WITHOUT_WORKSPACE_NO_CLIENT = ("Without it, every other command works, for example cairnmind brief, cairnmind "
                               "remember and cairnmind ask.")
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
                  "brief, impact, memory and file indexing work without models; cairnmind ask is a full-text "
                  "search of the project's memory and makes no answer. Saved as knowledge_agent in {config}.")
TURNED_ON = ("Knowledge agent: enabled, saved as knowledge_agent in {config}. Restart the knowledge service "
             "(cairnmind stop, then start Cairnmind again) so that it uses the models. To turn it off again, set "
             "knowledge_agent to disabled in that file and restart the service.")
ASK_OFF = ("Without models (deterministic-only mode) ask is a full-text search of the project's memory and makes no answer. "
           "The passages listed match words of the question: they are search results, not verified evidence.")
ASK_NO_MATCH = ("Without models (deterministic-only mode) ask is a full-text search of the project's memory and makes no answer. "
                "No passage matches words of the question.")
ASK_LEFT_OUT = ("Without models (deterministic-only mode) ask is a full-text search of the project's memory and makes no answer. "
                "Passages match words of the question, but none could be shown; the reasons are listed with the result.")
ASK_TIMED_OUT = ("Without models (deterministic-only mode) ask is a full-text search of the project's memory and makes no answer. "
                 "The search did not finish in time, so no passages are listed.")
MATCHES_HEADING = "Full-text matches (search results, not an answer)"
MATCHES_HELD_BACK = ("Some matching passages were left out because of their sensitivity class, as answers leave them "
                     "out for the remote model server (memory_remote_classes).")
NEEDS_MODELS = ("{command} needs the knowledge agent's models, and the knowledge agent is disabled "
                "(deterministic-only mode).")
KNOWLEDGE_INVALID = "knowledge_agent in config.json is {value}; it must be enabled or disabled."
RESTART_FOR_MODELS = ("knowledge_agent is enabled in config.json, but this service started without models. Restart "
                      "the knowledge service (cairnmind stop, then start Cairnmind again) so that it uses them.")
ASK_NEXT = ("Next: to use this memory from a coding assistant, install Codex or Claude Code, then run cairnmind "
            "install NAME.")
STORED_FINDING = "stored finding"  # how a note saved with remember is named in a list of matches
# Short on purpose: the sidebar shows this on a 24-column line of at most 5 rows, so the command must fit (#294).
PROCESS_OFF = "No models configured. Run `cairnmind models pull` to process memory."
PROCESS_RESTART = "Models are enabled. Restart the service (`cairnmind stop`, then start) to use them."

CLIENT_NAMES = {"codex": "Codex", "claude": "Claude Code"}  # as the first start's sentences call the clients

# The doctor summary (#287): what is required and present, required and missing, optional and off.
DATA_DIR = "Memory is kept in {data_dir}."
REQUIRED_PRESENT = "Required and present:"
REQUIRED_MISSING = "Required and missing:"
NOTHING_MISSING = "Required and missing: nothing."
OPTIONAL_BROKEN = "Optional and not working:"
OPTIONAL_OFF = "Optional and off:"
OPTIONAL_ON = "Optional and on:"
NOTES = "Notes:"
FULL_REPORT = "The full report: cairnmind doctor --json"
REPORT_UNREADABLE = "The doctor report could not be read."
NEXT_FIX = "Next: fix what is listed under Required and missing, then run cairnmind doctor again."
NEXT_START = ('Next: cairnmind project add NAME FOLDER registers a project; then cairnmind remember NAME "..." '
              'stores a first finding and cairnmind brief NAME shows it.')
NEXT_PROJECTS = ('Next: cairnmind project list names your projects; cairnmind brief NAME shows where work stands in one, '
                 'and cairnmind remember NAME "..." stores a finding.')
ENGINE_READY = "Database engine: answers at {url}; the memory database can be read."  # its internal name is in --json
SERVICE_RUNNING = "Local service: running at {url}."
SERVICE_ON_DEMAND = "Local service: not running now; the next command that needs it starts it."
ENGINE_ON_DEMAND = ("Database engine: not running now at {url}; the next command that needs it starts it "
                    "(cairnmind engine start starts it now).")
ENGINE_DOWN = ("The database engine does not answer at {url} ({error}); it may be starting or busy. If it is "
               "stopped: cairnmind engine start.")
ENGINE_RELEASE = ("The database engine at {url} runs release {found}, not the pinned {pinned}, and every command "
                  "that opens the store refuses it. Stop it (cairnmind stop, then cairnmind engine stop) and run "
                  "cairnmind engine start; if another program answers at that address, free it or set another "
                  "arcade_url.")
SERVER_DOWN_CONNECT = ("The database server at {url} does not answer ({error}). Check that it runs and that this "
                       "machine reaches it.")
JAVA_NEEDED = ("Java 21 or newer is needed for the engine: {found}. {hint}; or run the engine in Docker: "
               "cairnmind engine start --docker.")
MODELS_OFF = ("Local models: off (deterministic-only mode), so ask lists matching passages and makes no answer. "
              "To turn them on: cairnmind models pull, with Ollama running (about 7 GB).")
MODELS_ON = "Memory models: on, and their servers answer."
WORKSPACE_OFF = "Terminal workspace: {status}. {hint}. Every other command works without it."
WORKSPACE_ON = "Terminal workspace: ready; open it with cairnmind."
CLIENT_OFF = "{client}: not installed; needed only for coding sessions (cairnmind run). {note}"
CLIENT_ON = "{client}: installed and verified; start it with cairnmind run NAME {command}."

# What each command of the first start says it did and what comes next.
PROJECT_EXISTED = ('{project} was already registered for {root}; its files were checked again (files indexed: {files}).')
PROJECT_EXISTED_NEXT = 'Next: cairnmind brief {project} shows where work stands.'
PROJECT_FOLDER_MISSING = ("{root} does not exist. cairnmind project add NAME FOLDER takes the folder of a project you "
                          "already have: check the path, or create the folder first (mkdir -p {quoted}).")
PROJECT_ADDED = 'Registered {project} in {root}; files indexed: {files}.'
PROJECT_ADDED_NEXT = ('Next: cairnmind remember {project} "..." stores a first finding, and cairnmind brief {project} '
                      'shows it.')
NOT_GIT = ("{root} is not a Git repository. Memory works without Git; impact, the commits in brief and coding "
           "sessions (cairnmind run) need a repository with a first commit (git init, git add, git commit).")
NO_COMMIT = ("{root} is a Git repository without a commit yet. Memory works; impact, the commits in brief and "
             "coding sessions (cairnmind run) need a first commit (git add, git commit).")
REMEMBER_PROPOSED = ("Stored as a proposed {kind}. Nothing more is needed to go on: cairnmind brief {project} and "
                     "cairnmind ask {project} already use it, marked as unconfirmed. A person can confirm or dispute "
                     "it later, only in the memory inspector of the terminal workspace (cairnmind, then Ctrl+B i; "
                     "needs tmux 3.4+): the command line confirms nothing.")
REMEMBER_STORED = "Stored as {status} ({kind}), record {id}; cairnmind records {project} lists it."
BRIEF_START = ('Next: nothing is stored for this project yet. cairnmind remember {project} "..." stores a first '
               'finding; a coding session started with cairnmind run {project} claude (or codex) saves '
               'checkpoints, plans and tasks here.')
BRIEF_REVIEW = ("Next: nothing more is needed to go on; brief and ask already use proposed findings, marked as "
                "unconfirmed. A person can confirm or dispute them later, only in the memory inspector of the "
                "terminal workspace (cairnmind, then Ctrl+B i; needs tmux 3.4+): the command line confirms nothing.")
INSTALL_NO_CLIENT = ("Neither Codex nor Claude Code is installed here, so nothing was written and no client was "
                     "connected. Memory works without a client (cairnmind remember, brief, ask); after installing "
                     "one, run cairnmind install {project} again.")
INSTALL_DONE = ("Connected: {clients}. Written in the project: {files}; review the files before committing. "
                "Next: cairnmind run {project} {command}.")
INSTALL_ALSO = ("Not connected: {clients}. cairnmind doctor --contract shows which check fails and what it "
                "needs.")
INSTALL_NOT_CONNECTED = "Written in the project: {files}. " + INSTALL_ALSO
NOTHING_WRITTEN = "nothing new (the files were up to date)"
CLIENT_SKIPPED = ("{client} is not installed, so nothing was written for it; after installing it, run cairnmind "
                  "install again to connect it.")
REMOVED_COMMAND = ("{path} ({entry}) starts the devbrain command, which Cairnmind no longer installs; it is left "
                   "unchanged and fails to start. Write cairnmind there with the same arguments.")
REMOVED_COMMAND_BESIDE = ("{path} ({entry}) starts the devbrain command, which Cairnmind no longer installs; it is "
                          "left unchanged and fails to start. cairnmind install has just written its own entry there, "
                          "so remove this one; write cairnmind in it with the same arguments only if it does "
                          "something else.")
TRUST_NOTE = ("Each client asks you to trust this project's MCP server and hooks when it first starts here; "
              "Cairnmind answers no trust question for you.")
SERVICE_STARTED = ("Started the Cairnmind service at {url} for {data_dir}. It keeps running in the background and "
                   "starts the database engine when that is not running; cairnmind stop stops it.")
ENGINE_STARTING = "Starting the database engine ({mode}) for {data_dir}."
ENGINE_STARTED = ("The database engine ({mode}) answers and keeps running in the background (cairnmind engine stop "
                  "stops it, after cairnmind stop).")

STOP_STOPPING = "The Cairnmind service at {url} is stopping; the next command that needs it starts it again."
STOP_NOT_RUNNING = "The Cairnmind service at {url} was not running, so nothing was stopped."
STOP_REFUSED = "The Cairnmind service at {url} answered but did not stop; cairnmind status shows its state."
ENGINE_LEFT_RUNNING = "The database engine is not stopped by this; cairnmind engine stop stops it."
ENGINE_STOPPED = "The database engine of {data_dir} was stopped; the next command that needs it starts it again."
ENGINE_STOPPED_CONNECT = ("The engine this data directory started earlier was stopped; this installation only connects "
                          "to its database server, so nothing starts that engine again.")
ENGINE_NOT_STOPPED = "Nothing was stopped: {reason}."
PROJECTS_NONE = "No project is registered yet. Next: cairnmind project add NAME FOLDER registers one."
PROJECTS_HEADING = "Registered projects:"
PROJECTS_NEXT = ('Next: cairnmind brief NAME shows where work stands in one; cairnmind remember NAME "..." stores a '
                 'finding.')
INIT_READY = ("Memory is ready in {data_dir}. Next: cairnmind doctor checks every part, then "
              "cairnmind project add NAME FOLDER registers a project.")
INIT_MARKED = ("{marked} confirmed records were marked as confirmed by a person, because a review or a workspace form "
               "written on this computer covers each as it stands; {left} keep their status until you confirm them "
               "in the memory inspector.")
MODE_STATUS = "No answer without models; full-text matches only"
FOREIGN_ENGINE = ("The database engine at {url} does not accept the sign-in of {data_dir}, so it most likely belongs "
                  "to another data directory. Give this data directory a free engine port: set arcade_url in "
                  "{data_dir}/config.json, for example to http://127.0.0.1:2490. Or stop the engine that answers "
                  "there: cairnmind stop, then cairnmind engine stop, both run with the --data-dir that started it.")
OWN_ENGINE_REFUSED = ("The database engine of this data directory at {url} refused the sign-in. After a few failed "
                      "sign-ins ArcadeDB refuses every one for about 30 seconds: wait, then run the command again. "
                      "If it keeps refusing, the sign-in in {data_dir}/config.json does not match this engine.")

# Stops that name the next step.
UNKNOWN_PROJECT_NEXT = ("cairnmind project list shows the registered projects; cairnmind project add NAME FOLDER "
                        "registers one.")
PROJECT_FIRST = ("The project name comes first, then {missing}; cairnmind project list shows the registered "
                 "projects.")
PROJECT_NAME_TAKEN = ("The name {name} is already registered, for {root}. Choose another name; cairnmind project "
                      "list shows the registered projects.")
PROJECT_ROOT_TAKEN = "{root} is already registered, as the project {name}. cairnmind project list shows the registered projects."
RUN_NO_CLIENT = ("Install it and sign in ({url}), then run this command again. Without a coding client these "
                 "work: cairnmind remember, cairnmind brief and cairnmind ask.")
LAUNCH_REFUSED = ("Verified {client} launch refused; not in place: {missing}. cairnmind doctor --contract shows "
                  "which check fails and what it needs.")
PART_CONTRACT = "the client contract checks"
PART_MCP = "the MCP entry"
PART_HOOKS = "the session hooks"
PART_INSTRUCTIONS = "the instruction block"
STATUS_NOT_RUNNING = ("The Cairnmind service is not running, and diagnostics do not start it. The next command "
                      "that needs it starts it (for example cairnmind ask); cairnmind doctor checks the "
                      "installation without it.")
ENGINE_NOT_INSTALLED = ("The database engine (ArcadeDB) is not installed for this data directory. Run cairnmind "
                        "engine install, then cairnmind init; the installer runs both.")
ENGINE_EXITED = ("ArcadeDB exited during startup. See {log}. A port already in use ({url}) or too little memory "
                 "are the usual causes; after fixing it, run cairnmind engine start.")
ENGINE_TIMED_OUT = "ArcadeDB startup timed out. See {log}. After fixing the cause, run cairnmind engine start."

MESSAGES = {
    WORKSPACE_NEEDS: "Środowisko terminalowe wymaga tmux 3.4 lub nowszego.",
    TMUX_NOT_FOUND: "Nie znaleziono tmux.",
    TMUX_FOUND: "Znaleziono {version}.",
    TMUX_SILENT: "tmux nie podał swojej wersji.",
    WITHOUT_WORKSPACE: ("Bez niego działają wszystkie inne polecenia, na przykład cairnmind brief, cairnmind "
                        "remember, cairnmind ask i cairnmind run."),
    WITHOUT_WORKSPACE_NO_CLIENT: ("Bez niego działają wszystkie inne polecenia, na przykład cairnmind brief, "
                                  "cairnmind remember i cairnmind ask."),
    ASK_NEXT: ("Dalej: aby używać tej pamięci z asystentem programistycznym, zainstaluj Codexa lub Claude Code, a "
               "potem uruchom cairnmind install NAZWA."),
    STORED_FINDING: "zapisane ustalenie",
    PROJECT_FIRST: ("Najpierw podaje się nazwę projektu, potem {missing}; cairnmind project list pokazuje "
                    "zarejestrowane projekty."),
    PROJECT_ADDED_NEXT: ('Dalej: cairnmind remember {project} "..." zapisuje pierwsze ustalenie, a cairnmind brief '
                         '{project} je pokazuje.'),
    PROJECT_EXISTED_NEXT: "Dalej: cairnmind brief {project} pokazuje, na czym stoi praca.",
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
                     "jest wyszukiwaniem pełnotekstowym w pamięci projektu i nie tworzy odpowiedzi. "
                     "Zapisano jako knowledge_agent w {config}."),
    TURNED_ON: ("Agent wiedzy: włączony, zapisano jako knowledge_agent w {config}. Uruchom ponownie usługę wiedzy "
                "(cairnmind stop, a potem ponownie uruchom Cairnmind), aby używała modeli. Aby go znowu wyłączyć, "
                "ustaw knowledge_agent na disabled w tym pliku i uruchom usługę ponownie."),
    ASK_OFF: ("Bez modeli (tryb tylko deterministyczny) ask jest wyszukiwaniem pełnotekstowym w pamięci projektu i nie tworzy odpowiedzi. "
              "Wymienione fragmenty pasują do słów pytania: to wyniki wyszukiwania, a nie zweryfikowane dowody."),
    ASK_NO_MATCH: ("Bez modeli (tryb tylko deterministyczny) ask jest wyszukiwaniem pełnotekstowym w pamięci projektu i nie tworzy odpowiedzi. "
                   "Żaden fragment nie pasuje do słów pytania."),
    ASK_LEFT_OUT: ("Bez modeli (tryb tylko deterministyczny) ask jest wyszukiwaniem pełnotekstowym w pamięci projektu i nie tworzy odpowiedzi. "
                   "Są fragmenty pasujące do słów pytania, ale żadnego nie dało się pokazać; powody są wymienione przy wyniku."),
    ASK_TIMED_OUT: ("Bez modeli (tryb tylko deterministyczny) ask jest wyszukiwaniem pełnotekstowym w pamięci projektu i nie tworzy odpowiedzi. "
                    "Wyszukiwanie nie zakończyło się w czasie, więc nie wymieniono żadnych fragmentów."),
    MATCHES_HEADING: "Dopasowania pełnotekstowe (wyniki wyszukiwania, nie odpowiedź)",
    MATCHES_HELD_BACK: ("Część pasujących fragmentów pominięto ze względu na ich klasę wrażliwości, tak jak "
                        "odpowiedzi pomijają je dla zdalnego serwera modeli (memory_remote_classes)."),
    NEEDS_MODELS: ("{command} wymaga modeli agenta wiedzy, a agent wiedzy jest wyłączony "
                   "(tryb tylko deterministyczny)."),
    KNOWLEDGE_INVALID: "knowledge_agent w config.json ma wartość {value}; musi to być enabled albo disabled.",
    RESTART_FOR_MODELS: ("knowledge_agent jest włączony w config.json, ale ta usługa wystartowała bez modeli. "
                         "Uruchom ponownie usługę wiedzy (cairnmind stop, a potem ponownie uruchom Cairnmind), aby "
                         "używała modeli."),
    DATA_DIR: "Pamięć jest przechowywana w {data_dir}.",
    REQUIRED_PRESENT: "Wymagane i obecne:",
    REQUIRED_MISSING: "Wymagane i brakujące:",
    NOTHING_MISSING: "Wymagane i brakujące: nic.",
    OPTIONAL_OFF: "Opcjonalne i wyłączone:",
    OPTIONAL_ON: "Opcjonalne i włączone:",
    NOTES: "Uwagi:",
    FULL_REPORT: "Pełny raport: cairnmind doctor --json",
    REPORT_UNREADABLE: "Nie udało się odczytać raportu doctor.",
    NEXT_FIX: "Dalej: usuń braki wymienione w części Wymagane i brakujące, a potem uruchom ponownie cairnmind doctor.",
    NEXT_START: ('Dalej: cairnmind project add NAZWA FOLDER rejestruje projekt; potem cairnmind remember NAZWA "..." '
                 'zapisuje pierwsze ustalenie, a cairnmind brief NAZWA je pokazuje.'),
    ENGINE_READY: "Silnik bazy danych: odpowiada pod {url}; bazę pamięci można odczytać.",
    SERVICE_RUNNING: "Usługa lokalna: działa pod {url}.",
    SERVICE_ON_DEMAND: "Usługa lokalna: teraz nie działa; uruchomi ją następne polecenie, które jej potrzebuje.",
    ENGINE_ON_DEMAND: ("Silnik bazy danych: teraz nie działa pod {url}; uruchomi go następne polecenie, które go "
                       "potrzebuje (cairnmind engine start uruchamia go od razu)."),
    ENGINE_DOWN: ("Silnik bazy danych nie odpowiada pod {url} ({error}); może się uruchamiać albo być zajęty. Jeśli "
                  "jest zatrzymany: cairnmind engine start."),
    ENGINE_RELEASE: ("Silnik bazy danych pod {url} ma wydanie {found}, a nie przypięte {pinned}, i każde polecenie "
                     "otwierające magazyn go odrzuca. Zatrzymaj go (cairnmind stop, potem cairnmind engine stop) i "
                     "uruchom cairnmind engine start; jeśli pod tym adresem odpowiada inny program, zwolnij go albo "
                     "ustaw inny arcade_url."),
    SERVER_DOWN_CONNECT: ("Serwer bazy danych pod {url} nie odpowiada ({error}). Sprawdź, czy działa i czy ten "
                          "komputer ma do niego dostęp."),
    JAVA_NEEDED: ("Silnik wymaga Javy 21 lub nowszej: {found}. {hint}; albo uruchom silnik w Dockerze: "
                  "cairnmind engine start --docker."),
    MODELS_OFF: ("Modele lokalne: wyłączone (tryb tylko deterministyczny), więc ask wymienia pasujące fragmenty i "
                 "nie tworzy odpowiedzi. Aby je włączyć: cairnmind models pull przy działającej Ollamie "
                 "(około 7 GB)."),
    MODELS_ON: "Modele pamięci: włączone, a ich serwery odpowiadają.",
    WORKSPACE_OFF: "Środowisko terminalowe: {status}. {hint}. Wszystkie inne polecenia działają bez niego.",
    WORKSPACE_ON: "Środowisko terminalowe: gotowe; otwórz je poleceniem cairnmind.",
    CLIENT_OFF: ("{client}: nie zainstalowano; potrzebny tylko do sesji programistycznych (cairnmind run). "
                 "{note}"),
    CLIENT_ON: "{client}: zainstalowany i sprawdzony; uruchom go poleceniem cairnmind run NAZWA {command}.",
    PROJECT_ADDED: "Zarejestrowano {project} w {root}; zindeksowane pliki: {files}.",
    NOT_GIT: ("{root} nie jest repozytorium Git. Pamięć działa bez Gita; impact, commity w brief i sesje "
              "programistyczne (cairnmind run) wymagają repozytorium z pierwszym commitem (git init, git add, "
              "git commit)."),
    NO_COMMIT: ("{root} jest repozytorium Git bez żadnego commita. Pamięć działa; impact, commity w brief i sesje "
                "programistyczne (cairnmind run) wymagają pierwszego commita (git add, git commit)."),
    REMEMBER_PROPOSED: ("Zapisano jako proponowane ({kind}). Nic więcej nie jest potrzebne, aby pracować dalej: "
                        "cairnmind brief {project} i cairnmind ask {project} już z tego korzystają, z oznaczeniem "
                        "jako niepotwierdzone. Człowiek może je później potwierdzić lub zakwestionować, tylko w "
                        "inspektorze pamięci środowiska terminalowego (cairnmind, potem Ctrl+B i; wymaga "
                        "tmux 3.4+): wiersz poleceń niczego nie potwierdza."),
    BRIEF_START: ('Dalej: dla tego projektu nic jeszcze nie zapisano. cairnmind remember {project} "..." zapisuje '
                  'pierwsze ustalenie; sesja programistyczna uruchomiona przez cairnmind run {project} claude '
                  '(lub codex) zapisuje tu punkty kontrolne, plany i zadania.'),
    BRIEF_REVIEW: ("Dalej: nic więcej nie jest potrzebne, aby pracować dalej; brief i ask już korzystają z "
                   "proponowanych ustaleń, z oznaczeniem jako niepotwierdzone. Człowiek może je później potwierdzić "
                   "lub zakwestionować, tylko w inspektorze pamięci środowiska terminalowego (cairnmind, potem "
                   "Ctrl+B i; wymaga tmux 3.4+): wiersz poleceń niczego nie potwierdza."),
    INSTALL_NO_CLIENT: ("Nie zainstalowano tu ani Codexa, ani Claude Code, więc niczego nie zapisano i nie połączono "
                        "żadnego klienta. Pamięć działa bez klienta (cairnmind remember, brief, ask); po "
                        "zainstalowaniu jednego z nich uruchom ponownie cairnmind install {project}."),
    INSTALL_DONE: ("Połączono: {clients}. Zapisano w projekcie: {files}; przejrzyj pliki przed commitem. "
                   "Dalej: cairnmind run {project} {command}."),
    INSTALL_NOT_CONNECTED: ("Zapisano w projekcie: {files}. Nie połączono: {clients}. cairnmind doctor --contract "
                            "pokazuje, który test nie przechodzi i czego wymaga."),
    INSTALL_ALSO: ("Nie połączono: {clients}. cairnmind doctor --contract pokazuje, który test nie przechodzi "
                   "i czego wymaga."),
    NOTHING_WRITTEN: "nic nowego (pliki były aktualne)",
    CLIENT_SKIPPED: ("{client} nie jest zainstalowany, więc nic dla niego nie zapisano; po instalacji uruchom "
                     "ponownie cairnmind install, aby go połączyć."),
    REMOVED_COMMAND: ("{path} ({entry}) uruchamia polecenie devbrain, którego Cairnmind już nie instaluje; wpis "
                      "pozostaje bez zmian i nie uruchomi się. Wpisz tam cairnmind z tymi samymi argumentami."),
    REMOVED_COMMAND_BESIDE: ("{path} ({entry}) uruchamia polecenie devbrain, którego Cairnmind już nie instaluje; "
                             "wpis pozostaje bez zmian i nie uruchomi się. cairnmind install właśnie zapisał tam "
                             "własny wpis, więc usuń ten; wpisz w nim cairnmind z tymi samymi argumentami tylko "
                             "wtedy, gdy robi coś innego."),
    TRUST_NOTE: ("Każdy klient przy pierwszym uruchomieniu tutaj zapyta, czy ufasz serwerowi MCP i hookom tego "
                 "projektu; Cairnmind nie odpowiada za Ciebie na takie pytania."),
    SERVICE_STARTED: ("Uruchomiono usługę Cairnmind pod {url} dla {data_dir}. Działa w tle i uruchamia silnik "
                      "bazy danych, gdy ten nie działa; cairnmind stop ją zatrzymuje."),
    ENGINE_STARTING: "Uruchamianie silnika bazy danych ({mode}) dla {data_dir}.",
    ENGINE_STARTED: ("Silnik bazy danych ({mode}) odpowiada i działa w tle (cairnmind engine stop go zatrzymuje, po "
                     "cairnmind stop)."),
    UNKNOWN_PROJECT_NEXT: ("cairnmind project list pokazuje zarejestrowane projekty; cairnmind project add NAZWA "
                           "FOLDER rejestruje nowy."),
    PROJECT_NAME_TAKEN: ("Nazwa {name} jest już zarejestrowana, dla {root}. Wybierz inną nazwę; cairnmind project "
                         "list pokazuje zarejestrowane projekty."),
    PROJECT_ROOT_TAKEN: ("{root} jest już zarejestrowany jako projekt {name}. cairnmind project list pokazuje "
                         "zarejestrowane projekty."),
    RUN_NO_CLIENT: ("Zainstaluj go i zaloguj się ({url}), a potem uruchom to polecenie ponownie. Bez klienta "
                    "programistycznego działają: cairnmind remember, cairnmind brief i cairnmind ask."),
    LAUNCH_REFUSED: ("Odmowa zweryfikowanego uruchomienia {client}; brakuje: {missing}. cairnmind doctor --contract "
                     "pokazuje, który test nie przechodzi i czego wymaga."),
    PART_CONTRACT: "testy kontraktu klienta",
    PART_MCP: "wpis MCP",
    PART_HOOKS: "hooki sesji",
    PART_INSTRUCTIONS: "blok instrukcji",
    STATUS_NOT_RUNNING: ("Usługa Cairnmind nie działa, a diagnostyka jej nie uruchamia. Uruchomi ją następne "
                         "polecenie, które jej potrzebuje (na przykład cairnmind ask); cairnmind doctor sprawdza "
                         "instalację bez niej."),
    ENGINE_NOT_INSTALLED: ("Silnik bazy danych (ArcadeDB) nie jest zainstalowany dla tego katalogu danych. Uruchom "
                           "cairnmind engine install, a potem cairnmind init; instalator uruchamia oba."),
    ENGINE_EXITED: ("ArcadeDB zakończył działanie podczas startu. Zobacz {log}. Zwykle przyczyną jest zajęty port "
                    "({url}) albo za mało pamięci; po usunięciu przyczyny uruchom cairnmind engine start."),
    ENGINE_TIMED_OUT: "Start ArcadeDB przekroczył limit czasu. Zobacz {log}. Po usunięciu przyczyny uruchom cairnmind engine start.",
    OPTIONAL_BROKEN: "Opcjonalne i niedziałające:",
    NEXT_PROJECTS: ('Dalej: cairnmind project list wymienia twoje projekty; cairnmind brief NAZWA pokazuje, na czym '
                    'stoi praca w jednym z nich, a cairnmind remember NAZWA "..." zapisuje ustalenie.'),
    PROJECT_EXISTED: ('{project} był już zarejestrowany dla {root}; jego pliki sprawdzono ponownie (zindeksowane '
                      'pliki: {files}).'),
    PROJECT_FOLDER_MISSING: ("{root} nie istnieje. cairnmind project add NAZWA FOLDER przyjmuje folder projektu, "
                             "który już masz: sprawdź ścieżkę albo najpierw utwórz folder (mkdir -p {quoted})."),
    REMEMBER_STORED: "Zapisano jako {status} ({kind}), rekord {id}; cairnmind records {project} go wymienia.",
    STOP_STOPPING: ("Usługa Cairnmind pod {url} zatrzymuje się; następne polecenie, które jej potrzebuje, uruchomi "
                    "ją ponownie."),
    STOP_NOT_RUNNING: "Usługa Cairnmind pod {url} nie działała, więc niczego nie zatrzymano.",
    STOP_REFUSED: "Usługa Cairnmind pod {url} odpowiedziała, ale się nie zatrzymała; cairnmind status pokazuje jej stan.",
    ENGINE_LEFT_RUNNING: "To polecenie nie zatrzymuje silnika bazy danych; zatrzymuje go cairnmind engine stop.",
    ENGINE_STOPPED: ("Silnik bazy danych dla {data_dir} został zatrzymany; następne polecenie, które go potrzebuje, "
                     "uruchomi go ponownie."),
    ENGINE_STOPPED_CONNECT: ("Zatrzymano silnik, który ten katalog danych uruchomił wcześniej; ta instalacja tylko "
                             "łączy się ze swoim serwerem bazy danych, więc nic go ponownie nie uruchomi."),
    ENGINE_NOT_STOPPED: "Niczego nie zatrzymano: {reason}.",
    PROJECTS_NONE: "Nie zarejestrowano jeszcze żadnego projektu. Dalej: cairnmind project add NAZWA FOLDER rejestruje projekt.",
    PROJECTS_HEADING: "Zarejestrowane projekty:",
    PROJECTS_NEXT: ('Dalej: cairnmind brief NAZWA pokazuje, na czym stoi praca w jednym z nich; cairnmind remember '
                    'NAZWA "..." zapisuje ustalenie.'),
    INIT_READY: ("Pamięć jest gotowa w {data_dir}. Dalej: cairnmind doctor sprawdza każdą część, potem "
                 "cairnmind project add NAZWA FOLDER rejestruje projekt."),
    INIT_MARKED: ("Oznaczono {marked} potwierdzonych rekordów jako potwierdzone przez człowieka, bo każdy z nich w "
                  "obecnej postaci obejmuje przegląd lub formularz środowiska zapisany na tym komputerze; {left} "
                  "zachowuje swój status, dopóki nie potwierdzisz ich w inspektorze pamięci."),
    MODE_STATUS: "Brak odpowiedzi bez modeli; tylko dopasowania pełnotekstowe",
    FOREIGN_ENGINE: ("Silnik bazy danych pod {url} nie przyjmuje logowania {data_dir}, więc najpewniej należy do "
                     "innego katalogu danych. Daj temu katalogowi danych wolny port silnika: ustaw arcade_url w "
                     "{data_dir}/config.json, na przykład na http://127.0.0.1:2490. Albo zatrzymaj silnik, który "
                     "tam odpowiada: cairnmind stop, potem cairnmind engine stop, oba z --data-dir, który go "
                     "uruchomił."),
}
MESSAGES[OWN_ENGINE_REFUSED] = ("Silnik bazy danych tego katalogu danych pod {url} odrzucił logowanie. Po kilku nieudanych "
                                "logowaniach ArcadeDB odrzuca każde przez około 30 sekund: poczekaj i uruchom polecenie "
                                "ponownie. Jeśli dalej odmawia, logowanie z {data_dir}/config.json nie pasuje do tego "
                                "silnika.")
MESSAGES[PROCESS_OFF] = "Brak modeli. Uruchom `cairnmind models pull`, aby przetwarzać pamięć."
MESSAGES[PROCESS_RESTART] = "Modele włączone. Zrestartuj usługę (`cairnmind stop`, potem start), by ich użyła."


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
    # The check doctor makes: the command is on PATH (integrations._probe_cli).
    works = WITHOUT_WORKSPACE if any(shutil.which(name) for name in CLIENT_NAMES) else WITHOUT_WORKSPACE_NO_CLIENT
    return " ".join((WORKSPACE_NEEDS, state, hint, works))
