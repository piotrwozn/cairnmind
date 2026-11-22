"""A restore without the person's workspace keeps no person's act of the backup (#214).

`cairnmind restore` keeps the confirmations and accepted record kinds of a backup only when it read the key of
this installation's open workspace (person_proof.restore_proof). Otherwise, in the restore's one transaction
and as each row is inserted:

- a confirmed record, and each confirmed revision of one, whose mark is a person's or that has none (confirmed
  before the rule) gets the mark of an import with package `backup`: confirmed as the backup says, never
  verified here. Status, version, text, sources and evidence stay as the backup has them;
- every accepted transcript record kind of a project moves to its withdrawn ones, with route `restore`.

Everything else (disputed and superseded records, conflict decisions, audit notes) is restored as it is: none of
it is a confirmation.
"""
from __future__ import annotations

from .confirmation import MARK, confirmation_of, import_mark
from .sql import utc_now

PACKAGE = "backup"
ROUTE = "restore"
WITHDRAW_NOTE = "restored without the person's workspace"
RESTORED_WITHOUT_WORKSPACE = (
    "Restored without the person's workspace: no open workspace of this installation answered with its key (it is "
    "closed, the socket there is not this installation's private socket, or tmux could not be run: check "
    "tmux_executable). Nothing in the backup counts as a person's act here: "
    "{confirmations} confirmed record(s) are kept as confirmed only by the backup's claim, not verified here, and "
    "{kinds} accepted transcript record kind(s) were withdrawn. The person confirms again in the inspector of the "
    "workspace what still holds. To keep the person's confirmations instead, open the workspace (`cairnmind`), "
    "stop the service (`cairnmind stop`) and run the same restore again.")
RESTORED_WITH_AN_OLD_WORKSPACE = (
    "Restored without the person's workspace: the open workspace was started by an earlier version and holds no "
    "key for a person's acts. Nothing in the backup counts as a person's act here: {confirmations} confirmed "
    "record(s) are kept as confirmed only by the backup's claim, not verified here, and {kinds} accepted "
    "transcript record kind(s) were withdrawn. The person confirms again in the inspector of the workspace what "
    "still holds. To keep the person's confirmations instead, close the workspace (`cairnmind workspace stop`), "
    "open it again (`cairnmind`), stop the service (`cairnmind stop`) and run the same restore again.")
MESSAGES = {
    RESTORED_WITHOUT_WORKSPACE: (
        "Przywrócono bez środowiska osoby: żadne otwarte środowisko tej instalacji nie odpowiedziało swoim kluczem "
        "(jest zamknięte, gniazdo w tym miejscu nie jest prywatnym gniazdem tej instalacji albo nie udało się "
        "uruchomić tmux: sprawdź tmux_executable). Nic z kopii zapasowej nie liczy się "
        "tutaj jako czynność osoby. Ustalenia, które pozostają potwierdzone tylko według kopii zapasowej i nie są "
        "tutaj zweryfikowane: {confirmations}. Wycofane zaakceptowane rodzaje rekordów transkrypcji: {kinds}. Osoba "
        "ponownie potwierdza w inspektorze środowiska to, co nadal obowiązuje. Aby zamiast tego zachować "
        "potwierdzenia osoby, otwórz środowisko (`cairnmind`), zatrzymaj usługę (`cairnmind stop`) i uruchom to "
        "samo przywracanie jeszcze raz."),
    RESTORED_WITH_AN_OLD_WORKSPACE: (
        "Przywrócono bez środowiska osoby: otwarte środowisko uruchomiła wcześniejsza wersja i nie ma ono klucza dla "
        "czynności osoby. Nic z kopii zapasowej nie liczy się tutaj jako czynność osoby. Ustalenia, które pozostają "
        "potwierdzone tylko według kopii zapasowej i nie są tutaj zweryfikowane: {confirmations}. Wycofane "
        "zaakceptowane rodzaje rekordów transkrypcji: {kinds}. Osoba ponownie potwierdza w inspektorze środowiska "
        "to, co nadal obowiązuje. Aby zamiast tego zachować potwierdzenia osoby, zamknij środowisko (`cairnmind "
        "workspace stop`), otwórz je ponownie (`cairnmind`), zatrzymaj usługę (`cairnmind stop`) i uruchom to samo "
        "przywracanie jeszcze raz."),
}


class BackupClaims:
    """What a restore without the workspace does to each row, and how many it changed."""

    def __init__(self, where: str, at: str | None = None):
        self.where, self.at = where, at or utc_now()
        self.confirmations = self.record_kinds = 0

    def turn(self, collection: str, row: dict) -> dict:
        if collection == "records":
            mark = self._claim(row)
            if mark is None:
                return row
            self.confirmations += 1
            return _with_mark(row, mark)
        if collection == "record_revisions" and isinstance(row.get("snapshot"), dict):
            mark = self._claim(row["snapshot"])
            return row if mark is None else {**row, "snapshot": _with_mark(row["snapshot"], mark)}
        if collection == "projects":
            from .record_kinds import withdraw_all  # record_kinds imports the transcript reader: not at the top
            row, count = withdraw_all(row, self.at, ROUTE, WITHDRAW_NOTE)
            self.record_kinds += count
        return row

    def _claim(self, record):
        found = confirmation_of(record)
        if not found or found["by"] not in ("person", "before_rule"):
            return None
        claims = [] if found["by"] == "before_rule" else [
            {"name": found.get("actor_name"), "action": found.get("route"), "at": found.get("at")}]
        return import_mark(PACKAGE, self.where, claims, self.at, record.get("version"))


def _with_mark(record, mark):
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    return {**record, "metadata": {**metadata, MARK: mark}}


def restore_notice(proof: str, claims: BackupClaims) -> str:
    """The sentence `cairnmind restore` prints on standard error when it kept no person's act."""
    template = RESTORED_WITH_AN_OLD_WORKSPACE if proof == "no_key" else RESTORED_WITHOUT_WORKSPACE
    return template.format(confirmations=claims.confirmations, kinds=claims.record_kinds)
