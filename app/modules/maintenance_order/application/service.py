from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.maintenance_order.application.workflow import resolve_maintenance_review
from app.modules.maintenance_order.domain.models import (
    AbnormalityAssessment,
    WorkOrder,
    WorkOrderAttachment,
    WorkOrderTransition,
)
from app.modules.maintenance_order.infrastructure.repository import MaintenanceOrderRepository
from app.modules.operation_event.application.investigation_tools import authorize_knowledge_evidence
from app.modules.operation_event.domain.models import ProductionEvent, ProductionEventVersion
from app.ports.storage import StorageProvider, UploadGrant
from app.shared.errors import ConflictError, NotFoundError
from app.shared.media.names import safe_object_filename
from app.shared.media.schemas import AttachmentCreate
from app.shared.media.uploads import verify_pending_upload
from app.shared.platform.models import UploadSession, WorkflowRun
from app.shared.platform.service import add_audit
from app.shared.security.authorization.dependencies import require_data_scope
from app.shared.security.authorization.permissions import Permissions
from app.shared.security.authorization.schemas import CurrentUser

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"pending_review", "cancelled"},
    "pending_review": {"approved", "draft", "cancelled"},
    "approved": {"dispatched", "cancelled"},
    "dispatched": {"in_progress", "cancelled"},
    "in_progress": {"resolved", "cancelled"},
    "resolved": {"closed", "in_progress", "cancelled"},
    "closed": set(),
    "cancelled": set(),
}


async def get_workflow(session: AsyncSession, workflow_id: UUID) -> WorkflowRun:
    workflow = await MaintenanceOrderRepository(session).get_workflow(workflow_id)
    if workflow is None:
        raise NotFoundError("workflow", workflow_id)
    return workflow


