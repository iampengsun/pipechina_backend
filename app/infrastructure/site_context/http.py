"""Bounded HTTP adapters for QWeather, Amap and organization-scoped ArcGIS layers."""

import asyncio
import json
import logging
import math
from datetime import UTC, datetime, timedelta
from functools import wraps
from urllib.parse import quote
from uuid import UUID

import httpx

from app.bootstrap.config import Settings
from app.ports.site_context import ExternalContextError, Point


class SecretLogFilter(logging.Filter):
    secrets: frozenset[str] = frozenset()

    def filter(self, record):
        message = record.getMessage()
        for secret in self.secrets:
            message = message.replace(secret, "[redacted]").replace(
                quote(secret, safe=""), "[redacted]"
            )
        record.msg, record.args = message, ()
        return True


_secret_filter = SecretLogFilter()
for _logger in (
    "httpx",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
):
    logging.getLogger(_logger).addFilter(_secret_filter)


def read_operation(function):
    @wraps(function)
    async def execute(self, *args, **kwargs):
        try:
            async with asyncio.timeout(self.settings.site_context_timeout_seconds * 3):
                return await function(self, *args, **kwargs)
        except ExternalContextError:
            raise
        except (httpx.HTTPError, TimeoutError) as exc:
            raise ExternalContextError("UPSTREAM_UNAVAILABLE") from exc
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ExternalContextError("UPSTREAM_INVALID_RESPONSE") from exc

    return execute


