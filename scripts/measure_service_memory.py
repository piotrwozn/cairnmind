#!/usr/bin/env python3
"""Where the knowledge service's memory goes (#410). For maintainers; needs the database engine, Linux or WSL.

    python scripts/measure_service_memory.py --out memory.json            # the whole series, about 2 hours
    python scripts/measure_service_memory.py --quick --steps 1,3 --no-ollama   # a short run, no model server asked

The project is a copy of this repository, in a temporary directory with a database engine and ports of its own;
nothing of an installation is touched and everything is removed at the end. One service is started and kept for the
steps 1, 2, 3, 5, 6, 7 and 8; step 4 needs a different configuration (the knowledge agent disabled) and so a second,
fresh service. After every step the service's resident memory (VmRSS, VmHWM from /proc) is read, and again after an
idle wait, together with Python's own view (tracemalloc and the object counts after a collection; the service runs
under this script's launcher, the product is not changed). Each repetition of a step is read too, so a step up once
shows apart from a slope per edit or per answer.

    1 start and first scan   2 embedding queue to empty   3 one-file edits   4 answers without a model
    5 answers with the chat model   6 `status` and inspector graph requests   7 long idle
    8 the cockpit's broker asked for what a workspace asks most (#450; --repeat requests, in blocks of --block; the
      broker runs under a launcher with tracemalloc as the service does; --trace-at reads the allocation sites)

Steps 2 and 5 ask the model server of the settings (an Ollama on 127.0.0.1:11434 by default): inference only,
nothing is pulled or changed there. --no-ollama skips what needs it (2, 5) and points the service at a closed port.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import time

FORBIDDEN_PORTS = {8765, 8766, 2480}
MB = 1024 * 1024
STEP_NAMES = {1: "start and first scan", 2: "embedding queue to empty", 3: "one-file edits",
              4: "answers without a model", 5: "answers with the chat model", 6: "status and graph requests",
              7: "long idle", 8: "cockpit broker requests"}
QUESTIONS = [
    "Where is the tree signature of a project computed and what does it contain?",
    "What does the service do when the database engine stops answering?",
    "How are citations of an answer checked against the files on disk?",
    "Which setting controls how often the service looks for changed files?",
    "What happens to a record when its source is superseded?",
    "How does the service decide that only one process may write the store?",
    "Where is the answer cache of the service kept and how large can it grow?",
    "How is a session acknowledged before a client may write a record?",
    "What does a backup of the store contain and where is it written?",
    "How does the scanner treat a folder that is not available on this machine?",
    "Which module parses source code into symbols and imports?",
    "How are embedding jobs scheduled and claimed from the queue?",
    "What marks a record as degraded evidence?",
    "How does the retriever combine full-text and vector matches?",
    "Where is the code graph written to the database?",
    "How does a restore wait for the index before writing edges?",
    "What limits the size of a file that is indexed?",
    "How does the service shut down when it is asked to stop?",
    "Which checks decide that a citation is current?",
    "How does the inspector read the graph of a project?"]


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Measure the memory of a throwaway knowledge service.")
    p.add_argument("--steps", default="1,2,3,5,6,7,4", help="steps in the order to run them (default: all; 4 runs last, on a second service)")
    p.add_argument("--repeat", type=int, default=20, help="repetitions of steps 3 to 6")
    p.add_argument("--idle", type=float, default=60, help="seconds of idle wait after each step")
    p.add_argument("--long-idle", type=float, default=1800, help="seconds of step 7")
    p.add_argument("--queue-timeout", type=float, default=7200, help="seconds step 2 waits for the queue")
    p.add_argument("--quick", action="store_true", help="3 repetitions, 5 s idle, 30 s long idle, 300 s queue wait")
    p.add_argument("--no-trace", action="store_true", help="no tracemalloc: the resident figures without its overhead")
    p.add_argument("--no-ollama", action="store_true", help="point the service at a closed port and skip steps 2 and 5")
    p.add_argument("--routes", default="graph", help="step 6's requests, taken in turn: graph, groups, neighbors, and the same "
                   "with the main root as worktree: scoped-graph, scoped-groups, scoped-neighbors (comma separated)")
    p.add_argument("--no-status", action="store_true", help="step 6 sends its graph requests only, none of `status` before them: the "
                   "service's pool of worker threads grows during them, as on a fresh service asked for the graph")
    p.add_argument("--block", type=int, default=50, help="requests per block of the resident-memory figures of steps 6 and 8")
    p.add_argument("--trace-at", default="", help="request numbers (comma separated) after which steps 6 and 8 read the allocation sites")
    p.add_argument("--second-root", action="store_true", help="register a copy of the project as a second root with files of its own")
    p.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1], help="the project to copy")
    p.add_argument("--workdir", type=Path, default=Path.home() / "tmp" / "cm-measure" / "h410")
    p.add_argument("--keep", action="store_true", help="keep the working directory")
    p.add_argument("--out", type=Path, help="write the results here (JSON); the table goes to standard output")
    p.add_argument("--serve-child", type=Path, help=argparse.SUPPRESS)
    p.add_argument("--broker-child", type=Path, help=argparse.SUPPRESS)
    p.add_argument("--index-child", type=Path, help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if args.quick:
        args.repeat, args.idle, args.long_idle, args.queue_timeout = 3, 5, 30, 300
    try:
        args.step_list = [int(part) for part in args.steps.split(",") if part]
        args.trace_points = [int(part) for part in args.trace_at.split(",") if part]
    except ValueError:
        p.error("--steps and --trace-at take numbers, for example 1,3,5")
    if args.block < 1:
        p.error("--block must be at least 1")
    if not args.step_list or any(step not in STEP_NAMES for step in args.step_list):
        p.error("--steps takes numbers from 1 to 8")
    if args.repeat < 1:
        p.error("--repeat must be at least 1")
    if not set(args.routes.split(",")) <= {"graph", "groups", "neighbors", "scoped-graph", "scoped-groups", "scoped-neighbors"}:
        p.error("--routes takes graph, groups, neighbors, scoped-graph, scoped-groups, scoped-neighbors")
    return args


def memory_figures(status_text: str) -> tuple[float, float, int]:
    """Resident MB, peak resident MB and threads from the text of /proc/PID/status."""
    values = {line.split(":")[0]: line.split()[1] for line in status_text.splitlines() if line.split(":")[0] in ("VmRSS", "VmHWM", "Threads")}
    return int(values["VmRSS"]) / 1024, int(values["VmHWM"]) / 1024, int(values["Threads"])


def slope(values: list[float]) -> float:
    """Megabytes per repetition, the least-squares line through the readings after each repetition."""
    return statistics.linear_regression(range(len(values)), values).slope if len(values) > 1 else 0.0


def tree_rss(pid: int) -> tuple[float, int]:
    """Resident MB of a process and every process below it, and how many that are (from /proc)."""
    parents = {}
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            with contextlib.suppress(OSError, ValueError, IndexError):
                parents[int(entry.name)] = int((entry / "stat").read_text().rpartition(")")[2].split()[1])
    found, stack = {pid}, [pid]
    while stack:
        parent = stack.pop()
        children = [child for child, owner in parents.items() if owner == parent and child not in found]
        found.update(children)
        stack.extend(children)
    total = 0.0
    for process in found:
        with contextlib.suppress(OSError, KeyError, ValueError):
            total += memory_figures(Path(f"/proc/{process}/status").read_text())[0]
    return total, len(found)


def request_blocks(readings: list[float], milliseconds: list[float], size: int = 50, threads: list[int] | None = None) -> dict:
    """Mean resident MB of each block of `size` requests, the threads at the end of each, and the median and worst time (#450)."""
    blocks = [round(statistics.mean(readings[start:start + size]), 1) for start in range(0, len(readings), size)]
    result = {"request_blocks_mb": blocks, "request_ms_median": round(statistics.median(milliseconds), 1),
              "request_ms_worst": round(max(milliseconds), 1)}
    if threads:
        result["request_blocks_threads"] = [threads[min(start + size, len(threads)) - 1] for start in range(0, len(threads), size)]
    return result


def free_port() -> int:
    while True:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port not in FORBIDDEN_PORTS:
            return port


def throwaway(workdir: Path) -> Path:
    """The directory, refused when it could be an installation's."""
    workdir = workdir.expanduser().resolve()
    if any(part in str(workdir) for part in (".local/share", ".local/state")) or workdir == Path.home():
        raise SystemExit(f"refused: {workdir} could be an installation's directory")
    return workdir


# ---- the service under a launcher: tracemalloc and a way to ask the process what it holds ----

def serve_child(data: Path, trace: bool, broker: bool = False) -> None:
    """Runs `serve` (or, for the broker, `cockpit serve`) of this directory with tracemalloc on.

    SIGUSR1 writes a short view, SIGUSR2 the allocation sites."""
    import collections
    import gc
    import threading
    import tracemalloc
    out = data / ("trace-broker" if broker else "trace")
    out.mkdir(exist_ok=True)
    state = {"n": 0, "snapshot": None, "types": collections.Counter()}
    if trace:
        tracemalloc.start(1)

    def write(full: bool) -> None:
        gc.collect()
        state["n"] += 1
        traced = tracemalloc.get_traced_memory() if trace else (0, 0)
        record = {"traced_mb": traced[0] / MB, "traced_peak_mb": traced[1] / MB, "gc_objects": len(gc.get_objects()),
                  "threads": threading.active_count()}
        if full:
            types = collections.Counter(type(o).__name__ for o in gc.get_objects())
            record["types"] = types.most_common(10)
            record["types_growth"] = [[name, n - state["types"][name]] for name, n in types.most_common(40) if n - state["types"][name]][:10]
            state["types"] = types
            if trace:
                snapshot = tracemalloc.take_snapshot().filter_traces([tracemalloc.Filter(False, tracemalloc.__file__)])
                def site(stat):
                    return str(stat.traceback[0]).replace(str(data), "<DATA>")
                record["top"] = [[round(s.size / MB, 3), s.count, site(s)] for s in snapshot.statistics("lineno")[:12]]
                if state["snapshot"] is not None:
                    record["growth"] = [[round(s.size_diff / MB, 3), s.count_diff, site(s)]
                                        for s in snapshot.compare_to(state["snapshot"], "lineno")[:12]]
                state["snapshot"] = snapshot
        target = out / f"{state['n']}.json"
        target.with_suffix(".tmp").write_text(json.dumps(record))
        target.with_suffix(".tmp").rename(target)

    signal.signal(signal.SIGUSR1, lambda *_: write(False))
    signal.signal(signal.SIGUSR2, lambda *_: write(True))
    from cairnmind.cli import app
    sys.argv = ["cairnmind", "--data-dir", str(data), *(["cockpit", "serve"] if broker else ["serve"])]
    app()


def index_child(data: Path) -> None:
    """The first index of the project in a process of its own, so the service starts as the store's only writer."""
    from cairnmind import ingest
    from cairnmind.config import Settings
    from cairnmind.store import Store
    store = Store(data, settings=Settings.load(data))
    print(json.dumps({k: v for k, v in ingest.index_project(store, store.list_projects()[0]["id"]).items() if k != "errors"}))


# ---- the parent: engine, service, steps ----

class Measurement:
    def __init__(self, args):
        self.args, self.work = args, throwaway(args.workdir)
        self.data, self.repo = self.work / "data", self.work / "repo"
        self.child = None
        self.client = None
        self.seqs: dict[str, int] = {}
        self.steps: list[dict] = []
        self.traces: list[dict] = []

    # setup
    def environment(self):
        return {k: v for k, v in os.environ.items() if not k.startswith(("CAIRNMIND_", "DEVBRAIN_")) or k.endswith("_ARCADE_DIST")}

    def setup(self):
        from cairnmind.config import Settings
        from cairnmind.engine import install_engine, runtime_dir, start_engine
        from cairnmind.store import Store
        shutil.copytree(self.args.repo, self.repo, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".*_cache", "node_modules"))
        git = ["git", "-C", str(self.repo), "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
               "-c", "user.name=Measure", "-c", "user.email=measure@example.invalid"]
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        for command in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "Copy"]):
            subprocess.run([*git, *command], check=True, capture_output=True, env=env)
        self.data.mkdir(mode=0o700)
        for name in list(os.environ):  # the settings are read from the directory only
            if name.startswith(("CAIRNMIND_", "DEVBRAIN_")) and not name.endswith("_ARCADE_DIST"):
                os.environ.pop(name)
        settings = Settings.load(self.data, create=True)
        settings.port, settings.cockpit_port = free_port(), free_port()
        settings.arcade_url = f"http://127.0.0.1:{free_port()}"
        settings.arcade_user, settings.arcade_database = "root", "cairnmind_measure_h410"
        settings.engine_mode, settings.knowledge_agent = "native", "enabled"
        settings.arcade_password = secrets.token_urlsafe(24)
        if self.args.no_ollama:
            settings.ollama_url = f"http://127.0.0.1:{free_port()}"  # a port nothing listens on
        settings.save()
        settings = Settings.load(self.data)
        if {settings.port, settings.cockpit_port} & FORBIDDEN_PORTS or settings.data_dir != self.data:
            raise SystemExit("refused: these settings are not the throwaway measurement's")
        self.settings = settings
        distribution = Path(os.environ.get("CAIRNMIND_ARCADE_DIST") or os.environ.get("DEVBRAIN_ARCADE_DIST") or runtime_dir(settings))
        install_engine(settings, distribution)
        start_engine(settings)
        store = Store(self.data, settings=settings)
        project = store.add_project("measure", self.repo)
        if self.args.second_root:  # a second root with files of its own, which a worktree's scope holds back
            second = self.work / "second"
            shutil.copytree(self.repo, second)
            store.add_project_root(project["id"], second)
        del store
        print(subprocess.run([sys.executable, __file__, "--index-child", str(self.data)], check=True, capture_output=True,
                             text=True, env=self.environment()).stdout.strip(), flush=True)

    def teardown(self):
        self.stop_service()
        from cairnmind import process_identity
        from cairnmind.engine import stop_engine
        pid = None
        with contextlib.suppress(OSError, ValueError, KeyError):
            pid = int(json.loads((self.data / "arcade" / "process.json").read_text())["pid"])
        with contextlib.suppress(Exception):
            stop_engine(self.settings)
        deadline = time.monotonic() + 60
        while pid and process_identity.alive(pid) and time.monotonic() < deadline:
            time.sleep(0.2)
        if not self.args.keep:
            shutil.rmtree(self.work, ignore_errors=True)

    # the service
    def start_service(self):
        import httpx
        self.seqs["trace"] = 0
        shutil.rmtree(self.data / "trace", ignore_errors=True)
        command = [sys.executable, __file__, "--serve-child", str(self.data)] + (["--no-trace"] if self.args.no_trace else [])
        self.child = subprocess.Popen(command, env=self.environment(), stdout=(self.work / "service.out").open("a"),
                                      stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        self.client = httpx.Client(base_url=self.settings.url, headers={"Authorization": f"Bearer {self.settings.token}"},
                                   timeout=600, trust_env=False)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                if self.client.get("/health", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.2)
        raise SystemExit("the service did not start; see " + str(self.work / "service.out"))

    def stop_service(self):
        if not self.child:
            return
        with contextlib.suppress(Exception):
            self.client.post("/shutdown", timeout=10)
        try:
            self.child.wait(60)
        except subprocess.TimeoutExpired:
            self.child.terminate()
            self.child.wait(30)
        self.child = None

    def status(self) -> dict:
        return self.client.get("/status").json()

    def wait_scan_rounds(self, rounds: int, timeout=900):
        seen, count, deadline = None, 0, time.monotonic() + timeout
        while count < rounds and time.monotonic() < deadline:
            now = self.status()["background"].get("last_scan_at")
            if now and now != seen:
                seen, count = now, count + 1
            time.sleep(1)

    def trace(self, full: bool, pid: int | None = None, folder: str = "trace") -> dict:
        number = self.seqs[folder] = self.seqs.get(folder, 0) + 1
        os.kill(pid or self.child.pid, signal.SIGUSR2 if full else signal.SIGUSR1)
        target = self.data / folder / f"{number}.json"
        deadline = time.monotonic() + 300
        while not target.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        return json.loads(target.read_text())

    def sample(self, full=False) -> dict:
        rss, peak, threads = memory_figures(Path(f"/proc/{self.child.pid}/status").read_text())
        python = self.trace(full)
        return {"rss_mb": round(rss, 1), "peak_mb": round(peak, 1), "threads": threads, **python}

    def run_step(self, number: int, action, count=None, idle=None, label=None):
        """Memory before, after each repetition, after the step and after the idle wait; allocation sites at the end."""
        idle = self.args.idle if idle is None else idle
        row = {"step": number, "name": label or STEP_NAMES[number], "repetitions": count, "before": self.sample(full=number == 1)}
        readings = []
        action(readings)
        row["after"] = self.sample()
        time.sleep(idle)
        row["idle"] = self.sample(full=True)
        row["rss_after_each_repetition"] = readings
        row["growth_per_repetition_mb"] = round(slope(readings[-count:] if count else readings), 3) if readings else None  # step 6: only its graph half
        if number == 6 and getattr(self, "request_ms", None):
            row.update(request_blocks(readings[-count:], self.request_ms, self.args.block, self.request_threads))
            row["request_traces"] = self.request_traces
        self.steps.append(row)
        print(json.dumps({k: v for k, v in row.items() if k != "idle"} | {"idle_rss_mb": row["idle"]["rss_mb"]}), flush=True)

    def rss(self) -> float:
        return round(memory_figures(Path(f"/proc/{self.child.pid}/status").read_text())[0], 1)

    # the steps
    def step1(self, readings):
        self.wait_scan_rounds(2)

    def step2(self, readings):
        deadline = time.monotonic() + self.args.queue_timeout
        while time.monotonic() < deadline:
            jobs = self.status()["pipeline_jobs"]
            waiting = sum(jobs[group][state] for group in ("indexing", "extraction") for state in ("pending", "running"))
            readings.append(self.rss())
            if not waiting:
                return
            time.sleep(30)

    def step3(self, readings):
        files = sorted((self.repo / "src" / "cairnmind").glob("*.py"))[: self.args.repeat]
        for number, path in enumerate(files):
            with path.open("a") as handle:
                handle.write(f"\n# measured edit {number}\n")
            self.wait_scan_rounds(2)  # the first round may have begun before the edit; the second saw it
            readings.append(self.rss())

    def ask(self, readings):
        for number in range(self.args.repeat):
            reply = self.client.post("/ask", json={"project": "measure", "question": QUESTIONS[number % len(QUESTIONS)]})
            if reply.status_code != 200:
                raise SystemExit(f"/ask answered {reply.status_code}: {reply.text[:200]}")
            readings.append(self.rss())

    def step6(self, readings):
        pid = self.client.get("/projects").json()[0]["id"]
        for _ in range(0 if self.args.no_status else self.args.repeat):
            self.client.get("/status").raise_for_status()
            readings.append(self.rss())
        node = next(row["id"] for row in self.client.get("/inspector/graph", params={"project": pid, "limit": 300}).json()["nodes"]
                    if row["kind"] != "group")
        paths = {"graph": ("/inspector/graph", {"limit": 300}), "groups": ("/inspector/graph/groups", {"limit": 100}),
                 "neighbors": (f"/inspector/graph/{node}/neighbors", {"limit": 100})}
        scoped = {"worktree": str(self.repo)}  # the main root of a project that has another
        paths.update({f"scoped-{name}": (path, {**params, **scoped}) for name, (path, params) in list(paths.items())})
        routes = [paths[name] for name in self.args.routes.split(",")]
        self.request_ms, self.request_threads, self.request_traces = [], [], {}
        for number in range(self.args.repeat):
            path, params = routes[number % len(routes)]
            started = time.perf_counter()
            self.client.get(path, params={"project": pid, **params}).raise_for_status()
            self.request_ms.append((time.perf_counter() - started) * 1000)
            figures = memory_figures(Path(f"/proc/{self.child.pid}/status").read_text())
            readings.append(round(figures[0], 1))
            self.request_threads.append(figures[2])
            if number + 1 in self.args.trace_points:
                self.request_traces[number + 1] = self.trace(True)

    def step8(self):
        """The cockpit's broker, started as the product starts it, asked what a workspace asks most (#450).

        Its resident memory and that of every process below it, in blocks of 50 requests, with the time per request."""
        from cairnmind.cockpit import _runtime_file, stop_cockpit
        from cairnmind.terminal_client import BrokerClient
        pid = self.client.get("/projects").json()[0]["id"]
        shutil.rmtree(self.data / "trace-broker", ignore_errors=True)
        self.seqs["trace-broker"] = 0
        command = [sys.executable, __file__, "--broker-child", str(self.data)] + (["--no-trace"] if self.args.no_trace else [])
        process = subprocess.Popen(command, env=self.environment(), stdout=(self.work / "cockpit.out").open("a"),
                                   stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 60
            while not _runtime_file(self.settings).exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.2)
            broker = BrokerClient(self.settings)
            node = next(row["id"] for row in broker.request("GET", "/api/inspector/graph", params={"project": pid, "limit": 300})["nodes"]
                        if row["kind"] != "group")
            # What a workspace sends most: its state and status, the graph, its groups and a node's neighbors.
            routes = [("/api/state", {}), ("/api/status", {}), ("/api/inspector/graph", {"project": pid, "limit": 2000}),
                      ("/api/inspector/graph/groups", {"project": pid, "limit": 100}),
                      (f"/api/inspector/graph/{node}/neighbors", {"project": pid, "limit": 100}), ("/api/brief", {"project": pid})]
            broker.request("GET", "/api/state")
            start, readings, milliseconds, alone, threads, traces = tree_rss(process.pid), [], [], [], [], {}
            for number in range(self.args.repeat):
                path, params = routes[number % len(routes)]
                started = time.perf_counter()
                broker.request("GET", path, params=params, timeout=600)
                milliseconds.append((time.perf_counter() - started) * 1000)
                readings.append(tree_rss(process.pid)[0])
                figures = memory_figures(Path(f"/proc/{process.pid}/status").read_text())
                alone.append(figures[0])
                threads.append(figures[2])
                if number + 1 in self.args.trace_points:
                    traces[number + 1] = self.trace(True, process.pid, "trace-broker")
            row = {"step": 8, "name": "cockpit broker: state, status, graph, groups, neighbors and brief requests", "repetitions": self.args.repeat,
                   "processes_at_start": start[1], "rss_at_start_mb": round(start[0], 1), "rss_after_mb": round(readings[-1], 1),
                   "processes_after": tree_rss(process.pid)[1], "broker_blocks_mb": request_blocks(alone, milliseconds, self.args.block)["request_blocks_mb"],
                   "growth_per_repetition_mb": round(slope(readings), 3), "request_traces": traces,
                   **request_blocks(readings, milliseconds, self.args.block, threads)}
            self.cockpit = row
            print(json.dumps(row), flush=True)
        finally:
            with contextlib.suppress(Exception):
                stop_cockpit(self.settings)
            try:
                process.wait(30)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(30)

    def step7(self, readings):
        end = time.monotonic() + self.args.long_idle
        while time.monotonic() < end:
            time.sleep(min(60, max(0.0, end - time.monotonic())))
            readings.append(self.rss())

    def series(self):
        steps = [s for s in self.args.step_list if s != 4 and not (self.args.no_ollama and s in (2, 5))]
        if steps:
            self.start_service()
            self.run_step(1, self.step1)  # the service has only just started: "before" is the process before its first scan
            for number in steps:
                if number == 8:
                    self.step8()
                elif number != 1:
                    self.run_step(number, {2: self.step2, 3: self.step3, 5: self.ask, 6: self.step6, 7: self.step7}[number],
                                  count=self.args.repeat if number in (3, 5, 6) else None,
                                  idle=self.args.idle)
            self.stop_service()
        if 4 in self.args.step_list:
            from cairnmind.config import Settings
            settings = Settings.load(self.data)
            settings.knowledge_agent = "disabled"
            settings.save()
            self.start_service()
            self.run_step(1, self.step1, label="start and first scan (second service, knowledge agent disabled)")
            self.run_step(4, self.ask, count=self.args.repeat)
            self.stop_service()

    def run(self):
        if not sys.platform.startswith("linux"):
            raise SystemExit("Measure on Linux or WSL2.")
        if self.work.exists():
            raise SystemExit(f"refused: {self.work} exists; remove it or give another --workdir")
        try:
            self.setup()
            self.series()
        finally:
            self.teardown()
        return {"steps": self.steps, "cockpit": getattr(self, "cockpit", None), "arguments": {k: str(v) for k, v in vars(self.args).items() if k != "step_list"}}


def table(result: dict) -> str:
    lines = ["| step | before MB | after MB | after idle MB | per repetition MB | peak MB | traced MB before / after idle | objects |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in result["steps"]:
        per = "n/a" if row["growth_per_repetition_mb"] is None else f"{row['growth_per_repetition_mb']:+.2f}"
        lines.append(f"| {row['step']} {row['name']} | {row['before']['rss_mb']} | {row['after']['rss_mb']} | {row['idle']['rss_mb']} | {per} | "
                     f"{row['idle']['peak_mb']} | {row['before']['traced_mb']:.1f} / {row['idle']['traced_mb']:.1f} | "
                     f"{row['before']['gc_objects']} / {row['idle']['gc_objects']} |")
    return "\n".join(lines)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.serve_child or args.broker_child:
        serve_child(args.serve_child or args.broker_child, not args.no_trace, broker=bool(args.broker_child))
        return 0
    if args.index_child:
        index_child(args.index_child)
        return 0
    result = Measurement(args).run()
    if args.out:
        args.out.write_text(json.dumps(result, indent=1))
    print(table(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
