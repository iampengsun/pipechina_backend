"""Local HTTP acceptance using audited manual fixtures; does not invoke or assess an LLM."""

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx

from app.bootstrap.config import get_settings
from dev.acceptance_fixtures import create_synthetic_order


async def accept(output):
    if get_settings().app_env != "development":
        raise ValueError("this acceptance creates explicit local development fixtures only")
    report = {
        "provenance": "synthetic_manual_fixture_no_model",
        "expert_validated": False,
        "started_at": datetime.now(UTC).isoformat(),
        "steps": [],
    }
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8000", timeout=60, trust_env=False
    ) as http:

        async def login(username):
            response = await http.post(
                "http://127.0.0.1:9001/token",
                data={"username": username, "password": "123456", "grant_type": "password"},
            )
            response.raise_for_status()
            return {"Authorization": "Bearer " + response.json()["access_token"]}

        admin = await login("admin")
        reviewer = await login("acceptance")

        async def call(method, path, payload=None, headers=None):
            response = await http.request(method, path, headers=headers or admin, json=payload)
            response.raise_for_status()
            return response.json()

        try:
            user = (await call("GET", "/api/v1/auth/me"))["user_id"]
            marker = "SYN-GRAPH-" + uuid4().hex[:10]
            station = await call(
                "POST", "/api/v1/stations", {"code": marker, "name": "[合成验收]图谱测试站"}
            )
            equipment = await call(
                "POST",
                "/api/v1/equipment",
                {
                    "station_id": station["id"],
                    "code": "V-1",
                    "name": "[合成验收]测试阀",
                    "equipment_type": "synthetic",
                },
            )
            failure = await call(
                "POST", "/api/v1/failure-modes", {"code": marker, "name": "[合成验收]密封测试故障"}
            )
            fixtures = []
            for index in range(2):
                fixture = await create_synthetic_order(user)
                fixtures.append(fixture)
                event = fixture["event_id"]
                order = fixture["work_order_id"]
                await call("POST", f"/api/v1/events/{event}:confirm")
                await call("POST", "/api/v1/operational-graph:sync")
                trace = await call(
                    "GET", f"/api/v1/operational-graph/trace?kind=event&record_id={event}&depth=6"
                )
                root = next(n for n in trace["nodes"] if n["key"] == trace["root"])
                await call(
                    "POST",
                    "/api/v1/equipment-associations",
                    {
                        "equipment_id": equipment["id"],
                        "failure_mode_id": failure["id"],
                        "record_kind": "event",
                        "record_id": event,
                        "source_revision": root["binding_revision"],
                        "reason": "合成验收：人工核对固定设备与故障",
                    },
                )
                if index == 0:
                    order_node = next(
                        n for n in trace["nodes"] if n["key"] == f"work_order:{order}"
                    )
                    await call(
                        "POST",
                        "/api/v1/equipment-associations",
                        {
                            "equipment_id": equipment["id"],
                            "failure_mode_id": failure["id"],
                            "record_kind": "work_order",
                            "record_id": order,
                            "source_revision": order_node["binding_revision"],
                            "reason": "合成验收：确认本工单措施对应此设备及故障",
                        },
                    )
                if index == 0:
                    actions = [
                        ("submit-review", {}),
                        ("review", {"approved": True}),
                        ("dispatch", {"assignee_id": user}),
                        ("start", {}),
                    ]
                    for version, (action, extra) in enumerate(actions, 1):
                        await call(
                            "POST",
                            f"/api/v1/work-orders/{order}:{action}",
                            {
                                "expected_version": version,
                                "reason": "合成软件验收，不授权现场操作",
                                **extra,
                            },
                        )
                    measurement = {
                        "metric": "合成泄漏率",
                        "unit": "L/min",
                        "before": 2,
                        "after": 0,
                        "maximum": 0.1,
                        "criterion": "仅合成软件测试，不是现场标准",
                        "measured_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                    }
                    await call(
                        "POST",
                        f"/api/v1/work-orders/{order}:resolve",
                        {
                            "expected_version": 5,
                            "reason": "合成软件复测",
                            "action": "合成更换测试密封件",
                            "result": "合成读数通过",
                            "measurements": [measurement],
                        },
                    )
                    resolution = (await call("GET", f"/api/v1/work-orders/{order}/resolutions"))[0]
                    await call(
                        "POST",
                        f"/api/v1/work-orders/{order}:close",
                        {
                            "expected_version": 6,
                            "reason": "独立人员核验合成读数",
                            "resolution_id": resolution["id"],
                            "measurements": [measurement],
                        },
                        reviewer,
                    )
            # Leave the last association for Beat/Worker; do not invoke a manual final sync.
            deadline = time.monotonic() + 120
            while True:
                state = await call("GET", "/api/v1/operational-graph/status")
                if not state["pending_changes"]:
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError("automatic graph synchronization timed out")
                await asyncio.sleep(2)
            data = await call(
                "GET",
                f"/api/v1/equipment/{equipment['id']}/recurrences?failure_mode_id={failure['id']}",
            )
            assert data["visible_episode_count"] == 2 and data["projection"]["complete"]
            measured = [o for e in data["episodes"] for o in e["orders"] if o["measured_accepted"]]
            assert len(measured) == 1 and measured[0]["days_to_next_record"] is not None

            async def mcp(method, params):
                headers = {
                    **admin,
                    "MCP-Protocol-Version": "2026-07-28",
                    "Mcp-Method": method,
                    "Accept": "application/json, text/event-stream",
                }
                if method == "tools/call":
                    headers["Mcp-Name"] = params["name"]
                response = await http.post(
                    "/mcp/",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": method,
                        "params": {
                            **params,
                            "_meta": {
                                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                                "io.modelcontextprotocol/clientCapabilities": {},
                            },
                        },
                    },
                )
                response.raise_for_status()
                body = response.json()
                assert "error" not in body and not body["result"].get("isError"), body
                return body["result"]

            listed = await mcp("tools/list", {})
            assert len(listed["tools"]) == 18
            for name in ["trace_case_relations", "find_recurrent_hazards", "assess_change_impact"]:
                await mcp(
                    "tools/call",
                    {"name": name, "arguments": {"event_id": fixtures[-1]["event_id"]}},
                )
            report["mcp_tools"] = len(listed["tools"])
            report["mcp_read_tools"] = sum(
                tool["annotations"]["readOnlyHint"] for tool in listed["tools"]
            )
            event = fixtures[-1]["event_id"]
            note_text = "[合成验收] 外部助手补充观察，等待现场人工确认。"
            arguments = {
                "event_id": event,
                "expected_version_id": fixtures[-1]["event_version_id"],
                "content": note_text,
                "request_id": str(uuid4()),
            }
            first = await mcp(
                "tools/call", {"name": "submit_case_observation", "arguments": arguments}
            )
            replay = await mcp(
                "tools/call", {"name": "submit_case_observation", "arguments": arguments}
            )
            note = json.loads(first["content"][0]["text"])
            assert (
                json.loads(replay["content"][0]["text"])["id"] == note["id"]
                and note["status"] == "pending"
            )
            context = await mcp(
                "tools/call", {"name": "get_case_context", "arguments": {"event_id": event}}
            )
            assert note_text not in json.dumps(context, ensure_ascii=False)
            await call(
                "POST",
                f"/api/v1/events/{event}/observations/{note['id']}:review",
                {"expected_version": 1, "action": "confirm", "reason": "人工核对合成观察"},
            )
            context = await mcp(
                "tools/call", {"name": "get_case_context", "arguments": {"event_id": event}}
            )
            assert note_text in json.dumps(context, ensure_ascii=False)
            await mcp(
                "tools/call",
                {
                    "name": "get_work_order_verification",
                    "arguments": {"work_order_id": fixtures[0]["work_order_id"]},
                },
            )
            report["mcp_observation"] = {
                "id": note["id"],
                "duplicate_replay_same_id": True,
                "pending_excluded_from_facts": True,
                "human_confirmation_visible": True,
            }
            report.update(
                status="passed",
                equipment_id=equipment["id"],
                failure_mode_id=failure["id"],
                fixtures=fixtures,
                recurrence=data,
                projection=state,
            )
            report["steps"] = [
                "authenticated_mcp_discovery_and_three_graph_tool_calls",
                "authentication",
                "manual_equipment_confirmation",
                "two_confirmed_episodes",
                "measured_independent_acceptance",
                "automatic_beat_worker_projection",
                "recurrence_with_before_after_values",
            ]
        except Exception as exc:
            report.update(status="failed", error_type=type(exc).__name__)
            raise
        finally:
            report["finished_at"] = datetime.now(UTC).isoformat()
            await asyncio.to_thread(Path(output).parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(
                Path(output).write_text,
                json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
            )
    print(json.dumps({"status": report["status"], "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="dev/acceptance/20261110-business-graph-live.json")
    asyncio.run(accept(parser.parse_args().output))
