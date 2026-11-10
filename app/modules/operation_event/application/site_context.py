"""External context follows human equipment bindings and confirmed station positions."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select

from app.modules.operation_event.application.memory import event_access
from app.modules.operational_graph.application.catalog import binding_revision
from app.modules.operational_graph.application.locations import current_location, station_access
from app.modules.operational_graph.domain.models import Equipment, EquipmentAssociation
from app.ports.site_context import ExternalContextError
from app.shared.errors import ConflictError, PermissionDeniedError
from app.shared.platform.service import add_audit
from app.shared.security.authorization.dependencies import data_scope_clause
from app.shared.security.authorization.permissions import Permissions as P

OPERATIONS = {"get_weather_alerts", "get_map_context", "query_enterprise_gis", "get_access_route"}


async def event_stations(session, user, event):
    rows = await session.execute(
        select(EquipmentAssociation, Equipment)
        .join(Equipment, Equipment.id == EquipmentAssociation.equipment_id)
        .where(
            EquipmentAssociation.record_kind == "event",
            EquipmentAssociation.record_id == event.id,
            EquipmentAssociation.active.is_(True),
            Equipment.active.is_(True),
            Equipment.organization_unit_id == event.organization_unit_id,
            EquipmentAssociation.source_revision == binding_revision(event),
            data_scope_clause(
                user,
                P.EQUIPMENT_READ,
                Equipment.organization_unit_id,
                owner_column=Equipment.created_by,
            ),
        )
        .limit(51)
        .execution_options(populate_existing=True)
    )
    rows = rows.all()
    if len(rows) > 50:
        raise ConflictError("event has too many equipment bindings for a spatial query")
    result: dict = {}
    for association, equipment in rows:
        result.setdefault(equipment.station_id, []).append(
            {
                "id": str(association.id),
                "version": association.version,
                "equipment_id": str(equipment.id),
                "equipment_version": equipment.version,
                "source_revision": association.source_revision,
            }
        )
    return result


async def location_ref(session, user, station_id, bindings):
    station = await station_access(session, user, station_id)
    location = await current_location(session, station_id)
    if location is None or not location.active:
        return station, None, None
    return (
        station,
        location,
        {
            "station_id": str(station.id),
            "location_id": str(location.id),
            "location_version": location.version,
            "coordinate_system": "WGS84",
            "latitude": location.latitude,
            "longitude": location.longitude,
            "bindings": bindings,
            "location_source": location.source,
        },
    )


async def investigate_site(
    session, user, event_id, operation, provider, *, station_id=None, origin_station_id=None
):
    if operation not in OPERATIONS:
        raise ConflictError("unknown external context operation")
    event = await event_access(session, user, event_id)
    if not user.has_permission(P.EQUIPMENT_READ):
        raise PermissionDeniedError(P.EQUIPMENT_READ)
    if event.business_status != "confirmed":
        raise ConflictError("external investigation requires a confirmed event")
    stations = await event_stations(session, user, event)
    now = datetime.now(UTC)
    base = {
        "operation": operation,
        "event_id": str(event.id),
        "event_version_id": str(event.current_version_id),
        "record_id": str(event.id),
        "location_refs": [],
        "retrieved_at": now.isoformat(),
        "valid_until": (now + timedelta(minutes=5)).isoformat(),
    }
    if not stations:
        return [
            {
                **base,
                "status": "needs_location",
                "unknowns": ["请人工确认事件的设备及站场关联，再登记站场WGS84位置。"],
            }
        ]
    if station_id is None and len(stations) == 1:
        station_id = next(iter(stations))
    if station_id is None:
        choices = []
        for identifier in stations:
            station = await station_access(session, user, identifier)
            choices.append({"station_id": str(identifier), "name": station.name})
        return [
            {
                **base,
                "status": "needs_selection",
                "stations": choices,
                "unknowns": ["事件关联多个站场，请指定本次调查站场ID。"],
            }
        ]
    if station_id not in stations:
        raise ConflictError(
            "target station is not a current confirmed equipment binding of this event"
        )
    target, position, reference = await location_ref(
        session, user, station_id, stations[station_id]
    )
    if position is None:
        return [
            {
                **base,
                "status": "needs_location",
                "station_id": str(station_id),
                "unknowns": ["站场缺少有效的人工确认WGS84位置。"],
            }
        ]
    base["location_refs"] = [reference]
    point = (position.latitude, position.longitude)
    origin = None
    if operation == "get_access_route":
        if origin_station_id is None:
            return [
                {
                    **base,
                    "status": "needs_origin",
                    "unknowns": ["请指定有权读取且位置已确认的出发站场ID。"],
                }
            ]
        start, start_position, origin_ref = await location_ref(session, user, origin_station_id, [])
        if start.organization_unit_id != event.organization_unit_id:
            raise PermissionDeniedError(P.EQUIPMENT_READ)
        if start.id == target.id:
            raise ConflictError("route origin and destination must be different stations")
        if start_position is None:
            return [
                {**base, "status": "needs_location", "unknowns": ["出发站场缺少有效WGS84位置。"]}
            ]
        base["location_refs"].append(origin_ref)
        origin = (start_position.latitude, start_position.longitude)
    if provider is None:
        value = {"status": "not_configured", "unknowns": ["外部上下文Provider尚未配置。"]}
    else:
        try:
            if operation == "get_weather_alerts":
                value = await provider.weather_alerts(point)
            elif operation == "get_map_context":
                value = await provider.map_context(point)
            elif operation == "query_enterprise_gis":
                value = await provider.enterprise_gis(target.organization_unit_id, point)
            else:
                value = await provider.access_route(origin, point)
        except ExternalContextError as exc:
            value = {
                "status": "unavailable",
                "error_code": str(exc),
                "unknowns": ["外部服务本次查询失败，不能据此认定无预警、无周边设施或路线可通行。"],
            }
    result = {**base, **value, "location_refs": base["location_refs"]}
    result["version"] = hashlib.sha256(
        json.dumps(result, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    # A position/binding may change while the HTTP request is in flight.
    await validate_saved_context(session, user, result, require_fresh=True)
    await add_audit(
        session,
        user=user,
        action="investigation.external_context.read",
        resource_type="production_event",
        resource_id=event.id,
        after={
            "operation": operation,
            "status": result["status"],
            "location_versions": [
                {"station_id": r["station_id"], "version": r["location_version"]}
                for r in base["location_refs"]
            ],
        },
    )
    await session.commit()
    return [result]


async def validate_saved_context(session, user, content, *, require_fresh=False):
    if not user.has_permission(P.EQUIPMENT_READ):
        raise PermissionDeniedError(P.EQUIPMENT_READ)
    event = await event_access(session, user, UUID(content["event_id"]))
    if (
        str(event.current_version_id) != content["event_version_id"]
        or event.business_status != "confirmed"
    ):
        raise ConflictError("external context event version changed")
    bindings = await event_stations(session, user, event)
    for reference in content.get("location_refs", []):
        station = await station_access(session, user, UUID(reference["station_id"]))
        if station.organization_unit_id != event.organization_unit_id:
            raise PermissionDeniedError(P.EQUIPMENT_READ)
        current = await current_location(session, station.id)
        if current is None or not current.active or str(current.id) != reference["location_id"]:
            raise ConflictError("external context station position changed")
        if reference["bindings"] and sorted(reference["bindings"], key=lambda r: r["id"]) != sorted(
            bindings.get(station.id, []), key=lambda r: r["id"]
        ):
            raise ConflictError("external context equipment binding changed")
    if require_fresh and content.get("status") in {"ok", "partial", "no_data"}:
        if datetime.fromisoformat(content["valid_until"]) <= datetime.now(UTC):
            raise ConflictError(
                "external context expired; refresh evidence before making a decision"
            )
