"""Measured work outcomes and actionable station reminders; no external messaging."""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from app.modules.maintenance_order.application.reminders import reminder_recipient
from app.modules.maintenance_order.domain.models import (
    WorkOrder,
    WorkOrderReminder,
)
from app.modules.maintenance_order.domain.schemas import (
    ReminderAcknowledgeInput,
    WorkOrderReminderView,
    WorkOrderResolutionView,
)
from app.shared.db import SessionDep
from app.shared.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.shared.platform.service import add_audit
from app.shared.security.authorization.dependencies import (
    data_scope_clause,
    require_data_scope,
    require_permission,
)
from app.shared.security.authorization.permissions import Permissions as P
from app.shared.security.authorization.schemas import CurrentUser

router = APIRouter(tags=["maintenance-order"])
Reader = Annotated[CurrentUser, Depends(require_permission(P.MAINTENANCE_READ))]


def check_order(order, user):
    require_data_scope(
        user,
        order.organization_unit_id,
        P.MAINTENANCE_READ,
        owner_id=order.created_by,
        assignee_id=order.assignee_id,
    )


@router.get("/work-orders/{order_id}/resolutions", response_model=list[WorkOrderResolutionView])
async def resolutions(order_id: UUID, session: SessionDep, user: Reader):
    from app.modules.maintenance_order.application.verification import read_resolutions

    _, rows = await read_resolutions(session, user, order_id)
    return rows


@router.get("/work-order-reminders", response_model=list[WorkOrderReminderView])
async def reminders(
    session: SessionDep,
    user: Reader,
    status: Literal["pending", "acknowledged", "resolved"] | None = None,
    mine_only: bool = True,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    statement = (
        select(WorkOrderReminder)
        .join(WorkOrder, WorkOrder.id == WorkOrderReminder.work_order_id)
        .where(
            data_scope_clause(
                user,
                P.MAINTENANCE_READ,
                WorkOrder.organization_unit_id,
                owner_column=WorkOrder.created_by,
                assignee_column=WorkOrder.assignee_id,
            )
        )
    )
    if mine_only:
        statement = statement.where(WorkOrderReminder.recipient_id == user.user_id)
    if status:
        statement = statement.where(WorkOrderReminder.status == status)
    rows = await session.scalars(
        statement.order_by(WorkOrderReminder.scheduled_at, WorkOrderReminder.id)
        .limit(limit)
        .offset(offset)
    )
    return list(rows)


@router.post(
    "/work-order-reminders/{reminder_id}:acknowledge", response_model=WorkOrderReminderView
)
async def acknowledge(
    reminder_id: UUID, payload: ReminderAcknowledgeInput, session: SessionDep, user: Reader
):
    reference = await session.get(WorkOrderReminder, reminder_id)
    if reference is None:
        raise NotFoundError("work_order_reminder", reminder_id)
    # Same lock order as generation and closing: order, then reminder.
    order = await session.get(
        WorkOrder, reference.work_order_id, with_for_update=True, populate_existing=True
    )
    if order is None:
        raise NotFoundError("work_order", reference.work_order_id)
    check_order(order, user)
    row = await session.get(
        WorkOrderReminder, reminder_id, with_for_update=True, populate_existing=True
    )
    if row is None:
        raise NotFoundError("work_order_reminder", reminder_id)
    if row.recipient_id != user.user_id or await reminder_recipient(session, order) != user.user_id:
        raise PermissionDeniedError(P.MAINTENANCE_READ)
    if row.version != payload.expected_version or row.status != "pending":
        raise ConflictError("reminder changed or has already been acknowledged")
    if (
        order.status in {"closed", "cancelled"}
        or order.due_at is None
        or order.due_at > datetime.now(UTC)
        or row.scheduled_at != order.due_at
    ):
        raise ConflictError("reminder is no longer overdue")
    row.status = "acknowledged"
    row.version += 1
    row.acknowledged_by = user.user_id
    row.acknowledged_at = datetime.now(UTC)
    row.acknowledge_reason = payload.reason
    await add_audit(
        session,
        user=user,
        action="work_order.reminder.acknowledge",
        resource_type="work_order_reminder",
        resource_id=row.id,
        reason=payload.reason,
    )
    await session.commit()
    await session.refresh(row)
    return row
