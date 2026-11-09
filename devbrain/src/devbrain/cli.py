"""DevBrain CLI; native coding clients retain their own terminal and permissions."""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
from contextlib import contextmanager
from functools import wraps
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Annotated

import httpx
import typer

from devbrain.config import Settings
from devbrain.ollama import OllamaUnavailable
from devbrain.arcade import ArcadeError

app = typer.Typer(no_args_is_help=False, pretty_exceptions_enable=False,
                  help="Terminalowe środowisko z pamięcią: Codex, Claude, Ollama, API i GitHub Copilot.")
projects_app = typer.Typer(help="Rejestr projektów i katalogów.")
tasks_app = typer.Typer(help="Wspólne kroki planów, statusy i dowody weryfikacji.")
models_app = typer.Typer(help="Lokalne modele Ollama.")
engine_app = typer.Typer(help="Natywny silnik ArcadeDB (Java 21+).")
plans_app = typer.Typer(help="Wersjonowane plany i deterministyczny następny krok.")
checks_app = typer.Typer(help="Nazwane sprawdzenia projektu i aktualność dowodów.")
sessions_app = typer.Typer(help="Pakiety wznowienia, zakres historii i potwierdzenia odbioru.")
eval_app = typer.Typer(help="Izolowana ewaluacja rzeczywistej pamięci.")
jobs_app = typer.Typer(help="Diagnostyka i jawne ponawianie zadań jednego projektu.")
memory_app = typer.Typer(help="Przenośna pamięć jednego projektu i lokalny harmonogram snapshotów.")
cockpit_app = typer.Typer(help="Lokalny kokpit, trwałe terminale i podgląd pamięci.")
workspace_app = typer.Typer(help="Terminalowe środowisko pracy: stan, odłączanie i zatrzymanie.")
app.add_typer(projects_app, name="project")
app.add_typer(tasks_app, name="task")
app.add_typer(models_app, name="models")
app.add_typer(engine_app, name="engine")
app.add_typer(plans_app, name="plan")
app.add_typer(checks_app, name="check")
app.add_typer(sessions_app, name="session")
app.add_typer(eval_app, name="eval")
app.add_typer(jobs_app, name="jobs")
app.add_typer(memory_app, name="memory")
app.add_typer(cockpit_app, name="cockpit")
app.add_typer(workspace_app, name="workspace")


def output(value) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def initialized(command):
    """Initialize only when an operational leaf runs, after argument/help parsing."""
    @wraps(command)
    def invoke(ctx, *args, **kwargs):
        ctx.obj = Settings.load(ctx.obj.data_dir, create=True)
        return command(ctx, *args, **kwargs)
    return invoke


def entrypoint() -> None:
    try:
        app()
    except (ValueError, OSError, ArcadeError, httpx.HTTPError, OllamaUnavailable) as exc:
        typer.echo(f"DevBrain: {exc}", err=True)
        raise SystemExit(1) from None


def store_for(settings: Settings):
    from devbrain.store import Store
    return Store(settings.data_dir)


def actor_scope(store, project_id: str, worktree: Path | str | None = None):
    """Read the launcher's worktree without requiring an ACK merely to inspect state."""
    scope = str(Path(worktree).expanduser().resolve()) if worktree else None
    session_id = os.environ.get("DEVBRAIN_SESSION_ID")
    if session_id:
        rows = store.query("SELECT project_id,worktree FROM sessions WHERE id=:id", {"id": session_id})
        if not rows or rows[0]["project_id"] != project_id:
            raise ValueError("resume_required: command project differs from the session")
        if scope and scope != rows[0]["worktree"]:
            raise ValueError("resume_required: command worktree differs from the session")
        scope = rows[0]["worktree"]
    return scope


@contextmanager
def actor_write(store, settings: Settings, project_id: str, worktree: Path | str | None = None):
    """Native CLI commands inherit the launcher's logical session; manual repair stays possible."""
    session_id = os.environ.get("DEVBRAIN_SESSION_ID")
    if session_id:
        from devbrain.integrations import drain_hook_spool
        drain_hook_spool(settings, store)
    with store.transaction():
        scope = actor_scope(store, project_id, worktree)
        if session_id:
            from devbrain.sessions import SessionManager
            manager = SessionManager(store, settings)
            manager.require_ack(session_id)
        yield scope


def service_alive(settings: Settings) -> bool:
    from devbrain.service import instance_id
    try:
        with httpx.Client(trust_env=False, timeout=0.7) as client:
            result = client.get(settings.url + "/health")
            data = result.json()
            return result.is_success and data.get("service") == "devbrain" and data.get("instance") == instance_id(settings)
    except (httpx.HTTPError, ValueError):
        return False


