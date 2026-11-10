import asyncio
from datetime import datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.bootstrap.celery_app import celery_app
from app.bootstrap.config import Settings
from app.modules.report.application.service import (
    create_report,
    digest_scope,
    execute_report_export,
    execute_report_generation,
    generate_report,
    get_report,
)
from app.modules.report.domain.models import OperationReport, ReportExport
from app.modules.report.domain.schemas import ReportCreate
from app.shared.platform.execution import ExecutionBusyError
from app.shared.platform.service import get_job
from app.shared.security.authorization.schemas import CurrentUser
from app.shared.security.identity.models import OrganizationUnit, UserAccount
from app.shared.worker_runtime import job_user, mark_job_failed, worker_resources

DAILY_REPORT_WORKER_ID = UUID("00000000-0000-0000-0000-000000000001")


async def _generate(job_id: str) -> str:
    async with worker_resources() as resources:
        async with resources.database.session_factory() as session:
            job = await get_job(session, UUID(job_id))
            try:
                report = await get_report(session, job.resource_id)
                await execute_report_generation(
                    session,
                    job=job,
                    report=report,
                    user=await job_user(session, job),
                    provider=resources.text,
                )
            except Exception as exc:
                await mark_job_failed(session, job, exc)
                raise
            return job.status


async def _export(job_id: str) -> str:
    async with worker_resources() as resources:
        async with resources.database.session_factory() as session:
            job = await get_job(session, UUID(job_id))
            try:
                export = await session.get(ReportExport, job.resource_id)
                if export is None:
                    raise ValueError(f"report export {job.resource_id} does not exist")
                report = await get_report(session, export.report_id)
                await execute_report_export(
                    session,
                    job=job,
                    report=report,
                    export=export,
                    user=await job_user(session, job),
                    storage=resources.storage,
                )
            except Exception as exc:
                await mark_job_failed(session, job, exc)
                raise
            return job.status


async def process_daily_reports(settings: Settings | None = None) -> int:
    business_date = datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)
    async with worker_resources(settings) as resources:
        async with resources.database.session_factory() as session:
            organizations = list(
                await session.scalars(
                    select(OrganizationUnit).where(OrganizationUnit.active.is_(True))
                )
            )
            if not organizations:
                return 0
            worker = await session.get(UserAccount, DAILY_REPORT_WORKER_ID)
            if worker is None:
                session.add(
                    UserAccount(
                        id=DAILY_REPORT_WORKER_ID,
                        external_issuer="internal://scheduler",
                        external_subject="system:daily-report",
                        username="daily-report-worker",
                        display_name="日报定时任务",
                        organization_unit_id=organizations[0].id,
                        active=True,
                        authz_version=1,
                        attributes={"account_type": "system"},
                    )
                )
                await session.flush()
            created = 0
            for organization in organizations:
                # Keep the lookup explicit so the scheduled task remains idempotent.
                existing = await session.scalar(
                    select(OperationReport).where(
                        OperationReport.report_type == "daily",
                        OperationReport.business_date == business_date,
                        OperationReport.organization_unit_id == organization.id,
                        OperationReport.scope_digest == digest_scope({}),
                    )
                )
                if existing is not None:
                    continue
                user = CurrentUser(
                    user_id=DAILY_REPORT_WORKER_ID,
                    subject="system:daily-report",
                    username="daily-report-worker",
                    display_name="日报定时任务",
                    organization_unit_id=organization.id,
                    roles={"system_worker"},
                    permissions={"*"},
                    organization_scope={organization.id},
                )
                report = await create_report(
                    session,
                    ReportCreate(
                        report_type="daily",
                        business_date=business_date,
                        timezone="Asia/Shanghai",
                        scope_filter={},
                    ),
                    user,
                )
                await generate_report(
                    session,
                    report=report,
                    user=user,
                    provider=resources.text,
                    inline=True,
                )
                created += 1
            return created


@celery_app.task(
    autoretry_for=(ExecutionBusyError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 30},
    name="app.modules.report.generate_report",
    acks_late=True,
)
def generate_report_task(job_id: str) -> dict[str, str]:
    return {"job_id": job_id, "status": asyncio.run(_generate(job_id))}


@celery_app.task(
    autoretry_for=(ExecutionBusyError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 30},
    name="app.modules.report.export_report",
    acks_late=True,
)
def export_report_task(job_id: str) -> dict[str, str]:
    return {"job_id": job_id, "status": asyncio.run(_export(job_id))}


@celery_app.task(
    autoretry_for=(ExecutionBusyError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 30},
    name="app.modules.report.generate_daily_reports",
    acks_late=True,
)
def generate_daily_reports_task() -> dict[str, str]:
    return {"status": "ok", "created": str(asyncio.run(process_daily_reports()))}
