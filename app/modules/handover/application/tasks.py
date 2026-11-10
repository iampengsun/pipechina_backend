import asyncio
from uuid import UUID

from app.bootstrap.celery_app import celery_app
from app.bootstrap.config import Settings
from app.modules.handover.application.service import (
    execute_summary,
    execute_transcription,
    get_audio,
)
from app.shared.platform.execution import ExecutionBusyError
from app.shared.platform.service import get_job
from app.shared.worker_runtime import job_user, mark_job_failed, worker_resources


async def process_transcription(job_id: str, settings: Settings | None = None) -> str:
    async with worker_resources(settings) as resources:
        async with resources.database.session_factory() as session:
            job = await get_job(session, UUID(job_id))
            audio = await get_audio(session, job.resource_id)
            try:
                await execute_transcription(
                    session,
                    job=job,
                    audio=audio,
                    user=await job_user(session, job),
                    provider=resources.asr,
                )
            except Exception as exc:
                await mark_job_failed(session, job, exc)
                raise
            return job.status


async def process_summary(job_id: str, settings: Settings | None = None) -> str:
    async with worker_resources(settings) as resources:
        async with resources.database.session_factory() as session:
            job = await get_job(session, UUID(job_id))
            audio = await get_audio(session, job.resource_id)
            transcript_id = UUID(job.config_snapshot["transcript_version_id"])
            from app.modules.handover.domain.models import AudioTranscriptVersion

            transcript = await session.get(AudioTranscriptVersion, transcript_id)
            if transcript is None or transcript.audio_record_id != audio.id:
                raise ValueError(f"transcript version {transcript_id} does not exist")
            try:
                await execute_summary(
                    session,
                    job=job,
                    audio=audio,
                    transcript=transcript,
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
    name="app.modules.handover.transcribe_audio",
    acks_late=True,
)
def transcribe_audio_task(job_id: str) -> dict[str, str]:  # 这个任务音频转文字
    return {"job_id": job_id, "status": asyncio.run(process_transcription(job_id))}


@celery_app.task(
    autoretry_for=(ExecutionBusyError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 30},
    name="app.modules.handover.summarize_audio",
    acks_late=True,
)
def summarize_audio_task(job_id: str) -> dict[str, str]:  # 这个任务文字进行总结
    return {"job_id": job_id, "status": asyncio.run(process_summary(job_id))}
