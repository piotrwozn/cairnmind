"""What a person is told when the engine fails during a restore (#362); no engine, no Docker daemon."""
import string

import pytest

from cairnmind import engine_docker, restore_staged
from cairnmind.arcade import ArcadeError
from cairnmind.config import Settings
from cairnmind.restore_staged import MESSAGES
from cairnmind.terminal_i18n import CATALOG

HEAP = ArcadeError("ArcadeDB HTTP 500: Java heap space", status_code=500)
PRODUCTION = ArcadeError("ArcadeDB HTTP 500: Internal error", status_code=500)
TIMED_OUT = ArcadeError("Local ArcadeDB is unavailable (ReadTimeout); run cairnmind engine status")


@pytest.fixture(autouse=True)
def no_docker(monkeypatch):
    def called(*arguments, **keywords):
        raise AssertionError("a message about memory asks Docker nothing")
    monkeypatch.setattr(engine_docker, "run_docker", called)


def _settings(tmp_path, mode):
    if mode == "connect":
        return Settings(data_dir=tmp_path, arcade_connect_url="https://db.example.invalid:2480")
    return Settings(data_dir=tmp_path, engine_mode=mode)


def test_an_engine_that_ran_out_of_memory_is_named_with_what_to_change_native(tmp_path):
    settings = _settings(tmp_path, "native")
    log = tmp_path / "arcade" / "engine.log"
    log.parent.mkdir()
    log.write_text("started\n")
    offset = restore_staged._log_size(settings)
    seen = restore_staged.memory_hint(HEAP, settings, offset)
    assert seen.startswith(restore_staged.MEMORY_SEEN) and "-Xmx1024m" in seen and str(log) in seen
    # In production mode the answer is only "Internal error": the log says what happened, from the restore's start on.
    assert restore_staged.memory_hint(PRODUCTION, settings, offset).startswith(restore_staged.MEMORY_MAYBE)
    with log.open("a") as handle:
        handle.write("java.lang.OutOfMemoryError: Java heap space\n")
    assert restore_staged.memory_hint(TIMED_OUT, settings, offset).startswith(restore_staged.MEMORY_SEEN)
    # What the log held before the restore began is not this restore's.
    assert restore_staged.memory_hint(TIMED_OUT, settings, restore_staged._log_size(settings)).startswith(
        restore_staged.MEMORY_MAYBE)


def test_an_engine_that_ran_out_of_memory_is_named_with_what_to_change_docker(tmp_path):
    settings = _settings(tmp_path, "docker")
    seen = restore_staged.memory_hint(PRODUCTION, settings, restore_staged._log_size(settings))
    assert restore_staged._log_size(settings) is None
    assert seen.startswith(restore_staged.MEMORY_MAYBE) and "docker logs cairnmind-arcadedb-" in seen and "-Xmx1024m" in seen
    assert restore_staged.memory_hint(HEAP, settings, None).startswith(restore_staged.MEMORY_SEEN)


def test_an_engine_that_ran_out_of_memory_is_named_with_what_to_change_connect(tmp_path):
    settings = _settings(tmp_path, "connect")
    seen = restore_staged.memory_hint(PRODUCTION, settings, None)
    assert seen.startswith(restore_staged.MEMORY_MAYBE) and "ARCADEDB_JAVA_OPTS" in seen and "-Xmx4g" in seen
    assert "kubectl -n cairnmind-db logs statefulset/arcadedb" in seen and "-Xmx1024m" not in seen


def test_an_error_that_is_not_the_engines_failing_gets_no_hint(tmp_path):
    settings = _settings(tmp_path, "native")
    assert restore_staged.memory_hint(ArcadeError("ArcadeDB HTTP 400: bad query", status_code=400), settings, 0) is None
    assert restore_staged.memory_hint(ValueError("The backup file changed"), settings, 0) is None
    described = restore_staged._described(OSError("the engine went away"), settings, 0)
    assert described == "the engine went away."
    assert restore_staged._described(HEAP, settings, None).startswith("ArcadeDB HTTP 500: Java heap space. The database engine")


@pytest.mark.parametrize("sentence", [restore_staged.UNFINISHED, restore_staged.UNDO_UNFINISHED, restore_staged.UNREADABLE])
def test_every_refusal_names_the_way_out_from_another_installation(sentence):
    """When the installation that started the restore is gone, any other one finishes it with any valid backup."""
    for text in (sentence, MESSAGES[sentence]):
        assert "cairnmind store takeover --yes" in text and "cairnmind restore " in text
    assert "any valid backup" in sentence and "dowolną poprawną kopią" in MESSAGES[sentence]


def test_every_sentence_of_the_restore_has_its_polish():
    english = [value for name, value in vars(restore_staged).items()
               if name.isupper() and not name.startswith("_") and isinstance(value, str) and " " in value]
    assert len(english) == 16  # MARK_KEPT and MARK_NOT_CLEARED are the ones added after the rechecks; TIME_PER_TRANSACTION and the like are numbers

    def placeholders(sentence):
        return sorted(field for _, field, _, _ in string.Formatter().parse(sentence) if field)

    for text in english:
        assert text in MESSAGES, text[:60]
        assert CATALOG[text] == MESSAGES[text]  # the workspace's catalog has it too
        assert placeholders(MESSAGES[text]) == placeholders(text), text[:60]
    assert set(MESSAGES) == set(english)
