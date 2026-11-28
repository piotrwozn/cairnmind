"""CLI commands for configuring and pulling local/remote knowledge models."""
from __future__ import annotations

import json
from typing import Annotated

import httpx
import typer

from cairnmind.agents.ollama import OllamaUnavailable
from cairnmind.cli_cmds import io as _io
from cairnmind.cli_cmds.io import initialized
from cairnmind.config import tls_verify


def register(apps: dict[str, typer.Typer]) -> None:
    models_app = apps["models_app"]

    @models_app.command("list")
    @initialized
    def model_list(ctx: typer.Context):
        import asyncio

        from cairnmind.memory.memory_models import from_settings
        _io.output(asyncio.run(from_settings(ctx.obj).models()))

    @models_app.command("set")
    @initialized
    def model_set(ctx: typer.Context,
                  role: Annotated[str, typer.Option("--role", help="chat (answers, extraction) or embedding.")],
                  provider: Annotated[str, typer.Option("--provider", help="ollama or openai_compatible.")],
                  model: Annotated[str, typer.Option("--model", help="The model's name on that server.")],
                  base_url: Annotated[str | None, typer.Option(
                      "--base-url", help="The server's address; HTTPS unless it is this computer.")] = None,
                  credential_ref: Annotated[str | None, typer.Option(
                      "--credential-ref", help="env:NAME or profile:ID naming the key; never the key itself.")] = None,
                  context_window: Annotated[int | None, typer.Option(
                      "--context-window", help="Chat only: the model's context in tokens.")] = None,
                  reasoning_effort: Annotated[str | None, typer.Option(
                      "--reasoning-effort", help="Chat on an OpenAI-compatible server only: none, low, medium or high; "
                                                 "none turns thinking off where the server supports it.")] = None,
                  allow_remote: Annotated[bool, typer.Option(
                      "--allow-remote", help="Consent to send memory content to a server on another computer.")] = False):
        """Choose the knowledge agent's chat or embedding model and server, after a test call to it."""
        import asyncio

        from cairnmind.memory.memory_models import configure_role
        try:
            result = asyncio.run(configure_role(
                ctx.obj, role, provider=provider, model=model, base_url=base_url, credential_ref=credential_ref,
                context_window=context_window, reasoning_effort=reasoning_effort, allow_remote=allow_remote))
        except (ValueError, OllamaUnavailable) as exc:
            raise typer.BadParameter(str(exc)) from None
        from cairnmind.agents.knowledge_mode import turn_on
        said = turn_on(ctx.obj)  # its check passed: the knowledge agent is on from the service's next start (#274)
        if said:
            result["knowledge_agent"] = said
        _io.output(result)

    @models_app.command("pull")
    @initialized
    def model_pull(ctx: typer.Context):
        """Explicitly download the models (several GB) and record their digests."""
        from cairnmind.memory.memory_models import profile
        settings = ctx.obj
        local = [name for role, name in (("chat", settings.chat_model), ("embedding", settings.embedding_model))
                 if profile(settings, role) is None]
        if not local:
            raise typer.BadParameter("Neither memory model runs on the local Ollama; install them on their own server.")
        from cairnmind.memory.memory_models import local_server
        try:
            local_server(settings)
        except ValueError as exc:  # another computer: nothing is downloaded there or asked (#288)
            _io.refuse(str(exc))
        with httpx.Client(trust_env=False, timeout=httpx.Timeout(60, read=900),
                          verify=tls_verify(settings.ollama_url)) as client:
            for name in local:
                typer.echo(f"Downloading {name}…", err=True)
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
                "models": models.get("models", []), "locked_at": _io.time.time()}
        (settings.data_dir / "models.lock.json").write_text(json.dumps(lock, indent=2), encoding="utf-8")
        from cairnmind.agents.knowledge_mode import turn_on
        said = turn_on(settings)  # the models are here: the knowledge agent is on from the service's next start (#274)
        if said:
            typer.echo(f"Cairnmind: {said}", err=True)  # said now: the store steps below may still fail
        store = _io.store_for(settings)
        from cairnmind.service import schedule_embeddings
        for item in models.get("models", []):
            if item.get("name") == settings.embedding_model and item.get("digest") and settings.embedding_model in local:
                schedule_embeddings(store, f"{settings.embedding_model}@{item['digest']}")
        _io.output({"models": local, "digests_saved": True, **({"knowledge_agent": said} if said else {})})
