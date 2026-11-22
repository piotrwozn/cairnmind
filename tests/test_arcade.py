from __future__ import annotations

import json
import time

import httpx
import pytest

from cairnmind.arcade import ArcadeClient, ArcadeError
from cairnmind.config import Settings


def test_client_uses_readonly_endpoint_and_named_json_parameters(tmp_path):
    settings = Settings.load(tmp_path, create=True)
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"result": [{"value": 42}]})

    client = ArcadeClient(settings, transport=httpx.MockTransport(handle))
    assert client.query("SELECT :value AS value", {"value": 42}) == [{"value": 42}]
    assert seen[0].url.path.startswith("/api/v1/query/")
    assert json.loads(seen[0].content)["params"] == {"value": 42}
    assert "authorization" in seen[0].headers
    assert settings.arcade_password not in str(seen[0].url)


@pytest.mark.parametrize("body", [[], None, "bad response", {"error": "failure"}])
def test_client_normalizes_invalid_error_shapes(tmp_path, body):
    settings = Settings.load(tmp_path, create=True)
    client = ArcadeClient(settings, transport=httpx.MockTransport(lambda _: httpx.Response(503, json=body)))
    with pytest.raises(ArcadeError) as error:
        client.query("SELECT 1")
    assert error.value.status_code == 503


def test_client_redacts_password_in_backend_error(tmp_path):
    settings = Settings.load(tmp_path, create=True)
    client = ArcadeClient(settings, transport=httpx.MockTransport(
        lambda _: httpx.Response(401, json={"detail": "rejected " + settings.arcade_password})))
    with pytest.raises(ArcadeError) as error:
        client.query("SELECT 1")
    assert settings.arcade_password not in str(error.value)
    assert "[redacted]" in str(error.value)


def test_a_request_the_test_engine_does_not_answer_says_what_it_waited_for(tmp_path, capsys):
    """The report of a timeout (#221) names the statement, the wait and the engine's log, and the error is unchanged."""
    def silent(request):
        if request.url.path.startswith("/api/v1/command/"):
            time.sleep(5.1)  # the wait that makes the report worth printing
            raise httpx.ReadTimeout("no answer", request=request)
        return httpx.Response(200, text="<html>not json</html>")  # the probe gets a bad answer: the report must survive it
    client = ArcadeClient(Settings.load(tmp_path, create=True), transport=httpx.MockTransport(silent))
    with pytest.raises(ArcadeError, match=r"^Local ArcadeDB is unavailable \(ReadTimeout\); run cairnmind engine"):
        client.command("CREATE PROPERTY probe.name IF NOT EXISTS STRING")
    shown = capsys.readouterr().err
    assert "ENGINE UNAVAILABLE after " in shown and "CREATE PROPERTY probe.name" in shown
    assert "load average" in shown and "engine.log ends:" in shown
    # The engine's own threads, asked of the real test engine: what it was doing is the cause when it stalls.
    assert "engine threads:" in shown and "Full thread dump" in shown


def test_the_engine_fixture_wraps_requests_in_the_stall_watch():
    """Without the watch a stalled request is only reported after the client gave up (#221)."""
    wrapped = [cell.cell_contents for cell in ArcadeClient._request.__closure__]
    assert any(getattr(item, "__module__", None) == "stall_watch" for item in wrapped)


def test_native_transaction_rolls_back_and_query_rejects_writes(tmp_path):
    from cairnmind.store import Store
    store = Store(tmp_path)
    with pytest.raises(RuntimeError, match="abort"):
        with store.transaction() as tx:
            tx.command("INSERT INTO projects CONTENT :doc", {"doc": {
                "id": "rollback-project", "name": "rollback", "root": "/tmp/rollback"}})
            raise RuntimeError("abort")
    assert store.query("SELECT FROM projects WHERE id=:id", {"id": "rollback-project"}) == []
    with pytest.raises(ArcadeError):
        store.query("DELETE FROM projects")


def test_edge_id_cannot_be_reassigned_to_another_project(tmp_path):
    from cairnmind.store import Store
    store = Store(tmp_path / "data")
    first = store.add_project("first", tmp_path / "first")
    second = store.add_project("second", tmp_path / "second")
    for project in (first, second):
        for suffix in ("a", "b"):
            store.upsert_node({"id": project["id"] + suffix, "project_id": project["id"],
                               "kind": "symbol", "label": suffix})
    edge = {"id": "shared-edge", "project_id": first["id"], "from_id": first["id"] + "a",
            "to_id": first["id"] + "b", "kind": "calls"}
    store.upsert_edge(edge)
    with pytest.raises(ValueError, match="another project"):
        store.upsert_edge({**edge, "project_id": second["id"], "from_id": second["id"] + "a",
                           "to_id": second["id"] + "b"})
    assert store.query("SELECT project_id FROM edges WHERE id=:id", {"id": "shared-edge"})[0]["project_id"] == first["id"]
