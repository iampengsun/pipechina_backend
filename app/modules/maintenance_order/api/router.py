import csv
import io
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from sqlalchemy import select

from app.modules.maintenance_order.application.service import (
    complete_attachment_upload,
    create_attachment,
    get_work_order,
    get_workflow,
    review_workflow,
    transition_work_order,
)
from app.modules.maintenance_order.domain.models import (
    WorkOrder,
    WorkOrderAttachment,
    WorkOrderTransition,
)
from app.modules.maintenance_order.domain.schemas import (
    DispatchInput,
    WorkOrderAcceptanceInput,
    WorkOrderResolutionInput,
    WorkOrderReviewInput,
    WorkOrderTransitionInput,
    WorkOrderView,
)
from app.modules.operation_event.application.evaluation import (
    create_evaluation,
    get_evaluation,
)
from app.modules.operation_event.application.investigation_tools import authorize_knowledge_evidence
from app.modules.operation_event.domain.agent_models import (
    AgentEvidence,
    AgentQualityEvaluation,
    AgentRun,
    AgentStep,
    AgentTask,
)
from app.modules.operation_event.domain.evaluation_schemas import AgentEvaluationCreate
from app.modules.operation_event.domain.models import ProductionEvent
from app.shared.db import SessionDep
from app.shared.media.schemas import AttachmentCreate, AttachmentUploadComplete
from app.shared.platform.models import AICallLog, UploadSession
from app.shared.platform.schemas import ReviewInput
from app.shared.security.authorization.dependencies import (
    data_scope_clause,
    require_data_scope,
    require_permission,
)
from app.shared.security.authorization.permissions import Permissions
from app.shared.security.authorization.schemas import CurrentUser

router = APIRouter(tags=["maintenance-order"])


@router.post("/agent-evaluations", status_code=201)
async def post_agent_evaluation(
    payload: AgentEvaluationCreate,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.EVENT_CLASSIFY))],
) -> dict:
    evaluation = await create_evaluation(session, payload, user)
    return {
        "id": str(evaluation.id),
        "name": evaluation.name,
        "split": evaluation.split,
        "results": evaluation.results,
    }


@router.get("/agent-evaluations/{evaluation_id}")
async def read_agent_evaluation(
    evaluation_id: UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.EVENT_CLASSIFY))],
) -> dict:
    evaluation = await get_evaluation(session, evaluation_id, user)
    return {
        "id": str(evaluation.id),
        "name": evaluation.name,
        "split": evaluation.split,
        "requested_by": str(evaluation.requested_by),
        "created_at": evaluation.created_at.isoformat(),
        "dataset": evaluation.dataset,
        "results": evaluation.results,
    }


@router.get("/agent-runs/{run_id}")
async def read_agent_run(
    run_id: UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.EVENT_READ))],
) -> dict:
    run = await session.get(AgentRun, run_id)
    if run is None:
        from app.shared.errors import NotFoundError

        raise NotFoundError("agent_run", run_id)
    require_data_scope(
        user, run.organization_unit_id, Permissions.EVENT_READ, owner_id=run.created_by
    )
    await authorize_knowledge_evidence(session, user, run.id)
    tasks = list(
        await session.scalars(
            select(AgentTask).where(AgentTask.run_id == run.id).order_by(AgentTask.created_at)
        )
    )
    from app.shared.platform.models import AsyncJob

    job = await session.get(AsyncJob, run.job_id) if run.job_id else None
    status = (
        job.status if job and job.status in {"succeeded", "failed", "cancelled"} else run.status
    )
    return {
        "id": str(run.id),
        "event_id": str(run.event_id),
        "job_id": str(run.job_id) if run.job_id else None,
        "status": status,
        "current_stage": run.current_stage,
        "configuration": {
            "limits": run.config_snapshot.get("limits", {}),
            "skills": [
                {k: v for k, v in skill.items() if k != "instructions"}
                for skill in run.config_snapshot.get("selected_skills", [])
            ],
        },
        "trace_id": str(run.trace_id),
        "usage": {
            "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens,
            "total_tokens": run.input_tokens + run.output_tokens,
            "cost_microusd": run.cost_microusd
            if any(run.config_snapshot.get("pricing_microusd_per_million", {}).values())
            else None,
            "cost_usd": run.cost_microusd / 1_000_000
            if any(run.config_snapshot.get("pricing_microusd_per_million", {}).values())
            else None,
        },
        "result": run.result,
        "tasks": [
            {
                "id": str(item.id),
                "role": item.role,
                "status": item.status,
                "step_count": item.step_count,
                "input_tokens": item.input_tokens,
                "output_tokens": item.output_tokens,
                "cost_microusd": item.cost_microusd,
                "output": item.output,
            }
            for item in tasks
        ],
    }


