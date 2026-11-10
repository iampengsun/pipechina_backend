from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import AwareDatetime
from sqlalchemy import select

from app.modules.operational_graph.application import assets, impacts
from app.modules.operational_graph.application.catalog import build_catalog
from app.modules.operational_graph.application.projection import synchronize
from app.modules.operational_graph.application.queries import graph_client, projection_status, trace
from app.modules.operational_graph.application.recurrence import recurrence
from app.modules.operational_graph.domain.models import (
    Equipment,
    EquipmentAlias,
    EquipmentAssociation,
    FailureMode,
    ImpactReview,
    Station,
)
from app.modules.operational_graph.domain.schemas import (
    AliasCreate,
    AliasWithdrawal,
    AssociationCreate,
    AssociationUpdate,
    AssociationView,
    EquipmentCreate,
    EquipmentUpdate,
    EquipmentView,
    FailureModeCreate,
    ImpactDecision,
    ImpactView,
    RecurrenceView,
    StationCreate,
    StationLocationInput,
    StationLocationView,
    StationView,
    TraceView,
)
from app.shared.db import SessionDep
from app.shared.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.shared.platform.service import add_audit
from app.shared.security.authorization.dependencies import data_scope_clause, require_permission
from app.shared.security.authorization.permissions import Permissions as P
from app.shared.security.authorization.schemas import CurrentUser

router = APIRouter(tags=["operational-graph"])
AssetReader = Annotated[CurrentUser, Depends(require_permission(P.EQUIPMENT_READ))]
AssetEditor = Annotated[CurrentUser, Depends(require_permission(P.EQUIPMENT_MANAGE))]
GraphReader = Annotated[CurrentUser, Depends(require_permission(P.GRAPH_READ))]
GraphManager = Annotated[CurrentUser, Depends(require_permission(P.GRAPH_MANAGE))]
ImpactReviewer = Annotated[CurrentUser, Depends(require_permission(P.GRAPH_REVIEW))]


@router.post("/stations", status_code=201, response_model=StationView)
async def create_station(payload: StationCreate, session: SessionDep, user: AssetEditor):
    return await assets.create_station(session, user, payload)


