#!/usr/bin/env python3
"""Real isolated uv-tool upgrade from archived 0.7.1 to guarded 0.7.2.

Reuses a caller-owned ArcadeDB engine, creates/drops only a unique database,
and never starts model inference. Logs and fixture files survive failures.
"""
from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from uuid import uuid4
import zipfile
from pathlib import Path


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--shared-engine", type=Path, required=True)
    args = parser.parse_args()
    workspace = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("archived_smoke_helpers",
                                                workspace / "release-tools/test_devbrain_update071_smoke.py")
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    digest, write_json = helpers.digest, helpers.write_json
    manifest_path = workspace / "releases/devbrain-update-0.7.2-manifest.json"
    updater_path = workspace / "releases/devbrain-update-0.7.2.py"
    baseline_zip = workspace / "releases/devbrain-0.7.1.zip"
    manifest = json.loads(manifest_path.read_text())
    assert digest(baseline_zip.read_bytes()) == manifest["baselines"]["0.7.1"]["sha256"]
    args.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = args.root / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid4().hex[:8])
    directory.mkdir(mode=0o700)
    # Execute an immutable local snapshot even if a subsequent release build
    # refreshes the workspace artifacts while this validation is running.
    updater = directory / "devbrain-update-0.7.2.py"
    updater.write_bytes(updater_path.read_bytes())
    updater.chmod(0o600)
    source, data, state, isolated_home, tools, bins = (directory / name for name in
                                                     ("source", "data", "state", "home", "tools", "bin"))
    bins.mkdir()
    isolated_home.mkdir()
    shared = json.loads((args.shared_engine / "config.json").read_text())
    prefix = "devbrain_upgrade072_" + uuid4().hex[:10]
    inherited_home = Path.home()
    uv = shutil.which("uv") or str(inherited_home / ".local/bin/uv")
    tmux = shutil.which("tmux")
    assert Path(uv).is_file() and tmux
    dist = args.shared_engine / "runtime/arcadedb-26.11.1"
    assert (dist / "lib").is_dir()
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("DEVBRAIN_") and key not in
           {"UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "TMUX", "TMUX_PANE"}}
    env.update(HOME=str(isolated_home), DEVBRAIN_HOME=str(data), UV_TOOL_DIR=str(tools),
               UV_TOOL_BIN_DIR=str(bins), UV_CACHE_DIR=str(inherited_home / ".cache/uv"), UV_OFFLINE="true",
               XDG_STATE_HOME=str(state), DEVBRAIN_ARCADE_DIST=str(dist), DEVBRAIN_ARCADE_DB_PREFIX=prefix,
               DEVBRAIN_ARCADE_URL=shared["arcade_url"], DEVBRAIN_ARCADE_USER=shared["arcade_user"],
               DEVBRAIN_ARCADE_PASSWORD=shared["arcade_password"],
               JAVA_TOOL_OPTIONS="-Xmx1g", COPILOT_SKIP_CLI_DOWNLOAD="1",
               PATH=str(bins) + os.pathsep + str(Path(uv).parent) + os.pathsep + os.environ["PATH"],
               LANG="C.UTF-8", FIXTURE_071_NATIVE_TRACE=str(directory / "native-trace.jsonl"))
    for name in ("codex", "claude"):
        fake = bins / name
        fake.write_text("#!" + sys.executable + "\n" + helpers.FAKE_NATIVE)
        fake.chmod(0o700)
    binary, python = bins / "devbrain", tools / "devbrain/bin/python"
    checks = {}
    report = {"status": "running", "from_version": "0.7.1", "to_version": "0.7.2",
              "fixture_directory": str(directory), "baseline_sha256": digest(baseline_zip.read_bytes()),
              "updater_sha256": digest(updater.read_bytes()), "manifest_sha256": digest(manifest_path.read_bytes()),
              "release_zip_sha256": digest((workspace / "releases/devbrain-0.7.2.zip").read_bytes()),
              "scope": "real_uv_tool_upgrade_backup_preservation_idempotence",
              "model_inference": False, "checks": checks}
    count = 0

    def run(command, label, timeout=300, expected=0):
        nonlocal count
        count += 1
        log = directory / f"{count:02d}-{label}.log"
        print(json.dumps({"phase": label, "log": str(log)}), flush=True)
        result = subprocess.run(list(map(str, command)), cwd=directory, env=env, check=False,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        log.write_text(result.stdout + result.stderr)
        log.chmod(0o600)
        assert result.returncode == expected, (label, result.returncode, str(log))
        return result

    seeded = False
    try:
        helpers.extract(baseline_zip, source)
        write_json(data / "config.json", {"port": free_port(), "cockpit_port": free_port(),
                                         "ollama_url": "http://127.0.0.1:1", "poll_seconds": 3600})
        run(["bash", source / "scripts/install-wsl.sh"], "baseline-install-real-uv", timeout=600)
        project, neighbor = directory / "project", directory / "neighbor"
        project.mkdir()
        neighbor.mkdir()
        baseline = directory / "baseline.json"
        run([python, "-I", "-B", "-c", helpers.SEED, data, project, neighbor, baseline, "0.7.1", free_port()],
            "seed-owned-database")
        seeded = True
        protected = {
            data / "config.json": (data / "config.json").read_bytes(),
            data / "models.lock.json": b'{"canary":"existing model metadata"}\n',
            project / ".codex/config.toml": b'approval_policy="on-request"\nsandbox_mode="workspace-write"\n',
            project / ".claude/settings.local.json": b'{"permissions":{"deny":["Bash(rm:*)"]}}\n',
            isolated_home / ".codex/auth.json": b'{"fixture_auth":"PRIVATE_AUTH_CANARY_071"}\n',
            isolated_home / ".claude/.credentials.json": b'{"fixture_auth":"PRIVATE_AUTH_CANARY_071"}\n',
            data / "runtime/copilot/config.json": b'{"fixture_auth":"PRIVATE_AUTH_CANARY_071"}\n',
        }
        for path, raw in protected.items():
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_bytes(raw)
            path.chmod(0o600)
        keys = data / "credentials/api-keys.json"
        write_json(keys, {"version": 1, "keys": {"upgrade-key": "API_PRIVATE_CANARY_071"}})
        keys.parent.chmod(0o700)
        protected[keys] = keys.read_bytes()
        preferences = data / "cockpit/preferences.json"
        write_json(preferences, {"version": 2, "profiles": [
            {"id": "codex", "client": "codex", "label": "Custom Codex", "model": "native-fixture"},
            {"id": "claude", "client": "claude", "label": "Custom Claude"},
            {"id": "upgrade-key", "client": "api", "label": "Preserve API", "provider": "openai",
             "model": "fixture-model", "credential_ref": "profile:upgrade-key"},
            {"id": "copilot", "client": "copilot", "label": "Preserve Copilot", "model": "fixture-model"}],
            "scan_roots": [], "default_slots": [{"id": "codex", "profile_id": "codex"},
                                                {"id": "claude", "profile_id": "claude"}],
            "project_slots": {}, "last_project": None})
        protected[preferences] = preferences.read_bytes()
        before_source = helpers.inventory(source)
        run([sys.executable, updater, "--source-dir", source, "--check-only"], "check-only")
        assert helpers.inventory(source) == before_source and not state.exists()
        checks["check_only_nonmutating"] = True
        run([sys.executable, updater, "--source-dir", source], "apply-exact-updater", timeout=600)
        probe_script = helpers.PROBE.replace("=='0.7.1'", "=='0.7.2'").replace("'version':'0.7.1'", "'version':'0.7.2'")
        probe = json.loads(run([python, "-I", "-B", "-c", probe_script, data, baseline, source],
                               "probe-installed-package-and-memory").stdout)
        checks.update({key: value for key, value in probe.items() if key != "database"})
        for path, raw in protected.items():
            assert path.read_bytes() == raw and path.stat().st_mode & 0o777 == 0o600, str(path)
        checks["profiles_keys_auth_config_models_permissions_preserved"] = len(protected)
        for row in manifest["files"]:
            assert digest((source / row["path"]).read_bytes()) == row["after"], row["path"]
        checks["verified_updated_source_files"] = len(manifest["files"])
        backups = list((state / "devbrain-updates").glob("*/database.devbrain.json.gz"))
        assert len(backups) == 1
        with gzip.open(backups[0], "rt") as handle:
            backup = json.load(handle)
        assert backup["payload"]["format_version"] == 5
        assert backup["sha256"] == digest(json.dumps(backup["payload"], ensure_ascii=False,
            separators=(",", ":"), sort_keys=True).encode())
        old = json.loads(baseline.read_text())["export"]
        for key, rows in old.items():
            if key not in {"format_version", "exported_at"}:
                assert backup["payload"][key] == rows, key
        assert "API_PRIVATE_CANARY_071" not in json.dumps(backup)
        assert "PRIVATE_AUTH_CANARY_071" not in json.dumps(backup)
        checks["database_backup_exact_private_and_excludes_credentials"] = True
        assert backups[0].stat().st_mode & 0o777 == 0o600
        saved = json.loads((backups[0].parent / "manifest.json").read_text())
        with zipfile.ZipFile(baseline_zip) as archive:
            for entry in saved:
                path = backups[0].parent / entry["path"]
                if entry["existed"]:
                    assert path.read_bytes() == archive.read("devbrain/" + entry["path"])
                    assert path.stat().st_mode & 0o777 == 0o600
        checks["original_source_backup_exact_and_private"] = len(saved)
        paths = [*source.rglob("*"), *state.rglob("*"), *protected]
        before = helpers.file_signatures(paths)
        reapplied = run([sys.executable, updater, "--source-dir", source], "idempotent-reapply")
        assert "pliki i uslugi pozostaly bez zmian" in reapplied.stdout
        assert helpers.file_signatures(paths) == before
        checks["reapply_preserves_bytes_modes_mtimes_inodes"] = len(before)
        assert json.loads(run([binary, "version"], "installed-version").stdout)["version"] == "0.7.2"
        report["status"] = "passed"
    except Exception as exc:
        import traceback
        report.update(status="failed", failure_type=type(exc).__name__, failure=str(exc))
        (directory / "traceback.txt").write_text(traceback.format_exc())
    finally:
        if seeded:
            try:
                cleanup = run([python, "-I", "-B", "-c", helpers.CLEANUP, data, prefix], "cleanup-owned-database")
                checks.update(json.loads(cleanup.stdout))
            except Exception as exc:
                report.update(status="failed", cleanup_error=str(exc))
        write_json(directory / "report.json", report)
        write_json(workspace / "evidence/devbrain-0.7.2-upgrade-smoke.json", report)
        print(json.dumps({"status": report["status"], "report": str(directory / "report.json")}), flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