@router.get("/agent-runs/{run_id}/evidence")
async def read_agent_evidence(
    run_id: UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.EVENT_READ))],
) -> list[dict]:
    run = await session.get(AgentRun, run_id)
    if run is None:
        from app.shared.errors import NotFoundError

        raise NotFoundError("agent_run", run_id)
    require_data_scope(
        user, run.organization_unit_id, Permissions.EVENT_READ, owner_id=run.created_by
    )
    await authorize_knowledge_evidence(session, user, run.id)
    rows = list(await session.scalars(select(AgentEvidence).where(AgentEvidence.run_id == run.id)))
    return [
        {
            "id": str(row.id),
            "source_server": row.source_server,
            "source_resource": row.source_resource,
            "source_version": row.source_version,
            "content": row.content,
            "content_hash": row.content_hash,
        }
        for row in rows
    ]


@router.get("/agent-runs/{run_id}/quality")
async def read_agent_quality(
    run_id: UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.EVENT_READ))],
) -> dict:
    run = await session.get(AgentRun, run_id)
    if run is None:
        from app.shared.errors import NotFoundError

        raise NotFoundError("agent_run", run_id)
    require_data_scope(
        user, run.organization_unit_id, Permissions.EVENT_READ, owner_id=run.created_by
    )
    await authorize_knowledge_evidence(session, user, run.id)
    row = await session.scalar(
        select(AgentQualityEvaluation).where(AgentQualityEvaluation.run_id == run.id)
    )
    if row is None:
        return {"run_id": str(run.id), "status": "pending"}
    return {
        "run_id": str(run.id),
        "status": "completed",
        "overall_score": row.overall_score,
        "dimensions": {
            "evidence_coverage": row.evidence_coverage,
            "citation_validity": row.citation_validity,
            "completeness": row.completeness,
            "conflict_score": row.conflict_score,
            "critic_score": row.critic_score,
            "model_judge_score": row.model_judge_score,
        },
        "details": row.details,
    }


@router.get("/agent-runs/{run_id}/trace")
async def read_agent_trace(
    run_id: UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.EVENT_READ))],
) -> dict:
    run = await session.get(AgentRun, run_id)
    if run is None:
        from app.shared.errors import NotFoundError

        raise NotFoundError("agent_run", run_id)
    require_data_scope(
        user, run.organization_unit_id, Permissions.EVENT_READ, owner_id=run.created_by
    )
    await authorize_knowledge_evidence(session, user, run.id)
    steps = list(
        await session.scalars(
            select(AgentStep)
            .join(AgentTask, AgentTask.id == AgentStep.task_id)
            .where(AgentTask.run_id == run.id)
            .order_by(AgentStep.created_at)
        )
    )
    calls = list(
        await session.scalars(
            select(AICallLog).where(AICallLog.run_id == run.id).order_by(AICallLog.created_at)
        )
    )
    return {
        "run_id": str(run.id),
        "trace_id": str(run.trace_id),
        "steps": [
            {
                "id": str(step.id),
                "task_id": str(step.task_id),
                "span_id": str(step.span_id),
                "parent_span_id": str(step.parent_span_id) if step.parent_span_id else None,
                "action": step.action,
                "tool_name": step.tool_name,
                "duration_ms": step.duration_ms,
                "input_tokens": step.input_tokens,
                "output_tokens": step.output_tokens,
                "cost_microusd": step.cost_microusd,
                "observation": step.observation,
            }
            for step in steps
        ],
        "model_calls": [
            {
                "id": str(call.id),
                "provider": call.provider,
                "model": call.model_name,
                "provider_request_id": call.provider_request_id,
                "duration_ms": call.duration_ms,
                "input_tokens": call.input_units,
                "output_tokens": call.output_units,
                "status": call.status,
                "error_code": call.error_code,
            }
            for call in calls
        ],
    }


