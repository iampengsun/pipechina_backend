import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select

from app.bootstrap.celery_app import celery_app
from app.bootstrap.config import Settings
from app.modules.meeting.application.service import (
    delete_recording,
    execute_minutes_generation,
    execute_transcription,
    get_meeting,
)
from app.modules.meeting.domain.models import Meeting
from app.shared.errors import ConflictError
from app.shared.platform.execution import ExecutionBusyError
from app.shared.platform.service import get_job
from app.shared.worker_runtime import job_user, mark_job_failed, worker_resources


async def process(job_id: str) -> str:
    async with worker_resources() as resources:
        async with resources.database.session_factory() as session:
            job = await get_job(session, UUID(job_id))
            meeting = await get_meeting(session, job.resource_id)
            try:
                await execute_transcription(
                    session,
                    job=job,
                    meeting=meeting,
                    user=await job_user(session, job),
                    provider=resources.asr,
                )
            except Exception as exc:
                await mark_job_failed(session, job, exc)
                raise
            return job.status


@celery_app.task(
    autoretry_for=(ExecutionBusyError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 30},
    name="app.modules.meeting.transcribe",
    acks_late=True,
)
def transcribe(job_id: str):
    return {"job_id": job_id, "status": asyncio.run(process(job_id))}


async def process_recording_retention(settings: Settings | None = None) -> int:
    async with worker_resources(settings) as resources:
        days = resources.settings.meeting_recording_retention_days
        if days is None:
            return 0
        cutoff = datetime.now(UTC) - timedelta(days=days)
        async with resources.database.session_factory() as session:
            meeting_ids = list(
                await session.scalars(
                    select(Meeting.id).where(
                        Meeting.upload_status.in_(["verified", "deleting"]),
                        Meeting.started_at <= cutoff,
                    )
                )
            )
        deleted = 0
        for meeting_id in meeting_ids:
            async with resources.database.session_factory() as session:
                meeting = await get_meeting(session, meeting_id)
                if meeting.ended_at is not None and meeting.ended_at > cutoff:
                    continue
                try:
                    await delete_recording(
                        session,
                        meeting=meeting,
                        storage=resources.storage,
                        reason="configured retention period expired",
                    )
                except ConflictError:
                    await session.rollback()
                    continue
                deleted += 1
        return deleted


@celery_app.task(
    autoretry_for=(ExecutionBusyError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 30},
    name="app.modules.meeting.cleanup_recordings",
    acks_late=True,
)
def cleanup_recordings():
    return {"deleted": asyncio.run(process_recording_retention())}


async def process_minutes(job_id: str) -> str:
    async with worker_resources() as resources:
        async with resources.database.session_factory() as session:
            job = await get_job(session, UUID(job_id))
            meeting = await get_meeting(session, job.resource_id)
            try:
                await execute_minutes_generation(
                    session,
                    job=job,
                    meeting=meeting,
                    user=await job_user(session, job),
                    provider=resources.text,
                )
            except Exception as exc:
                await mark_job_failed(session, job, exc)
                raise
            return job.status


@celery_app.task(
    autoretry_for=(ExecutionBusyError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 30},
    name="app.modules.meeting.generate_minutes",
    acks_late=True,
)
def generate_minutes(job_id: str):
    return {"job_id": job_id, "status": asyncio.run(process_minutes(job_id))}
