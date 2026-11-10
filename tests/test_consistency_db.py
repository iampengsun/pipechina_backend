"""Integration regressions; run only against the dedicated TEST_DATABASE_URL."""

from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.modules.operation_event.domain.agent_models import AgentRun, AgentTask
from app.modules.operation_event.domain.models import ProductionEvent, ProductionEventVersion
from app.shared.errors import ConflictError
from app.shared.platform.execution import claim_job
from app.shared.platform.models import AsyncJob
from app.shared.worker_runtime import job_user
from tests.helpers import create_confirmed_event
from tests.security_helpers import request_mock_token


def test_expired_token_cannot_replay_real_cached_write(client, settings):
    headers = {"Idempotency-Key": "cached-private-create"}
    payload = {
        "shift_date": "2026-09-19",
        "shift_code": "day",
        "filename": "private.m4a",
        "size_bytes": 100,
        "mime_type": "audio/mp4",
    }
    first = client.post("/api/v1/audio-records", json=payload, headers=headers)
    assert first.status_code == 201, first.text
    headers["Authorization"] = "Bearer " + request_mock_token(settings, scenario="expired")
    replay = client.post("/api/v1/audio-records", json=payload, headers=headers)
    assert replay.status_code == 401
    assert "private.m4a" not in replay.text


def test_rag_retry_cancel_targets_new_attempt(client):
    client.app.state.settings.knowledge_backend = "fake"
    client.app.state.settings.run_tasks_inline = False
    created = client.post("/api/v1/knowledge/runs", json={"question": "测试重试"})
    assert created.status_code == 202, created.text
    run_id, old_id = created.json()["id"], created.json()["job_id"]

    async def fail():
        async with client.app.state.database.session_factory() as session:
            job = await session.get(AsyncJob, UUID(old_id))
            job.status = "failed"
            await session.commit()

    client.portal.call(fail)
    retried = client.post(f"/api/v1/jobs/{old_id}:retry")
    assert retried.status_code == 202, retried.text
    new_id = retried.json()["id"]
    cancelled = client.post(f"/api/v1/knowledge/runs/{run_id}:cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert client.get(f"/api/v1/jobs/{new_id}").json()["status"] == "cancelled"
    assert client.get(f"/api/v1/jobs/{old_id}").json()["status"] == "failed"
    assert client.post(f"/api/v1/jobs/{old_id}:retry").status_code == 409


def test_agent_retry_owns_new_tasks_and_duplicate_delivery_is_fenced(client, monkeypatch):
    from app.modules.operation_event.application import service

    event_id = create_confirmed_event(client)["event_id"]
    created = client.post(f"/api/v1/events/{event_id}:classify")
    assert created.status_code == 202, created.text
    old_id = UUID(created.json()["job_id"])
    old_run_id = UUID(created.json()["agent_run_id"])

    async def fail():
        async with client.app.state.database.session_factory() as session:
            job = await session.get(AsyncJob, old_id)
            job.status = "failed"
            run = await session.get(AgentRun, old_run_id)
            run.status = "failed"
            await session.commit()

    client.portal.call(fail)
    retried = client.post(f"/api/v1/jobs/{old_id}:retry")
    assert retried.status_code == 202, retried.text
    new_id = UUID(retried.json()["id"])
    executor = AsyncMock()
    monkeypatch.setattr(service, "_execute_event_classification", executor)

    async def check():
        async with client.app.state.database.session_factory() as session:
            job = await session.get(AsyncJob, new_id)
            run_id = UUID(job.config_snapshot["agent_run_id"])
            assert run_id != old_run_id
            assert (
                await session.scalar(
                    select(func.count()).select_from(AgentTask).where(AgentTask.run_id == run_id)
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(AgentTask)
                    .where(AgentTask.run_id == old_run_id)
                )
                == 5
            )
            token = await claim_job(session, new_id, lease_seconds=120)
            event = await session.get(ProductionEvent, UUID(event_id))
            version = await session.get(ProductionEventVersion, event.current_version_id)
            with pytest.raises(ConflictError, match="active execution lease"):
                await service.execute_event_classification(
                    session,
                    job=job,
                    event=event,
                    version=version,
                    user=await job_user(session, job),
                    provider=client.app.state.providers.text,
                )
            fresh = await session.get(AsyncJob, new_id)
            assert fresh.status == "running"
            assert fresh.execution_token == token

    client.portal.call(check)
    executor.assert_not_awaited()
