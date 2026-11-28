"""Authenticated inspector HTTP routes and committed journal subscriptions."""
from __future__ import annotations

import asyncio
import hmac
import json
import time
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from cairnmind.memory import conflicts, impact_verdicts, project_forget
from cairnmind.memory import stale_proposed as proposed
from cairnmind.memory.findings import MAX_FINDING_CHARS
from cairnmind.pal import memory_release
from cairnmind.storage.arcade import ArcadeError
from cairnmind.terminal import person_proof
from cairnmind.terminal.inspector import Inspector, InspectorNotFound


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: str
    action: Literal["confirm", "dispute"]
    expected_version: int = Field(strict=True, ge=1)
    expected_plan_version: int | None = Field(default=None, strict=True, ge=1)
    reason: str | None = Field(default=None, max_length=4000)
    worktree: str | None = Field(default=None, max_length=4096)  # Whose branch and commit the audit names (#247).


class ConfirmPlanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: str
    expected_plan_version: int = Field(strict=True, ge=1)
    reason: str | None = Field(default=None, max_length=4000)
    worktree: str | None = Field(default=None, max_length=4096)


class ReleaseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: str
    reason: str = Field(min_length=1, max_length=2000)
    worktree: str | None = Field(default=None, max_length=4096)


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
    worktree: str | None = Field(default=None, max_length=4096)


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


class RecordKindBody(BaseModel):
    # No session field: only a person accepts or withdraws a transcript record kind (#210).
    model_config = ConfigDict(extra="forbid")
    project: str
    client: Literal["claude", "codex", "unknown"]
    kind: str = Field(min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=1000)


class AdoptGroupBody(BaseModel):
    # No session field: only a person adopts an old group into a plan (#305).
    model_config = ConfigDict(extra="forbid")
    project: str
    group: str = Field(min_length=1, max_length=1000)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    task_versions: dict[str, StrictInt] = Field(min_length=1, max_length=10000)
    worktree: str | None = Field(default=None, max_length=4096)


class ForgetProjectBody(BaseModel):
    # No session field: only a person deletes a project's memory, in the workspace (#347).
    model_config = ConfigDict(extra="forbid")
    project: str = Field(min_length=1, max_length=200)


