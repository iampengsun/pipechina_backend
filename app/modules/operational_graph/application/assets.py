import unicodedata

from sqlalchemy import select

from app.modules.operational_graph.application.catalog import binding_revision, build_catalog, key
from app.modules.operational_graph.domain.models import (
    Equipment,
    EquipmentAlias,
    EquipmentAssociation,
    FailureMode,
    Station,
)
from app.shared.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.shared.platform.service import add_audit
from app.shared.security.authorization.dependencies import require_data_scope
from app.shared.security.authorization.permissions import Permissions as P


def normalize_identifier(value):
    normalized = " ".join(unicodedata.normalize("NFKC", value).split()).casefold()
    if not normalized:
        raise ConflictError("asset identifiers cannot be blank")
    return normalized


def asset_access(row, user, permission=P.EQUIPMENT_READ):
    require_data_scope(user, row.organization_unit_id, permission, owner_id=row.created_by)


async def equipment_access(session, user, equipment_id, *, lock=False, permission=P.EQUIPMENT_READ):
    row = await session.get(Equipment, equipment_id, with_for_update=lock, populate_existing=True)
    if row is None:
        raise NotFoundError("equipment", equipment_id)
    asset_access(row, user, permission)
    return row


async def create_station(session, user, payload, *, failure_mode=False):
    org = payload.organization_unit_id or user.organization_unit_id
    require_data_scope(user, org, P.EQUIPMENT_MANAGE, owner_id=user.user_id)
    from app.shared.security.identity.models import OrganizationUnit

    organization = await session.get(OrganizationUnit, org, with_for_update=True)
    if organization is None or not organization.active:
        raise NotFoundError("organization", org)
    model = FailureMode if failure_mode else Station
    code = normalize_identifier(payload.code)
    if await session.scalar(
        select(model.id).where(model.organization_unit_id == org, model.code == code)
    ):
        raise ConflictError("station or failure mode code already exists in this organization")
    row = model(organization_unit_id=org, code=code, name=payload.name, created_by=user.user_id)
    session.add(row)
    await session.flush()
    await add_audit(
        session,
        user=user,
        action="equipment.failure_mode.create" if failure_mode else "equipment.station.create",
        resource_type=model.__tablename__,
        resource_id=row.id,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def create_equipment(session, user, payload):
    station = await session.get(Station, payload.station_id, with_for_update=True)
    if station is None:
        raise NotFoundError("station", payload.station_id)
    asset_access(station, user, P.EQUIPMENT_MANAGE)
    code = normalize_identifier(payload.code)
    if await session.scalar(
        select(EquipmentAlias.id).where(
            EquipmentAlias.station_id == station.id, EquipmentAlias.normalized == code
        )
    ):
        raise ConflictError("equipment code conflicts with a confirmed identifier or alias")
    row = Equipment(
        station_id=station.id,
        organization_unit_id=station.organization_unit_id,
        code=code,
        name=payload.name,
        equipment_type=payload.equipment_type,
        created_by=user.user_id,
    )
    session.add(row)
    await session.flush()
    session.add(
        EquipmentAlias(
            station_id=station.id,
            equipment_id=row.id,
            alias=payload.code,
            normalized=code,
            reason="人工登记的设备编号",
            confirmed_by=user.user_id,
        )
    )
    await add_audit(
        session, user=user, action="equipment.create", resource_type="equipment", resource_id=row.id
    )
    await session.commit()
    await session.refresh(row)
    return row


async def add_alias(session, user, equipment_id, payload):
    row = await equipment_access(
        session, user, equipment_id, lock=True, permission=P.EQUIPMENT_MANAGE
    )
    # The station lock serializes code/alias reservations across different equipment.
    await session.get(Station, row.station_id, with_for_update=True)
    if row.version != payload.expected_version:
        raise ConflictError("equipment version changed")
    normalized = normalize_identifier(payload.alias)
    existing = await session.scalar(
        select(EquipmentAlias).where(
            EquipmentAlias.station_id == row.station_id, EquipmentAlias.normalized == normalized
        )
    )
    if existing:
        if existing.equipment_id != row.id:
            raise ConflictError("alias already identifies another equipment in this station")
        raise ConflictError("alias is already confirmed")
    alias = EquipmentAlias(
        station_id=row.station_id,
        equipment_id=row.id,
        alias=payload.alias,
        normalized=normalized,
        reason=payload.reason,
        confirmed_by=user.user_id,
    )
    session.add(alias)
    row.version += 1
    await session.flush()
    await add_audit(
        session,
        user=user,
        action="equipment.alias.confirm",
        resource_type="equipment_alias",
        resource_id=alias.id,
        reason=payload.reason,
    )
    await session.commit()
    return {
        "id": alias.id,
        "alias": alias.alias,
        "normalized": alias.normalized,
        "equipment_id": row.id,
        "version": row.version,
    }


async def associate(session, user, payload):
    equipment = await equipment_access(
        session, user, payload.equipment_id, lock=True, permission=P.EQUIPMENT_MANAGE
    )
    if not equipment.active:
        raise ConflictError("inactive equipment cannot receive associations")
    failure = await session.get(FailureMode, payload.failure_mode_id)
    if failure is None:
        raise NotFoundError("failure_mode", payload.failure_mode_id)
    asset_access(failure, user)
    if failure.organization_unit_id != equipment.organization_unit_id:
        raise ConflictError("failure mode belongs to another organization")
    catalog = await build_catalog(session)
    identifier = key(payload.record_kind, payload.record_id)
    if identifier not in catalog.records:
        raise NotFoundError("business_record", payload.record_id)
    if not catalog.can_read(identifier, user):
        raise PermissionDeniedError(P.GRAPH_READ)
    record = catalog.records[identifier]
    await session.refresh(record.row, with_for_update=True)
    if record.organization != equipment.organization_unit_id:
        raise ConflictError("equipment association crosses organization boundaries")
    if binding_revision(record.row) != payload.source_revision:
        raise ConflictError("business source changed; confirm the current version")
    if payload.record_kind == "event" and record.state != "confirmed":
        raise ConflictError("only confirmed events can be associated")
    if record.state == "cancelled":
        raise ConflictError("cancelled records cannot be associated")
    if await session.scalar(
        select(EquipmentAssociation.id).where(
            EquipmentAssociation.equipment_id == equipment.id,
            EquipmentAssociation.record_kind == payload.record_kind,
            EquipmentAssociation.record_id == payload.record_id,
            EquipmentAssociation.failure_mode_id == failure.id,
        )
    ):
        raise ConflictError("association already exists; revise its confirmation instead")
    row = EquipmentAssociation(
        **payload.model_dump(exclude={"reason"}), reason=payload.reason, confirmed_by=user.user_id
    )
    session.add(row)
    await session.flush()
    await add_audit(
        session,
        user=user,
        action="equipment.association.confirm",
        resource_type="equipment_association",
        resource_id=row.id,
        reason=payload.reason,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def update_association(session, user, association_id, payload):
    row = await session.get(
        EquipmentAssociation, association_id, with_for_update=True, populate_existing=True
    )
    if row is None:
        raise NotFoundError("equipment_association", association_id)
    equipment = await equipment_access(
        session, user, row.equipment_id, lock=True, permission=P.EQUIPMENT_MANAGE
    )
    if payload.active and not equipment.active:
        raise ConflictError("inactive equipment cannot receive associations")
    catalog = await build_catalog(session)
    identifier = key(row.record_kind, row.record_id)
    if not catalog.can_read(identifier, user):
        raise PermissionDeniedError(P.GRAPH_READ)
    await session.refresh(catalog.records[identifier].row, with_for_update=True)
    if (
        row.version != payload.expected_version
        or binding_revision(catalog.records[identifier].row) != payload.source_revision
    ):
        raise ConflictError("association or its source changed")
    source = catalog.records[identifier].row
    if payload.active and (
        (row.record_kind == "event" and source.business_status != "confirmed")
        or getattr(source, "status", None) == "cancelled"
    ):
        raise ConflictError("only current eligible business records can be associated")
    row.active = payload.active
    row.source_revision = payload.source_revision
    row.reason = payload.reason
    row.confirmed_by = user.user_id
    row.version += 1
    await add_audit(
        session,
        user=user,
        action="equipment.association.reconfirm"
        if payload.active
        else "equipment.association.withdraw",
        resource_type="equipment_association",
        resource_id=row.id,
        reason=payload.reason,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def withdraw_alias(session, user, equipment_id, alias_id, payload):
    equipment = await equipment_access(
        session, user, equipment_id, lock=True, permission=P.EQUIPMENT_MANAGE
    )
    await session.get(Station, equipment.station_id, with_for_update=True)
    alias = await session.get(EquipmentAlias, alias_id, with_for_update=True)
    if alias is None or alias.equipment_id != equipment.id:
        raise NotFoundError("equipment_alias", alias_id)
    if equipment.version != payload.expected_version:
        raise ConflictError("equipment version changed")
    if alias.normalized == equipment.code:
        raise ConflictError("canonical equipment code cannot be withdrawn")
    await add_audit(
        session,
        user=user,
        action="equipment.alias.withdraw",
        resource_type="equipment_alias",
        resource_id=alias.id,
        reason=payload.reason,
    )
    await session.delete(alias)
    equipment.version += 1
    await session.commit()
    return {
        "equipment_id": equipment.id,
        "version": equipment.version,
        "withdrawn_alias_id": alias_id,
    }
