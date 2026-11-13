"""Authenticated inspector HTTP routes and committed journal subscriptions."""
from __future__ import annotations

import asyncio
import hmac
import json
import time
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from . import conflicts, impact_verdicts
from .arcade import ArcadeError
from .inspector import Inspector, InspectorNotFound


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: str
    action: Literal["confirm", "dispute"]
    expected_version: int = Field(strict=True, ge=1)
    expected_plan_version: int | None = Field(default=None, strict=True, ge=1)
    reason: str | None = Field(default=None, max_length=4000)


class ConflictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: str
    record_id: str = Field(min_length=1, max_length=200)
    other_id: str = Field(min_length=1, max_length=200)
    action: Literal["supersede", "different_scopes", "dismiss"]
    expected_version: int = Field(strict=True, ge=1)
    other_expected_version: int = Field(strict=True, ge=1)
    keep: str | None = Field(default=None, max_length=200)
    reason: str | None = Field(default=None, max_length=4000)


class ImpactVerdictBody(BaseModel):
    # No session field: a verdict is a person's, and a coding session has no way to send one.
    model_config = ConfigDict(extra="forbid")
    project: str
    worktree: str | None = Field(default=None, max_length=4096)
    since: str = Field(min_length=1, max_length=200)
    record_id: str = Field(min_length=1, max_length=200)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    verdict: Literal["still_valid", "needs_update", "not_related"]
    note: str | None = Field(default=None, max_length=4000)


def _event_id(cursor):
    return f"{cursor['epoch']}:{cursor['seq']}"


async def event_stream(journal, request, after=None, project_id=None, *, once=False):
    last_heartbeat = time.monotonic()
    while not await request.is_disconnected():
        result = await asyncio.to_thread(journal.read, after=after, project_id=project_id, limit=200)
        if result.get("reset_required"):
            yield "event: reset\nid: " + _event_id(result["cursor"]) + "\ndata: " + json.dumps(result) + "\n\n"
        else:
            for event in result.get("events", []):
                yield "event: change\nid: " + _event_id(event) + "\ndata: " + json.dumps(event) + "\n\n"
        after = _event_id(result["cursor"])
        if once:
            return
        if time.monotonic() - last_heartbeat >= 15:
            yield ": heartbeat\n\n"
            last_heartbeat = time.monotonic()
        await asyncio.sleep(.5)


