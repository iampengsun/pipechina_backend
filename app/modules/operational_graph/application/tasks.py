"""Refresh the business projection and detect time-based knowledge expiry."""

import asyncio

from app.bootstrap.celery_app import celery_app
from app.modules.operational_graph.application.projection import synchronize
from app.modules.operational_graph.application.queries import graph_client
from app.shared.worker_runtime import worker_resources


async def synchronize_business_graph():
    async with worker_resources() as resources:
        return await synchronize(
            resources.database.session_factory, graph_client(resources.knowledge)
        )


@celery_app.task(name="app.modules.operational_graph.synchronize")
def synchronize_task():
    return asyncio.run(synchronize_business_graph())
