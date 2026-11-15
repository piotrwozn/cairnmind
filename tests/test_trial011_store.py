"""Findings of the 0.11.0 release trial in the store and its upkeep (#238)."""
from __future__ import annotations

import pytest

from cairnmind.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "memory")


@pytest.fixture
def project(store, tmp_path):
    return store.add_project("example", tmp_path / "repo")


@pytest.mark.parametrize("place", ["under a file", "read-only folder"])
def test_a_backup_into_a_folder_that_cannot_be_written_says_so_plainly(tmp_path, place):
    import os
    from cairnmind.store_backup import TEMPORARY_PREFIX, write_temporary
    if place == "under a file":
        (tmp_path / "plain-file").write_text("")
        destination = tmp_path / "plain-file" / "dir" / "x.json.gz"
    else:
        if os.geteuid() == 0:
            pytest.skip("root writes into a read-only folder")
        (tmp_path / "locked").mkdir(mode=0o500)
        destination = tmp_path / "locked" / "x.json.gz"
    with pytest.raises(ValueError) as refused:
        write_temporary(destination, {})
    message = str(refused.value)
    assert message.startswith(f"Cannot write a backup into {destination.parent}: ")
    assert TEMPORARY_PREFIX not in message and "Errno" not in message
    assert not any(path.name.startswith(TEMPORARY_PREFIX) for path in tmp_path.rglob("*"))


@pytest.mark.parametrize("existing", [False, True])
def test_engine_log_is_private_whatever_the_umask(tmp_path, monkeypatch, existing):
    import os
    from cairnmind import engine
    from cairnmind.arcade import ArcadeError
    from test_store_prune import fake_engine
    settings = fake_engine(tmp_path, monkeypatch, engine_log_bytes=1000, engine_log_copies=2)
    log = settings.data_dir / "arcade" / "engine.log"
    if existing:
        log.parent.mkdir(parents=True)
        log.write_bytes(b"o" * 10)
        log.chmod(0o644)
    previous = os.umask(0o022)
    try:
        with pytest.raises(ArcadeError, match="exited during startup"):
            engine.start_engine(settings)
    finally:
        os.umask(previous)
    assert log.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("name,value", [("engine_log_bytes", "2000"), ("engine_log_bytes", 0),
                                        ("engine_log_bytes", -5), ("engine_log_copies", 99),
                                        ("engine_log_copies", "2")])
def test_an_invalid_log_setting_is_reported_where_the_engine_starts(tmp_path, monkeypatch, capsys, name, value):
    from cairnmind import engine
    from cairnmind.arcade import ArcadeError
    from test_store_prune import fake_engine
    settings = fake_engine(tmp_path, monkeypatch, **{name: value})
    assert engine.log_settings_problems(settings) and len(engine.log_settings_problems(settings)) == 1
    with pytest.raises(ArcadeError, match="exited during startup"):
        engine.start_engine(settings)
    said = capsys.readouterr().err
    assert name in said and repr(value) in said and "config.json" in said
    assert name.encode() in (settings.data_dir / "arcade" / "engine.log").read_bytes()


def test_valid_log_settings_report_nothing(tmp_path):
    from cairnmind import engine
    from cairnmind.config import Settings
    assert engine.log_settings_problems(Settings(tmp_path / "data", engine_log_bytes=2000, engine_log_copies=0)) == []


def test_engine_status_of_a_stopped_engine_says_to_start_it(tmp_path):
    import socket
    from cairnmind import engine
    from cairnmind.config import Settings
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    status = engine.engine_status(Settings(tmp_path / "data", arcade_url=f"http://127.0.0.1:{port}",
                                           arcade_database="status_test"))
    assert status["available"] is False
    assert "engine status" not in status["error"]
    assert "cairnmind engine start" in status["error"] and "not running" in status["error"]


def test_store_size_names_an_empty_leftover_vector_type_for_what_it_is(store):
    from cairnmind.store_size import measure
    # What a restore leaves: an emptied vector type whose registry entry the backup did not bring.
    store.client.command("CREATE DOCUMENT TYPE vector_leftover0 IF NOT EXISTS")
    report = measure(store)
    text = report["text"]
    section = text[text.index("Vectors by namespace"):text.index("Jobs by state")]
    assert "(none)" not in section
    assert "unregistered, empty (left by a restore): 1 type(s), 0 rows" in section
    # The JSON keeps its shape: the namespace of an unregistered type is null.
    assert [row["namespace"] for row in report["vectors"]["namespaces"]] == [None]