@router.post("/agent-runs/{run_id}:cancel")
async def cancel_agent_run(
    run_id: UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.EVENT_CLASSIFY))],
) -> dict:
    from app.modules.operation_event.application.input_requests import run_access

    run = await run_access(session, user, run_id, lock=True)
    require_data_scope(
        user, run.organization_unit_id, Permissions.EVENT_CLASSIFY, owner_id=run.created_by
    )
    if run.status not in {"queued", "running", "waiting_input"}:
        from app.shared.errors import ConflictError

        raise ConflictError("agent run is already terminal", status=run.status)
    run.cancel_requested = True
    if run.status in {"queued", "waiting_input"}:
        run.status = "cancelled"
        run.completed_at = datetime.now(UTC)
    if run.job_id:
        from app.shared.platform.models import AsyncJob
        from app.shared.platform.service import request_cancel

        job = await session.get(AsyncJob, run.job_id)
        if job is not None:
            await request_cancel(session, job)
    await session.commit()
    return {"id": str(run.id), "cancel_requested": True}


@router.get("/workflows/{workflow_id}")
async def read_workflow(
    workflow_id: UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.MAINTENANCE_READ))],
) -> dict:
    row = await get_workflow(session, workflow_id)
    event = await session.get(ProductionEvent, row.resource_id)
    require_data_scope(
        user,
        row.organization_unit_id,
        Permissions.MAINTENANCE_READ,
        owner_id=event.created_by if event else None,
    )
    if run_id := row.state_snapshot.get("agent_run_id"):
        await authorize_knowledge_evidence(session, user, UUID(run_id))
    return {
        "id": str(row.id),
        "workflow_type": row.workflow_type,
        "version": row.lock_version,
        "status": row.status,
        "current_node": row.current_node,
        "state": row.state_snapshot,
    }


@router.post("/workflows/{workflow_id}:review")
async def post_workflow_review(
    workflow_id: UUID,
    payload: ReviewInput,
    session: SessionDep,
    user: Annotated[
        CurrentUser,
        Depends(require_permission(Permissions.MAINTENANCE_REVIEW)),
    ],
) -> dict:
    order = await review_workflow(
        session,
        workflow=await get_workflow(session, workflow_id),
        approved=payload.approved,
        reason=payload.reason,
        user=user,
        expected_version=payload.expected_version,
    )
    return {"approved": payload.approved, "work_order_id": str(order.id) if order else None}


@router.get("/work-orders", response_model=list[WorkOrderView])
async def list_work_orders(
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.MAINTENANCE_READ))],
) -> list[WorkOrderView]:
    statement = select(WorkOrder).order_by(WorkOrder.created_at.desc()).limit(100)
    statement = statement.where(
        data_scope_clause(
            user,
            Permissions.MAINTENANCE_READ,
            WorkOrder.organization_unit_id,
            owner_column=WorkOrder.created_by,
            assignee_column=WorkOrder.assignee_id,
        )
    )
    rows = await session.scalars(statement)
    return [WorkOrderView.model_validate(row) for row in rows]


@router.get("/work-orders/{order_id}", response_model=WorkOrderView)
async def read_work_order(
    order_id: UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.MAINTENANCE_READ))],
) -> WorkOrderView:
    order = await get_work_order(session, order_id)
    require_data_scope(
        user,
        order.organization_unit_id,
        Permissions.MAINTENANCE_READ,
        owner_id=order.created_by,
        assignee_id=order.assignee_id,
    )
    return WorkOrderView.model_validate(order)


