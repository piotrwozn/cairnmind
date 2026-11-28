"""Playwright E2E helper: seed a real temporary SQLite store and spawn `cairnmind ui`."""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from cairnmind.config import Settings
from cairnmind.memory.confirmation import remember_as_person
from cairnmind.store import Store
from cairnmind.terminal import person_proof


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_tcp(host: str, port: int, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"Timed out waiting for {host}:{port}")


def main() -> int:
    tmp_root = Path(tempfile.mkdtemp(prefix="cairnmind-ui-e2e-"))
    proc: subprocess.Popen[str] | None = None
    try:
        data_dir = tmp_root / "data"
        repo_dir = tmp_root / "repo"
        repo_dir.mkdir(parents=True, exist_ok=True)
        (repo_dir / "app.py").write_text(
            "REQUEST_TIMEOUT = 30\nMAX_RETRIES = 3\n",
            encoding="utf-8",
        )

        settings = Settings.load(data_dir, create=True)
        settings.storage_backend = "sqlite"
        settings.knowledge_agent = "disabled"
        settings.save()

        store = Store(data_dir, settings=settings, backend="sqlite")
        try:
            project = store.add_project("e2e-demo", repo_dir)
            src = store.snapshot(
                project["id"],
                (repo_dir / "app.py").as_uri(),
                "REQUEST_TIMEOUT = 30\nMAX_RETRIES = 3\n",
                "code",
                {
                    "root": str(repo_dir),
                    "relative_path": "app.py",
                    "path": str(repo_dir / "app.py"),
                },
            )
            proposed = store.remember(
                project["id"],
                {
                    "kind": "decision",
                    "key": "e2e.proposed",
                    "content": "Default request timeout is 30 seconds",
                    "metadata": {
                        "source_path": "app.py",
                        "line_start": 1,
                        "line_end": 1,
                        "evidence": "REQUEST_TIMEOUT = 30",
                    },
                },
                [src["id"]],
            )
            token = person_proof._PROVEN.set(True)
            try:
                confirmed = remember_as_person(
                    store,
                    project["id"],
                    {
                        "kind": "decision",
                        "key": "e2e.confirmed",
                        "status": "confirmed",
                        "content": "Max retries is locked at 3",
                        "metadata": {
                            "source_path": "app.py",
                            "line_start": 2,
                            "line_end": 2,
                            "evidence": "MAX_RETRIES = 3",
                        },
                    },
                    [src["id"]],
                )
            finally:
                person_proof._PROVEN.reset(token)
        finally:
            store.close()

        port = _free_port()
        env = {
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "CAIRNMIND_STORAGE_BACKEND": "sqlite",
        }
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "cairnmind",
                "--data-dir",
                str(data_dir),
                "ui",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-open",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            env=env,
        )
        stdout_pipe = proc.stdout
        if stdout_pipe is None:
            raise RuntimeError("cairnmind ui subprocess missing stdout pipe")
        bootstrap_url = ""
        url_rx = re.compile(r"(http://127\.0\.0\.1:\d+/auth/bootstrap\?token=[^\s]+)")
        deadline = time.monotonic() + 25.0
        output_lines: list[str] = []
        while time.monotonic() < deadline:
            line = stdout_pipe.readline()
            if not line:
                if proc.poll() is not None:
                    raise RuntimeError(f"cairnmind ui exited early ({proc.returncode}): {''.join(output_lines)}")
                time.sleep(0.05)
                continue
            output_lines.append(line)
            match = url_rx.search(line)
            if match:
                bootstrap_url = match.group(1)
                break
        if not bootstrap_url:
            raise RuntimeError(f"Did not observe bootstrap URL from cairnmind ui: {''.join(output_lines)}")

        def _drain_stdout() -> None:
            for extra_line in stdout_pipe:
                sys.stderr.write(f"[cairnmind ui] {extra_line}")
                sys.stderr.flush()

        threading.Thread(target=_drain_stdout, daemon=True).start()

        _wait_tcp("127.0.0.1", port, timeout=20.0)
        ready = {
            "bootstrapUrl": bootstrap_url,
            "baseUrl": f"http://127.0.0.1:{port}",
            "projectId": project["id"],
            "proposedDecisionId": proposed["id"],
            "confirmedDecisionId": confirmed["id"],
        }
        print(f"E2E_READY {json.dumps(ready)}", flush=True)

        # Keep serving until Playwright closes stdin or sends SIGTERM.
        with contextlib.suppress(KeyboardInterrupt):
            sys.stdin.read()
        return 0
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
