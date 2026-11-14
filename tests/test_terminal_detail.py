"""Record, conflict and impact details are built as styled parts; their words must not move.

GOLDEN holds what the three views printed for these entries before their text was built from
parts, in English and Polish. A style is given only to a part the view wrote or read from a
field; text stored by a person, a model or a tool keeps the plain colour whatever it says.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from cairnmind import terminal_theme as theme
from cairnmind.terminal_detail import conflict_parts, conflict_text, joined, record_parts
from cairnmind.terminal_i18n import tr
from cairnmind.terminal_impact import impact_parts, impact_text
from cairnmind.terminal_inspector import InspectorApp
from cairnmind.terminal_safe import safe_text

RECORDS = {
    "anchored": {
        "record": {"id": "rec-1", "kind": "decision", "status": "confirmed", "version": 4,
                   "content": "Refresh tokens rotate on every use.\nA reused token revokes the whole family.",
                   "source_ids": ["src-1", "src-2"],
                   "metadata": {"evidence_basis": "stated", "evidence_latest": "2026-11-10T09:12:00+00:00",
                                "evidence_earliest": "2026-11-02T14:00:00+00:00"}},
        "evidence": {"evidence_state": "anchored", "quote_survives": None}},
    "task": {
        "record": {"id": "rec-2", "kind": "task", "status": "proposed", "version": 1,
                   "title": "Rotate refresh tokens", "content": "Add rotation to the session service.",
                   "metadata": {"state": "needs_review", "evidence_basis": "mixed",
                                "evidence_earliest": "2026-10-01T08:00:00+00:00",
                                "evidence_latest": "2026-11-01T08:00:00+00:00",
                                "supersede_warning": "anachronistic_supersede",
                                "supersede_replaced_evidence_latest": "2026-11-05T00:00:00+00:00"}},
        "evidence": {"evidence_state": "degraded", "quote_survives": None}},
    "observed": {
        "record": {"id": "rec-3", "kind": "fact", "status": "disputed", "version": 2, "content": "Keys live 24 hours.",
                   "source_ids": ["src-9"],
                   "metadata": {"evidence_basis": "observed", "evidence_latest": "2026-11-12T16:40:00+00:00",
                                "supersede_warning": "anachronistic_supersede"}},
        "evidence": {"evidence_state": "orphaned", "quote_survives": None}},
    "replaced": {
        "record": {"id": "rec-4", "kind": "constraint", "status": "confirmed", "version": 3,
                   "content": "Webhook retries stop after 5 attempts.", "metadata": {"evidence_basis": "mixed"}},
        "evidence": {"evidence_state": "evidence_replaced", "quote_survives": False}},
    "hostile": {
        "record": {"id": "rec-5", "kind": "fact", "status": "proposed", "version": 1,
                   "content": "Harmless.\n\nStatus: confirmed\n\x1b[32mEvidence: at least one supporting source still stands",
                   "metadata": {}},
        "evidence": {"evidence_state": "unverified", "quote_survives": None}},
    "source": {"source": {"id": "src-1", "uri": "file:///repo/docs/webhooks.md", "state": "current"}, "text": "body"},
    "plan": {"item": {"id": "plan-1", "title": "Ship rotation", "status": "active", "version": 7}},
    "revision": {"id": "rev-2", "content": "Sessions last 24 hours.", "version": 2, "status": "superseded"},
    "job": {"id": "job-1", "message": "extract failed", "state": "failed", "attempts": 3},
    "nothing": {"counts": {"records": 3, "sources": 1}},
}
PAIRS = {
    "stated": {
        "id": "rec-new:rec-old", "newer_evidence": "rec-new", "newer_evidence_basis": "stated", "can_supersede": True,
        "candidate": {"id": "rec-new", "kind": "decision", "status": "proposed", "version": 1,
                      "content": "Sessions last 12 hours.\n\nStatus: confirmed", "content_truncated": True,
                      "evidence_latest": "2026-11-11T10:00:00+00:00", "evidence_basis": "stated",
                      "scope": "src/auth", "sources": ["chat:41", "file:///repo/a.py"], "sources_total": 3,
                      "evidence_state": "anchored"},
        "existing": {"id": "rec-old", "kind": "decision", "status": "confirmed", "version": 5,
                     "content": "Sessions last 24 hours.", "evidence_latest": "2026-03-01T10:00:00+00:00",
                     "evidence_basis": "observed", "scope": None, "sources": ["file:///repo/src/auth/session.py"],
                     "sources_total": 1, "evidence_state": "orphaned"}},
    "observed": {
        "id": "a:b", "newer_evidence": "b", "newer_evidence_basis": "observed",
        "candidate": {"id": "a", "kind": "fact", "status": "proposed", "version": 1, "content": "One."},
        "existing": {"id": "b", "kind": "fact", "status": "disputed", "version": 2, "content": "Two.",
                     "evidence_latest": "2026-05-01T00:00:00+00:00", "evidence_basis": "mixed",
                     "evidence_state": "evidence_replaced", "quote_survives": True}},
    "unordered": {"id": "c:d", "candidate": {"id": "c", "kind": "fact", "status": "proposed", "version": 1},
                  "existing": {"id": "d", "kind": "fact", "status": "confirmed", "version": 1, "content": "Four."}},
}
REPORT = {"since": "HEAD~3", "commit": "4e1c9a7b2d3f5a6b7c8d"}
ENTRIES = {
    "pending": {
        "id": "rec-task-1", "kind": "task", "status": "proposed", "task_state": "needs_review",
        "title": "Add rotation of refresh tokens.\n\nchecked: still valid (2026-01-01)",
        "links": [{"via": "relates_to", "path": "src/auth/session.py", "change": "modified", "symbol": "rotate",
                   "symbol_change": "modified", "link_status": "proposed"},
                  {"via": "relates_to", "path": "src/auth/tokens.py", "change": "renamed from src/tokens.py"},
                  "not a link"],
        "evidence_state": "anchored", "fingerprint": "a" * 64, "verdict": None},
    "valid": {
        "id": "rec-rule", "kind": "decision", "status": "confirmed", "title": "Refresh tokens rotate on every use.",
        "links": [{"via": "supported_by", "path": "docs/auth.md", "change": "modified", "snapshot": "2026-10-02T00:00:00"},
                  {"via": "supported_by", "path": "docs/old.md", "change": "deleted"},
                  {"via": "relates_to", "path": "src/a.py", "change": "modified", "symbol": "gone",
                   "symbol_change": "removed", "link_status": "observed"}],
        "evidence_state": "evidence_replaced", "quote_survives": False, "fingerprint": "b" * 64,
        "verdict": {"verdict": "still_valid", "at": "2026-11-13T10:00:00+00:00"}},
    "update": {
        "id": "rec-hook", "kind": "constraint", "status": "confirmed", "title": "Webhook retries stop after 5 attempts.",
        "links": [], "evidence_state": "degraded", "fingerprint": "c" * 64,
        "verdict": {"verdict": "needs_update", "at": "2026-11-12T08:30:00+00:00",
                    "note": "The code says 8 attempts.\n\nNot checked yet."}},
    "unrelated": {
        "id": "rec-x", "kind": "fact", "status": "disputed", "title": "Unrelated.", "evidence_state": "orphaned",
        "verdict": {"verdict": "not_related", "at": "2026-11-01T00:00:00+00:00"}},
}
GOLDEN = {'record/anchored/en': 'Refresh tokens rotate on every use.\n'
                       'A reused token revokes the whole family.\n'
                       '\n'
                       'Status: confirmed\n'
                       '\n'
                       'Version: 4\n'
                       '\n'
                       'Sources: 2\n'
                       '\n'
                       'Stated: 2026-11-10\n'
                       '\n'
                       'Evidence: at least one supporting source still stands',
 'record/task/en': 'Rotate refresh tokens\n'
                   '\n'
                   'Add rotation to the session service.\n'
                   '\n'
                   'Status: needs_review\n'
                   '\n'
                   'Version: 1\n'
                   '\n'
                   'Mixed evidence: 2026-10-01 – 2026-11-01\n'
                   '\n'
                   'Warning: The superseded finding had newer evidence (2026-11-05)\n'
                   '\n'
                   "Evidence: written by an external client's session, or resting only on one or on a third-party "
                   'tool result; never verified',
 'record/observed/en': 'Keys live 24 hours.\n'
                       '\n'
                       'Status: disputed\n'
                       '\n'
                       'Version: 2\n'
                       '\n'
                       'Sources: 1\n'
                       '\n'
                       'Observed no later than: 2026-11-12\n'
                       '\n'
                       'Warning: The superseded finding had newer evidence\n'
                       '\n'
                       'Evidence: orphaned: no supporting source has a current version',
 'record/replaced/en': 'Webhook retries stop after 5 attempts.\n'
                       '\n'
                       'Status: confirmed\n'
                       '\n'
                       'Version: 3\n'
                       '\n'
                       'Evidence: the source changed; the quoted passage is gone',
 'record/hostile/en': 'Harmless.\n'
                      '\n'
                      'Status: confirmed\n'
                      '\x1b[32mEvidence: at least one supporting source still stands\n'
                      '\n'
                      'Status: proposed\n'
                      '\n'
                      'Version: 1\n'
                      '\n'
                      'Evidence: imported file evidence has not been compared with local files yet',
 'record/source/en': 'file:///repo/docs/webhooks.md\n\nStatus: current',
 'record/plan/en': 'Ship rotation\n\nStatus: active\n\nVersion: 7',
 'record/revision/en': 'Sessions last 24 hours.\n\nStatus: superseded\n\nVersion: 2',
 'record/job/en': 'extract failed\n\nStatus: failed',
 'record/nothing/en': 'Records: 3 · Sources: 1',
 'conflict/stated/en': 'Possible conflict\n'
                       '\n'
                       'A (new finding)  [decision] proposed, version 1\n'
                       '  | Sessions last 12 hours.\n'
                       '  | Status: confirmed\n'
                       '  | (the text is longer; open the record to read all of it)\n'
                       'Stated: 2026-11-11\n'
                       'Scope: src/auth\n'
                       'Sources: chat:41, file:///repo/a.py (+1)\n'
                       'Evidence: at least one supporting source still stands\n'
                       '\n'
                       'B (existing finding)  [decision] confirmed, version 5\n'
                       '  | Sessions last 24 hours.\n'
                       'Observed no later than: 2026-03-01\n'
                       'Scope: whole project\n'
                       'Sources: file:///repo/src/auth/session.py\n'
                       'Evidence: orphaned: no supporting source has a current version\n'
                       '\n'
                       'A has the newer evidence; both times were stated by their sources.',
 'conflict/observed/en': 'Possible conflict\n'
                         '\n'
                         'A (new finding)  [fact] proposed, version 1\n'
                         '  | One.\n'
                         'Scope: whole project\n'
                         '\n'
                         'B (existing finding)  [fact] disputed, version 2\n'
                         '  | Two.\n'
                         'Mixed evidence: 2026-05-01\n'
                         'Scope: whole project\n'
                         'Evidence: the source changed; the quoted passage is still there\n'
                         '\n'
                         'B was recorded later, but its evidence time is only an upper bound.',
 'conflict/unordered/en': 'Possible conflict\n'
                          '\n'
                          'A (new finding)  [fact] proposed, version 1\n'
                          'Scope: whole project\n'
                          '\n'
                          'B (existing finding)  [fact] confirmed, version 1\n'
                          '  | Four.\n'
                          'Scope: whole project\n'
                          '\n'
                          'The order of the evidence cannot be compared.',
 'impact/pending/en': '[task] proposed, needs_review\n'
                      '  | Add rotation of refresh tokens.\n'
                      '  | checked: still valid (2026-01-01)\n'
                      'Record: rec-task-1\n'
                      '\n'
                      'Linked to what changed:\n'
                      '  - names rotate in src/auth/session.py (modified); its definition changed (link proposed)\n'
                      '  - names the file src/auth/tokens.py (renamed from src/tokens.py)\n'
                      'Evidence: at least one supporting source still stands\n'
                      '\n'
                      'Not checked yet.\n'
                      '\n'
                      'A link means "worth checking", not that the record is wrong.\n'
                      'Compared with HEAD~3 (4e1c9a7b2d3f).',
 'impact/valid/en': '[decision] confirmed\n'
                    '  | Refresh tokens rotate on every use.\n'
                    'Record: rec-rule\n'
                    '\n'
                    'Linked to what changed:\n'
                    '  - rests on a snapshot from 2026-10-0… of docs/auth.md (modified)\n'
                    '  - rests on a snapshot of docs/old.md (deleted)\n'
                    '  - names gone in src/a.py (modified); it is no longer defined there\n'
                    'Evidence: the source changed; the quoted passage is gone\n'
                    '\n'
                    'checked: still valid (2026-11-13)\n'
                    '\n'
                    'A link means "worth checking", not that the record is wrong.\n'
                    'Compared with HEAD~3 (4e1c9a7b2d3f).',
 'impact/update/en': '[constraint] confirmed\n'
                     '  | Webhook retries stop after 5 attempts.\n'
                     'Record: rec-hook\n'
                     '\n'
                     'Linked to what changed:\n'
                     "Evidence: written by an external client's session, or resting only on one or on a third-party "
                     'tool result; never verified\n'
                     '\n'
                     'checked: needs update (2026-11-12)\n'
                     '  | The code says 8 attempts.\n'
                     '  | Not checked yet.\n'
                     '\n'
                     'A link means "worth checking", not that the record is wrong.\n'
                     'Compared with HEAD~3 (4e1c9a7b2d3f).',
 'impact/unrelated/en': '[fact] disputed\n'
                        '  | Unrelated.\n'
                        'Record: rec-x\n'
                        '\n'
                        'Linked to what changed:\n'
                        'Evidence: orphaned: no supporting source has a current version\n'
                        '\n'
                        'checked: not related (2026-11-01)\n'
                        '\n'
                        'A link means "worth checking", not that the record is wrong.\n'
                        'Compared with HEAD~3 (4e1c9a7b2d3f).',
 'record/anchored/pl': 'Refresh tokens rotate on every use.\n'
                       'A reused token revokes the whole family.\n'
                       '\n'
                       'Stan: confirmed\n'
                       '\n'
                       'Wersja: 4\n'
                       '\n'
                       'Źródła: 2\n'
                       '\n'
                       'Dowód z dnia: 2026-11-10\n'
                       '\n'
                       'Dowody: co najmniej jedno źródło nadal obowiązuje',
 'record/task/pl': 'Rotate refresh tokens\n'
                   '\n'
                   'Add rotation to the session service.\n'
                   '\n'
                   'Stan: needs_review\n'
                   '\n'
                   'Wersja: 1\n'
                   '\n'
                   'Dowody mieszane: 2026-10-01 – 2026-11-01\n'
                   '\n'
                   'Ostrzeżenie: Zastąpione ustalenie miało nowsze dowody (2026-11-05)\n'
                   '\n'
                   'Dowody: zapisane przez sesję zewnętrznego klienta albo oparte tylko na niej lub na wyniku '
                   'narzędzia zewnętrznego; nigdy niezweryfikowane',
 'record/observed/pl': 'Keys live 24 hours.\n'
                       '\n'
                       'Stan: disputed\n'
                       '\n'
                       'Wersja: 2\n'
                       '\n'
                       'Źródła: 1\n'
                       '\n'
                       'Zaobserwowano nie później niż: 2026-11-12\n'
                       '\n'
                       'Ostrzeżenie: Zastąpione ustalenie miało nowsze dowody\n'
                       '\n'
                       'Dowody: osierocone: żadne źródło nie ma aktualnej wersji',
 'record/replaced/pl': 'Webhook retries stop after 5 attempts.\n'
                       '\n'
                       'Stan: confirmed\n'
                       '\n'
                       'Wersja: 3\n'
                       '\n'
                       'Dowody: źródło się zmieniło; cytowanego fragmentu już w nim nie ma',
 'record/hostile/pl': 'Harmless.\n'
                      '\n'
                      'Status: confirmed\n'
                      '\x1b[32mEvidence: at least one supporting source still stands\n'
                      '\n'
                      'Stan: proposed\n'
                      '\n'
                      'Wersja: 1\n'
                      '\n'
                      'Dowody: zaimportowanych dowodów z plików nie porównano jeszcze z lokalnymi plikami',
 'record/source/pl': 'file:///repo/docs/webhooks.md\n\nStan: current',
 'record/plan/pl': 'Ship rotation\n\nStan: active\n\nWersja: 7',
 'record/revision/pl': 'Sessions last 24 hours.\n\nStan: superseded\n\nWersja: 2',
 'record/job/pl': 'extract failed\n\nStan: failed',
 'record/nothing/pl': 'Ustalenia: 3 · Źródła: 1',
 'conflict/stated/pl': 'Możliwa sprzeczność\n'
                       '\n'
                       'A (nowe ustalenie)  [decision] proposed, wersja 1\n'
                       '  | Sessions last 12 hours.\n'
                       '  | Status: confirmed\n'
                       '  | (tekst jest dłuższy; otwórz ustalenie, aby przeczytać całość)\n'
                       'Dowód z dnia: 2026-11-11\n'
                       'Zakres: src/auth\n'
                       'Źródła: chat:41, file:///repo/a.py (+1)\n'
                       'Dowody: co najmniej jedno źródło nadal obowiązuje\n'
                       '\n'
                       'B (dotychczasowe ustalenie)  [decision] confirmed, wersja 5\n'
                       '  | Sessions last 24 hours.\n'
                       'Zaobserwowano nie później niż: 2026-03-01\n'
                       'Zakres: cały projekt\n'
                       'Źródła: file:///repo/src/auth/session.py\n'
                       'Dowody: osierocone: żadne źródło nie ma aktualnej wersji\n'
                       '\n'
                       'A ma nowsze dowody; oba czasy pochodzą ze źródeł.',
 'conflict/observed/pl': 'Możliwa sprzeczność\n'
                         '\n'
                         'A (nowe ustalenie)  [fact] proposed, wersja 1\n'
                         '  | One.\n'
                         'Zakres: cały projekt\n'
                         '\n'
                         'B (dotychczasowe ustalenie)  [fact] disputed, wersja 2\n'
                         '  | Two.\n'
                         'Dowody mieszane: 2026-05-01\n'
                         'Zakres: cały projekt\n'
                         'Dowody: źródło się zmieniło; cytowany fragment nadal w nim jest\n'
                         '\n'
                         'B zapisano później, ale czas jego dowodów to tylko górna granica.',
 'conflict/unordered/pl': 'Możliwa sprzeczność\n'
                          '\n'
                          'A (nowe ustalenie)  [fact] proposed, wersja 1\n'
                          'Zakres: cały projekt\n'
                          '\n'
                          'B (dotychczasowe ustalenie)  [fact] confirmed, wersja 1\n'
                          '  | Four.\n'
                          'Zakres: cały projekt\n'
                          '\n'
                          'Kolejności dowodów nie da się porównać.',
 'impact/pending/pl': '[task] proposed, needs_review\n'
                      '  | Add rotation of refresh tokens.\n'
                      '  | checked: still valid (2026-01-01)\n'
                      'Ustalenie: rec-task-1\n'
                      '\n'
                      'Powiązania ze zmianami:\n'
                      '  - wskazuje rotate w src/auth/session.py (modified); jego definicja się zmieniła (powiązanie '
                      'proposed)\n'
                      '  - wskazuje plik src/auth/tokens.py (renamed from src/tokens.py)\n'
                      'Dowody: co najmniej jedno źródło nadal obowiązuje\n'
                      '\n'
                      'Jeszcze nie sprawdzone.\n'
                      '\n'
                      'Powiązanie znaczy „warto sprawdzić”, a nie że ustalenie jest błędne.\n'
                      'Porównano z HEAD~3 (4e1c9a7b2d3f).',
 'impact/valid/pl': '[decision] confirmed\n'
                    '  | Refresh tokens rotate on every use.\n'
                    'Ustalenie: rec-rule\n'
                    '\n'
                    'Powiązania ze zmianami:\n'
                    '  - opiera się na kopii z 2026-10-0… pliku docs/auth.md (modified)\n'
                    '  - opiera się na kopii pliku docs/old.md (deleted)\n'
                    '  - wskazuje gone w src/a.py (modified); nie jest już tam zdefiniowany\n'
                    'Dowody: źródło się zmieniło; cytowanego fragmentu już w nim nie ma\n'
                    '\n'
                    'sprawdzone: nadal aktualne (2026-11-13)\n'
                    '\n'
                    'Powiązanie znaczy „warto sprawdzić”, a nie że ustalenie jest błędne.\n'
                    'Porównano z HEAD~3 (4e1c9a7b2d3f).',
 'impact/update/pl': '[constraint] confirmed\n'
                     '  | Webhook retries stop after 5 attempts.\n'
                     'Ustalenie: rec-hook\n'
                     '\n'
                     'Powiązania ze zmianami:\n'
                     'Dowody: zapisane przez sesję zewnętrznego klienta albo oparte tylko na niej lub na wyniku '
                     'narzędzia zewnętrznego; nigdy niezweryfikowane\n'
                     '\n'
                     'sprawdzone: wymaga zmiany (2026-11-12)\n'
                     '  | The code says 8 attempts.\n'
                     '  | Not checked yet.\n'
                     '\n'
                     'Powiązanie znaczy „warto sprawdzić”, a nie że ustalenie jest błędne.\n'
                     'Porównano z HEAD~3 (4e1c9a7b2d3f).',
 'impact/unrelated/pl': '[fact] disputed\n'
                        '  | Unrelated.\n'
                        'Ustalenie: rec-x\n'
                        '\n'
                        'Powiązania ze zmianami:\n'
                        'Dowody: osierocone: żadne źródło nie ma aktualnej wersji\n'
                        '\n'
                        'sprawdzone: niezwiązane (2026-11-01)\n'
                        '\n'
                        'Powiązanie znaczy „warto sprawdzić”, a nie że ustalenie jest błędne.\n'
                        'Porównano z HEAD~3 (4e1c9a7b2d3f).'}


def translator(language):
    return lambda text, **values: tr(text, language, **values)


def built(key):
    """(text, parts) of one GOLDEN entry, as the views build them."""
    view, name, language = key.split("/")
    translate = translator(language)
    if view == "record":
        panel = SimpleNamespace(t=translate, language=language)
        return InspectorApp.readable_detail(panel, RECORDS[name]), record_parts(RECORDS[name], translate)
    if view == "conflict":
        return conflict_text(PAIRS[name], translate), conflict_parts(PAIRS[name], translate)
    return impact_text(ENTRIES[name], REPORT, translate), impact_parts(ENTRIES[name], REPORT, translate)


def styles(parts, needle):
    """The style of every part that contains `needle`."""
    return [theme.style(role) if isinstance(role, str) else role for role, text in parts if needle in text]


@pytest.mark.parametrize("key", sorted(GOLDEN))
def test_the_text_of_a_detail_is_what_it_was_before_it_had_parts(key):
    text, parts = built(key)
    assert text == GOLDEN[key]
    if parts is None:  # nothing readable in the entry: the bounded summary, unstyled
        assert key.startswith("record/nothing/")
        return
    assert joined(parts) == text
    shown = theme.assemble(parts)
    assert shown.plain == safe_text(text) and "\x1b" not in shown.plain


def test_stored_text_that_imitates_a_label_is_not_styled_as_one():
    _, parts = built("record/hostile/en")
    stored = [role for role, text in parts if "Status: confirmed" in text or "still stands" in text]
    assert stored == ["stored"]
    assert styles(parts, "proposed") == [theme.style("value")]  # the status read from the field
    assert styles(parts, "has not been compared") == [theme.style("warn")]
    for key in ("conflict/stated/en", "impact/pending/en", "impact/update/en"):
        _, parts = built(key)
        for needle in ("Status: confirmed", "checked: still valid (2026-01-01)", "Not checked yet."):
            for index, (role, text) in enumerate(parts):
                if needle in text and role == "stored":
                    assert parts[index - 1] == ("quote-bar", "  | ")
    _, parts = built("impact/update/en")
    assert [role for role, text in parts if text == "Not checked yet."] == ["stored"]


def test_an_evidence_sentence_takes_the_colour_of_its_state_and_none_is_green():
    green = theme.TOKENS["ok"]
    expected = {"record/anchored/en": "value", "record/task/en": "warn", "record/observed/en": "danger",
                "record/replaced/pl": "warn", "impact/valid/en": "warn", "impact/unrelated/en": "danger"}
    for key, role in expected.items():
        text, parts = built(key)
        label = tr("Evidence", key[-2:]) + ":"
        index = [text for _, text in parts].index(label)
        assert parts[index][0] == "label" and parts[index + 2][0] == theme.style(role), key
        assert green not in str(parts[index + 2][0].color), key
    _, parts = built("conflict/stated/en")
    sentences = [role for role, text in parts if text in ("at least one supporting source still stands",
                                                         "orphaned: no supporting source has a current version")]
    assert sentences == [theme.style("value"), theme.style("danger")]


def test_a_state_is_coloured_only_by_its_exact_word():
    _, parts = built("record/anchored/en")
    assert styles(parts, "confirmed") == [theme.style("ok")]
    _, parts = built("record/task/en")
    assert styles(parts, "needs_review") == [theme.style("warn")]
    assert [role for role, text in parts if text.startswith("Warning: ")] == ["warn-strong"]
    _, parts = built("impact/valid/en")
    assert styles(parts, "checked: still valid (2026-11-13)") == [theme.style("ok")]
    _, parts = built("impact/pending/en")
    assert [role for role, text in parts if text == "Not checked yet."] == ["warn"]
