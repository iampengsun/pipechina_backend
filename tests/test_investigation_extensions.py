"""Integrated skills, bounded branches, durable interruption and prompt search."""

import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.infrastructure.providers.fake import FakeTextLLMProvider
from app.modules.operation_event.application.context import evidence_view, role_context
from app.modules.operation_event.application.skills import catalog, load_selected
from app.modules.operation_event.domain.agent_models import AgentRun
from app.shared.errors import ConflictError
from tests.helpers import create_confirmed_event
from tests.test_evaluation_improvements import independent_reviewer
from tests.test_mcp_server import _mcp


def evaluation(client, event_id, *, split="development", candidate=None):
    r = client.post(
        f"/api/v1/events/{event_id}:classify",
        json={"prompt_candidate_id": candidate} if candidate else {},
    )
    assert r.status_code == 202, r.text
    run_id = r.json()["agent_run_id"]
    r = client.post(
        "/api/v1/agent-evaluations",
        json={
            "name": "提示词验证",
            "split": split,
            "cases": [
                {
                    "run_id": run_id,
                    "expected_category": "equipment_leak",
                    "expected_risk_level": "medium",
                    "relevant_evidence_ids": [],
                    "action_acceptable": True,
                    "unsafe_action": False,
                }
            ],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_skills_freeze_versions_and_reject_changed_or_unknown_package():
    entries = catalog()
    assert len(entries) == 3
    assert entries["valve-leak-investigation"].version == "1.1.0"
    assert all(
        s.version == "1.0.0" for name, s in entries.items() if name != "valve-leak-investigation"
    )
    snapshots = {name: s.digest for name, s in entries.items()}
    name = next(iter(entries))
    assert "instructions" in load_selected([name], snapshots)[0]
    with pytest.raises(ConflictError):
        load_selected(["../../arbitrary"])
    with pytest.raises(ConflictError):
        load_selected([name], {name: "stale"})


def test_context_retains_source_hash_and_readback_handle():
    item = SimpleNamespace(
        id=uuid4(),
        content={"text": "证据正文" * 10000},
        content_hash="abc",
        source_server="event-tools",
        source_version="1",
    )
    view = evidence_view([item], limit=2000)
    assert view[0]["content"]["offloaded"]
    assert view[0]["content"]["content_hash"] == "abc"
    assert len(json.dumps(view, ensure_ascii=False)) < 2000
    assert role_context("multi_agent_evidence", {"evidence": []}, [item])["evidence_sources"]


def test_parallel_branches_and_one_shared_budget(client, monkeypatch):
    original = FakeTextLLMProvider.generate_structured

    async def generate(self, **kwargs):
        if kwargs["operation"] == "multi_agent_coordinator":
            return kwargs["response_model"].model_validate(
                {
                    "roles": ["evidence", "risk", "plan", "critic"],
                    "focus": ["泄漏"],
                    "skills": ["valve-leak-investigation"],
                    "investigations": [
                        {"kind": "history", "query": "阀门"},
                        {"kind": "verification", "query": "阀门"},
                    ],
                }
            )
        return await original(self, **kwargs)

    monkeypatch.setattr(FakeTextLLMProvider, "generate_structured", generate)
    create_confirmed_event(client)
    event_id = create_confirmed_event(client)["event_id"]
    r = client.post(f"/api/v1/events/{event_id}:classify", json={"max_parallel": 2})
    assert r.status_code == 202, r.text
    run = client.get(f"/api/v1/agent-runs/{r.json()['agent_run_id']}").json()
    assert run["status"] == "succeeded"
    assert run["configuration"]["skills"][0]["name"] == "valve-leak-investigation"
    assert run["result"]["budget"]["tool_calls"] == 5
    assert len([t for t in run["tasks"] if t["role"].startswith("sub:")]) == 2


def test_human_pause_rejects_pending_then_resumes_same_run(client, monkeypatch):
    original = FakeTextLLMProvider.generate_structured
    paused = False

    async def generate(self, **kwargs):
        nonlocal paused
        if kwargs["operation"] == "multi_agent_evidence" and not paused:
            paused = True
            return kwargs["response_model"].model_validate(
                {
                    "action": "finish",
                    "query": "",
                    "questions": ["请提供现场实测压力"],
                    "unknowns": ["压力未知"],
                }
            )
        return await original(self, **kwargs)

    monkeypatch.setattr(FakeTextLLMProvider, "generate_structured", generate)
    event_id = create_confirmed_event(client)["event_id"]
    r = client.post(f"/api/v1/events/{event_id}:classify")
    assert r.status_code == 202, r.text
    run_id, job_id = r.json()["agent_run_id"], r.json()["job_id"]
    run = client.get(f"/api/v1/agent-runs/{run_id}").json()
    assert run["status"] == "waiting_input"
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "waiting_input"
    duplicate = client.post(f"/api/v1/events/{event_id}:classify")
    assert duplicate.json()["job_id"] == job_id
    version = client.get(f"/api/v1/events/{event_id}").json()["current_version_id"]
    note = client.post(
        f"/api/v1/events/{event_id}/observations",
        json={"expected_version_id": version, "content": "现场核验压力6MPa，周边已设置警戒"},
    ).json()
    payload = {
        "request_version": 1,
        "confirmed_observation_ids": [note["id"]],
        "reason": "现场人员已核实",
    }
    assert client.post(f"/api/v1/agent-runs/{run_id}:resume", json=payload).status_code == 409
    r = client.post(
        f"/api/v1/events/{event_id}/observations/{note['id']}:review",
        json={"expected_version": 1, "action": "confirm", "reason": "核实原始记录"},
    )
    assert r.status_code == 200, r.text
    r = client.post(f"/api/v1/agent-runs/{run_id}:resume", json=payload)
    assert r.status_code == 202, r.text
    assert r.json()["agent_run_id"] == run_id
    assert client.get(f"/api/v1/agent-runs/{run_id}").json()["status"] == "succeeded"
    assert client.post(f"/api/v1/agent-runs/{run_id}:resume", json=payload).status_code == 202


def test_mcp_task_negotiation_idempotency_cancellation_and_owner(client, settings):
    event_id = create_confirmed_event(client)["event_id"]
    arguments = {"event_id": event_id, "request_id": str(uuid4())}
    r = _mcp(client, "tools/call", {"name": "start_investigation", "arguments": arguments})
    assert r["error"]["code"] == -32021
    r = _mcp(
        client, "tools/call", {"name": "start_investigation", "arguments": arguments}, tasks=True
    )
    assert "result" in r, r
    task = r["result"]
    assert task["resultType"] == "task"
    identifier = task["taskId"]
    replay = _mcp(
        client, "tools/call", {"name": "start_investigation", "arguments": arguments}, tasks=True
    )
    assert replay["result"]["taskId"] == identifier
    assert (
        _mcp(client, "tasks/get", {"taskId": identifier}, tasks=True)["result"]["status"]
        == "working"
    )
    result = _mcp(client, "tasks/cancel", {"taskId": identifier}, tasks=True)
    assert result["result"]["resultType"] == "complete", result
    assert (
        _mcp(client, "tasks/get", {"taskId": identifier}, tasks=True)["result"]["status"]
        == "cancelled"
    )
    _, headers = independent_reviewer(client, settings)
    original = client.headers["Authorization"]
    client.headers.update(headers)
    assert _mcp(client, "tasks/get", {"taskId": identifier}, tasks=True)["error"]["code"] == -32602
    client.headers["Authorization"] = original


def test_prompt_search_and_held_out_publication(client, settings):
    dev_event = create_confirmed_event(client)["event_id"]
    dev = evaluation(client, dev_event)
    r = client.post(
        "/api/v1/prompt-optimizations", json={"evaluation_id": dev["id"], "generations": 1}
    )
    assert r.status_code == 202, r.text
    opt = r.json()
    assert opt["status"] == "succeeded", opt
    assert len(opt["results"]["candidates"]) == 2
    assert opt["results"]["candidates"][1]["reflection"]["lessons"]
    assert opt["results"]["usage"]["model_calls"] == 13
    assert (
        client.post(
            f"/api/v1/admin/prompt-templates/{opt['candidate_id']}:activate",
            json={"reason": "测试不能绕过独立验收"},
        ).status_code
        == 409
    )
    held_event = create_confirmed_event(client)["event_id"]
    baseline = evaluation(client, held_event, split="held_out")
    forbidden = client.post("/api/v1/prompt-optimizations", json={"evaluation_id": baseline["id"]})
    assert forbidden.status_code == 409
    reviewer_id, reviewer_headers = independent_reviewer(client, settings)
    r = client.post(
        "/api/v1/evaluation-improvements",
        json={
            "kind": "agent",
            "reviewer_id": reviewer_id,
            "baseline_id": baseline["id"],
            "title": "调查提示词优化",
            "problem": "规范适用性提示不足",
            "measures": "轨迹反思与候选比较",
            "targets": {"category_accuracy": {"operator": "gte", "value": 1}},
        },
    )
    assert r.status_code == 201, r.text
    improvement = r.json()
    url = f"/api/v1/evaluation-improvements/{improvement['id']}"
    publish_url = f"/api/v1/prompt-optimizations/{opt['id']}:publish"
    body = {"improvement_id": improvement["id"], "reason": "独立复测与人工审核已通过"}
    assert client.post(publish_url, json=body).status_code == 409
    r = client.post(
        url + ":implement",
        json={
            "expected_version": 1,
            "changes": "固定优化候选用于复测",
            "revision": opt["candidate_id"],
        },
    )
    assert r.status_code == 200, r.text
    retest = evaluation(client, held_event, split="held_out", candidate=opt["candidate_id"])
    r = client.post(url + ":retest", json={"expected_version": 2, "evaluation_id": retest["id"]})
    assert r.status_code == 200, r.text
    r = client.post(
        url + ":accept",
        headers=reviewer_headers,
        json={
            "expected_version": 3,
            "approved": True,
            "reason": "独立核查分类风险和处置建议，验收通过",
        },
    )
    assert r.status_code == 200, r.text
    r = client.post(publish_url, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["published_improvement_id"] == improvement["id"]
    previous = r.json()["results"]["publication"]["previous_prompt_ids"]
    assert previous
    rollback = client.post(
        f"/api/v1/admin/prompt-templates/{previous[0]}:activate",
        json={"reason": "核验原版本可以审计回退"},
    )
    assert rollback.status_code == 200, rollback.text
    assert rollback.json()["active"]
    retained = client.get(f"/api/v1/prompt-optimizations/{opt['id']}").json()
    assert retained["published_improvement_id"] == improvement["id"]
    assert retained["results"]["publication"]["candidate_id"] == opt["candidate_id"]


def test_parallel_rag_model_calls_share_usage_and_budget(client, monkeypatch):
    from app.modules.knowledge.domain.schemas import QueryPlan
    from app.modules.operation_event.application import investigation_tools
    from app.modules.operation_event.domain.agent_schemas import CoordinatorPlan

    original = FakeTextLLMProvider.generate_structured

    async def generate(self, **kwargs):
        if kwargs["operation"] == "multi_agent_coordinator":
            return CoordinatorPlan(
                roles=["evidence", "risk", "plan", "critic"],
                investigations=[
                    {"kind": "knowledge", "query": "阀门"},
                    {"kind": "knowledge", "query": "压力"},
                ],
            )
        return await original(self, **kwargs)

    async def retrieve(session, user, payload, knowledge, text):
        await text.generate_structured(
            operation="knowledge_query_plan",
            system_prompt="检索查询计划",
            user_prompt=json.dumps({"question": payload.question}),
            response_model=QueryPlan,
        )
        return [], {}

    monkeypatch.setattr(FakeTextLLMProvider, "generate_structured", generate)
    monkeypatch.setattr(investigation_tools, "retrieve", retrieve)
    event_id = create_confirmed_event(client)["event_id"]
    result = client.post(f"/api/v1/events/{event_id}:classify")
    assert result.status_code == 202, result.text
    run = client.get(f"/api/v1/agent-runs/{result.json()['agent_run_id']}").json()
    assert run["result"]["budget"]["model_calls"] == 12
    # The overridden coordinator reports no usage; other calls use 20 fake tokens.
    assert run["usage"]["total_tokens"] == 220


def test_mcp_task_input_update_and_final_result(client, monkeypatch):
    from app.modules.operation_event.application.service import execute_event_classification
    from app.modules.operation_event.domain.models import ProductionEvent, ProductionEventVersion
    from app.shared.platform.models import AsyncJob
    from app.shared.worker_runtime import job_user

    original = FakeTextLLMProvider.generate_structured
    paused = False

    async def generate(self, **kwargs):
        nonlocal paused
        if kwargs["operation"] == "multi_agent_evidence" and not paused:
            paused = True
            return kwargs["response_model"].model_validate(
                {
                    "action": "finish",
                    "query": "",
                    "questions": ["补充现场压力"],
                    "unknowns": ["未测量"],
                }
            )
        return await original(self, **kwargs)

    monkeypatch.setattr(FakeTextLLMProvider, "generate_structured", generate)
    event_id = create_confirmed_event(client)["event_id"]
    created = _mcp(
        client,
        "tools/call",
        {
            "name": "start_investigation",
            "arguments": {"event_id": event_id, "request_id": str(uuid4())},
        },
        tasks=True,
    )
    identifier = created["result"]["taskId"]

    async def worker():
        async with client.app.state.database.session_factory() as session:
            run = await session.get(AgentRun, UUID(identifier))
            job = await session.get(AsyncJob, run.job_id)
            await execute_event_classification(
                session,
                job=job,
                event=await session.get(ProductionEvent, run.event_id),
                version=await session.get(ProductionEventVersion, run.event_version_id),
                user=await job_user(session, job),
                provider=client.app.state.providers.text,
                knowledge=client.app.state.knowledge,
            )

    client.portal.call(worker)
    state = _mcp(client, "tasks/get", {"taskId": identifier}, tasks=True)["result"]
    assert state["status"] == "input_required"
    key = next(iter(state["inputRequests"]))
    version = client.get(f"/api/v1/events/{event_id}").json()["current_version_id"]
    note = client.post(
        f"/api/v1/events/{event_id}/observations",
        json={"expected_version_id": version, "content": "压力实测6MPa，现场已警戒"},
    ).json()
    response = {
        "taskId": identifier,
        "inputResponses": {
            key: {
                "action": "accept",
                "content": {"confirmed_observation_ids": note["id"], "reason": "现场已核实"},
            }
        },
    }
    assert _mcp(client, "tasks/update", response, tasks=True)["error"]["code"] == -32000
    client.post(
        f"/api/v1/events/{event_id}/observations/{note['id']}:review",
        json={"expected_version": 1, "action": "confirm", "reason": "现场实测确认"},
    ).raise_for_status()
    updated = _mcp(client, "tasks/update", response, tasks=True)
    assert updated["result"]["resultType"] == "complete", updated
    client.portal.call(worker)
    state = _mcp(client, "tasks/get", {"taskId": identifier}, tasks=True)["result"]
    assert state["status"] == "completed"
    assert state["result"]["isError"] is False
    assert state["result"]["structuredContent"]["agent_run_id"] == identifier
    # Replaying a response after completion acknowledges without creating another run.
    assert _mcp(client, "tasks/update", response, tasks=True)["result"]["resultType"] == "complete"


def test_generic_job_cancel_finishes_waiting_investigation(client, monkeypatch):
    original = FakeTextLLMProvider.generate_structured

    async def generate(self, **kwargs):
        if kwargs["operation"] == "multi_agent_evidence":
            return kwargs["response_model"].model_validate(
                {"action": "finish", "query": "", "questions": ["请提供现场压力"], "unknowns": []}
            )
        return await original(self, **kwargs)

    monkeypatch.setattr(FakeTextLLMProvider, "generate_structured", generate)
    event_id = create_confirmed_event(client)["event_id"]
    run = client.post(f"/api/v1/events/{event_id}:classify").json()
    result = client.post(f"/api/v1/jobs/{run['job_id']}:cancel")
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "cancelled"
    assert client.get(f"/api/v1/agent-runs/{run['agent_run_id']}").json()["status"] == "cancelled"
    assert (
        client.post(
            f"/api/v1/agent-runs/{run['agent_run_id']}:resume",
            json={
                "request_version": 1,
                "confirmed_observation_ids": [str(uuid4())],
                "reason": "已取消任务不能恢复",
            },
        ).status_code
        == 409
    )


def test_unset_provider_output_limit_is_bounded_without_mutating_shared_provider():
    from app.modules.operation_event.application.budget import bounded_provider
    from app.modules.operation_event.domain.agent_schemas import PlanReport

    provider = SimpleNamespace(name="qwen", model="model", max_tokens=None)
    run = SimpleNamespace(
        config_snapshot={"limits": {"max_tokens": 12000}},
        input_tokens=0,
        output_tokens=0,
        cost_microusd=0,
    )
    bounded = bounded_provider(provider, run, "系统指令", "事件内容", PlanReport)
    assert 128 <= bounded.max_tokens < 12000
    assert provider.max_tokens is None
    with pytest.raises(ConflictError):
        bounded_provider(provider, run, "系统指令", "X" * 20000, PlanReport)


def test_role_context_uses_latest_source_and_replays_at_recorded_time():
    from datetime import UTC, datetime, timedelta

    from app.modules.operation_event.application.context import current_evidence

    now = datetime.now(UTC)

    def source(minutes):
        return SimpleNamespace(
            id=uuid4(),
            source_server="site-context",
            source_version="1",
            content_hash="digest",
            content={
                "operation": "get_weather_alerts",
                "event_id": "event",
                "location_refs": [{"station_id": "station"}],
                "status": "ok",
                "retrieved_at": (now + timedelta(minutes=minutes)).isoformat(),
                "valid_until": (now + timedelta(minutes=minutes + 5)).isoformat(),
            },
        )

    old, new = source(-6), source(-1)
    assert current_evidence([old, new]) == [new]
    assert current_evidence([old, new], as_of=now - timedelta(minutes=5)) == [old]
    payload = {"evidence": {"evidence_ids": [str(old.id), str(new.id)], "facts": []}}
    view = role_context("multi_agent_risk", payload, [old, new])
    assert view["evidence"]["evidence_ids"] == [str(new.id)]
