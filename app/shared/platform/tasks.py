import asyncio
from datetime import UTC, datetime, timedelta

from app.bootstrap.celery_app import celery_app
from app.shared.platform.service import (
    expire_upload_sessions,
    fail_stale_jobs,
    publish_pending_outbox,
)
from app.shared.worker_runtime import worker_resources


# 平台定时任务：发布事务Outbox，并清理过期上传和失去心跳的任务。
# Outbox可以理解成先把准备发送的消息可靠地记到数据库里，再由后台任务慢慢发送到 RabbitMQ/Celery。
async def _publish() -> int:
    """创建Worker资源，并发布当前批次中可以投递的Outbox消息。"""
    async with worker_resources() as resources:
        async with resources.database.session_factory() as session:
            # 发送函数作为参数传入，服务层无需直接依赖Celery。
            return await publish_pending_outbox(
                session,
                celery_app.send_task,
                max_attempts=resources.settings.outbox_max_attempts,
                base_retry_seconds=resources.settings.outbox_base_retry_seconds,
            )


async def _maintain() -> dict[str, int]:
    """清理过期上传，并终止超过心跳期限的运行任务。"""
    async with worker_resources() as resources:
        async with resources.database.session_factory() as session:
            object_keys = await expire_upload_sessions(session)

        # 数据库状态优先完成；对象删除失败时留待后续存储清理处理。
        deleted = 0
        for object_key in object_keys:
            try:
                await resources.storage.delete(object_key)
                deleted += 1
            except Exception:
                continue

        async with resources.database.session_factory() as session:
            stale_jobs = await fail_stale_jobs(
                session,
                stale_before=datetime.now(UTC)
                - timedelta(seconds=resources.settings.job_heartbeat_timeout_seconds),
            )
        return {
            "expired_uploads": len(object_keys),
            "deleted_objects": deleted,
            "stale_jobs": stale_jobs,
        }


@celery_app.task(name="app.shared.platform.publish_outbox")
def publish_outbox() -> dict[str, str]:
    """供Celery Beat调用的同步入口，每秒发布一次待处理消息。"""
    published = asyncio.run(_publish())
    return {"status": "ok", "published": str(published)}


@celery_app.task(name="app.shared.platform.maintain_runtime")
def maintain_runtime() -> dict[str, int]:
    """供Celery Beat调用的同步平台维护入口。"""
    return asyncio.run(_maintain())
