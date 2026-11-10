"""Business graph integration: PostgreSQL authority, bounded traversal and human review."""

import json
from datetime import UTC, datetime, timedelta
from functools import partial
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import func, select, update

from app.infrastructure.providers.fake import FakeTextLLMProvider
from app.modules.maintenance_order.domain.models import WorkOrder
from app.modules.operation_event.domain.models import ProductionEvent
from app.modules.operational_graph.application.catalog import binding_revision
from app.modules.operational_graph.application.projection import synchronize
from app.modules.operational_graph.domain.models import GraphChange
from tests.helpers import create_confirmed_event, ensure_independent_reviewer, retest_measurement
from tests.test_agent_rag_tools import publish_manual
from tests.test_mcp_server import _mcp
from tests.test_measured_work_order_outcomes import executing_order, resolve


def post(client, path, payload):
    response = client.post("/api/v1" + path, json=payload)
    assert response.status_code in {200, 201, 202}, response.text
    return response.json()


def setup_asset(client):
    station = post(client, "/stations", {"code": "S-1", "name": "测试站"})
    equipment = post(
        client,
        "/equipment",
        {"station_id": station["id"], "code": "V-1", "name": "测试阀", "equipment_type": "valve"},
    )
    failure = post(client, "/failure-modes", {"code": "seal-leak", "name": "密封泄漏"})
    return station, equipment, failure


def associate(client, equipment, failure, kind, identifier):
    model = ProductionEvent if kind == "event" else WorkOrder

    async def revision():
        async with client.app.state.database.session_factory() as session:
            return binding_revision(await session.get(model, UUID(identifier)))

    return post(
        client,
        "/equipment-associations",
        {
            "equipment_id": equipment["id"],
            "failure_mode_id": failure["id"],
            "record_kind": kind,
            "record_id": identifier,
            "source_revision": client.portal.call(revision),
            "reason": "人工核对设备编号及故障类型",
        },
    )


def sync(client):
    response = client.post("/api/v1/operational-graph:sync")
    assert response.status_code == 200, response.text
    return response.json()


def test_identity_alias_conflict_correction_and_station_scope(client):
    station, equipment, _ = setup_asset(client)
    alias = post(
        client,
        f"/equipment/{equipment['id']}/aliases",
        {"expected_version": 1, "alias": " ＶＡＬＶＥ  ２ ", "reason": "现场人员确认别名"},
    )
    resolved = client.get(
        "/api/v1/equipment/resolve", params={"station_id": station["id"], "alias": "valve 2"}
    )
    assert resolved.json()["id"] == equipment["id"]
    duplicate = client.post(
        "/api/v1/equipment",
        json={
            "station_id": station["id"],
            "code": "VALVE 2",
            "name": "另一阀",
            "equipment_type": "valve",
        },
    )
    assert duplicate.status_code == 409
    other = post(client, "/stations", {"code": "S-2", "name": "另一站"})
    post(
        client,
        "/equipment",
        {"station_id": other["id"], "code": "VALVE 2", "name": "另一阀", "equipment_type": "valve"},
    )
    post(
        client,
        f"/equipment/{equipment['id']}/aliases/{alias['id']}:withdraw",
        {"expected_version": 2, "reason": "现场核对后撤销错误别名"},
    )
    assert (
        client.get(
            "/api/v1/equipment/resolve", params={"station_id": station["id"], "alias": "VALVE 2"}
        ).status_code
        == 404
    )


