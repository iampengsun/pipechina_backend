"""Shared lease wrapper for business jobs, including inline execution."""

from contextvars import ContextVar
from datetime import UTC, datetime
from functools import wraps
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.shared.errors import ConflictError
from app.shared.platform.execution import execution_lease
from app.shared.platform.models import AsyncJob

_execution: ContextVar[tuple[UUID, UUID] | None] = ContextVar(
    "business_job_execution", default=None
)


async def guard_job_update(session, job, status):
    context = _execution.get()
    if context is None or context[0] != job.id:
        return
    row = (
        await session.execute(
            select(
                AsyncJob.execution_token,
                AsyncJob.status,
                AsyncJob.cancel_requested,
                AsyncJob.lease_expires_at,
            )
            .where(AsyncJob.id == job.id)
            .with_for_update()
        )
    ).one()
    if (
        row.execution_token != context[1]
        or row.status != "running"
        or row.lease_expires_at is None
        or row.lease_expires_at <= datetime.now(UTC)
        or (row.cancel_requested and status != "cancelled")
    ):
        raise ConflictError("execution lease lost, cancelled or expired")
    if status == "succeeded":
        await session.flush()
        from app.shared.worker_runtime import job_user

        current = await job_user(session, job)
        from app.shared.security.authorization.dependencies import require_data_scope

        permission = {
            "audio_transcription": "handover:process",
            "handover_summary": "handover:process",
            "meeting_transcription": "meeting:process",
            "meeting_minutes": "meeting:process",
            "inspection_image_analysis": "inspection:analyze",
            "report_generation": "report:generate",
            "report_export": "report:export",
            "event_extraction": "event:extract",
            "prompt_optimization": "admin:prompt",
            "experience_consolidation": "event:classify",
        }[job.job_type]
        require_data_scope(current, job.organization_unit_id, permission, owner_id=job.requested_by)
        if job.job_type.startswith("meeting_"):
            from app.modules.meeting.application.service import _check_acl, get_meeting

            _check_acl(await get_meeting(session, job.resource_id), current, permission)


def leased_job(function):
    @wraps(function)
    async def execute(session, *args, **kwargs):
        job = kwargs["job"]
        # Inline callers may have just created the job in this transaction.
        await session.commit()
        await session.refresh(job)
        if job.status in {"succeeded", "failed", "cancelled"} or job.cancel_requested:
            return await function(session, *args, **kwargs)
        factory = async_sessionmaker(session.bind, expire_on_commit=False, autoflush=False)
        token = None
        context_token = None
        job_id = job.id
        try:
            async with execution_lease(factory, job_id) as token:
                context_token = _execution.set((job_id, token))
                await session.refresh(job)
                try:
                    return await function(session, *args, **kwargs)
                except BaseException:
                    # Release publication locks before the separate lease-cleanup transaction.
                    await session.rollback()
                    raise
                finally:
                    _execution.reset(context_token)
                    context_token = None
        except BaseException as error:
            if context_token is not None:
                _execution.reset(context_token)
            await session.rollback()
            if token is not None:
                # Only this attempt may record failure. Never overwrite a replacement worker.
                async with factory() as failure:
                    saved = await failure.get(AsyncJob, job_id, with_for_update=True)
                    if saved and saved.execution_token == token and saved.status == "running":
                        from app.shared.platform.service import update_job

                        cancelled = saved.cancel_requested
                        await update_job(
                            failure,
                            saved,
                            status="cancelled" if cancelled else "failed",
                            progress=saved.progress,
                            error_code=None if cancelled else "BUSINESS_JOB_FAILED",
                            message="job cancelled"
                            if cancelled
                            else "business job execution failed",
                        )
                        await failure.commit()
                await session.refresh(job)
            while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
                error = error.exceptions[0]
            raise error

    return execute
