import json
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.infrastructure.providers.fake import FakeTextLLMProvider
from app.modules.operation_event.application.investigation_tools import (
    InvestigationTool,
    InvestigationToolRegistry,
)
from app.modules.operation_event.application.service import execute_event_classification
from app.modules.operation_event.domain.agent_models import AgentRun, AgentTask
from app.modules.operation_event.domain.models import ProductionEvent, ProductionEventVersion
from app.shared.errors import ConflictError
from app.shared.platform.models import AsyncJob
from app.shared.worker_runtime import job_user
from tests.helpers import create_confirmed_event


def test_mcp_registry_rejects_write_tools() -> None:
    async def handler(query: str) -> list[dict]:
        return []

    registry = InvestigationToolRegistry()
    with pytest.raises(ConflictError, match="write investigation tools"):
        registry.register(
            InvestigationTool(
                server="work-order-mcp", name="create", read_only=False, handler=handler
            )
        )


@pytest.mark.asyncio
async def test_mcp_registry_rejects_unknown_tools() -> None:
    run = AgentRun(id=uuid4(), organization_unit_id=uuid4())
    task = AgentTask(id=uuid4(), run_id=run.id, role="evidence", goal="test")
    with pytest.raises(ConflictError, match="not allowed"):
        await InvestigationToolRegistry().call(
            AsyncMock(), run=run, task=task, tool_name="shell", query="ignored"
        )


@pytest.mark.parametrize("first_decision", ["needs_revision", "insufficient_evidence"])
def test_investigation_revises_after_critic_feedback(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, first_decision: str
) -> None:
    original = FakeTextLLMProvider.generate_structured
    calls = {"critic": 0, "evidence": 0, "plan": 0}

    async def generate(self, *, operation, system_prompt, user_prompt, response_model):
        if operation == "multi_agent_critic":
            calls["critic"] += 1
            if calls["critic"] == 1:
                payload = json.loads(user_prompt)
                assert payload["evidence_sources"]
                assert all(source["content"] for source in payload["evidence_sources"])
                assert {source["evidence_id"] for source in payload["evidence_sources"]} == set(
                    payload["valid_evidence"]
                )
                assert "二号" in json.dumps(payload["evidence_sources"], ensure_ascii=False)
                return response_model.model_validate(
                    {
                        "decision": first_decision,
                        "issues": ["补充阀门相关记录"],
                        "conflicts": [],
                        "evidence_ids": payload["valid_evidence"],
                    }
                )
        if operation == "multi_agent_plan":
            calls["plan"] += 1
            if calls["plan"] == 2:
                assert json.loads(user_prompt)["review_issues"] == ["补充阀门相关记录"]
        if operation == "multi_agent_evidence":
            calls["evidence"] += 1
            if calls["evidence"] == 1 and first_decision == "insufficient_evidence":
                return response_model.model_validate(
                    {"action": "search_events", "query": "不存在", "unknowns": []}
                )
            if calls["evidence"] == 5 and first_decision == "insufficient_evidence":
                assert json.loads(user_prompt)["review_issues"] == ["补充阀门相关记录"]
                return response_model.model_validate(
                    {"action": "search_events", "query": "二号", "unknowns": []}
                )
        return await original(
            self,
            operation=operation,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_model=response_model,
        )

    monkeypatch.setattr(FakeTextLLMProvider, "generate_structured", generate)
    if first_decision == "insufficient_evidence":
        create_confirmed_event(client)
    event_id = create_confirmed_event(client)["event_id"]
    classified = client.post(f"/api/v1/events/{event_id}:classify")
    assert classified.status_code == 202, classified.text
    workflow = client.get(f"/api/v1/workflows/{classified.json()['workflow_id']}").json()
    run_id = workflow["state"]["agent"]["run_id"]
    run = client.get(f"/api/v1/agent-runs/{run_id}").json()
    assert run["status"] == "succeeded"
    assert calls["critic"] == 2
    assert calls["plan"] == 2
    assert calls["evidence"] == (6 if first_decision == "insufficient_evidence" else 4)
    quality = client.get(f"/api/v1/agent-runs/{run_id}/quality").json()
    assert quality["details"]["revision_count"] == 1


