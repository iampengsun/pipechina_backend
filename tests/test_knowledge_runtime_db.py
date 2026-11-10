"""Requires dedicated PostgreSQL test database, never a developer data schema."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.shared.errors import ConflictError
from app.shared.platform.execution import claim_job, guard_execution
from app.shared.platform.models import AsyncJob
from tests.helpers import create_confirmed_event


def test_lease_takeover_fences_old_execution(client):
    event_id = create_confirmed_event(client)["event_id"]
    response = client.post(f"/api/v1/events/{event_id}:classify")
    assert response.status_code == 202

    async def verify():
        async with client.app.state.database.session_factory() as session:
            original = await session.scalar(
                select(AsyncJob).where(AsyncJob.job_type == "event_classification")
            )
            job = AsyncJob(
                id=uuid4(),
                job_type="knowledge_index",
                resource_type="test",
                resource_id=uuid4(),
                organization_unit_id=original.organization_unit_id,
                requested_by=original.requested_by,
                config_snapshot={},
                status="queued",
            )
            session.add(job)
            await session.commit()
            job_id = job.id
            first = await claim_job(session, job_id, lease_seconds=60)
            with pytest.raises(ConflictError):
                await claim_job(session, job.id, lease_seconds=60)
            await session.rollback()
            job = await session.get(AsyncJob, job_id)
            job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
            second = await claim_job(session, job.id, lease_seconds=60)
            assert second != first
            with pytest.raises(ConflictError):
                await guard_execution(session, job.id, first)
            assert (await guard_execution(session, job.id, second)).execution_token == second

    asyncio.run(verify())
