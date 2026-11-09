#!/usr/bin/env python3
"""Run installed DevBrain's real recall evaluation with durable, content-free progress.

This is an explicit live-model command, never part of pytest. The original scorer
is called once per completed case and its exact return value is returned unchanged.
Only the production evaluation's synthetic database/worktrees are used.
"""
from __future__ import annotations

import argparse
import csv
import functools
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4


def durable_json(path: Path, value: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=".checkpoint-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def append_durable(path: Path, value: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Progress:
    def __init__(self, directory: Path, metadata: dict):
        self.directory = directory
        self.started = time.monotonic()
        self.cases = []
        self.state = {**metadata, "status": "running", "started_at": utc_now(),
                      "completed_cases": 0, "expected_cases": 20, "cases": self.cases}
        durable_json(directory / "checkpoint.json", self.state)

    def record_case(self, row: dict) -> None:
        entry = {"completed_at": utc_now(), "elapsed_run_seconds": round(time.monotonic() - self.started, 3),
                 **row}
        append_durable(self.directory / "cases.jsonl", entry)
        self.cases.append(entry)
        self.state["completed_cases"] = len(self.cases)
        durable_json(self.directory / "checkpoint.json", self.state)
        print(json.dumps({"case": row["id"], "status": row["status"], "completed": len(self.cases)}), flush=True)

    def wrap_score(self, original):
        @functools.wraps(original)
        def score(case, result, *, private_marker):
            scored = original(case, result, private_marker=private_marker)
            passed, reasons = scored
            answer_status = result.get("status")
            if answer_status not in {"complete", "partial", "unavailable"}:
                answer_status = "other"
            # Original score reasons are fixed strings. No questions, answers,
            # expected values, sources, markers or exception bodies are copied.
            self.record_case({"id": case.key, "critical": bool(case.critical),
                "status": "not_run" if answer_status == "unavailable" else "passed" if passed else "failed",
                "score_passed": passed, "score_reasons": list(reasons), "answer_status": answer_status,
                "checkpoint_source": "original_score_answer"})
            return scored
        return score


class ResourceMonitor:
    INTERVAL_SECONDS = 5

    def __init__(self, path: Path):
        self.path = path
        self.stop = threading.Event()
        self.errors = []
        self.samples = 0
        self.thread = threading.Thread(target=self._run, name="devbrain-recall-monitor", daemon=True)
        self.binary = shutil.which("nvidia-smi")
        fallback = Path("/usr/lib/wsl/lib/nvidia-smi")
        if self.binary is None and fallback.is_file():
            self.binary = str(fallback)

    @staticmethod
    def memory():
        wanted = {"MemTotal", "MemAvailable", "MemFree", "SwapTotal", "SwapFree", "Cached", "SReclaimable"}
        values = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            if key in wanted:
                values[key + "_kib"] = int(value.split()[0])
        values["uptime_seconds"] = float(Path("/proc/uptime").read_text().split()[0])
        return values

    def gpu(self):
        if self.binary is None:
            return {"status": "unavailable", "reason": "nvidia_smi_missing"}
        fields = ("index", "memory.total", "memory.used", "utilization.gpu", "temperature.gpu", "power.draw")
        try:
            process = subprocess.run([self.binary, "--query-gpu=" + ",".join(fields),
                                      "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.TimeoutExpired) as error:
            return {"status": "unavailable", "reason": type(error).__name__}
        if process.returncode:
            return {"status": "unavailable", "exit_code": process.returncode}
        rows = []
        for row in csv.reader(process.stdout.splitlines()):
            if len(row) != len(fields):
                return {"status": "unavailable", "reason": "unexpected_nvidia_smi_shape"}
            values = []
            for cell in row:
                try:
                    value = float(cell.strip())
                    values.append(value if math.isfinite(value) else None)
                except ValueError:
                    values.append(None)
            rows.append(dict(zip(fields, values)))
        return {"status": "observed", "units": {"memory": "MiB", "utilization": "percent",
                "temperature": "C", "power": "W"}, "devices": rows}

    def _run(self):
        while not self.stop.is_set():
            sampled = time.monotonic()
            try:
                append_durable(self.path, {"at": utc_now(), "wsl_memory": self.memory(), "gpu": self.gpu()})
                self.samples += 1
            except Exception as error:
                self.errors.append(type(error).__name__)
                return
            remaining = max(0, self.INTERVAL_SECONDS - (time.monotonic() - sampled))
            if self.stop.wait(remaining):
                return

    def finish(self):
        self.stop.set()
        # nvidia-smi has its own three-second deadline. This join never blocks
        # indefinitely; a daemon thread cannot survive the helper process.
        self.thread.join(timeout=6)
        return {"interval_seconds": self.INTERVAL_SECONDS, "samples": self.samples,
                "stopped": not self.thread.is_alive(), "errors": self.errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path.home() / ".local/share/devbrain")
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--work-root", type=Path, default=Path.home() / ".cache/devbrain-recall-validation")
    parser.add_argument("--expected-version", default="0.7.2")
    args = parser.parse_args()

    import devbrain
    from devbrain import evaluation
    from devbrain.config import Settings
    from devbrain.engine import start_engine

    installed = importlib.metadata.version("devbrain")
    package = Path(devbrain.__file__).resolve()
    if (installed != args.expected_version or devbrain.__version__ != args.expected_version
            or not package.is_relative_to(Path(sys.prefix).resolve())):
        parser.error("Run this helper with the installed DevBrain tool's Python after the expected version is deployed")
    settings = Settings.load(args.data_dir)
    identity = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid4().hex[:8]
    output = args.evidence_dir.expanduser().resolve() / ("local-recall-" + identity)
    work = args.work_root.expanduser().resolve() / identity
    for directory in (output, work):
        directory.mkdir(parents=True, mode=0o700)
    progress = Progress(output, {"devbrain_version": installed, "installed_package": str(package),
        "helper_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "kernel_release": os.uname().release,
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "model": settings.chat_model, "embedding_model": settings.embedding_model,
        "num_ctx": settings.num_ctx, "clients": False, "real_ollama": True,
        "scoring_unchanged": True, "project_content_in_checkpoints": False})
    original_score, original_tempdir = evaluation.score_answer, tempfile.tempdir
    monitor = ResourceMonitor(output / "resources.jsonl")
    result = None
    try:
        tempfile.tempdir = str(work)
        evaluation.score_answer = progress.wrap_score(original_score)
        monitor.thread.start()
        print(json.dumps({"stage": "starting_real_recall", "evidence": str(output),
                          "version": installed, "clients": False}), flush=True)
        start_engine(settings)
        # No injected Ollama, client runner, scorer result, answer or threshold.
        result = evaluation.run_evaluation(settings, clients=False, report_dir=output)
        completed = {row["id"] for row in progress.cases}
        for row in result["scenarios"]:
            if row["id"] not in completed:
                # Unavailable model/embedding setup can skip scoring entirely.
                progress.record_case({"id": row["id"], "status": row["status"],
                    "critical": row["critical"], "score_passed": None,
                    "checkpoint_source": "final_report_not_scored"})
        report_path = Path(result["report_path"])
        # The production report contains synthetic fixtures only. Flush its
        # existing exact contents durably; do not replace its scoring fields.
        durable_json(report_path, json.loads(report_path.read_text()))
        progress.state.update({key: result[key] for key in ("status", "attempted", "factual_accuracy",
                              "required_accuracy", "critical_facts_passed", "deterministic_checks")})
        progress.state["final_report"] = str(report_path)
    except BaseException as error:
        progress.state.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                              exception_type=type(error).__name__)
    finally:
        evaluation.score_answer = original_score
        tempfile.tempdir = original_tempdir
        progress.state["monitoring"] = monitor.finish() if monitor.thread.ident is not None else {"stopped": True}
        progress.state["finished_at"] = utc_now()
        progress.state["elapsed_seconds"] = round(time.monotonic() - progress.started, 3)
        durable_json(output / "checkpoint.json", progress.state)
        print(json.dumps({"status": progress.state["status"], "completed_cases": len(progress.cases),
                          "checkpoint": str(output / "checkpoint.json"),
                          "monitoring": progress.state["monitoring"]}), flush=True)
    monitoring = progress.state["monitoring"]
    return 0 if (result and progress.state["status"] == result["status"] == "passed"
                 and monitoring.get("stopped") and not monitor.errors) else 1


if __name__ == "__main__":
    raise SystemExit(main())
