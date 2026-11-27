"""`cairnmind setup PROFILE`: where storage and the memory models run (#74).

A profile is validated and tested before anything is saved, then written only through the functions the existing
commands use, so every check they make still runs:

- the memory models: memory_models.check_role (the test), configure_role (the check and the save of `models set`),
  knowledge_mode.turn_on; a model server on another computer needs the consent and HTTPS that normalize_memory_model
  asks, and `ollama_url` is never written here;
- the database this installation only connects to: database_credentials (the private password file, https for
  another host, never root there, the version, the sign-in, create_user of `db create-user`), and save_storage, the
  one writer these settings have, which writes five named fields and never a password;
- the engine on this machine: engine_docker.choose_engine_mode and engine.start_command, as `init` and
  `engine start` call them;
- the writer of a store (store_writer): only read here. A store another installation writes makes this one a reader;
  `cairnmind store takeover` stays a person's separate act.

Nothing is saved when a test fails. When a step of the change fails, config.json is put back as it was. Every
sentence is an English constant with its Polish in MESSAGES; an item is (template, values), and a value may itself be
an item, or a list of them.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from cairnmind.storage import database_credentials, database_mode, engine, engine_docker, store_writer
from cairnmind.agents import knowledge_mode
from cairnmind.memory import memory_egress, memory_models
from cairnmind.cli_cmds import topology
from cairnmind.storage.arcade import ArcadeError
from cairnmind.pal.file_locks import lock_exclusive
from cairnmind.pal.private_files import atomic_replace, make_private
from cairnmind.config import Settings, derived_database, env_names, env_value
from cairnmind.agents.ollama import OllamaUnavailable
from cairnmind.pal.terminal_safe import safe_text

PROFILES = ("local", "remote-db", "remote")
STORAGE_FIELDS = ("arcade_url", "arcade_connect_url", "arcade_user", "arcade_database", "arcade_password_file",
                  "arcade_ca_file")
# The variables that replace a setting setup saves; an empty one names nothing where Settings.load says so.
SETTINGS_VARIABLES = {"arcade_url": "ARCADE_URL", "arcade_connect_url": "ARCADE_CONNECT_URL",
                      "arcade_user": "ARCADE_USER", "arcade_database": "ARCADE_DATABASE",
                      "arcade_password_file": "ARCADE_PASSWORD_FILE", "arcade_ca_file": "ARCADE_CA_FILE",
                      "arcade_password": "ARCADE_PASSWORD", "ollama_url": "OLLAMA_URL"}
EMPTY_IS_UNSET = frozenset({"ARCADE_CONNECT_URL", "ARCADE_PASSWORD", "OLLAMA_URL"})
REMOTE_OPTIONS = ("url", "user", "database", "password_file")
MODEL_OPTIONS = ("models_url", "provider", "chat_model", "embedding_model")
OPTION_NAMES = {"url": "--url", "user": "--user", "database": "--database", "password_file": "--password-file",
                "ca_file": "--ca-file", "admin_password_file": "--admin-password-file", "models_url": "--models-url",
                "provider": "--provider", "chat_model": "--chat-model", "embedding_model": "--embedding-model",
                "credential_ref": "--credential-ref", "allow_remote": "--allow-remote", "backup": "--backup",
                "empty": "--empty"}
LONG = frozenset({"error", "reason", "advice", "problem", "sentence"})  # values that are whole sentences of other modules

# Refusals and option errors
SERVICE_RUNS = ("The knowledge service of this data directory is running; run `cairnmind stop` first, then this "
                "command again. Nothing was changed.")
IN_SESSION = ("A coding session cannot change where storage and models run; only a person can, outside the session. "
              "Nothing was changed.")
VARIABLE_SET = ("{variable} is set in this shell and would replace what setup saves; unset it and run setup again. "
                "Nothing was changed.")
UNKNOWN_PROFILE = "Unknown profile {profile}: choose local, remote-db or remote."
NEEDS = "`cairnmind setup {profile}` needs these options: {options}."
NOT_FOR = "{option} does not go with `cairnmind setup {profile}`."
WITHOUT_PROFILE = "{option} goes with a profile: `cairnmind setup local`, `remote-db` or `remote`."
BOTH_CHOICES = "Choose one of --backup and --empty."
COMBINATION = ("`cairnmind setup {profile}` runs the memory models on this computer and takes no model options. Models "
               "on another computer with this storage are not a profile: run `cairnmind setup {profile}`, then "
               "`cairnmind models set` once for chat and once for embedding, with --allow-remote. `cairnmind setup "
               "remote` moves storage and models together.")
BACK_NEEDS_CHOICE = ("Going back to a store on this machine needs a choice about the data of the store at {url}: "
                     "--backup FILE takes a backup of it into FILE and restores that into the store on this machine; "
                     "--empty uses the store on this machine without that data. The store at {url} is left as it is "
                     "either way. Nothing was changed.")
ALREADY_LOCAL = ("Storage already runs on this machine; --backup and --empty are only for going back from a store this "
                 "installation only connects to. Nothing was changed.")
BACKUP_EXISTS = "{path} already exists; name a new file for the backup. Nothing was changed."
ADDRESS_INVALID = ("The address must start with http:// or https:// and name a host, with no user, password, query or "
                   "fragment.")
STORAGE_REFUSED = "The store at {url} cannot be used: {error} Nothing was saved."
SIGN_IN_REFUSED = "The sign-in was refused{advice}"
QUERY_FAILED = "The test query failed (status {status})."
MODEL_REFUSED = "The {role} model {model} at {url} cannot be used: {error} Nothing was saved."
MODELS_HERE_NEEDED = ("`cairnmind setup {profile}` runs the {role} model on this computer, and the Ollama at {url} "
                      "cannot serve {model}: {error} Install the model there (`ollama pull {model}`), then run this "
                      "again. Nothing was saved.")
INVALID_ROLE = ("The settings of the {role} model cannot be read: {error} Fix them with `cairnmind models set --role "
                "{role}`. Nothing was changed.")
REMOTE_NOT_READABLE = "The store at {url} cannot be read for the backup: {error} Nothing was changed."
# A sentence another module wrote, shown as that module wrote it (English); the one constant without its own Polish.
# The placeholder is not `text`: terminal_i18n.tr takes a parameter of that name.
VERBATIM = "{sentence}"
ADDRESS_SAVED = ("The database address that CAIRNMIND_ARCADE_CONNECT_URL gave was saved in config.json first, as every "
                 "command saves it the first time it sees it; nothing else changed.")
BAD_PREFIX = ("{variable} is not a valid database prefix (letters, digits and underscores); fix it and run setup again. "
              "Nothing was changed.")
STORAGE_REFUSED_NOTED = "The store at {url} cannot be used: {error} Nothing was saved.{notes}"

# Tests, plan and result
TEST_STORAGE_OK = ("Tested: ArcadeDB at {url} answers with version {version}, and the user {user} signs in to the "
                   "database {database}.")
TEST_ADMIN_OK = ("Tested: ArcadeDB at {url} answers with version {version} and accepts the administrator; nothing was "
                 "created.")
ADMIN_REFUSED = ("The administrator was refused: ArcadeDB at {url} did not accept root with the password in "
                 "--admin-password-file.")
# ArcadeDB cannot list users and answers alike for a user that does not exist and for a wrong password.
CREATE_BOTH = ("Not tested yet: the user {user} does not sign in to the database {database}, which does not exist. With "
               "--yes, --admin-password-file creates the database and, if it does not exist yet, the user, with rights "
               "on that database only. A user that exists with another password is never changed: that run is refused "
               "and nothing is created.")
CREATE_DB = ("Not tested yet: the user {user} exists, but the database {database} does not. With --yes, "
             "--admin-password-file creates the database; the user is used only if it has rights on it, otherwise "
             "nothing is created.")
NO_RIGHTS = ("Not tested yet: the user {user} exists, but it has no rights on the database {database}. With --yes the "
             "run is refused and nothing is created: each database gets its own user, so use a new user name.")
CREATE_USER = ("Not tested yet: the user {user} does not sign in to the database {database}, which exists. With --yes, "
               "--admin-password-file creates the user, if it does not exist yet, with rights on that database only. "
               "A user that exists with another password is never changed: that run is refused and nothing is "
               "created.")
TEST_READABLE = "Tested: the store at {url} can be read for the backup."
TEST_MODEL_OK = "Tested: the {role} model {model} at {url} answers."
CHANGE_STORAGE_REMOTE = ("Storage: from {before} to ArcadeDB at {url}, which this installation only connects to: user "
                         "{user}, database {database}, password read from {password_file}.")
CHANGE_CA = "Its certificate is checked against {ca_file}."
BEFORE_LOCAL = "the engine on this machine"
BEFORE_REMOTE = "ArcadeDB at {url} (database {database}, user {user})"
CHANGE_STORAGE_LOCAL = ("Storage: from ArcadeDB at {before} to the engine on this machine at {url}, with the database "
                        "{database}.")
ENGINE_START = ("The engine on this machine will be started ({engine}). Nothing is installed: the native engine needs "
                "`cairnmind engine install` done before; in Docker a missing engine image is downloaded, which is said "
                "when it happens.")
ENGINE_NATIVE = "native, Java 21 or newer"
ENGINE_DOCKER = "in Docker"
ENGINE_CHOSEN = "native with Java 21 or newer, or in Docker when it is not found, as `cairnmind init` chooses"
LOCAL_DATA_FOUND = ("The store on this machine still holds the database {database} from before; with --empty it is "
                    "used as it was left, with nothing from the store at {url}.")
LOCAL_DATA_NEW = "The store on this machine has no database {database}; with --empty it starts empty."
BACKUP_PLAN = ("The backup: the store at {url} is read into {path}, and nothing is written to it; the file is checked, "
               "then restored into the store on this machine, whose current contents first go to a safety backup in "
               "backups/. The backup carries no vectors: after the restore the service rebuilds them from the text, "
               "and until then search uses the text only.")
RESTORE_WILL_KEEP = ("A person's confirmations in the backup: the open workspace of this installation answers with its "
                     "key now, so they will be kept, as `cairnmind restore` keeps them; the key is read again just "
                     "before the restore.")
RESTORE_WILL_CLAIM = ("A person's confirmations in the backup: no open workspace of this installation answers with its "
                      "key now, so, as with `cairnmind restore`, each will be kept only as the backup's claim, not "
                      "verified here, and accepted transcript record kinds will be withdrawn. To keep them, open the "
                      "workspace (`cairnmind`), stop the service (`cairnmind stop`) and run this command from there; "
                      "the key is read again just before the restore.")
RESTORE_KEPT = ("A person's confirmations in the backup were kept: the open workspace of this installation answered "
                "with its key, as `cairnmind restore` requires.")
CHANGE_MODEL = "The {role} model: from {before} to {after}."
MODEL_AT = "{model} at {url}"
AGENT_ON = "The knowledge agent will be turned on; it is disabled now (deterministic-only mode)."
SENDS = ("{host} will receive your questions and the memory text of the classes {classes} (memory_remote_classes); "
         "nothing else leaves this computer.")
SENDS_NO_TEXT = "{host} will receive your questions and no memory text (memory_remote_classes is empty)."
REBUILD_VECTORS = ("To rebuild: the vectors, because the embedding model changes from {before} to {after}. Run "
                   "`cairnmind rebuild-vectors PROJECT` for each project after the change; the earlier vectors are kept "
                   "apart and never searched with the new model.")
WRITER_KEEP = "Writer: this installation already writes the store at {url}."
WRITER_NEW = ("Writer: no installation writes the store at {url} yet; this installation becomes its writer when it first "
              "opens it (`cairnmind init`).")
WRITER_OTHER = ("Writer: another installation writes the store at {url}, so this installation will only read it; setup "
                "does not take it over. {reason}")
WRITER_NOT_READ = ("Writer: it cannot be read yet ({reason}). The store decides when this installation first opens it: "
                   "this installation becomes the writer when none is recorded, and only reads when another "
                   "installation writes it.")
WRITER_AFTER_USER = ("Writer: not read yet, because the user does not sign in to the store at {url} yet; it is read "
                     "once the user signs in.")
LEFT_LOCAL_STORE = ("Left as it is: the store on this machine, in {path}. This installation stops using it, and it is "
                    "not copied to the new store. To take its data along, run `cairnmind backup FILE` before this "
                    "command, and `cairnmind init` and `cairnmind restore FILE` after it.")
LEFT_ENGINE = ("Left as it is: the engine this data directory started on this machine; it is not used any more, and "
               "`cairnmind engine stop` stops it.")
LEFT_REMOTE_STORE = "Left as it is: the store at {url} (database {database}); nothing in it is changed or deleted."
LOCAL_NAME_DIFFERS = ("The store there holds the database {database}, not {derived}, the name `cairnmind setup local` "
                      "uses on this machine; its data stays on disk under {database}.")
LEFT_WRITER_NOTE = ("This installation stays named as its writer; another installation that should write it runs "
                    "`cairnmind store takeover --yes`.")
LEFT_MODEL_SERVER = "Left as it is: the model server at {url}; nothing is sent to it any more."
NOTHING_TO_DO = ("Storage and the memory models already run as `cairnmind setup {profile}` says; nothing to change.")
NOT_APPLIED = "Nothing was changed. To make these changes, run the same command with --yes."
BACKING_UP = "Writing a backup of the store at {url} into {path}; nothing is written to that store."
ENGINE_STARTING = "Starting the engine on this machine."
RESTORING = "Restoring {path} into the store on this machine."
BACKUP_WRITTEN = "Backup written and checked: {path}."
USER_CREATED = "Created on ArcadeDB at {url}: the user {user}, with rights on the database {database} only."
DATABASE_CREATED = ("Created on ArcadeDB at {url}: the database {database}. The user {user} already existed, with "
                    "rights on it.")
CREATED = (USER_CREATED, DATABASE_CREATED)  # the sentences that say the server was changed
USER_EXISTED = ("The user {user} and the database {database} already existed on ArcadeDB at {url}; nothing was created "
                "there.")
ENGINE_STARTED = "The engine on this machine answers ({engine})."
RESTORED = "Restored {path} into the store on this machine; its earlier contents are in a safety backup in {backups}."
SAVED = "Saved in {config}."
NEXT_REMOTE = "Next: `cairnmind init` opens the new store; then start Cairnmind as usual."
NEXT_LOCAL = "Next: start Cairnmind as usual."
NEXT_LOCAL_EMPTY = "Next: `cairnmind init` opens the store on this machine; then start Cairnmind as usual."
STEP_STORAGE = "saving the storage settings"
STEP_ENGINE = "starting the engine on this machine"
STEP_RESTORE = "the restore"
STEP_MODEL = "saving the {role} model"
STEP_AGENT = "turning the knowledge agent on"
PUT_BACK = "The change stopped at {step}: {error} config.json was put back as it was.{notes}"
PUT_BACK_FAILED = ("The change stopped at {step}: {error} config.json could NOT be put back ({reason}); check it "
                   "before you start Cairnmind.{notes}")
KEPT_BACKUP = "The backup {path} is kept."
KEPT_PASSWORD = ("The password that engine now uses stays in config.json (arcade_password), so that `cairnmind setup "
                 "local` can be run again.")
KEPT_PASSWORD_START = ("The engine did not finish starting, and ArcadeDB may already have taken the password this "
                       "change generated for it, so that password stays in config.json (arcade_password) and "
                       "`cairnmind setup local` can be run again.")
PASSWORD_NOT_KEPT_NEW = ("The password generated for the engine on this machine could not be kept in config.json "
                         "(arcade_password): {reason}. The engine may now hold a password nobody knows. This command "
                         "just created its `arcade` folder in the data directory, so stopping the engine "
                         "(`cairnmind engine stop`) and removing that `arcade` folder lets `cairnmind setup local` "
                         "run again.")
PASSWORD_NOT_KEPT_OLD = ("The password generated for the engine on this machine could not be kept in config.json "
                         "(arcade_password): {reason}. The `arcade` folder in the data directory held a store before "
                         "this command, so remove nothing: the arcade_password from your own earlier copy of "
                         "config.json is what signs in to that engine.")
RESTORE_DONE = ("The restore had already finished: the store on this machine holds the data of {path}, and its earlier "
                "contents are in a safety backup in {backups}.")
CONFIRMATIONS = ("The restore marked {marked} records as confirmed by a person and left {left} before the rule; confirm "
                 "the others in the inspector.")
ENGINE_LEFT = "The engine on this machine was started and is left running; `cairnmind engine stop` stops it."
BACKUP_FAILED = "The backup of the store at {url} failed: {error} Nothing was changed."
BACKUP_CHECK_FAILED = "The backup {path} did not pass its check: {error} The file is kept; nothing was changed."

MESSAGES = {
    SERVICE_RUNS: "Usługa wiedzy tego katalogu danych działa; najpierw uruchom `cairnmind stop`, a potem to polecenie ponownie. Nic nie zostało zmienione.",
    IN_SESSION: "Sesja programistyczna nie może zmienić miejsca działania bazy i modeli; może to zrobić tylko człowiek, poza sesją. Nic nie zostało zmienione.",
    VARIABLE_SET: "{variable} jest ustawiona w tej powłoce i zastąpiłaby to, co zapisze setup; usuń ją i uruchom setup ponownie. Nic nie zostało zmienione.",
    UNKNOWN_PROFILE: "Nieznany profil {profile}: wybierz local, remote-db albo remote.",
    NEEDS: "`cairnmind setup {profile}` wymaga tych opcji: {options}.",
    NOT_FOR: "{option} nie pasuje do `cairnmind setup {profile}`.",
    WITHOUT_PROFILE: "{option} podaje się razem z profilem: `cairnmind setup local`, `remote-db` albo `remote`.",
    BOTH_CHOICES: "Wybierz jedno z --backup i --empty.",
    COMBINATION: "`cairnmind setup {profile}` uruchamia modele pamięci na tym komputerze i nie przyjmuje opcji modeli. Modele na innym komputerze przy tej bazie to nie profil: uruchom `cairnmind setup {profile}`, a potem `cairnmind models set` raz dla chat i raz dla embedding, z --allow-remote. `cairnmind setup remote` przenosi bazę i modele razem.",
    BACK_NEEDS_CHOICE: "Powrót do bazy na tym komputerze wymaga decyzji o danych bazy pod adresem {url}: --backup PLIK zapisuje jej kopię zapasową do PLIKU i odtwarza ją w bazie na tym komputerze; --empty używa bazy na tym komputerze bez tych danych. Baza pod adresem {url} w obu przypadkach zostaje bez zmian. Nic nie zostało zmienione.",
    ALREADY_LOCAL: "Baza już działa na tym komputerze; --backup i --empty służą tylko do powrotu z bazy, z którą ta instalacja tylko się łączy. Nic nie zostało zmienione.",
    BACKUP_EXISTS: "{path} już istnieje; podaj nowy plik na kopię zapasową. Nic nie zostało zmienione.",
    ADDRESS_INVALID: "Adres musi zaczynać się od http:// lub https:// i wskazywać host, bez użytkownika, hasła, zapytania i fragmentu.",
    STORAGE_REFUSED: "Nie można użyć bazy pod adresem {url}: {error} Nic nie zostało zapisane.",
    SIGN_IN_REFUSED: "Logowanie zostało odrzucone{advice}",
    QUERY_FAILED: "Zapytanie testowe nie powiodło się (status {status}).",
    MODEL_REFUSED: "Nie można użyć modelu {role} {model} pod adresem {url}: {error} Nic nie zostało zapisane.",
    MODELS_HERE_NEEDED: "`cairnmind setup {profile}` uruchamia model {role} na tym komputerze, a Ollama pod adresem {url} nie może udostępnić {model}: {error} Zainstaluj tam ten model (`ollama pull {model}`) i uruchom to ponownie. Nic nie zostało zapisane.",
    INVALID_ROLE: "Nie można odczytać ustawień modelu {role}: {error} Popraw je poleceniem `cairnmind models set --role {role}`. Nic nie zostało zmienione.",
    REMOTE_NOT_READABLE: "Nie można odczytać bazy pod adresem {url} do kopii zapasowej: {error} Nic nie zostało zmienione.",
    TEST_STORAGE_OK: "Sprawdzono: ArcadeDB pod adresem {url} odpowiada w wersji {version}, a użytkownik {user} loguje się do bazy {database}.",
    TEST_ADMIN_OK: "Sprawdzono: ArcadeDB pod adresem {url} odpowiada wersją {version} i przyjmuje administratora; nic nie zostało utworzone.",
    ADMIN_REFUSED: "Administrator został odrzucony: ArcadeDB pod adresem {url} nie przyjął konta root z hasłem z --admin-password-file.",
    CREATE_BOTH: "Jeszcze nie sprawdzono: użytkownik {user} nie loguje się do bazy {database}, która nie istnieje. Z --yes opcja --admin-password-file tworzy bazę i, jeśli jeszcze nie istnieje, użytkownika, z uprawnieniami tylko do tej bazy. Istniejącemu użytkownikowi z innym hasłem nic się nie zmienia: taki przebieg jest odrzucany i nic nie powstaje.",
    CREATE_DB: "Jeszcze nie sprawdzono: użytkownik {user} istnieje, ale baza {database} nie. Z --yes opcja --admin-password-file tworzy bazę; użytkownik zostaje użyty tylko wtedy, gdy ma do niej uprawnienia, w przeciwnym razie nic nie powstaje.",
    NO_RIGHTS: "Jeszcze nie sprawdzono: użytkownik {user} istnieje, ale nie ma uprawnień do bazy {database}. Z --yes przebieg jest odrzucany i nic nie powstaje: każda baza ma własnego użytkownika, więc użyj nowej nazwy użytkownika.",
    CREATE_USER: "Jeszcze nie sprawdzono: użytkownik {user} nie loguje się do bazy {database}, która istnieje. Z --yes opcja --admin-password-file tworzy użytkownika, jeśli jeszcze nie istnieje, z uprawnieniami tylko do tej bazy. Istniejącemu użytkownikowi z innym hasłem nic się nie zmienia: taki przebieg jest odrzucany i nic nie powstaje.",
    TEST_READABLE: "Sprawdzono: bazę pod adresem {url} można odczytać do kopii zapasowej.",
    TEST_MODEL_OK: "Sprawdzono: model {role} {model} pod adresem {url} odpowiada.",
    CHANGE_STORAGE_REMOTE: "Baza: z {before} na ArcadeDB pod adresem {url}, z którą ta instalacja tylko się łączy: użytkownik {user}, baza {database}, hasło czytane z {password_file}.",
    CHANGE_CA: "Jej certyfikat jest sprawdzany względem {ca_file}.",
    BEFORE_LOCAL: "silnika na tym komputerze",
    BEFORE_REMOTE: "ArcadeDB pod adresem {url} (baza {database}, użytkownik {user})",
    ADDRESS_SAVED: "Adres bazy z CAIRNMIND_ARCADE_CONNECT_URL został najpierw zapisany w config.json, tak jak zapisuje go każde polecenie przy pierwszym zobaczeniu; nic poza tym nie zostało zmienione.",
    BAD_PREFIX: "{variable} nie jest poprawnym prefiksem bazy (litery, cyfry i podkreślenia); popraw go i uruchom setup ponownie. Nic nie zostało zmienione.",
    STORAGE_REFUSED_NOTED: "Nie można użyć bazy pod adresem {url}: {error} Nic nie zostało zapisane.{notes}",
    LOCAL_NAME_DIFFERS: "Baza tam ma bazę danych {database}, a nie {derived}, czyli nazwę, której `cairnmind setup local` używa na tym komputerze; jej dane zostają na dysku pod nazwą {database}.",
    KEPT_PASSWORD: "Hasło, którego ten silnik teraz używa, zostaje w config.json (arcade_password), aby można było ponownie uruchomić `cairnmind setup local`.",
    KEPT_PASSWORD_START: "Silnik nie dokończył uruchamiania, a ArcadeDB mogła już przyjąć hasło wygenerowane dla niego przez tę zmianę, więc to hasło zostaje w config.json (arcade_password), aby można było ponownie uruchomić `cairnmind setup local`.",
    PASSWORD_NOT_KEPT_NEW: "Hasła wygenerowanego dla silnika na tym komputerze nie udało się zachować w config.json (arcade_password): {reason}. Silnik może teraz mieć hasło, którego nikt nie zna. To polecenie dopiero co utworzyło jego folder `arcade` w katalogu danych, więc zatrzymanie silnika (`cairnmind engine stop`) i usunięcie tego folderu `arcade` pozwala uruchomić `cairnmind setup local` ponownie.",
    PASSWORD_NOT_KEPT_OLD: "Hasła wygenerowanego dla silnika na tym komputerze nie udało się zachować w config.json (arcade_password): {reason}. Folder `arcade` w katalogu danych zawierał bazę jeszcze przed tym poleceniem, więc niczego nie usuwaj: do tego silnika loguje arcade_password z Twojej własnej wcześniejszej kopii config.json.",
    RESTORE_DONE: "Odtwarzanie już się zakończyło: baza na tym komputerze ma dane z {path}, a jej wcześniejsza zawartość jest w kopii bezpieczeństwa w {backups}.",
    CONFIRMATIONS: "Odtwarzanie oznaczyło {marked} rekordów jako potwierdzone przez człowieka i zostawiło {left} sprzed reguły; pozostałe potwierdź w inspektorze.",
    CHANGE_STORAGE_LOCAL: "Baza: z ArcadeDB pod adresem {before} na silnik na tym komputerze pod adresem {url}, z bazą {database}.",
    ENGINE_START: "Silnik na tym komputerze zostanie uruchomiony ({engine}). Nic nie jest instalowane: silnik natywny wymaga wcześniejszego `cairnmind engine install`; w Dockerze brakujący obraz silnika jest pobierany, co zostaje wtedy powiedziane.",
    ENGINE_NATIVE: "natywny, Java 21 lub nowsza",
    ENGINE_DOCKER: "w Dockerze",
    ENGINE_CHOSEN: "natywny z Javą 21 lub nowszą albo w Dockerze, gdy jej nie ma, tak jak wybiera `cairnmind init`",
    LOCAL_DATA_FOUND: "Baza na tym komputerze nadal ma bazę danych {database} sprzed zmiany; z --empty jest używana tak, jak została zostawiona, bez niczego z bazy pod adresem {url}.",
    LOCAL_DATA_NEW: "Baza na tym komputerze nie ma bazy danych {database}; z --empty zaczyna pusta.",
    BACKUP_PLAN: "Kopia zapasowa: baza pod adresem {url} jest czytana do {path}, bez żadnego zapisu do niej; plik jest sprawdzany, a potem odtwarzany w bazie na tym komputerze, której obecna zawartość najpierw trafia do kopii bezpieczeństwa w backups/. Kopia zapasowa nie zawiera wektorów: po odtworzeniu usługa odbudowuje je z tekstu, a do tego czasu wyszukiwanie korzysta tylko z tekstu.",
    RESTORE_WILL_KEEP: "Potwierdzenia osoby w kopii zapasowej: otwarte środowisko tej instalacji odpowiada teraz swoim kluczem, więc zostaną zachowane, tak jak zachowuje je `cairnmind restore`; klucz jest czytany ponownie tuż przed odtwarzaniem.",
    RESTORE_WILL_CLAIM: "Potwierdzenia osoby w kopii zapasowej: żadne otwarte środowisko tej instalacji nie odpowiada teraz swoim kluczem, więc, tak jak przy `cairnmind restore`, każde zostanie zachowane tylko jako twierdzenie kopii zapasowej, niezweryfikowane tutaj, a zaakceptowane rodzaje rekordów transkrypcji zostaną wycofane. Aby je zachować, otwórz środowisko (`cairnmind`), zatrzymaj usługę (`cairnmind stop`) i uruchom to polecenie stamtąd; klucz jest czytany ponownie tuż przed odtwarzaniem.",
    RESTORE_KEPT: "Potwierdzenia osoby w kopii zapasowej zostały zachowane: otwarte środowisko tej instalacji odpowiedziało swoim kluczem, czego wymaga `cairnmind restore`.",
    CHANGE_MODEL: "Model {role}: z {before} na {after}.",
    MODEL_AT: "{model} pod adresem {url}",
    AGENT_ON: "Agent wiedzy zostanie włączony; teraz jest wyłączony (tryb tylko deterministyczny).",
    SENDS: "{host} będzie otrzymywać twoje pytania i tekst pamięci z klas {classes} (memory_remote_classes); nic więcej nie opuszcza tego komputera.",
    SENDS_NO_TEXT: "{host} będzie otrzymywać twoje pytania, ale żadnego tekstu pamięci (memory_remote_classes jest puste).",
    REBUILD_VECTORS: "Do przebudowania: wektory, bo model embeddingu zmienia się z {before} na {after}. Po zmianie uruchom `cairnmind rebuild-vectors PROJEKT` dla każdego projektu; wcześniejsze wektory są trzymane osobno i nigdy nie są przeszukiwane nowym modelem.",
    WRITER_KEEP: "Zapisujący: ta instalacja już zapisuje bazę pod adresem {url}.",
    WRITER_NEW: "Zapisujący: żadna instalacja jeszcze nie zapisuje bazy pod adresem {url}; ta instalacja zostanie zapisującą, gdy pierwszy raz ją otworzy (`cairnmind init`).",
    WRITER_OTHER: "Zapisujący: bazę pod adresem {url} zapisuje inna instalacja, więc ta instalacja będzie ją tylko czytać; setup jej nie przejmuje. {reason}",
    WRITER_NOT_READ: "Zapisujący: nie można go jeszcze odczytać ({reason}). Baza rozstrzyga to, gdy ta instalacja pierwszy raz ją otworzy: ta instalacja zostaje zapisującą, gdy żadnej nie zapisano, i tylko czyta, gdy zapisuje ją inna instalacja.",
    WRITER_AFTER_USER: "Zapisujący: jeszcze nie odczytano, bo użytkownik nie loguje się jeszcze do bazy {url}; zostanie odczytany, gdy użytkownik się zaloguje.",
    LEFT_LOCAL_STORE: "Zostaje bez zmian: baza na tym komputerze, w {path}. Ta instalacja przestaje jej używać i nie jest ona kopiowana do nowej bazy. Aby zabrać jej dane, uruchom `cairnmind backup PLIK` przed tym poleceniem, a `cairnmind init` i `cairnmind restore PLIK` po nim.",
    LEFT_ENGINE: "Zostaje bez zmian: silnik, który ten katalog danych uruchomił na tym komputerze; nie jest już używany, a `cairnmind engine stop` go zatrzymuje.",
    LEFT_REMOTE_STORE: "Zostaje bez zmian: baza pod adresem {url} (baza danych {database}); nic w niej nie jest zmieniane ani usuwane.",
    LEFT_WRITER_NOTE: "Ta instalacja pozostaje w niej zapisana jako zapisująca; inna instalacja, która ma ją zapisywać, uruchamia `cairnmind store takeover --yes`.",
    LEFT_MODEL_SERVER: "Zostaje bez zmian: serwer modeli pod adresem {url}; nic więcej nie jest do niego wysyłane.",
    NOTHING_TO_DO: "Baza i modele pamięci już działają tak, jak mówi `cairnmind setup {profile}`; nie ma czego zmieniać.",
    NOT_APPLIED: "Nic nie zostało zmienione. Aby wprowadzić te zmiany, uruchom to samo polecenie z --yes.",
    BACKING_UP: "Zapisywanie kopii zapasowej bazy pod adresem {url} do {path}; nic nie jest zapisywane do tej bazy.",
    ENGINE_STARTING: "Uruchamianie silnika na tym komputerze.",
    RESTORING: "Odtwarzanie {path} w bazie na tym komputerze.",
    BACKUP_WRITTEN: "Kopia zapasowa zapisana i sprawdzona: {path}.",
    USER_CREATED: "Utworzono w ArcadeDB pod adresem {url}: użytkownika {user} z uprawnieniami tylko do bazy {database}.",
    DATABASE_CREATED: "Utworzono w ArcadeDB pod adresem {url}: bazę {database}. Użytkownik {user} już istniał i ma do niej uprawnienia.",
    USER_EXISTED: "Użytkownik {user} i baza {database} już istniały w ArcadeDB pod adresem {url}; nic tam nie utworzono.",
    ENGINE_STARTED: "Silnik na tym komputerze odpowiada ({engine}).",
    RESTORED: "Odtworzono {path} w bazie na tym komputerze; jej wcześniejsza zawartość jest w kopii bezpieczeństwa w {backups}.",
    SAVED: "Zapisano w {config}.",
    NEXT_REMOTE: "Dalej: `cairnmind init` otwiera nową bazę; potem uruchom Cairnmind jak zwykle.",
    NEXT_LOCAL: "Dalej: uruchom Cairnmind jak zwykle.",
    NEXT_LOCAL_EMPTY: "Dalej: `cairnmind init` otwiera bazę na tym komputerze; potem uruchom Cairnmind jak zwykle.",
    STEP_STORAGE: "zapisywaniu ustawień bazy",
    STEP_ENGINE: "uruchamianiu silnika na tym komputerze",
    STEP_RESTORE: "odtwarzaniu",
    STEP_MODEL: "zapisywaniu modelu {role}",
    STEP_AGENT: "włączaniu agenta wiedzy",
    PUT_BACK: "Zmiana zatrzymała się przy {step}: {error} config.json przywrócono do poprzedniego stanu.{notes}",
    PUT_BACK_FAILED: "Zmiana zatrzymała się przy {step}: {error} config.json NIE udało się przywrócić ({reason}); sprawdź go, zanim uruchomisz Cairnmind.{notes}",
    KEPT_BACKUP: "Kopia zapasowa {path} zostaje zachowana.",
    ENGINE_LEFT: "Silnik na tym komputerze został uruchomiony i nadal działa; `cairnmind engine stop` go zatrzymuje.",
    BACKUP_FAILED: "Kopia zapasowa bazy pod adresem {url} nie powiodła się: {error} Nic nie zostało zmienione.",
    BACKUP_CHECK_FAILED: "Kopia zapasowa {path} nie przeszła sprawdzenia: {error} Plik zostaje zachowany; nic nie zostało zmienione.",
}


# --- Sentences ------------------------------------------------------------------------------------------------------

def _value(key: str, value, language: str) -> str:
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str) and isinstance(value[1], dict):
        return render_one(value, language)  # a sentence inside a sentence
    if isinstance(value, list):  # sentences that follow: each after a space, none when the list is empty
        return "".join(" " + render_one(item, language) for item in value)
    text = safe_text(value, limit=600 if key in LONG else 200)
    if key == "error" and text and not text.endswith((".", "!", "?")):
        text += "."  # every {error} is followed by a sentence of its own ("... Nothing was saved.")
    return text


def render_one(item: tuple[str, dict], language: str = "en") -> str:
    """One sentence in ``language``; every value from settings, a server or an exception is made literal text."""
    template, values = item
    shown = {key: _value(key, value, language) for key, value in values.items()}
    if language == "en":
        return template.format(**shown)
    from cairnmind.terminal.terminal_i18n import tr  # imported here: terminal_i18n merges MESSAGES of this module
    return tr(template, language, **shown)


class Said(ValueError):
    """A sentence of this module with its values, so the workspace can say it in Polish."""

    def __init__(self, template: str, **values):
        self.template, self.values = template, values
        super().__init__(render_one((template, values), "en"))


class SetupRefused(Said):
    """Refused before anything changed (exit 2)."""


class SetupFailed(Said):
    """A step of the change failed; config.json was put back (exit 1)."""


@dataclass(frozen=True)
class Request:
    profile: str
    url: str | None = None
    user: str | None = None
    database: str | None = None
    password_file: str | None = None
    ca_file: str | None = None
    admin_password_file: str | None = None
    models_url: str | None = None
    provider: str | None = None
    chat_model: str | None = None
    embedding_model: str | None = None
    credential_ref: str | None = None
    allow_remote: bool = False
    backup: str | None = None
    empty: bool = False


def given_options(fields: dict) -> list[str]:
    """The options a person typed, as typed (`--password-file`), in the order of OPTION_NAMES."""
    return [OPTION_NAMES[name] for name in OPTION_NAMES if fields.get(name)]


def option_problem(request: Request) -> Said | None:
    """What is wrong with the options for the profile, as a sentence, or None (the command line's error, exit 2)."""
    profile = request.profile
    if profile not in PROFILES:
        return Said(UNKNOWN_PROFILE, profile=profile)

    def first(*names):
        return next((OPTION_NAMES[name] for name in names if getattr(request, name)), None)
    models = (*MODEL_OPTIONS, "credential_ref", "allow_remote")
    if profile == "local":
        found = first(*REMOTE_OPTIONS, "ca_file", "admin_password_file")
        if found:
            return Said(NOT_FOR, option=found, profile=profile)
        if first(*models):
            return Said(COMBINATION, profile=profile)
        return Said(BOTH_CHOICES) if request.backup and request.empty else None
    if first("backup", "empty"):
        return Said(NOT_FOR, option=first("backup", "empty"), profile=profile)
    if profile == "remote-db" and first(*models):
        return Said(COMBINATION, profile=profile)
    needed = REMOTE_OPTIONS if profile == "remote-db" else REMOTE_OPTIONS + MODEL_OPTIONS
    if any(not getattr(request, name) for name in needed):
        return Said(NEEDS, profile=profile, options=", ".join(OPTION_NAMES[name] for name in needed))
    return None


def environment_problem(settings) -> SetupRefused | None:
    """A variable that replaces a setting setup saves would make the test and the saved value disagree (a later
    process would use the variable): refused and named, never worked around."""
    for variable in SETTINGS_VARIABLES.values():
        named = next((key for key in env_names(variable)
                      if (os.environ.get(key) if variable in EMPTY_IS_UNSET else key in os.environ)), None)
        if named:
            return SetupRefused(VARIABLE_SET, variable=named)
    return None


# --- Storage --------------------------------------------------------------------------------------------------------

def remote_storage(settings, request) -> tuple[dict, Settings]:
    """The five settings of a database this installation only connects to, and the settings that would result.

    The password is read from the private file by the code every command uses (read_private_file: an absolute path, a
    regular file of this user, mode 0600, not a link) into the candidate only; config.json gets the path."""
    url = request.url
    if not database_mode.savable(url):
        raise SetupRefused(STORAGE_REFUSED, url=database_mode.shown(url), error=(ADDRESS_INVALID, {}))
    password_file = os.path.abspath(os.path.expanduser(request.password_file))
    ca_file = os.path.abspath(os.path.expanduser(request.ca_file)) if request.ca_file else ""
    password, source, problem = database_credentials.resolve_password(
        {"arcade_url": url, "arcade_password_file": password_file}, environ={})
    candidate = replace(settings, arcade_connect_url=url, arcade_url=url, arcade_user=request.user,
                        arcade_database=request.database, arcade_password_file=password_file,
                        arcade_ca_file=ca_file, arcade_password=password,
                        _origin={"password": source, "problem": problem, "kept": {}, "environment": {},
                                 "database": "configured", "user": "configured", "connect_problem": None})
    refused = database_credentials.problem(candidate)
    if refused:
        raise SetupRefused(STORAGE_REFUSED, url=database_mode.shown(url), error=refused)
    return ({"arcade_connect_url": url, "arcade_user": request.user, "arcade_database": request.database,
             "arcade_password_file": password_file, "arcade_ca_file": ca_file}, candidate)


def save_storage(data_dir: Path, fields: dict) -> None:
    """Change the named storage settings in config.json as stored, under the settings lock (as save_role does).

    Settings in memory also hold environment values and a password read from a file; saving those would make them
    permanent, so only these fields change, and never through Settings.save."""
    assert set(fields) <= set(STORAGE_FIELDS), fields
    path = data_dir / "config.json"
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (data_dir / "config.lock").open("a", encoding="utf-8") as lock:
        lock_exclusive(lock)
        values = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(values, dict):
            raise ValueError("config.json is not a settings object; nothing was saved.")
        values.update(fields)
        memory_models._write_json(path, values)


def _put_back(path: Path, data: bytes) -> None:
    """config.json as it was, byte for byte: atomic, owner-readable, under the settings lock."""
    with (path.parent / "config.lock").open("a", encoding="utf-8") as lock:
        lock_exclusive(lock)
        descriptor, name = tempfile.mkstemp(prefix=".config-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            make_private(name)
            atomic_replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)


def _generated_password(path: Path, snapshot: bytes) -> str | None:
    """The managed password config.json gained since the snapshot, or None."""
    try:
        before, now = json.loads(snapshot), json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    password = now.get("arcade_password") if isinstance(now, dict) and isinstance(before, dict) else None
    return password if password and not before.get("arcade_password") else None


def _keep_password(path: Path, password: str) -> None:
    """After a put-back: the password a started engine uses stays, unless config.json already holds one."""
    with (path.parent / "config.lock").open("a", encoding="utf-8") as lock:
        lock_exclusive(lock)
        values = json.loads(path.read_text(encoding="utf-8"))
        values.setdefault("arcade_password", password)
        memory_models._write_json(path, values)


def local_storage(settings) -> dict:
    """The settings of the engine this installation manages on this machine, as every managed installation has them.

    No password is named: the managed engine's root signs in with the generated arcade_password that config.json
    kept, or Settings.load(create=True) generates one after the switch. The database is the name derived from the
    data directory, so a --database given to remote-db cannot lose the local store. The prefix of that name is checked
    here as Settings.load checks it, because derived_database does not."""
    if not env_value("ARCADE_DB_PREFIX", "cairnmind").replace("_", "").isalnum():
        raise SetupRefused(BAD_PREFIX, variable=next(key for key in env_names("ARCADE_DB_PREFIX") if key in os.environ))
    stored = (settings._origin or {}).get("kept", {}).get("arcade_url", (settings.arcade_url,))[0]
    default = Settings.__dataclass_fields__["arcade_url"].default
    return {"arcade_url": stored if isinstance(stored, str) and database_mode.is_managed_url(stored) else default,
            "arcade_connect_url": "", "arcade_user": "root", "arcade_database": derived_database(settings.data_dir),
            "arcade_password_file": "", "arcade_ca_file": ""}


def _test_readable(settings, result) -> None:
    """Open the store this installation connects to for reading only (no write, no engine start)."""
    from cairnmind.store import Store
    from cairnmind.storage.store_readonly import StoreNotReady
    url = database_mode.address(settings)
    try:
        store = Store.open_readonly(settings)
    except (StoreNotReady, ArcadeError, ValueError) as exc:
        raise SetupRefused(REMOTE_NOT_READABLE, url=url, error=str(exc)) from None
    store.client.close()
    result["tests"].append((TEST_READABLE, {"url": url}))


def _storage_plan(settings, request, result) -> dict | None:
    """The storage change, or None when storage stays as it is. Reads and tests; writes nothing."""
    connect = database_mode.connect_only(settings)
    if request.profile == "local":
        if not connect:
            if request.backup or request.empty:
                raise SetupRefused(ALREADY_LOCAL)
            return None
        if not (request.backup or request.empty):  # never an empty store on this machine unless the person said so
            raise SetupRefused(BACK_NEEDS_CHOICE, url=database_mode.address(settings))
        plan = {"fields": local_storage(settings), "target": None}
        if request.backup:
            target = Path(request.backup).expanduser().absolute()
            if target.exists() or target.is_symlink():  # a person's earlier backup is never overwritten
                raise SetupRefused(BACKUP_EXISTS, path=target)
            _test_readable(settings, result)
            plan["target"] = target
        return plan
    fields, candidate = remote_storage(settings, request)
    if connect and all(getattr(settings, name) == value for name, value in fields.items()):
        return None
    return {"fields": fields, "candidate": candidate}


def _writer_items(found: dict, url: str) -> list:
    """What the store code will do at this installation's first open of the store, from a read-only look."""
    role, reason = found.get("role"), found.get("reason", "")
    if role == "writer":
        items = [(WRITER_KEEP, {"url": url})]
    elif role == "none":
        items = [(WRITER_NEW, {"url": url})]
    elif role in ("reader", "lost"):
        items = [(WRITER_OTHER, {"url": url, "reason": reason})]
    else:
        items = [(WRITER_NOT_READ, {"url": url, "reason": reason})]
    if found.get("problem"):  # a writer row that the journal head contradicts: said as it is
        items.append((VERBATIM, {"sentence": found["problem"]}))
    return items


def _plan_user(candidate, admin: str, url: str) -> dict:
    """What creating the user would meet, found without creating anything. The dry run signs in as the administrator,
    which a user that does not exist yet cannot (#365)."""
    try:
        return database_credentials.plan_user(candidate, admin)
    except ArcadeError as exc:
        if exc.status_code in (401, 403):
            raise SetupRefused(STORAGE_REFUSED, url=url, error=(ADMIN_REFUSED, {"url": url})) from None
        raise SetupRefused(STORAGE_REFUSED, url=url, error=str(exc)) from None
    except ValueError as exc:
        raise SetupRefused(STORAGE_REFUSED, url=url, error=str(exc)) from None


def _create_sentence(found: dict) -> str:
    """What --admin-password-file would create, for a user that does not sign in."""
    # 403 is the answer to a user that exists and has no rights, 401 to one that does not or whose password differs
    # and 404 to one with rights on a database that does not exist yet
    if found["sign_in"] in (403, 404):
        return NO_RIGHTS if found["database_exists"] else CREATE_DB
    return CREATE_USER if found["database_exists"] else CREATE_BOTH


def _test_storage(plan, request, yes, result, before_create=None) -> None:
    """Test the candidate store without saving anything; with --admin-password-file and --yes, create its user first.

    Without --yes the administrator's file only reads what can be read, and the plan says what --yes would create."""
    candidate = plan["candidate"]
    url = database_mode.address(candidate)
    named = {"url": url, "user": candidate.arcade_user, "database": candidate.arcade_database}
    admin = None
    if request.admin_password_file:
        try:
            admin = database_credentials.admin_password(os.path.abspath(os.path.expanduser(request.admin_password_file)))
        except ValueError as exc:
            raise SetupRefused(STORAGE_REFUSED, url=url, error=str(exc)) from None
    if admin is not None and yes:
        if before_create is not None:  # a database or user that appeared since the dry run: nothing is created for it
            found = _plan_user(candidate, admin, url)
            before_create((TEST_STORAGE_OK, {**named, "version": found["version"]}) if found["sign_in"] is None
                          else (_create_sentence(found), named))
        try:
            created = database_credentials.create_user(candidate, admin)
        except (ValueError, ArcadeError) as exc:
            raise SetupRefused(STORAGE_REFUSED, url=url, error=str(exc)) from None
        # database_created tells "the user and the database" from "the user" for the plan check; it is not in a sentence
        result["done"].append((USER_CREATED, {**named, "database_created": created["database_created"]}) if created["created"]
                              else (DATABASE_CREATED if created["database_created"] else USER_EXISTED, named))
    if admin is not None and not yes:
        found = _plan_user(candidate, admin, url)
        if found["sign_in"] is None:
            result["tests"].append((TEST_STORAGE_OK, {**named, "version": found["version"]}))
        else:
            result["tests"].append((TEST_ADMIN_OK, {"url": url, "version": found["version"]}))
            result["tests"].append((_create_sentence(found), named))
        # A user that does not sign in cannot read the store either: no second refusal, no repeated advice.
        result["writer"] = ([(WRITER_AFTER_USER, {"url": url})] if found["sign_in"] is not None
                            else _writer_items(_inspected(candidate, url), url))
        return
    block = database_credentials.doctor_block(candidate)
    if block.get("available") is True and block.get("database_available") is True and not block.get("error"):
        result["tests"].append((TEST_STORAGE_OK, {**named, "version": block.get("version", "")}))
    else:
        if block.get("error"):
            error = block["error"]
        elif block.get("database_status") in (401, 403):
            error = (SIGN_IN_REFUSED, {"advice": database_mode.sign_in_refused(candidate)})
        else:
            error = (QUERY_FAILED, {"status": block.get("database_status")})
        notes = [item for item in result["done"] if item[0] in CREATED]  # the server was changed before the test failed
        raise SetupRefused(STORAGE_REFUSED_NOTED, url=url, error=error, notes=notes)
    result["writer"] = _writer_items(_inspected(candidate, url), url)


def _inspected(candidate, url: str) -> dict:
    """The writer block of the candidate; a store with the mark of an unfinished restore (#362) is refused, with the
    whole sentence that names the way out, instead of being switched to and read as an unknown writer."""
    found = store_writer.inspect(candidate)
    if found.get("restore_unfinished"):
        raise SetupRefused(STORAGE_REFUSED, url=url, error=found["reason"])
    return found


# --- Models ---------------------------------------------------------------------------------------------------------

def _current(settings, role: str) -> tuple[str, str, bool]:
    """(model, address, on another computer) of one role now."""
    row = memory_models.profile(settings, role)
    url = row["base_url"] if row else settings.ollama_url
    return (getattr(settings, memory_models.MODEL_FIELDS[role]), database_mode.shown(url),
            not memory_models.is_loopback(url))


def _move(settings, role: str, kwargs: dict) -> dict:
    model, url, away = _current(settings, role)
    return {"role": role, "kwargs": kwargs, "before": (model, url), "away": away}


def model_moves(settings, request) -> list[dict]:
    """The roles that change, each with the arguments of configure_role (`models set`); [] when none does."""
    enabled = knowledge_mode.enabled(settings)  # an invalid knowledge_agent is refused with its own sentence
    moves = []
    if request.profile == "remote":
        for role, model in (("chat", request.chat_model), ("embedding", request.embedding_model)):
            kwargs = {"provider": request.provider, "model": model, "base_url": request.models_url,
                      "credential_ref": request.credential_ref, "allow_remote": request.allow_remote}
            kwargs = {key: value for key, value in kwargs.items() if value is not None}
            row = {key: value for key, value in (("provider", request.provider), ("base_url", request.models_url),
                                                 ("credential_ref", request.credential_ref)) if value is not None}
            if request.allow_remote:
                row["allow_remote"] = True
            try:
                wanted = memory_models.normalize_memory_model(row, role)
            except ValueError as exc:
                raise SetupRefused(MODEL_REFUSED, role=role, model=model,
                                   url=database_mode.shown(request.models_url), error=str(exc)) from None
            try:
                # A role without a profile runs on the Ollama of ollama_url: asking for that same server and model
                # changes nothing (check_role saves it as no profile too).
                current = memory_models.profile(settings, role) or {
                    "provider": "ollama", "base_url": memory_models.normalize_base_url(settings.ollama_url)}
                rest = [{key: value for key, value in row.items() if key != "base_url"} for row in (current, wanted)]
                same = (rest[0] == rest[1] and memory_models.same_place(current["base_url"], wanted["base_url"])
                        and getattr(settings, memory_models.MODEL_FIELDS[role]) == model)
            except ValueError:
                same = False  # a stored profile that cannot be read is replaced
            if not same or not enabled:  # with the agent off the model is still tested, before it is turned on
                moves.append({**_move(settings, role, kwargs), "same": same})
        return moves
    if not enabled:
        return []  # nothing runs anywhere: the models stay off
    defaults = {role: Settings.__dataclass_fields__[memory_models.MODEL_FIELDS[role]].default
                for role in memory_models.ROLES}
    for role in memory_models.ROLES:
        try:
            row = memory_models.profile(settings, role)
        except ValueError as exc:
            raise SetupRefused(INVALID_ROLE, role=role, error=str(exc)) from None
        if row is None or not memory_models.is_loopback(row["base_url"]):  # the role will use the Ollama of this computer
            try:
                memory_models.local_server(settings)  # an ollama_url on another computer is never written or used (#288)
            except memory_models.RemoteServerRefused as exc:
                raise SetupRefused(VERBATIM, sentence=str(exc)) from None
        if row is not None and not memory_models.is_loopback(row["base_url"]):
            moves.append(_move(settings, role, {"provider": "ollama", "model": defaults[role]}))
    return moves


def _test_models(settings, request, moves, result) -> None:
    for move in moves:
        role = move["role"]
        try:
            checked = asyncio.run(memory_models.check_role(settings, role, **move["kwargs"]))
        except (ValueError, OllamaUnavailable) as exc:
            model, url = move["kwargs"]["model"], database_mode.shown(
                move["kwargs"].get("base_url") or settings.ollama_url)
            # `models set --reasoning-effort none` does not help here: setup plans its move without the option and
            # would check (and save) the role without it again, so the advice would loop.
            error = memory_models.LENGTH_CUT if str(exc) == memory_models.LENGTH_CUT_EFFORT else str(exc)
            if request.profile == "remote":
                raise SetupRefused(MODEL_REFUSED, role=role, model=model, url=url, error=error) from None
            raise SetupRefused(MODELS_HERE_NEEDED, profile=request.profile, role=role, url=url, model=model,
                               error=error) from None
        move["checked"] = checked
        url = database_mode.shown(checked["row"]["base_url"])
        move["after"] = (move["kwargs"]["model"], url)
        result["tests"].append((TEST_MODEL_OK, {"role": role, "model": move["after"][0], "url": url}))


# --- The plan -------------------------------------------------------------------------------------------------------

def _at(model: str, url: str) -> tuple[str, dict]:
    return (MODEL_AT, {"model": model, "url": url})


def _engine_item(settings) -> tuple[str, dict]:
    """The stored engine mode as a sentence; an engine_mode that cannot be used is refused (ValueError), not guessed."""
    return (ENGINE_DOCKER if database_mode.engine_mode(settings) == "docker" else ENGINE_NATIVE, {})


def _plan_lines(settings, request, plan, moves, result) -> None:
    changes, left = result["changes"], result["left_behind"]
    was_connect = database_mode.connect_only(settings)
    if plan and request.profile != "local":
        fields = plan["fields"]
        before = (BEFORE_REMOTE, {"url": database_mode.address(settings), "database": settings.arcade_database,
                                  "user": settings.arcade_user}) if was_connect else (BEFORE_LOCAL, {})
        changes.append((CHANGE_STORAGE_REMOTE, {"before": before, "url": database_mode.shown(fields["arcade_connect_url"]),
                                                "user": fields["arcade_user"], "database": fields["arcade_database"],
                                                "password_file": fields["arcade_password_file"]}))
        if fields["arcade_ca_file"]:
            changes.append((CHANGE_CA, {"ca_file": fields["arcade_ca_file"]}))
        if not was_connect:
            left.append((LEFT_LOCAL_STORE, {"path": settings.data_dir / "arcade"}))
            if settings.arcade_database != derived_database(settings.data_dir):  # a moved directory, another prefix
                left.append((LOCAL_NAME_DIFFERS, {"database": settings.arcade_database,
                                                  "derived": derived_database(settings.data_dir)}))
            if engine.local_process_recorded(settings) or engine_docker.container_recorded(settings):
                left.append((LEFT_ENGINE, {}))
    if plan and request.profile == "local":
        fields = plan["fields"]
        url = database_mode.address(settings)
        changes.append((CHANGE_STORAGE_LOCAL, {"before": url, "url": fields["arcade_url"],
                                               "database": fields["arcade_database"]}))
        if settings.engine_mode != "":
            how = _engine_item(settings)
        elif engine_docker._state(settings).exists():  # choose_engine_mode keeps native for an engine this directory holds
            how = (ENGINE_NATIVE, {})
        else:
            how = (ENGINE_CHOSEN, {})
        changes.append((ENGINE_START, {"engine": how}))
        if plan["target"]:
            changes.append((BACKUP_PLAN, {"url": url, "path": plan["target"]}))
            from cairnmind.terminal.person_proof import restore_proof  # as far as it is known now; checked_restore reads it again
            changes.append((RESTORE_WILL_KEEP if restore_proof(settings) == "key" else RESTORE_WILL_CLAIM, {}))
        else:
            data = settings.data_dir / "arcade" / "databases" / fields["arcade_database"]
            changes.append((LOCAL_DATA_FOUND if data.is_dir() else LOCAL_DATA_NEW,
                            {"database": fields["arcade_database"], "url": url}))
    # Nothing is left behind when the same store is kept, or when the earlier settings had no password and so never
    # reached one.
    unreachable = bool((settings._origin or {}).get("problem")) or not settings.arcade_password
    same_store = bool(plan and request.profile != "local"
                      and database_mode.address(settings) == database_mode.address(plan["candidate"])
                      and settings.arcade_database == plan["fields"]["arcade_database"])
    if plan and was_connect and not same_store and not unreachable:
        left.append((LEFT_REMOTE_STORE, {"url": database_mode.address(settings), "database": settings.arcade_database}))
        if store_writer.inspect(settings).get("role") == "writer":
            left.append((LEFT_WRITER_NOTE, {}))
    sent = False
    for move in moves:
        if not move.get("same"):
            changes.append((CHANGE_MODEL, {"role": move["role"], "before": _at(*move["before"]),
                                           "after": _at(*move["after"])}))
        candidate = move["checked"]["candidate"]
        if not sent and not memory_models.is_loopback(move["checked"]["row"]["base_url"]):
            sent = True
            sends = memory_egress.describe(candidate, move["role"])["sends"]
            host = urlsplit(move["checked"]["row"]["base_url"]).hostname
            changes.append((SENDS, {"host": host, "classes": ", ".join(sends)}) if sends
                           else (SENDS_NO_TEXT, {"host": host}))
        if move["role"] == "embedding" and not move.get("same") and (
                move["before"][0] != move["after"][0]
                or not memory_models.same_place(move["before"][1], move["after"][1])):
            result["rebuild"].append((REBUILD_VECTORS, {"before": _at(*move["before"]), "after": _at(*move["after"])}))
    if moves and not knowledge_mode.enabled(settings):
        changes.append((AGENT_ON, {}))
    for url in dict.fromkeys(move["before"][1] for move in moves if move["away"] and not move.get("same")):
        left.append((LEFT_MODEL_SERVER, {"url": url}))


# --- The change -----------------------------------------------------------------------------------------------------

def _add_next(result, steps) -> None:
    for step in steps:
        if (VERBATIM, {"sentence": step}) not in result["next"]:
            result["next"].append((VERBATIM, {"sentence": step}))


def _back_to_local(settings, plan, result, say, state) -> Settings:
    """Back to the engine on this machine, in an order that cannot lose data: the backup is written and checked while
    config.json is still as it was, then the settings switch, the engine starts, the backup is restored. The remote
    store is only read."""
    from cairnmind.storage.backup_reader import opened_backup
    from cairnmind.storage.restore_claims import checked_restore
    from cairnmind.store import RESTORE_TYPES, Store
    url, target = database_mode.address(settings), plan["target"]
    if target:
        say(render_one((BACKING_UP, {"url": url, "path": target})))
        try:
            store = Store.open_readonly(settings)  # a store that cannot write; the backup is `cairnmind backup`'s
            try:
                written = Path(store.backup(target))
            finally:
                store.client.close()
        except (ArcadeError, ValueError, OSError) as exc:
            raise SetupFailed(BACKUP_FAILED, url=url, error=str(exc)) from None
        state["backup"] = written
        try:
            with opened_backup(written, Store._validated_backup, RESTORE_TYPES):  # what restore checks before it writes
                pass
        except (ValueError, OSError) as exc:
            raise SetupFailed(BACKUP_CHECK_FAILED, path=written, error=str(exc)) from None
        result["done"].append((BACKUP_WRITTEN, {"path": written}))
    state["step"] = (STEP_STORAGE, {})
    save_storage(settings.data_dir, plan["fields"])
    local = Settings.load(settings.data_dir, create=True)  # generates the managed password a connect-only one lacks
    state["step"] = (STEP_ENGINE, {})
    state["arcade_is_new"] = not (settings.data_dir / "arcade").exists()  # what a failed password keep may say to remove
    state["engine_tried"] = True  # from here ArcadeDB may hold the password Settings.load generated
    chosen = engine_docker.choose_engine_mode(local)  # as `init` chooses; nothing is installed
    if chosen:
        say(chosen)
    say(render_one((ENGINE_STARTING, {})))
    engine.start_command(local)  # the stored mode; a missing image is downloaded as `engine start` does, saying so
    state["engine"] = True
    result["done"].append((ENGINE_STARTED, {"engine": _engine_item(local)}))
    if target:
        state["step"] = (STEP_RESTORE, {})
        say(render_one((RESTORING, {"path": written})))
        # The rule of `cairnmind restore` (#214): validation, writer check, safety backup and the workspace's key.
        outcome = checked_restore(local, written, lambda: Store(local.data_dir, settings=local, for_restore=True))
        state["restored"] = (written, local.data_dir / "backups")
        result["done"].append((RESTORED, {"path": written, "backups": local.data_dir / "backups"}))
        notice = outcome.pop("notice", None)
        result["restore"] = outcome
        marks = outcome.get("confirmations_before_rule")
        result["done"].append(notice or (RESTORE_KEPT, {}))
        if outcome.get("vectors_notice"):
            result["done"].append(outcome.pop("vectors_notice"))  # said, as `cairnmind restore` says it (#187)
        if marks:
            result["done"].append((CONFIRMATIONS, {"marked": marks["marked_as_a_persons"],
                                                   "left": marks["still_before_the_rule"]}))
    return local


def _apply(settings, request, plan, moves, result, say, state) -> None:
    local = _back_to_local(settings, plan, result, say, state) if plan and request.profile == "local" else settings
    for move in moves:
        if move.get("same"):
            continue  # already saved so; only the agent is turned on
        state["step"] = (STEP_MODEL, {"role": move["role"]})
        report = asyncio.run(memory_models.configure_role(local, move["role"], **move["kwargs"]))
        _add_next(result, report["next"])
    if request.profile == "remote" and moves:
        state["step"] = (STEP_AGENT, {})
        said = knowledge_mode.turn_on(settings)
        if said:
            result["done"].append((VERBATIM, {"sentence": said}))
    if moves:
        # Decided once for the pair after the saves, also for a role that was not moved again: the release's pair, or
        # one that passed both evaluations, is not labelled, and any other is.
        evaluate = (VERBATIM, {"sentence": memory_models.EVALUATE_STEP})
        if asyncio.run(memory_models.unevaluated(Settings.load(settings.data_dir))):
            if evaluate not in result["next"]:
                result["next"].append(evaluate)
        else:
            result["next"] = [item for item in result["next"] if item != evaluate]
    if plan and request.profile != "local":
        state["step"] = (STEP_STORAGE, {})
        save_storage(settings.data_dir, plan["fields"])
    result["done"].append((SAVED, {"config": settings.data_dir / "config.json"}))
    result["next"].append((NEXT_REMOTE if plan and request.profile != "local" else
                           NEXT_LOCAL_EMPTY if plan and not plan["target"] else NEXT_LOCAL, {}))


def put_back(path: Path, snapshot: bytes, state: dict, notes: list | None = None) -> list:
    """config.json as the snapshot has it, after a change that stopped half way or was applied and then refused. The
    password a started engine uses stays. ``state`` is the change's (engine, engine_tried, arcade_is_new); what is left
    is added to ``notes`` (the list is returned), also when the put-back itself raises an OSError, which propagates."""
    notes = [] if notes is None else notes
    # A started engine, or one whose start failed late, may hold the managed password this change generated.
    kept = _generated_password(path, snapshot) if state["engine_tried"] else None
    _put_back(path, snapshot)
    if kept:
        try:
            _keep_password(path, kept)
            notes.append((KEPT_PASSWORD if state["engine"] else KEPT_PASSWORD_START, {}))
        except (OSError, ValueError) as failure:  # the put-back worked; the original error is still raised by the caller
            notes.append((PASSWORD_NOT_KEPT_NEW if state["arcade_is_new"] else PASSWORD_NOT_KEPT_OLD,
                          {"reason": str(failure)}))
    return notes


def run(settings, request: Request, *, yes: bool, say: Callable[[str], None],
        before_change: Callable[[dict], None] | None = None,
        before_create: Callable[[tuple], None] | None = None) -> dict:
    """Validate, test, plan and (with ``yes``) change. Raises SetupRefused before anything changed, SetupFailed
    after config.json was put back. The result holds (template, values) items; see render. ``before_change`` is
    called with the plan after every test and before anything changes; a SetupRefused it raises stops the run with
    config.json untouched. ``before_create`` is called, before the administrator creates anything, with what the server
    would have created now (the sentence and its values; TEST_STORAGE_OK when the user signs in), and may refuse in the
    same way."""
    result: dict = {"profile": request.profile, "tests": [], "changes": [], "writer": [], "rebuild": [],
                    "left_behind": [], "applied": False, "done": [], "next": [], "topology": None}
    refused = environment_problem(settings)
    if refused:
        raise refused
    plan = _storage_plan(settings, request, result)
    moves = model_moves(settings, request)
    if not plan and not moves:
        result["done"].append((NOTHING_TO_DO, {"profile": request.profile}))
        return result
    _test_models(settings, request, moves, result)  # the models first: they write nothing anywhere
    if plan and request.profile != "local":
        _test_storage(plan, request, yes, result, before_create)
    _plan_lines(settings, request, plan, moves, result)
    if not yes:
        return result
    if before_change is not None:
        before_change(result)
    path = settings.data_dir / "config.json"
    snapshot = path.read_bytes()
    state: dict = {"step": None, "backup": None, "engine": False, "engine_tried": False, "arcade_is_new": False,
                    "restored": None}
    try:
        _apply(settings, request, plan, moves, result, say, state)
        # Not in as_json or render: what put_back needs when the caller refuses the change after it was made (#339).
        result["applied_state"] = {key: state[key] for key in ("engine", "engine_tried", "arcade_is_new")}
        result["topology"] = topology.topology(Settings.load(settings.data_dir))  # inside: a failure here is put back
        result["applied"] = True
    except SetupFailed:  # the backup failed or did not pass its check: config.json was not touched yet
        raise
    except BaseException as exc:  # Ctrl-C too: the configuration is never left half changed
        notes = [item for item in result["done"] if item[0] in CREATED]
        if state["backup"]:
            notes.append((KEPT_BACKUP, {"path": state["backup"]}))
        if state["engine"]:
            notes.append((ENGINE_LEFT, {}))
        if state["restored"]:
            notes.append((RESTORE_DONE, {"path": state["restored"][0], "backups": state["restored"][1]}))
        try:
            put_back(path, snapshot, state, notes)
        except OSError as failure:
            raise SetupFailed(PUT_BACK_FAILED, step=state["step"], error=str(exc), reason=str(failure),
                              notes=notes) from exc
        if not isinstance(exc, Exception):
            say(render_one((PUT_BACK, {"step": state["step"], "error": str(exc) or type(exc).__name__,
                                       "notes": notes})))
            raise
        raise SetupFailed(PUT_BACK, step=state["step"], error=str(exc), notes=notes) from exc
    return result


# --- Output ---------------------------------------------------------------------------------------------------------

def render(result: dict, language: str = "en") -> list[str]:
    """The text the command prints: tests, changes, writer, rebuild, left behind, then NOT_APPLIED or what was done."""
    lines = [render_one(item, language) for key in ("tests", "changes", "writer", "rebuild", "left_behind", "done")
             for item in result[key]]
    if result["applied"]:
        lines += [render_one(item, language) for item in result["next"]]
        lines += ["", *topology.lines(result["topology"], language)]
    elif result["changes"]:
        lines.append(render_one((NOT_APPLIED, {}), language))
    return lines


def as_json(result: dict) -> dict:
    """The result for --json: every sentence in English, and the three topology lines after a change."""
    shown = {key: [render_one(item) for item in result[key]]
             for key in ("tests", "changes", "writer", "rebuild", "left_behind", "done", "next")}
    return {"profile": result["profile"], "applied": result["applied"], **shown, **result.get("restore", {}),
            "topology": (result["topology"] or {}).get("lines", [])}
