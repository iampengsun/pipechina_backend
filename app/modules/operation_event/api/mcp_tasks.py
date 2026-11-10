"""MCP Tasks adapter over the durable investigation run and transactional outbox."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from mcp.server.extension import Extension, MethodBinding
from mcp.shared.exceptions import MCPError
from mcp_types import RequestParams
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text

from app.modules.operation_event.application import input_requests, memory
from app.modules.operation_event.application.service import classify_event
from app.modules.operation_event.domain.agent_models import AgentRun
from app.modules.operation_event.domain.investigation_schemas import (
    InvestigationOptions,
    ResumeInvestigation,
)
from app.shared.errors import AppError
from app.shared.platform.models import AsyncJob, JobEvent
from app.shared.platform.service import request_cancel

TASK_EXTENSION = "io.modelcontextprotocol/tasks"


class TaskParams(RequestParams):
    taskId: UUID


class UpdateParams(TaskParams):
    inputResponses: dict[str, dict] = Field(default_factory=dict)


class StartInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: UUID
    request_id: UUID
    options: InvestigationOptions = Field(default_factory=InvestigationOptions)


def negotiated(ctx):
    caps = (ctx.meta or {}).get("io.modelcontextprotocol/clientCapabilities", {})
    if TASK_EXTENSION not in caps.get("extensions", {}):
        raise MCPError(
            -32021,
            "Missing required client capability",
            {"requiredCapabilities": {"extensions": {TASK_EXTENSION: {}}}},
        )


def task_summary(run, job):
    state = job.status if job.status in {"succeeded", "failed", "cancelled"} else run.status
    status = {
        "waiting_input": "input_required",
        "succeeded": "completed",
        "failed": "completed",
        "cancelled": "cancelled",
    }.get(state, "working")
    return {
        "taskId": str(run.id),
        "status": status,
        "createdAt": run.created_at.isoformat(),
        "lastUpdatedAt": max(run.updated_at, job.updated_at).isoformat(),
        "ttlMs": None,
        "pollIntervalMs": 2000,
        "statusMessage": job.message or run.current_stage,
    }


class InvestigationTasks(Extension):
    identifier = TASK_EXTENSION

    def __init__(self, caller, provider):
        self.caller, self.provider = caller, provider

    def methods(self):
        def safe(handler):
            async def dispatch(ctx, params):
                try:
                    return await handler(ctx, params)
                except AppError as exc:
                    raise MCPError(-32000, exc.message, {"code": exc.code}) from exc
                except ValueError as exc:
                    raise MCPError(-32602, "Invalid task input") from exc

            return dispatch

        return [
            MethodBinding("tasks/get", TaskParams, safe(self.get_task)),
            MethodBinding("tasks/update", UpdateParams, safe(self.update_task)),
            MethodBinding("tasks/cancel", TaskParams, safe(self.cancel_task)),
        ]

    async def owned(self, session, user, identifier, *, lock=False):
        run = await session.get(AgentRun, identifier)
        if run is None or run.created_by != user.user_id or not run.config_snapshot.get("mcp_task"):
            raise MCPError(-32602, "Unknown task")
        try:
            return await input_requests.run_access(session, user, identifier, lock=lock)
        except AppError as exc:
            raise MCPError(-32602, "Unknown task") from exc

    async def intercept_tool_call(self, params, ctx, call_next):
        if params.name != "start_investigation":
            return await call_next(ctx)
        negotiated(ctx)
        try:
            return await self.start(params, ctx)
        except AppError as exc:
            raise MCPError(-32000, exc.message, {"code": exc.code}) from exc
        except ValueError as exc:
            raise MCPError(-32602, "Invalid investigation input") from exc

    async def start(self, params, ctx):
        payload = StartInput.model_validate(params.arguments or {})
        async with self.caller(ctx) as (session, user):
            await memory.event_access(session, user, payload.event_id)
            key = str(payload.request_id)
            identifier = uuid5(NAMESPACE_URL, f"pipechina:mcp:task:{user.user_id}:{key}")
            lock_key = int.from_bytes(hashlib.sha256(identifier.bytes).digest()[:8], signed=True)
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
            prior = await session.get(AgentRun, identifier)
            request = payload.model_dump(mode="json")
            if prior:
                if prior.config_snapshot["mcp_task"]["request"] != request:
                    raise MCPError(-32602, "request_id was already used for different arguments")
                run = prior
            else:
                event = await memory.event_access(session, user, payload.event_id, lock=True)
                job_id, _, _ = await classify_event(
                    session,
                    event=event,
                    user=user,
                    provider=self.provider(),
                    inline=False,
                    options=payload.options,
                    invocation_id=identifier,
                    invocation_snapshot={"request_id": key, "request": request},
                )
                run = await session.scalar(select(AgentRun).where(AgentRun.job_id == job_id))
                if run is None or run.id != identifier:
                    raise MCPError(-32602, "Another investigation is active")
            job = await session.get(AsyncJob, run.job_id)
            return {"resultType": "task", **task_summary(run, job)}

    async def get_task(self, ctx, params):
        negotiated(ctx)
        async with self.caller(ctx) as (session, user):
            run = await self.owned(session, user, params.taskId)
            job = await session.get(AsyncJob, run.job_id)
            result = {"resultType": "complete", **task_summary(run, job)}
            if result["status"] == "input_required":
                request = (await input_requests.read_request(session, user, run.id))["request"]
                key = str(request["request_version"])
                result["inputRequests"] = {
                    key: {
                        "method": "elicitation/create",
                        "params": {
                            "mode": "form",
                            "message": "；".join(request["questions"]),
                            "requestedSchema": {
                                "type": "object",
                                "properties": {
                                    "confirmed_observation_ids": {
                                        "type": "string",
                                        "description": (
                                            "Newly human-confirmed observation IDs, comma-separated"
                                        ),
                                    },
                                    "reason": {"type": "string"},
                                },
                                "required": ["confirmed_observation_ids", "reason"],
                            },
                        },
                    }
                }
            elif result["status"] == "completed":
                if job.status == "succeeded":
                    from app.modules.operation_event.application.investigation_tools import (
                        authorize_knowledge_evidence,
                    )

                    await authorize_knowledge_evidence(session, user, run.id)
                completed = await session.scalar(
                    select(JobEvent)
                    .where(JobEvent.job_id == job.id, JobEvent.event_type == "succeeded")
                    .order_by(JobEvent.created_at.desc())
                    .limit(1)
                )
                data = {
                    "agent_run_id": str(run.id),
                    "job_id": str(job.id),
                    "status": job.status,
                    "result": (completed.payload.get("result", {}) if completed else {})
                    if job.status == "succeeded"
                    else {"error_code": job.error_code},
                }
                result["result"] = {
                    "content": [{"type": "text", "text": json.dumps(data)}],
                    "structuredContent": data,
                    "isError": job.status == "failed",
                }
            return result

    async def update_task(self, ctx, params):
        negotiated(ctx)
        async with self.caller(ctx) as (session, user):
            run = await self.owned(session, user, params.taskId, lock=True)
            if run.status != "waiting_input":
                return {"resultType": "complete"}
            request = (await input_requests.read_request(session, user, run.id))["request"]
            response = params.inputResponses.get(str(request["request_version"]))
            if response:
                if response.get("action") == "accept":
                    content = response.get("content", {})
                    payload = ResumeInvestigation(
                        request_version=request["request_version"],
                        confirmed_observation_ids=[
                            UUID(i.strip())
                            for i in content.get("confirmed_observation_ids", "").split(",")
                            if i.strip()
                        ],
                        reason=content.get("reason", ""),
                    )
                    await input_requests.resume(session, user, run.id, payload)
                elif response.get("action") in {"decline", "cancel"}:
                    await self._cancel(session, run)
            return {"resultType": "complete"}

    async def _cancel(self, session, run):
        if run.status in {"succeeded", "failed", "cancelled"}:
            return
        run.cancel_requested = True
        job = await session.get(AsyncJob, run.job_id)
        await request_cancel(session, job)
        if job.status in {"succeeded", "failed"}:
            run.status = job.status
            run.completed_at = job.completed_at
            await session.commit()
            return
        if run.status in {"queued", "waiting_input"}:
            run.status = "cancelled"
            run.completed_at = datetime.now(UTC)
        await session.commit()

    async def cancel_task(self, ctx, params):
        negotiated(ctx)
        async with self.caller(ctx) as (session, user):
            run = await self.owned(session, user, params.taskId, lock=True)
            await self._cancel(session, run)
        return {"resultType": "complete"}
