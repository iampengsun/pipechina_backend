"""Track overdue reminders through acknowledgement and automatic resolution."""

from datetime import UTC, datetime

from sqlalchemy import or_, select

from app.modules.maintenance_order.domain.models import WorkOrder, WorkOrderReminder
from app.shared.security.identity.models import UserAccount


async def reminder_recipient(session, order):
    account = await session.get(UserAccount, order.assignee_id or order.created_by)
    if account and account.active and account.organization_unit_id == order.organization_unit_id:
        return account.id
    return order.created_by


async def refresh_reminders(session, *, now=None):
    now = now or datetime.now(UTC)
    created = 0
    orders = await session.scalars(
        select(WorkOrder)
        .where(
            or_(
                WorkOrder.due_at.is_not(None),
                WorkOrder.id.in_(select(WorkOrderReminder.work_order_id)),
            )
        )
        .order_by(WorkOrder.id)
        .with_for_update(skip_locked=True)
    )
    for order in orders:
        overdue = (
            order.due_at is not None
            and order.due_at <= now
            and order.status not in {"closed", "cancelled"}
        )
        rows = list(
            await session.scalars(
                select(WorkOrderReminder)
                .where(WorkOrderReminder.work_order_id == order.id)
                .with_for_update()
            )
        )
        for row in rows:
            if (not overdue or row.scheduled_at != order.due_at) and row.status != "resolved":
                row.status = "resolved"
                row.version += 1
        if not overdue:
            continue
        recipient = await reminder_recipient(session, order)
        row = next(
            (r for r in rows if r.scheduled_at == order.due_at and r.reminder_type == "overdue"),
            None,
        )
        if row is None:
            session.add(
                WorkOrderReminder(
                    work_order_id=order.id,
                    reminder_type="overdue",
                    scheduled_at=order.due_at,
                    status="pending",
                    recipient_id=recipient,
                    version=1,
                    payload={"order_no": order.order_no},
                )
            )
            created += 1
        elif row.recipient_id != recipient or row.status == "resolved":
            row.recipient_id = recipient
            row.status = "pending"
            row.acknowledged_by = row.acknowledged_at = row.acknowledge_reason = None
            row.version += 1
    await session.commit()
    return created