@router.get("/work-orders/{order_id}/timeline")
async def read_timeline(
    order_id: UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.MAINTENANCE_READ))],
    format: Literal["json", "csv"] = Query(default="json"),
):
    order = await get_work_order(session, order_id)
    require_data_scope(
        user,
        order.organization_unit_id,
        Permissions.MAINTENANCE_READ,
        owner_id=order.created_by,
        assignee_id=order.assignee_id,
    )
    rows = list(
        await session.scalars(
            select(WorkOrderTransition)
            .where(WorkOrderTransition.work_order_id == order.id)
            .order_by(WorkOrderTransition.occurred_at)
        )
    )
    data = [
        {
            "from_status": row.from_status,
            "to_status": row.to_status,
            "actor_id": str(row.actor_id),
            "reason": row.reason,
            "occurred_at": row.occurred_at.isoformat(),
            "version_after": row.version_after,
        }
        for row in rows
    ]
    if format == "json":
        return data
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(data[0]) if data else ["from_status"])
    writer.writeheader()
    writer.writerows(data)
    return Response(content=output.getvalue(), media_type="text/csv; charset=utf-8")


async def _transition(
    order_id: UUID,
    target: str,
    payload: WorkOrderTransitionInput,
    session: SessionDep,
    user: CurrentUser,
    permission: str,
) -> WorkOrderView:
    order = await transition_work_order(
        session,
        order=await get_work_order(session, order_id),
        target=target,
        reason=payload.reason,
        expected_version=payload.expected_version,
        user=user,
        permission=permission,
        resolution=payload if isinstance(payload, WorkOrderResolutionInput) else None,
        acceptance=payload if isinstance(payload, WorkOrderAcceptanceInput) else None,
    )
    return WorkOrderView.model_validate(order)


@router.post("/work-orders/{order_id}:submit-review", response_model=WorkOrderView)
async def submit_review(
    order_id: UUID,
    payload: WorkOrderTransitionInput,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.MAINTENANCE_WRITE))],
) -> WorkOrderView:
    return await _transition(
        order_id, "pending_review", payload, session, user, Permissions.MAINTENANCE_WRITE
    )


@router.post("/work-orders/{order_id}:review", response_model=WorkOrderView)
async def review_order(
    order_id: UUID,
    payload: WorkOrderReviewInput,
    session: SessionDep,
    user: Annotated[
        CurrentUser,
        Depends(require_permission(Permissions.MAINTENANCE_APPROVE)),
    ],
) -> WorkOrderView:
    target = "approved" if payload.approved else "draft"
    order = await transition_work_order(
        session,
        order=await get_work_order(session, order_id),
        target=target,
        reason=payload.reason,
        expected_version=payload.expected_version,
        user=user,
        permission=Permissions.MAINTENANCE_APPROVE,
    )
    return WorkOrderView.model_validate(order)


@router.post("/work-orders/{order_id}:dispatch", response_model=WorkOrderView)
async def dispatch_order(
    order_id: UUID,
    payload: DispatchInput,
    session: SessionDep,
    user: Annotated[
        CurrentUser,
        Depends(require_permission(Permissions.MAINTENANCE_DISPATCH)),
    ],
) -> WorkOrderView:
    order = await transition_work_order(
        session,
        order=await get_work_order(session, order_id),
        target="dispatched",
        reason=payload.reason,
        expected_version=payload.expected_version,
        user=user,
        permission=Permissions.MAINTENANCE_DISPATCH,
        assignee_id=payload.assignee_id,
        due_at=payload.due_at,
    )
    return WorkOrderView.model_validate(order)


@router.post("/work-orders/{order_id}:start", response_model=WorkOrderView)
async def start_order(
    order_id: UUID,
    payload: WorkOrderTransitionInput,
    session: SessionDep,
    user: Annotated[
        CurrentUser,
        Depends(require_permission(Permissions.MAINTENANCE_EXECUTE)),
    ],
) -> WorkOrderView:
    return await _transition(
        order_id, "in_progress", payload, session, user, Permissions.MAINTENANCE_EXECUTE
    )


