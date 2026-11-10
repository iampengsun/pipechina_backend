from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.report.domain.models import OperationReport


class ReportRepository:
    """SQLAlchemy persistence adapter used by reporting application services."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_report(self, report_id: UUID) -> OperationReport | None:
        return await self.session.get(OperationReport, report_id)