def ensure_service(settings: Settings, *, background_inference: bool = True) -> None:
    if service_alive(settings):
        return
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    log = settings.data_dir / "service.log"
    with log.open("ab") as handle:
        env = dict(os.environ)
        # The service is a shared system worker, not a child actor of one coding session.
        for key in ("DEVBRAIN_SESSION_ID", "DEVBRAIN_PACKET_ID"):
            env.pop(key, None)
        command = [sys.executable, "-m", "devbrain", "--data-dir", str(settings.data_dir), "serve"]
        if not background_inference:
            command.append("--defer-background-inference")
        subprocess.Popen(command,
                         stdin=subprocess.DEVNULL, stdout=handle, stderr=handle, start_new_session=True, env=env)
    for _ in range(300):
        if service_alive(settings):
            return
        time.sleep(0.2)
    raise typer.BadParameter(f"Usługa nie wystartowała. Sprawdź {log}; port {settings.port} może być zajęty.")


def request(settings: Settings, method: str, path: str, *, start_service: bool = True, **kwargs):
    if start_service:
        ensure_service(settings)
    elif not (settings.data_dir / "config.json").is_file() or not settings.token:
        raise typer.BadParameter("Brak istniejącej konfiguracji DevBrain; uruchom jawnie `devbrain init`.")
    retry_job = path.startswith("/jobs/") and path.endswith("/retry")
    if (method.upper() == "POST" and (retry_job or path in {
            "/remember", "/checkpoint", "/plans/save", "/plans/activate",
            "/memory/snapshot", "/memory/import"})
            or method.upper() == "PATCH" and path == "/memory/settings"):
        session_id = os.environ.get("DEVBRAIN_SESSION_ID")
        if session_id:
            payload = dict(kwargs.get("json") or {})
            if payload.get("session_id") not in (None, session_id):
                raise typer.BadParameter("resume_required: another session cannot be impersonated")
            payload["session_id"] = session_id
            kwargs["json"] = payload
    elif method.upper() == "POST" and path in {"/ask", "/plans/next", "/memory/preview", "/memory/inspect"}:
        session_id = os.environ.get("DEVBRAIN_SESSION_ID")
        if session_id:
            payload = dict(kwargs.get("json") or {})
            payload["session_id"] = session_id
            kwargs["json"] = payload
    elif method.upper() == "GET" and (path in {"/context", "/checks", "/jobs", "/memory/status"}
                                      or path.startswith("/jobs/")):
        session_id = os.environ.get("DEVBRAIN_SESSION_ID")
        if session_id:
            kwargs["params"] = {**dict(kwargs.get("params") or {}), "session_id": session_id}
    with httpx.Client(trust_env=False, timeout=settings.ask_timeout + 90,
                      headers={"Authorization": f"Bearer {settings.token}"}) as client:
        try:
            response = client.request(method, settings.url + path, **kwargs)
        except httpx.HTTPError:
            if not start_service:
                raise typer.BadParameter(
                    "Istniejąca usługa DevBrain jest niedostępna; diagnostyka jej nie uruchamia. "
                    "Uruchom jawnie `devbrain serve`.") from None
            raise
        if not response.is_success:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise typer.BadParameter(str(detail))
        return response.json()


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context, data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None):
    ctx.obj = Settings.load(data_dir, create=False)
    if ctx.invoked_subcommand is None:
        _launch_terminal_workspace(ctx)


def _interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _launch_terminal_workspace(ctx: typer.Context, *, takeover: bool = False) -> None:
    # Check before initialization: redirected CLI diagnostics must never create
    # configuration, launch a browser or silently start interactive processes.
    if not _interactive_terminal():
        raise typer.BadParameter(
            "Uruchom `devbrain` w interaktywnym terminalu. "
            "Dostępne polecenia: `devbrain --help`; stan: `devbrain workspace status`.")
    from devbrain.workspace import launch_workspace
    ctx.obj = Settings.load(ctx.obj.data_dir, create=True)
    code = launch_workspace(ctx.obj, takeover=True) if takeover else launch_workspace(ctx.obj)
    if code:
        raise typer.Exit(code=code)


@app.command()
def tui(ctx: typer.Context):
    """Otwórz terminalowe środowisko DevBrain (tak samo jak polecenie bez argumentów)."""
    _launch_terminal_workspace(ctx)


@app.command("version")
def version():
    """Pokaż wersję bez inicjalizacji konfiguracji lub usług."""
    from devbrain import __version__
    output({"version": __version__})


@workspace_app.command("status")
def terminal_workspace_status(ctx: typer.Context):
    """Odczytaj stan bez uruchamiania usług i inicjalizacji konfiguracji."""
    from devbrain.workspace import workspace_status
    output(workspace_status(ctx.obj))


@workspace_app.command("detach")
def terminal_workspace_detach(ctx: typer.Context):
    """Odłącz widok terminalowy, zachowując sesje klientów."""
    from devbrain.workspace import detach_workspace
    output(detach_workspace(ctx.obj))


@workspace_app.command("takeover")
def terminal_workspace_takeover(ctx: typer.Context):
    """Przejmij sterowanie w tym terminalu; poprzedni widok przejdzie w podgląd."""
    _launch_terminal_workspace(ctx, takeover=True)


@workspace_app.command("stop")
def terminal_workspace_stop(ctx: typer.Context, clients: bool = False):
    """Zamknij środowisko; --clients zatrzymuje też należące do niego sesje."""
    from devbrain.workspace import stop_workspace
    output(stop_workspace(ctx.obj, stop_clients=clients))


