"""Build cross-release contract test fixtures from published Cairnmind wheels (#249).

Usage:
  python scripts/build_release_fixtures.py --version 0.10.0 --download-release
  python scripts/build_release_fixtures.py --version 0.13.0 --wheel dist/cairnmind-0.13.0-py3-none-any.whl
  python scripts/build_release_fixtures.py --build-schema-v3

Each release directory under tests/fixtures/releases/<version>/ holds:
  - manifest.json            (release metadata, wheel SHA-256, schema version, capabilities)
  - raw_store.json.gz        (raw ArcadeDB rows before any migration on a newer tree)
  - expected_export.json     (normalized Store.export() baseline of that release)
  - backup.cairnmind.json.gz (logical backup created by Store.backup() of that release)
  - portable_memory/         (.memory/cairnmind/ package written by that release)
  - published_memory/        (.memory/cairnmind-published/ directory, when supported by the release)
"""
from __future__ import annotations

import argparse
from contextlib import closing
import gzip
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import textwrap
import urllib.request
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

RAW_TABLES = (
    "devbrain_meta",
    "projects",
    "project_roots",
    "sources",
    "chunks",
    "records",
    "record_revisions",
    "jobs",
    "embeddings",
    "import_cursors",
    "ingestion_inbox",
    "vector_indexes",
    "plans",
    "plan_revisions",
    "plan_requests",
    "worktree_states",
    "sessions",
    "resume_packets",
    "resume_acks",
    "session_events",
    "check_runs",
    "memory_settings",
    "memory_snapshots",
    "memory_imports",
    "memory_bindings",
    "journal_state",
    "journal_events",
    "nodes",
    "edges",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _download_release_wheel(version: str, staging: Path) -> tuple[Path, str]:
    wheel_name = f"cairnmind-{version}-py3-none-any.whl"
    sums_path = staging / "SHA256SUMS"
    wheel_path = staging / wheel_name
    base_url = f"https://github.com/piotrwozn/cairnmind/releases/download/v{version}"
    for url, dest in ((f"{base_url}/SHA256SUMS", sums_path), (f"{base_url}/{wheel_name}", wheel_path)):
        with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 - pinned github.com release URL
            dest.write_bytes(resp.read())
    expected = None
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1].lstrip("*") == wheel_name:
            expected = parts[0].lower()
            break
    if not expected:
        raise ValueError(f"{wheel_name} missing from SHA256SUMS for v{version}")
    actual = _sha256_file(wheel_path)
    if actual != expected:
        raise ValueError(f"SHA-256 mismatch for {wheel_name}: {actual} != {expected}")
    return wheel_path, actual


