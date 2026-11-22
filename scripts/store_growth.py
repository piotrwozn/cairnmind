"""Measure what an imported transcript event and a file revision add to the store, in bytes (#185).

    uv run python scripts/store_growth.py
    uv run python scripts/store_growth.py --events 100 --revisions 5 --json growth.json

It starts its own engine on a free loopback port in a new temporary data directory, writes synthetic
transcript events and file revisions through the product's own import and indexing, embeds every chunk with
a fake model that returns seeded pseudo-random vectors of the default dimension, and after each step reads
the rows and bytes of every collection (`store size`), the size of the database directory and the size of
one gzip backup. The Markdown table it prints gives the growth per event and per file revision as the
difference between two steps divided by the units written in the second one. No Ollama or other model is
used, and neither the default data directory nor its ports. See docs/evaluation/RESULTS.md for what it does
not show.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import random
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
EVENT_SIZES = (200, 1024, 8192)  # bytes of text of an event
FILE_SIZES = (2048, 20480, 102400)  # bytes of a file; the files take these in turn
DIMENSION = 1024  # what the default embedding model returns (to be confirmed on a real installation, #185)
PER_UNIT = ("database_bytes", "backup_bytes", "vector_bytes", "store_bytes", "sources", "chunks", "embeddings", "jobs")


class FakeEmbedder:
    """Seeded pseudo-random vectors: a constant would compress to nothing and make the backup figure false."""

    def __init__(self, dimension: int, seed: int):
        self.dimension, self.random = dimension, random.Random(seed)

    async def embed(self, texts):
        return [[self.random.random() - 0.5 for _ in range(self.dimension)] for _ in texts]

    async def model_identity(self, name):
        return f"growth-fake-{self.dimension}@1"


class Text:
    """Text of seeded pseudo-random words from one vocabulary: it compresses about as words of prose do."""

    def __init__(self, seed: int):
        self.random = random.Random(seed)
        self.vocabulary = ["".join(self.random.choices("abcdefghijklmnopqrstuvwxyz", k=self.random.randint(3, 10)))
                           for _ in range(2000)]

    def line(self, size: int) -> str:
        words, length = [], 0
        while length < size:
            words.append(self.random.choice(self.vocabulary))
            length += len(words[-1]) + 1
        return " ".join(words)[:size]


def embed_all(store, embedder) -> None:
    """Run every embed job as the service does: the model embeds the chunk and the job completes."""
    from cairnmind.retrieval import Retriever
    retriever = Retriever(store, store.settings, embedder)

    async def drain():
        while job := store.claim_job("store-growth", kinds=["embed"]):
            await retriever.embed_pending(job)
            store.finish_job(job["id"], worker=job["worker"], expected_attempts=job["attempts"])
    asyncio.run(drain())


def measured(store, scratch: Path, step: str, units: int, unit: str, settle=None) -> dict:
    """The state of the store after a step: `store size`, the database directory and one gzip backup.

    ``settle`` runs first when given and returns the size of the database directory: the engine keeps files it has
    not yet written while it runs (a 20 MiB difference on a nearly empty store), so the script stops it, reads the
    directory and starts it again."""
    from cairnmind import store_size
    on_disk = settle() if settle else None
    report = store_size.measure(store)
    collections = {row["name"]: {"rows": row["rows"], "bytes": row["bytes"]} for row in report["collections"]}
    vector_bytes = sum(row["bytes"] or 0 for row in report["collections"]
                       if row["name"] == "embeddings" or row["name"].startswith("vector_"))
    backup = store.backup(scratch / "backup" / "measure.cairnmind.json.gz")
    backup_bytes = backup.stat().st_size
    backup.unlink()
    return {"step": step, "units": units, "unit": unit, "collections": collections, "vector_bytes": vector_bytes,
            "store_bytes": sum(row["bytes"] or 0 for row in collections.values()),
            "database_bytes": report["disk"]["database"]["bytes"] if on_disk is None else on_disk,
            "backup_bytes": backup_bytes}


def per_unit(steps: list[dict]) -> None:
    """Add to each step what it added per unit: the difference to the step before, over its units."""
    for earlier, later in zip(steps, steps[1:]):
        def value(step, key):
            return step["collections"][key]["rows"] if key in step["collections"] else step[key]
        later["per_unit"] = {key: (value(later, key) - value(earlier, key)) / later["units"] for key in PER_UNIT}


def grow(store, scratch, *, events: int = 500, event_sizes=EVENT_SIZES, files: int = 10, file_sizes=FILE_SIZES,
         revisions: int = 20, dimension: int = DIMENSION, seed: int = 1, settle=None) -> dict:
    """Write the events and the revisions into ``store`` and return the steps. ``scratch`` must lie outside its data directory."""
    from cairnmind import ingest
    scratch = Path(scratch)
    root = scratch / "project"
    root.mkdir(parents=True, exist_ok=True)
    project = store.add_project("store-growth", root)["id"]
    text, embedder = Text(seed), FakeEmbedder(dimension, seed)
    steps = [measured(store, scratch, "empty store", 0, "-", settle)]
    for size in event_sizes:
        path = scratch / "transcripts" / f"events-{size}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as handle:
            for number in range(events):
                role = "user" if number % 2 == 0 else "assistant"
                body = text.line(size)
                handle.write(json.dumps({
                    "type": role, "sessionId": f"growth-{size}", "cwd": str(root), "uuid": f"{size}-{number}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "message": {"role": role, "content": body if role == "user" else [{"type": "text", "text": body}]},
                }) + "\n")
        done = ingest.import_history(store, path, project, "claude")
        if done["archived"] != events or done["errors"]:
            raise RuntimeError(f"The import archived {done['archived']} of {events} events: {done['errors'][:3]}")
        embed_all(store, embedder)
        steps.append(measured(store, scratch, f"{events} events of {size} B", events, "event", settle))
    names = [root / f"file{number:02d}.md" for number in range(files)]
    for round_number in range(revisions + 1):
        for number, name in enumerate(names):
            size = file_sizes[number % len(file_sizes)]
            if round_number == 0:
                lines = [text.line(60) for _ in range(max(1, size // 61))]
            else:  # a revision changes a tenth of the lines
                lines = name.read_text().split("\n")
                for index in text.random.sample(range(len(lines)), max(1, len(lines) // 10)):
                    lines[index] = text.line(60)
            name.write_text("\n".join(lines))
        done = ingest.index_project(store, project)
        if done["changed"] != files or done["errors"]:
            raise RuntimeError(f"The index changed {done['changed']} of {files} files: {done['errors'][:3]}")
        embed_all(store, embedder)
        steps.append(measured(store, scratch, "first index of the files" if round_number == 0 else f"revision {round_number}",
                              files, "file", settle))
    per_unit(steps)
    return {"events": events, "event_sizes": list(event_sizes), "files": files, "file_sizes": list(file_sizes),
            "revisions": revisions, "dimension": dimension, "seed": seed, "steps": steps}


def table(result: dict, header: dict) -> str:
    """The Markdown of the steps, headed by what the issue asks to state: date, commit and engine version."""
    lines = [f"Measured {header['date']} on commit {header['commit']}, ArcadeDB {header['engine']}; "
             f"fake vectors of {result['dimension']} numbers.", "",
             "| Step | Units | Database bytes | per unit | gzip backup bytes | per unit | Vector bytes | per unit | Chunks per unit |",
             "|---|---|---|---|---|---|---|---|---|"]
    for step in result["steps"]:
        added = step.get("per_unit")
        cells = [f"{added[key]:,.0f}" if added else "-" for key in ("database_bytes", "backup_bytes", "vector_bytes")]
        chunks = f"{added['chunks']:.1f}" if added else "-"
        lines.append(f"| {step['step']} | {step['units']} | {step['database_bytes']:,} | {cells[0]} | "
                     f"{step['backup_bytes']:,} | {cells[1]} | {step['vector_bytes']:,} | {cells[2]} | {chunks} |")
    return "\n".join(lines)


def commit() -> str:
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=15, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def numbers(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split(",") if part)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--events", type=int, default=500, help="events of each size (default 500)")
    parser.add_argument("--event-sizes", type=numbers, default=EVENT_SIZES, help="bytes of text, comma separated")
    parser.add_argument("--files", type=int, default=10, help="files, taking the sizes in turn (default 10)")
    parser.add_argument("--file-sizes", type=numbers, default=FILE_SIZES, help="bytes of a file, comma separated")
    parser.add_argument("--revisions", type=int, default=20, help="revisions of every file (default 20)")
    parser.add_argument("--dimension", type=int, default=DIMENSION, help="numbers in a fake vector (default 1024)")
    parser.add_argument("--json", type=Path, help="also write the whole report here")
    arguments = parser.parse_args()
    from cairnmind import process_identity
    from cairnmind.config import Settings, env_value
    from cairnmind.engine import install_engine, start_engine, stop_engine
    from cairnmind.store import Store
    with tempfile.TemporaryDirectory(prefix="cairnmind-growth-") as temporary:
        base = Path(temporary)
        data = base / "data"
        settings = Settings.load(data, create=True)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        settings.arcade_url = url
        settings.save()
        # An environment variable can name another database (a person's installation); this never uses one.
        settings = Settings.load(data)
        if settings.arcade_url != url or settings.arcade_connect_url:
            print("An environment variable names another database address; unset it: this script starts its own engine.",
                  file=sys.stderr)
            return 2
        distribution = env_value("ARCADE_DIST")
        install_engine(settings, Path(distribution) if distribution else None)
        directory = data / "arcade" / "databases" / settings.arcade_database

        def stop() -> None:
            pid = int(json.loads((data / "arcade" / "process.json").read_text())["pid"])
            stop_engine(settings)
            for _ in range(300):  # stop_engine returns when the port closes; the process still closes the database
                try:
                    if os.waitpid(pid, os.WNOHANG)[0] == pid:  # our child: alive() would see it as a zombie
                        return
                except ChildProcessError:
                    if not process_identity.alive(pid):
                        return
                time.sleep(0.1)
            raise RuntimeError("The engine did not exit")

        def settle() -> int:
            """The database directory as it is on disk with the engine stopped, then the engine again."""
            stop()
            size = sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())
            start_engine(settings)
            return size

        start_engine(settings)
        try:
            store = Store(data, settings=settings)
            try:
                result = grow(store, base / "scratch", events=arguments.events, event_sizes=arguments.event_sizes,
                              files=arguments.files, file_sizes=arguments.file_sizes, revisions=arguments.revisions,
                              dimension=arguments.dimension, settle=settle)
            finally:
                store.client.close()
        finally:
            stop()
    header = {"date": date.today().isoformat(), "commit": commit(), "engine": settings.arcade_version}
    result["header"] = header
    print(table(result, header))
    if arguments.json:
        arguments.json.write_text(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