@cockpit_app.command("open")
@initialized
def cockpit_open(ctx: typer.Context, no_browser: bool = False):
    """Uruchom lub przyłącz kokpit; --no-browser wypisuje adres do ręcznego otwarcia."""
    from devbrain.cockpit import open_cockpit
    output(open_cockpit(ctx.obj, open_browser=not no_browser))


@cockpit_app.command("serve")
@initialized
def cockpit_serve(ctx: typer.Context, port: int | None = None):
    """Uruchom serwer kokpitu; normalnie zarządza nim polecenie devbrain."""
    from devbrain.cockpit import serve_cockpit
    serve_cockpit(ctx.obj, port=port)


@cockpit_app.command("status")
def cockpit_status(ctx: typer.Context):
    """Odczytaj stan kokpitu bez uruchamiania usług i inicjalizacji konfiguracji."""
    from devbrain.cockpit import cockpit_status as inspect
    output(inspect(ctx.obj))


@cockpit_app.command("stop")
def cockpit_stop(ctx: typer.Context, clients: bool = False):
    """Zatrzymaj kokpit; --clients jawnie zatrzymuje także zarządzane terminale."""
    from devbrain.cockpit import stop_cockpit
    output(stop_cockpit(ctx.obj, stop_clients=clients))


@app.command()
@initialized
def init(ctx: typer.Context):
    """Utwórz lokalny magazyn. Nie pobiera modeli ani nie zmienia konfiguracji klientów."""
    store_for(ctx.obj)
    output({"data_dir": str(ctx.obj.data_dir), "next": "devbrain doctor; devbrain models pull"})


@engine_app.command("install")
@initialized
def engine_install(ctx: typer.Context, distribution: Path | None = None):
    """Pobierz przypięty silnik ArcadeDB i sprawdź SHA-256 (około 87 MB)."""
    from devbrain.engine import install_engine
    path = install_engine(ctx.obj, distribution)
    output({"installed": str(path), "version": ctx.obj.arcade_version})


@engine_app.command("start")
@initialized
def engine_start(ctx: typer.Context):
    from devbrain.engine import start_engine
    output(start_engine(ctx.obj))


@engine_app.command("status")
@initialized
def engine_inspect(ctx: typer.Context):
    from devbrain.engine import engine_status
    output(engine_status(ctx.obj))


@engine_app.command("stop")
@initialized
def engine_stop(ctx: typer.Context):
    from devbrain.engine import stop_engine
    if service_alive(ctx.obj):
        raise typer.BadParameter("Najpierw devbrain stop; usługa wiedzy korzysta z ArcadeDB")
    output(stop_engine(ctx.obj))


@app.command()
@initialized
def doctor(ctx: typer.Context):
    from devbrain.integrations import doctor as inspect
    result = inspect(ctx.obj)
    result["service_running"] = service_alive(ctx.obj)
    result["data_dir"] = str(ctx.obj.data_dir)
    result["models_lock_present"] = (ctx.obj.data_dir / "models.lock.json").exists()
    from devbrain.api_runtime import coding_profiles_status
    result["coding_profiles"] = coding_profiles_status(ctx.obj)
    output(result)


