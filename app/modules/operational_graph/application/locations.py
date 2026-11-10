"""Human confirmation of station positions; no geocoding result edits these records."""

from sqlalchemy import select

from app.modules.operational_graph.application.assets import asset_access
from app.modules.operational_graph.domain.models import Station, StationLocation
from app.shared.errors import ConflictError, NotFoundError
from app.shared.platform.service import add_audit
from app.shared.security.authorization.permissions import Permissions as P


async def station_access(session, user, station_id, *, edit=False):
    station = await session.get(Station, station_id, with_for_update=edit, populate_existing=True)
    if station is None:
        raise NotFoundError("station", station_id)
    asset_access(station, user, P.EQUIPMENT_MANAGE if edit else P.EQUIPMENT_READ)
    return station


async def current_location(session, station_id):
    return await session.scalar(
        select(StationLocation)
        .where(StationLocation.station_id == station_id)
        .order_by(StationLocation.version.desc())
        .limit(1)
    )


async def save_location(session, user, station_id, payload):
    await station_access(session, user, station_id, edit=True)
    previous = await current_location(session, station_id)
    version = previous.version if previous else 0
    if payload.expected_version != version:
        raise ConflictError("station location changed", current_version=version)
    row = StationLocation(
        station_id=station_id,
        version=version + 1,
        created_by=user.user_id,
        **payload.model_dump(exclude={"expected_version", "coordinate_system"}),
    )
    session.add(row)
    await session.flush()
    await add_audit(
        session,
        user=user,
        action="station.location.confirm" if row.active else "station.location.withdraw",
        resource_type="station_location",
        resource_id=row.id,
        reason=payload.reason,
        after={
            "station_id": str(station_id),
            "version": row.version,
            "source": row.source,
            "coordinate_system": "WGS84",
            "active": row.active,
        },
    )
    await session.commit()
    await session.refresh(row)
    return row
