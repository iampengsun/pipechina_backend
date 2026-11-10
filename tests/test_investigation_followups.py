"""Multi-step investigation, partial progress, and real GEPA engine integration."""

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import select

from app.infrastructure.providers.fake import FakeTextLLMProvider
from app.modules.operation_event.application.prompt_search import paired_comparison, search
from app.modules.operation_event.domain.agent_models import AgentRun
from app.modules.operation_event.domain.optimization_schemas import Reflection
from tests.helpers import create_confirmed_event


def branch_provider(monkeypatch, decide, *, plans=None):
    original = FakeTextLLMProvider.generate_structured

    async def generate(self, **kwargs):
        if kwargs["operation"] == "multi_agent_coordinator":
            return kwargs["response_model"].model_validate(
                {
                    "roles": ["evidence", "risk", "plan", "critic"],
                    "investigations": plans or [{"kind": "history", "query": "阀门"}],
                }
            )
        if kwargs["operation"] == "multi_agent_subinvestigation":
            import json

            payload = json.loads(kwargs["user_prompt"])
            return kwargs["response_model"].model_validate(decide(payload))
        return await original(self, **kwargs)

    monkeypatch.setattr(FakeTextLLMProvider, "generate_structured", generate)


def finish(payload):
    return {
        "action": "finish",
        "query": "",
        "complete": True,
        "conclusion": "历史事实已核对，供主调查复核。",
        "evidence_ids": [e["evidence_id"] for e in payload["evidence_sources"]],
    }


def classify(client, *, expect_failure=False, **options):
    create_confirmed_event(client)
    event_id = create_confirmed_event(client)["event_id"]
    response = client.post(f"/api/v1/events/{event_id}:classify", json=options)
    assert response.status_code == (409 if expect_failure else 202), response.text
    if expect_failure:

        async def identifier():
            async with client.app.state.database.session_factory() as session:
                return await session.scalar(
                    select(AgentRun.id).where(AgentRun.event_id == UUID(event_id))
                )

        run_id = client.portal.call(identifier)
    else:
        run_id = response.json()["agent_run_id"]
    run = client.get(f"/api/v1/agent-runs/{run_id}").json()
    return event_id, run


def test_branch_changes_tool_and_preserves_auditable_steps(client, monkeypatch):
    def decide(payload):
        if len(payload["history"]["queries"]) == 1:
            return {"action": "search_work_orders", "query": "阀门"}
        return finish(payload)

    branch_provider(monkeypatch, decide)
    _, run = classify(client)
    assert run["status"] == "succeeded"
    branch = next(t for t in run["tasks"] if t["role"].startswith("sub:"))
    output = branch["output"]
    assert [q["tool"] for q in output["queries"]] == ["search_events", "search_work_orders"]
    assert output["decisions"] == 2
    assert output["cited_evidence_ids"]
    assert branch["step_count"] == 4


@pytest.mark.parametrize("max_steps, reason", [(1, "step_limit"), (4, "repeated_query")])
def test_branch_stops_at_local_limits_without_losing_main_run(
    client, monkeypatch, max_steps, reason
):
    branch_provider(monkeypatch, lambda p: {"action": "search_events", "query": "阀门"})
    _, run = classify(client, max_branch_steps=max_steps)
    assert run["status"] == "succeeded"
    branch = next(t for t in run["tasks"] if t["role"].startswith("sub:"))
    assert branch["output"]["stop_reason"] == reason
    assert len(branch["output"]["queries"]) == 1
    assert (
        "子问题未完成：阀门"
        in next(t for t in run["tasks"] if t["role"] == "risk")["output"]["unknowns"]
    )