def test_transaction_capture_and_failed_projection_retries_without_losing_changes(client):
    setup_asset(client)
    sync(client)
    graph = client.app.state.knowledge.get().business_graph
    before = client.get("/api/v1/operational-graph/status").json()["generation"]

    async def rollback():
        async with client.app.state.database.session_factory() as session:
            count = await session.scalar(select(func.count()).select_from(GraphChange))
            await session.execute(update(ProductionEvent).values(title="rolled back"))
            await session.rollback()
            assert await session.scalar(select(func.count()).select_from(GraphChange)) == count

    client.portal.call(rollback)
    create_confirmed_event(client)
    original = graph.replace
    graph.replace = AsyncMock(side_effect=RuntimeError("simulated graph outage"))
    with pytest.raises(RuntimeError):
        client.portal.call(partial(synchronize, client.app.state.database.session_factory, graph))
    state = client.get("/api/v1/operational-graph/status").json()
    assert state["generation"] == before and state["pending_changes"] > 0
    graph.replace = original
    assert sync(client)["generation"] != before
    assert client.get("/api/v1/operational-graph/status").json()["pending_changes"] == 0


def test_recurrence_measured_acceptance_and_stale_binding(client):
    _, equipment, failure = setup_asset(client)
    order = executing_order(client)
    event = client.get(f"/api/v1/work-orders/{order}").json()["event_id"]
    link = associate(client, equipment, failure, "event", event)
    order_link = associate(client, equipment, failure, "work_order", order)
    assert resolve(client, order, 5, retest_measurement()).status_code == 200
    resolution = client.get(f"/api/v1/work-orders/{order}/resolutions").json()[0]
    post_response = client.post(
        f"/api/v1/work-orders/{order}:close",
        headers=ensure_independent_reviewer(client),
        json={
            "expected_version": 6,
            "reason": "独立复测通过",
            "resolution_id": resolution["id"],
            "measurements": resolution["measurements"],
        },
    )
    assert post_response.status_code == 200, post_response.text
    second = create_confirmed_event(client)["event_id"]
    associate(client, equipment, failure, "event", second)
    sync(client)
    path = f"/api/v1/equipment/{equipment['id']}/recurrences"
    result = client.get(path, params={"failure_mode_id": failure["id"]})
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["visible_episode_count"] == 2 and data["repeated_observations"]
    measured = [o for e in data["episodes"] for o in e["orders"] if o["measured_accepted"]]
    assert len(measured) == 1 and measured[0]["measurements"][0]["before"] == 2
    assert measured[0]["days_to_next_record"] is not None
    trace = client.get(
        "/api/v1/operational-graph/trace", params={"kind": "event", "record_id": event, "depth": 6}
    ).json()
    assert {"equipment", "work_order", "resolution", "acceptance"} <= {
        n["kind"] for n in trace["nodes"]
    }
    response = client.patch(
        f"/api/v1/equipment-associations/{link['id']}",
        json={
            "expected_version": 1,
            "active": False,
            "source_revision": link["source_revision"],
            "reason": "撤销错误设备关联",
        },
    )
    assert response.status_code == 200, response.text
    # Even before projection refresh, the old Neo4j path is filtered against authoritative SQL.
    result = client.get(path, params={"failure_mode_id": failure["id"]}).json()
    assert result["visible_episode_count"] == 2
    assert not result["projection"]["complete"]
    response = client.patch(
        f"/api/v1/equipment-associations/{order_link['id']}",
        json={
            "expected_version": 1,
            "active": False,
            "source_revision": order_link["source_revision"],
            "reason": "撤销此工单设备确认",
        },
    )
    assert response.status_code == 200, response.text
    assert (
        client.get(path, params={"failure_mode_id": failure["id"]}).json()["visible_episode_count"]
        == 1
    )


