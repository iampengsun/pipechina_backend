"""Read-only external tools preserve location authority, scope and temporal provenance."""

import json
import logging
from datetime import UTC, datetime, timedelta
from functools import partial
from uuid import UUID, uuid4

import httpx
import pytest

from app.bootstrap.config import Settings
from app.infrastructure.providers.fake import FakeTextLLMProvider
from app.infrastructure.site_context.http import HTTPSiteContext
from app.modules.operation_event.application.site_context import validate_saved_context
from app.modules.operation_event.domain.agent_models import AgentRun
from app.shared.errors import ConflictError
from app.shared.platform.models import AsyncJob
from app.shared.worker_runtime import job_user
from tests.helpers import create_confirmed_event
from tests.test_mcp_server import _mcp
from tests.test_operational_graph import associate
from tests.test_operational_graph import setup_asset as assets


def configure_gateway(client, handler, **config):
    async def replace():
        await client.app.state.site_context.close()
        client.app.state.site_context = HTTPSiteContext(
            Settings(_env_file=None, **config), transport=httpx.MockTransport(handler)
        )

    client.portal.call(replace)


def location(client, station_id, *, version=0, active=True):
    body = {
        "expected_version": version,
        "active": active,
        "source": "合成验收：人工测绘记录",
        "reason": "软件测试位置确认",
    }
    if active:
        body.update(latitude=39.9, longitude=116.4)
    response = client.put(f"/api/v1/stations/{station_id}/location", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def bound_event(client):
    station, equipment, failure = assets(client)
    event_id = create_confirmed_event(client)["event_id"]
    associate(client, equipment, failure, "event", event_id)
    location(client, station["id"])
    return event_id, station


def weather_payload():
    now = datetime.now(UTC)

    def alert(identifier, **values):
        return {
            "id": identifier,
            "headline": "大风预警",
            "severity": "severe",
            "senderName": "合成气象发布机构",
            "messageType": {"code": "alert"},
            "issuedTime": now.isoformat(),
            "effectiveTime": now.isoformat(),
            "expireTime": (now + timedelta(hours=1)).isoformat(),
            **values,
        }

    return {
        "metadata": {
            "tag": "weather-version",
            "zeroResult": False,
            "attributions": ["合成测试，非真实预警"],
        },
        "alerts": [
            alert("active"),
            alert("expired", expireTime=(now - timedelta(seconds=1)).isoformat()),
            alert("cancelled", messageType={"code": "cancel"}),
            alert("superseded", latestChange={"id": "new-alert"}),
        ],
    }


def test_station_location_versions_bounds_and_withdrawal(client):
    station, _, _ = assets(client)
    assert client.get(f"/api/v1/stations/{station['id']}/location").json() is None
    first = location(client, station["id"])
    assert first["version"] == 1 and first["coordinate_system"] == "WGS84"
    bad = {
        "expected_version": 0,
        "latitude": 39.9,
        "longitude": 116.4,
        "source": "测试测绘",
        "reason": "旧版本并发修改",
    }
    assert client.put(f"/api/v1/stations/{station['id']}/location", json=bad).status_code == 409
    bad.update(expected_version=1, latitude=91)
    assert client.put(f"/api/v1/stations/{station['id']}/location", json=bad).status_code == 422
    withdrawn = location(client, station["id"], version=1, active=False)
    assert withdrawn["version"] == 2 and withdrawn["latitude"] is None


@pytest.mark.asyncio
async def test_qweather_auth_times_cancellations_and_missing_data():
    seen = []

    def handler(request):
        seen.append(request)
        assert request.headers["X-QW-Api-Key"] == "weather-secret"
        assert request.url.path.endswith("/39.900000/116.400000")
        return httpx.Response(200, json=weather_payload())

    gateway = HTTPSiteContext(
        Settings(
            _env_file=None,
            qweather_api_host="https://example.qweatherapi.com",
            qweather_api_key="weather-secret",
        ),
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await gateway.weather_alerts((39.9, 116.4))
        assert [a["id"] for a in result["alerts"]] == ["active"]
        assert result["status"] == "partial" and result["ignored_messages"] == 3
        assert result["attributions"]
        assert "weather-secret" not in json.dumps(result)
        assert "key" not in str(seen[0].url)
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_amap_converts_coordinates_routes_and_redacts_key(caplog):
    seen = []

    def handler(request):
        seen.append(request)
        assert request.url.params["key"] == "amap-secret-key"
        if request.url.path.endswith("/coordinate/convert"):
            assert request.url.params["coordsys"] == "gps"
            assert request.url.params["locations"].startswith("116.400000,39.900000")
            value = "116.406000,39.902000"
            if "|" in request.url.params["locations"]:
                value += ";117.006000,40.002000"
            return httpx.Response(200, json={"status": "1", "locations": value})
        if request.url.path.endswith("/regeo"):
            assert request.url.params["location"] == "116.406000,39.902000"
            assert request.url.params["radius"] == "3000"
            return httpx.Response(
                200,
                json={
                    "status": "1",
                    "regeocode": {"formatted_address": "合成测试站周边", "pois": [], "roads": []},
                },
            )
        assert request.url.params["origin"] == "116.406000,39.902000"
        return httpx.Response(
            200,
            json={
                "status": "1",
                "route": {
                    "paths": [
                        {"distance": "1200", "duration": "300", "steps": [{"road": "合成道路"}]}
                    ]
                },
            },
        )

    gateway = HTTPSiteContext(
        Settings(_env_file=None, amap_api_key="amap-secret-key", site_context_radius_meters=8000),
        transport=httpx.MockTransport(handler),
    )
    try:
        with caplog.at_level(logging.INFO, logger="httpx"):
            result = await gateway.map_context((39.9, 116.4))
            route = await gateway.access_route((39.9, 116.4), (40.0, 117.0))
        assert result["coordinate_system"] == "GCJ02"
        assert route["routes"][0]["distance_meters"] == 1200
        assert len(seen) == 4
        assert "amap-secret-key" not in caplog.text
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_arcgis_scope_allowlisted_fields_and_transfer_limit():
    org = uuid4()
    seen = []

    def handler(request):
        seen.append(request)
        assert request.url.params["geometry"] == "116.400000,39.900000"
        assert request.url.params["inSR"] == "4326"
        assert request.url.params["resultRecordCount"] == "20"
        assert request.headers["X-Esri-Authorization"] == "Bearer gis-secret"
        return httpx.Response(
            200,
            json={
                "features": [
                    {
                        "attributes": {"OBJECTID": 1, "name": "合成管段", "secret": "excluded"},
                        "geometry": {"x": 116.4, "y": 39.9},
                    }
                ],
                "exceededTransferLimit": True,
            },
        )

    gateway = HTTPSiteContext(
        Settings(
            _env_file=None,
            enterprise_gis_layers={
                str(org): "https://gis.example.com/arcgis/rest/services/test/FeatureServer/0"
            },
            enterprise_gis_token="gis-secret",
        ),
        transport=httpx.MockTransport(handler),
    )
    try:
        assert (await gateway.enterprise_gis(uuid4(), (39.9, 116.4)))["status"] == "not_configured"
        assert not seen
        result = await gateway.enterprise_gis(org, (39.9, 116.4))
        assert result["limited"] is True
        assert "secret" not in result["features"][0]["attributes"]
        assert "token" not in str(seen[0].url)
    finally:
        await gateway.close()


def test_context_missing_binding_position_config_and_route_origin(client):
    event_id = create_confirmed_event(client)["event_id"]
    url = f"/api/v1/events/{event_id}/site-context/get_weather_alerts"
    assert client.get(url).json()[0]["status"] == "needs_location"
    station, equipment, failure = assets(client)
    associate(client, equipment, failure, "event", event_id)
    assert client.get(url).json()[0]["status"] == "needs_location"
    location(client, station["id"])
    assert client.get(url).json()[0]["status"] == "not_configured"
    route = client.get(f"/api/v1/events/{event_id}/site-context/get_access_route")
    assert route.json()[0]["status"] == "needs_origin"
    unrelated = client.post(
        "/api/v1/stations", json={"code": "unrelated", "name": "无关联站场"}
    ).json()
    assert client.get(url, params={"station_id": unrelated["id"]}).status_code == 409


def test_weather_rest_mcp_agent_evidence_and_location_freshness(client, monkeypatch):
    event_id, station = bound_event(client)
    configure_gateway(
        client,
        lambda request: httpx.Response(200, json=weather_payload()),
        qweather_api_host="https://example.qweatherapi.com",
        qweather_api_key="weather-secret",
    )
    direct = client.get(f"/api/v1/events/{event_id}/site-context/get_weather_alerts")
    assert direct.status_code == 200, direct.text
    context = direct.json()[0]
    assert context["location_refs"][0]["location_version"] == 1
    protocol = _mcp(
        client, "tools/call", {"name": "get_weather_alerts", "arguments": {"event_id": event_id}}
    )
    assert protocol["result"]["isError"] is False, protocol
    assert "active" in json.dumps(protocol)
    original = FakeTextLLMProvider.generate_structured

    async def generate(self, **kwargs):
        if kwargs["operation"] == "multi_agent_coordinator":
            return kwargs["response_model"].model_validate(
                {
                    "roles": ["evidence", "risk", "plan", "critic"],
                    "focus": ["现场环境"],
                    "investigations": [{"kind": "weather", "query": ""}],
                }
            )
        return await original(self, **kwargs)

    monkeypatch.setattr(FakeTextLLMProvider, "generate_structured", generate)
    response = client.post(f"/api/v1/events/{event_id}:classify")
    assert response.status_code == 202, response.text
    run_id = response.json()["agent_run_id"]
    rows = client.get(f"/api/v1/agent-runs/{run_id}/evidence").json()
    assert any(e["source_server"] == "site-context" for e in rows)

    async def check():
        async with client.app.state.database.session_factory() as session:
            run = await session.get(AgentRun, UUID(run_id))
            user = await job_user(session, await session.get(AsyncJob, run.job_id))
            expired = {
                **context,
                "valid_until": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            }
            await validate_saved_context(session, user, expired)
            with pytest.raises(ConflictError, match="expired"):
                await validate_saved_context(session, user, expired, require_fresh=True)

    client.portal.call(check)
    location(client, station["id"], version=1, active=False)
    assert client.get(f"/api/v1/agent-runs/{run_id}/evidence").status_code == 409


def test_outbound_failure_is_unknown_and_does_not_leak_provider_errors(client):
    event_id, _ = bound_event(client)

    def handler(request):
        return httpx.Response(403, json={"message": "secret=not-for-output"})

    configure_gateway(
        client,
        handler,
        qweather_api_host="https://example.qweatherapi.com",
        qweather_api_key="weather-secret",
    )
    response = client.get(f"/api/v1/events/{event_id}/site-context/get_weather_alerts")
    assert response.status_code == 200, response.text
    data = response.json()[0]
    assert data["status"] == "unavailable" and data["error_code"] == "UPSTREAM_HTTP_ERROR"
    assert "not-for-output" not in response.text


def test_external_tools_require_equipment_permission(client, settings):
    from app.shared.security.authorization.permissions import Permissions as P
    from app.shared.security.authorization.scopes import ScopeType
    from tests.security_helpers import provision_test_identity, request_mock_token

    event_id, _ = bound_event(client)
    client.portal.call(
        partial(
            provision_test_identity,
            client.app,
            settings,
            username="peter",
            permissions={P.EVENT_READ},
            scope_type=ScopeType.GLOBAL,
        )
    )
    headers = {"Authorization": "Bearer " + request_mock_token(settings, username="peter")}
    result = client.get(
        f"/api/v1/events/{event_id}/site-context/get_weather_alerts", headers=headers
    )
    assert result.status_code == 403, result.text


def test_resume_refreshes_weather_without_reusing_expired_evidence(client, monkeypatch):
    from sqlalchemy import select

    from app.modules.operation_event.domain.agent_models import AgentEvidence, AgentProposal

    event_id, _ = bound_event(client)
    configure_gateway(
        client,
        lambda request: httpx.Response(200, json=weather_payload()),
        qweather_api_host="https://example.qweatherapi.com",
        qweather_api_key="weather-secret",
    )
    original = FakeTextLLMProvider.generate_structured
    paused = False

    async def generate(self, **kwargs):
        nonlocal paused
        if kwargs["operation"] == "multi_agent_coordinator":
            return kwargs["response_model"].model_validate(
                {
                    "roles": ["evidence", "risk", "plan", "critic"],
                    "focus": ["天气与现场压力"],
                    "investigations": [{"kind": "weather", "query": ""}],
                }
            )
        if kwargs["operation"] == "multi_agent_evidence" and not paused:
            paused = True
            return kwargs["response_model"].model_validate(
                {
                    "action": "finish",
                    "query": "",
                    "questions": ["请人工确认现场压力"],
                    "unknowns": ["压力待确认"],
                }
            )
        return await original(self, **kwargs)

    monkeypatch.setattr(FakeTextLLMProvider, "generate_structured", generate)
    response = client.post(f"/api/v1/events/{event_id}:classify")
    assert response.status_code == 202, response.text
    run_id = response.json()["agent_run_id"]
    assert client.get(f"/api/v1/agent-runs/{run_id}").json()["status"] == "waiting_input"

    async def expire():
        async with client.app.state.database.session_factory() as session:
            evidence = await session.scalar(
                select(AgentEvidence).where(
                    AgentEvidence.run_id == UUID(run_id),
                    AgentEvidence.source_server == "site-context",
                )
            )
            evidence.content = {
                **evidence.content,
                "valid_until": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            }
            identifier = str(evidence.id)
            await session.commit()
            return identifier

    expired_id = client.portal.call(expire)
    version = client.get(f"/api/v1/events/{event_id}").json()["current_version_id"]
    note = client.post(
        f"/api/v1/events/{event_id}/observations",
        json={"expected_version_id": version, "content": "现场压力已核对，当前6MPa。"},
    ).json()
    client.post(
        f"/api/v1/events/{event_id}/observations/{note['id']}:review",
        json={"expected_version": 1, "action": "confirm", "reason": "现场实测核对"},
    ).raise_for_status()
    response = client.post(
        f"/api/v1/agent-runs/{run_id}:resume",
        json={
            "request_version": 1,
            "confirmed_observation_ids": [note["id"]],
            "reason": "补充实测压力",
        },
    )
    assert response.status_code == 202, response.text
    assert client.get(f"/api/v1/agent-runs/{run_id}").json()["status"] == "succeeded"
    evidence = client.get(f"/api/v1/agent-runs/{run_id}/evidence").json()
    assert len([e for e in evidence if e["source_server"] == "site-context"]) == 2

    async def proposal():
        async with client.app.state.database.session_factory() as session:
            row = await session.scalar(
                select(AgentProposal).where(AgentProposal.run_id == UUID(run_id))
            )
            return row.evidence_ids

    assert expired_id not in client.portal.call(proposal)


def test_unconfigured_weather_branch_keeps_gap_in_risk_report(client, monkeypatch):
    event_id, _ = bound_event(client)
    original = FakeTextLLMProvider.generate_structured

    async def generate(self, **kwargs):
        if kwargs["operation"] == "multi_agent_coordinator":
            return kwargs["response_model"].model_validate(
                {
                    "roles": ["evidence", "risk", "plan", "critic"],
                    "focus": ["室外天气"],
                    "investigations": [{"kind": "weather", "query": ""}],
                }
            )
        return await original(self, **kwargs)

    monkeypatch.setattr(FakeTextLLMProvider, "generate_structured", generate)
    response = client.post(f"/api/v1/events/{event_id}:classify")
    assert response.status_code == 202, response.text
    run_id = response.json()["agent_run_id"]
    run = client.get(f"/api/v1/agent-runs/{run_id}").json()
    risk = next(task["output"] for task in run["tasks"] if task["role"] == "risk")
    assert any("QWEATHER_API_HOST" in message for message in risk["unknowns"])