@router.get("/stations", response_model=list[StationView])
async def stations(
    session: SessionDep,
    user: AssetReader,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    return list(
        await session.scalars(
            select(Station)
            .where(
                data_scope_clause(
                    user,
                    P.EQUIPMENT_READ,
                    Station.organization_unit_id,
                    owner_column=Station.created_by,
                )
            )
            .order_by(Station.code, Station.id)
            .limit(limit)
            .offset(offset)
        )
    )


@router.post("/failure-modes", status_code=201, response_model=StationView)
async def create_failure_mode(payload: FailureModeCreate, session: SessionDep, user: AssetEditor):
    return await assets.create_station(session, user, payload, failure_mode=True)


@router.get("/failure-modes", response_model=list[StationView])
async def failure_modes(
    session: SessionDep,
    user: AssetReader,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    return list(
        await session.scalars(
            select(FailureMode)
            .where(
                data_scope_clause(
                    user,
                    P.EQUIPMENT_READ,
                    FailureMode.organization_unit_id,
                    owner_column=FailureMode.created_by,
                )
            )
            .order_by(FailureMode.code, FailureMode.id)
            .limit(limit)
            .offset(offset)
        )
    )


@router.post("/equipment", status_code=201, response_model=EquipmentView)
async def create_equipment(payload: EquipmentCreate, session: SessionDep, user: AssetEditor):
    return await assets.create_equipment(session, user, payload)


@router.get("/equipment", response_model=list[EquipmentView])
async def equipment_list(
    session: SessionDep,
    user: AssetReader,
    station_id: UUID | None = None,
    active: bool | None = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    statement = select(Equipment).where(
        data_scope_clause(
            user,
            P.EQUIPMENT_READ,
            Equipment.organization_unit_id,
            owner_column=Equipment.created_by,
        )
    )
    if station_id:
        statement = statement.where(Equipment.station_id == station_id)
    if active is not None:
        statement = statement.where(Equipment.active == active)
    return list(
        await session.scalars(
            statement.order_by(Equipment.station_id, Equipment.code, Equipment.id)
            .limit(limit)
            .offset(offset)
        )
    )


@router.get("/equipment/resolve", response_model=EquipmentView)
async def resolve_equipment(station_id: UUID, alias: str, session: SessionDep, user: AssetReader):
    station = await session.get(Station, station_id)
    if station is None:
        raise NotFoundError("station", station_id)
    assets.asset_access(station, user)
    matched = await session.scalar(
        select(EquipmentAlias).where(
            EquipmentAlias.station_id == station_id,
            EquipmentAlias.normalized == assets.normalize_identifier(alias),
        )
    )
    if matched is None:
        raise NotFoundError("equipment_alias", alias)
    return await assets.equipment_access(session, user, matched.equipment_id)


@router.get("/equipment/{equipment_id}", response_model=EquipmentView)
async def equipment(equipment_id: UUID, session: SessionDep, user: AssetReader):
    return await assets.equipment_access(session, user, equipment_id)


@router.patch("/equipment/{equipment_id}", response_model=EquipmentView)
async def update_equipment(
    equipment_id: UUID, payload: EquipmentUpdate, session: SessionDep, user: AssetEditor
):
    row = await assets.equipment_access(
        session, user, equipment_id, lock=True, permission=P.EQUIPMENT_MANAGE
    )
    if row.version != payload.expected_version:
        raise ConflictError("equipment version changed")
    row.active = payload.active
    row.version += 1
    await add_audit(
        session,
        user=user,
        action="equipment.update",
        resource_type="equipment",
        resource_id=row.id,
        reason=payload.reason,
    )
    await session.commit()
    await session.refresh(row)
    return row


@router.post("/equipment/{equipment_id}/aliases", status_code=201)
async def add_alias(
    equipment_id: UUID, payload: AliasCreate, session: SessionDep, user: AssetEditor
):
    return await assets.add_alias(session, user, equipment_id, payload)


@router.get("/equipment/{equipment_id}/aliases")
async def aliases(equipment_id: UUID, session: SessionDep, user: AssetReader):
    await assets.equipment_access(session, user, equipment_id)
    rows = await session.scalars(
        select(EquipmentAlias)
        .where(EquipmentAlias.equipment_id == equipment_id)
        .order_by(EquipmentAlias.normalized)
    )
    return [
        {
            "id": r.id,
            "alias": r.alias,
            "normalized": r.normalized,
            "reason": r.reason,
            "confirmed_by": r.confirmed_by,
        }
        for r in rows
    ]


@router.post("/equipment-associations", status_code=201, response_model=AssociationView)
async def create_association(payload: AssociationCreate, session: SessionDep, user: AssetEditor):
    return await assets.associate(session, user, payload)


@router.patch("/equipment-associations/{association_id}", response_model=AssociationView)
async def update_association(
    association_id: UUID, payload: AssociationUpdate, session: SessionDep, user: AssetEditor
):
    return await assets.update_association(session, user, association_id, payload)


@router.get("/equipment/{equipment_id}/associations", response_model=list[AssociationView])
async def associations(equipment_id: UUID, session: SessionDep, user: AssetReader):
    await assets.equipment_access(session, user, equipment_id)
    catalog = await build_catalog(session)
    rows = await session.scalars(
        select(EquipmentAssociation)
        .where(EquipmentAssociation.equipment_id == equipment_id)
        .order_by(EquipmentAssociation.created_at)
    )
    return [r for r in rows if catalog.can_read(f"{r.record_kind}:{r.record_id}", user)]


@router.get("/operational-graph/status")
async def status(session: SessionDep, user: GraphManager):
    if not user.has_global_permission(P.GRAPH_MANAGE):
        raise PermissionDeniedError(P.GRAPH_MANAGE)
    return await projection_status(session)


@router.post("/operational-graph:sync")
async def sync_graph(request: Request, user: GraphManager, force: bool = False):
    if not user.has_global_permission(P.GRAPH_MANAGE):
        raise PermissionDeniedError(P.GRAPH_MANAGE)
    return await synchronize(
        request.app.state.database.session_factory,
        graph_client(request.app.state.knowledge),
        force=force,
    )


@router.get("/operational-graph/trace", response_model=TraceView)
async def graph_trace(
    request: Request,
    session: SessionDep,
    user: GraphReader,
    kind: str,
    record_id: UUID,
    depth: int = Query(4, ge=1, le=6),
    limit: int = Query(100, ge=1, le=1000),
):
    return await trace(
        session,
        user,
        graph_client(request.app.state.knowledge),
        kind,
        record_id,
        depth=depth,
        limit=limit,
    )


@router.get("/knowledge/versions/{version_id}/impact", response_model=ImpactView)
async def version_impact(
    version_id: UUID, request: Request, session: SessionDep, user: GraphReader
):
    return await impacts.impact(
        session, user, graph_client(request.app.state.knowledge), version_id
    )


@router.get("/equipment/{equipment_id}/recurrences", response_model=RecurrenceView)
async def equipment_recurrences(
    equipment_id: UUID,
    failure_mode_id: UUID,
    request: Request,
    session: SessionDep,
    user: GraphReader,
    since: AwareDatetime | None = None,
    until: AwareDatetime | None = None,
):
    return await recurrence(
        session,
        user,
        graph_client(request.app.state.knowledge),
        equipment_id,
        failure_mode_id,
        since=since,
        until=until,
    )


@router.get("/impact-reviews")
async def impact_reviews(
    session: SessionDep,
    user: ImpactReviewer,
    status: Literal["pending", "needs_action", "closed", "superseded"] | None = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    catalog = await build_catalog(session)
    statement = select(ImpactReview).order_by(ImpactReview.created_at, ImpactReview.id)
    if status:
        statement = statement.where(ImpactReview.status == status)
    # Filter authorization before pagination so private cases never affect visible page sizes.
    result = []
    for row in await session.scalars(statement):
        try:
            impacts.review_access(catalog, user, row)
        except PermissionDeniedError:
            continue
        result.append(impacts.view_review(catalog, row))
    return result[offset : offset + limit]


@router.get("/impact-reviews/{review_id}")
async def read_impact(review_id: UUID, session: SessionDep, user: ImpactReviewer):
    return await impacts.read_review(session, user, review_id)


@router.post("/impact-reviews/{review_id}:review")
async def review_impact(
    review_id: UUID, payload: ImpactDecision, session: SessionDep, user: ImpactReviewer
):
    return await impacts.decide_review(session, user, review_id, payload)


@router.post("/equipment/{equipment_id}/aliases/{alias_id}:withdraw")
async def withdraw_alias(
    equipment_id: UUID,
    alias_id: UUID,
    payload: AliasWithdrawal,
    session: SessionDep,
    user: AssetEditor,
):
    return await assets.withdraw_alias(session, user, equipment_id, alias_id, payload)


@router.get("/stations/{station_id}/location", response_model=StationLocationView | None)
async def station_location(station_id: UUID, session: SessionDep, user: AssetReader):
    from app.modules.operational_graph.application.locations import current_location, station_access

    await station_access(session, user, station_id)
    return await current_location(session, station_id)


@router.put("/stations/{station_id}/location", response_model=StationLocationView)
async def confirm_station_location(
    station_id: UUID,
    payload: StationLocationInput,
    session: SessionDep,
    user: AssetEditor,
):
    from app.modules.operational_graph.application.locations import save_location

    return await save_location(session, user, station_id, payload)
