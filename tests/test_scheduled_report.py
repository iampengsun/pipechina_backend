import asyncio

from sqlalchemy import func, select

from app.bootstrap.config import Settings
from app.modules.report.application.tasks import process_daily_reports
from app.modules.report.domain.models import OperationReport, ReportVersion
from app.shared.db import Database
from app.shared.platform.models import AsyncJob
from app.shared.security.identity.models import OrganizationUnit


async def prepare_database(settings: Settings) -> None:
    database = Database(settings.database_url)
    try:
        await database.create_all()
        async with database.session_factory() as session:
            session.add(
                OrganizationUnit(
                    code="TEST-STATION",
                    name="测试输气站",
                    unit_type="station",
                    path="/TEST-STATION",
                    active=True,
                )
            )
            await session.commit()
    finally:
        await database.dispose()


async def scheduled_counts(settings: Settings) -> tuple[int, int, int]:
    database = Database(settings.database_url)
    try:
        async with database.session_factory() as session:
            reports = await session.scalar(select(func.count(OperationReport.id)))
            versions = await session.scalar(select(func.count(ReportVersion.id)))
            successful_jobs = await session.scalar(
                select(func.count(AsyncJob.id)).where(AsyncJob.status == "succeeded")
            )
            return int(reports or 0), int(versions or 0), int(successful_jobs or 0)
    finally:
        await database.dispose()


def test_daily_report_scheduler_is_idempotent(settings: Settings) -> None:
    asyncio.run(prepare_database(settings))
    assert asyncio.run(process_daily_reports(settings)) == 1
    assert asyncio.run(process_daily_reports(settings)) == 0
    assert asyncio.run(scheduled_counts(settings)) == (1, 1, 1)
