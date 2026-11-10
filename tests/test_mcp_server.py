"""Exercise the real MCP wire endpoint and its business authorization boundary."""

from __future__ import annotations

import json
from functools import partial
from uuid import uuid4

from fastapi.testclient import TestClient

from app.bootstrap.config import Settings
from app.shared.security.authorization.permissions import Permissions
from app.shared.security.authorization.scopes import ScopeType
from tests.helpers import create_confirmed_event
from tests.security_helpers import provision_test_identity, request_mock_token


def _mcp(client: TestClient, method: str, params: dict | None = None, *, tasks=False) -> dict:
    response = client.post(
        "/mcp/",
        headers={
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": method,
            **(
                {"Mcp-Name": str(params["taskId"])}
                if method.startswith("tasks/") and params
                else {}
            ),
            **(
                {"Mcp-Name": str(params["uri"] if method == "resources/read" else params["name"])}
                if method in {"tools/call", "prompts/get", "resources/read"} and params
                else {}
            ),
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {
                **(params or {}),
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {
                        "extensions": {"io.modelcontextprotocol/tasks": {}}
                    }
                    if tasks
                    else {},
                },
            },
        },
    )
    assert response.status_code in {200, 400}, response.text
    return response.json()


def test_mcp_requires_bearer_before_discovery(anonymous_client: TestClient) -> None:
    response = anonymous_client.post(
        "/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


def test_mcp_lists_only_read_tools_and_calls_with_scope(client: TestClient) -> None:
    first = create_confirmed_event(client)["event_id"]
    second = create_confirmed_event(client)["event_id"]
    title = client.get(f"/api/v1/events/{first}").json()["title"]

    listed = _mcp(client, "tools/list")
    tools = listed["result"]["tools"]
    assert {tool["name"] for tool in tools} == {
        "submit_case_observation",
        "start_investigation",
        "get_work_order_verification",
        "get_impact_review",
        "trace_case_relations",
        "find_recurrent_hazards",
        "assess_change_impact",
        "search_events",
        "search_work_orders",
        "search_handover",
        "get_case_context",
        "get_weather_alerts",
        "get_map_context",
        "query_enterprise_gis",
        "get_access_route",
        "search_knowledge",
        "search_experiences",
        "get_evaluation_improvement",
    }
    assert {tool["name"] for tool in tools if not tool["annotations"]["readOnlyHint"]} == {
        "submit_case_observation",
        "start_investigation",
    }
    assert all(
        "event_id" in tool["inputSchema"]["required"]
        for tool in tools
        if tool["name"]
        not in {"get_evaluation_improvement", "get_work_order_verification", "get_impact_review"}
    )

    result = _mcp(
        client,
        "tools/call",
        {"name": "search_events", "arguments": {"event_id": second, "query": title}},
    )
    assert "error" not in result, result
    assert result["result"]["isError"] is False
    assert first in json.dumps(result["result"])
    assert second not in json.dumps(result["result"])

    denied = _mcp(
        client,
        "tools/call",
        {"name": "search_events", "arguments": {"event_id": str(uuid4()), "query": first}},
    )
    assert denied["result"]["isError"] is True


def test_mcp_rechecks_database_permissions_for_each_call(
    client: TestClient, settings: Settings
) -> None:
    event_id = create_confirmed_event(client)["event_id"]
    assert client.portal is not None
    client.portal.call(
        partial(
            provision_test_identity,
            client.app,
            settings,
            username="peter",
            permissions={Permissions.MAINTENANCE_READ},
            scope_type=ScopeType.OWN_ORG,
        )
    )
    client.headers["Authorization"] = f"Bearer {request_mock_token(settings, username='peter')}"
    result = _mcp(
        client,
        "tools/call",
        {"name": "search_work_orders", "arguments": {"event_id": event_id, "query": "维检"}},
    )
    assert result["result"]["isError"] is True
    resource = _mcp(client, "resources/read", {"uri": f"pipechina://events/{event_id}/context"})
    assert "error" in resource
    prompt = _mcp(
        client,
        "prompts/get",
        {
            "name": "investigation_handover",
            "arguments": {"event_id": event_id},
        },
    )
    assert "error" in prompt


def test_mcp_cross_shift_context_resource_and_prompt(client):
    event_id = create_confirmed_event(client)["event_id"]
    event = client.get(f"/api/v1/events/{event_id}").json()
    client.post(
        f"/api/v1/events/{event_id}/observations",
        json={"expected_version_id": event["current_version_id"], "content": "尚未确认的班次补充"},
    ).raise_for_status()
    context = _mcp(
        client,
        "tools/call",
        {
            "name": "get_case_context",
            "arguments": {"event_id": event_id},
        },
    )
    assert context["result"]["isError"] is False, context
    assert event["current_version_id"] in json.dumps(context)
    assert "尚未确认的班次补充" not in json.dumps(context, ensure_ascii=False)
    templates = _mcp(client, "resources/templates/list")
    assert "pipechina://events/{event_id}/context" in json.dumps(templates)
    resource = _mcp(client, "resources/read", {"uri": f"pipechina://events/{event_id}/context"})
    assert "error" not in resource, resource
    data = json.loads(resource["result"]["contents"][0]["text"])
    assert data["event_id"] == event_id
    prompts = _mcp(client, "prompts/list")
    assert "investigation_handover" in json.dumps(prompts)
    prompt = _mcp(
        client,
        "prompts/get",
        {
            "name": "investigation_handover",
            "arguments": {"event_id": event_id},
        },
    )
    assert "error" not in prompt, prompt
    assert "search_knowledge" in json.dumps(prompt)
    assert event_id in json.dumps(prompt)


def test_mcp_rag_hides_withdrawn_knowledge_and_unknown_improvement(client):
    from tests.test_agent_rag_tools import publish_manual

    version_id = publish_manual(client)
    event_id = create_confirmed_event(client)["event_id"]
    params = {"name": "search_knowledge", "arguments": {"event_id": event_id, "query": "阀门"}}
    result = _mcp(client, "tools/call", params)
    assert result["result"]["isError"] is False, result
    assert version_id in json.dumps(result)
    client.post(
        f"/api/v1/knowledge/versions/{version_id}:withdraw", json={"reason": "撤回不再适用的规范"}
    ).raise_for_status()
    after = _mcp(client, "tools/call", params)
    assert after["result"]["isError"] is False, after
    assert version_id not in json.dumps(after)
    denied = _mcp(
        client,
        "tools/call",
        {
            "name": "get_evaluation_improvement",
            "arguments": {"improvement_id": str(uuid4())},
        },
    )
    assert denied["result"]["isError"] is True


def test_mcp_reviewed_experience_and_improvement_participant_boundary(client, settings):
    from tests.test_evaluation_improvements import agent_evaluation
    from tests.test_investigation_memory import approve, closed_order, consolidate, reviewer

    event_id, order_id = closed_order(client)
    draft = consolidate(client, order_id)
    headers = reviewer(client, settings)
    params = {"name": "search_experiences", "arguments": {"event_id": event_id, "query": "阀门"}}
    pending = _mcp(client, "tools/call", params)
    assert draft["id"] not in json.dumps(pending)
    approved = approve(client, draft, headers)
    result = _mcp(client, "tools/call", params)
    assert approved["id"] in json.dumps(result)
    client.post(
        f"/api/v1/experiences/{approved['id']}:withdraw",
        json={"expected_version": approved["version"], "reason": "撤回经验适用条件"},
        headers=headers,
    ).raise_for_status()
    assert approved["id"] not in json.dumps(_mcp(client, "tools/call", params))

    baseline = agent_evaluation(client, event_id)
    baseline.raise_for_status()
    improvement = client.post(
        "/api/v1/evaluation-improvements",
        json={
            "kind": "agent",
            "baseline_id": baseline.json()["id"],
            "title": "改进取证流程",
            "problem": "需要核查引用依据",
            "measures": "补充来源复核",
            "targets": {"category_accuracy": {"operator": "gte", "value": 1}},
        },
    )
    improvement.raise_for_status()
    params = {
        "name": "get_evaluation_improvement",
        "arguments": {"improvement_id": improvement.json()["id"]},
    }
    assert improvement.json()["id"] in json.dumps(_mcp(client, "tools/call", params))
    client.headers.update(headers)  # Same-organization reviewer is not a named participant here.
    denied = _mcp(client, "tools/call", params)
    assert denied["result"]["isError"] is True


def test_mcp_pending_observation_retries_and_human_confirmation(client):
    event = create_confirmed_event(client)["event_id"]
    version = client.get(f"/api/v1/events/{event}").json()["current_version_id"]
    content = "外部助手提交：现场补充观察，等待人工核对。"
    arguments = {
        "event_id": event,
        "expected_version_id": version,
        "content": content,
        "request_id": str(uuid4()),
    }

    def call():
        result = _mcp(
            client, "tools/call", {"name": "submit_case_observation", "arguments": arguments}
        )["result"]
        assert not result["isError"], result
        return json.loads(result["content"][0]["text"])

    first = call()
    second = call()
    assert first["id"] == second["id"] and first["status"] == "pending"
    client.headers["Idempotency-Key"] = "mcp-live-context"
    context = _mcp(
        client, "tools/call", {"name": "get_case_context", "arguments": {"event_id": event}}
    )
    assert content not in json.dumps(context, ensure_ascii=False)
    response = client.post(
        f"/api/v1/events/{event}/observations/{first['id']}:review",
        json={"expected_version": 1, "action": "confirm", "reason": "人工现场核查后确认"},
    )
    assert response.status_code in {200, 400}, response.text
    context = _mcp(
        client, "tools/call", {"name": "get_case_context", "arguments": {"event_id": event}}
    )
    assert content in json.dumps(context, ensure_ascii=False)
    replay = call()
    assert replay["status"] == "confirmed" and replay["version"] == 2
    conflict = _mcp(
        client,
        "tools/call",
        {
            "name": "submit_case_observation",
            "arguments": {**arguments, "content": "相同请求编号的不同材料"},
        },
    )
    assert conflict["result"]["isError"]
    stale = _mcp(
        client,
        "tools/call",
        {
            "name": "submit_case_observation",
            "arguments": {
                **arguments,
                "request_id": str(uuid4()),
                "expected_version_id": str(uuid4()),
            },
        },
    )
    assert stale["result"]["isError"]


def test_mcp_work_order_verification_resource_and_prompt(client):
    from tests.helpers import retest_measurement
    from tests.test_measured_work_order_outcomes import executing_order, resolve

    order = executing_order(client)
    assert resolve(client, order, 5, retest_measurement()).status_code == 200
    result = _mcp(
        client,
        "tools/call",
        {"name": "get_work_order_verification", "arguments": {"work_order_id": order}},
    )["result"]
    assert not result["isError"], result
    data = json.loads(result["content"][0]["text"])
    assert data["expected_version"] == 6 and data["current_resolution_id"]
    assert data["resolutions"][0]["measurements"][0]["before"] == 2
    assert not data["independent_reviewer"]
    resource = _mcp(
        client, "resources/read", {"uri": f"pipechina://work-orders/{order}/verification"}
    )
    assert "合成验收泄漏率" in json.dumps(resource, ensure_ascii=False)
    prompt = _mcp(
        client,
        "prompts/get",
        {"name": "work_order_acceptance", "arguments": {"work_order_id": order}},
    )
    assert "本次仍须实测" in json.dumps(prompt, ensure_ascii=False)
    denied = _mcp(
        client,
        "tools/call",
        {"name": "get_work_order_verification", "arguments": {"work_order_id": str(uuid4())}},
    )
    assert denied["result"]["isError"]
