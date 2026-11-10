"""Read-only weather and spatial context; coordinates are WGS84 latitude/longitude."""

from typing import Protocol
from uuid import UUID

Point = tuple[float, float]


class ExternalContextError(Exception):
    """Sanitized outbound failure code."""


class SiteContextProvider(Protocol):
    async def weather_alerts(self, point: Point) -> dict: ...
    async def map_context(self, point: Point) -> dict: ...
    async def access_route(self, origin: Point, destination: Point) -> dict: ...
    async def enterprise_gis(self, organization_id: UUID, point: Point) -> dict: ...
    async def close(self) -> None: ...