async def review_workflow(
    session: AsyncSession,
    *,
    workflow: WorkflowRun,
    approved: bool,
    reason: str,
    user: CurrentUser,
    expected_version: int | None = None,
) -> WorkOrder | None:
    locked = await session.scalar(
        select(WorkflowRun)
        .where(WorkflowRun.id == workflow.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked is None:
        raise NotFoundError("workflow", workflow.id)
    workflow = locked
    if expected_version is not None and workflow.lock_version != expected_version:
        raise ConflictError("workflow version changed", version=workflow.lock_version)
    event = await session.get(ProductionEvent, workflow.resource_id)
    if event is None:
        raise NotFoundError("production_event", workflow.resource_id)
    require_data_scope(
        user,
        workflow.organization_unit_id,
        Permissions.MAINTENANCE_REVIEW,
        owner_id=event.created_by,
    )
    if workflow.status != "awaiting_review":
        raise ConflictError("workflow is not awaiting review", status=workflow.status)
    source_version = workflow.state_snapshot.get("event_version_id")
    await session.refresh(event, with_for_update=True)
    if (
        approved
        and source_version
        and (
            event.business_status != "confirmed" or str(event.current_version_id) != source_version
        )
    ):
        raise ConflictError("event changed; a new assessment is required")
    if approved and "observation_revision" in workflow.state_snapshot:
        from app.modules.operation_event.application.memory import observation_revision

        if workflow.state_snapshot["observation_revision"] != await observation_revision(
            session, event
        ):
            raise ConflictError("case observations changed; a new investigation is required")
    if approved and (run_id := workflow.state_snapshot.get("agent_run_id")):
        await authorize_knowledge_evidence(
            session, user, UUID(run_id), require_current_context=True
        )
    assessment_id = UUID(workflow.state_snapshot["assessment_id"])
    assessment = await session.get(AbnormalityAssessment, assessment_id)
    if assessment is None:
        raise NotFoundError("abnormality_assessment", assessment_id)
    assessment.reviewed_by = user.user_id
    assessment.review_reason = reason
    decision = await resolve_maintenance_review(approved=approved, reason=reason)
    workflow.state_snapshot = {
        **workflow.state_snapshot,
        "node": decision,
        "review": {"approved": approved, "reason": reason, "reviewed_by": str(user.user_id)},
    }
    workflow.lock_version += 1
    if decision == "rejected":
        assessment.review_status = "rejected"
        workflow.status = "rejected"
        workflow.current_node = "rejected"
        await add_audit(
            session,
            user=user,
            action="maintenance_assessment.reject",
            resource_type="workflow",
            resource_id=workflow.id,
            reason=reason,
        )
        await session.commit()
        return None
    assessment.review_status = "approved"
    workflow.status = "completed"
    workflow.current_node = "approved"
    existing = await session.scalar(
        select(WorkOrder).where(WorkOrder.source_workflow_run_id == workflow.id)
    )
    if existing:
        return existing
    version = await session.get(ProductionEventVersion, event.current_version_id)
    description = version.description if version else event.title
    order = WorkOrder(
        organization_unit_id=workflow.organization_unit_id,
        event_id=assessment.event_id,
        source_workflow_run_id=workflow.id,
        order_no=f"WO-{datetime.now(UTC):%Y%m%d}-{str(workflow.id)[:8].upper()}",
        title=event.title,
        description=f"{description}\n建议：{assessment.recommended_action}",
        risk_level=assessment.risk_level,
        status="draft",
        version=1,
        created_by=user.user_id,
    )
    session.add(order)
    await session.flush()
    session.add(
        WorkOrderTransition(
            work_order_id=order.id,
            from_status="none",
            to_status="draft",
            actor_id=user.user_id,
            reason="approved assessment created work order",
            occurred_at=datetime.now(UTC),
            version_after=order.version,
        )
    )
    await add_audit(
        session,
        user=user,
        action="work_order.create",
        resource_type="work_order",
        resource_id=order.id,
        reason=reason,
    )
    await session.commit()
    return order


async def get_work_order(session: AsyncSession, order_id: UUID) -> WorkOrder:
    order = await MaintenanceOrderRepository(session).get_work_order(order_id)
    if order is None:
        raise NotFoundError("work_order", order_id)
    return order


async def transition_work_order(
    session: AsyncSession,
    *,
    order: WorkOrder,
    target: str,
    reason: str,
    expected_version: int,
    user: CurrentUser,
    permission: str,
    assignee_id: UUID | None = None,
    due_at: datetime | None = None,
    resolution=None,
    acceptance=None,
) -> WorkOrder:
    await session.refresh(order, with_for_update=True)
    require_data_scope(
        user,
        order.organization_unit_id,
        permission,
        owner_id=order.created_by,
        assignee_id=order.assignee_id,
    )
    if order.version != expected_version:
        raise ConflictError(
            "work order version mismatch", expected=expected_version, actual=order.version
        )
    if target not in ALLOWED_TRANSITIONS.get(order.status, set()):
        raise ConflictError(
            "illegal work order transition", from_status=order.status, to_status=target
        )
    if assignee_id is not None:
        from app.shared.security.authorization.participants import validate_assignee

        await validate_assignee(
            session,
            assignee_id,
            order.organization_unit_id,
            permissions=[Permissions.MAINTENANCE_READ, Permissions.MAINTENANCE_EXECUTE],
            owner_id=order.created_by,
        )
    if due_at is not None and due_at <= datetime.now(UTC):
        raise ConflictError("dispatch deadline must be in the future")
    from app.modules.maintenance_order.application.acceptance import (
        save_acceptance,
        save_resolution,
    )

    if target == "resolved":
        await save_resolution(session, order, resolution, user)
    if acceptance is not None or target == "closed":
        await save_acceptance(session, order, acceptance, user)
        target = "closed" if acceptance.approved else "in_progress"
    before = order.status
    order.status = target
    order.version += 1
    if assignee_id is not None:
        order.assignee_id = assignee_id
    if due_at is not None:
        order.due_at = due_at
    session.add(
        WorkOrderTransition(
            work_order_id=order.id,
            from_status=before,
            to_status=target,
            actor_id=user.user_id,
            reason=reason,
            occurred_at=datetime.now(UTC),
            version_after=order.version,
        )
    )
    await add_audit(
        session,
        user=user,
        action=f"work_order.{target}",
        resource_type="work_order",
        resource_id=order.id,
        before={"status": before},
        after={"status": target, "version": order.version},
        reason=reason,
    )
    if target in {"closed", "cancelled"}:
        from app.modules.maintenance_order.domain.models import WorkOrderReminder

        reminders = await session.scalars(
            select(WorkOrderReminder)
            .where(
                WorkOrderReminder.work_order_id == order.id,
                WorkOrderReminder.status.in_(["pending", "acknowledged"]),
            )
            .with_for_update()
        )
        for reminder in reminders:
            reminder.status = "resolved"
            reminder.version += 1
    await session.commit()
    return order


async def create_attachment(
    session: AsyncSession,
    *,
    order: WorkOrder,
    payload: AttachmentCreate,
    user: CurrentUser,
    storage: StorageProvider,
) -> tuple[WorkOrderAttachment, UploadGrant]:
    require_data_scope(
        user,
        order.organization_unit_id,
        Permissions.MAINTENANCE_ATTACHMENT,
        owner_id=order.created_by,
        assignee_id=order.assignee_id,
    )
    attachment = WorkOrderAttachment(
        work_order_id=order.id,
        object_key="pending",
        filename=payload.filename,
        mime_type=payload.mime_type,
        size_bytes=payload.size_bytes,
        uploaded_by=user.user_id,
    )
    session.add(attachment)
    await session.flush()
    attachment.object_key = (
        f"work-orders/{order.id}/{attachment.id}/{safe_object_filename(payload.filename)}"
    )
    grant = await storage.create_upload(
        object_key=attachment.object_key,
        mime_type=payload.mime_type,
        size_bytes=payload.size_bytes,
    )
    session.add(
        UploadSession(
            resource_type="work_order_attachment",
            resource_id=attachment.id,
            organization_unit_id=order.organization_unit_id,
            object_key=attachment.object_key,
            filename=attachment.filename,
            mime_type=attachment.mime_type,
            size_bytes=attachment.size_bytes,
            client_sha256=payload.sha256,
            status="pending",
            expires_at=datetime.now(UTC) + timedelta(seconds=grant.expires_in),
        )
    )
    await session.commit()
    return attachment, grant


async def complete_attachment_upload(
    session: AsyncSession,
    *,
    order: WorkOrder,
    attachment_id: UUID,
    user: CurrentUser,
    storage: StorageProvider,
    server_sha256: str | None,
) -> UploadSession:
    require_data_scope(
        user,
        order.organization_unit_id,
        Permissions.MAINTENANCE_ATTACHMENT,
        owner_id=order.created_by,
        assignee_id=order.assignee_id,
    )
    attachment = await session.get(WorkOrderAttachment, attachment_id)
    if attachment is None or attachment.work_order_id != order.id:
        raise NotFoundError("work_order_attachment", attachment_id)
    upload = await session.scalar(
        select(UploadSession)
        .where(
            UploadSession.resource_type == "work_order_attachment",
            UploadSession.resource_id == attachment.id,
        )
        .with_for_update()
    )
    upload = await verify_pending_upload(session, upload, storage, server_sha256=server_sha256)
    await add_audit(
        session,
        user=user,
        action="work_order.attachment_verified",
        resource_type="work_order_attachment",
        resource_id=attachment.id,
    )
    await session.commit()
    return upload