def test_graph_agent_tool_and_saved_evidence_source_freshness(client, monkeypatch):
    event = create_confirmed_event(client)["event_id"]
    sync(client)
    original = FakeTextLLMProvider.generate_structured

    async def generate(self, *, operation, system_prompt, user_prompt, response_model):
        if operation == "multi_agent_evidence":
            payload = json.loads(user_prompt)
            return response_model.model_validate(
                {
                    "action": "trace_case_relations"
                    if payload["remaining_steps"] == 6
                    else "finish",
                    "query": "业务证据链",
                    "unknowns": [],
                }
            )
        return await original(
            self,
            operation=operation,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_model=response_model,
        )

    monkeypatch.setattr(FakeTextLLMProvider, "generate_structured", generate)
    classified = client.post(f"/api/v1/events/{event}:classify")
    assert classified.status_code == 202, classified.text
    run = classified.json()["agent_run_id"]
    evidence = client.get(f"/api/v1/agent-runs/{run}/evidence")
    assert evidence.status_code == 200, evidence.text
    assert any(e["source_server"] == "business-graph" for e in evidence.json())
    wire = _mcp(
        client, "tools/call", {"name": "trace_case_relations", "arguments": {"event_id": event}}
    )
    assert not wire["result"].get("isError"), wire
    for name in ["find_recurrent_hazards", "assess_change_impact"]:
        response = _mcp(client, "tools/call", {"name": name, "arguments": {"event_id": event}})
        assert not response["result"].get("isError"), response

    async def changed():
        async with client.app.state.database.session_factory() as session:
            await session.execute(
                update(ProductionEvent)
                .where(ProductionEvent.id == UUID(event))
                .values(title="人工更新后的事件标题")
            )
            await session.commit()

    client.portal.call(changed)
    assert client.get(f"/api/v1/agent-runs/{run}/evidence").status_code == 409


def test_knowledge_withdrawal_generates_review_and_requires_fresh_acceptance(client, monkeypatch):
    version = publish_manual(client)
    original = FakeTextLLMProvider.generate_structured

    async def generate(self, *, operation, system_prompt, user_prompt, response_model):
        if operation == "multi_agent_evidence":
            payload = json.loads(user_prompt)
            return response_model.model_validate(
                {
                    "action": "search_knowledge" if payload["remaining_steps"] == 6 else "finish",
                    "query": "阀门",
                    "unknowns": [],
                }
            )
        return await original(
            self,
            operation=operation,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_model=response_model,
        )

    monkeypatch.setattr(FakeTextLLMProvider, "generate_structured", generate)
    order = executing_order(client)
    sync(client)
    assert client.get("/api/v1/impact-reviews").json() == []

    async def publish_replacement():
        from uuid import uuid4

        from app.modules.knowledge.domain.models import KnowledgeVersion

        async with client.app.state.database.session_factory() as session:
            old = await session.get(KnowledgeVersion, UUID(version))
            values = {
                c.name: getattr(old, c.name)
                for c in KnowledgeVersion.__table__.columns
                if c.name not in {"id", "created_at", "updated_at"}
            }
            values.update(
                number=old.number + 1,
                object_key=old.object_key + "-replacement",
                generation=uuid4().hex,
                effective_from=datetime.now(UTC) - timedelta(seconds=1),
            )
            replacement = KnowledgeVersion(**values)
            session.add(replacement)
            await session.commit()
            return replacement.id

    replacement = client.portal.call(publish_replacement)
    sync(client)
    revisions = client.get("/api/v1/impact-reviews").json()
    assert any(
        r["change_type"] == "new_revision"
        and r["target_key"] == f"work_order:{order}"
        and r["replacement_version_id"] == str(replacement)
        for r in revisions
    )

    async def expire():
        from app.modules.knowledge.domain.models import KnowledgeVersion

        async with client.app.state.database.session_factory() as session:
            await session.execute(
                update(KnowledgeVersion)
                .where(KnowledgeVersion.id.in_([UUID(version), replacement]))
                .values(effective_to=datetime.now(UTC) - timedelta(milliseconds=100))
            )
            await session.commit()

    client.portal.call(expire)
    sync(client)
    expired = client.get("/api/v1/impact-reviews").json()
    assert any(r["change_type"] == "expiry" and r["status"] == "pending" for r in expired)
    assert any(r["change_type"] == "new_revision" and r["status"] == "superseded" for r in expired)
    post(client, f"/knowledge/versions/{version}:withdraw", {"reason": "人工撤回已失效规范"})
    sync(client)
    impact = client.get(f"/api/v1/knowledge/versions/{version}/impact")
    assert impact.status_code == 200, impact.text
    assert any(t["record"]["record_id"] == order for t in impact.json()["targets"])
    reviews = client.get("/api/v1/impact-reviews").json()
    case = next(
        r for r in reviews if r["target_key"] == f"work_order:{order}" and r["status"] == "pending"
    )
    needs = post(
        client,
        f"/impact-reviews/{case['id']}:review",
        {
            "expected_version": case["version"],
            "target_revision": case["target_revision"],
            "decision": "reassessment_required",
            "reason": "按新依据重新安排实测并记录措施",
        },
    )
    invalid = client.post(
        f"/api/v1/impact-reviews/{case['id']}:review",
        json={
            "expected_version": needs["version"],
            "target_revision": needs["target_revision"],
            "decision": "revalidated",
            "reason": "没有验收不能关闭",
        },
    )
    assert invalid.status_code == 409
    assert resolve(client, order, 5, retest_measurement()).status_code == 200
    resolution = client.get(f"/api/v1/work-orders/{order}/resolutions").json()[0]
    response = client.post(
        f"/api/v1/work-orders/{order}:close",
        headers=ensure_independent_reviewer(client),
        json={
            "expected_version": 6,
            "reason": "独立复测通过",
            "resolution_id": resolution["id"],
            "measurements": resolution["measurements"],
        },
    )
    assert response.status_code == 200, response.text
    case = client.get(f"/api/v1/impact-reviews/{case['id']}").json()
    acceptance = client.get(f"/api/v1/work-orders/{order}/resolutions").json()[0]["acceptance"][
        "id"
    ]
    closed = post(
        client,
        f"/impact-reviews/{case['id']}:review",
        {
            "expected_version": case["version"],
            "target_revision": case["target_revision"],
            "decision": "revalidated",
            "reason": "重新安排措施和复测，独立验收通过",
            "verification_id": acceptance,
        },
    )
    assert closed["status"] == "closed"
    review_tool = _mcp(
        client, "tools/call", {"name": "get_impact_review", "arguments": {"review_id": case["id"]}}
    )
    assert not review_tool["result"]["isError"], review_tool
    resource = _mcp(client, "resources/read", {"uri": f"pipechina://impact-reviews/{case['id']}"})
    assert "revalidated" in json.dumps(resource)

    assert [h["action"] for h in closed["history"]] == [
        "detected",
        "reassessment_required",
        "revalidated",
    ]


