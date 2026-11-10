"""Fenced execution leases. Guard the lease in the publication transaction."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.shared.errors import AppError, ConflictError
from app.shared.platform.models import AsyncJob


class ExecutionBusyError(ConflictError):
    """Another worker still owns this job; delivery should be retried later."""


async def claim_job(session: AsyncSession, job_id: UUID, *, lease_seconds: int) -> UUID:
    """为可执行任务申请带过期时间的唯一租约。"""

    if lease_seconds <= 0:
        raise ValueError("lease duration must be positive")  # 拒绝无效租期
    job = await session.scalar(
        select(AsyncJob)
        .where(AsyncJob.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = datetime.now(UTC)
    if job is None or job.status not in {"queued", "running"} or job.cancel_requested:
        raise ConflictError("job is unavailable for execution")  # 任务不可执行
    if job.lease_expires_at and job.lease_expires_at > now:
        raise ExecutionBusyError("job already has an active execution lease")  # 防止重复Worker
    token = uuid4()
    job.execution_token = token
    job.lease_expires_at = now + timedelta(seconds=lease_seconds)
    job.heartbeat_at = now
    job.status = "running"
    job.started_at = job.started_at or now
    job.lock_version += 1
    await session.commit()
    return token


async def guard_execution(session: AsyncSession, job_id: UUID, token: UUID) -> AsyncJob:
    """在发布结果前确认当前Worker仍持有有效租约。"""

    job = await session.scalar(
        select(AsyncJob)
        .where(AsyncJob.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        job is None
        or job.execution_token != token
        or job.status != "running"
        or job.cancel_requested
        or job.lease_expires_at is None
        or job.lease_expires_at <= datetime.now(UTC)
    ):
        raise ConflictError("execution lease lost, cancelled or expired")  # 阻止旧Worker写回
    return job


@asynccontextmanager
async def execution_lease(
    factory: async_sessionmaker[AsyncSession],
    job_id: UUID,
    *,
    lease_seconds: int = 120,
    interval_seconds: int = 20,
) -> AsyncIterator[UUID]:
    """申请租约并在任务执行期间定时续租。"""

    if interval_seconds * 2 >= lease_seconds:
        raise ValueError("heartbeat interval must be less than half lease duration")
    async with factory() as session:
        token = await claim_job(session, job_id, lease_seconds=lease_seconds)

    async def heartbeat():
        """周期续租并在所有权丢失时终止执行。"""

        while True:
            await asyncio.sleep(interval_seconds)  # 控制续租频率
            now = datetime.now(UTC)
            async with factory() as session:
                result = await session.execute(
                    update(AsyncJob)
                    .where(
                        AsyncJob.id == job_id,
                        AsyncJob.execution_token == token,
                        AsyncJob.status == "running",
                        AsyncJob.cancel_requested.is_(False),
                        AsyncJob.lease_expires_at > now,
                    )
                    .values(
                        heartbeat_at=now, lease_expires_at=now + timedelta(seconds=lease_seconds)
                    )
                )
                await session.commit()
                if getattr(result, "rowcount", 0) != 1:
                    raise ConflictError("execution lease heartbeat rejected")  # 租约已丢失

    try:
        async with asyncio.TaskGroup() as group:
            task = group.create_task(heartbeat())
            try:
                yield token
            finally:
                task.cancel()
    except ExceptionGroup as error:
        if len(error.exceptions) == 1 and isinstance(error.exceptions[0], AppError):
            raise error.exceptions[0] from None
        raise
    finally:
        async with factory() as session:
            await session.execute(
                update(AsyncJob)
                .where(
                    AsyncJob.id == job_id,
                    AsyncJob.execution_token == token,
                )
                .values(lease_expires_at=datetime.now(UTC))
            )
            await session.commit()


def retryable_error(error: Exception) -> bool:
    """判断任务异常是否适合自动重试。"""

    if isinstance(error, (TimeoutError, httpx.TransportError)):
        return True
    if isinstance(error, AppError):
        return error.code == "KNOWLEDGE_TRANSPORT_ERROR" or bool(
            (error.details or {}).get("retryable")
        )
    return False
