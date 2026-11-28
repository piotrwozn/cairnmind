"""The times of the writer row and its history come from the database server, not from each client's clock (#372)."""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from cairnmind.storage import store_writer
from cairnmind.storage.arcade import ArcadeClient
from cairnmind.storage.sql import update_document, utc_now
from cairnmind.store import Store

FORMAT = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}\+00:00$")


def second_installation(tmp_path: Path, first: Store) -> Path:
    data_dir = tmp_path / "second"
    data_dir.mkdir(mode=0o700)
    (data_dir / "config.json").write_text(json.dumps({"arcade_database": first.settings.arcade_database}))
    return data_dir


def row(store: Store) -> dict:
    return store_writer.read_writer(ArcadeClient(store.settings))


def seconds_from_now(text: str) -> float:
    return abs((datetime.fromisoformat(text) - datetime.now(UTC)).total_seconds())


def takeovers_with_wrong_clocks(monkeypatch, first: Store, second_dir: Path) -> list[str]:
    """The second installation takes over with a clock years behind, then the first takes back with one ahead."""
    monkeypatch.setattr(store_writer, "utc_now", lambda: "2020-01-01T00:00:00.000000+00:00")
    assert store_writer.take_over_at(second_dir)["taken_over"] is True
    monkeypatch.setattr(store_writer, "utc_now", lambda: "2035-01-01T00:00:00.000000+00:00")
    assert store_writer.take_over_at(first.data_dir)["taken_over"] is True
    return [entry["at"] for entry in row(first)["history"]]


def test_two_takeovers_with_wrong_client_clocks_give_a_history_that_does_not_go_backwards(tmp_path, monkeypatch):
    first = Store(tmp_path / "first")
    second_dir = second_installation(tmp_path, first)
    assert Store(second_dir).read_only
    times = takeovers_with_wrong_clocks(monkeypatch, first, second_dir)
    assert len(times) == 3 and times == sorted(times) and all(FORMAT.fullmatch(at) for at in times), times
    assert all(seconds_from_now(at) < 300 for at in times), times  # the server's time, not the clients' 2020 and 2035
    assert row(first)["since"] == times[-1] and row(first)["installation_id"] == first.installation.id


def test_the_old_behaviour_went_backwards(tmp_path, monkeypatch):
    """The trial's finding, kept as a check on the test: with each client's own clock the entry committed last is earlier."""
    first = Store(tmp_path / "first")
    second_dir = second_installation(tmp_path, first)
    assert Store(second_dir).read_only
    monkeypatch.setattr(store_writer, "server_now", lambda tx: store_writer.utc_now())
    times = takeovers_with_wrong_clocks(monkeypatch, first, second_dir)
    assert times[1] == "2020-01-01T00:00:00.000000+00:00" and times != sorted(times)


def test_a_row_written_with_a_clients_clock_is_still_read_and_extended(tmp_path):
    first = Store(tmp_path / "first")
    second_dir = second_installation(tmp_path, first)
    client = ArcadeClient(first.settings)
    old = "2026-01-01T00:00:00.123456+00:00"  # what a 0.12.0 client wrote
    history = [{"installation_id": first.installation.id, "host": "old-host", "at": old, "how": "first"}]
    update_document(client, "devbrain_meta", {"since": old, "host": "old-host", "history": history}, "name='writer'", {})
    shown = store_writer.describe(row(first))
    assert shown["since"] == old and shown["history"][0]["at"] == old
    reader = Store(second_dir)
    assert reader.writer_state()["writer"]["since"] == old and "old-host" in reader.not_writer
    assert store_writer.take_over_at(second_dir)["taken_over"] is True
    after = row(first)
    assert [entry["at"] for entry in after["history"]][0] == old
    assert after["history"][1]["at"] > old and after["since"] == after["history"][1]["at"]
    assert after["history"][1]["previous"] == first.installation.id and utc_now() > after["since"]
