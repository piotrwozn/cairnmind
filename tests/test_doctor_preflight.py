"""Tests for `cairnmind doctor` pre-flight integration (Pulse, MCP presets, --check-mcp, host/mount checks)."""
from __future__ import annotations

import json
from pathlib import PurePosixPath
from types import SimpleNamespace

from typer.testing import CliRunner

from cairnmind import cli, doctor_attention, doctor_preflight, integrations, mcp_presets
from cairnmind.config import Settings

runner = CliRunner()


def test_doctor_json_and_summary_include_preflight_and_check_mcp(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.chdir(repo)

    data_dir = tmp_path / "memory"
    settings = Settings.load(data_dir, create=True)

    # Write a project Cursor preset so doctor discovers it.
    mcp_presets.generate_preset("cursor", settings, repo, scope="project", write=True, home=home)

    # Stub doctor's heavy probes so this runs in the fast tier without ArcadeDB/Ollama.
    monkeypatch.setattr(integrations, "doctor", lambda s: {
        "python": "3.12.3",
        "python_executable": "/usr/bin/python3",
        "openssl": "OpenSSL 3.0",
        "platform": "Linux",
        "wsl": False,
        "data_dir": str(s.data_dir),
        "clients": {"codex": {"installed": False, "verified_resume": False, "checks": {}, "notes": []},
                    "claude": {"installed": False, "verified_resume": False, "checks": {}, "notes": []}},
        "ollama": {"available": False, "url": s.ollama_url},
        "service": {"available": False},
        "arcadedb": {"available": True, "database_available": True, "version_matches": True, "url": s.arcade_url},
        "java": {"on_path": True, "compatible": True, "major": 21},
        "tmux": {"available": False},
        "memory_models": {},
    })
    monkeypatch.setattr("cairnmind.knowledge_mode.doctor_fields", lambda s: {"knowledge_agent": "disabled (deterministic-only mode)"})
    monkeypatch.setattr("cairnmind.store_writer.inspect", lambda s: {"role": "writer"})
    monkeypatch.setattr(cli, "service_alive", lambda s: False)
    monkeypatch.setattr(cli, "store_for_read", lambda s: (_ for _ in ()).throw(RuntimeError("no engine in fast test")))

    res = runner.invoke(cli.app, ["--data-dir", str(data_dir), "doctor", "--check-mcp", "--json"])
    assert res.exit_code == 0, res.output
    report = json.loads(res.stdout)

    assert "pulse" in report
    assert any(p["client"] == "cursor" and p["configured"] for p in report["mcp_presets"])
    assert report["mcp_handshake"]["ok"] is True
    assert report["mcp_handshake"]["stdout_clean"] is True
    assert report["security_guards"]["read_before_write_ack"] is True
    assert report["storage_preflight"]["writable"] is True

    lines = doctor_attention.summary(report)
    rendered = "\n".join(lines)
    assert "Pre-flight:" in rendered
    assert "MCP Clients:" in rendered and "cursor" in rendered
    assert "MCP Handshake: OK" in rendered
    assert "Security Guards: Read-before-write ACK ON" in rendered


def test_doctor_check_mcp_exits_1_when_handshake_fails(tmp_path, monkeypatch):
    data_dir = tmp_path / "memory"
    Settings.load(data_dir, create=True)
    monkeypatch.setattr(integrations, "doctor", lambda s: {
        "python": "3.12.3", "data_dir": str(s.data_dir), "clients": {}, "ollama": {"available": False},
        "service": {"available": False},
        "arcadedb": {"available": True, "database_available": True, "version_matches": True, "url": s.arcade_url},
        "java": {"on_path": True, "compatible": True, "major": 21}, "tmux": {}, "memory_models": {},
    })
    monkeypatch.setattr("cairnmind.knowledge_mode.doctor_fields", lambda s: {"knowledge_agent": "disabled (deterministic-only mode)"})
    monkeypatch.setattr("cairnmind.store_writer.inspect", lambda s: {"role": "writer"})
    monkeypatch.setattr(cli, "service_alive", lambda s: False)
    monkeypatch.setattr(cli, "store_for_read", lambda s: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(
        doctor_preflight,
        "doctor_mcp_handshake",
        lambda s, project_root=None, timeout=5.0: {
            "ok": False, "latency_ms": 5000, "stdout_clean": False, "error": "Dirty stdout: banner"
        },
    )

    res = runner.invoke(cli.app, ["--data-dir", str(data_dir), "doctor", "--check-mcp", "--json"])
    assert res.exit_code == 1
    report = json.loads(res.stdout)
    assert report["mcp_handshake"]["ok"] is False
    assert any("MCP Handshake failed" in line for line in doctor_attention.required_missing(report))


def test_storage_preflight_warns_on_wsl_mnt_network_fs_and_uid_mismatch(tmp_path, monkeypatch):
    wsl_path = PurePosixPath("/mnt/c/Users/alice/cairnmind-data")
    fake_data = SimpleNamespace(expanduser=lambda: SimpleNamespace(resolve=lambda: wsl_path))
    settings = SimpleNamespace(data_dir=fake_data)
    monkeypatch.setattr(doctor_preflight.platform_hints, "is_wsl", lambda: True)
    monkeypatch.setattr(doctor_preflight, "_check_writable", lambda p: (True, False, "UID mismatch warning"))
    monkeypatch.setattr(doctor_preflight, "_mount_fs_type", lambda p: "nfs4")

    report = doctor_preflight.storage_preflight(settings, project_root=tmp_path)
    assert report["writable"] is False
    assert report["uid_mismatch"] is True
    assert report["wsl_windows_mount"] is True
    assert report["network_fs"] == "nfs4"
    assert any("/mnt/" in w for w in report["warnings"])
    assert any("not writable" in w for w in report["warnings"])
    assert any("UID mismatch" in w for w in report["warnings"])


def test_doctor_attention_renders_pulse_status_and_marks_broken_as_missing():
    base = {
        "data_dir": "/tmp/cm",
        "environment_notes": [],
        "environment_blockers": [],
        "store_writer": {"role": "writer"},
        "arcadedb": {"available": True, "database_available": True, "version_matches": True, "url": "http://127.0.0.1:2480"},
        "pulse": {"status": "healthy", "drift_files": [], "stale_test_tasks": [], "degraded_entities": []},
        "security_guards": {"read_before_write_ack": True, "snapshot_profile": "team"},
    }
    assert doctor_attention.required_missing(base) == []
    text = "\n".join(doctor_attention.summary(base))
    assert "Memory Pulse: HEALTHY (0 drift, 0 stale tests, 0 degraded)" in text
    assert "snapshot profile=team" in text

    broken = {**base, "pulse": {"status": "broken", "integrity_errors": ["Blocked migration lock present"]}}
    missing = doctor_attention.required_missing(broken)
    assert any("Memory Pulse: BROKEN" in line for line in missing)
