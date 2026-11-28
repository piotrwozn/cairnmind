"""Phase 5 fast tests: SQLite scale telemetry, FS risk detection, doctor & service quiesce (PR #8).

Runs without ArcadeDB or Java (registered in NO_ARCADE_MODULES and windows-fast-tests).
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from typer.testing import CliRunner

from cairnmind import cli, doctor_attention, integrations, migration, sqlite_telemetry
from cairnmind.config import Settings
from cairnmind.service import create_app
from cairnmind.store import Store


def _init_sqlite_settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "cm-data"
    settings = Settings.load(data_dir, create=True)
    settings.storage_backend = "sqlite"
    settings.knowledge_agent = "disabled"
    settings.save()
    return Settings.load(data_dir, create=False)


def test_sqlite_metrics_persistence_and_threshold_breaches(tmp_path: Path):
    settings = _init_sqlite_settings(tmp_path)
    store = Store(settings.data_dir, settings=settings)
    try:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")
        proj = store.add_project("scale_proj", repo)
        src = store.snapshot(proj["id"], f"file://{repo / 'app.py'}", "def hello(): pass", "code", {})
        n1 = store.upsert_node({"id": "n-a", "project_id": proj["id"], "kind": "symbol", "label": "a", "source_id": src["id"]})
        n2 = store.upsert_node({"id": "n-b", "project_id": proj["id"], "kind": "symbol", "label": "b", "source_id": src["id"]})
        store.upsert_edge({
            "id": "e-ab",
            "project_id": proj["id"],
            "from_id": n1["id"],
            "to_id": n2["id"],
            "kind": "calls",
            "source_id": src["id"],
        })

        sqlite_telemetry.record_sqlite_metrics(
            settings.data_dir,
            {
                "db_bytes": 600 * 1024 * 1024,
                "fts_p95_ms": 310.5,
                "graph_p95_ms": 275.0,
                "busy_retries_10m": 14,
                "busy_retries_total": 20,
            },
        )
        # Normal default thresholds trigger on latency & busy retries & db_bytes override via custom thresholds
        health = sqlite_telemetry.evaluate_sqlite_health(
            settings,
            store=store,
            thresholds={
                "max_db_bytes": 100,
                "max_files_count": 0,
                "max_edges_count": 0,
                "max_fts_p95_ms": 250.0,
                "max_graph_p95_ms": 250.0,
                "max_busy_retries_10m": 10,
            },
            mounts_text="/dev/sda1 / ext4 rw 0 0\n",
            platform_name="linux",
            is_wsl=False,
        )
    finally:
        store.close()

    assert health["recommend_server_migration"] is True
    breached_names = {item["metric"] for item in health["breached_thresholds"]}
    assert breached_names == {
        "db_bytes",
        "files_count",
        "edges_count",
        "fts_p95_ms",
        "graph_p95_ms",
        "busy_retries_10m",
    }
    assert any("cairnmind migrate --to server" in w for w in health["warnings"])


@pytest.mark.parametrize(
    ("path_str", "plat", "is_wsl", "mounts", "drive_type", "expect_risk", "expect_kind"),
    [
        ("/mnt/nfs/cm/state.db", "linux", False, "10.0.0.1:/exp /mnt/nfs nfs4 rw 0 0\n/dev/sda1 / ext4 rw 0 0\n", None, True, "network_fs"),
        ("/media/share/state.db", "linux", False, "//srv/share /media/share cifs rw 0 0\n/dev/sda1 / ext4 rw 0 0\n", None, True, "network_fs"),
        ("/mnt/c/Users/dev/state.db", "linux", True, "C:\\134 /mnt/c 9p rw 0 0\n/dev/sdb / ext4 rw 0 0\n", None, True, "wsl_drvfs"),
        ("/mnt/d/work/state.db", "linux", True, "/dev/sdb / ext4 rw 0 0\n", None, True, "wsl_drvfs"),
        ("/home/dev/.local/share/cairnmind/state.db", "linux", True, "/dev/sdb / ext4 rw 0 0\n", None, False, None),
        (r"\\fileserver\team\cairnmind\state.db", "win32", False, "", None, True, "unc"),
        (r"Z:\cairnmind\state.db", "win32", False, "", 4, True, "network_drive"),
        (r"C:\Users\dev\AppData\Local\cairnmind\state.db", "win32", False, "", 3, False, None),
        (":memory:", "linux", False, "", None, False, None),
    ],
)
def test_filesystem_risk_detection_matrix(
    path_str: str,
    plat: str,
    is_wsl: bool,
    mounts: str,
    drive_type: int | None,
    expect_risk: bool,
    expect_kind: str | None,
):
    result = sqlite_telemetry.detect_filesystem_risk(
        path_str,
        platform_name=plat,
        is_wsl=is_wsl,
        mounts_text=mounts,
        drive_type_fn=(lambda _p: drive_type) if drive_type is not None else None,
    )
    assert result["risk"] is expect_risk
    assert result["kind"] == expect_kind


def test_passive_daily_stderr_hint_rate_limit_and_json_hygiene(tmp_path: Path, monkeypatch):
    settings = _init_sqlite_settings(tmp_path)
    store = Store(settings.data_dir, settings=settings)
    store.close()

    sqlite_telemetry.record_sqlite_metrics(
        settings.data_dir,
        {"fts_p95_ms": 420.0, "busy_retries_10m": 15, "busy_retries_total": 15},
        now=1000.0,
    )
    health = sqlite_telemetry.evaluate_sqlite_health(settings)
    assert health["recommend_server_migration"] is True

    buf1 = io.StringIO()
    assert sqlite_telemetry.maybe_emit_scale_hint(settings, health, now=1000.0, stream=buf1) is True
    assert "cairnmind migrate --to server" in buf1.getvalue()

    # Within 24h cooldown -> suppressed!
    buf2 = io.StringIO()
    assert sqlite_telemetry.maybe_emit_scale_hint(settings, health, now=1000.0 + 3600.0, stream=buf2) is False
    assert buf2.getvalue() == ""

    # After 24h -> emits again!
    buf3 = io.StringIO()
    assert (
        sqlite_telemetry.maybe_emit_scale_hint(
            settings, health, now=1000.0 + sqlite_telemetry.HINT_COOLDOWN_SECONDS + 5.0, stream=buf3
        )
        is True
    )
    assert "cairnmind migrate --to server" in buf3.getvalue()

    # CLI `doctor --json` must NEVER leak hint text onto stdout!
    monkeypatch.setattr(
        sqlite_telemetry,
        "load_sqlite_metrics",
        lambda _d: {"fts_p95_ms": 500.0, "busy_retries_10m": 20, "last_warned_at": 0.0},
    )
    runner = CliRunner()
    res = runner.invoke(cli.app, ["--data-dir", str(settings.data_dir), "doctor", "--json"])
    assert res.exit_code == 0, res.output
    parsed = json.loads(res.stdout)
    assert parsed["storage_backend"] == "sqlite"
    assert parsed["java"]["needed"] is False
    assert parsed["arcadedb"]["needed"] is False
    assert parsed["sqlite"]["recommend_server_migration"] is True


def test_doctor_sqlite_mode_requires_no_java_and_surfaces_warnings(tmp_path: Path):
    settings = _init_sqlite_settings(tmp_path)
    store = Store(settings.data_dir, settings=settings)
    store.close()

    sqlite_telemetry.record_sqlite_metrics(
        settings.data_dir,
        {"graph_p95_ms": 380.0, "busy_retries_10m": 12},
    )
    report = integrations.doctor(settings)
    report["store_writer"] = {"role": "writer"}
    report["environment_notes"] = []
    report["environment_blockers"] = []
    assert report["storage_backend"] == "sqlite"
    assert report["java"]["needed"] is False
    assert report["arcadedb"]["needed"] is False
    assert doctor_attention.required_missing(report) == []

    lines = doctor_attention.summary(report)
    joined = "\n".join(lines)
    assert "Embedded SQLite database" in joined
    assert "cairnmind migrate --to server" in joined


def test_service_migration_quiesce_503_and_reload_store(tmp_path: Path):
    settings = _init_sqlite_settings(tmp_path)
    store = Store(settings.data_dir, settings=settings)
    repo = tmp_path / "svc-repo"
    repo.mkdir()
    store.add_project("svc_proj", repo)

    app = create_app(settings, start_worker=False, background_inference=False, store=store)
    headers = {"Authorization": f"Bearer {settings.token}"}
    with TestClient(app) as client:
        # Normal request succeeds before migration marker
        ok = client.get("/projects", headers=headers)
        assert ok.status_code == 200
        assert len(ok.json()) == 1
        assert client.get("/health").json()["backend"] == "sqlite"

        # Place migration-in-progress marker -> all routes except /health, /shutdown, /internal/reload-store return 503!
        marker = migration.migration_marker_path(settings.data_dir)
        marker.write_text(json.dumps({"migration_id": "test-mig"}), encoding="utf-8")
        try:
            blocked = client.get("/projects", headers=headers)
            assert blocked.status_code == 503
            body = blocked.json()
            assert body["error_code"] == "migration_in_progress"
            # /health remains reachable during migration
            assert client.get("/health").status_code == 200
        finally:
            marker.unlink(missing_ok=True)

        # Reload store cleanly via /internal/reload-store
        reloaded = client.post("/internal/reload-store", headers=headers)
        assert reloaded.status_code == 200
        assert reloaded.json() == {"reloaded": True, "backend": "sqlite"}
        after = client.get("/projects", headers=headers)
        assert after.status_code == 200
        assert after.json()[0]["name"] == "svc_proj"


def test_migrate_preflight_refusals(tmp_path: Path, monkeypatch):
    settings = _init_sqlite_settings(tmp_path)
    store = Store(settings.data_dir, settings=settings)
    store.close()

    # 1. Refused inside coding session (ADR-04)
    monkeypatch.setenv("CAIRNMIND_SESSION_ID", "sess-active-123")
    with pytest.raises(migration.MigrationRefused) as exc_info:
        migration.migrate(settings, to="server")
    assert exc_info.value.code == "in_session"
    with pytest.raises(migration.MigrationRefused) as exc_abort:
        migration.abort_migration(settings)
    assert exc_abort.value.code == "in_session"
    monkeypatch.delenv("CAIRNMIND_SESSION_ID")

    # 2. Refused when already on target backend (--to sqlite while on sqlite)
    with pytest.raises(migration.MigrationRefused) as exc_same:
        migration.migrate(settings, to="sqlite")
    assert exc_same.value.code == "already_on_target"

    # 3. Refused on insufficient disk space (< 2x source / minimum floor)
    with pytest.raises(migration.MigrationRefused) as exc_disk:
        migration.migrate(
            settings,
            to="server",
            disk_usage_fn=lambda _p: SimpleNamespace(total=1024, used=1000, free=24),
        )
    assert exc_disk.value.code == "insufficient_disk_space"

    # 4. Refused --resume / --abort without active checkpoint
    with pytest.raises(migration.MigrationRefused) as exc_res:
        migration.migrate(settings, resume=True)
    assert exc_res.value.code == "no_migration_state"
    with pytest.raises(migration.MigrationRefused) as exc_ab:
        migration.abort_migration(settings)
    assert exc_ab.value.code == "no_migration_state"

    # 5. CLI rejects conflicting options cleanly
    runner = CliRunner()
    bad = runner.invoke(cli.app, ["--data-dir", str(settings.data_dir), "migrate", "--abort", "--to", "server"])
    assert bad.exit_code != 0
