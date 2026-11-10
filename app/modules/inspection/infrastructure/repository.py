from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.inspection.domain.models import InspectionFinding, InspectionRecord


class InspectionRepository:
    """SQLAlchemy persistence adapter used by inspection application services."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_inspection(self, inspection_id: UUID) -> InspectionRecord | None:
        return await self.session.get(InspectionRecord, inspection_id)

    async def get_finding(self, finding_id: UUID) -> InspectionFinding | None:
        return await self.session.get(InspectionFinding, finding_id)