def _inner_driver_code() -> str:
    return textwrap.dedent("""
        from __future__ import annotations
        import gzip
        import json
        import shutil
        import subprocess
        import sys
        from pathlib import Path

        from cairnmind.config import Settings
        from cairnmind.ingest import index_project
        from cairnmind.memory import MemoryManager
        from cairnmind.sql import clean_row
        from cairnmind.store import Store

        work = Path(sys.argv[1])
        out = Path(sys.argv[2])
        version = sys.argv[3]

        data_dir = work / "data"
        repo = work / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "fixture@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture Builder"], check=True)
        (repo / "src").mkdir(exist_ok=True)
        (repo / "src" / "app.py").write_text(
            "def add(a, b):\\n    return a + b\\n",
            encoding="utf-8",
        )
        (repo / "README.md").write_text("# Fixture App\\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "src/app.py", "README.md"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "initial"], check=True)

        store = Store(data_dir)
        project = store.add_project("fixture-app", repo)
        pid = project["id"]
        index_project(store, pid)
        src_app = next(
            row for row in store.query(
                "SELECT FROM sources WHERE project_id=:pid AND is_current=true AND kind='code'",
                {"pid": pid},
            )
            if "app.py" in row.get("uri", "")
        )
        src_note = store.snapshot(
            pid,
            f"workspace://finding/release-{version}",
            "Keep portable snapshots backwards compatible across releases.",
            kind="manual",
            metadata={"origin": "workspace_finding", "root": str(repo)},
        )
        try:
            from cairnmind import person_proof
            person_proof._PROVEN.set(True)
        except (ImportError, AttributeError):
            pass
        rec = store.remember(
            pid,
            {
                "kind": "decision",
                "key": "keep-contract",
                "content": "Keep portable snapshots backwards compatible across releases.",
                "status": "proposed",
                "metadata": {"root": str(repo), "evidence": "def add(a, b):"},
            },
            [src_note["id"], src_app["id"]],
        )
        try:
            from cairnmind.inspector import Inspector
            Inspector(store).review(pid, rec["id"], "confirm", rec["version"])
        except (ImportError, AttributeError, ValueError):
            pass
        store.remember(
            pid,
            {
                "kind": "fact",
                "key": "adder-exists",
                "content": "src/app.py defines add(a, b).",
                "status": "proposed",
                "metadata": {"root": str(repo), "evidence": "def add(a, b):"},
            },
            [src_app["id"]],
        )

        # Portable memory package
        MemoryManager(store).snapshot(pid, repo)
        portable_dst = out / "portable_memory"
        if portable_dst.exists():
            shutil.rmtree(portable_dst)
        shutil.copytree(repo / ".memory" / "cairnmind", portable_dst)

        # Published team memory (added in 0.11.0 / #170)
        has_published = False
        try:
            from cairnmind.memory_published import Publisher
            published_res = Publisher(store).publish(pid, repo)
            assert published_res["records"] >= 1, published_res
            published_dst = out / "published_memory"
            if published_dst.exists():
                shutil.rmtree(published_dst)
            shutil.copytree(repo / ".memory" / "cairnmind-published", published_dst)
            has_published = True
        except ImportError:
            pass

        # Backup and export
        backup_dst = out / "backup.cairnmind.json.gz"
        store.backup(backup_dst)
        exported = store.export()
        (out / "expected_export.json").write_text(
            json.dumps(exported, ensure_ascii=False, indent=2, sort_keys=True) + "\\n",
            encoding="utf-8",
        )

        # Raw ArcadeDB tables dump
        tables = {}
        existing_types = {
            row.get("name")
            for row in store.query("SELECT name FROM schema:types")
            if isinstance(row.get("name"), str)
        }
        for name in sorted(existing_types):
            rows = []
            for row in store.query(f"SELECT FROM {name}"):
                cleaned = clean_row(row)
                cleaned.pop("@rid", None)
                cleaned.pop("@type", None)
                cleaned.pop("@cat", None)
                rows.append(cleaned)
            tables[name] = rows
        meta = {
            row.get("name"): row.get("value")
            for row in store.query("SELECT FROM devbrain_meta")
            if isinstance(row.get("name"), str)
        }
        with gzip.open(out / "raw_store.json.gz", "wt", encoding="utf-8") as gz:
            json.dump({"tables": tables, "meta": meta}, gz, ensure_ascii=False, sort_keys=True)

        summary = {
            "version": version,
            "schema_version": meta.get("schema_version"),
            "lookup_indexes": meta.get("lookup_indexes"),
            "has_published_memory": has_published,
            "project_id": pid,
            "repo_path": str(repo),
            "tables": sorted(tables),
        }
        (out / "_summary.json").write_text(json.dumps(summary, indent=2) + "\\n", encoding="utf-8")
    """).strip() + "\n"


def _start_ephemeral_engine(settings):
    from cairnmind.engine import ensure_engine, install_engine

    dist = os.environ.get("CAIRNMIND_ARCADE_DIST")
    if dist:
        install_engine(settings, Path(dist))
    ensure_engine(settings)


