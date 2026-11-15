"""The secrets gate's refusal advises only what can work (#239): `memory release` never takes a registered secret."""
from __future__ import annotations

import pytest

from cairnmind.inspector import Inspector
from cairnmind.memory_published import Publisher
from cairnmind.memory_published_release import SecretsRefused

from conftest import person_edit
from test_published_memory_release import ada  # noqa: F401 (a fixture)
from test_published_memory import reviewed
from test_published_memory_secrets import SHAPES


def refusal(store, pid):
    with pytest.raises(SecretsRefused) as refused:
        Publisher(store).publish(pid, preview=True)
    return " ".join(str(refused.value).split())


def test_a_registered_secret_is_not_sent_to_memory_release_and_a_key_shape_still_is(ada, monkeypatch):  # noqa: F811
    store, pid = ada["store"], ada["project"]
    monkeypatch.setenv("OPENAI_API_KEY", "plain env key 22")
    leaked = reviewed(store, pid, "decision", "Connect with plain env key 22 for now.", "connect")
    words = refusal(store, pid)
    assert "registered secret in text" in words and "memory release" not in words, words
    assert "never released" in words, words
    token = reviewed(store, pid, "fact", f"CI uses {SHAPES['github_token']}.", "ci-token")
    both = refusal(store, pid)
    assert "memory release" in both and "never released" in both, both
    # What the refusal says is true: a later text without the value does not take it out of the record ...
    changed = person_edit(store, pid, {"id": leaked["id"], "expected_version": leaked["version"],
                                       "content": "Connect with the key in the vault."}, leaked["source_ids"])
    Inspector(store).review(pid, leaked["id"], "confirm", changed["version"])
    assert leaked["id"] in refusal(store, pid)
    # ... and once the credential is no longer registered here, only the other value is refused.
    monkeypatch.delenv("OPENAI_API_KEY")
    remaining = refusal(store, pid)
    assert token["id"] in remaining and leaked["id"] not in remaining, remaining