class ReplaceBody(BaseModel):
    # No session field: only a person replaces a record with a new text (#222).
    model_config = ConfigDict(extra="forbid")
    project: str
    content: str = Field(min_length=1, max_length=MAX_FINDING_CHARS)
    expected_version: int = Field(strict=True, ge=1)
    reason: str | None = Field(default=None, max_length=4000)
    worktree: str | None = Field(default=None, max_length=4096)


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

    async def person_route(request: Request):
        # A person's act (#214): the open workspace's key, on top of the token. Nothing is written without it.
        await person_proof.require(request, settings)
    person = [Depends(person_route)]

    async def forget_route(request: Request):
        # Deleting a project's memory (#347): the open workspace's key, with its own refusal.
        await person_proof.require(request, settings, person_proof.FORGET_REFUSED)

    async def call(method, *args, **kwargs):
        try:
            return await asyncio.to_thread(method, *args, **kwargs)
        except InspectorNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=409 if "conflict" in str(exc).lower() else 400, detail=str(exc)) from None
        except ArcadeError:
            raise HTTPException(status_code=503, detail="Memory storage is unavailable") from None

    async def whole_graph(method, *args, **kwargs):
        # These ask the database for a page, and read the whole graph only for a scope that holds back more than
        # inspector_graph.HELD_LIMIT rows (#450). What a read took goes back to the system either way (#410).
        try:
            return await call(method, *args, **kwargs)
        finally:
            await asyncio.to_thread(memory_release.release_freed)

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

    @router.get("/inspector/extraction-stats")
    async def extraction_stats(project: str):
        return await call(inspector.extraction_stats, project)

    @router.get("/inspector/sources/{source_id}")
    async def source(source_id: str, project: str, worktree: str | None = None,
                     view: str = "raw", offset: int = 0, limit: int = 65536):
        return await call(inspector.source, project, source_id, worktree=worktree, view=view, offset=offset, limit=limit)

    @router.get("/inspector/graph")
    async def graph(project: str | None = None, worktree: str | None = None, group: str | None = None,
                    limit: int = 2000, page_cursor: str | None = None):
        return await whole_graph(inspector.graph, project, worktree=worktree, group=group, limit=limit, page_cursor=page_cursor)

    @router.get("/inspector/graph/groups")
    async def groups(project: str, worktree: str | None = None, cursor: str | None = None, limit: int = 100):
        return await whole_graph(inspector.groups, project, worktree=worktree, cursor=cursor, limit=limit)

    @router.get("/inspector/graph/{node_id:path}/neighbors")
    async def neighbors(node_id: str, project: str, worktree: str | None = None, cursor: str | None = None, limit: int = 100):
        return await whole_graph(inspector.neighbors, project, node_id, worktree=worktree, cursor=cursor, limit=limit)

    @router.get("/inspector/conflicts")
    async def conflict_inbox(project: str, worktree: str | None = None):
        return await call(conflicts.inbox, inspector, project, worktree)

    @router.get("/inspector/proposed")
    async def stale_proposed(project: str, worktree: str | None = Query(default=None, max_length=4096),
                             days: int = Query(default=proposed.DEFAULT_DAYS, ge=0, le=proposed.MAX_DAYS),
                             limit: int = Query(default=proposed.MAX_ITEMS, ge=1, le=proposed.MAX_ITEMS)):
        # Read-only, what `cairnmind proposed --json` prints (#189); acting on an entry is the review route below.
        return await call(proposed.listing, inspector, project, worktree, days, limit)

    @router.post("/inspector/conflicts/resolve", dependencies=person)
    async def resolve_conflict(body: ConflictBody):
        return await call(conflicts.resolve, inspector, body.project, body.record_id, body.other_id, body.action,
                          body.expected_version, body.other_expected_version, keep=body.keep, reason=body.reason,
                          worktree=body.worktree)

    async def impact_call(method, *args):
        # Mapped by type, not by text: a commit name or a path in a message must not turn 400 into 409.
        try:
            return await asyncio.to_thread(method, inspector, *args)
        except InspectorNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except impact_verdicts.StaleVerdict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except ArcadeError:
            raise HTTPException(status_code=503, detail="Memory storage is unavailable") from None

    @router.get("/inspector/impact")
    async def impact_list(project: str, since: str = Query(min_length=1, max_length=200),
                          worktree: str | None = Query(default=None, max_length=4096), pending: bool = False):
        # Read-only, the parameters of `cairnmind impact`, and its --json answer.
        return await impact_call(impact_verdicts.listing, project, worktree, since, pending)

    @router.post("/inspector/impact/verdict", dependencies=person)
    async def impact_verdict(body: ImpactVerdictBody):
        return await impact_call(impact_verdicts.resolve, body.project, body.worktree, body.since, body.record_id,
                                 body.fingerprint, body.verdict, body.note)

    @router.post("/inspector/records/{record_id}/review", dependencies=person)
    async def review(record_id: str, body: ReviewBody):
        return await call(inspector.review, body.project, record_id, body.action, body.expected_version,
                          expected_plan_version=body.expected_plan_version, reason=body.reason,
                          worktree=body.worktree)

    @router.post("/inspector/plans/{plan_id}/confirm", dependencies=person)
    async def confirm_plan(plan_id: str, body: ConfirmPlanBody):
        # A person confirms a plan and its steps at once (#170); the audit lets them travel as published memory.
        from cairnmind.memory.memory_acts import confirm
        return await call(confirm, inspector, body.project, plan_id, body.expected_plan_version, reason=body.reason,
                          worktree=body.worktree)

    @router.post("/inspector/records/{record_id}/release", dependencies=person)
    async def release_record(record_id: str, body: ReleaseBody):
        # A person lets one record's false alarms of the secrets gate through (#171), with a published reason;
        # it needs the open workspace's key as a review does (#306).
        from cairnmind.memory.memory_published_release import release
        project_id = (await call(inspector._project, body.project))["id"]
        return await call(release, store, project_id, record_id, body.reason, body.worktree)

    @router.post("/inspector/records/{record_id}/replace", dependencies=person)
    async def replace_record(record_id: str, body: ReplaceBody):
        # A person replaces a record with a new text (#222); it needs the open workspace's key.
        from cairnmind.memory.record_replace import replace
        return await call(replace, inspector, body.project, record_id, body.content, body.expected_version,
                          reason=body.reason, worktree=body.worktree)

    @router.get("/inspector/record-kinds")
    async def record_kinds_overview(project: str):
        from cairnmind.memory import record_kinds
        project_id = (await call(inspector._project, project))["id"]
        return await call(record_kinds.overview, store, project_id)

    async def record_kind_act(body, withdraw):
        # The person's act of the inspector route, as a review is (#177): the note is a review audit.
        from cairnmind.memory import record_kinds
        project_id = (await call(inspector._project, body.project))["id"]
        return await call(record_kinds.act_as_person, store, project_id, body.client, body.kind, body.note,
                          route="inspector_review", withdraw=withdraw)

    @router.post("/inspector/record-kinds/accept", dependencies=person)
    async def accept_record_kind(body: RecordKindBody):
        return await record_kind_act(body, False)

    @router.post("/inspector/record-kinds/withdraw", dependencies=person)
    async def withdraw_record_kind(body: RecordKindBody):
        return await record_kind_act(body, True)

    @router.get("/inspector/legacy-groups")
    async def legacy_groups(project: str):
        # Read-only: the old task groups a person can adopt in the workspace (#305).
        from cairnmind.memory.plans import legacy_groups as listing
        project_id = (await call(inspector._project, project))["id"]
        return await call(listing, store, project_id)

    @router.post("/inspector/legacy-groups/adopt", dependencies=person)
    async def adopt_legacy_group(body: AdoptGroupBody):
        # A person adopts an old task group into a plan (#305); it needs the open workspace's key.
        from cairnmind.memory.memory_acts import adopt_group
        project_id = (await call(inspector._project, body.project))["id"]
        return await call(adopt_group, store, project_id, body.group, body.task_versions, title=body.title,
                          worktree=body.worktree)

    @router.get("/inspector/projects/deletable")
    async def deletable_projects():
        # Read-only: the projects that have memory in the store, for the workspace's Delete project memory (#347).
        return await call(project_forget.deletable, store)

    @router.get("/inspector/projects/forget")
    async def forget_plan(project: str = Query(min_length=1, max_length=200)):
        # Read-only: the checks of the deletion and what it would delete, for the consent dialog (#347).
        return await call(project_forget.plan, settings, store, project)

    @router.post("/inspector/projects/forget", dependencies=[Depends(forget_route)])
    async def forget_project(body: ForgetProjectBody):
        # A person deletes a project's whole memory (#347); it needs the open workspace's key.
        return await call(project_forget.delete, settings, store, body.project)

    @router.get("/events")
    async def events(request: Request, after: str | None = None, project: str | None = None, once: bool = False,
                     last_event_id: str | None = Header(default=None)):
        from cairnmind.storage.journal import Journal

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
