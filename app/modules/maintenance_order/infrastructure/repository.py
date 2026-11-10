from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.maintenance_order.domain.models import WorkOrder
from app.shared.platform.models import WorkflowRun


class MaintenanceOrderRepository:
    """SQLAlchemy persistence adapter used by maintenance-order services."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_workflow(self, workflow_id: UUID) -> WorkflowRun | None:
        return await self.session.get(WorkflowRun, workflow_id)

    async def get_work_order(self, order_id: UUID) -> WorkOrder | None:
        return await self.session.get(WorkOrder, order_id)
