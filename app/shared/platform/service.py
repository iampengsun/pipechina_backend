from __future__ import annotations

# 平台领域服务：负责任务状态、审计链、配置快照和Outbox可靠发布。
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.shared.errors import ConflictError, NotFoundError
from app.shared.platform.models import (
    AICallLog,
    AsyncJob,
    AuditLog,
    JobEvent,
    TaskOutbox,
    UploadSession,
)
from app.shared.security.audit.context import current_request_id
from app.shared.security.authorization.schemas import CurrentUser


async def lock_configuration_key(session: AsyncSession, key: str) -> None:
    """在PostgreSQL中对配置业务键加事务级建议锁，序列化版本分配与激活。"""
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})


async def add_audit(
    session: AsyncSession,
    *,
    user: CurrentUser,
    action: str,
    resource_type: str,
    resource_id: UUID,
    before: dict | None = None,
    after: dict | None = None,
    reason: str | None = None,
) -> None:
    """
    创建一条审计日志。

    该函数记录某个用户对某项业务资源执行的操作，包括操作人、
    所属组织、操作名称、资源类型、资源ID、修改前数据、修改后数据
    和操作理由。

    该函数只把AuditLog对象加入当前Session，不会自行提交事务。
    审计日志将和调用方的业务数据在同一个数据库事务中提交。
    """
    await lock_configuration_key(session, "platform:audit-chain")
    occurred_at = datetime.now(UTC)
    previous = await session.scalar(
        select(AuditLog)
        .order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
        .limit(1)
        .with_for_update()
    )
    before = _redact_audit_data(before)
    after = _redact_audit_data(after)
    previous_hash = previous.entry_hash if previous is not None else None
    canonical = json.dumps(
        {
            "occurred_at": occurred_at.isoformat(),
            "actor_id": str(user.user_id),
            "organization_unit_id": str(user.organization_unit_id),
            "action": action,
            "resource_type": resource_type,
            "resource_id": str(resource_id),
            "before": before,
            "after": after,
            "request_id": current_request_id(),
            "reason": reason,
            "previous_hash": previous_hash,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    session.add(
        AuditLog(
            occurred_at=occurred_at,
            actor_id=user.user_id,  # 记录执行操作的当前用户ID。
            organization_unit_id=user.organization_unit_id,  # 记录用户所属组织机构ID。
            action=action,  # 记录操作名称，例如report.publish。
            resource_type=resource_type,  # 记录被操作资源的类型。
            resource_id=resource_id,  # 记录被操作资源的唯一ID。
            before_data=before,  # 保存操作前的数据快照。
            after_data=after,  # 保存操作后的数据快照。
            request_id=current_request_id(),
            reason=reason,  # 保存用户执行该操作时填写的理由。
            previous_hash=previous_hash,
            entry_hash=hashlib.sha256(canonical.encode()).hexdigest(),
        )
    )


_SENSITIVE_AUDIT_KEYS = {"password", "secret", "token", "api_key", "authorization"}


def _redact_audit_data(value: Any) -> Any:
    """递归移除审计快照中的凭据，防止审计系统反向成为泄密源。"""
    if isinstance(value, dict):
        return {
            str(key): "***REDACTED***"
            if any(part in str(key).lower() for part in _SENSITIVE_AUDIT_KEYS)
            else _redact_audit_data(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_audit_data(item) for item in value]
    return value


async def create_job(
    session: AsyncSession,
    *,
    user: CurrentUser,
    job_type: str,
    resource_type: str,
    resource_id: UUID,
    task_name: str,
    queue: str,
    config_snapshot: dict[str, Any] | None = None,
    operation_key: str | None = None,
    enqueue: bool = True,
    reuse_active: bool = True,
    organization_unit_id: UUID | None = None,
) -> AsyncJob:
    """
    创建一个统一的AI异步任务。

    创建任务前可以检查相同业务资源是否已经存在queued或running状态
    的同类型任务。如果存在并且reuse_active=True，则直接返回已有任务，
    避免重复执行。

    创建新任务后会根据enqueue参数决定是否同时创建TaskOutbox记录。
    TaskOutbox由独立发布器发送到Celery和RabbitMQ。

    该函数还会创建一条queued类型的JobEvent，用于记录任务时间线。

    该函数不会主动提交事务，由调用方统一执行commit。
    """
    if reuse_active:  # 判断是否允许复用已经存在的活动任务。
        active = await session.scalar(  # 查询符合条件的最新活动任务。
            select(AsyncJob)
            .where(
                AsyncJob.job_type == job_type,  # 限制为相同任务类型。
                AsyncJob.resource_type == resource_type,  # 限制为相同资源类型。
                AsyncJob.resource_id == resource_id,  # 限制为相同业务资源。
                AsyncJob.status.in_(("queued", "running", "waiting_input")),  # 只查活动任务。
            )
            .order_by(AsyncJob.created_at.desc())  # 优先返回最近创建的任务。
        )

        if active is not None:  # 如果已经存在活动任务。
            return active  # 直接返回已有任务，避免重复创建。

    snapshot = dict(config_snapshot or {})
    snapshot["task_name"] = task_name
    snapshot["queue"] = queue
    job = AsyncJob(
        job_type=job_type,  # 保存任务类型。
        resource_type=resource_type,  # 保存业务资源类型。
        resource_id=resource_id,  # 保存业务资源ID。
        organization_unit_id=organization_unit_id or user.organization_unit_id,
        status="queued",  # 新任务初始状态为queued。
        progress=0,  # 新任务初始进度为0。
        requested_by=user.user_id,  # 保存发起任务的用户ID。
        config_snapshot=snapshot,
    )

    try:
        async with session.begin_nested():
            session.add(job)
            await session.flush()
    except IntegrityError as exc:
        active = await session.scalar(
            select(AsyncJob)
            .where(
                AsyncJob.job_type == job_type,
                AsyncJob.resource_type == resource_type,
                AsyncJob.resource_id == resource_id,
                AsyncJob.status.in_(("queued", "running", "waiting_input")),
            )
            .order_by(AsyncJob.created_at.desc())
        )
        if reuse_active and active is not None:
            return active
        raise ConflictError("an active job already exists for this resource") from exc

    if enqueue:  # 判断是否需要把该任务发送到异步消息队列。
        op_key = operation_key or (  # 优先使用调用方提供的操作键，否则自动生成。
            f"{resource_type}:{resource_id}:{job_type}:{job.attempt}"
        )

        session.add(  # 创建事务发件箱记录并加入当前Session。
            TaskOutbox(
                job_id=job.id,  # 关联刚刚创建的异步任务。
                operation_key=op_key,  # 保存幂等操作键，防止重复创建Outbox消息。
                task_name=task_name,  # 保存需要发送的Celery任务名称。
                queue=queue,  # 保存任务需要进入的RabbitMQ队列。
                payload={"args": [str(job.id)], "kwargs": {}},
                status="pending",  # 新Outbox记录初始状态为pending。
                available_at=datetime.now(UTC),  # 设置该消息从当前时间开始可以发布。
            )
        )

    session.add(  # 创建任务排队事件并加入当前Session。
        JobEvent(
            job_id=job.id,  # 关联当前异步任务。
            event_type="queued",  # 表示任务已经进入排队状态。
            payload={"progress": 0},  # 记录该事件发生时任务进度为0。
        )
    )

    return job  # 返回新创建的异步任务对象。


async def get_job(
    session: AsyncSession,
    job_id: UUID,
) -> AsyncJob:
    """
    根据任务ID查询异步任务。

    如果任务存在，则返回对应的AIJob对象。
    如果任务不存在，则抛出NotFoundError，由统一异常处理器转换为404响应。
    """
    job = await session.get(AsyncJob, job_id)  # 根据异步任务主键查询任务。

    if job is None:  # 判断数据库中是否不存在该任务。
        raise NotFoundError("job", job_id)  # 抛出任务不存在异常。

    return job  # 返回查询到的任务对象。


async def update_job(
    session: AsyncSession,
    job: AsyncJob,
    *,
    status: str,
    progress: int,
    message: str | None = None,
    error_code: str | None = None,
    error_detail: str | None = None,
    worker_id: str | None = None,
    result: dict | None = None,
) -> None:
    """
    更新异步任务的状态、进度和错误信息。

    succeeded、failed和cancelled属于终态。任务进入终态后，不允许再转换成
    其他状态，避免已经完成或失败的任务重新变成running。

    更新任务的同时会追加一条JobEvent，形成完整的任务状态变化时间线。

    该函数不会提交事务，由调用方统一执行commit。
    """
    from app.shared.platform.leased_jobs import guard_job_update

    await guard_job_update(session, job, status)
    transitions = {
        "queued": {"running", "failed", "cancelled"},
        "running": {"running", "waiting_input", "succeeded", "failed", "cancelled"},
        "waiting_input": {"queued", "cancelled", "failed"},
        "succeeded": {"succeeded"},
        "failed": {"failed"},
        "cancelled": {"cancelled"},
    }
    if status not in transitions.get(job.status, set()):
        raise ConflictError(
            "invalid job state transition", current_status=job.status, requested_status=status
        )
    if progress < job.progress and status not in {"failed", "cancelled"}:
        raise ConflictError(
            "job progress cannot decrease",
            current_progress=job.progress,
            requested_progress=progress,
        )
    if not 0 <= progress <= 100:
        raise ConflictError("job progress must be between 0 and 100", progress=progress)
    if status == "succeeded" and progress != 100:
        raise ConflictError("succeeded job must have 100 progress")
    if status == "failed" and not error_code:
        raise ConflictError("failed job requires an error code")

    now = datetime.now(UTC)
    if status == "running":
        job.started_at = job.started_at or now
        job.heartbeat_at = now
        job.worker_id = worker_id or job.worker_id
    if status in {"succeeded", "failed", "cancelled"}:
        job.completed_at = now
    job.status = status
    job.progress = progress
    job.lock_version += 1

    job.message = message  # 更新用于前端展示的任务说明。

    job.error_code = error_code  # 更新稳定的任务错误代码。

    job.error_detail = error_detail  # 更新任务失败的详细错误信息。

    session.add(  # 创建一条新的任务状态事件。
        JobEvent(
            job_id=job.id,  # 关联当前任务。
            event_type=status,  # 使用新的任务状态作为事件类型。
            payload={
                **({"result": result} if result is not None else {}),
                "progress": job.progress,  # 保存更新后的任务进度。
                "message": message,  # 保存任务状态说明。
                "error_code": error_code,  # 保存任务错误代码。
            },
        )
    )


async def request_cancel(
    session: AsyncSession,
    job: AsyncJob,
) -> None:
    """
    请求取消一个尚未结束的异步任务。

    这里采用协作式取消，不会直接强制终止Worker进程。该函数只是把
    cancel_requested设置为True，真正执行任务的Worker需要在适当阶段
    检查该字段并主动停止后续处理。

    如果任务已经成功、失败或取消，则直接返回，不重复修改终态任务。
    """
    await session.refresh(job, with_for_update=True)
    if job.status in {"succeeded", "failed", "cancelled"}:  # 判断任务是否已经结束。
        return  # 终态任务不再处理取消请求。

    if job.status in {"queued", "waiting_input"}:
        await update_job(session, job, status="cancelled", progress=job.progress)
    job.cancel_requested = True  # 标记用户已经请求取消任务。

    job.message = "cancel requested"  # 更新任务说明，提示取消请求已经收到。
    job.lock_version += 1

    session.add(  # 创建取消请求事件。
        JobEvent(
            job_id=job.id,  # 关联当前异步任务。
            event_type="cancel_requested",  # 表示用户发出了取消请求。
            payload={"progress": job.progress},  # 记录请求取消时的任务进度。
        )
    )


async def retry_job(
    session: AsyncSession,
    original: AsyncJob,
    user: CurrentUser,
    *,
    on_retry: Callable[[AsyncSession, AsyncJob, AsyncJob, CurrentUser], Awaitable[None]]
    | None = None,
) -> AsyncJob:
    """
    为失败或已取消的任务创建一次新的重试任务。

    只有failed或cancelled状态的任务可以重试。函数会根据原任务的job_type
    找到对应的Celery任务名称和RabbitMQ队列，然后创建新的AIJob和Outbox记录。

    新任务通过parent_job_id关联原任务，attempt在原任务基础上加1，
    从而形成完整的任务重试链。
    """
    await session.flush()
    await session.refresh(original, with_for_update=True)
    existing_child = await session.scalar(
        select(AsyncJob).where(AsyncJob.parent_job_id == original.id).limit(1)
    )
    if existing_child is not None:
        raise ConflictError("this attempt already has a retry; use the latest attempt")
    if original.config_snapshot.get("retry_handler") and on_retry is None:
        raise ConflictError("business retry handler is required")
    # Recover attempts left queued by older publishers after their Outbox exhausted retries.
    if original.status == "queued":
        dead = await session.scalar(
            select(TaskOutbox.id)
            .where(TaskOutbox.job_id == original.id, TaskOutbox.status == "failed")
            .limit(1)
        )
        if dead:
            await update_job(
                session,
                original,
                status="failed",
                progress=original.progress,
                error_code="OUTBOX_PUBLISH_FAILED",
            )
    if original.status not in {"failed", "cancelled"}:  # 检查原任务是否允许重试。
        raise ConflictError(  # 非失败或取消状态不能重试。
            "only failed or cancelled jobs can be retried",
            status=original.status,
        )

    legacy_task_routes = {
        "audio_transcription": (
            "app.modules.handover.transcribe_audio",
            "audio",
        ),
        "handover_summary": (
            "app.modules.handover.summarize_audio",
            "ai_text",
        ),
        "event_extraction": (
            "app.modules.operation_event.extract_events",
            "ai_text",
        ),
        "event_classification": (
            "app.modules.operation_event.classify_event",
            "ai_text",
        ),
        "inspection_image_analysis": (
            "app.modules.inspection.analyze_inspection",
            "vision",
        ),
        "report_generation": (
            "app.modules.report.generate_report",
            "report",
        ),
        "report_export": (
            "app.modules.report.export_report",
            "report",
        ),
    }

    task_name = original.config_snapshot.get("task_name")
    queue = original.config_snapshot.get("queue")
    route = (
        (str(task_name), str(queue))
        if task_name and queue
        else legacy_task_routes.get(original.job_type)
    )

    if route is None:  # 判断当前任务类型是否没有配置重试路由。
        raise ConflictError(  # 拒绝不支持重试的任务类型。
            "job type does not support retry",
            job_type=original.job_type,
        )

    task_name, queue = route  # 拆出Celery任务名称和RabbitMQ队列名称。

    retried = await create_job(  # 创建新的重试任务和Outbox记录。
        session,
        user=user,  # 记录本次发起重试的用户。
        job_type=original.job_type,  # 沿用原任务类型。
        resource_type=original.resource_type,  # 沿用原资源类型。
        resource_id=original.resource_id,  # 沿用原业务资源ID。
        task_name=task_name,  # 使用任务类型对应的Celery任务名称。
        queue=queue,  # 使用任务类型对应的RabbitMQ队列。
        config_snapshot=original.config_snapshot,  # 沿用原任务的配置快照。
        operation_key=(  # 生成本次重试专用的幂等操作键。
            f"retry:{original.id}:{original.attempt + 1}"
        ),
        reuse_active=False,  # 重试时强制创建新任务，不复用已有活动任务。
        organization_unit_id=original.organization_unit_id,
    )

    retried.parent_job_id = original.id  # 记录新任务的父任务ID。

    retried.attempt = original.attempt + 1  # 将执行尝试次数增加1。

    if on_retry is not None:
        await on_retry(session, original, retried, user)

    return retried  # 返回新创建的重试任务。


def scope_digest(data_scope: dict) -> str:
    """
    计算数据权限范围的稳定SHA-256摘要。

    函数先把字典转换成键顺序稳定、无多余空格的JSON字符串，
    再计算SHA-256。只要数据范围内容相同，即使原字典键顺序不同，
    最终摘要也会保持一致。

    该摘要可以用于唯一约束、缓存键和数据范围比较。
    """
    raw = json.dumps(  # 把数据权限范围转换成规范化JSON字符串。
        data_scope,
        sort_keys=True,  # 按键名排序，消除字典键顺序差异。
        separators=(",", ":"),  # 删除JSON中的多余空格。
    )

    return hashlib.sha256(  # 计算规范化JSON字符串的SHA-256摘要。
        raw.encode()  # 将字符串编码成字节数据。
    ).hexdigest()  # 返回64字符十六进制摘要。


async def list_job_events(
    session: AsyncSession,
    job_id: UUID,
    *,
    after_event_id: UUID | None = None,
    limit: int = 100,
) -> list[JobEvent]:
    """
    查询指定异步任务的全部事件。

    事件按照创建时间和事件ID升序排列，从而形成稳定的任务时间线。
    使用事件ID作为第二排序条件，可以避免多个事件创建时间相同时顺序不稳定。
    """
    statement = select(JobEvent).where(JobEvent.job_id == job_id)
    if after_event_id is not None:
        cursor = await session.get(JobEvent, after_event_id)
        if cursor is not None and cursor.job_id == job_id:
            statement = statement.where(
                or_(
                    JobEvent.created_at > cursor.created_at,
                    (JobEvent.created_at == cursor.created_at) & (JobEvent.id > cursor.id),
                )
            )
    result = await session.scalars(
        statement.order_by(
            JobEvent.created_at,  # 首先按照事件创建时间升序排列。
            JobEvent.id,  # 创建时间相同时按照事件ID排列。
        ).limit(max(1, min(limit, 500)))
    )

    return list(result)  # 将SQLAlchemy标量结果转换成普通列表。


async def publish_pending_outbox(
    session: AsyncSession,
    send_task: Callable[..., Any],
    *,
    batch_size: int = 100,
    max_attempts: int = 8,
    base_retry_seconds: int = 5,
) -> int:
    """
    批量发布等待处理的事务发件箱消息。

    函数从task_outbox表中查询status为pending并且已经到达available_at
    的记录，然后通过send_task发送到Celery和RabbitMQ。

    查询使用FOR UPDATE SKIP LOCKED锁定记录。多个Outbox发布器同时运行时，
    已被其他事务锁定的记录会被跳过，从而避免多个发布器同时处理同一行。

    发布成功后将记录更新为published；发布失败时保留pending状态并记录错误。
    最后统一提交数据库事务，并返回成功发布的消息数量。
    """
    now = datetime.now(UTC)  # 记录本轮Outbox发布开始时的UTC时间。

    rows = list(  # 将查询结果转换成普通列表。
        await session.scalars(
            select(TaskOutbox)
            .where(
                TaskOutbox.status == "pending",  # 只查询尚未发布的Outbox消息。
                TaskOutbox.available_at <= now,  # 只查询已经到达可发布时间的消息。
            )
            .order_by(TaskOutbox.created_at)  # 优先处理最早创建的消息。
            .limit(batch_size)  # 限制单次处理数量，避免一次锁定过多记录。
            .with_for_update(skip_locked=True)  # 锁定记录并跳过其他事务已锁定的行。
        )
    )

    published = 0  # 初始化本次成功发布的消息数量。

    for row in rows:  # 逐条处理查询到的Outbox消息。
        job = await session.get(AsyncJob, row.job_id, with_for_update=True, populate_existing=True)
        if job is None or job.status in {"failed", "cancelled", "succeeded"}:
            row.status = "failed"
            row.last_error = "source job is terminal; do not publish an obsolete attempt"
            continue
        try:
            payload = row.payload or {}
            args = payload.get("args")
            if args is None and payload.get("job_id") is not None:
                args = [payload["job_id"]]
            send_task(
                row.task_name,  # 指定需要执行的Celery任务名称。
                args=args or [str(row.job_id)],
                kwargs=payload.get("kwargs") or {},
                queue=row.queue,  # 指定任务进入的RabbitMQ队列。
                task_id=str(row.id),  # 使用Outbox记录ID作为Celery任务ID。
            )

            row.status = "published"  # 将Outbox记录标记为已发布。

            row.published_at = now  # 记录成功发布时间。

            row.publish_attempts += 1  # 增加发布尝试次数。

            row.last_error = None  # 清除以前的发布错误信息。

            published += 1  # 增加成功发布计数。

        except Exception as exc:
            row.publish_attempts += 1  # 发布失败时仍然增加尝试次数。
            row.last_error = str(exc)[:2000]  # 保存错误信息并限制最大长度。
            if row.publish_attempts >= max_attempts:
                row.status = "failed"
                if job.status in {"queued", "waiting_input"}:
                    await update_job(
                        session,
                        job,
                        status="failed",
                        progress=job.progress,
                        error_code="OUTBOX_PUBLISH_FAILED",
                        message="message delivery exhausted retries; retry this job after recovery",
                    )
            else:
                delay = min(base_retry_seconds * (2 ** (row.publish_attempts - 1)), 3600)
                row.available_at = now + timedelta(seconds=delay)

    await session.commit()  # 提交所有Outbox状态、次数和错误信息更新。

    return published  # 返回本次成功发布的Outbox消息数量。


async def add_ai_call_log(
    session: AsyncSession,
    *,
    job: AsyncJob,
    provider: str,
    model_alias: str,
    model_name: str,
    started_at: datetime,
    status: str,
    provider_request_id: str | None = None,
    input_units: int | None = None,
    output_units: int | None = None,
    error_code: str | None = None,
    run_id: UUID | None = None,
    capability: str | None = None,
) -> None:
    """记录一次模型调用；不保存提示词和业务正文，避免日志泄露生产数据。"""
    duration_ms = max(0, int((datetime.now(UTC) - started_at).total_seconds() * 1000))
    session.add(
        AICallLog(
            job_id=job.id,
            run_id=run_id,
            capability=capability,
            provider=provider,
            model_alias=model_alias,
            model_name=model_name,
            provider_request_id=provider_request_id,
            duration_ms=duration_ms,
            input_units=input_units,
            output_units=output_units,
            status=status,
            error_code=error_code,
        )
    )


async def expire_upload_sessions(
    session: AsyncSession, *, now: datetime | None = None, batch_size: int = 200
) -> list[str]:
    """把已过期的待上传会话置为expired，并返回需要清理的对象键。"""
    current = now or datetime.now(UTC)
    rows = list(
        await session.scalars(
            select(UploadSession)
            .where(
                UploadSession.status == "pending",
                UploadSession.expires_at.is_not(None),
                UploadSession.expires_at <= current,
            )
            .order_by(UploadSession.expires_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
    )
    for row in rows:
        row.status = "expired"
    await session.commit()
    return [row.object_key for row in rows]


async def fail_stale_jobs(
    session: AsyncSession,
    *,
    stale_before: datetime,
    batch_size: int = 200,
) -> int:
    """终止长期没有心跳的运行任务，使失联Worker不会永久占用活动任务槽位。"""
    rows = list(
        await session.scalars(
            select(AsyncJob)
            .where(
                AsyncJob.status == "running",
                AsyncJob.heartbeat_at.is_not(None),
                AsyncJob.heartbeat_at < stale_before,
            )
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
    )
    for row in rows:
        await update_job(
            session,
            row,
            status="failed",
            progress=row.progress,
            message="worker heartbeat timed out",
            error_code="WORKER_HEARTBEAT_TIMEOUT",
        )
    await session.commit()
    return len(rows)
