import asyncio
from typing import Any
from uuid import UUID

from app.bootstrap.celery_app import celery_app
from app.modules.knowledge.application.answers import execute_answer
from app.modules.knowledge.application.evaluation import execute_evaluation
from app.modules.knowledge.application.pipeline import execute_ingestion
from app.shared.errors import NotFoundError
from app.shared.platform.models import AsyncJob
from app.shared.worker_runtime import worker_resources


async def dispatch(job_id: str, kind: str):
    async with worker_resources() as resources:
        async with resources.database.session_factory() as session:
            job = await session.get(AsyncJob, UUID(job_id))
            if job is None:
                raise NotFoundError("job", job_id)
            snapshot = job.config_snapshot
        async with resources.knowledge.scoped(snapshot) as knowledge:
            common: dict[str, Any] = dict(
                factory=resources.database.session_factory,
                knowledge=knowledge,
                text=resources.text,
                settings=resources.settings,
                job_id=UUID(job_id),
            )
            if kind == "index":
                await execute_ingestion(**common, storage=resources.storage)
            elif kind == "answer":
                await execute_answer(**common)
            else:
                await execute_evaluation(**common)


@celery_app.task(name="app.modules.knowledge.index_document", acks_late=True)
def index_document(job_id: str):
    asyncio.run(dispatch(job_id, "index"))


@celery_app.task(name="app.modules.knowledge.answer", acks_late=True)
def answer(job_id: str):
    asyncio.run(dispatch(job_id, "answer"))


@celery_app.task(name="app.modules.knowledge.evaluate", acks_late=True)
def evaluate(job_id: str):
    asyncio.run(dispatch(job_id, "evaluate"))