def test_optional_timeout_preserves_sibling_results(client, monkeypatch):
    from app.modules.operation_event.application.investigation_tools import (
        InvestigationToolRegistry,
    )

    original = InvestigationToolRegistry.call

    async def call(self, session, **kwargs):
        if kwargs["task"].role.startswith("sub:history"):
            raise TimeoutError
        return await original(self, session, **kwargs)

    monkeypatch.setattr(InvestigationToolRegistry, "call", call)
    branch_provider(
        monkeypatch,
        finish,
        plans=[{"kind": "history", "query": "阀门"}, {"kind": "verification", "query": "阀门"}],
    )
    _, run = classify(client)
    assert run["status"] == "succeeded"
    branches = [t["output"] for t in run["tasks"] if t["role"].startswith("sub:")]
    by_kind = {b["kind"]: b for b in branches}
    assert by_kind["history"]["stop_reason"] == "timeout"
    assert by_kind["verification"]["decisions"] == 1
    assert next(t for t in run["tasks"] if t["role"] == "risk")["output"]["unknowns"]


def test_branch_human_question_resumes_a_new_round(client, monkeypatch):
    def decide(payload):
        if not payload["case_memory"].get("observations"):
            return {
                "action": "finish",
                "query": "",
                "questions": ["请核实现场压力"],
                "unknowns": ["现场压力未知"],
            }
        return finish(payload)

    branch_provider(monkeypatch, decide)
    event_id, run = classify(client)
    assert run["status"] == "waiting_input"
    version = client.get(f"/api/v1/events/{event_id}").json()["current_version_id"]
    note = client.post(
        f"/api/v1/events/{event_id}/observations",
        json={"expected_version_id": version, "content": "现场实测压力6MPa，已设置警戒。"},
    ).json()
    client.post(
        f"/api/v1/events/{event_id}/observations/{note['id']}:review",
        json={"expected_version": 1, "action": "confirm", "reason": "核实现场实测记录"},
    ).raise_for_status()
    response = client.post(
        f"/api/v1/agent-runs/{run['id']}:resume",
        json={
            "request_version": 1,
            "confirmed_observation_ids": [note["id"]],
            "reason": "现场读数经人工确认",
        },
    )
    assert response.status_code == 202, response.text
    resumed = client.get(f"/api/v1/agent-runs/{run['id']}").json()
    assert resumed["status"] == "succeeded"
    branches = [t for t in resumed["tasks"] if t["role"].startswith("sub:")]
    assert len(branches) == 2
    assert {t["role"].rsplit(":", 1)[1] for t in branches} == {"0", "1"}


async def test_official_gepa_selects_measured_candidate_with_paired_repeats():
    calls, saved = [], []

    async def evaluate(text, case):
        calls.append((text, case))
        return {
            "run_id": str(case),
            "metrics": {
                "category_correct": text != "baseline",
                "risk_correct": True,
                "evidence_f1": 1,
                "risk_underestimated": False,
            },
            "invalid_references": [],
        }

    async def reflect(candidate, dataset):
        assert dataset["system_prompt"][0]["trajectory"]["repeats"]
        return Reflection(
            lessons=["核对设备类别"], instructions="根据证据核对设备类别，未知事项保持待确认。"
        )

    async def save(candidates):
        saved.append(candidates)

    result = await search(
        seed="baseline",
        case_ids=[0, 1],
        repetitions=2,
        generations=1,
        evaluate_case=evaluate,
        reflect=reflect,
        save=save,
    )
    assert result["algorithm"] == "gepa"
    assert result["library_version"] == "0.1.4"
    assert result["selected"] == 1
    assert len(calls) == 8  # Parent trace reuse must not repeat paid calls.
    assert result["engine"]["parents"][1] == [0]
    comparison = result["candidates"][1]["comparison"]
    assert comparison["safety_proxy_passed"] and not comparison["expert_safety_verified"]
    assert all(c["delta"]["category"] == 1 for c in comparison["cases"])


def test_pairwise_gate_catches_single_case_regression_despite_average_gain():
    old = {
        "outcomes": [
            {
                "run_id": "a",
                "objectives": {"risk_not_underestimated": 1.0, "valid_references": 1.0},
            },
            {
                "run_id": "b",
                "objectives": {"risk_not_underestimated": 0.0, "valid_references": 1.0},
            },
        ]
    }
    new = {
        "outcomes": [
            {
                "run_id": "a",
                "objectives": {"risk_not_underestimated": 0.0, "valid_references": 1.0},
            },
            {
                "run_id": "b",
                "objectives": {"risk_not_underestimated": 1.0, "valid_references": 1.0},
            },
        ]
    }
    assert not paired_comparison(old, new)["safety_proxy_passed"]


