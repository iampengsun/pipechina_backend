from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select

from app.modules.knowledge.domain.models import KnowledgeEvaluation
from app.modules.operation_event.domain.agent_models import AgentOfflineEvaluation
from app.modules.quality.application.service import (
    assign_improvement,
    create_improvement,
    evaluation_snapshot,
    get_improvement,
    participant_context,
    transition,
)
from app.modules.quality.domain.models import EvaluationImprovement
from app.modules.quality.domain.schemas import (
    AcceptInput,
    AssignInput,
    ImplementInput,
    ImprovementCreate,
    ImprovementView,
    RetestInput,
)
from app.shared.db import SessionDep
from app.shared.errors import NotFoundError, PermissionDeniedError
from app.shared.security.authorization.dependencies import get_current_user
from app.shared.security.authorization.permissions import Permissions as P
from app.shared.security.authorization.schemas import CurrentUser
from app.shared.security.identity.models import UserAccount

router = APIRouter(prefix="/evaluation-improvements", tags=["evaluation-improvement"])
# Source-specific permissions and data scope are checked by service on every request.
User = Annotated[CurrentUser, Depends(get_current_user)]


@router.post("", status_code=201, response_model=ImprovementView)
async def create(payload: ImprovementCreate, session: SessionDep, user: User):
    return await create_improvement(session, user, payload)


@router.get("", response_model=list[ImprovementView])
async def listing(
    session: SessionDep,
    user: User,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    rows = await session.scalars(
        select(EvaluationImprovement)
        .where(
            or_(
                EvaluationImprovement.created_by == user.user_id,
                EvaluationImprovement.assignee_id == user.user_id,
                EvaluationImprovement.reviewer_id == user.user_id,
            )
        )
        .order_by(EvaluationImprovement.created_at.desc(), EvaluationImprovement.id)
        .limit(limit)
        .offset(offset)
    )
    # Do not return stored source snapshots without checking current authorization.
    return [await get_improvement(session, user, row.id) for row in rows]


@router.get("/reminders", response_model=list[ImprovementView])
async def reminders(session: SessionDep, user: User):

    rows = await session.scalars(
        select(EvaluationImprovement)
        .where(
            EvaluationImprovement.status != "closed",
            EvaluationImprovement.due_at < datetime.now(UTC),
            or_(
                EvaluationImprovement.created_by == user.user_id,
                EvaluationImprovement.assignee_id == user.user_id,
                EvaluationImprovement.reviewer_id == user.user_id,
            ),
        )
        .order_by(EvaluationImprovement.due_at)
        .limit(100)
    )
    return [await get_improvement(session, user, row.id) for row in rows]


@router.get("/participants")
async def participants(session: SessionDep, user: User):

    if not (user.has_permission(P.KNOWLEDGE_EVALUATE) or user.has_permission(P.EVENT_CLASSIFY)):
        raise PermissionDeniedError("evaluation:read")
    rows = await session.scalars(
        select(UserAccount).where(
            UserAccount.organization_unit_id == user.organization_unit_id,
            UserAccount.active.is_(True),
        )
    )

    result = []
    for row in rows:
        context = await participant_context(session, row)
        kinds = [
            kind
            for kind, permission in (("rag", P.KNOWLEDGE_EVALUATE), ("agent", P.EVENT_CLASSIFY))
            if context.has_permission(permission)
        ]
        if kinds:
            result.append({"id": str(row.id), "name": row.display_name, "kinds": kinds})
    return result


@router.get("/{item_id}", response_model=ImprovementView)
async def read(item_id: UUID, session: SessionDep, user: User):
    return await get_improvement(session, user, item_id)


@router.post("/{item_id}:implement", response_model=ImprovementView)
async def implement(item_id: UUID, payload: ImplementInput, session: SessionDep, user: User):
    item = await get_improvement(session, user, item_id, lock=True)
    return await transition(session, user, item, "implement", payload)


@router.post("/{item_id}:retest", response_model=ImprovementView)
async def retest(item_id: UUID, payload: RetestInput, session: SessionDep, user: User):
    item = await get_improvement(session, user, item_id, lock=True)
    return await transition(session, user, item, "retest", payload)


@router.post("/{item_id}:accept", response_model=ImprovementView)
async def accept(item_id: UUID, payload: AcceptInput, session: SessionDep, user: User):
    item = await get_improvement(session, user, item_id, lock=True)
    return await transition(session, user, item, "accept", payload)


@router.post("/{item_id}:assign", response_model=ImprovementView)
async def assign(item_id: UUID, payload: AssignInput, session: SessionDep, user: User):
    item = await get_improvement(session, user, item_id, lock=True)
    return await assign_improvement(session, user, item, payload)


@router.get("/data/evaluations")
async def evaluations(session: SessionDep, user: User, kind: Literal["rag", "agent"] = "rag"):

    permission = P.KNOWLEDGE_EVALUATE if kind == "rag" else P.EVENT_CLASSIFY
    if not user.has_permission(permission):
        raise PermissionDeniedError(permission)

    rows: list[KnowledgeEvaluation | AgentOfflineEvaluation]
    if kind == "rag":
        rows = list(
            await session.scalars(
                select(KnowledgeEvaluation)
                .where(KnowledgeEvaluation.requested_by == user.user_id)
                .order_by(KnowledgeEvaluation.created_at.desc())
                .limit(100)
            )
        )
    else:
        rows = list(
            await session.scalars(
                select(AgentOfflineEvaluation)
                .where(AgentOfflineEvaluation.requested_by == user.user_id)
                .order_by(AgentOfflineEvaluation.created_at.desc())
                .limit(100)
            )
        )
    for row in rows:
        if not isinstance(row, KnowledgeEvaluation) or row.status == "succeeded":
            await evaluation_snapshot(session, user, kind, row.id)
    return [
        {
            "id": str(row.id),
            "name": row.dataset.get("name", str(row.id)),
            "created_at": row.created_at,
            "status": row.status if isinstance(row, KnowledgeEvaluation) else "succeeded",
            "reviewed": "expert_review" in row.results if kind == "rag" else True,
            "results": row.results,
            "dataset": row.dataset,
            "review_revision": row.options.get("review_revision", 0)
            if isinstance(row, KnowledgeEvaluation)
            else 0,
        }
        for row in rows
    ]


@router.get("/data/review/{evaluation_id}")
async def review_source(evaluation_id: UUID, session: SessionDep, user: User):

    # Explicit reviewer access still requires the author's organization and all source ACLs.
    evaluation = await session.get(KnowledgeEvaluation, evaluation_id)

    if evaluation is None:
        raise NotFoundError("knowledge_evaluation", evaluation_id)
    await evaluation_snapshot(session, user, "rag", evaluation_id, owner_id=evaluation.requested_by)
    return {
        "id": str(evaluation.id),
        "name": evaluation.dataset["name"],
        "results": evaluation.results,
        "review_revision": evaluation.options.get("review_revision", 0),
    }
