from __future__ import annotations

# 为Celery任务创建独立数据库会话、Provider和最小权限执行身份。
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from app.bootstrap.config import Settings, get_settings
from app.bootstrap.knowledge import KnowledgeResourceManager
from app.bootstrap.providers import create_provider_bundle
from app.bootstrap.site_context import create_site_context
from app.ports.speech import SpeechToTextProvider
from app.ports.storage import StorageProvider
from app.ports.text import TextLLMProvider
from app.ports.vision import VisionProvider
from app.shared.db import Database
from app.shared.errors import AppError
from app.shared.platform.models import AsyncJob
from app.shared.platform.service import update_job
from app.shared.security.authentication.schemas import AuthenticatedIdentity
from app.shared.security.authorization.permissions import Permissions
from app.shared.security.authorization.repository import load_current_user
from app.shared.security.authorization.schemas import CurrentUser
from app.shared.security.identity.models import OrganizationUnit, UserAccount


# 异步任务交给 Worker 执行，此处统一管理执行时需要的资源。
class WorkerResources:
    """集中管理单次Worker执行依赖的基础资源。"""

    def __init__(self, settings: Settings) -> None:
        """按Worker配置创建数据库和Provider资源。"""

        self.settings = settings
        self.database = Database(settings.database_url)
        self.providers = create_provider_bundle(settings)
        self.knowledge = KnowledgeResourceManager(settings)
        self.site_context = create_site_context(settings)

    @property
    def text(self) -> TextLLMProvider:
        """返回文字模型Provider。"""

        return self.providers.text

    @property
    def asr(self) -> SpeechToTextProvider:
        """返回语音识别Provider。"""

        return self.providers.asr

    @property
    def vision(self) -> VisionProvider:
        """返回视觉分析Provider。"""

        return self.providers.vision

    @property
    def storage(self) -> StorageProvider:
        """返回文件存储Provider。"""

        return self.providers.storage

    async def close(self) -> None:
        """按依赖顺序关闭Worker持有的全部资源。"""

        try:
            try:
                await self.site_context.close()
            finally:
                await self.knowledge.close()
        finally:
            try:
                await self.providers.close()
            finally:
                await self.database.dispose()


@asynccontextmanager
async def worker_resources(settings: Settings | None = None) -> AsyncIterator[WorkerResources]:
    """为一次Worker执行创建并自动释放资源。"""

    resources = WorkerResources(settings or get_settings())
    try:
        yield resources
    finally:
        await resources.close()


async def job_user(session: AsyncSession, job: AsyncJob) -> CurrentUser:
    """重新加载任务发起人的当前权限用于执行时复核。"""

    permission_by_job = {
        "audio_transcription": Permissions.HANDOVER_PROCESS,
        "handover_summary": Permissions.HANDOVER_PROCESS,
        "meeting_transcription": Permissions.MEETING_PROCESS,
        "meeting_minutes": Permissions.MEETING_PROCESS,
        "event_extraction": Permissions.EVENT_EXTRACT,
        "event_classification": Permissions.EVENT_CLASSIFY,
        "prompt_optimization": Permissions.ADMIN_PROMPT,
        "experience_consolidation": Permissions.EVENT_CLASSIFY,
        "inspection_image_analysis": Permissions.INSPECTION_ANALYZE,
        "report_generation": Permissions.REPORT_GENERATE,
        "report_export": Permissions.REPORT_EXPORT,
        "knowledge_parse": Permissions.KNOWLEDGE_INDEX,
        "knowledge_index": Permissions.KNOWLEDGE_INDEX,
        "knowledge_evaluate": Permissions.KNOWLEDGE_EVALUATE,
        "knowledge_answer": Permissions.KNOWLEDGE_READ,
    }
    permission = permission_by_job.get(job.job_type)
    if permission is None:
        raise ValueError(f"no worker permission mapping for job type {job.job_type}")
    account = await session.get(UserAccount, job.requested_by, populate_existing=True)
    if account is None or not account.active:
        raise AppError("ACCOUNT_DISABLED", "task requester is unavailable", 403)
    organization = await session.get(
        OrganizationUnit, account.organization_unit_id, populate_existing=True
    )
    if organization is None or not organization.active:
        raise AppError("ORGANIZATION_DISABLED", "task requester organization is unavailable", 403)
    if (
        account.external_issuer == "internal://scheduler"
        and account.external_subject == "system:daily-report"
        and job.job_type == "report_generation"
    ):
        target_org = await session.get(
            OrganizationUnit, job.organization_unit_id, populate_existing=True
        )
        if target_org is None or not target_org.active:
            raise AppError("ORGANIZATION_DISABLED", "task organization is unavailable", 403)
        return CurrentUser(
            user_id=account.id,
            subject=account.external_subject,
            username=account.username,
            display_name=account.display_name,
            organization_unit_id=job.organization_unit_id,
            permissions={Permissions.REPORT_GENERATE},
            organization_scope={job.organization_unit_id},
        )
    user = await load_current_user(
        session,
        AuthenticatedIdentity(
            user_id=account.id,
            issuer=account.external_issuer,
            subject=account.external_subject,
            username=account.username,
            display_name=account.display_name,
            organization_unit_id=account.organization_unit_id,
            authz_version=account.authz_version,
        ),
    )
    if not user.has_permission(permission):
        raise AppError("PERMISSION_DENIED", "task permission was revoked", 403)  # 权限已撤销
    return user


async def mark_job_failed(session: AsyncSession, job: AsyncJob, exc: Exception) -> None:
    """在Celery重投前保存经过脱敏的失败状态。"""
    await session.rollback()
    await session.refresh(job)
    if job.execution_token is not None:
        return  # Lease-managed executors exclusively own their failure state.
    if job.status in {"succeeded", "failed", "cancelled"}:
        return
    await update_job(
        session,
        job,
        status="failed",
        progress=job.progress,
        message="worker execution failed",
        error_code=type(exc).__name__,
        error_detail=str(exc)[:2000],
    )
    await session.commit()