def create_inspector_router(store, settings):
    inspector = Inspector(store)

    async def authorize(authorization: str | None = Header(default=None)):
        if not authorization or not hmac.compare_digest(authorization, "Bearer " + settings.token):
            raise HTTPException(status_code=401, detail="Invalid local service token")

    router = APIRouter(dependencies=[Depends(authorize)])

    async def call(method, *args, **kwargs):
        try:
            return await asyncio.to_thread(method, *args, **kwargs)
        except InspectorNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=409 if "conflict" in str(exc).lower() else 400, detail=str(exc)) from None
        except ArcadeError:
            raise HTTPException(status_code=503, detail="Memory storage is unavailable") from None

    @router.get("/inspector/summary")
    async def summary(project: str | None = None):
        return await call(inspector.summary, project)

    @router.get("/inspector/inbox")
    async def inbox(cursor: str | None = None, limit: int = 100):
        return await call(inspector.inbox, cursor=cursor, limit=limit)

    @router.get("/inspector/inbox/{identifier}")
    async def inbox_source(identifier: str, view: str = "raw", offset: int = 0, limit: int = 65536):
        return await call(inspector.inbox_source, identifier, view=view, offset=offset, limit=limit)

    @router.get("/inspector/records")
    async def records(project: str, worktree: str | None = None, kind: str | None = None,
                      status: str | None = None, cursor: str | None = None, limit: int = 100):
        return await call(inspector.page, "records", project, worktree=worktree, kind=kind, status=status, cursor=cursor, limit=limit)

    @router.get("/inspector/sources")
    async def sources(project: str, worktree: str | None = None, kind: str | None = None,
                      cursor: str | None = None, limit: int = 100):
        return await call(inspector.page, "sources", project, worktree=worktree, kind=kind, cursor=cursor, limit=limit)

    @router.get("/inspector/plans")
    async def plans(project: str, worktree: str | None = None, cursor: str | None = None, limit: int = 100):
        return await call(inspector.page, "plans", project, worktree=worktree, cursor=cursor, limit=limit)

    @router.get("/inspector/tasks")
    async def tasks(project: str, worktree: str | None = None, status: str | None = None, state: str | None = None,
                    cursor: str | None = None, limit: int = 100):
        return await call(inspector.page, "tasks", project, worktree=worktree, status=status, state=state, cursor=cursor, limit=limit)

    @router.get("/inspector/checks")
    async def checks(project: str, worktree: str | None = None, status: str | None = None,
                     cursor: str | None = None, limit: int = 100):
        return await call(inspector.page, "checks", project, worktree=worktree, status=status, cursor=cursor, limit=limit)

    @router.get("/inspector/jobs")
    async def jobs(project: str, worktree: str | None = None, kind: str | None = None, status: str | None = None,
                   cursor: str | None = None, limit: int = 100):
        return await call(inspector.page, "jobs", project, worktree=worktree, kind=kind, status=status, cursor=cursor, limit=limit)

    @router.get("/inspector/records/{record_id}")
    async def record(record_id: str, project: str, worktree: str | None = None):
        return await call(inspector.record, project, record_id, worktree)

    @router.get("/inspector/plans/{plan_id}")
    async def plan(plan_id: str, project: str, worktree: str | None = None):
        return await call(inspector.entity, "plans", project, plan_id, worktree=worktree)

    @router.get("/inspector/plans/{plan_id}/revisions")
    async def plan_revisions(plan_id: str, project: str, cursor: str | None = None, limit: int = 100):
        return await call(inspector.plan_revisions, project, plan_id, cursor=cursor, limit=limit)

    @router.get("/inspector/checks/{check_id}")
    async def check(check_id: str, project: str, worktree: str | None = None):
        return await call(inspector.entity, "check_runs", project, check_id, worktree=worktree)

    @router.get("/inspector/jobs/{job_id}")
    async def job(job_id: str, project: str, worktree: str | None = None):
        return await call(inspector.entity, "jobs", project, job_id, worktree=worktree)

    @router.get("/inspector/records/{record_id}/revisions")
    async def revisions(record_id: str, project: str, worktree: str | None = None,
                        cursor: str | None = None, limit: int = 100):
        return await call(inspector.revisions, project, record_id, worktree=worktree, cursor=cursor, limit=limit)

    @router.get("/inspector/sources/{source_id}")
    async def source(source_id: str, project: str, worktree: str | None = None,
                     view: str = "raw", offset: int = 0, limit: int = 65536):
        return await call(inspector.source, project, source_id, worktree=worktree, view=view, offset=offset, limit=limit)

    @router.get("/inspector/graph")
    async def graph(project: str | None = None, worktree: str | None = None, group: str | None = None,
                    limit: int = 2000, page_cursor: str | None = None):
        return await call(inspector.graph, project, worktree=worktree, group=group, limit=limit, page_cursor=page_cursor)

    @router.get("/inspector/graph/groups")
    async def groups(project: str, worktree: str | None = None, cursor: str | None = None, limit: int = 100):
        return await call(inspector.groups, project, worktree=worktree, cursor=cursor, limit=limit)

    @router.get("/inspector/graph/{node_id:path}/neighbors")
    async def neighbors(node_id: str, project: str, worktree: str | None = None, cursor: str | None = None, limit: int = 100):
        return await call(inspector.neighbors, project, node_id, worktree=worktree, cursor=cursor, limit=limit)

    @router.get("/inspector/conflicts")
    async def conflict_inbox(project: str, worktree: str | None = None):
        return await call(conflicts.inbox, inspector, project, worktree)

    @router.post("/inspector/conflicts/resolve")
    async def resolve_conflict(body: ConflictBody):
        return await call(conflicts.resolve, inspector, body.project, body.record_id, body.other_id, body.action,
                          body.expected_version, body.other_expected_version, keep=body.keep, reason=body.reason)

    @router.post("/inspector/impact/verdict")
    async def impact_verdict(body: ImpactVerdictBody):
        # Mapped by type, not by text: a commit name or a path in a message must not turn 400 into 409.
        try:
            return await asyncio.to_thread(impact_verdicts.resolve, inspector, body.project, body.worktree,
                                           body.since, body.record_id, body.fingerprint, body.verdict,
                                           body.note)
        except InspectorNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except impact_verdicts.StaleVerdict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except ArcadeError:
            raise HTTPException(status_code=503, detail="Memory storage is unavailable") from None

    @router.post("/inspector/records/{record_id}/review")
    async def review(record_id: str, body: ReviewBody):
        return await call(inspector.review, body.project, record_id, body.action, body.expected_version,
                          expected_plan_version=body.expected_plan_version, reason=body.reason)

    @router.get("/events")
    async def events(request: Request, after: str | None = None, project: str | None = None, once: bool = False,
                     last_event_id: str | None = Header(default=None)):
        from .journal import Journal

        pid = (await call(inspector._project, project))["id"] if project else None
        # Validate a supplied cursor before streaming starts so malformed input
        # has an ordinary HTTP response, rather than a broken event stream.
        journal = Journal(store)
        initial = after
        if last_event_id:
            try:
                epoch, sequence = last_event_id.rsplit(":", 1)
                if str(UUID(epoch)) == epoch and int(sequence) >= 0:
                    initial = last_event_id
            except (ValueError, TypeError):
                pass
        await call(journal.read, after=initial, project_id=pid, limit=1)
        return StreamingResponse(event_stream(journal, request, initial, pid, once=once), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return router


def attach_inspector_routes(app, store, settings):
    app.include_router(create_inspector_router(store, settings))
