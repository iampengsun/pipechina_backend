from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.hazard.domain.models import (
    Hazard,
    HazardAcceptance,
    HazardAssessment,
    HazardRectification,
    HazardTransition,
    HazardWorkOrderLink,
)
from app.modules.hazard.domain.schemas import (
    HazardAcceptanceInput,
    HazardAssessmentInput,
    HazardCreate,
    HazardRectificationInput,
    HazardStartInput,
)
from app.modules.maintenance_order.domain.models import WorkOrder
from app.shared.errors import ConflictError, NotFoundError
from app.shared.platform.service import add_audit
from app.shared.security.authorization.dependencies import require_data_scope
from app.shared.security.authorization.permissions import Permissions
from app.shared.security.authorization.schemas import CurrentUser


def risk_level_for(score: int) -> str:
    if score <= 4:
        return "low"
    if score <= 9:
        return "medium"
    if score <= 16:
        return "high"
    return "major"


async def get_hazard(session: AsyncSession, hazard_id: UUID, *, for_update: bool = False) -> Hazard:
    statement = select(Hazard).where(Hazard.id == hazard_id)
    if for_update:
        statement = statement.with_for_update()
    row = await session.scalar(statement)
    if row is None:
        raise NotFoundError("hazard", hazard_id)
    return row


def _check_version(hazard: Hazard, expected: int) -> None:
    if hazard.version != expected:
        raise ConflictError(
            "hazard was changed by another request",
            expected_version=expected,
            current_version=hazard.version,
        )


async def _transition(
    session: AsyncSession,
    *,
    hazard: Hazard,
    target: str,
    reason: str,
    user: CurrentUser,
) -> None:
    previous = hazard.status
    hazard.status = target
    hazard.version += 1
    session.add(
        HazardTransition(
            hazard_id=hazard.id,
            from_status=previous,
            to_status=target,
            actor_id=user.user_id,
            reason=reason,
            occurred_at=datetime.now(UTC),
            version_after=hazard.version,
        )
    )


async def validate_source(session, payload, user, organization_id):
    if payload.source_type == "manual":
        if payload.source_id is not None:
            raise ConflictError("manual hazards must not claim an external source id")
        return
    if payload.source_id is None:
        raise ConflictError("a non-manual hazard requires a real source id")
    from app.modules.handover.domain.models import AudioRecord, ManualOperationRecord
    from app.modules.inspection.domain.models import InspectionFinding, InspectionRecord
    from app.modules.meeting.application.service import _check_acl
    from app.modules.meeting.domain.models import Meeting
    from app.modules.operation_event.domain.models import ProductionEvent

    permission = Permissions.EVENT_READ
    if payload.source_type == "inspection_finding":
        finding = await session.get(InspectionFinding, payload.source_id)
        if finding is None:
            raise NotFoundError("inspection_finding", payload.source_id)
        if finding.review_status != "confirmed":
            raise ConflictError("source finding must be confirmed")
        row = await session.get(InspectionRecord, finding.inspection_id)
        permission = Permissions.INSPECTION_READ
    elif payload.source_type == "production_event":
        row = await session.get(ProductionEvent, payload.source_id)
        if row is not None and row.business_status != "confirmed":
            raise ConflictError("source event must be confirmed")
    elif payload.source_type == "meeting":
        row = await session.get(Meeting, payload.source_id)
        permission = Permissions.MEETING_READ
        if row is not None:
            _check_acl(row, user, permission)
    else:
        row = await session.get(AudioRecord, payload.source_id)
        if row is None:
            row = await session.get(ManualOperationRecord, payload.source_id)
        permission = Permissions.HANDOVER_READ
        if row is not None and (
            getattr(row, "deleted", False) or row.business_status != "confirmed"
        ):
            raise ConflictError("source handover must be confirmed and available")
    if row is None:
        raise NotFoundError(payload.source_type, payload.source_id)
    require_data_scope(user, row.organization_unit_id, permission, owner_id=row.created_by)
    if row.organization_unit_id != organization_id:
        raise ConflictError("hazard and source must belong to the same organization")


