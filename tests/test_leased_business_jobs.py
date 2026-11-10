"""Duplicate delivery and late results must not publish competing business data."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.shared.errors import ConflictError
from app.shared.platform.leased_jobs import leased_job
from app.shared.platform.models import AsyncJob, JobEvent
from app.shared.platform.service import create_job, update_job
from app.shared.worker_runtime import job_user
from tests.helpers import create_confirmed_event


@pytest.mark.parametrize("takeover", [False, True])
def test_business_lease_blocks_duplicate_and_fences_late_results(client, takeover):
    source_job = create_confirmed_event(client)["job_id"]

    async def scenario():
        factory = client.app.state.database.session_factory
        async with factory() as session:
            from uuid import UUID

            original = await session.get(AsyncJob, UUID(source_job))
            user = await job_user(session, original)
            job = await create_job(
                session,
                user=user,
                job_type="event_extraction",
                resource_type="manual_operation",
                resource_id=uuid4(),
                task_name="test",
                queue="ai_text",
                enqueue=False,
            )
            await session.commit()
            job_id = job.id
        started, release = asyncio.Event(), asyncio.Event()

        @leased_job
        async def execute(session, *, job, wait):
            await update_job(session, job, status="running", progress=10)
            await session.commit()
            if wait:
                started.set()
                await release.wait()
            session.add(JobEvent(job_id=job.id, event_type="produced", payload={"late": wait}))
            await update_job(session, job, status="succeeded", progress=100)
            await session.commit()

        async def run(wait):
            async with factory() as session:
                job = await session.get(AsyncJob, job_id)
                await execute(session, job=job, wait=wait)

        first = asyncio.create_task(run(True))
        await asyncio.wait_for(started.wait(), 5)
        if takeover:
            async with factory() as session:
                await session.execute(
                    update(AsyncJob)
                    .where(AsyncJob.id == job_id)
                    .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
                )
                await session.commit()
            await run(False)
            release.set()
            with pytest.raises(ConflictError):
                await first
        else:
            with pytest.raises(ConflictError, match="active execution lease"):
                await run(False)
            async with factory() as session:
                assert (await session.get(AsyncJob, job_id)).status == "running"
            release.set()
            await first
        async with factory() as session:
            assert (await session.get(AsyncJob, job_id)).status == "succeeded"
            produced = list(
                await session.scalars(
                    select(JobEvent).where(
                        JobEvent.job_id == job_id, JobEvent.event_type == "produced"
                    )
                )
            )
            assert len(produced) == 1
            assert produced[0].payload["late"] is (not takeover)

    client.portal.call(scenario)


def test_business_failure_releases_publication_lock_before_lease_cleanup(client):
    source_job = create_confirmed_event(client)["job_id"]

    async def scenario():
        from uuid import UUID

        factory = client.app.state.database.session_factory
        async with factory() as session:
            source = await session.get(AsyncJob, UUID(source_job))
            job = await create_job(
                session,
                user=await job_user(session, source),
                job_type="event_extraction",
                resource_type="manual_operation",
                resource_id=uuid4(),
                task_name="test",
                queue="ai_text",
                enqueue=False,
            )
            await session.commit()
            job_id = job.id

        @leased_job
        async def execute(session, *, job):
            await update_job(session, job, status="running", progress=10)
            raise RuntimeError("model failure while publication row is locked")

        async with factory() as session:
            job = await session.get(AsyncJob, job_id)
            with pytest.raises(RuntimeError, match="model failure"):
                await asyncio.wait_for(execute(session, job=job), timeout=5)
        async with factory() as session:
            saved = await session.get(AsyncJob, job_id)
            assert saved.status == "failed"
            assert saved.error_code == "BUSINESS_JOB_FAILED"

    client.portal.call(scenario)
