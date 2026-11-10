"""Store measured before/after outcomes and an independent, version-bound acceptance."""

from sqlalchemy import select

from app.modules.maintenance_order.domain.models import (
    WorkOrderAcceptance,
    WorkOrderAttachment,
    WorkOrderResolution,
)
from app.shared.errors import ConflictError
from app.shared.measurements import measurements_by_key, verify_measurements
from app.shared.platform.models import UploadSession


async def save_resolution(session, order, payload, user):
    if payload is None:
        raise ConflictError("resolution requires action, result and measured retest evidence")
    measurements_by_key(payload.measurements)
    if len(set(payload.attachment_ids)) != len(payload.attachment_ids):
        raise ConflictError("duplicate retest attachments")
    for attachment_id in payload.attachment_ids:
        attachment = await session.get(WorkOrderAttachment, attachment_id)
        verified = await session.scalar(
            select(UploadSession.id).where(
                UploadSession.resource_type == "work_order_attachment",
                UploadSession.resource_id == attachment_id,
                UploadSession.status == "verified",
            )
        )
        if attachment is None or attachment.work_order_id != order.id or not verified:
            raise ConflictError("retest attachments must be verified and belong to this order")
    row = WorkOrderResolution(
        work_order_id=order.id,
        order_version=order.version + 1,
        action=payload.action,
        result=payload.result,
        measurements=[m.model_dump(mode="json") for m in payload.measurements],
        attachment_ids=[str(key) for key in payload.attachment_ids],
        submitted_by=user.user_id,
    )
    session.add(row)


async def save_acceptance(session, order, payload, user):
    if payload is None:
        raise ConflictError(
            "closure requires an independent acceptance and verification measurements"
        )
    resolution = await session.get(WorkOrderResolution, payload.resolution_id)
    if (
        resolution is None
        or resolution.work_order_id != order.id
        or resolution.order_version != order.version
    ):
        raise ConflictError("acceptance must refer to the current resolution")
    if user.user_id in {resolution.submitted_by, order.assignee_id}:
        raise ConflictError("work order acceptance requires an independent reviewer")
    verify_measurements(resolution.measurements, payload.measurements, approved=payload.approved)
    session.add(
        WorkOrderAcceptance(
            work_order_id=order.id,
            resolution_id=resolution.id,
            approved=payload.approved,
            measurements=[m.model_dump(mode="json") for m in payload.measurements],
            reason=payload.reason,
            reviewed_by=user.user_id,
        )
    )
