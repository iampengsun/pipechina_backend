from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from app.modules.hazard.application.service import (
    accept_hazard,
    assess_hazard,
    create_hazard,
    get_hazard,
    link_work_order,
    start_rectification,
    submit_rectification,
)
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
    HazardView,
    HazardWorkOrderLinkInput,
)
from app.shared.db import SessionDep
from app.shared.security.authorization.dependencies import (
    data_scope_clause,
    require_data_scope,
    require_permission,
)
from app.shared.security.authorization.permissions import Permissions
from app.shared.security.authorization.schemas import CurrentUser

router = APIRouter(prefix="/hazards", tags=["hazard"])


def _view(row: Hazard) -> dict:
    data = HazardView.model_validate(row).model_dump(mode="json")
    due_at = row.due_at
    if due_at is not None and due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=UTC)
    data["overdue"] = bool(
        due_at is not None
        and due_at < datetime.now(UTC)
        and row.status not in {"closed", "cancelled"}
    )
    return data


@router.post("", status_code=201)
async def post_hazard(
    payload: HazardCreate,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.HAZARD_CREATE))],
) -> dict:
    return _view(await create_hazard(session, payload, user))


@router.get("")
async def list_hazards(
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.HAZARD_READ))],
    status: str | None = None,
    risk_level: Literal["low", "medium", "high", "major"] | None = None,
    overdue: bool | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    statement = select(Hazard).where(
        data_scope_clause(
            user,
            Permissions.HAZARD_READ,
            Hazard.organization_unit_id,
            owner_column=Hazard.created_by,
            assignee_column=Hazard.assignee_id,
        )
    )
    if status:
        statement = statement.where(Hazard.status == status)
    if risk_level:
        statement = statement.where(Hazard.risk_level == risk_level)
    if overdue is True:
        statement = statement.where(
            Hazard.due_at < datetime.now(UTC), Hazard.status.notin_(("closed", "cancelled"))
        )
    elif overdue is False:
        statement = statement.where(
            (Hazard.due_at.is_(None))
            | (Hazard.due_at >= datetime.now(UTC))
            | Hazard.status.in_(("closed", "cancelled"))
        )
    rows = list(
        await session.scalars(
            statement.order_by(Hazard.created_at.desc()).offset(offset).limit(limit)
        )
    )
    return [_view(row) for row in rows]


@router.get("/{hazard_id}")
async def read_hazard(
    hazard_id: UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.HAZARD_READ))],
) -> dict:
    hazard = await get_hazard(session, hazard_id)
    require_data_scope(
        user,
        hazard.organization_unit_id,
        Permissions.HAZARD_READ,
        owner_id=hazard.created_by,
        assignee_id=hazard.assignee_id,
    )
    assessments = list(
        await session.scalars(
            select(HazardAssessment)
            .where(HazardAssessment.hazard_id == hazard.id)
            .order_by(HazardAssessment.version)
        )
    )
    rectifications = list(
        await session.scalars(
            select(HazardRectification)
            .where(HazardRectification.hazard_id == hazard.id)
            .order_by(HazardRectification.created_at)
        )
    )
    acceptances = list(
        await session.scalars(
            select(HazardAcceptance)
            .where(HazardAcceptance.hazard_id == hazard.id)
            .order_by(HazardAcceptance.created_at)
        )
    )
    transitions = list(
        await session.scalars(
            select(HazardTransition)
            .where(HazardTransition.hazard_id == hazard.id)
            .order_by(HazardTransition.occurred_at)
        )
    )
    links = list(
        await session.scalars(
            select(HazardWorkOrderLink).where(HazardWorkOrderLink.hazard_id == hazard.id)
        )
    )
    return {
        "hazard": _view(hazard),
        "assessments": [
            {
                "id": str(item.id),
                "version": item.version,
                "likelihood": item.likelihood,
                "consequence": item.consequence,
                "risk_score": item.risk_score,
                "risk_level": item.risk_level,
                "rationale": item.rationale,
                "assessed_by": str(item.assessed_by),
            }
            for item in assessments
        ],
        "rectifications": [
            {
                "id": str(item.id),
                "hazard_version": item.hazard_version,
                "measurements": item.measurements,
                "attachment_ids": item.attachment_ids,
                "action": item.action,
                "result": item.result,
                "submitted_by": str(item.submitted_by),
                "submitted_at": item.submitted_at.isoformat(),
            }
            for item in rectifications
        ],
        "acceptances": [
            {
                "id": str(item.id),
                "rectification_id": item.rectification_id,
                "measurements": item.measurements,
                "decision": item.decision,
                "reason": item.reason,
                "accepted_by": str(item.accepted_by),
                "accepted_at": item.accepted_at.isoformat(),
            }
            for item in acceptances
        ],
        "transitions": [
            {
                "from_status": item.from_status,
                "to_status": item.to_status,
                "reason": item.reason,
                "actor_id": str(item.actor_id),
                "occurred_at": item.occurred_at.isoformat(),
                "version_after": item.version_after,
            }
            for item in transitions
        ],
        "work_order_ids": [str(item.work_order_id) for item in links],
    }


@router.post("/{hazard_id}:assess")
async def post_assessment(
    hazard_id: UUID,
    payload: HazardAssessmentInput,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.HAZARD_ASSESS))],
) -> dict:
    return _view(await assess_hazard(session, hazard_id, payload, user))


@router.post("/{hazard_id}:start-rectification")
async def post_start_rectification(
    hazard_id: UUID,
    payload: HazardStartInput,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.HAZARD_RECTIFY))],
) -> dict:
    return _view(await start_rectification(session, hazard_id, payload, user))


@router.post("/{hazard_id}:submit-rectification")
async def post_submit_rectification(
    hazard_id: UUID,
    payload: HazardRectificationInput,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.HAZARD_RECTIFY))],
) -> dict:
    return _view(await submit_rectification(session, hazard_id, payload, user))


@router.post("/{hazard_id}:accept")
async def post_accept(
    hazard_id: UUID,
    payload: HazardAcceptanceInput,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.HAZARD_ACCEPT))],
) -> dict:
    return _view(await accept_hazard(session, hazard_id, payload, user, approved=True))


@router.post("/{hazard_id}:reject")
async def post_reject(
    hazard_id: UUID,
    payload: HazardAcceptanceInput,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.HAZARD_ACCEPT))],
) -> dict:
    return _view(await accept_hazard(session, hazard_id, payload, user, approved=False))


@router.post("/{hazard_id}/work-orders", status_code=201)
async def post_work_order_link(
    hazard_id: UUID,
    payload: HazardWorkOrderLinkInput,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.HAZARD_LINK))],
) -> dict:
    link = await link_work_order(
        session, await get_hazard(session, hazard_id), payload.work_order_id, user
    )
    return {"id": str(link.id), "work_order_id": str(link.work_order_id)}