def test_investigation_stops_after_two_revisions(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = FakeTextLLMProvider.generate_structured
    critic_calls = 0

    async def generate(self, *, operation, system_prompt, user_prompt, response_model):
        nonlocal critic_calls
        if operation == "multi_agent_critic":
            critic_calls += 1
            payload = json.loads(user_prompt)
            return response_model.model_validate(
                {
                    "decision": "needs_revision",
                    "issues": ["建议仍需修订"],
                    "conflicts": [],
                    "evidence_ids": payload["valid_evidence"],
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
    event_id = create_confirmed_event(client)["event_id"]
    response = client.post(f"/api/v1/events/{event_id}:classify")
    assert response.status_code == 409
    assert "multi-agent proposal requires more evidence" in response.json()["message"]
    assert critic_calls == 3


def test_investigation_rejects_empty_evidence_retry(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = FakeTextLLMProvider.generate_structured

    async def generate(self, *, operation, system_prompt, user_prompt, response_model):
        if operation == "multi_agent_critic":
            payload = json.loads(user_prompt)
            return response_model.model_validate(
                {
                    "decision": "insufficient_evidence",
                    "issues": ["缺少历史证据"],
                    "conflicts": [],
                    "evidence_ids": payload["valid_evidence"],
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
    event_id = create_confirmed_event(client)["event_id"]
    response = client.post(f"/api/v1/events/{event_id}:classify")
    assert response.status_code == 409
    assert "evidence agent found no new evidence" in response.json()["message"]


@pytest.mark.parametrize("failure_point", ["risk_checkpoint", "after_graph"])
def test_investigation_resumes_after_interruption(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, failure_point: str
) -> None:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from app.modules.operation_event.application import investigation

    original_put = AsyncPostgresSaver.aput
    original_generate = FakeTextLLMProvider.generate_structured
    original_proposal = investigation.AgentProposal
    interrupted = False
    risk_calls = 0

    async def fail_once(self, config, checkpoint, metadata, new_versions):
        nonlocal interrupted
        if (
            failure_point == "risk_checkpoint"
            and not interrupted
            and "risk" in checkpoint["channel_values"]
        ):
            interrupted = True
            raise RuntimeError("simulated worker loss after risk commit")
        return await original_put(self, config, checkpoint, metadata, new_versions)

    def fail_proposal(*args, **kwargs):
        nonlocal interrupted
        interrupted = True
        raise RuntimeError("simulated worker loss after graph completion")

    async def count_calls(self, *, operation, system_prompt, user_prompt, response_model):
        nonlocal risk_calls
        if operation == "multi_agent_risk":
            risk_calls += 1
        return await original_generate(
            self,
            operation=operation,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_model=response_model,
        )

    event_id = create_confirmed_event(client)["event_id"]
    client.app.state.settings.run_tasks_inline = False
    queued = client.post(f"/api/v1/events/{event_id}:classify")
    assert queued.status_code == 202, queued.text
    job_id = UUID(queued.json()["job_id"])
    if failure_point == "risk_checkpoint":
        monkeypatch.setattr(AsyncPostgresSaver, "aput", fail_once)
    else:
        monkeypatch.setattr(investigation, "AgentProposal", fail_proposal)
    monkeypatch.setattr(FakeTextLLMProvider, "generate_structured", count_calls)

    async def execute():
        async with client.app.state.database.session_factory() as session:
            job = await session.get(AsyncJob, job_id)
            event = await session.get(ProductionEvent, UUID(event_id))
            version = await session.get(ProductionEventVersion, event.current_version_id)
            return await execute_event_classification(
                session,
                job=job,
                event=event,
                version=version,
                user=await job_user(session, job),
                provider=client.app.state.providers.text,
            )

    with pytest.raises(ExceptionGroup) as error:
        client.portal.call(execute)
    assert interrupted, repr(error.value.exceptions)
    if failure_point == "after_graph":
        monkeypatch.setattr(investigation, "AgentProposal", original_proposal)

    async def requeue():
        async with client.app.state.database.session_factory() as session:
            job = await session.get(AsyncJob, job_id)
            run = await session.get(AgentRun, UUID(job.config_snapshot["agent_run_id"]))
            job.status = "queued"
            job.lease_expires_at = None
            run.status = "queued"
            run.completed_at = None
            await session.commit()

    client.portal.call(requeue)
    workflow, assessment = client.portal.call(execute)
    assert workflow.status == "awaiting_review"
    assert assessment.review_status == "pending"
    assert risk_calls == 1


def test_classification_redelivery_waits_for_existing_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.operation_event.application import tasks

    async def busy(_job_id: str) -> str:
        raise ConflictError("job already has an active execution lease")

    retry = Mock(side_effect=RuntimeError("rescheduled"))
    monkeypatch.setattr(tasks, "_classify", busy)
    monkeypatch.setattr(tasks.classify_event_task, "retry", retry)
    with pytest.raises(RuntimeError, match="rescheduled"):
        tasks.classify_event_task.run(str(uuid4()))
    assert retry.call_args.kwargs["countdown"] == 30