async def create_hazard(session: AsyncSession, payload: HazardCreate, user: CurrentUser) -> Hazard:
    organization_id = payload.organization_unit_id or user.organization_unit_id
    require_data_scope(user, organization_id, Permissions.HAZARD_CREATE, owner_id=user.user_id)
    await validate_source(session, payload, user, organization_id)
    row = Hazard(
        organization_unit_id=organization_id,
        hazard_no=f"HZ-{datetime.now(UTC):%Y%m%d}-{uuid4().hex[:8].upper()}",
        title=payload.title,
        description=payload.description,
        location=payload.location,
        category=payload.category,
        source_type=payload.source_type,
        source_id=payload.source_id,
        status="pending_assessment",
        version=1,
        created_by=user.user_id,
    )
    session.add(row)
    await session.flush()
    await add_audit(
        session,
        user=user,
        action="hazard.create",
        resource_type="hazard",
        resource_id=row.id,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def assess_hazard(
    session: AsyncSession,
    hazard_id: UUID,
    payload: HazardAssessmentInput,
    user: CurrentUser,
) -> Hazard:
    hazard = await get_hazard(session, hazard_id, for_update=True)
    require_data_scope(
        user, hazard.organization_unit_id, Permissions.HAZARD_ASSESS, owner_id=hazard.created_by
    )
    _check_version(hazard, payload.expected_version)
    if hazard.status != "pending_assessment":
        raise ConflictError("hazard is not pending assessment", status=hazard.status)
    due_at = payload.due_at
    if due_at <= datetime.now(UTC):
        raise ConflictError("rectification due time must be in the future")
    score = payload.likelihood * payload.consequence
    level = risk_level_for(score)
    assessment_version = (
        await session.scalar(
            select(func.coalesce(func.max(HazardAssessment.version), 0)).where(
                HazardAssessment.hazard_id == hazard.id
            )
        )
        or 0
    ) + 1
    session.add(
        HazardAssessment(
            hazard_id=hazard.id,
            version=assessment_version,
            likelihood=payload.likelihood,
            consequence=payload.consequence,
            risk_score=score,
            risk_level=level,
            rationale=payload.rationale,
            assessed_by=user.user_id,
        )
    )
    hazard.likelihood = payload.likelihood
    hazard.consequence = payload.consequence
    hazard.risk_score = score
    hazard.risk_level = level
    hazard.control_measures = payload.control_measures
    from app.shared.security.authorization.participants import validate_assignee

    await validate_assignee(
        session,
        payload.assignee_id,
        hazard.organization_unit_id,
        permissions=[Permissions.HAZARD_READ, Permissions.HAZARD_RECTIFY],
        owner_id=hazard.created_by,
    )
    hazard.assignee_id = payload.assignee_id
    hazard.due_at = due_at
    await _transition(
        session,
        hazard=hazard,
        target="pending_rectification",
        reason=payload.rationale,
        user=user,
    )
    await add_audit(
        session,
        user=user,
        action="hazard.assess",
        resource_type="hazard",
        resource_id=hazard.id,
        after={"risk_level": level, "risk_score": score},
        reason=payload.rationale,
    )
    await session.commit()
    await session.refresh(hazard)
    return hazard


async def start_rectification(
    session: AsyncSession, hazard_id: UUID, payload: HazardStartInput, user: CurrentUser
) -> Hazard:
    hazard = await get_hazard(session, hazard_id, for_update=True)
    require_data_scope(
        user,
        hazard.organization_unit_id,
        Permissions.HAZARD_RECTIFY,
        owner_id=hazard.created_by,
        assignee_id=hazard.assignee_id,
    )
    _check_version(hazard, payload.expected_version)
    if hazard.status != "pending_rectification":
        raise ConflictError("hazard is not pending rectification", status=hazard.status)
    await _transition(session, hazard=hazard, target="rectifying", reason=payload.reason, user=user)
    await add_audit(
        session,
        user=user,
        action="hazard.rectification.start",
        resource_type="hazard",
        resource_id=hazard.id,
        reason=payload.reason,
    )
    await session.commit()
    await session.refresh(hazard)
    return hazard


async def submit_rectification(
    session: AsyncSession,
    hazard_id: UUID,
    payload: HazardRectificationInput,
    user: CurrentUser,
) -> Hazard:
    hazard = await get_hazard(session, hazard_id, for_update=True)
    require_data_scope(
        user,
        hazard.organization_unit_id,
        Permissions.HAZARD_RECTIFY,
        owner_id=hazard.created_by,
        assignee_id=hazard.assignee_id,
    )
    _check_version(hazard, payload.expected_version)
    if hazard.status != "rectifying":
        raise ConflictError("hazard is not being rectified", status=hazard.status)
    from app.modules.hazard.domain.models import HazardAttachment
    from app.shared.measurements import measurements_by_key
    from app.shared.platform.models import UploadSession

    measurements_by_key(payload.measurements)
    if len(set(payload.attachment_ids)) != len(payload.attachment_ids):
        raise ConflictError("duplicate rectification attachments")
    for key in payload.attachment_ids:
        attachment = await session.get(HazardAttachment, key)
        upload = await session.scalar(
            select(UploadSession).where(
                UploadSession.resource_type == "hazard_attachment",
                UploadSession.resource_id == key,
                UploadSession.status == "verified",
            )
        )
        if attachment is None or attachment.hazard_id != hazard.id or upload is None:
            raise ConflictError(
                "rectification attachments must be verified and belong to this hazard"
            )
    session.add(
        HazardRectification(
            hazard_version=hazard.version + 1,
            measurements=[m.model_dump(mode="json") for m in payload.measurements],
            attachment_ids=[str(key) for key in payload.attachment_ids],
            hazard_id=hazard.id,
            action=payload.action,
            result=payload.result,
            submitted_by=user.user_id,
            submitted_at=datetime.now(UTC),
        )
    )
    await _transition(
        session,
        hazard=hazard,
        target="pending_acceptance",
        reason=payload.result,
        user=user,
    )
    await add_audit(
        session,
        user=user,
        action="hazard.rectification.submit",
        resource_type="hazard",
        resource_id=hazard.id,
        reason=payload.result,
    )
    await session.commit()
    await session.refresh(hazard)
    return hazard


async def accept_hazard(
    session: AsyncSession,
    hazard_id: UUID,
    payload: HazardAcceptanceInput,
    user: CurrentUser,
    *,
    approved: bool,
) -> Hazard:
    hazard = await get_hazard(session, hazard_id, for_update=True)
    require_data_scope(
        user, hazard.organization_unit_id, Permissions.HAZARD_ACCEPT, owner_id=hazard.created_by
    )
    _check_version(hazard, payload.expected_version)
    if hazard.status != "pending_acceptance":
        raise ConflictError("hazard is not pending acceptance", status=hazard.status)
    from app.shared.measurements import verify_measurements

    rectification = await session.get(HazardRectification, payload.rectification_id)
    if (
        rectification is None
        or rectification.hazard_id != hazard.id
        or rectification.hazard_version != hazard.version
    ):
        raise ConflictError("acceptance requires the current measured rectification")
    if user.user_id in {rectification.submitted_by, hazard.assignee_id}:
        raise ConflictError("hazard acceptance requires an independent reviewer")
    verify_measurements(rectification.measurements, payload.measurements, approved=approved)
    now = datetime.now(UTC)
    decision = "accepted" if approved else "rejected"
    session.add(
        HazardAcceptance(
            rectification_id=rectification.id,
            measurements=[m.model_dump(mode="json") for m in payload.measurements],
            hazard_id=hazard.id,
            decision=decision,
            reason=payload.reason,
            accepted_by=user.user_id,
            accepted_at=now,
        )
    )
    target = "closed" if approved else "rectifying"
    await _transition(session, hazard=hazard, target=target, reason=payload.reason, user=user)
    if approved:
        hazard.closed_by = user.user_id
        hazard.closed_at = now
    await add_audit(
        session,
        user=user,
        action=f"hazard.acceptance.{decision}",
        resource_type="hazard",
        resource_id=hazard.id,
        reason=payload.reason,
    )
    await session.commit()
    await session.refresh(hazard)
    return hazard


async def link_work_order(
    session: AsyncSession, hazard: Hazard, work_order_id: UUID, user: CurrentUser
) -> HazardWorkOrderLink:
    require_data_scope(
        user, hazard.organization_unit_id, Permissions.HAZARD_LINK, owner_id=hazard.created_by
    )
    work_order = await session.get(WorkOrder, work_order_id)
    if work_order is None:
        raise NotFoundError("work_order", work_order_id)
    require_data_scope(user, work_order.organization_unit_id, Permissions.MAINTENANCE_READ)
    if work_order.organization_unit_id != hazard.organization_unit_id:
        raise ConflictError("hazard and work order must belong to the same organization")
    existing = await session.scalar(
        select(HazardWorkOrderLink).where(
            HazardWorkOrderLink.hazard_id == hazard.id,
            HazardWorkOrderLink.work_order_id == work_order.id,
        )
    )
    if existing is not None:
        return existing
    link = HazardWorkOrderLink(
        hazard_id=hazard.id, work_order_id=work_order.id, created_by=user.user_id
    )
    session.add(link)
    await add_audit(
        session,
        user=user,
        action="hazard.link_work_order",
        resource_type="hazard",
        resource_id=hazard.id,
        after={"work_order_id": str(work_order.id)},
    )
    await session.commit()
    await session.refresh(link)
    return link