def build_from_wheel(version: str, wheel_path: Path, wheel_sha256: str, output_dir: Path) -> Path:
    from cairnmind.config import Settings
    from cairnmind.engine import stop_engine

    dest = output_dir / version
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"cm-fixture-{version}-") as tmp:
        tmp_path = Path(tmp)
        engine_dir = tmp_path / "engine-data"
        port = _free_port()
        settings = Settings.load(engine_dir, create=True)
        settings.arcade_url = f"http://127.0.0.1:{port}"
        settings.arcade_database = f"cairnmind_fixture_{version.replace('.', '_')}"
        settings.save()
        _start_ephemeral_engine(settings)
        try:
            venv_dir = tmp_path / "venv"
            subprocess.run(["uv", "venv", str(venv_dir)], check=True)
            py = venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
            subprocess.run(["uv", "pip", "install", "--python", str(py), str(wheel_path)], check=True)

            driver = tmp_path / "driver.py"
            driver.write_text(_inner_driver_code(), encoding="utf-8")
            work = tmp_path / "work"
            work.mkdir()
            env = {
                **os.environ,
                "CAIRNMIND_ARCADE_URL": settings.arcade_url,
                "CAIRNMIND_ARCADE_PASSWORD": settings.arcade_password,
                "CAIRNMIND_ARCADE_DATABASE": settings.arcade_database,
            }
            for drop_key in ("CAIRNMIND_HOME", "DEVBRAIN_HOME", "CAIRNMIND_SESSION_ID", "CAIRNMIND_PACKET_ID"):
                env.pop(drop_key, None)
            subprocess.run([str(py), str(driver), str(work), str(dest), version], env=env, check=True)
        finally:
            stop_engine(settings)

    summary = json.loads((dest / "_summary.json").read_text(encoding="utf-8"))
    (dest / "_summary.json").unlink(missing_ok=True)
    manifest = {
        "version": version,
        "wheel": wheel_path.name,
        "wheel_sha256": wheel_sha256,
        "schema_version": summary["schema_version"],
        "lookup_indexes": summary["lookup_indexes"],
        "has_published_memory": summary["has_published_memory"],
        "project_id": summary["project_id"],
        "repo_path": summary["repo_path"],
        "collections": summary["tables"],
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return dest


def build_schema_v3_fixture(output_dir: Path) -> Path:
    """Create a synthetic schema_version=3 raw fixture to test 3 -> 4 DDL upgrade and before-schema backup."""
    from cairnmind.arcade import ArcadeClient
    from cairnmind.config import Settings
    from cairnmind.engine import stop_engine
    from cairnmind.sql import clean_row
    from cairnmind.store import (
        EXPORT_TYPES,
        LEGACY_DOCUMENT_TYPES,
        V03_DOCUMENT_TYPES,
        V04_DOCUMENT_TYPES,
        V04_EXPORT_TYPES,
    )

    dest = output_dir / "schema_v3"
    dest.mkdir(parents=True, exist_ok=True)
    v3_docs = (*LEGACY_DOCUMENT_TYPES, *V03_DOCUMENT_TYPES, *V04_DOCUMENT_TYPES)
    evidence_content = "Legacy v3 evidence."
    evidence_sha = hashlib.sha256(evidence_content.encode("utf-8")).hexdigest()
    with tempfile.TemporaryDirectory(prefix="cm-fixture-v3-") as tmp:
        tmp_path = Path(tmp)
        port = _free_port()
        settings = Settings.load(tmp_path / "data", create=True)
        settings.arcade_url = f"http://127.0.0.1:{port}"
        settings.arcade_database = f"cairnmind_fixture_v3_{uuid4().hex[:8]}"
        settings.save()
        _start_ephemeral_engine(settings)
        try:
            with closing(ArcadeClient(settings)) as client:
                client.ensure_database()
                stmts = ["CREATE DOCUMENT TYPE devbrain_meta IF NOT EXISTS"]
                for collection in v3_docs:
                    stmts.append(f"CREATE DOCUMENT TYPE {collection} IF NOT EXISTS")
                stmts.extend(["CREATE VERTEX TYPE nodes IF NOT EXISTS", "CREATE EDGE TYPE edges IF NOT EXISTS"])
                for collection in V04_EXPORT_TYPES:
                    stmts.append(f"CREATE PROPERTY {collection}.id IF NOT EXISTS STRING")
                    stmts.append(f"CREATE INDEX IF NOT EXISTS ON {collection}(id) UNIQUE NULL_STRATEGY SKIP")
                    if collection != "projects":
                        stmts.append(f"CREATE PROPERTY {collection}.project_id IF NOT EXISTS STRING")
                        stmts.append(f"CREATE INDEX IF NOT EXISTS ON {collection}(project_id) NOTUNIQUE NULL_STRATEGY SKIP")
                for collection, field in (("projects", "name"), ("projects", "root"), ("project_roots", "path"),
                                          ("sources", "current_key"), ("records", "active_key"), ("jobs", "job_key"),
                                          ("import_cursors", "path")):
                    stmts.append(f"CREATE PROPERTY {collection}.{field} IF NOT EXISTS STRING")
                    stmts.append(f"CREATE INDEX IF NOT EXISTS ON {collection}({field}) UNIQUE NULL_STRATEGY SKIP")
                for collection in ("sources", "chunks", "nodes", "edges"):
                    stmts.append(f"CREATE PROPERTY {collection}.is_current IF NOT EXISTS BOOLEAN")
                for collection, field in (("chunks", "text"), ("records", "content")):
                    stmts.append(f"CREATE PROPERTY {collection}.{field} IF NOT EXISTS STRING")
                    stmts.append(f"CREATE INDEX IF NOT EXISTS ON {collection}({field}) FULL_TEXT METADATA {{similarity:'BM25'}}")
                client.script(";\n".join(stmts))
                pid = str(uuid4())
                sid = str(uuid4())
                rid = str(uuid4())
                with client.transaction() as tx:
                    tx.command("INSERT INTO devbrain_meta SET name='schema_version', value=3")
                    tx.command(
                        "INSERT INTO projects SET id=:id, name=:name, root=:root, created_at=:at",
                        {"id": pid, "name": "v3-app", "root": "/tmp/v3-app", "at": "2026-11-10T00:00:00+00:00"},
                    )
                    tx.command(
                        "INSERT INTO project_roots SET id=:id, project_id=:pid, path=:path",
                        {"id": str(uuid4()), "pid": pid, "path": "/tmp/v3-app"},
                    )
                    tx.command(
                        "INSERT INTO sources SET id=:id, project_id=:pid, uri=:uri, kind='manual', "
                        "content=:content, sha256=:sha, metadata={}, is_current=true, "
                        "current_key=:ck, created_at=:at",
                        {"id": sid, "pid": pid, "uri": "manual://v3", "content": evidence_content,
                         "sha": evidence_sha, "ck": f'["{pid}", "manual://v3"]',
                         "at": "2026-11-10T00:00:00+00:00"},
                    )
                    tx.command(
                        "INSERT INTO records SET id=:id, project_id=:pid, kind='decision', key='v3-rule', "
                        "content='Preserve v3 data during migration.', status='confirmed', version=1, "
                        "source_ids=:sids, metadata={}, active_key=:ak, created_at=:at, updated_at=:at",
                        {"id": rid, "pid": pid, "sids": [sid], "ak": f'["{pid}", "decision", "v3-rule"]',
                         "at": "2026-11-10T00:00:00+00:00"},
                    )
                    tx.command(
                        "INSERT INTO record_revisions SET id=:id, project_id=:pid, record_id=:rid, version=1, "
                        "snapshot=:snap, created_at=:at",
                        {"id": f"{rid}:1", "pid": pid, "rid": rid,
                         "snap": {"id": rid, "project_id": pid, "kind": "decision", "key": "v3-rule",
                                  "content": "Preserve v3 data during migration.", "status": "confirmed",
                                  "version": 1, "source_ids": [sid], "metadata": {}},
                         "at": "2026-11-10T00:00:00+00:00"},
                    )
                tables = {}
                for name in ("devbrain_meta", *V04_EXPORT_TYPES):
                    rows = []
                    for row in client.query(f"SELECT FROM {name}"):
                        cleaned = clean_row(row)
                        cleaned.pop("@rid", None)
                        cleaned.pop("@type", None)
                        cleaned.pop("@cat", None)
                        rows.append(cleaned)
                    tables[name] = rows
                exported = {
                    "format_version": 5,
                    "backend": "arcadedb",
                    "exported_at": "2026-11-10T00:00:00+00:00",
                    "vector_data": {},
                    **{col: tables.get(col, []) for col in EXPORT_TYPES},
                }
                with gzip.open(dest / "raw_store.json.gz", "wt", encoding="utf-8") as gz:
                    json.dump({"tables": tables, "meta": {"schema_version": 3}}, gz, ensure_ascii=False, sort_keys=True)
                (dest / "expected_export.json").write_text(
                    json.dumps(exported, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                manifest = {
                    "version": "schema_v3",
                    "wheel": None,
                    "wheel_sha256": None,
                    "schema_version": 3,
                    "lookup_indexes": None,
                    "has_published_memory": False,
                    "project_id": pid,
                    "repo_path": "/tmp/v3-app",
                    "collections": sorted(tables),
                }
                (dest / "manifest.json").write_text(
                    json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
        finally:
            stop_engine(settings)
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build release contract fixtures for tests/fixtures/releases/.")
    parser.add_argument("--version", help="Release version string (e.g. 0.12.0 or 0.13.0).")
    parser.add_argument("--wheel", type=Path, help="Local wheel path to build fixtures from.")
    parser.add_argument("--download-release", action="store_true", help="Download wheel + SHA256SUMS from GitHub Releases.")
    parser.add_argument("--build-schema-v3", action="store_true", help="Build synthetic schema_version=3 upgrade fixture.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "tests" / "fixtures" / "releases",
        help="Destination directory for release fixtures.",
    )
    args = parser.parse_args(argv)

    if args.build_schema_v3:
        built = build_schema_v3_fixture(args.output_dir)
        print(f"Built schema_v3 fixture in {built}")
        return 0
    if not args.version or (bool(args.wheel) == bool(args.download_release)):
        parser.error("Provide --build-schema-v3 OR --version with exactly one of --wheel or --download-release.")
    if args.wheel:
        wheel = args.wheel.expanduser().resolve()
        built = build_from_wheel(args.version, wheel, _sha256_file(wheel), args.output_dir)
    else:
        with tempfile.TemporaryDirectory(prefix="cm-dl-") as staging:
            wheel, sha = _download_release_wheel(args.version, Path(staging))
            built = build_from_wheel(args.version, wheel, sha, args.output_dir)
    print(f"Built release {args.version} fixtures in {built}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
