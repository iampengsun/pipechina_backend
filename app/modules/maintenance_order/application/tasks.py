import asyncio

from app.bootstrap.celery_app import celery_app
from app.shared.worker_runtime import worker_resources


async def _check_reminders() -> int:
    from app.modules.maintenance_order.application.reminders import refresh_reminders

    async with worker_resources() as resources:
        async with resources.database.session_factory() as session:
            return await refresh_reminders(session)


@celery_app.task(name="app.modules.maintenance_order.check_reminders", acks_late=True)
def check_reminders_task() -> dict[str, str]:
    return {"status": "ok", "created": str(asyncio.run(_check_reminders()))}
