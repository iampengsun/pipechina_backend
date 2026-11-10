from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select

from app.modules.operation_event.application import memory
from app.modules.operation_event.domain.memory_models import CaseObservation, ExperienceMemory
from app.modules.operation_event.domain.memory_schemas import (
    CaseMemoryView,
    ConsolidationInput,
    ConsolidationView,
    ExperienceMerge,
    ExperienceReview,
    ExperienceRevision,
    ExperienceView,
    ExperienceWithdrawal,
    ObservationCreate,
    ObservationReview,
    ObservationView,
)
from app.shared.db import SessionDep
from app.shared.errors import ConflictError
from app.shared.security.authorization.dependencies import data_scope_clause, require_permission
from app.shared.security.authorization.permissions import Permissions as P
from app.shared.security.authorization.schemas import CurrentUser

router = APIRouter(tags=["investigation-memory"])
Reader = Annotated[CurrentUser, Depends(require_permission(P.EVENT_READ))]
Editor = Annotated[CurrentUser, Depends(require_permission(P.EVENT_EDIT))]
EventReviewer = Annotated[CurrentUser, Depends(require_permission(P.EVENT_REVIEW))]
Classifier = Annotated[CurrentUser, Depends(require_permission(P.EVENT_CLASSIFY))]
MemoryReader = Annotated[CurrentUser, Depends(require_permission(P.MAINTENANCE_READ))]
MemoryReviewer = Annotated[CurrentUser, Depends(require_permission(P.MAINTENANCE_REVIEW))]


@router.get("/events/{event_id}/memory", response_model=CaseMemoryView)
async def case_memory(event_id: UUID, session: SessionDep, user: Reader):
    event = await memory.event_access(session, user, event_id)
    context = await memory.case_context(session, user, event)
    observations = await session.scalars(
        select(CaseObservation)
        .where(CaseObservation.event_id == event.id)
        .order_by(CaseObservation.created_at.desc())
        .limit(100)
    )
    return {
        **context,
        "event_id": str(event.id),
        "event_version_id": str(event.current_version_id),
        "observations": [
            ObservationView.model_validate(row, from_attributes=True) for row in observations
        ],
    }


@router.post("/events/{event_id}/observations", status_code=201, response_model=ObservationView)
async def add_observation(
    event_id: UUID, payload: ObservationCreate, session: SessionDep, user: Editor
):
    return await memory.add_observation(session, user, event_id, payload)


@router.post("/events/{event_id}/observations/{note_id}:review", response_model=ObservationView)
async def review_observation(
    event_id: UUID,
    note_id: UUID,
    payload: ObservationReview,
    session: SessionDep,
    user: EventReviewer,
):
    return await memory.review_observation(session, user, event_id, note_id, payload)


@router.post(
    "/work-orders/{order_id}:consolidate-memory", status_code=202, response_model=ConsolidationView
)
async def consolidate(
    order_id: UUID,
    payload: ConsolidationInput,
    request: Request,
    session: SessionDep,
    user: Classifier,
):
    return await memory.start_consolidation(
        session,
        user,
        [order_id],
        request.app.state.providers.text,
        expected_versions={str(order_id): payload.expected_version},
        expires_at=payload.expires_at,
        inline=request.app.state.settings.run_tasks_inline,
    )


@router.get("/experiences", response_model=list[ExperienceView])
async def experiences(
    session: SessionDep,
    user: MemoryReader,
    status: Literal["pending", "approved", "rejected", "withdrawn", "superseded"] | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    statement = select(ExperienceMemory).where(
        data_scope_clause(
            user,
            P.MAINTENANCE_READ,
            ExperienceMemory.organization_unit_id,
            owner_column=ExperienceMemory.created_by,
        )
    )
    if status:
        statement = statement.where(ExperienceMemory.status == status)
    rows = await session.scalars(
        statement.order_by(ExperienceMemory.created_at.desc()).limit(limit).offset(offset)
    )
    return [await memory.memory_access(session, user, row.id) for row in rows]


@router.get("/experiences/{memory_id}", response_model=ExperienceView)
async def experience(memory_id: UUID, session: SessionDep, user: MemoryReader):
    return await memory.memory_access(session, user, memory_id)


@router.post("/experiences/{memory_id}:review", response_model=ExperienceView)
async def review_memory(
    memory_id: UUID, payload: ExperienceReview, session: SessionDep, user: MemoryReviewer
):
    return await memory.review_memory(session, user, memory_id, payload)


@router.post("/experiences/{memory_id}:revise", status_code=201, response_model=ExperienceView)
async def revise_memory(
    memory_id: UUID, payload: ExperienceRevision, session: SessionDep, user: Classifier
):
    return await memory.revise_memory(session, user, memory_id, payload)


@router.post("/experiences/{memory_id}:withdraw", response_model=ExperienceView)
async def withdraw_memory(
    memory_id: UUID, payload: ExperienceWithdrawal, session: SessionDep, user: MemoryReviewer
):
    return await memory.withdraw_memory(session, user, memory_id, payload)


@router.post("/experiences:merge", status_code=202, response_model=ConsolidationView)
async def merge_memory(
    payload: ExperienceMerge, request: Request, session: SessionDep, user: Classifier
):
    orders: list[UUID] = []
    supersedes = []
    dependencies = []
    for memory_id, version in sorted(payload.expected_versions.items()):
        row = await memory.memory_access(session, user, memory_id)
        if row.version != version or row.status not in {"pending", "approved"}:
            raise ConflictError("memory merge source changed or is unavailable")
        orders.extend(UUID(source["id"]) for source in row.source_orders)
        if row.status == "approved":
            supersedes.append(row.id)
        dependencies.extend([str(row.id), *row.dependency_ids])
    # Pending duplicate drafts can supply closed source records but never become accepted facts.
    return await memory.start_consolidation(
        session,
        user,
        list(dict.fromkeys(orders)),
        request.app.state.providers.text,
        expected_versions={},
        expires_at=payload.expires_at,
        supersedes=supersedes,
        merge_reason=payload.reason,
        dependencies=dependencies,
        expected_memory_versions={str(k): v for k, v in payload.expected_versions.items()},
        inline=request.app.state.settings.run_tasks_inline,
    )