def test_late_commit_outbox_is_not_lost_by_sequence_watermark(client):
    from app.modules.operational_graph.domain.models import FailureMode, Station

    station, _, failure = setup_asset(client)
    sync(client)

    async def late_commit():
        factory = client.app.state.database.session_factory
        graph = client.app.state.knowledge.get().business_graph
        async with factory() as first:
            await first.execute(
                update(Station)
                .where(Station.id == UUID(station["id"]))
                .values(name="延后提交的新站名")
            )
            async with factory() as second:
                await second.execute(
                    update(FailureMode)
                    .where(FailureMode.id == UUID(failure["id"]))
                    .values(name="先提交的故障名称")
                )
                await second.commit()
            await synchronize(factory, graph)
            await first.commit()
        async with factory() as check:
            pending = list(await check.scalars(select(GraphChange)))
            assert len(pending) == 1 and pending[0].source_table == "operational_stations"
        await synchronize(factory, graph)
        async with factory() as check:
            assert await check.scalar(select(func.count()).select_from(GraphChange)) == 0

    client.portal.call(late_commit)


def test_graph_read_never_grants_source_access_or_global_statistics(client):
    from app.shared.security.authorization.permissions import Permissions as P
    from tests.security_helpers import provision_test_identity, request_mock_token

    _, equipment, failure = setup_asset(client)
    event = create_confirmed_event(client)["event_id"]
    associate(client, equipment, failure, "event", event)
    sync(client)
    client.portal.call(
        partial(
            provision_test_identity,
            client.app,
            client.app.state.settings,
            username="peter",
            permissions={P.GRAPH_READ, P.EQUIPMENT_READ},
        )
    )
    headers = {
        "Authorization": "Bearer " + request_mock_token(client.app.state.settings, username="peter")
    }
    denied = client.get(
        "/api/v1/operational-graph/trace",
        headers=headers,
        params={"kind": "event", "record_id": event},
    )
    assert denied.status_code == 403
    allowed = client.get(
        "/api/v1/operational-graph/trace",
        headers=headers,
        params={"kind": "equipment", "record_id": equipment["id"]},
    )
    assert allowed.status_code == 200, allowed.text
    assert "event" not in {n["kind"] for n in allowed.json()["nodes"]}
    assert "counts" not in allowed.json()["projection"]
    assert client.get("/api/v1/operational-graph/status", headers=headers).status_code == 403


