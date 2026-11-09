"""Opt-in, supervised real TUI with explicit human or agent observations.

The controller starts a fresh engine and the real service/cockpit, with scanners
and job workers disabled. --attach only joins an existing private workspace and
offers a small checklist menu after C-b d; it never calls ensure_service/create.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import pwd
import secrets
import select
import signal
import shutil
import socket
import stat
import subprocess
import sys
import time
from uuid import uuid4


SIZES = {"80x24": (80, 24), "120x36": (120, 36), "180x55": (180, 55)}
TASKS = {
    "start": "A: jawnie wybierz Start work; przeglądanie wcześniej nie uruchamia asystenta.",
    "switch": "A → B → A: wróć do istniejącej sesji, bez duplikatu procesu/rozmowy.",
    "source": "Znajdź źródło ux://A/evidence i przeczytaj znacznik UX_A_ZAZOLC.",
    "connection": "Otwórz połączenia, wybierz Codex/Claude i model; zmiana musi być widoczna.",
    "reconnect": "C-b d, następnie 1 w menu: wróć do tej samej sesji bez polecenia powłoki.",
}


def operator_acceptance(observations, operator):
    required = {"task:" + key for key in TASKS} | {"geometry:" + size for size in SIZES}
    return (operator in {"human", "agent"} and set(observations) == required and all(
        value.get("status") == "passed" and value.get("operator") == operator
        and value.get("human_reported") is (operator == "human")
        and value.get("agent_reported") is (operator == "agent")
        and value.get("size") in SIZES and value.get("actual_size") == list(SIZES[value["size"]])
        and (key.startswith("geometry:") or value["size"] == "120x36")
        for key, value in observations.items()))


def human_acceptance(observations):
    return operator_acceptance(observations, "human")


def agent_acceptance(observations):
    return operator_acceptance(observations, "agent")


def finish_native_auth(common, rows, report):
    """Always sync, then release; no private row/digest/exception is published."""
    try:
        try:
            result = common.sync_native_auth(rows)
        except Exception:
            result = {"passed": False, "error_codes": ["oauth_cache_sync_failed"]}
        report["oauth_cache_sync"] = result
        if result.get("passed") is not True:
            report["errors"].append("oauth_cache_sync_failed")
    finally:
        try:
            released = common.release_native_auth(rows, close_journal=(
                report.get("cleanup_complete") is True and report.get("oauth_cache_sync", {}).get("passed") is True))
        except Exception:
            released = {"passed": False, "error_codes": ["oauth_lease_release_failed"]}
        report["oauth_cache_release"] = released
        if released.get("passed") is not True:
            report["errors"].append("oauth_lease_release_failed")


def attach_auth_environment(state, common):
    """Reconstruct only public Claude storage metadata; never a Codex lease."""
    metadata = state.get("claude_secure_storage")
    if state.get("baseline_only"):
        common.require(metadata is None, "baseline_auth_storage_forbidden")
        return {}
    common.require(isinstance(metadata, dict) and set(metadata) == {"directory", "binary", "binary_sha256"},
                   "claude_attach_storage_metadata_required")
    common.require(isinstance(state.get("native_binaries"), dict)
                   and metadata["binary_sha256"] == common.CLAUDE_SECURE_STORAGE_BINARY_SHA256
                   and state["native_binaries"].get(metadata["binary"]) == metadata["binary_sha256"],
                   "claude_attach_binary_mismatch")
    directory, binary = Path(metadata["directory"]), Path(metadata["binary"])
    common.require(directory.is_absolute() and directory.resolve() == directory
                   and binary.is_absolute() and binary.resolve() == binary, "claude_attach_path_not_canonical")
    return common.native_auth_environment([{"mode": "claude_official_storage", "source": directory / ".credentials.json", "binary": binary}])


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), Path(__file__).with_name(name + ".py"))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def private_json(path, common):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as stream:
        info = os.fstat(stream.fileno())
        common.require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                       and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size < 1024 * 1024,
                       "private_manifest_required")
        return json.load(stream)


def installed(source, common):
    origin = importlib.util.find_spec("devbrain")
    common.require(sys.flags.isolated == 1 and sys.prefix != sys.base_prefix and origin
                   and Path(origin.origin).resolve() == source / "src/devbrain/__init__.py"
                   and importlib.metadata.version("devbrain") == "0.8.0", "qualified_isolated_install_required")


def admit(row, runtime, supervision):
    """Observe a viewer; never signal an out-of-scope process."""
    if (not row or row.get("uid") != os.getuid()
            or row.get("executable") not in (runtime["python_executable"], runtime["tmux_executable"])
            or not supervision.same_process(row)):
        raise ValueError("viewer_identity_not_verified")
    handle = os.pidfd_open(row["pid"])
    if not supervision.same_process(row):
        os.close(handle)
        raise ValueError("viewer_identity_changed")
    return handle


def read_choice(prompt, alive):
    print(prompt, end="", flush=True)
    while alive():
        if select.select([sys.stdin], [], [], .25)[0]:
            line = sys.stdin.readline()
            return line.strip() if line else None
    return None


def attach(args, *, baseline=False):
    common, supervision = load("validate-native-account080"), load("supervise-080")
    manifest_path = args.manifest
    common.require(manifest_path.is_absolute() and manifest_path.resolve() == manifest_path,
                   "canonical_private_manifest_required")
    state = private_json(manifest_path, common)
    common.require(state.get("operator") in {"human", "agent"}
                   and args.operator in {None, state["operator"]}, "attach_operator_mismatch")
    common.require(state.get("scope") == "devbrain_interactive_ux_private" and state["status"] == "running"
                   and state.get("baseline_only", False) is baseline
                   and time.time() < state["expires_unix_time"], "interactive_run_not_available")
    common.require(common.sha(Path(__file__)) == state["helper_sha256"], "attach_helper_changed")
    common.require(common.sha(Path(sys.argv[0]).resolve()) == state["entrypoint_sha256"], "attach_entrypoint_changed")
    runtime = supervision.qualified_runtime(supervision.read_json(Path(state["gate_path"])))
    common.require(runtime == state["runtime"], "attach_runtime_changed")
    if baseline:
        load("run-baseline-ux072").verify(Path(state["baseline_archive"]), runtime)
    else:
        installed(Path(state["source_root"]), common)
    common.require(supervision.same_process(state["controller_identity"]), "controller_not_alive")
    work = common.private_directory(Path(state["work_root"]))
    cache = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".cache"
    common.require(work == manifest_path.parent and work.is_relative_to(cache)
                   and Path(state["data_dir"]) == work / "engine-home", "attach_private_data_scope_mismatch")
    common.private_directory(Path(state["tmp_root"]))
    environment = common.clean_environment(work, runtime, state["run_id"], state["native_binaries"])
    os.environ.clear()
    os.environ.update(environment)
    os.environ.update(attach_auth_environment(state, common))
    if baseline:
        os.environ["PATH"] = baseline_path(runtime)
    os.environ.update(TERM="xterm-256color", TMPDIR=state["tmp_root"])
    import tempfile
    tempfile.tempdir = None
    from devbrain.config import Settings
    from devbrain.workspace import WorkspaceManager
    common.require(sys.stdin.isatty() and sys.stdout.isatty(), "interactive_terminal_required")
    manager = WorkspaceManager(Settings.load(Path(state["data_dir"])), tmux_binary=runtime["tmux_executable"])
    common.require(str(manager.socket) == state["socket"] and manager.exists(), "existing_workspace_required")
    viewer_id = uuid4().hex
    registration = work / "viewers" / (viewer_id + ".json")
    supervision.atomic_json(registration, {"identity": supervision.identity(os.getpid()), "nonce": state["nonce"],
                                          "size": args.size, "status": "awaiting_admission"})
    os.chmod(registration, 0o600)

    def alive():
        current = private_json(manifest_path, common)
        return (current.get("status") == "running" and time.time() < current["expires_unix_time"]
                and supervision.same_process(state["controller_identity"]))

    try:
        # The controller must open an actual pidfd before this external viewer
        # can attach or submit results. It waits on that handle during cleanup.
        deadline = time.monotonic() + 10
        while alive() and not registration.with_suffix(".admitted").exists():
            common.require(time.monotonic() < deadline, "viewer_admission_timeout")
            time.sleep(.1)
        while alive():
            print("\nDevBrain — " + ("porównanie wizualne 0.7.2 (bez modeli) " if baseline else "ocena 0.8.0 ") + args.size + " — operator: " + state["operator"])
            if not baseline:
                for number, text in enumerate(TASKS.values(), 1):
                    print(f"{number}. {text}")
            menu = ("1: dołącz/ponów  3: zamknij okno  4: ocena geometrii  9: zakończ porównanie\n> " if baseline else
                    "1: dołącz/ponów  2: wynik zadania (120x36)  3: zamknij okno  4: ocena geometrii  9: zakończ całą ocenę\n> ")
            choice = read_choice(menu, alive)
            if choice == "1":
                actual = tuple(os.get_terminal_size(sys.stdout.fileno()))
                if actual != SIZES[args.size]:
                    print(f"Rozmiar faktyczny {actual[0]}x{actual[1]}; wymagany {args.size}. Dopasuj okno/font.")
                    continue
                common.require(manager.exists(), "workspace_has_stopped")
                code = manager.attach()  # no startup, fallback, recovery or takeover
                common.require(code == 0, "terminal_attachment_failed")
            elif choice in {"2", "4"}:
                if baseline and choice == "2":
                    print("Baseline służy tylko porównaniu ekranów. Nie zalicza zadań 0.8.0; użyj oceny geometrii (4).")
                    continue
                if choice == "2" and args.size != "120x36":
                    print("Pięć zadań wykonujemy raz w 120x36. W tym rozmiarze zapisz tylko ocenę geometrii (4).")
                    continue
                number = read_choice("Numer zadania 1..5: ", alive) if choice == "2" else None
                verdict = read_choice("p=PASS, f=FAIL, b=BLOCKED (wyłącznie własna obserwacja): ", alive)
                if (choice == "2" and number not in {"1", "2", "3", "4", "5"}) or verdict not in {"p", "f", "b"}:
                    continue
                actual = tuple(os.get_terminal_size(sys.stdout.fileno()))
                if actual != SIZES[args.size]:
                    print("Zapis odrzucony: faktyczna geometria różni się od wybranej.")
                    continue
                event = {"nonce": state["nonce"], "viewer_id": viewer_id, "size": args.size,
                         "actual_size": list(actual), "kind": "task" if choice == "2" else "geometry",
                         "task": list(TASKS)[int(number) - 1] if choice == "2" else None,
                         "status": {"p": "passed", "f": "failed", "b": "blocked"}[verdict],
                         "operator": state["operator"], "human_reported": state["operator"] == "human",
                         "agent_reported": state["operator"] == "agent", "time": time.time()}
                target = work / "operator-events" / (uuid4().hex + ".json")
                supervision.atomic_json(target, event)
                os.chmod(target, 0o600)
            elif choice in {None, "3", "9"}:
                if choice == "9":
                    supervision.atomic_json(work / "finish-requested.json", {"nonce": state["nonce"]})
                    os.chmod(work / "finish-requested.json", 0o600)
                break
    finally:
        manager.close()
    return 0


def baseline_path(runtime):
    return os.pathsep.join([str(Path(sys.executable).parent), str(Path(runtime["java_executable"]).parent),
                           str(Path(runtime["tmux_executable"]).parent), "/usr/bin", "/bin"])


async def workload(args, common, supervision, guard, scope, parent, report, root, runtime, auth_rows):
    baseline = report.get("baseline_only", False)
    work = common.private_directory(args.private_root / ("interactive-" + uuid4().hex), create=True)
    for name in ("home", "codex-auth", "claude-auth", "tmp", "bin", "engine-home", "projects", "viewers", "operator-events"):
        common.private_directory(work / name, create=True)
    # tmux has a short Unix socket path limit; this fresh 0700 directory holds
    # only per-run transient sockets, never original account files.
    tmp = common.private_directory(Path("/tmp") / ("dbux-" + uuid4().hex[:12]), create=True)
    binaries, viewers, seen_events = {}, {}, set()
    engine = manager = cockpit = service = None
    servers, tasks, listeners = [], [], []
    private = {"scope": "devbrain_interactive_ux_private", "run_id": args.run_id, "status": "starting", "operator": report["operator"],
               "nonce": secrets.token_hex(32), "work_root": str(work), "tmp_root": str(tmp),
               "runtime": runtime, "gate_path": str(root / "gate.json"), "source_root": str(args.source_root),
               "controller_identity": supervision.identity(os.getpid()), "helper_sha256": common.sha(Path(__file__)),
               "entrypoint_sha256": common.sha(Path(sys.argv[0]).resolve()), "baseline_only": baseline,
               "baseline_archive": str(args.baseline_archive) if baseline else None,
               "expires_unix_time": time.time() + args.duration}
    manifest = work / "interactive.json"
    def publish():
        report["human_reported"] = bool(report["operator_observations"]) and report["operator"] == "human"
        report["agent_reported"] = bool(report["operator_observations"]) and report["operator"] == "agent"
        supervision.atomic_json(manifest, private)
        os.chmod(manifest, 0o600)
        supervision.atomic_json(root / "controller-report.json", report)
        supervision.atomic_json(root / "interactive-results.json", {"run_id": args.run_id,
            "tasks": {} if baseline else TASKS, "baseline_only": baseline,
            "operator": report["operator"], "observations": report["operator_observations"], "automatic_visual_pass": False,
            "human_reported": report["human_reported"], "agent_reported": report["agent_reported"],
            "human_acceptance_passed": report.get("human_acceptance_passed", False),
            "agent_acceptance_passed": report.get("agent_acceptance_passed", False)})
    def track_viewers():
        for path in (work / "viewers").glob("*.json"):
            value = private_json(path, common)
            common.require(value.get("nonce") == private["nonce"], "viewer_registration_mismatch")
            row = value["identity"]
            key = (row["pid"], row["birth"])
            if key not in viewers:
                viewers[key] = admit(row, runtime, supervision)
                path.with_suffix(".admitted").touch(mode=0o600, exist_ok=False)
        if manager and manager.exists():
            for client in manager.clients():
                row = supervision.identity(client["pid"])
                if row and (row["pid"], row["birth"]) not in viewers:
                    viewers[(row["pid"], row["birth"])] = admit(row, runtime, supervision)
    try:
        for client in (() if baseline else ("codex", "claude")):
            binary = getattr(args, client + "_bin").resolve(strict=True)
            common.require(binary.is_file() and os.access(binary, os.X_OK), "native_binary_required")
            binaries[str(binary)] = common.sha(binary)
            (work / "bin" / client).symlink_to(binary)
            filename = "auth.json" if client == "codex" else ".credentials.json"
            auth_rows.append(common.prepare_native_auth(client, getattr(args, client + "_auth"), work / (client + "-auth") / filename, binary, scope=scope))
            if client == "claude":
                private["claude_secure_storage"] = {"directory": str(auth_rows[-1]["source"].parent),
                    "binary": str(binary), "binary_sha256": binaries[str(binary)]}
        environment = common.clean_environment(work, runtime, args.run_id, binaries)
        os.environ.clear()
        os.environ.update(environment)
        os.environ.update(common.native_auth_environment(auth_rows))
        if baseline:
            os.environ["PATH"] = baseline_path(runtime)
            common.require(not shutil.which("codex") and not shutil.which("claude"), "baseline_native_clients_must_be_unavailable")
        os.environ.update(TMPDIR=str(tmp), TERM="xterm-256color")
        import tempfile
        tempfile.tempdir = None
        import uvicorn
        from devbrain import __version__
        from devbrain.cockpit import create_cockpit_app, _runtime_file, _birth, instance_id
        from devbrain.cockpit_state import CockpitState, _atomic
        from devbrain.config import Settings
        from devbrain.service import create_app
        from devbrain.store import Store
        from devbrain.workspace import WorkspaceManager
        for _ in range(3):
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listeners.append(listener)
        settings_kwargs = dict(data_dir=work / "engine-home", host="127.0.0.1", port=listeners[0].getsockname()[1],
            cockpit_port=listeners[1].getsockname()[1], arcade_url=f"http://127.0.0.1:{listeners[2].getsockname()[1]}",
            arcade_password=secrets.token_urlsafe(32), token=secrets.token_urlsafe(32),
            arcade_database="devbrain_interactive_" + uuid4().hex, ollama_url="http://127.0.0.1:1")
        if not baseline:
            settings_kwargs.update(java_executable=runtime["java_executable"], tmux_executable=runtime["tmux_executable"])
        settings = Settings(**settings_kwargs)
        listeners.pop().close()  # only JVM port needs release before its own bind
        settings.save()
        engine = common.OwnedEngine(settings, args.arcade_dist, runtime, scope, supervision, work)
        await asyncio.to_thread(engine.start)
        store = await asyncio.to_thread(Store, settings.data_dir, settings=settings)
        projects = []
        for name in ("A", "B"):
            folder = common.private_directory(work / "projects" / name, create=True)
            (folder / "README.md").write_text(f"Synthetic UX project {name}.\n", encoding="utf-8")
            for argv in (["git", "init", "-q", str(folder)], ["git", "-C", str(folder), "add", "README.md"],
                         ["git", "-C", str(folder), "-c", "user.name=DevBrain UX", "-c", "user.email=ux@localhost", "commit", "-qm", "Synthetic UX fixture"]):
                await asyncio.to_thread(subprocess.run, argv, check=True, capture_output=True, timeout=10)
            project = await asyncio.to_thread(store.add_project, "UX " + name + " · Zażółć 日本語", folder)
            source = await asyncio.to_thread(store.snapshot, project["id"], f"ux://{name}/evidence", f"UX_{name}_ZAZOLC · syntetyczne źródło", "manual")
            await asyncio.to_thread(store.remember, project["id"], {"kind": "fact", "status": "confirmed", "content": "Źródło służy wyłącznie ręcznej ocenie."}, [source["id"]])
            await asyncio.to_thread(store.remember, project["id"], {"kind": "task", "status": "confirmed", "content": "Sprawdź ręcznie interfejs", "metadata": {"state": "todo", "acceptance_criteria": ["Wynik człowieka jest zapisany"]}}, [source["id"]])
            projects.append(project)
        CockpitState(settings).update({"scan_roots": []} if baseline else {"scan_roots": [], "view": "focus"})
        service = create_app(settings, start_worker=False)
        from devbrain.api_runtime import RuntimeSupervisor
        cockpit = create_cockpit_app(settings, runtime=RuntimeSupervisor(settings, tmux_binary=runtime["tmux_executable"]),
                                    public_url=f"http://127.0.0.1:{settings.cockpit_port}")
        if baseline:
            from fastapi.responses import JSONResponse
            @cockpit.middleware("http")
            async def baseline_read_only(request, call_next):
                if request.method not in {"GET", "HEAD", "OPTIONS"} and request.url.path not in {"/_control/ticket", "/auth/exchange"}:
                    return JSONResponse({"detail": "Read-only visual baseline: operation disabled."}, status_code=403)
                return await call_next(request)
        for app, listener in zip((service, cockpit), listeners):
            listener.listen(128)
            server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", proxy_headers=False,
                access_log=False, log_level="warning", timeout_graceful_shutdown=5))
            app.state.shutdown = lambda selected=server: setattr(selected, "should_exit", True)
            servers.append(server)
            tasks.append(asyncio.create_task(server.serve(sockets=[listener])))
        deadline = time.monotonic() + 20
        while not all(server.started for server in servers):
            common.require(time.monotonic() < deadline and not any(task.done() for task in tasks), "private_http_start_failed")
            await asyncio.sleep(.1)
        _atomic(_runtime_file(settings), {"url": f"http://127.0.0.1:{settings.cockpit_port}", "pid": os.getpid(),
            "birth": _birth(os.getpid()), "instance": instance_id(settings), "version": __version__})
        manager = WorkspaceManager(settings, tmux_binary=runtime["tmux_executable"])
        await asyncio.to_thread(manager.create, start_supervisor=False)
        manager.state.select_project(projects[0], intent="inspect")
        private.update(status="running", data_dir=str(settings.data_dir), socket=str(manager.socket), native_binaries=dict(binaries))
        report.update(status="awaiting_operator", private_work_root=str(work), worker_started=False,
                      codex_private_auth_copy=not baseline, claude_original_secure_store=not baseline, local_model_inference=False)
        publish()
        supervision.atomic_json(root / "interactive-ready.json", {"run_id": args.run_id, "manifest": str(manifest),
            "python_launcher": sys.executable, "controller": str(Path(sys.argv[0]).resolve()), "operator": report["operator"], "sizes": list(SIZES),
            "expires_unix_time": private["expires_unix_time"]})
        human_deadline = time.monotonic() + max(0, private["expires_unix_time"] - time.time())
        while time.monotonic() < human_deadline:
            common.require(all(not task.done() for task in tasks) and engine.process.poll() is None, "owned_service_exited")
            await asyncio.to_thread(manager.refresh)
            track_viewers()
            for path in (work / "operator-events").glob("*.json"):
                if path.name in seen_events:
                    continue
                value = private_json(path, common)
                common.require(value.get("nonce") == private["nonce"] and value.get("operator") == report["operator"]
                    and value.get("human_reported") is (report["operator"] == "human")
                    and value.get("agent_reported") is (report["operator"] == "agent")
                    and value.get("size") in SIZES and (value.get("kind") == "geometry" or
                        not baseline and value.get("kind") == "task" and value.get("task") in TASKS and value["size"] == "120x36")
                    and value.get("status") in {"passed", "failed", "blocked"}
                    and value.get("actual_size") == list(SIZES[value["size"]]), "invalid_operator_observation")
                seen_events.add(path.name)
                key = "task:" + value["task"] if value["kind"] == "task" else "geometry:" + value["size"]
                report["operator_observations"][key] = {
                    field: value[field] for field in ("kind", "size", "actual_size", "task", "status", "operator", "human_reported", "agent_reported", "time")}
                if value["kind"] == "task":
                    report["oauth_phase_sync"] = await asyncio.to_thread(common.sync_native_auth, auth_rows)
                    common.require(report["oauth_phase_sync"].get("passed") is True, "oauth_phase_sync_failed")
                publish()
            finish = work / "finish-requested.json"
            if finish.exists():
                common.require(private_json(finish, common).get("nonce") == private["nonce"], "invalid_finish_request")
                break
            await asyncio.sleep(.5)
        report["human_acceptance_passed"] = not baseline and human_acceptance(report["operator_observations"])
        report["agent_acceptance_passed"] = not baseline and agent_acceptance(report["operator_observations"])
        report["baseline_visual_observations_complete"] = baseline and set(report["operator_observations"]) == {"geometry:" + size for size in SIZES}
    finally:
        private["status"] = "stopping"
        publish()
        # Never call stop_workspace: it uses a separately launched supervisor.
        # This controller is the only coordinator, and verifies both tmux servers
        # against the cgroup before invoking the exact private socket cleanup.
        try:
            if manager:
                track_viewers()
            if cockpit:
                native = cockpit.state.runtime
                for row in await asyncio.to_thread(native.list_runs):
                    pane = await asyncio.to_thread(native._pane, row)
                    if pane and not pane["dead"]:
                        identity = supervision.identity(pane["pid"])
                        common.require(identity and scope.owns(identity), "native_session_not_owned")
                        handle = os.pidfd_open(identity["pid"])
                        try:
                            common.require(scope.owns(identity), "native_session_identity_changed")
                            signal.pidfd_send_signal(handle, signal.SIGTERM)
                            exited = await asyncio.to_thread(select.select, [handle], [], [], 15)
                            common.require(bool(exited[0]), "native_session_graceful_exit_missing")
                        finally:
                            os.close(handle)
                        # NativeRuntime.stop normally has a SIGKILL fallback.
                        # Only let it finalize metadata after the real pane has
                        # exited, so a forced native shutdown cannot be hidden.
                        deadline = time.monotonic() + 3
                        while True:
                            current = await asyncio.to_thread(native._pane, row)
                            if not current or current["dead"]:
                                break
                            common.require(time.monotonic() < deadline, "native_pane_exit_not_observed")
                            await asyncio.sleep(.05)
                    await asyncio.to_thread(native.stop, row["id"], generation=row.get("generation"))
                await native.aclose()
            for label, exists, command in (("workspace", manager and manager.exists(), manager.tmux if manager else None),
                                          ("native", cockpit and cockpit.state.runtime.socket.exists(), cockpit.state.runtime._tmux if cockpit else None)):
                if exists:
                    row = supervision.identity(int(command("display-message", "-p", "#{pid}").stdout.strip()))
                    common.require(row and row["executable"] == runtime["tmux_executable"] and scope.owns(row), "tmux_cleanup_not_owned")
                    command("kill-server")
            for server in servers:
                server.should_exit = True
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks), 12)
            if manager:
                manager.close()
            deadline = time.monotonic() + 12
            while any(not select.select([handle], [], [], 0)[0] for handle in viewers.values()) and time.monotonic() < deadline:
                await asyncio.sleep(.1)
            common.require(all(select.select([handle], [], [], 0)[0] for handle in viewers.values()), "external_viewer_actual_exit_missing")
            report["external_viewers_actual_exit_verified"] = True
            common.require(engine is None or await asyncio.to_thread(engine.stop), "engine_cleanup_failed")
            report["engine"] = engine.report if engine else {"started": False}
            report["cleanup_grace_seconds"] = await asyncio.to_thread(common.quiet_scope, scope, supervision, parent)
            report["cleanup_complete"] = True
        except Exception as exc:
            report["errors"].append(str(exc) if isinstance(exc, common.ValidationError) else type(exc).__name__)
        finally:
            # An earlier UI/server shutdown error must not prevent an attempt
            # to stop the independently owned JVM. Its exact pidfd is retained.
            if engine is not None and engine.handle is not None:
                try:
                    common.require(await asyncio.to_thread(engine.stop), "engine_cleanup_failed")
                except Exception as exc:
                    report["cleanup_complete"] = False
                    report["errors"].append(str(exc) if isinstance(exc, common.ValidationError) else type(exc).__name__)
                report["engine"] = engine.report
            report["forced_cleanup"] = bool(engine and engine.report.get("forced_cleanup"))
            for handle in viewers.values():
                os.close(handle)
            for listener in listeners:
                listener.close()
            for server in servers:
                server.should_exit = True
            report["native_binaries_unchanged"] = all(common.sha(Path(path)) == expected for path, expected in binaries.items())
            private["status"] = "closed"
            publish()


def main(*, baseline=False):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attach", action="store_true")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--size", choices=SIZES, default="180x55")
    parser.add_argument("--operator", choices=("human", "agent"))
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--run-id")
    for flag in ("artifact-root", "source-root", "private-root", "arcade-dist", "codex-bin", "claude-bin", "codex-auth", "claude-auth"):
        parser.add_argument("--" + flag, type=Path)
    parser.add_argument("--duration", type=int, default=1800)
    if baseline:
        parser.add_argument("--baseline-archive", type=Path)
    args = parser.parse_args()
    if args.attach:
        if args.manifest is None:
            parser.error("--attach requires --manifest")
        return attach(args, baseline=baseline)
    if not args.execute:
        print(json.dumps({"status": "not_run", "tasks": TASKS, "sizes": list(SIZES), "maximum_seconds": 1800}))
        return 0
    common, supervision, guard = load("validate-native-account080"), load("supervise-080"), load("attest-full-suite-080")
    mandatory = ("run_id", "artifact_root", "private_root", "arcade_dist") + (("baseline_archive",) if baseline else
                ("source_root", "codex_bin", "claude_bin", "codex_auth", "claude_auth"))
    common.require(all(getattr(args, key) is not None for key in mandatory)
                   and 1 <= args.duration <= 1800, "explicit_bounded_inputs_required")
    root = args.artifact_root.resolve(strict=True)
    common.require(root == args.artifact_root and root.is_relative_to(Path("/mnt/c")) and not (root / "controller-report.json").exists(), "fresh_external_artifact_root_required")
    runtime = supervision.qualified_runtime(supervision.read_json(root / "gate.json"))
    common.require(os.environ.get("DEVBRAIN_QUALIFICATION_RUN_ID") == args.run_id, "external_supervision_required")
    proof = supervision.read_json(root / "mapping-proof.json")
    common.require(proof.get("run_id") == args.run_id, "scope_run_identity_mismatch")
    scope = supervision.ScopeOwnership(proof.get("scope_unit"))
    parent = supervision.identity(os.getppid())
    common.require(parent and scope.owns(parent), "supervisor_parent_required")
    common.private_directory(args.private_root)
    common.require(args.private_root.is_relative_to(Path(pwd.getpwuid(os.getuid()).pw_dir) / ".cache"), "private_linux_cache_required")
    if baseline:
        baseline_probe = load("run-baseline-ux072").verify(args.baseline_archive, runtime)
        hashes = baseline_probe["package_sha256"]
    else:
        installed(args.source_root, common)
        hashes = guard.source_manifest(args.source_root)
    report = {"schema_version": 1, "scope": "devbrain_baseline_visual_comparison" if baseline else "devbrain_interactive_ux", "run_id": args.run_id,
              "operator": args.operator or "human", "human_acceptance_passed": False, "agent_acceptance_passed": False,
              "human_reported": False, "agent_reported": False,
              "baseline_only": baseline, "baseline_is_080_human_acceptance": False,
              "runtime": runtime, "python_launcher": sys.executable, "status": "starting", "errors": [],
              "source_hashes": hashes, "runtime_gate_sha256": common.sha(root / "gate.json"),
              "operator_observations": {}, "automatic_visual_pass": False, "controller_exit_verified": False,
              "cleanup_complete": False, "forced_cleanup": False}
    previous = dict(os.environ)
    old_umask = os.umask(0o077)
    auth_rows = []  # private transaction/lease state: never serialize this list
    try:
        asyncio.run(workload(args, common, supervision, guard, scope, parent, report, root, runtime, auth_rows))
        after = load("run-baseline-ux072").verify(args.baseline_archive, runtime)["package_sha256"] if baseline else guard.source_manifest(args.source_root)
        common.require(hashes == after, "source_changed")
        common.require(supervision.qualified_runtime(supervision.read_json(root / "gate.json")) == runtime
                       and common.sha(root / "gate.json") == report["runtime_gate_sha256"], "gate_or_runtime_changed")
        common.require(report.get("native_binaries_unchanged"), "native_binary_changed")
    except Exception as exc:
        report["errors"].append(str(exc) if isinstance(exc, common.ValidationError) else type(exc).__name__)
    finally:
        # Independent of every UI/server/engine cleanup path, including failures
        # before their finally blocks. An unverified CLI exit keeps the journal
        # open for explicit local recovery after its previous scope is empty.
        finish_native_auth(common, auth_rows, report)
        os.environ.clear()
        os.environ.update(previous)
        os.umask(old_umask)
        complete = (report.get("baseline_visual_observations_complete") if baseline else
                    report.get(report["operator"] + "_acceptance_passed"))
        report["status"] = (("baseline_visual_completed_pending_external_watch" if baseline else "interactive_ux_completed_pending_external_watch") if complete
                            and report["cleanup_complete"] and not report["forced_cleanup"] and not report["errors"] else "failed")
        supervision.atomic_json(root / "controller-report.json", report)
        result_path = root / "interactive-results.json"
        if result_path.is_file():
            result = supervision.read_json(result_path)
            result.update(oauth_cache_sync=report["oauth_cache_sync"], oauth_cache_release=report["oauth_cache_release"],
                          controller_status=report["status"])
            supervision.atomic_json(result_path, result)
    return 0 if report["status"].endswith("_completed_pending_external_watch") else 1


if __name__ == "__main__":
    raise SystemExit(main())