@app.command()
@initialized
def serve(ctx: typer.Context, defer_background_inference: bool = False):
    """Uruchom lokalną usługę (jeden właściciel GPU i kolejki)."""
    import uvicorn
    from devbrain.service import create_app
    settings = ctx.obj
    with (settings.data_dir / "service.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise typer.BadParameter("Usługa już działa dla tego magazynu.")
        api = create_app(settings, background_inference=not defer_background_inference)
        server = uvicorn.Server(uvicorn.Config(api, host=settings.host, port=settings.port, log_level="info"))
        api.state.shutdown = lambda: setattr(server, "should_exit", True)
        server.run()


@app.command()
@initialized
def stop(ctx: typer.Context):
    """Zatrzymaj tylko usługę tego magazynu, bez usuwania danych."""
    if not service_alive(ctx.obj):
        output({"stopping": False, "reason": "Usługa nie działa"})
        return
    output(request(ctx.obj, "POST", "/shutdown"))


@projects_app.command("add")
@initialized
def project_add(ctx: typer.Context, name: str, root: Path):
    store = store_for(ctx.obj)
    root = root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise typer.BadParameter("Katalog projektu musi być katalogiem")
    project = store.add_project(name, root)
    from devbrain.ingest import index_project, register_default_histories
    result = index_project(store, project["id"])
    discovery = register_default_histories(store, project["id"], Path(project["root"]))
    output({"project": project, "indexed": result, "history": discovery})


@projects_app.command("list")
@initialized
def project_list(ctx: typer.Context):
    output(store_for(ctx.obj).list_projects())


@projects_app.command("root")
@initialized
def project_root(ctx: typer.Context, project: str, path: Path):
    """Jawnie przypisz dodatkowy katalog lub worktree do projektu."""
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    root = path.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise typer.BadParameter("Katalog projektu musi być katalogiem")
    store.add_project_root(pid, root)
    output({"project": pid, "root": str(root)})


@app.command()
@initialized
def index(ctx: typer.Context, project: str):
    output(request(ctx.obj, "POST", "/ingest", json={"project": project}))


@app.command("import")
@initialized
def import_command(ctx: typer.Context, path: Path, project: str | None = None, client: str = "auto"):
    """Importuj JSONL klienta; bez projektu zachowaj historię w skrzynce importu."""
    output(request(ctx.obj, "POST", "/import", json={"path": str(path.expanduser().resolve()),
                                                    "project": project, "client": client}))


@app.command()
@initialized
def ask(ctx: typer.Context, project: str, question: str, worktree: Path | None = None,
        answer_schema: Path | None = None):
    payload = {"project": project, "question": question,
               "worktree": str(worktree.resolve()) if worktree else None}
    if answer_schema is not None:
        from devbrain.answer_contract import prepare_answer_schema
        payload["answer_schema"] = prepare_answer_schema(json.loads(answer_schema.read_text(encoding="utf-8")))
    output(request(ctx.obj, "POST", "/ask", json=payload))


@app.command()
@initialized
def context(ctx: typer.Context, project: str, worktree: Path | None = None):
    output(request(ctx.obj, "GET", "/context", params={"project": project,
                   **({"worktree": str(worktree.resolve())} if worktree else {})}))


@app.command()
def status(ctx: typer.Context, project: str | None = None):
    output(request(ctx.obj, "GET", "/status", start_service=False,
                   params={"project": project} if project else {}))


@jobs_app.command("list")
def jobs_list(ctx: typer.Context, project: str,
              state: Annotated[str | None, typer.Option("--state", "--status")] = None,
              kind: str | None = None,
              limit: Annotated[int, typer.Option(min=1, max=100)] = 50):
    """Odczytaj ograniczoną listę zadań; nie uruchamiaj usługi ani silnika."""
    params = {"project": project, "limit": limit}
    if state is not None:
        params["state"] = state
    if kind is not None:
        params["kind"] = kind
    output(request(ctx.obj, "GET", "/jobs", start_service=False, params=params))


@jobs_app.command("show")
def jobs_show(ctx: typer.Context, project: str, job_id: str):
    """Odczytaj zadanie należące do wskazanego projektu."""
    output(request(ctx.obj, "GET", f"/jobs/{job_id}", start_service=False, params={"project": project}))


@jobs_app.command("retry")
@initialized
def jobs_retry(ctx: typer.Context, project: str, job_id: str,
               expected_attempts: Annotated[int, typer.Option("--expected-attempts", min=0)],
               include_omissions: Annotated[bool, typer.Option(
                   "--include-omissions", help="Uwzględnij zakończoną ekstrakcję z samymi odrzuconymi rekordami.")
               ] = False):
    """Jawnie ponów wybrane zadanie, jeśli licznik prób nadal się zgadza."""
    payload = {"project": project, "expected_attempts": expected_attempts}
    if include_omissions:
        payload["include_omissions"] = True
    output(request(ctx.obj, "POST", f"/jobs/{job_id}/retry", json=payload))


@app.command()
@initialized
def records(ctx: typer.Context, project: str, history: bool = False):
    store = store_for(ctx.obj)
    output(store.list_records(store.resolve_project(project)["id"], include_history=history))


@app.command()
@initialized
def source(ctx: typer.Context, source_id: str):
    """Odczytaj zachowany snapshot źródła, nawet gdy oryginał już nie istnieje."""
    output(store_for(ctx.obj).get_source(source_id))


@app.command()
@initialized
def remember(ctx: typer.Context, project: str, content: str, kind: str = "fact",
             source_id: Annotated[list[str] | None, typer.Option("--source")] = None,
             confirmed: bool = False, key: str | None = None):
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    with actor_write(store, ctx.obj, pid):
        sources = source_id or [store.snapshot(pid, f"manual://{uuid.uuid4()}", content, "manual",
                                              {"role": "user", "origin": "cli"})["id"]]
        record = {"kind": kind, "content": content, "status": "confirmed" if confirmed else "proposed"}
        if key is not None:
            record["key"] = key
        result = store.remember(pid, record, sources)
    output(result)


@app.command()
@initialized
def checkpoint(ctx: typer.Context, project: str, state_file: Path):
    output(request(ctx.obj, "POST", "/checkpoint", json={"project": project,
                   "state": json.loads(state_file.read_text(encoding="utf-8"))}))


@tasks_app.command("list")
@initialized
def task_list(ctx: typer.Context, project: str, worktree: Path | None = None):
    from devbrain.verification import verification_status
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    scope = actor_scope(store, pid, worktree)
    tasks = [r for r in store.list_records(pid) if r["kind"] == "task"]
    output([{**task, "verification": verification_status(store, task, scope)} for task in tasks])


@tasks_app.command("add")
@initialized
def task_add(ctx: typer.Context, project: str, title: str, plan: str | None = None, step: int = 1,
             criterion: Annotated[list[str] | None, typer.Option("--criterion")] = None,
             depends_on: Annotated[list[str] | None, typer.Option("--depends-on")] = None,
             requires_test: bool = False, requires_review: bool = False):
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    metadata = {"state": "todo", "plan_id": plan, "step_index": step,
                "acceptance_criteria": criterion or [], "depends_on": depends_on or [],
                "requires_test": requires_test, "requires_review": requires_review}
    text = json.dumps({"title": title, **metadata}, ensure_ascii=False)
    with actor_write(store, ctx.obj, pid):
        src = store.snapshot(pid, f"manual://task/{uuid.uuid4()}", text, "manual", {"role": "user"})
        result = store.remember(pid, {"kind": "task", "content": title, "status": "confirmed",
                                     "metadata": metadata}, [src["id"]])
    output(result)


@tasks_app.command("update")
@initialized
def task_update(ctx: typer.Context, project: str, task_id: str, state: str,
                evidence: Path | None = None, test_passed: bool = False,
                review_passed: bool = False, acceptance_passed: bool = False,
                version: int | None = None, worktree: Path | None = None,
                plan_version: int | None = None,
                check_run: Annotated[list[str] | None, typer.Option("--check-run")] = None):
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    task = next((r for r in store.list_records(pid) if r["id"] == task_id and r["kind"] == "task"), None)
    if task is None:
        raise typer.BadParameter("Nie znaleziono zadania w tym projekcie")
    sources = task["source_ids"]
    if isinstance(sources, str):
        sources = json.loads(sources)
    metadata = {"state": state}
    if check_run:
        metadata["check_run_ids"] = check_run
    evidence_text = None
    if test_passed or review_passed or acceptance_passed:
        if evidence is None:
            raise typer.BadParameter("Weryfikacja wymaga --evidence PLIK z wynikiem sprawdzenia")
        evidence_text = evidence.read_text(encoding="utf-8")
        if not evidence_text.strip():
            raise typer.BadParameter("Plik dowodu musi zawierać niepusty wynik sprawdzenia")
    with actor_write(store, ctx.obj, pid, worktree) as scope:
        if evidence_text is not None:
            src = store.snapshot(pid, f"verification://{uuid.uuid4()}", evidence_text, "verification",
                                 {"path": str(evidence.resolve()), "role": "user", "reported": True})
            sources = [*sources, src["id"]]
            for key, passed in (("test", test_passed), ("review", review_passed), ("acceptance", acceptance_passed)):
                if passed:
                    metadata[f"{key}_passed"] = True
                    metadata[f"{key}_source_ids"] = [src["id"]]
        record = {"id": task_id, "kind": "task", "content": task["content"], "status": task["status"],
                  "metadata": metadata, "worktree": scope,
                  "expected_version": version if version is not None else task["version"]}
        plan_ref = task.get("metadata", {}).get("plan_ref")
        if plan_ref:
            from devbrain.plans import PlanManager
            current_plan = PlanManager(store).get(pid, plan_ref)["plan"]
            record["expected_plan_version"] = plan_version if plan_version is not None else current_plan["version"]
        result = store.remember(pid, record, sources)
    output(result)


@plans_app.command("save")
@initialized
def plan_save(ctx: typer.Context, project: str, input_json: Path,
              idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
              expected_version: int | None = None, task_versions: Path | None = None,
              worktree: Path | None = None, activate: bool = False):
    from devbrain.plans import PlanManager
    document = json.loads(input_json.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise typer.BadParameter("Plik musi zawierać obiekt z polami plan i steps")
    versions = (json.loads(task_versions.read_text(encoding="utf-8")) if task_versions
                else document.get("task_versions"))
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    with actor_write(store, ctx.obj, pid, worktree) as scope:
        result = PlanManager(store).save(pid, document.get("plan", {}), document.get("steps", []),
            idempotency_key, expected_version if expected_version is not None else document.get("expected_version"),
            versions, scope, activate)
    output(result)


@plans_app.command("list")
@initialized
def plan_list(ctx: typer.Context, project: str):
    from devbrain.plans import PlanManager
    store = store_for(ctx.obj)
    output(PlanManager(store).list(store.resolve_project(project)["id"]))


@plans_app.command("show")
@initialized
def plan_show(ctx: typer.Context, project: str, plan_id: str):
    from devbrain.plans import PlanManager
    store = store_for(ctx.obj)
    output(PlanManager(store).get(store.resolve_project(project)["id"], plan_id))


@plans_app.command("next")
@initialized
def plan_next(ctx: typer.Context, project: str, plan_id: str | None = None, worktree: Path | None = None):
    from devbrain.plans import PlanManager
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    scope = actor_scope(store, pid, worktree)
    result = PlanManager(store).next_task(pid, plan_id, scope)
    if result.get("task"):
        from devbrain.verification import verification_status
        result["verification"] = verification_status(store, result["task"], scope)
    output(result)


@plans_app.command("activate")
@initialized
def plan_activate(ctx: typer.Context, project: str, plan_id: str, worktree: Path | None = None):
    from devbrain.plans import PlanManager
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    with actor_write(store, ctx.obj, pid, worktree) as scope:
        result = PlanManager(store).activate(pid, plan_id, scope)
    output(result)


@plans_app.command("adopt")
@initialized
def plan_adopt(ctx: typer.Context, project: str, group: str,
               idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
               title: str | None = None, worktree: Path | None = None, activate: bool = False):
    from devbrain.plans import PlanManager
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    with actor_write(store, ctx.obj, pid, worktree) as scope:
        result = PlanManager(store).adopt_legacy(pid, group, plan={"title": title} if title else None,
            idempotency_key=idempotency_key, worktree=scope, activate=activate)
    output(result)


@checks_app.command("profiles")
@initialized
def check_profiles(ctx: typer.Context, project: str, worktree: Path | None = None):
    from devbrain.verification import load_profiles
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    output(load_profiles(store, pid, actor_scope(store, pid, worktree)))


@checks_app.command("run")
@initialized
def check_run(ctx: typer.Context, project: str, profile: str,
              task: Annotated[str, typer.Option("--task")], worktree: Path | None = None):
    from devbrain.verification import run_check
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    with actor_write(store, ctx.obj, pid, worktree) as scope:
        pass  # Validate receipt first; do not hold the database lock while executing a program.
    result = run_check(store, ctx.obj, pid, profile, task, scope)
    output(result)
    if not result.get("passed"):
        raise typer.Exit(1)


@checks_app.command("list")
@initialized
def check_list(ctx: typer.Context, project: str,
               task: Annotated[str | None, typer.Option("--task")] = None,
               worktree: Path | None = None):
    from devbrain.verification import recover_interrupted_runs, verification_status
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    scope = actor_scope(store, pid, worktree)
    recover_interrupted_runs(store, ctx.obj)
    rows = store.query("SELECT FROM check_runs WHERE project_id=:project "
                       + ("AND task_id=:task " if task else "")
                       + ("AND worktree=:worktree " if scope else "") + "ORDER BY ended_at DESC,id LIMIT 100",
                       {"project": pid, "task": task, "worktree": scope})
    result = {"checks": [{key: value for key, value in row.items() if not key.startswith("@")} for row in rows]}
    if task:
        records = store.query("SELECT FROM records WHERE id=:id AND project_id=:project AND kind='task'",
                              {"id": task, "project": pid})
        if not records:
            raise typer.BadParameter("Brak zadania w tym projekcie")
        result["verification"] = verification_status(store, records[0], scope)
    output(result)


@sessions_app.command("prepare")
@initialized
def session_prepare(ctx: typer.Context, project: str, client: str, worktree: Path | None = None):
    result = request(ctx.obj, "POST", "/sessions/prepare", json={"project": project, "client": client,
                     "worktree": str(worktree.resolve()) if worktree else None})
    output(result)
    if result["status"] != "ready":
        raise typer.Exit(2)


@sessions_app.command("show")
@initialized
def session_show(ctx: typer.Context, session_id: str):
    output(request(ctx.obj, "GET", f"/sessions/{session_id}"))


@sessions_app.command("context")
@initialized
def session_context(ctx: typer.Context, session_id: str):
    output(request(ctx.obj, "POST", "/sessions/context", json={"session_id": session_id}))


@sessions_app.command("ack")
@initialized
def session_ack(ctx: typer.Context, session_id: str, packet_id: str, packet_hash: str, readback_json: Path):
    actor = os.environ.get("DEVBRAIN_SESSION_ID")
    if actor and actor != session_id:
        raise typer.BadParameter("Nie można potwierdzać innej sesji")
    output(request(ctx.obj, "POST", "/sessions/ack", json={"session_id": session_id,
        "packet_id": packet_id, "packet_hash": packet_hash,
        "readback": json.loads(readback_json.read_text(encoding="utf-8"))}))


@eval_app.command("run")
@initialized
def eval_run(ctx: typer.Context, clients: bool = False, report_dir: Path | None = None,
             skip_claude: bool = False):
    from devbrain.evaluation import run_evaluation
    if skip_claude and not clients:
        raise typer.BadParameter("--skip-claude wymaga --clients")
    result = run_evaluation(ctx.obj, clients=clients, report_dir=report_dir, skip_claude=skip_claude)
    output(result)
    if result.get("status") == "failed":
        raise typer.Exit(1)
    if result.get("status") == "not_run":
        raise typer.Exit(3)


@eval_app.command("memory")
@initialized
def eval_memory(ctx: typer.Context, report_dir: Path | None = None):
    """Jawnie sprawdź ekstrakcję, przeniesienie i odtworzenie pamięci przez lokalną Ollamę."""
    from devbrain.memory_evaluation import run_memory_evaluation
    result = run_memory_evaluation(ctx.obj, report_dir=report_dir)
    output(result)
    if result.get("status") != "passed":
        raise typer.Exit(3 if result.get("status") == "not_run" else 1)


@memory_app.command("snapshot")
@initialized
def memory_snapshot(ctx: typer.Context, project: str, worktree: Path | None = None, preview: bool = False):
    """Zapisz pełną pamięć projektu; --preview pokazuje zakres bez zapisu snapshotu."""
    payload = {"project": project, "worktree": str(worktree.expanduser().resolve()) if worktree else None}
    output(request(ctx.obj, "POST", "/memory/preview" if preview else "/memory/snapshot", json=payload))


@memory_app.command("inspect")
def memory_inspect(ctx: typer.Context, path: Path):
    """Sprawdź pliki snapshotu bez inicjalizacji magazynu i uruchamiania usług."""
    from devbrain.memory import inspect_snapshot
    # Preserve symlink components for the package validator to reject them.
    selected = str(path.expanduser().absolute())
    if os.environ.get("DEVBRAIN_SESSION_ID"):
        output(request(ctx.obj, "POST", "/memory/inspect", start_service=False, json={"path": selected}))
    else:
        output(inspect_snapshot(selected))


@memory_app.command("import")
def memory_import(ctx: typer.Context, path: Path,
                  root: Annotated[Path, typer.Option("--root")], name: str | None = None,
                  dry_run: bool = False):
    """Wczytaj pamięć wyłącznie tego projektu; --dry-run wymaga istniejącej usługi i niczego nie zapisuje."""
    from devbrain.memory import inspect_snapshot
    selected = str(path.expanduser().absolute())
    inspect_snapshot(selected)  # Validate the transfer before initializing anything.
    if not dry_run:
        ctx.obj = Settings.load(ctx.obj.data_dir, create=True)
    payload = {"path": selected, "root": str(root.expanduser().resolve()), "dry_run": dry_run}
    if name is not None:
        payload["name"] = name
    output(request(ctx.obj, "POST", "/memory/import", start_service=not dry_run, json=payload))


@memory_app.command("auto")
def memory_auto(ctx: typer.Context, project: str, enable: bool = False, disable: bool = False,
                interval_minutes: Annotated[int, typer.Option(min=1, max=10080)] = 15,
                worktree: Path | None = None):
    """Włącz lub wyłącz lokalny harmonogram jednego projektu; bez commitów i wysyłania do GitHub."""
    if enable == disable:
        raise typer.BadParameter("Wybierz dokładnie jedno: --enable albo --disable.")
    ctx.obj = Settings.load(ctx.obj.data_dir, create=True)
    payload = {"project": project, "enabled": enable, "interval_minutes": interval_minutes,
               "worktree": str(worktree.expanduser().resolve()) if worktree else None}
    output(request(ctx.obj, "PATCH", "/memory/settings", json=payload))


@memory_app.command("status")
def memory_status(ctx: typer.Context, project: str, worktree: Path | None = None):
    """Odczytaj ostatni snapshot i automat; nie uruchamiaj usługi ani nie twórz konfiguracji."""
    params = {"project": project}
    if worktree is not None:
        params["worktree"] = str(worktree.expanduser().resolve())
    output(request(ctx.obj, "GET", "/memory/status", start_service=False, params=params))


@app.command()
@initialized
def install(ctx: typer.Context, project: str):
    """Dodaj projektowe MCP, hooki i instrukcje, zachowując obecną konfigurację."""
    from devbrain.integrations import install_integrations
    store = store_for(ctx.obj)
    output(install_integrations(ctx.obj, store.resolve_project(project)))


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
@initialized
def run(ctx: typer.Context, project: str, client: str, worktree: Path | None = None):
    from devbrain.integrations import run_client
    ensure_service(ctx.obj)
    selected = store_for(ctx.obj).resolve_project(project)
    if worktree:
        store = store_for(ctx.obj)
        root = str(worktree.expanduser().resolve())
        if not store.query("SELECT id FROM project_roots WHERE project_id=:project AND path=:path",
                            {"project": selected["id"], "path": root}):
            raise typer.BadParameter("Worktree musi być zarejestrowanym katalogiem projektu")
        selected = {**selected, "root": root}
    args = list(ctx.args)
    if args[:1] == ["--"]:
        args = args[1:]
    raise typer.Exit(run_client(ctx.obj, selected, client, args))


@app.command()
@initialized
def mcp(ctx: typer.Context):
    """MCP stdio; nie wypisuje logów ani komunikatów CLI na stdout."""
    from devbrain.mcp_server import run
    run(ctx.obj)


@app.command()
@initialized
def hook(ctx: typer.Context, client: Annotated[str, typer.Option("--client")]):
    from devbrain.integrations import hook as handle_hook
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        result = handle_hook(ctx.obj, payload, client)
        if result:
            typer.echo(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        # Never corrupt the coding client's protocol or block a turn on memory failures.
        typer.echo(f"DevBrain hook: {exc}", err=True)


@models_app.command("list")
@initialized
def model_list(ctx: typer.Context):
    from devbrain.ollama import Ollama
    output(asyncio.run(Ollama(ctx.obj).models()))


@models_app.command("pull")
@initialized
def model_pull(ctx: typer.Context):
    """Jawnie pobierz modele (kilka GB) i zapisz ich digests."""
    settings = ctx.obj
    with httpx.Client(trust_env=False, timeout=httpx.Timeout(60, read=900)) as client:
        for name in (settings.chat_model, settings.embedding_model):
            typer.echo(f"Pobieranie {name}…", err=True)
            with client.stream("POST", settings.ollama_url + "/api/pull", json={"name": name, "stream": True}) as res:
                res.raise_for_status()
                previous = None
                for line in res.iter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    if data.get("error"):
                        raise typer.BadParameter(data["error"])
                    status = data.get("status")
                    if status != previous:
                        typer.echo(str(status), err=True)
                        previous = status
        result = client.get(settings.ollama_url + "/api/tags")
        result.raise_for_status()
        models = result.json()
    lock = {"chat_model": settings.chat_model, "embedding_model": settings.embedding_model,
            "models": models.get("models", []), "locked_at": time.time()}
    (settings.data_dir / "models.lock.json").write_text(json.dumps(lock, indent=2), encoding="utf-8")
    store = store_for(settings)
    from devbrain.service import schedule_embeddings
    for item in models.get("models", []):
        if item.get("name") == settings.embedding_model and item.get("digest"):
            schedule_embeddings(store, f"{settings.embedding_model}@{item['digest']}")
    output({"models": [settings.chat_model, settings.embedding_model], "digests_saved": True})


def _retry_jobs(store):
    store.retry_jobs()


@app.command("retry-jobs")
@initialized
def retry_jobs(ctx: typer.Context):
    if os.environ.get("DEVBRAIN_SESSION_ID"):
        raise typer.BadParameter(
            "Sesja modelu nie może ponawiać zadań wszystkich projektów. "
            "Użyj `devbrain jobs retry PROJEKT JOB_ID --expected-attempts N` z aktualnym ACK.")
    _retry_jobs(store_for(ctx.obj))
    output({"retried": True})


@app.command("rebuild-vectors")
@initialized
def rebuild_vectors(ctx: typer.Context, project: str):
    """Odbuduj pochodne wektory projektu, zachowując źródła i historię."""
    from devbrain.ollama import Ollama
    from devbrain.service import schedule_embeddings
    settings = ctx.obj
    store = store_for(settings)
    pid = store.resolve_project(project)["id"]
    namespace = asyncio.run(Ollama(settings).model_identity(settings.embedding_model))
    store.delete_embeddings(pid)
    count = schedule_embeddings(store, namespace)
    output({"project": pid, "missing_vectors_queued": count, "namespace": namespace,
            "sources_preserved": True})


@app.command()
@initialized
def export(ctx: typer.Context, destination: Path, project: str | None = None, markdown: bool = False):
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"] if project else None
    data = store.export(pid)
    if markdown:
        parts = ["# DevBrain — eksport pamięci\n"]
        for record in data.get("records", []):
            parts.append(f"## {record['kind']}: {record.get('key') or record['id']}\n\n{record['content']}\n")
            parts.append("```json\n" + json.dumps(record, ensure_ascii=False, indent=2) + "\n```\n")
        for src in data.get("sources", []):
            parts.append(f"## Źródło {src['id']}\n\nURI: {src['uri']}\n\n````text\n{src['content']}\n````\n")
        text = "\n".join(parts)
    else:
        text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    destination.write_text(text, encoding="utf-8")
    output({"export": str(destination.resolve())})


@app.command()
@initialized
def backup(ctx: typer.Context, destination: Path):
    output({"backup": str(store_for(ctx.obj).backup(destination))})


@app.command()
@initialized
def restore(ctx: typer.Context, source: Path):
    with (ctx.obj.data_dir / "service.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise typer.BadParameter("Najpierw devbrain stop; usługa nadal posiada blokadę magazynu")
        if service_alive(ctx.obj):
            raise typer.BadParameter("Najpierw devbrain stop; odtwarzanie wymaga zatrzymanej usługi")
        store_for(ctx.obj).restore(source)
    output({"restored": True})


@app.command()
@initialized
def forget(ctx: typer.Context, project: str,
           delete_project: Annotated[bool, typer.Option("--delete-project", help="Jawne usunięcie danych projektu")] = False):
    if not delete_project:
        raise typer.BadParameter("Usunięcie wymaga --delete-project. Istniejące backupy pozostają bez zmian.")
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    store.forget_project(pid)
    output({"deleted": pid, "backups_unchanged": True})


@app.command()
@initialized
def benchmark(ctx: typer.Context, project: str, question: str):
    """Zmierz pierwszą odpowiedź i powtórzenie z cache; nie zastępuje oceny jakości."""
    runs = []
    for label in ("first", "cached"):
        started = time.perf_counter()
        result = request(ctx.obj, "POST", "/ask", json={"project": project, "question": question})
        runs.append({"run": label, "seconds": round(time.perf_counter() - started, 3),
                     "status": result["status"], "cached": result.get("cached"),
                     "sources": len(result.get("sources", []))})
    try:
        gpu = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=5, check=False).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        gpu = "nvidia-smi unavailable"
    output({"runs": runs, "gpu": gpu})
