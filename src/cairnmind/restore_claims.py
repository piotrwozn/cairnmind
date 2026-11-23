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

from pathlib import Path

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
VECTORS_REBUILT = (
    "A backup holds no vectors: {jobs} embedding job(s) are pending again and the service rebuilds the vectors in the "
    "background. Until that ends, search uses the text only, and an answer that needs vectors says so. Progress: "
    "`cairnmind status` (pipeline_jobs.indexing).")
VECTORS_WAIT = (
    "A backup holds no vectors: {jobs} embedding job(s) are pending again, and they wait, as every embedding job does "
    "without models (deterministic-only mode), until models are turned on (`cairnmind models pull`). Until then, search "
    "uses the text only, and an answer that needs vectors says so.")
# knowledge_agent "" (not chosen yet): the service's first start decides, by whether the models are there (knowledge_mode.choose).
VECTORS_UNDECIDED = (
    "A backup holds no vectors: {jobs} embedding job(s) are pending again. When the service starts with the models "
    "available, it rebuilds the vectors in the background (progress: `cairnmind status`, pipeline_jobs.indexing); "
    "when they are missing, it starts in deterministic-only mode and the jobs wait until models are turned on "
    "(`cairnmind models pull`). Until the vectors exist, search uses the text only, and an answer that needs vectors says so.")
MESSAGES = {
    VECTORS_UNDECIDED: (
        "Kopia zapasowa nie zawiera wektorów: zadań osadzania, które znów czekają, jest {jobs}. Gdy usługa startuje z "
        "dostępnymi modelami, odbuduje wektory w tle (postęp: `cairnmind status`, pipeline_jobs.indexing); gdy ich "
        "brakuje, startuje w trybie tylko deterministycznym, a zadania czekają, aż modele zostaną włączone (`cairnmind "
        "models pull`). Do czasu odbudowy wektorów wyszukiwanie korzysta tylko z tekstu, a odpowiedź, która wymaga "
        "wektorów, mówi o tym."),
    VECTORS_REBUILT: (
        "Kopia zapasowa nie zawiera wektorów: zadań osadzania, które znów czekają, jest {jobs}, a usługa odbuduje "
        "wektory w tle. Do tego czasu wyszukiwanie korzysta tylko z tekstu, a odpowiedź, która wymaga wektorów, mówi "
        "o tym. Postęp: `cairnmind status` (pipeline_jobs.indexing)."),
    VECTORS_WAIT: (
        "Kopia zapasowa nie zawiera wektorów: zadań osadzania, które znów czekają, jest {jobs}, i czekają one, jak "
        "każde zadanie osadzania bez modeli (tryb tylko deterministyczny), aż modele zostaną włączone (`cairnmind "
        "models pull`). Do tego czasu wyszukiwanie korzysta tylko z tekstu, a odpowiedź, która wymaga wektorów, mówi "
        "o tym."),
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


def notice_item(proof: str, claims: BackupClaims) -> tuple[str, dict]:
    """The sentence said when a restore kept no person's act, as (template, values) for either language."""
    template = RESTORED_WITH_AN_OLD_WORKSPACE if proof == "no_key" else RESTORED_WITHOUT_WORKSPACE
    return template, {"confirmations": claims.confirmations, "kinds": claims.record_kinds}


def restore_notice(proof: str, claims: BackupClaims) -> str:
    """The sentence `cairnmind restore` prints on standard error when it kept no person's act."""
    template, values = notice_item(proof, claims)
    return template.format(**values)


def vectors_template(settings) -> str:
    """What happens to the pending embedding jobs, by the knowledge agent's setting (the undo of a failed restore says it too)."""
    return {"disabled": VECTORS_WAIT, "enabled": VECTORS_REBUILT}.get(getattr(settings, "knowledge_agent", ""), VECTORS_UNDECIDED)


def checked_restore(settings, source, open_store) -> dict:
    """Every restore a command makes: `cairnmind restore` and the restore step of `cairnmind setup local --backup`.

    The workspace's key is read once, before the store is opened or changed; only "key" keeps a person's
    confirmations, otherwise BackupClaims turns them into the backup's claims. Returns what both commands print:
    `person_confirmations` with its counts, and under "notice" the (template, values) sentence when nothing was kept.
    """
    from .person_proof import restore_proof
    proof = restore_proof(settings)
    claims = None if proof == "key" else BackupClaims(Path(source).name)
    store = open_store()
    store.restore(source, claims=claims)
    # A backup holds no vectors (#187): the embed jobs it brought are pending again, and both commands say so.
    jobs = getattr(store, "embed_jobs_requeued", None)
    rebuilt = {} if not jobs else {
        "embed_jobs_requeued": jobs,
        "vectors_notice": (vectors_template(settings), {"jobs": jobs})}
    if claims is not None:
        return {"person_confirmations": "claims_of_the_backup", "confirmations_turned_into_claims": claims.confirmations,
                "record_kinds_withdrawn": claims.record_kinds, "notice": notice_item(proof, claims), **rebuilt}
    from .confirmation_upgrade import report
    marks = report(getattr(store, "confirmations_before_rule", None))
    return {"person_confirmations": "kept", **({"confirmations_before_rule": marks} if marks else {}), **rebuilt}
