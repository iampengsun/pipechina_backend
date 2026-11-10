import asyncio
from uuid import UUID

from app.bootstrap.celery_app import celery_app
from app.modules.operation_event.application.service import (
    execute_event_classification,
    execute_event_extraction,
    get_event,
    load_persisted_source,
)
from app.modules.operation_event.domain.models import ProductionEventVersion
from app.shared.errors import ConflictError
from app.shared.platform.execution import ExecutionBusyError
from app.shared.platform.service import get_job
from app.shared.worker_runtime import job_user, mark_job_failed, worker_resources


async def _extract(job_id: str) -> str:
    """恢复 Worker 资源与来源快照，执行抽取并在异常时记录失败。"""
    async with worker_resources() as resources:  # 按 Worker 生命周期加载并释放数据库和模型资源
        async with (
            resources.database.session_factory() as session
        ):  # 为本次后台任务创建独立数据库会话
            job = await get_job(session, UUID(job_id))
            try:
                source_type = str(job.config_snapshot["source_type"])
                version_id = UUID(job.config_snapshot["source_version_id"])
                text = await load_persisted_source(
                    session, source_type=source_type, source_version_id=version_id
                )
                await execute_event_extraction(
                    session,
                    job=job,
                    text=text,
                    source_type=source_type,  # 来源对象类型
                    source_id=job.resource_id,  # 来源主记录标识
                    source_version_id=version_id,  # 固定来源版本标识
                    user=await job_user(session, job),
                    provider=resources.text,
                )
            except Exception as exc:
                await mark_job_failed(session, job, exc)  # 记录抽取任务异常，再交给 Celery 感知失败
                raise
            return job.status  # 返回任务当前状态


async def _classify(job_id: str) -> str:
    """恢复分类任务的固定事件版本，将执行与失败收尾交给分类执行器。"""
    async with worker_resources() as resources:  # 按 Worker 生命周期加载并释放数据库和模型资源
        async with (
            resources.database.session_factory() as session
        ):  # 为本次后台任务创建独立数据库会话
            job = await get_job(session, UUID(job_id))
            if job.status in {"succeeded", "failed", "cancelled", "waiting_input"}:
                return job.status
            try:
                event = await get_event(session, job.resource_id)
                version_id = UUID(job.config_snapshot["event_version_id"])
                version = await session.get(ProductionEventVersion, version_id)
                if version is None:
                    raise ValueError(f"event version {version_id} does not exist")
                await execute_event_classification(
                    session,
                    job=job,
                    event=event,
                    version=version,  # 本次任务固定的事件版本实体
                    user=await job_user(session, job),
                    provider=resources.text,
                    knowledge=resources.knowledge,
                    site_context=resources.site_context,
                )
            except Exception:
                raise  # 分类执行器按租约令牌收尾，重复投递不能覆盖其他执行者状态
            return job.status  # 返回任务当前状态


@celery_app.task(
    autoretry_for=(ExecutionBusyError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 30},
    name="app.modules.operation_event.extract_events",
    acks_late=True,
)
def extract_events_task(job_id: str) -> dict[str, str]:
    return {"job_id": job_id, "status": asyncio.run(_extract(job_id))}


@celery_app.task(
    autoretry_for=(ExecutionBusyError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 30},
    name="app.modules.operation_event.classify_event",
    acks_late=True,
    bind=True,
    max_retries=30,
)
def classify_event_task(self, job_id: str) -> dict[str, str]:
    try:
        status = asyncio.run(_classify(job_id))
    except ConflictError as exc:
        if exc.message != "job already has an active execution lease":
            raise
        raise self.retry(exc=exc, countdown=30) from exc
    return {"job_id": job_id, "status": status}


async def _consolidate_memory(job_id: str) -> str:
    from app.modules.operation_event.application.memory import execute_consolidation

    async with worker_resources() as resources:
        async with resources.database.session_factory() as session:
            job = await get_job(session, UUID(job_id))
            if job.status in {"succeeded", "failed", "cancelled", "waiting_input"}:
                return job.status
            try:
                await execute_consolidation(
                    session, job=job, user=await job_user(session, job), provider=resources.text
                )
            except Exception as error:
                await mark_job_failed(session, job, error)
                raise
            return job.status


@celery_app.task(
    name="app.modules.operation_event.consolidate_memory",
    acks_late=True,
    autoretry_for=(ExecutionBusyError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 30},
)
def consolidate_memory_task(job_id: str) -> dict[str, str]:
    return {"job_id": job_id, "status": asyncio.run(_consolidate_memory(job_id))}


async def _optimize_prompt(job_id: str) -> str:
    from app.modules.operation_event.application.optimization import execute

    async with worker_resources() as resources:
        async with resources.database.session_factory() as session:
            job = await get_job(session, UUID(job_id))
            await execute(session, job=job, provider=resources.text)
            return job.status


@celery_app.task(
    name="app.modules.operation_event.optimize_prompt",
    acks_late=True,
    autoretry_for=(ExecutionBusyError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 30},
)
def optimize_prompt_task(job_id: str) -> dict[str, str]:
    return {"job_id": job_id, "status": asyncio.run(_optimize_prompt(job_id))}