def test_event_hazard_dedup_and_source_revision_invalidation(client):
    from app.modules.hazard.domain.models import Hazard

    station, equipment, failure = setup_asset(client)
    event = create_confirmed_event(client)["event_id"]
    associate(client, equipment, failure, "event", event)
    hazard = post(
        client,
        "/hazards",
        {
            "title": "测试阀渗漏",
            "description": "同一事件派生的测试隐患",
            "location": station["name"],
            "category": "设备设施",
            "source_type": "production_event",
            "source_id": event,
        },
    )
    user = client.get("/api/v1/auth/me").json()["user_id"]
    post(
        client,
        f"/hazards/{hazard['id']}:assess",
        {
            "likelihood": 2,
            "consequence": 2,
            "due_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "rationale": "合成测试矩阵",
            "control_measures": "合成警戒措施",
            "assignee_id": user,
            "expected_version": 1,
        },
    )

    async def bind_hazard():
        async with client.app.state.database.session_factory() as session:
            return binding_revision(await session.get(Hazard, UUID(hazard["id"])))

    post(
        client,
        "/equipment-associations",
        {
            "equipment_id": equipment["id"],
            "failure_mode_id": failure["id"],
            "record_kind": "hazard",
            "record_id": hazard["id"],
            "source_revision": client.portal.call(bind_hazard),
            "reason": "确认与事件为同一设备故障",
        },
    )
    sync(client)
    path = f"/api/v1/equipment/{equipment['id']}/recurrences"
    data = client.get(path, params={"failure_mode_id": failure["id"]}).json()
    assert data["visible_episode_count"] == 1 and len(data["episodes"][0]["records"]) == 2

    post(
        client,
        f"/hazards/{hazard['id']}:start-rectification",
        {"expected_version": 2, "reason": "开始合成整改"},
    )
    post(
        client,
        f"/hazards/{hazard['id']}:submit-rectification",
        {
            "expected_version": 3,
            "action": "更换合成测试密封件",
            "result": "合成读数通过",
            "measurements": [retest_measurement()],
        },
    )
    rectification = client.get(f"/api/v1/hazards/{hazard['id']}").json()["rectifications"][-1]
    accepted = client.post(
        f"/api/v1/hazards/{hazard['id']}:accept",
        headers=ensure_independent_reviewer(client),
        json={
            "expected_version": 4,
            "rectification_id": rectification["id"],
            "reason": "独立验收合成隐患整改",
            "measurements": rectification["measurements"],
        },
    )
    assert accepted.status_code == 200, accepted.text
    sync(client)
    outcome = client.get(path, params={"failure_mode_id": failure["id"]}).json()["episodes"][0][
        "hazard_outcomes"
    ][0]
    assert outcome["measured_accepted"] and outcome["action"] == "更换合成测试密封件"
    assert (
        outcome["measurements"][0]["before"] == 2
        and outcome["verification_measurements"][0]["after"] == 0
    )

    async def change_identity():
        async with client.app.state.database.session_factory() as session:
            await session.execute(
                update(Hazard)
                .where(Hazard.id == UUID(hazard["id"]))
                .values(location="另一台设备的位置")
            )
            await session.commit()

    client.portal.call(change_identity)
    data = client.get(path, params={"failure_mode_id": failure["id"]}).json()
    assert data["stale_associations"] and len(data["episodes"][0]["records"]) == 1


