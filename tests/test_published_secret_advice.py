"""The secrets gate's refusal advises only what can work (#239): a release never takes a registered secret."""
from __future__ import annotations

import pytest
from conftest import person_edit
from test_published_memory import reviewed
from test_published_memory_release import ada  # noqa: F401 (a fixture)
from test_published_memory_secrets import SHAPES

from cairnmind.memory.memory_published import Publisher
from cairnmind.memory.memory_published_release import SecretsRefused
from cairnmind.terminal.inspector import Inspector


def refusal(store, pid):
    with pytest.raises(SecretsRefused) as refused:
        Publisher(store).publish(pid, preview=True)
    return " ".join(str(refused.value).split())


def test_a_registered_secret_is_not_sent_to_memory_release_and_a_key_shape_still_is(ada, monkeypatch):  # noqa: F811
    store, pid = ada["store"], ada["project"]
    monkeypatch.setenv("OPENAI_API_KEY", "plain env key 22")
    leaked = reviewed(store, pid, "decision", "Connect with plain env key 22 for now.", "connect")
    words = refusal(store, pid)
    assert "registered secret in text" in words and "choose Release" not in words, words
    assert "never released" in words and "memory release" not in words, words
    token = reviewed(store, pid, "fact", f"CI uses {SHAPES['github_token']}.", "ci-token")
    both = refusal(store, pid)
    assert "inspector of the workspace (`cairnmind`) and choose Release" in both and "never released" in both, both
    assert "memory release" not in both, both
    # What the refusal says is true: a later text without the value does not take it out of the record ...
    changed = person_edit(store, pid, {"id": leaked["id"], "expected_version": leaked["version"],
                                       "content": "Connect with the key in the vault."}, leaked["source_ids"])
    Inspector(store).review(pid, leaked["id"], "confirm", changed["version"])
    assert leaked["id"] in refusal(store, pid)
    # ... and once the credential is no longer registered here, only the other value is refused.
    monkeypatch.delenv("OPENAI_API_KEY")
    remaining = refusal(store, pid)
    assert token["id"] in remaining and leaked["id"] not in remaining, remaining


def test_the_held_list_tells_a_terminal_confirmation_from_a_proposal_and_shows_which_record(ada):  # noqa: F811
    from conftest import as_person
    store, pid = ada["store"], ada["project"]
    first = store.snapshot(pid, "note://cli", "Agents may write facts.", "note")
    typed = as_person(store, pid, {"kind": "decision", "key": "agents", "content": "Agents may write facts."},
                      [first["id"]])
    second = store.snapshot(pid, "note://proposal", "x", "note")
    proposed = store.remember(pid, {"kind": "fact", "key": "hid‮den\x1b[31m", "content": "Line one\x1b[31m red‮ and "
                                    + "a long tail " * 20}, [second["id"]])
    held = {item["id"]: item for item in Publisher(store).publish(pid, preview=True)["held"]["items"]}
    assert held[typed["id"]]["reason"].startswith("confirmed in a terminal, which writes no audit that travels")
    assert "inspector" in held[typed["id"]]["reason"]
    assert held[proposed["id"]]["reason"] == "proposed, not yet confirmed; review it in the inspector"
    assert (held[typed["id"]]["key"], held[typed["id"]]["excerpt"]) == ("agents", "Agents may write facts.")
    excerpt = held[proposed["id"]]["excerpt"]
    # The record's own text, cut to one short line, with nothing in it that a terminal would act on.
    assert len(excerpt) <= 80 and excerpt.endswith("…")
    assert "\x1b" not in excerpt and "‮" not in excerpt and excerpt.startswith("Line one")
    # The key a session wrote is as untrusted as the text: no bidi override, no escape sequence (#239 review).
    key = held[proposed["id"]]["key"]
    assert key.startswith("hid") and "‮" not in key and "\x1b" not in key, repr(key)


def test_the_held_list_prints_no_value_the_secrets_gate_refuses(ada, monkeypatch):  # noqa: F811
    import json
    store, pid = ada["store"], ada["project"]
    monkeypatch.setenv("OPENAI_API_KEY", "plain env key 22")
    source = store.snapshot(pid, "note://proposal", "x", "note")
    in_text = store.remember(pid, {"kind": "fact", "key": "connect", "content": "plain env key 22 is the key"},
                             [source["id"]])
    in_key = store.remember(pid, {"kind": "fact", "key": "plain env key 22", "content": "The service key."},
                            [source["id"]])
    shaped = store.remember(pid, {"kind": "fact", "key": "ci", "content": f"CI uses {SHAPES['github_token']}."},
                            [source["id"]])
    clean = store.remember(pid, {"kind": "fact", "key": "plain", "content": "Nothing secret here."}, [source["id"]])
    result = Publisher(store).publish(pid, preview=True)
    printed = json.dumps(result, ensure_ascii=False)
    assert "plain env key 22" not in printed and SHAPES["github_token"] not in printed
    held = {item["id"]: item for item in result["held"]["items"]}
    for record in (in_text, in_key, shaped):
        item = held[record["id"]]
        assert item["key"] is None and item["excerpt"] is None and "secrets gate" in item["withheld"], item
        assert item["reason"] == "proposed, not yet confirmed; review it in the inspector"
    assert (held[clean["id"]]["key"], held[clean["id"]]["excerpt"]) == ("plain", "Nothing secret here.")
    assert "withheld" not in held[clean["id"]]