async def test_gepa_cancellation_waits_for_adapter_to_stop():
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def evaluate(text, case):
        calls.append(case)
        entered.set()
        await release.wait()
        return {
            "run_id": str(case),
            "metrics": {
                "category_correct": True,
                "risk_correct": True,
                "evidence_f1": 1,
                "risk_underestimated": False,
            },
        }

    async def unused(*args):
        return None

    task = asyncio.create_task(
        search(
            seed="baseline",
            case_ids=[0, 1],
            repetitions=1,
            generations=1,
            evaluate_case=evaluate,
            reflect=unused,
            save=unused,
        )
    )
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == [0]


def test_global_tool_budget_stops_followup_and_keeps_saved_evidence(client, monkeypatch):
    branch_provider(monkeypatch, lambda p: {"action": "search_work_orders", "query": "阀门"})
    _, run = classify(client, expect_failure=True, max_tool_calls=1)
    assert run["status"] == "failed"
    assert run["result"]["budget"]["tool_calls"] == 1
    assert client.get(f"/api/v1/agent-runs/{run['id']}/evidence").json()


def test_branch_rejects_tool_outside_its_investigation_scope(client, monkeypatch):
    branch_provider(monkeypatch, lambda p: {"action": "get_weather_alerts", "query": "阀门"})
    _, run = classify(client, expect_failure=True)
    assert run["status"] == "failed"
    branch = next(t for t in run["tasks"] if t["role"].startswith("sub:"))
    assert len(branch["output"]["queries"]) == 1


def test_optimization_preserves_unknown_usage_in_budget(client, monkeypatch):
    from app.modules.operation_event.domain.agent_models import PromptOptimization
    from app.ports.text import TextUsage, set_text_usage
    from tests.test_investigation_extensions import evaluation

    dev = evaluation(client, create_confirmed_event(client)["event_id"])
    original = FakeTextLLMProvider.generate_structured

    async def generate(self, **kwargs):
        result = await original(self, **kwargs)
        set_text_usage(TextUsage())
        return result

    monkeypatch.setattr(FakeTextLLMProvider, "generate_structured", generate)
    response = client.post(
        "/api/v1/prompt-optimizations",
        json={"evaluation_id": dev["id"], "max_tokens": 10000, "generations": 1},
    )
    assert response.status_code == 409, response.text

    async def read():
        async with client.app.state.database.session_factory() as session:
            return await session.scalar(select(PromptOptimization))

    row = client.portal.call(read)
    assert row.results["usage"]["estimated_tokens"] > 0
    assert row.results["usage"]["model_calls"] < 13
    assert row.candidate_id is None


def test_optimization_cancellation_stops_before_next_role(client, monkeypatch):
    from app.modules.operation_event.domain.agent_models import PromptOptimization
    from app.shared.platform.models import AsyncJob
    from tests.test_investigation_extensions import evaluation

    dev = evaluation(client, create_confirmed_event(client)["event_id"])
    original = FakeTextLLMProvider.generate_structured
    calls = []

    async def generate(self, **kwargs):
        calls.append(kwargs["operation"])
        async with client.app.state.database.session_factory() as session:
            row = await session.scalar(select(PromptOptimization))
            job = await session.get(AsyncJob, row.job_id)
            job.cancel_requested = True
            await session.commit()
        return await original(self, **kwargs)

    monkeypatch.setattr(FakeTextLLMProvider, "generate_structured", generate)
    response = client.post("/api/v1/prompt-optimizations", json={"evaluation_id": dev["id"]})
    assert response.status_code == 409, response.text

    async def read():
        async with client.app.state.database.session_factory() as session:
            row = await session.scalar(select(PromptOptimization))
            job = await session.get(AsyncJob, row.job_id)
            return row.candidate_id, job.status

    assert client.portal.call(read) == (None, "cancelled")
    assert calls == ["multi_agent_risk"]
