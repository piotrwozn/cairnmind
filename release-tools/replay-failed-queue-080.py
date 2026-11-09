"""Replay the six historical failures on a fresh private copy, never a live service.

Run with the qualified 0.8 interpreter under run-supervised-080.ps1. Reports
contain identifiers, counters and fixed diagnostic codes, not source text or
original exception messages. A completed replay is not permission to retry
production jobs and is not an eight-hour stability attestation.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import socket
import sys
import time


def helper(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), Path(__file__).with_name(name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def private_root(path, run_id):
    """Reject reuse, symlinks, Windows paths and every production data directory."""
    expected = Path("/home/piotr/.cache/devbrain-qualification-080") / run_id / "queue-copy"
    if path != expected or path.exists() or path.is_symlink():
        raise ValueError("A new run-specific private queue-copy directory is required")
    if any(parent.is_symlink() for parent in path.parents):
        raise ValueError("Private working directories cannot use symlinks")
    return expected


def claim_selected(store, job_id, attempts, worker):
    """Claim exactly the reviewed retry, leaving the rest of the queue untouched."""
    from devbrain.agent import inference_lease_seconds
    from devbrain.store import _one, _set, _now

    with store.transaction() as tx:
        row = _one(tx, "SELECT FROM jobs WHERE id=:id", {"id": job_id}, False)
        if (not row or row["state"] != "pending" or row.get("attempts", 0) != attempts
                or row.get("worker") is not None or row.get("lease_until") is not None):
            raise ValueError("Selected retry changed before its claim")
        row.update(state="running", worker=worker, lease_until=time.time() + inference_lease_seconds(),
                   attempts=attempts + 1, diagnostics={}, updated_at=_now())
        _set(tx, "jobs", row, "id=:id", {"id": job_id})
        return row


def job_fingerprint(store, omit):
    rows = store.query("SELECT id,state,attempts,retry_count,worker,lease_until FROM jobs")
    return {row["id"]: {key: row.get(key) for key in
            ("state", "attempts", "retry_count", "worker", "lease_until")}
            for row in rows if row["id"] not in omit}


def duplicates(store):
    result = {}
    for table, columns in (("jobs", ("job_key",)),
                           ("sources", ("project_id", "uri", "sha256"))):
        rows = store.query("SELECT " + ",".join(columns) + " FROM " + table)
        counts = Counter(tuple(row.get(key) for key in columns) for row in rows)
        result[table] = sum(count - 1 for count in counts.values() if count > 1)
    return result


def job_result(store, job):
    row = store.job_status(job["project_id"], job["id"])
    # target/error/history may contain private paths or original errors.
    return {key: row.get(key) for key in
            ("id", "kind", "state", "attempts", "retry_count", "error_code", "diagnostics")}


def stop_owned_engine(handle, identity, scope, guard):
    poller = select.poll()
    poller.register(handle, select.POLLIN)
    guard.require(not poller.poll(0) and scope.owns(identity),
                  "Queue-copy engine exited or changed before controlled shutdown")
    # The admitted handle cannot target a later process that reuses its PID.
    signal.pidfd_send_signal(handle, signal.SIGTERM)
    guard.require(bool(poller.poll(30000)), "Owned queue-copy engine did not exit")
    result = {"engine_actual_exit_verified": True}
    try:
        child, status = os.waitpid(identity["pid"], os.WNOHANG)
    except ChildProcessError:
        child = 0  # Popen's child reaper may already have collected it.
    if child:
        code = os.waitstatus_to_exitcode(status)
        result["engine_returncode"] = code
        guard.require(code in (0, 143, -15), "Queue-copy engine shutdown had an unexpected result")
    return result


async def replay(store, settings, jobs, report, write):
    from devbrain.agent import KnowledgeAgent, extraction_outer_timeout
    from devbrain.ingest import import_history

    agent = KnowledgeAgent(store, settings)
    try:
        for original in jobs:
            before = original.get("attempts", 0)
            store.retry_job(original["project_id"], original["id"], before)
            job = claim_selected(store, original["id"], before, "qualification-" + report["run_id"])
            error = None
            try:
                if job["kind"] == "import":
                    payload = job["payload"]
                    if isinstance(payload, str):
                        payload = json.loads(payload)
                    path = Path(payload["path"])
                    if not path.is_absolute() or not path.is_file():
                        raise ValueError("Captured history is unavailable")
                    # The production scanner calls this same importer. No scan,
                    # scheduler, snapshot writer or background worker is started.
                    imported = await asyncio.to_thread(import_history, store, path,
                                                        job["project_id"], payload.get("client", "auto"))
                    if imported.get("errors"):
                        error = "; ".join(map(str, imported["errors"]))[:2000]
                elif job["kind"] == "extract":
                    await asyncio.wait_for(agent.extract(job), extraction_outer_timeout())
                else:
                    raise ValueError("Historical replay supports only import and extract jobs")
            except Exception as exc:
                # The private copy retains its original detailed diagnosis.
                # Only the fixed public categories below leave that directory.
                error = str(exc) or type(exc).__name__
            store.finish_job(job["id"], error, worker=job["worker"], expected_attempts=before + 1)
            result = job_result(store, job)
            result["previous_attempts"] = before
            if result["attempts"] != before + 1:
                raise ValueError("Retry did not execute exactly one attempt")
            report["jobs"].append(result)
            write()
            # Do not continue through an unresolved failure or uncertain result.
            if result["state"] != "completed":
                raise ValueError("Historical failure remains unresolved on the copy")
    finally:
        await agent.ollama.aclose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--gate-report", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--arcade-dist", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.run_id):
        parser.error("Invalid run identifier")
    if not args.execute:
        print(json.dumps({"execute": False, "scope": "historical_queue_copy_only", "expected_jobs": 6}))
        return 0

    common = helper("run-full-suite-080.py")
    guard = common.attestor()
    guard.require(sys.flags.isolated == 1 and sys.prefix != sys.base_prefix,
                  "Use the isolated qualified installation with Python -I")
    guard.require(os.environ.get("DEVBRAIN_QUALIFICATION_RUN_ID") == args.run_id,
                  "The independent supervised wrapper is required")
    gate = guard.document(args.gate_report)
    runtime = gate.get("runtime", {})
    guard.verify_gate(gate, runtime)
    guard.verify_runtime_files(runtime)
    guard.require(str(Path(sys.executable).resolve()) == runtime["python_executable"],
                  "The interpreter differs from the runtime gate")
    source = args.source_root.resolve(strict=True)
    origin = common.isolated_origin(source, guard)
    import devbrain
    guard.require(devbrain.__version__ == "0.8.0", "Replay requires the frozen 0.8.0 application")
    source_hashes = guard.source_manifest(source)
    guard.require(args.backup.is_file() and not args.backup.is_symlink()
                  and args.backup.resolve().is_relative_to(Path("/home/piotr/.local/state/devbrain-updates")),
                  "Use the preserved historical backup without following a linked file")
    root = private_root(args.data_dir, args.run_id)
    artifacts = args.artifact_root.resolve(strict=True)
    guard.require(artifacts.is_relative_to(Path("/mnt/c")) and not (artifacts / "controller-report.json").exists(),
                  "A new Windows-observed evidence directory is required")
    report = {"schema_version": 1, "scope": "devbrain_historical_queue_copy_replay",
              "run_id": args.run_id, "runtime": runtime, "status": "running", "jobs": [],
              "python_launcher": sys.executable,
              "cleanup_complete": False, "production_modified": False, "all_six_replayed": False,
              "backup_sha256": guard.digest(args.backup), "runtime_gate_sha256": guard.digest(args.gate_report),
              "source_root": str(source), "source_hashes": source_hashes, "isolated_package": origin}
    guard.require(report["backup_sha256"] == "698591d33252a1f0e802a01fac78feb90f56ef37fc0c5f717ed26667f635ac70",
                  "The preserved historical archive differs from the reviewed backup")

    def write():
        common.write(artifacts / "controller-report.json", report)

    write()
    settings = None
    engine_pidfd = None
    engine_identity = None
    supervision = helper("supervise-080.py")
    proof = supervision.read_json(artifacts / "mapping-proof.json")
    guard.require(proof.get("run_id") == args.run_id, "Scope proof belongs to another run")
    scope = supervision.ScopeOwnership(proof.get("scope_unit"))
    try:
        from devbrain.config import Settings
        from devbrain.engine import install_engine, managed_java_executable
        from devbrain.store import Store

        # Validate the original archive before starting a JVM or creating a copy.
        payload = Store._validated_backup(args.backup)
        jobs = sorted((row for row in payload["jobs"] if row["state"] == "failed"), key=lambda row: row["id"])
        guard.require(len(jobs) == 6, "The archive must contain the six reviewed historical failures")
        guard.require(Counter(row["kind"] for row in jobs) == {"import": 3, "extract": 3},
                      "The historical failure set differs from the reviewed baseline")
        del payload
        root.mkdir(parents=True, mode=0o700)
        os.chmod(root.parent, 0o700)
        copied = root / "original.devbrain.json.gz"
        shutil.copyfile(args.backup, copied)
        os.chmod(copied, 0o600)
        guard.require(guard.digest(copied) == report["backup_sha256"], "Backup changed during copying")
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        settings = Settings.load(root, create=True)
        settings.arcade_url = f"http://127.0.0.1:{port}"
        settings.java_executable = runtime["java_executable"]
        settings.tmux_executable = runtime["tmux_executable"]
        settings.save()
        install_engine(settings, args.arcade_dist)
        store = Store(root, settings=settings)
        guard.require(managed_java_executable(settings) == runtime["java_executable"], "No owned qualified engine")
        process = json.loads((root / "arcade/process.json").read_text())
        engine_identity = supervision.identity(process["pid"])
        guard.require(engine_identity and engine_identity["parent_pid"] == os.getpid()
                      and scope.owns(engine_identity)
                      and engine_identity["executable"] == runtime["java_executable"], "Queue-copy engine is not owned")
        engine_pidfd = os.pidfd_open(process["pid"])
        guard.require(scope.owns(engine_identity), "Queue-copy engine identity changed during admission")
        store.restore(copied)
        baseline = job_fingerprint(store, {row["id"] for row in jobs})
        before_duplicates = duplicates(store)
        report["counts_before"] = store.status()
        write()
        asyncio.run(replay(store, settings, jobs, report, write))
        after = job_fingerprint(store, {row["id"] for row in jobs})
        guard.require(all(after.get(key) == value for key, value in baseline.items()),
                      "An unrelated historical job changed")
        guard.require(duplicates(store) == before_duplicates, "The replay introduced duplicate jobs or sources")
        report.update(all_six_replayed=len(report["jobs"]) == 6, unrelated_jobs_unchanged=True,
                      duplicate_counts_before=before_duplicates, duplicate_counts_after=duplicates(store),
                      counts_after=store.status(), status="queue_copy_completed_pending_external_watch")
        guard.require(guard.source_manifest(source) == source_hashes, "The application changed during replay")
        guard.verify_runtime_files(runtime)
        guard.require(guard.digest(args.gate_report) == report["runtime_gate_sha256"], "Runtime gate changed")
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__)
    finally:
        if engine_pidfd is not None:
            try:
                report.update(stop_owned_engine(engine_pidfd, engine_identity, scope, guard))
                common.await_owned_exit(artifacts, args.run_id, guard)
                report["cleanup_complete"] = True
            except Exception as exc:
                report.update(status="failed", cleanup_error_type=type(exc).__name__)
            finally:
                os.close(engine_pidfd)
        elif settings is None:
            report["cleanup_complete"] = True
        write()
    return 0 if report["status"] == "queue_copy_completed_pending_external_watch" else 1


if __name__ == "__main__":
    raise SystemExit(main())