def timestamp(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("provider timestamp must include a timezone")
    return parsed


def text(value, limit=2000):
    return value[:limit] if isinstance(value, str) else ""


class HTTPSiteContext:
    def __init__(self, settings: Settings, *, transport=None):
        self.settings = settings
        for secret in (
            settings.qweather_api_key,
            settings.qweather_jwt,
            settings.amap_api_key,
            settings.enterprise_gis_token,
        ):
            if secret:
                SecretLogFilter.secrets |= frozenset({secret.get_secret_value()})
        self.client = httpx.AsyncClient(
            timeout=settings.site_context_timeout_seconds,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    async def close(self):
        await self.client.aclose()

    async def get(self, url, *, params=None, headers=None):
        async with self.client.stream("GET", url, params=params, headers=headers) as response:
            if response.status_code != 200:
                raise ExternalContextError("UPSTREAM_HTTP_ERROR")
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > 1024 * 1024:
                    raise ExternalContextError("UPSTREAM_RESPONSE_TOO_LARGE")
        try:
            value = json.loads(
                chunks,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite number")),
            )
        except ValueError as exc:
            raise ExternalContextError("UPSTREAM_INVALID_JSON") from exc
        if not isinstance(value, dict):
            raise ExternalContextError("UPSTREAM_INVALID_RESPONSE")
        return value

    def envelope(self, provider, *, status="ok", **data):
        now = datetime.now(UTC)
        return {
            "provider": provider,
            "source": {
                "qweather": self.settings.qweather_api_host,
                "amap": "https://restapi.amap.com",
                "arcgis": "organization-scoped ArcGIS layer",
            }.get(provider),
            "status": status,
            "retrieved_at": now.isoformat(),
            "valid_until": (
                now + timedelta(seconds=self.settings.site_context_ttl_seconds)
            ).isoformat(),
            **data,
        }

    @read_operation
    async def weather_alerts(self, point: Point):
        settings = self.settings
        if not settings.qweather_api_host or not (
            settings.qweather_api_key or settings.qweather_jwt
        ):
            return self.envelope(
                "qweather",
                status="not_configured",
                missing=["QWEATHER_API_HOST", "QWEATHER_API_KEY or QWEATHER_JWT"],
            )
        credential = settings.qweather_api_key or settings.qweather_jwt
        assert credential is not None
        headers = (
            {"X-QW-Api-Key": credential.get_secret_value()}
            if settings.qweather_api_key
            else {"Authorization": "Bearer " + credential.get_secret_value()}
        )
        lat, lon = point
        data = await self.get(
            settings.qweather_api_host.rstrip("/")
            + f"/weatheralert/v1/current/{lat:.6f}/{lon:.6f}",
            headers=headers,
            params={"lang": "zh", "localTime": "false"},
        )
        if not isinstance(data.get("alerts"), list) or not isinstance(data.get("metadata"), dict):
            raise ExternalContextError("UPSTREAM_INVALID_RESPONSE")
        alerts, ignored = [], 0
        now = datetime.now(UTC)
        # Remove explicitly superseded/cancelled/expired messages; retain provider times verbatim.
        for alert in data["alerts"]:
            if not isinstance(alert, dict):
                raise ExternalContextError("UPSTREAM_INVALID_RESPONSE")
            expiration = timestamp(alert.get("expireTime"))
            if (
                (expiration is not None and expiration <= now)
                or alert.get("latestChange")
                or alert.get("messageType", {}).get("code") == "cancel"
            ):
                ignored += 1
                continue
            alerts.append(
                {
                    k: alert.get(k)
                    for k in (
                        "id",
                        "senderName",
                        "issuedTime",
                        "effectiveTime",
                        "onsetTime",
                        "expireTime",
                        "eventType",
                        "severity",
                        "urgency",
                        "certainty",
                        "color",
                        "messageType",
                    )
                }
            )
            alerts[-1].update(
                headline=text(alert.get("headline"), 500),
                description=text(alert.get("description"), 6000),
                instruction=text(alert.get("instruction"), 3000),
            )
        limited = (
            len(alerts) > 20
            or ignored > 0
            or any(
                len(str(a.get("description", ""))) > 6000
                or len(str(a.get("instruction", ""))) > 3000
                for a in data["alerts"]
            )
        )
        result = self.envelope(
            "qweather",
            status="partial" if limited else "ok" if alerts else "no_data",
            alerts=alerts[:20],
            limited=limited,
            ignored_messages=ignored,
            source_tag=text(data["metadata"].get("tag"), 200),
            attributions=data["metadata"].get("attributions", []),
            interpretation="预警为外部辅助证据；未返回数据不表示没有风险，须核对发布时间、有效期与实际影响范围。",
        )
        expirations = [timestamp(a["expireTime"]) for a in alerts[:20] if a.get("expireTime")]
        if expirations:
            result["valid_until"] = min(timestamp(result["valid_until"]), *expirations).isoformat()
        return result

    async def amap(self, path, params):
        key = self.settings.amap_api_key
        if key is None:
            raise ExternalContextError("AMAP_NOT_CONFIGURED")
        data = await self.get(
            "https://restapi.amap.com" + path,
            params={
                **params,
                "key": key.get_secret_value(),
                "output": "JSON",
            },
        )
        if data.get("status") != "1":
            raise ExternalContextError("AMAP_REQUEST_REJECTED")
        return data

    async def converted(self, points):
        value = await self.amap(
            "/v3/assistant/coordinate/convert",
            {
                "coordsys": "gps",
                "locations": "|".join(f"{lon:.6f},{lat:.6f}" for lat, lon in points),
            },
        )
        locations = value.get("locations", "").split(";")
        if len(locations) != len(points):
            raise ExternalContextError("UPSTREAM_INVALID_COORDINATES")
        for location in locations:
            lon, lat = [float(n) for n in location.split(",")]
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                raise ExternalContextError("UPSTREAM_INVALID_COORDINATES")
        return locations

    @read_operation
    async def map_context(self, point: Point):
        if not self.settings.amap_api_key:
            return self.envelope("amap", status="not_configured", missing=["AMAP_API_KEY"])
        (location,) = await self.converted([point])
        radius = min(self.settings.site_context_radius_meters, 3000)
        value = await self.amap(
            "/v3/geocode/regeo", {"location": location, "radius": radius, "extensions": "all"}
        )
        data = value["regeocode"]
        if not isinstance(data, dict):
            raise ExternalContextError("UPSTREAM_INVALID_RESPONSE")
        pois, roads = data.get("pois", []), data.get("roads", [])
        return self.envelope(
            "amap",
            coordinate_system="GCJ02",
            converted_location=location,
            radius_meters=radius,
            address=text(data.get("formatted_address"), 1000),
            pois=[
                {k: p.get(k) for k in ("id", "name", "type", "location", "distance")}
                for p in pois[:10]
            ],
            roads=[
                {k: p.get(k) for k in ("id", "name", "location", "distance")} for p in roads[:10]
            ],
            limited=len(pois) > 10 or len(roads) > 10,
            attribution="高德地图",
            interpretation="地图为位置及周边参考，不证明现场道路或作业区域安全。",
        )

    @read_operation
    async def access_route(self, origin: Point, destination: Point):
        if not self.settings.amap_api_key:
            return self.envelope("amap", status="not_configured", missing=["AMAP_API_KEY"])
        start, end = await self.converted([origin, destination])
        value = await self.amap(
            "/v3/direction/driving",
            {"origin": start, "destination": end, "extensions": "base", "strategy": 0},
        )
        paths = value["route"]["paths"]
        if not paths:
            return self.envelope("amap", status="no_data", routes=[], attribution="高德地图")
        path = paths[0]
        steps = path.get("steps", [])
        distance, duration = float(path["distance"]), float(path["duration"])
        if not all(math.isfinite(v) and v >= 0 for v in (distance, duration)):
            raise ExternalContextError("UPSTREAM_INVALID_RESPONSE")
        return self.envelope(
            "amap",
            coordinate_system="GCJ02",
            origin=start,
            destination=end,
            routes=[
                {
                    "distance_meters": distance,
                    "duration_seconds": duration,
                    "steps": [
                        {k: text(step.get(k), 1000) for k in ("instruction", "road", "action")}
                        for step in steps[:20]
                    ],
                }
            ],
            limited=len(paths) > 1 or len(steps) > 20,
            attribution="高德地图",
            interpretation="普通道路到场参考；不计算泄漏扩散、隔离范围或安全疏散路线，现场通行须人工核实。",
        )

    @read_operation
    async def enterprise_gis(self, organization_id: UUID, point: Point):
        layer = self.settings.enterprise_gis_layers.get(str(organization_id))
        if not layer:
            return self.envelope(
                "arcgis",
                status="not_configured",
                missing=["ENTERPRISE_GIS_LAYERS entry for this organization"],
            )
        lat, lon = point
        headers = (
            {
                "X-Esri-Authorization": "Bearer "
                + self.settings.enterprise_gis_token.get_secret_value()
            }
            if self.settings.enterprise_gis_token
            else {}
        )
        value = await self.get(
            layer + "/query",
            headers=headers,
            params={
                "f": "json",
                "where": "1=1",
                "geometry": f"{lon:.6f},{lat:.6f}",
                "geometryType": "esriGeometryPoint",
                "inSR": 4326,
                "outSR": 4326,
                "spatialRel": "esriSpatialRelIntersects",
                "distance": self.settings.site_context_radius_meters,
                "units": "esriSRUnit_Meter",
                "outFields": self.settings.enterprise_gis_fields,
                "returnGeometry": "true",
                "resultRecordCount": 20,
            },
        )
        if "error" in value or not isinstance(value.get("features"), list):
            raise ExternalContextError("ARCGIS_QUERY_REJECTED")
        fields = set(self.settings.enterprise_gis_fields.split(","))
        features = []
        for feature in value["features"][:20]:
            geometry = feature.get("geometry")
            omitted = len(json.dumps(geometry)) > 8000
            features.append(
                {
                    "attributes": {
                        k: v for k, v in feature.get("attributes", {}).items() if k in fields
                    },
                    "geometry": None if omitted else geometry,
                    "geometry_omitted": omitted,
                }
            )
        return self.envelope(
            "arcgis",
            source=layer,
            features=features,
            coordinate_system="WGS84",
            radius_meters=self.settings.site_context_radius_meters,
            limited=bool(value.get("exceededTransferLimit"))
            or len(value["features"]) > 20
            or any(f["geometry_omitted"] for f in features),
            interpretation="按当前组织配置图层作空间邻域查询，最多20项；几何关系不等于设备归属或事故因果。",
        )