@pytest.mark.parametrize("error,code", [
    ("Local Ollama /api/embed returned HTTP 400: runtime reported a context-length limit", "model_context_length"),
    ("Ollama /api/chat returned HTTP 500: runtime reported a model runner or loading failure", "model_runtime_error"),
    ("Local Ollama /api/embed returned HTTP 400: runtime rejected the request", "model_runtime_error"),
    ("Local Ollama /api/chat request failed (ReadTimeout): response read timed out", "model_runtime_error"),
    ("Embedding model returned a zero vector", "model_runtime_error"),
    ("something nobody classified", "job_error"),
])
def test_a_model_runtime_failure_of_a_job_has_its_own_category(error, code):
    shown = Store._job_error(error, {})
    assert shown["error_code"] == code
    assert "legacy" not in shown["error_code"]
    if code == "model_context_length":
        assert "context length" in shown["error"]


def source(store, project, text="We decided to keep the queue in the database."):
    return store.snapshot(project["id"], "conversation://one", text, "transcript")


@pytest.mark.parametrize("kind", ["decision", "task"])
def test_a_null_depends_on_is_stored_as_no_dependencies(store, project, kind):
    # A model's metadata may carry depends_on: null; the whole extraction of a message failed on it.
    cited = source(store, project)
    record = store.remember(project["id"], {"kind": kind, "content": "Keep the queue in the database",
                                            "metadata": {"depends_on": None}}, [cited["id"]])
    assert "depends_on" not in record["metadata"] or record["metadata"]["depends_on"] == []
    edges = store.query("SELECT kind FROM edges WHERE from_id=:id", {"id": f"record:{record['id']}"})
    assert [row["kind"] for row in edges] == ["supported_by"]


def test_a_key_held_by_a_person_confirmed_record_gets_the_kept_refusal(store, project):
    from conftest import as_person
    from cairnmind.confirmation import VerdictKept
    cited = source(store, project)
    held = as_person(store, project["id"], {"kind": "decision", "key": "queue", "content": "Queue in the database"},
                     [cited["id"]])
    # A session that writes under that key is told what it can do, not to update by id (a second refusal).
    with pytest.raises(VerdictKept) as refused:
        store.remember(project["id"], {"kind": "decision", "key": "queue", "content": "Queue in Redis"}, [cited["id"]])
    message = str(refused.value)
    assert held["id"] in message and "possible_conflicts" in message and "another key" in message
    assert "expected_version" not in message


def test_a_key_held_by_another_record_names_that_record(store, project):
    cited = source(store, project)
    held = store.remember(project["id"], {"kind": "decision", "key": "queue", "content": "Queue in the database"},
                          [cited["id"]])
    with pytest.raises(ValueError, match="^Active record key already exists") as refused:
        store.remember(project["id"], {"kind": "decision", "key": "queue", "content": "Queue in Redis"}, [cited["id"]])
    assert held["id"] in str(refused.value)


def test_the_command_line_key_clash_names_what_a_person_can_do(tmp_path, monkeypatch):
    from conftest import plain_words
    from cairnmind import confirmation, ingest
    from test_confirmed_by_person import cli
    monkeypatch.setattr(ingest, "register_default_histories", lambda *a: {})
    monkeypatch.setattr(confirmation, "person_at_terminal", lambda *a: True)
    run, _ = cli(tmp_path)
    first = run("remember", "demo", "Queue in the database", "--kind", "decision", "--key", "queue", "--confirmed")
    assert first.exit_code == 0, first.output
    for extra in ([], ["--confirmed"]):
        again = run("remember", "demo", "Queue in Redis", "--kind", "decision", "--key", "queue", *extra)
        words = plain_words(again.output)
        assert again.exit_code != 0
        assert "expected_version" not in words and "--id" not in words
        assert "another key" in words and "inspector" in words
    import json
    assert [row["content"] for row in json.loads(run("records", "demo").output)] == ["Queue in the database"]