@router.post("/work-orders/{order_id}:resolve", response_model=WorkOrderView)
async def resolve_order(
    order_id: UUID,
    payload: WorkOrderResolutionInput,
    session: SessionDep,
    user: Annotated[
        CurrentUser,
        Depends(require_permission(Permissions.MAINTENANCE_EXECUTE)),
    ],
) -> WorkOrderView:
    return await _transition(
        order_id, "resolved", payload, session, user, Permissions.MAINTENANCE_EXECUTE
    )


@router.post("/work-orders/{order_id}:close", response_model=WorkOrderView)
async def close_order(
    order_id: UUID,
    payload: WorkOrderAcceptanceInput,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.MAINTENANCE_CLOSE))],
) -> WorkOrderView:
    return await _transition(
        order_id, "closed", payload, session, user, Permissions.MAINTENANCE_CLOSE
    )


@router.post("/work-orders/{order_id}:cancel", response_model=WorkOrderView)
async def cancel_order(
    order_id: UUID,
    payload: WorkOrderTransitionInput,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.MAINTENANCE_CLOSE))],
) -> WorkOrderView:
    return await _transition(
        order_id, "cancelled", payload, session, user, Permissions.MAINTENANCE_CLOSE
    )


@router.post("/work-orders/{order_id}/attachments", status_code=201)
async def post_attachment(
    order_id: UUID,
    payload: AttachmentCreate,
    request: Request,
    session: SessionDep,
    user: Annotated[
        CurrentUser,
        Depends(require_permission(Permissions.MAINTENANCE_ATTACHMENT)),
    ],
) -> dict:
    attachment, grant = await create_attachment(
        session,
        order=await get_work_order(session, order_id),
        payload=payload,
        user=user,
        storage=request.app.state.providers.storage,
    )
    return {
        "attachment_id": str(attachment.id),
        "status": "pending",
        "object_key": grant.object_key,
        "upload_url": grant.upload_url,
        "headers": grant.headers,
    }


@router.get("/work-orders/{order_id}/attachments")
async def list_work_order_attachments(
    order_id: UUID,
    request: Request,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.MAINTENANCE_READ))],
) -> list[dict]:
    order = await get_work_order(session, order_id)
    require_data_scope(
        user,
        order.organization_unit_id,
        Permissions.MAINTENANCE_READ,
        owner_id=order.created_by,
        assignee_id=order.assignee_id,
    )
    rows = list(
        await session.execute(
            select(WorkOrderAttachment, UploadSession)
            .join(
                UploadSession,
                (UploadSession.resource_type == "work_order_attachment")
                & (UploadSession.resource_id == WorkOrderAttachment.id),
            )
            .where(WorkOrderAttachment.work_order_id == order.id)
            .order_by(WorkOrderAttachment.created_at)
        )
    )
    return [
        {
            "id": str(attachment.id),
            "filename": attachment.filename,
            "mime_type": attachment.mime_type,
            "size_bytes": attachment.size_bytes,
            "status": upload.status,
            "download_url": (
                await request.app.state.providers.storage.signed_download_url(attachment.object_key)
                if upload.status == "verified"
                else None
            ),
        }
        for attachment, upload in rows
    ]


@router.post("/work-orders/{order_id}/attachments/{attachment_id}/uploads:complete")
async def post_attachment_upload_complete(
    order_id: UUID,
    attachment_id: UUID,
    payload: AttachmentUploadComplete,
    request: Request,
    session: SessionDep,
    user: Annotated[
        CurrentUser,
        Depends(require_permission(Permissions.MAINTENANCE_ATTACHMENT)),
    ],
) -> dict:
    upload = await complete_attachment_upload(
        session,
        order=await get_work_order(session, order_id),
        attachment_id=attachment_id,
        user=user,
        storage=request.app.state.providers.storage,
        server_sha256=payload.server_sha256,
    )
    return {"attachment_id": str(attachment_id), "status": upload.status}
