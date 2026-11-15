"""Known secrets replaced in conversation text before it is indexed or extracted (#182), on real ArcadeDB.

Every value here is made up for the test.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from cairnmind.agent_journal import AgentJournal, redact_secrets
from cairnmind.config import Settings
from cairnmind.extraction_evaluation import load_transcripts, transcript_sources
from cairnmind.extraction_input import extraction_request
from cairnmind.ingest import import_history, index_pending_sources, reparse_history_sources
from cairnmind.secret_mask import MASK, Masker, registered_secrets
from cairnmind.store import Store

GITHUB_SHAPE = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # the well-known shape, not a real token
SAVED_KEY = "fixture-saved-provider-key-0123456789"
PROFILE_KEY = "fixture-profile-env-value-9876543210"
MEMORY_MODEL_KEY = "fixture-memory-model-env-value-1357"
EVALUATION_SET = Path(__file__).resolve().parents[1] / "docs" / "evaluation" / "extraction-transcripts.json"
TRANSCRIPT_METADATA = {"client", "role", "session_id", "cwd", "import_path", "byte_offset", "normalized_text",
                       "schema_warning", "source_timestamp", "transcript_parser_revision"}


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    store = Store(tmp_path / "memory")
    project = store.add_project("masking", root)
    return store, project, root


def user_line(root, text, session="s"):
    return json.dumps({"type": "user", "cwd": str(root), "sessionId": session,
                       "message": {"role": "user", "content": text}}) + "\n"


def sources_of(store, project):
    return store.query("SELECT FROM sources WHERE project_id=:p ORDER BY metadata.byte_offset,id", {"p": project["id"]})


def chunks_of(store, source_id):
    return store.query("SELECT FROM chunks WHERE source_id=:s ORDER BY line_start,id", {"s": source_id})


# The helper ------------------------------------------------------------------------------------------

def test_masker_replaces_registered_values_shapes_and_a_truncated_tail_and_leaves_clean_text_alone():
    masker = Masker(["fixture-registered-value-42", "short"])
    text = "Use fixture-registered-value-42 and " + GITHUB_SHAPE + " then stop"
    masked, kinds = masker.mask(text)
    assert masked == f"Use {MASK} and {MASK} then stop"
    assert kinds == ["github_token", "registered_secret"]
    # A value shorter than eight characters is not looked for: "short" is an ordinary word here.
    clean = "A short note about the parser, nothing private."
    assert masker.mask(clean) == (clean, [])
    assert masker.mask(clean)[0] is clean
    # A bounded tool result that ends inside a registered value.
    assert masker.mask("output: fixture-regis")[0] == f"output: {MASK}"
    # Overlapping spans (a bearer header around a registered value) are replaced once.
    bearer = Masker(["Zfixture0registered9value8"])
    assert bearer.mask("Authorization: token Zfixture0registered9value8 ok") == (
        f"{MASK} ok", ["bearer_token", "registered_secret"])


def test_the_journal_keeps_its_own_replacement_through_the_shared_helper():
    secret = "fixture-journal-secret-1"
    assert redact_secrets({"text": f"a {secret} b", "end": "tail fixture-jou", "n": 3}, [secret]) == {
        "text": f"a {MASK} b", "end": f"tail {MASK}", "n": 3}
    # The journal replaces any non-empty value it is given, also short ones, as before.
    assert redact_secrets("pw abc", ["abc"]) == f"pw {MASK}"


def test_registered_secrets_cover_each_kind_the_issue_names(tmp_path, monkeypatch):
    from cairnmind.api_credentials import CredentialStore
    from cairnmind.cockpit_state import CockpitState
    settings = Settings(data_dir=tmp_path, token="fixture-service-token-000000000",
                        arcade_password="fixture-arcade-password-11111",
                        memory_chat={"provider": "openai_compatible", "base_url": "http://127.0.0.1:1234/v1",
                                     "credential_ref": "env:FIXTURE_MEMORY_MODEL"})
    CredentialStore(settings).set("saved-profile", SAVED_KEY, persist=True)
    state = CockpitState(settings)
    state.update({"profiles": [*state.load()["profiles"], {
        "id": "vendor", "client": "api", "label": "Vendor", "provider": "openai_compatible", "model": "m",
        "base_url": "http://127.0.0.1:12345/v1", "credential_ref": "env:FIXTURE_VENDOR_VALUE"}]})
    environ = {"FIXTURE_VENDOR_VALUE": PROFILE_KEY, "FIXTURE_MEMORY_MODEL": MEMORY_MODEL_KEY,
               "OPENAI_API_KEY": "fixture-openai-environment-value", "UNRELATED": "not-a-secret-value"}
    found = set(registered_secrets(settings, environ))
    assert {"fixture-service-token-000000000", "fixture-arcade-password-11111", SAVED_KEY, PROFILE_KEY,
            MEMORY_MODEL_KEY, "fixture-openai-environment-value"} <= found
    assert "not-a-secret-value" not in found


def test_unreadable_saved_profiles_refuse_instead_of_passing_text_through(tmp_path):
    settings = Settings(data_dir=tmp_path, token="fixture-service-token-000000000")
    (tmp_path / "cockpit").mkdir()
    (tmp_path / "cockpit" / "preferences.json").write_text("{\"version\": 99}")
    with pytest.raises(ValueError, match="profiles cannot be read"):
        registered_secrets(settings, {})


# Transcript import -----------------------------------------------------------------------------------

def test_a_transcript_line_with_the_service_token_is_indexed_and_extracted_masked(workspace, tmp_path):
    store, project, root = workspace
    token = store.settings.token
    assert len(token) >= 32
    history = tmp_path / "claude.jsonl"
    secret_line = user_line(root, f"Deploy with the service token {token} and keep the staging hook.")
    clean_line = user_line(root, "Keep the parser strict about CRLF lines.")
    history.write_text(secret_line + clean_line)
    assert import_history(store, history, project["id"], "claude")["archived"] == 2
    secret, clean = sources_of(store, project)
    # The raw archived line is kept as received: its hash and identity do not change.
    assert secret["content"] == secret_line and token in secret["content"]
    metadata = secret["metadata"]
    assert metadata["normalized_text"] == f"Deploy with the service token {MASK} and keep the staging hook."
    assert metadata["masked"] is True and metadata["masked_kinds"] == ["registered_secret"]
    chunks = chunks_of(store, secret["id"])
    assert chunks and all(token not in chunk["text"] for chunk in chunks)
    assert any(MASK in chunk["text"] for chunk in chunks)
    request = extraction_request(secret)
    assert request is not None and token not in json.dumps(request) and MASK in request["text"]
    # A line without a secret is written exactly as before: no new metadata, chunks of its own text.
    assert set(clean["metadata"]) == TRANSCRIPT_METADATA
    assert clean["metadata"]["normalized_text"] == "Keep the parser strict about CRLF lines."
    assert [c["text"] for c in chunks_of(store, clean["id"])] == ["Keep the parser strict about CRLF lines."]


@pytest.mark.parametrize("kind", ["saved_key", "profile_env", "github_shape", "private_key"])
def test_each_kind_of_secret_is_masked_in_a_transcript(workspace, tmp_path, monkeypatch, kind):
    store, project, root = workspace
    from cairnmind.api_credentials import CredentialStore
    from cairnmind.cockpit_state import CockpitState
    if kind == "saved_key":
        CredentialStore(store.settings).set("saved-profile", SAVED_KEY, persist=True)
        value, expected = SAVED_KEY, "registered_secret"
    elif kind == "profile_env":
        state = CockpitState(store.settings)
        state.update({"profiles": [*state.load()["profiles"], {
            "id": "vendor", "client": "api", "label": "Vendor", "provider": "openai_compatible", "model": "m",
            "base_url": "http://127.0.0.1:12345/v1", "credential_ref": "env:FIXTURE_VENDOR_VALUE"}]})
        monkeypatch.setenv("FIXTURE_VENDOR_VALUE", PROFILE_KEY)
        value, expected = PROFILE_KEY, "registered_secret"
    elif kind == "github_shape":
        value, expected = GITHUB_SHAPE, "github_token"
    else:
        value = "-----BEGIN PRIVATE KEY-----\nMIIfixtureNOTAREALKEY\n-----END PRIVATE KEY-----"
        expected = "private_key"
    history = tmp_path / "one.jsonl"
    history.write_text(user_line(root, f"Here it is:\n{value}\nUse it for the release job."))
    import_history(store, history, project["id"], "claude")
    (source,) = sources_of(store, project)
    assert value in json.loads(source["content"])["message"]["content"]
    assert value not in source["metadata"]["normalized_text"]
    assert source["metadata"]["masked_kinds"] == [expected]
    assert all(value not in chunk["text"] and "MIIfixture" not in chunk["text"]
               for chunk in chunks_of(store, source["id"]))


def test_a_metadata_record_chunked_from_its_raw_line_is_masked_and_still_reparses(workspace, tmp_path):
    store, project, root = workspace
    token = store.settings.token
    history = tmp_path / "title.jsonl"
    history.write_text(json.dumps({"type": "ai-title", "sessionId": "s", "aiTitle": f"Rotate {token}"}) + "\n")
    import_history(store, history, project["id"], "claude")
    (source,) = sources_of(store, project)
    assert source["kind"] == "transcript" and source["metadata"]["normalized_text"] == ""
    assert source["metadata"]["masked"] is True and token in source["content"]
    assert all(token not in chunk["text"] for chunk in chunks_of(store, source["id"]))
    # As an earlier parser archived it: an unknown record. Its reparse checks the chunks against the same
    # masked view, so the record is repaired instead of reported as a mismatch.
    from cairnmind.ingest import UNKNOWN_TRANSCRIPT_WARNING
    store.command("UPDATE sources SET kind='transcript_unknown',metadata=:m WHERE id=:id", {"id": source["id"], "m": {
        **source["metadata"], "role": "unknown", "schema_warning": UNKNOWN_TRANSCRIPT_WARNING}})
    result = reparse_history_sources(store, history, project["id"])
    assert result["errors"] == [] and result["repaired"] == 1


def test_a_reimport_finds_the_same_sources_and_chunks_and_writes_nothing_new(workspace, tmp_path):
    store, project, root = workspace
    history = tmp_path / "claude.jsonl"
    history.write_text(user_line(root, f"service token {store.settings.token} for the hook")
                       + user_line(root, "A plain decision: keep CRLF."))
    import_history(store, history, project["id"], "claude")
    before = ([(s["id"], s["sha256"]) for s in sources_of(store, project)],
              sorted(c["id"] for c in store.query("SELECT id FROM chunks")),
              sorted(j["id"] for j in store.query("SELECT id FROM jobs")))
    # Forget the cursor, so every line is read and written again.
    store.command("DELETE FROM import_cursors")
    assert import_history(store, history, project["id"], "claude")["chunks"] == 0
    after = ([(s["id"], s["sha256"]) for s in sources_of(store, project)],
             sorted(c["id"] for c in store.query("SELECT id FROM chunks")),
             sorted(j["id"] for j in store.query("SELECT id FROM jobs")))
    assert after == before


# Evidence ----------------------------------------------------------------------------------------------

class QuotingModel:
    """A fake extraction model that quotes the visible text it was given."""

    def __init__(self):
        self.calls = []

    async def chat(self, messages, tools=None, **kwargs):
        self.calls.append(json.dumps(messages))
        text = json.loads(messages[-1]["content"])["text"]
        quote = kwargs["format"]["properties"]["records"]["items"]["properties"]["evidence"]["enum"][0]
        assert quote in text
        return {"message": {"role": "assistant", "content": json.dumps({"records": [
            {"kind": "decision", "key": "staging-hook", "content": "Keep the staging hook for deploys.",
             "evidence": quote, "metadata": {}}]})}, "done_reason": "stop", "prompt_eval_count": 1, "eval_count": 1}


async def test_an_extracted_record_quotes_the_masked_view_and_its_evidence_verifies(workspace, tmp_path):
    from cairnmind.agent import KnowledgeAgent
    from cairnmind.evidence import evidence_states
    store, project, root = workspace
    token = store.settings.token
    history = tmp_path / "claude.jsonl"
    history.write_text(user_line(root, f"Deploy with {token}. Decision: keep the staging hook for deploys."))
    import_history(store, history, project["id"], "claude")
    job = store.claim_job("mask-test", lease_seconds=120, kinds=["extract"])
    model = QuotingModel()
    await KnowledgeAgent(store, Settings(data_dir=store.data_dir), model).extract(job)
    assert model.calls and all(token not in call for call in model.calls)
    (record,) = [r for r in store.list_records(project["id"]) if r["key"] == "staging-hook"]
    assert token not in json.dumps(record) and MASK in record["metadata"]["evidence"]
    state = evidence_states(store, project["id"], [record])[record["id"]]
    assert state["evidence_state"] == "anchored"


# The agent archive and remember notes ------------------------------------------------------------------

def test_an_archived_agent_event_is_indexed_masked(workspace):
    from cairnmind.agent_memory import archive_agent_target, capture_agent_target
    from cairnmind.sessions import SessionManager
    store, project, root = workspace
    subprocess_git(root)
    manager = SessionManager(store, store.settings)
    owner = manager._session(manager.prepare(project["id"], "api", timeout=0)["session_id"])
    journal = AgentJournal(store.settings, owner["id"])
    journal.append("message", {"role": "user", "content": f"The deploy key is {GITHUB_SHAPE}, keep it out."})
    target = capture_agent_target(store.settings, owner)
    result = archive_agent_target(store, store.settings, owner, target, time.monotonic() + 30)
    assert result["status"] == "complete"
    (source,) = [store.get_source(sid) for sid in result["source_ids"]]
    assert GITHUB_SHAPE in source["content"]  # the archived public event, as the journal wrote it
    assert source["metadata"]["normalized_text"] == f"The deploy key is {MASK}, keep it out."
    assert source["metadata"]["masked"] is True
    assert all(GITHUB_SHAPE not in chunk["text"] for chunk in chunks_of(store, source["id"]))


def subprocess_git(root):
    import subprocess
    for arguments in (("init", "-q", "-b", "main"), ("config", "user.email", "mask@example.invalid"),
                      ("config", "user.name", "Mask Fixture")):
        subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)
    (root / "app.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "initial"], check=True, capture_output=True)


def test_a_remember_note_is_indexed_masked_and_its_record_is_kept_as_written(workspace):
    from cairnmind.remember_input import external_report_source
    store, project, _ = workspace
    token = store.settings.token
    note = {"kind": "fact", "content": f"The hook uses {token} to authenticate."}
    source = store.get_source(external_report_source(store, project["id"], note))
    assert source["content"] == note["content"]
    assert source["metadata"]["normalized_text"] == f"The hook uses {MASK} to authenticate."
    assert source["metadata"]["masked"] is True
    index_pending_sources(store)
    chunks = chunks_of(store, source["id"])
    assert chunks and all(token not in chunk["text"] for chunk in chunks)
    plain = store.get_source(external_report_source(store, project["id"], {"kind": "fact", "content": "No secret."}))
    assert "masked" not in plain["metadata"] and "normalized_text" not in plain["metadata"]


def test_a_cli_remember_note_is_indexed_masked(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from cairnmind import ingest
    from cairnmind.cli import app
    monkeypatch.setattr(ingest, "register_default_histories", lambda *a: {})
    data, root = tmp_path / "memory", tmp_path / "repo"
    root.mkdir()
    runner = CliRunner()
    for arguments in (("init",), ("project", "add", "demo", str(root))):
        assert runner.invoke(app, ["--data-dir", str(data), *arguments]).exit_code == 0
    store = Store(data)
    token = store.settings.token
    result = runner.invoke(app, ["--data-dir", str(data), "remember", "demo", f"The hook token is {token}"])
    assert result.exit_code == 0, result.output
    (source,) = store.query("SELECT FROM sources WHERE kind='manual'")
    assert source["metadata"]["normalized_text"] == f"The hook token is {MASK}"
    index_pending_sources(store)
    assert all(token not in chunk["text"] for chunk in chunks_of(store, source["id"]))


# The extraction evaluation set ------------------------------------------------------------------------

def test_the_extraction_evaluation_set_is_unchanged_by_masking(tmp_path):
    settings = Settings(data_dir=tmp_path, token="fixture-service-token-000000000",
                        arcade_password="fixture-arcade-password-11111")
    masker = Masker(registered_secrets(settings, {}))
    count = 0
    for transcript in load_transcripts(EVALUATION_SET):
        # The sources the evaluation builds, masked as ingestion masks them, are those it built before.
        assert transcript_sources(transcript, masker) == transcript_sources(transcript)
        for item in transcript_sources(transcript, masker):
            text = item["source"]["metadata"]["normalized_text"]
            assert masker.mask(text) == (text, []), item["source"]["id"]
            assert masker.mask(item["source"]["content"])[1] == [], item["source"]["id"]
            assert "masked" not in item["source"]["metadata"]
            count += 1
    assert count > 20


def test_a_text_block_beside_a_tool_call_is_given_to_extraction_masked(workspace, tmp_path):
    store, project, root = workspace
    history = tmp_path / "assistant.jsonl"
    history.write_text(json.dumps({"type": "assistant", "cwd": str(root), "sessionId": "s", "message": {
        "role": "assistant", "content": [
            {"type": "text", "text": f"We decided to rotate {GITHUB_SHAPE} every month."},
            {"type": "tool_use", "name": "Read", "input": {"path": "app.py"}}]}}) + "\n")
    import_history(store, history, project["id"], "claude")
    (source,) = sources_of(store, project)
    request = extraction_request(source, Masker().mask)
    assert request["segments"] == [f"We decided to rotate {MASK} every month."]
    assert GITHUB_SHAPE not in json.dumps(request)
    # Without the masker the raw block is not part of the view: it is left out, never shown.
    assert extraction_request(source) is None


# Review of the first version --------------------------------------------------------------------------

async def test_a_metadata_line_is_read_masked_everywhere_its_readable_view_is_used(workspace, tmp_path):
    from cairnmind.agent import KnowledgeAgent
    from cairnmind.extraction_input import source_text
    from cairnmind.ingest import _chunks
    store, project, root = workspace
    token = store.settings.token
    history = tmp_path / "title.jsonl"
    history.write_text(json.dumps({"type": "ai-title", "sessionId": "s", "cwd": str(root),
                                   "aiTitle": f"Rotate {token}"}) + "\n")
    import_history(store, history, project["id"], "claude")
    (source,) = sources_of(store, project)
    # normalized_text stays empty (the line is not extracted); the masked raw view is kept beside it.
    assert source["metadata"]["normalized_text"] == "" and MASK in source["metadata"]["indexed_text"]
    assert token in source["content"] and token not in source_text(source)
    chunks = chunks_of(store, source["id"])
    # Every chunk is a passage of the readable view, so a search hit of this line can be cited, and the
    # chunks a memory snapshot import rebuilds from that view are the masked ones written here.
    assert chunks and all(chunk["text"] in source_text(source) for chunk in chunks)
    assert [text for text, _, _ in _chunks(source_text(source))] == [chunk["text"] for chunk in chunks]
    agent = KnowledgeAgent(store, Settings(data_dir=store.data_dir), None)
    shown = await agent._tool("read_source", {"source_id": source["id"], "current": False}, project["id"], {})
    assert token not in json.dumps(shown) and MASK in shown["content"]


def test_a_secret_split_between_two_transcript_lines_is_masked_in_both(workspace, tmp_path):
    store, project, root = workspace
    token = store.settings.token
    half = len(token) // 2
    history = tmp_path / "split.jsonl"
    history.write_text(user_line(root, f"first half {token[:half]}") + user_line(root, f"{token[half:]} second half"))
    import_history(store, history, project["id"], "claude")
    texts = [c["text"] for s in sources_of(store, project) for c in chunks_of(store, s["id"])]
    assert texts == [f"first half {MASK}", f"{MASK} second half"]


def test_cut_off_pieces_shorter_than_twelve_characters_are_ordinary_words():
    masker = Masker(["lm-studio", "sk-ant-api03-fixturefixturefixture"])
    assert masker.mask("Anthropic keys start with sk-ant") == ("Anthropic keys start with sk-ant", [])
    assert masker.mask("run it with lm-stu") == ("run it with lm-stu", [])
    # A whole saved value is still replaced, even a placeholder: ingestion cannot tell it from a key.
    assert masker.mask("We run lm-studio locally.")[0] == f"We run {MASK} locally."
    assert masker.mask("output: sk-ant-api03-fix")[0] == f"output: {MASK}"


def test_a_private_key_header_without_its_end_masks_only_the_key_lines():
    masker = Masker()
    prose = "A key file must start with -----BEGIN RSA PRIVATE KEY----- and end with its END line."
    assert masker.mask(prose) == (f"A key file must start with {MASK} and end with its END line.", ["private_key"])
    pasted = ("Here:\n-----BEGIN RSA PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\nMIIEowIBAAKCAQEAfixturefixture0\n"
              "AbCdEf==\nThat was all of it.")
    assert masker.mask(pasted)[0] == f"Here:\n{MASK}\nThat was all of it."
    # The raw JSON line of a metadata record carries escaped line breaks.
    raw = json.dumps({"t": "-----BEGIN PRIVATE KEY-----\nMIIEowIBAAKCAQEAfixturefixture0\nabc=\nThen words"})
    masked = masker.mask(raw)[0]
    assert "MIIEow" not in masked and "abc=" not in masked and masked.endswith('\\nThen words"}')


def assistant_with_tool(root, text):
    return json.dumps({"type": "assistant", "cwd": str(root), "sessionId": "s", "message": {
        "role": "assistant", "content": [{"type": "text", "text": text},
                                         {"type": "tool_use", "name": "Read", "input": {"path": "app.py"}}]}}) + "\n"


def test_a_text_block_that_ends_in_a_piece_of_a_value_still_matches_the_view(workspace, tmp_path):
    from cairnmind.secret_mask import ingest_masker
    store, project, root = workspace
    piece = store.settings.token[:14]
    history = tmp_path / "piece.jsonl"
    history.write_text(assistant_with_tool(root, f"We decided to drop {GITHUB_SHAPE}, it began {piece}"))
    import_history(store, history, project["id"], "claude")
    (source,) = sources_of(store, project)
    request = extraction_request(source, ingest_masker(store.settings).mask)
    # Inside the view the block is followed by the tool call, so its last characters are not the text's end.
    assert request["segments"] == [f"We decided to drop {MASK}, it began {piece}"]


async def test_extraction_of_a_masked_source_fails_instead_of_skipping_when_secrets_are_unreadable_or_changed(
        workspace, tmp_path):
    from cairnmind.agent import KnowledgeAgent
    store, project, root = workspace
    token = store.settings.token
    history = tmp_path / "assistant.jsonl"
    history.write_text(assistant_with_tool(root, f"We decided to rotate {token} every month."))
    import_history(store, history, project["id"], "claude")
    (source,) = sources_of(store, project)
    # A masker that no longer knows the token: the block is not part of the view, and that is an error.
    with pytest.raises(ValueError, match="known secrets changed"):
        extraction_request(source, Masker().mask)
    (store.data_dir / "cockpit").mkdir(exist_ok=True)
    (store.data_dir / "cockpit" / "preferences.json").write_text('{"version": 99}')
    job = store.claim_job("mask-test", lease_seconds=120, kinds=["extract"])
    model = QuotingModel()
    with pytest.raises(ValueError, match="secrets_unreadable"):
        await KnowledgeAgent(store, Settings(data_dir=store.data_dir), model).extract(job)
    assert model.calls == []


def test_session_findings_checkpoints_and_tasks_are_indexed_masked(workspace, tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from cairnmind.cli import app
    from cairnmind.findings import save_finding
    store, project, _ = workspace
    token = store.settings.token
    store.checkpoint(project["id"], {"summary": f"The hook uses {token}."})
    with store.transaction():
        finding = save_finding(store, project["id"], "8d2e4f60-1a3b-4c5d-9e7f-0a1b2c3d4e5f",
                               f"Never paste {token} into a commit.", "constraint", "proposed", session_id="s-1")
    monkeypatch.setattr("cairnmind.ingest.register_default_histories", lambda *a: {})
    result = CliRunner().invoke(app, ["--data-dir", str(store.data_dir), "task", "add", "masking", f"Rotate {token}"])
    assert result.exit_code == 0, result.output
    sources = [store.get_source(finding["source"]["id"])]
    sources += store.query("SELECT FROM sources WHERE project_id=:p AND (kind='checkpoint' OR uri LIKE 'manual://task/%')",
                           {"p": project["id"]})
    assert len(sources) == 3
    for source in sources:
        assert token in source["content"] and source["metadata"]["masked"] is True
        assert token not in source["metadata"]["normalized_text"]
    index_pending_sources(store)
    for source in sources:
        chunks = chunks_of(store, source["id"])
        assert chunks and all(token not in chunk["text"] for chunk in chunks)