async def test_directional_impact_does_not_cross_document_versions():
    from app.infrastructure.knowledge.business_graph import MemoryBusinessGraph
    from app.ports.business_graph import BusinessEdge, BusinessNode

    graph = MemoryBusinessGraph()
    nodes = [
        BusinessNode(key=k, kind=k, record_id=k, revision="1")
        for k in ["doc", "old", "new", "old-run", "new-run", "old-order", "new-order"]
    ]
    links = [
        ("doc", "old", "HAS_VERSION"),
        ("doc", "new", "HAS_VERSION"),
        ("old", "old-run", "CITED_BY"),
        ("new", "new-run", "CITED_BY"),
        ("old-run", "old-order", "PROPOSED_ORDER"),
        ("new-run", "new-order", "PROPOSED_ORDER"),
    ]
    edges = [
        BusinessEdge(key=str(i), source=a, target=b, relation=r, reference=str(i))
        for i, (a, b, r) in enumerate(links)
    ]
    await graph.replace("g", nodes, edges)
    paths = await graph.paths("g", "old", [n.key for n in nodes], 6, 100, dependencies_only=True)
    assert {p.nodes[-1] for p in paths} == {"old-run", "old-order"}
    denied = await graph.paths("g", "old", ["old", "old-order"], 6, 100, dependencies_only=True)
    assert not denied


def test_audio_transcript_extraction_keeps_the_handover_path(client):
    from tests.helpers import create_confirmed_handover

    audio = create_confirmed_handover(client)["audio_id"]
    row = client.get(f"/api/v1/audio-records/{audio}").json()
    extracted = post(
        client,
        "/event-extractions",
        {
            "source_type": "audio_transcript",
            "source_id": audio,
            "source_version_id": row["current_transcript_version_id"],
        },
    )
    event = extracted["event_ids"][0]
    post(
        client,
        f"/events/{event}:confirm",
        {"expected_version_id": client.get(f"/api/v1/events/{event}").json()["current_version_id"]},
    )
    sync(client)
    result = client.get(
        "/api/v1/operational-graph/trace", params={"kind": "event", "record_id": event}
    ).json()
    assert any(n["key"] == f"audio:{audio}" for n in result["nodes"])
    assert any(
        e["relation"] == "SOURCE_OF" and e["source"] == f"audio:{audio}" for e in result["edges"]
    )


def test_knowledge_effectivity_boundary_changes_graph_revision_without_a_write(client, monkeypatch):
    from app.modules.knowledge.domain.models import KnowledgeVersion
    from app.modules.operational_graph.application import catalog

    version = publish_manual(client)
    now = datetime.now(UTC)

    async def set_expiry():
        async with client.app.state.database.session_factory() as session:
            await session.execute(
                update(KnowledgeVersion)
                .where(KnowledgeVersion.id == UUID(version))
                .values(effective_to=now + timedelta(days=1))
            )
            await session.commit()

    client.portal.call(set_expiry)
    sync(client)
    params = {"kind": "knowledge_version", "record_id": version}
    before_trace = client.get("/api/v1/operational-graph/trace", params=params).json()
    before = next(n for n in before_trace["nodes"] if n["key"] == before_trace["root"])

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return now + timedelta(days=2)

    monkeypatch.setattr(catalog, "datetime", Later)
    after_trace = client.get("/api/v1/operational-graph/trace", params=params).json()
    after = next(n for n in after_trace["nodes"] if n["key"] == after_trace["root"])
    assert before["details"]["currently_effective"] and not after["details"]["currently_effective"]
    assert before["revision"] != after["revision"]
