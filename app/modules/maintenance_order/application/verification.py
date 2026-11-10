"""Read measured order history for REST and external acceptance assistants."""

from sqlalchemy import select

from app.modules.maintenance_order.application.service import get_work_order
from app.modules.maintenance_order.domain.models import WorkOrderAcceptance, WorkOrderResolution
from app.modules.maintenance_order.domain.schemas import (
    WorkOrderAcceptanceView,
    WorkOrderResolutionView,
)
from app.shared.security.authorization.dependencies import require_data_scope
from app.shared.security.authorization.permissions import Permissions as P


async def read_resolutions(session, user, order_id):
    order = await get_work_order(session, order_id)
    require_data_scope(
        user,
        order.organization_unit_id,
        P.MAINTENANCE_READ,
        owner_id=order.created_by,
        assignee_id=order.assignee_id,
    )
    rows = await session.scalars(
        select(WorkOrderResolution)
        .where(WorkOrderResolution.work_order_id == order_id)
        .order_by(WorkOrderResolution.order_version)
    )
    acceptances = await session.scalars(
        select(WorkOrderAcceptance).where(WorkOrderAcceptance.work_order_id == order_id)
    )
    by_resolution = {
        row.resolution_id: WorkOrderAcceptanceView.model_validate(row) for row in acceptances
    }
    return order, [
        WorkOrderResolutionView.model_validate(row).model_copy(
            update={"acceptance": by_resolution.get(row.id)}
        )
        for row in rows
    ]


async def verification_context(session, user, order_id):
    order, history = await read_resolutions(session, user, order_id)
    latest = history[-1] if history else None
    return {
        "work_order_id": str(order.id),
        "title": order.title,
        "status": order.status,
        "expected_version": order.version,
        "event_id": str(order.event_id) if order.event_id else None,
        "resolutions": [row.model_dump(mode="json") for row in history],
        "current_resolution_id": str(latest.id)
        if latest and latest.order_version == order.version
        else None,
        "independent_reviewer": bool(
            latest and user.user_id not in {latest.submitted_by, order.assignee_id}
        ),
        "instruction": (
            "核查措施、阈值依据和前后读数；独立验收必须提交本次实际复测值，并通过业务审批接口办理。"
        ),
    }
