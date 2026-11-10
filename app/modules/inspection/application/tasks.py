import asyncio
from uuid import UUID

from sqlalchemy import select

from app.bootstrap.celery_app import celery_app
from app.modules.inspection.application.service import execute_inspection_analysis, get_inspection
from app.modules.inspection.domain.models import InspectionImage
from app.shared.platform.execution import ExecutionBusyError
from app.shared.platform.service import get_job
from app.shared.worker_runtime import job_user, mark_job_failed, worker_resources


async def _analyze(job_id: str) -> str:
    async with worker_resources() as resources:
        async with resources.database.session_factory() as session:
            job = await get_job(session, UUID(job_id))
            try:
                inspection = await get_inspection(session, job.resource_id)
                images = list(
                    await session.scalars(
                        select(InspectionImage).where(
                            InspectionImage.inspection_id == inspection.id,
                            InspectionImage.status.in_(("verified", "analyzing")),
                            InspectionImage.deleted.is_(False),
                        )
                    )
                )
                if not images:
                    raise ValueError("no verified inspection images remain")
                await execute_inspection_analysis(
                    session,
                    job=job,
                    inspection=inspection,
                    images=images,
                    user=await job_user(session, job),
                    provider=resources.vision,
                )
            except Exception as exc:
                await mark_job_failed(session, job, exc)
                raise
            return job.status


@celery_app.task(
    autoretry_for=(ExecutionBusyError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 30},
    name="app.modules.inspection.analyze_inspection",
    acks_late=True,
)
def analyze_inspection_task(job_id: str) -> dict[str, str]:
    return {"job_id": job_id, "status": asyncio.run(_analyze(job_id))}
