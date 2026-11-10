from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bootstrap.config import Settings
from app.modules.knowledge.application import service as knowledge_service
from app.modules.knowledge.domain.models import KnowledgeDocument, KnowledgeVersion
from app.modules.knowledge.domain.schemas import DocumentCreate, VersionCreate
from app.modules.meeting.domain.models import (
    Meeting,
    MeetingKnowledgePublication,
    MeetingMinutesVersion,
    MeetingParticipant,
    MeetingTranscriptSegment,
    MeetingTranscriptVersion,
)
from app.modules.meeting.domain.schemas import (
    MeetingCreate,
    MeetingMinutes,
    MinutesUpdate,
    TranscriptUpdate,
)
from app.ports.models import MediaRef
from app.ports.speech import SpeechToTextProvider
from app.ports.storage import StorageProvider
from app.ports.text import TextLLMProvider
from app.shared.errors import AppError, ConflictError, NotFoundError, PermissionDeniedError
from app.shared.media.integrity import normalize_sha256
from app.shared.media.names import safe_object_filename
from app.shared.platform.leased_jobs import leased_job
from app.shared.platform.models import AsyncJob, UploadSession
from app.shared.platform.runtime import (
    asr_provider_for_job,
    build_runtime_snapshot,
    business_payload,
    render_user_prompt,
    system_prompt,
    text_provider_for_job,
)
from app.shared.platform.service import add_audit, create_job, update_job
from app.shared.security.authorization.dependencies import require_data_scope
from app.shared.security.authorization.permissions import Permissions as P
from app.shared.security.authorization.schemas import CurrentUser


def _check_acl(meeting: Meeting, user: CurrentUser, permission: str) -> None:
    require_data_scope(user, meeting.organization_unit_id, permission, owner_id=meeting.created_by)
    if user.has_global_permission(P.ALL) or user.user_id == meeting.created_by:
        return
    if meeting.reader_ids and str(user.user_id) not in meeting.reader_ids:
        raise PermissionDeniedError(permission)


async def get_meeting(session: AsyncSession, meeting_id: UUID) -> Meeting:
    meeting = await session.get(Meeting, meeting_id, populate_existing=True)
    if meeting is None:
        raise NotFoundError("meeting", meeting_id)
    return meeting


async def create_meeting(
    session: AsyncSession,
    *,
    payload: MeetingCreate,
    user: CurrentUser,
    storage: StorageProvider,
    settings: Settings,
):
    org_id = payload.organization_unit_id or user.organization_unit_id
    require_data_scope(user, org_id, P.MEETING_CREATE, owner_id=user.user_id)
    if payload.size_bytes > settings.max_audio_bytes:
        raise AppError("FILE_TOO_LARGE", "meeting recording exceeds configured size limit", 413)
    if payload.mime_type.lower() not in settings.allowed_audio_types:
        raise AppError("UNSUPPORTED_MEDIA_TYPE", "audio MIME type is not allowed", 415)
    meeting = Meeting(
        organization_unit_id=org_id,
        title=payload.title,
        started_at=payload.started_at,
        ended_at=payload.ended_at,
        location=payload.location,
        filename=payload.filename,
        object_key="pending",
        mime_type=payload.mime_type,
        size_bytes=payload.size_bytes,
        reader_ids=[str(value) for value in payload.reader_ids],
        created_by=user.user_id,
    )
    session.add(meeting)
    await session.flush()
    meeting.object_key = f"meetings/{org_id}/{meeting.id}/{safe_object_filename(payload.filename)}"
    for item in payload.participants:
        session.add(MeetingParticipant(meeting_id=meeting.id, **item.model_dump()))
    grant = await storage.create_upload(
        object_key=meeting.object_key, mime_type=meeting.mime_type, size_bytes=meeting.size_bytes
    )
    upload = UploadSession(
        resource_type="meeting",
        resource_id=meeting.id,
        organization_unit_id=org_id,
        object_key=meeting.object_key,
        filename=meeting.filename,
        mime_type=meeting.mime_type,
        size_bytes=meeting.size_bytes,
        client_sha256=payload.sha256,
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(seconds=grant.expires_in),
    )
    session.add(upload)
    await add_audit(
        session, user=user, action="meeting.create", resource_type="meeting", resource_id=meeting.id
    )
    await session.commit()
    return meeting, grant, upload.id


async def complete_upload(
    session: AsyncSession,
    *,
    meeting: Meeting,
    user: CurrentUser,
    storage: StorageProvider,
    server_sha256: str | None,
):
    _check_acl(meeting, user, P.MEETING_PROCESS)
    await session.refresh(meeting, with_for_update=True)
    upload = await session.scalar(
        select(UploadSession)
        .where(UploadSession.resource_type == "meeting", UploadSession.resource_id == meeting.id)
        .with_for_update()
    )
    if upload is None or upload.status != "pending":
        raise ConflictError("meeting upload session is not pending")
    expires = upload.expires_at
    if expires and (expires if expires.tzinfo else expires.replace(tzinfo=UTC)) <= datetime.now(
        UTC
    ):
        upload.status = "expired"
        raise ConflictError("meeting upload session has expired")
    meta = await storage.head(meeting.object_key)
    if meta.size_bytes != meeting.size_bytes or meta.mime_type != meeting.mime_type:
        raise ConflictError("uploaded meeting metadata mismatch")
    trusted = normalize_sha256(meta.checksum)
    reported = normalize_sha256(server_sha256)
    if trusted and reported and trusted != reported:
        raise ConflictError("reported checksum differs from storage checksum")
    if upload.client_sha256 and (not trusted or upload.client_sha256.lower() != trusted):
        raise ConflictError("uploaded meeting checksum differs from declared checksum")
    upload.status, upload.server_sha256, meeting.upload_status = "verified", trusted, "verified"
    await add_audit(
        session,
        user=user,
        action="meeting.upload_verified",
        resource_type="meeting",
        resource_id=meeting.id,
    )
    await session.commit()


async def delete_recording(
    session: AsyncSession,
    *,
    meeting: Meeting,
    storage: StorageProvider,
    reason: str,
    user: CurrentUser | None = None,
) -> Meeting:
    """先标记删除中，再删除对象；失败可安全重试，逐字稿继续保留。"""
    if user is not None:
        _check_acl(meeting, user, P.MEETING_EDIT)
    await session.refresh(meeting, with_for_update=True)
    if meeting.upload_status == "deleted":
        return meeting
    if meeting.upload_status not in {"verified", "deleting"}:
        raise ConflictError("meeting recording is not ready for deletion")
    active_job = await session.scalar(
        select(AsyncJob.id).where(
            AsyncJob.resource_type == "meeting",
            AsyncJob.resource_id == meeting.id,
            AsyncJob.status.in_(["queued", "running"]),
        )
    )
    if active_job is not None:
        raise ConflictError("meeting has an active processing job")
    publications = list(
        await session.scalars(
            select(MeetingKnowledgePublication).where(
                MeetingKnowledgePublication.meeting_id == meeting.id
            )
        )
    )
    for publication in publications:
        version = await session.get(KnowledgeVersion, publication.knowledge_version_id)
        if version is not None and version.status not in {"withdrawn", "rejected"}:
            raise ConflictError("withdraw linked knowledge before deleting recording")
    meeting.upload_status = "deleting"
    meeting.recording_delete_reason = reason
    await session.commit()
    await storage.delete(meeting.object_key)
    meeting.upload_status = "deleted"
    meeting.recording_deleted_at = datetime.now(UTC)
    if user is not None:
        await add_audit(
            session,
            user=user,
            action="meeting.recording.delete",
            resource_type="meeting",
            resource_id=meeting.id,
            reason=reason,
        )
    await session.commit()
    return meeting


async def start_transcription(
    session: AsyncSession,
    *,
    meeting: Meeting,
    user: CurrentUser,
    provider: SpeechToTextProvider,
    inline: bool,
):
    _check_acl(meeting, user, P.MEETING_PROCESS)
    if meeting.upload_status != "verified":
        raise ConflictError("meeting recording must be verified before transcription")
    if meeting.status in {"published", "withdrawn"}:
        raise ConflictError("published meeting transcript cannot be replaced")
    job = await create_job(
        session,
        user=user,
        job_type="meeting_transcription",
        resource_type="meeting",
        resource_id=meeting.id,
        task_name="app.modules.meeting.transcribe",
        queue="audio",
        config_snapshot=await build_runtime_snapshot(
            session, job_type="audio_transcription", provider=provider
        ),
        enqueue=not inline,
    )
    if inline:
        await execute_transcription(session, job=job, meeting=meeting, user=user, provider=provider)
    else:
        await session.commit()
    return job.id


@leased_job
async def execute_transcription(
    session: AsyncSession,
    *,
    job: AsyncJob,
    meeting: Meeting,
    user: CurrentUser,
    provider: SpeechToTextProvider,
):
    if job.status in {"succeeded", "failed", "cancelled"}:
        return
    if job.cancel_requested:
        await update_job(session, job, status="cancelled", progress=job.progress)
        await session.commit()
        return
    _check_acl(meeting, user, P.MEETING_PROCESS)
    await update_job(session, job, status="running", progress=10, message="transcribing meeting")
    await session.commit()
    provider = asr_provider_for_job(session, job, provider)
    hotwords = business_payload(job.config_snapshot, "asr.hotwords")
    try:
        result = await provider.transcribe(
            MediaRef(
                object_key=meeting.object_key,
                mime_type=meeting.mime_type,
                size_bytes=meeting.size_bytes,
                filename=meeting.filename,
            ),
            hotwords=[str(value) for value in hotwords.get("hotwords", [])],
            language=str(hotwords.get("language", "zh")),
        )
    except Exception as exc:
        await update_job(
            session,
            job,
            status="failed",
            progress=job.progress,
            message="meeting transcription failed",
            error_code=type(exc).__name__,
            error_detail=str(exc)[:2000],
        )
        await session.commit()
        raise
    await session.refresh(meeting, with_for_update=True)
    if meeting.status in {"published", "withdrawn"}:
        raise ConflictError("meeting changed while transcription was running")
    number = (
        await session.scalar(
            select(func.max(MeetingTranscriptVersion.version)).where(
                MeetingTranscriptVersion.meeting_id == meeting.id
            )
        )
        or 0
    ) + 1
    version = MeetingTranscriptVersion(
        meeting_id=meeting.id,
        version=number,
        source="ai",
        full_text=result.full_text,
        language=result.language,
        provider_request_id=result.provider_request_id,
        created_by=user.user_id,
    )
    session.add(version)
    await session.flush()
    for item in result.segments:
        session.add(
            MeetingTranscriptSegment(
                transcript_version_id=version.id,
                segment_index=item.index,
                start_ms=item.start_ms,
                end_ms=item.end_ms,
                text=item.text,
                speaker_label=item.speaker_label,
                confidence=item.confidence,
            )
        )
    meeting.duration_ms = result.duration_ms
    meeting.current_transcript_version_id = version.id
    meeting.status = "transcribed"
    await update_job(
        session, job, status="succeeded", progress=100, message="meeting transcript persisted"
    )
    await session.commit()


async def update_transcript(
    session: AsyncSession, *, meeting: Meeting, payload: TranscriptUpdate, user: CurrentUser
):
    _check_acl(meeting, user, P.MEETING_EDIT)
    await session.refresh(meeting, with_for_update=True)
    if meeting.status in {"published", "withdrawn"}:
        raise ConflictError("published meeting transcript is immutable")
    if (
        meeting.duration_ms
        and payload.segments
        and payload.segments[-1].end_ms > meeting.duration_ms
    ):
        raise ConflictError("transcript segment exceeds recording duration")
    number = (
        await session.scalar(
            select(func.max(MeetingTranscriptVersion.version)).where(
                MeetingTranscriptVersion.meeting_id == meeting.id
            )
        )
        or 0
    ) + 1
    version = MeetingTranscriptVersion(
        meeting_id=meeting.id,
        version=number,
        source="manual",
        full_text=payload.full_text,
        created_by=user.user_id,
    )
    session.add(version)
    await session.flush()
    for index, item in enumerate(payload.segments):
        session.add(
            MeetingTranscriptSegment(
                transcript_version_id=version.id, segment_index=index, **item.model_dump()
            )
        )
    meeting.current_transcript_version_id, meeting.status = version.id, "transcribed"
    await add_audit(
        session,
        user=user,
        action="meeting.transcript.update",
        resource_type="meeting",
        resource_id=meeting.id,
    )
    await session.commit()
    return version


async def latest_minutes(session: AsyncSession, meeting: Meeting) -> MeetingMinutesVersion | None:
    if meeting.current_transcript_version_id is None:
        return None
    return await session.scalar(
        select(MeetingMinutesVersion)
        .where(
            MeetingMinutesVersion.meeting_id == meeting.id,
            MeetingMinutesVersion.transcript_version_id == meeting.current_transcript_version_id,
        )
        .order_by(MeetingMinutesVersion.version.desc())
        .limit(1)
    )


async def start_minutes_generation(
    session: AsyncSession,
    *,
    meeting: Meeting,
    user: CurrentUser,
    provider: TextLLMProvider,
    inline: bool,
) -> UUID:
    _check_acl(meeting, user, P.MEETING_PROCESS)
    if meeting.current_transcript_version_id is None:
        raise ConflictError("meeting has no transcript")
    job = await create_job(
        session,
        user=user,
        job_type="meeting_minutes",
        resource_type="meeting",
        resource_id=meeting.id,
        task_name="app.modules.meeting.generate_minutes",
        queue="ai_text",
        config_snapshot=await build_runtime_snapshot(
            session,
            job_type="meeting_minutes",
            provider=provider,
            base={"transcript_version_id": str(meeting.current_transcript_version_id)},
        ),
        enqueue=not inline,
    )
    if inline:
        await execute_minutes_generation(
            session, job=job, meeting=meeting, user=user, provider=provider
        )
    else:
        await session.commit()
    return job.id


@leased_job
async def execute_minutes_generation(
    session: AsyncSession,
    *,
    job: AsyncJob,
    meeting: Meeting,
    user: CurrentUser,
    provider: TextLLMProvider,
) -> None:
    if job.status in {"succeeded", "failed", "cancelled"}:
        return
    if job.cancel_requested:
        await update_job(session, job, status="cancelled", progress=job.progress)
        await session.commit()
        return
    transcript_id = UUID(job.config_snapshot["transcript_version_id"])
    _check_acl(meeting, user, P.MEETING_PROCESS)
    transcript = await session.get(MeetingTranscriptVersion, transcript_id)
    if transcript is None or transcript.meeting_id != meeting.id:
        raise ConflictError("meeting minutes source transcript is missing")
    await update_job(session, job, status="running", progress=20, message="generating minutes")
    await session.commit()
    provider = text_provider_for_job(session, job, provider)
    minutes = await provider.generate_structured(
        operation="meeting_minutes",
        system_prompt=system_prompt(job.config_snapshot),
        user_prompt=render_user_prompt(job.config_snapshot, transcript.full_text),
        response_model=MeetingMinutes,
    )
    await session.refresh(meeting, with_for_update=True)
    if meeting.current_transcript_version_id != transcript_id:
        raise ConflictError("meeting transcript changed during minutes generation")
    number = (
        await session.scalar(
            select(func.max(MeetingMinutesVersion.version)).where(
                MeetingMinutesVersion.meeting_id == meeting.id
            )
        )
        or 0
    ) + 1
    session.add(
        MeetingMinutesVersion(
            meeting_id=meeting.id,
            transcript_version_id=transcript_id,
            version=number,
            source="ai",
            content=minutes.model_dump(mode="json"),
            review_status="draft",
            created_by=user.user_id,
        )
    )
    await update_job(
        session, job, status="succeeded", progress=100, message="minutes draft persisted"
    )
    await session.commit()


async def update_minutes(
    session: AsyncSession, *, meeting: Meeting, payload: MinutesUpdate, user: CurrentUser
) -> MeetingMinutesVersion:
    _check_acl(meeting, user, P.MEETING_EDIT)
    await session.refresh(meeting, with_for_update=True)
    if meeting.current_transcript_version_id is None:
        raise ConflictError("meeting has no transcript")
    number = (
        await session.scalar(
            select(func.max(MeetingMinutesVersion.version)).where(
                MeetingMinutesVersion.meeting_id == meeting.id
            )
        )
        or 0
    ) + 1
    version = MeetingMinutesVersion(
        meeting_id=meeting.id,
        transcript_version_id=meeting.current_transcript_version_id,
        version=number,
        source="manual",
        content=payload.content.model_dump(mode="json"),
        review_status="draft",
        created_by=user.user_id,
    )
    session.add(version)
    await session.commit()
    return version


async def confirm_minutes(
    session: AsyncSession, *, meeting: Meeting, user: CurrentUser
) -> MeetingMinutesVersion:
    _check_acl(meeting, user, P.MEETING_EDIT)
    await session.refresh(meeting, with_for_update=True)
    version = await latest_minutes(session, meeting)
    if version is None:
        raise ConflictError("meeting has no minutes draft for current transcript")
    if version.review_status != "confirmed":
        version.review_status = "confirmed"
        version.confirmed_by = user.user_id
        await add_audit(
            session,
            user=user,
            action="meeting.minutes.confirm",
            resource_type="meeting",
            resource_id=meeting.id,
        )
        await session.commit()
    return version


async def render_transcript(
    session: AsyncSession, meeting: Meeting
) -> tuple[MeetingTranscriptVersion, bytes]:
    if meeting.current_transcript_version_id is None:
        raise ConflictError("meeting has no transcript")
    version = await session.get(MeetingTranscriptVersion, meeting.current_transcript_version_id)
    if version is None:
        raise ConflictError("current meeting transcript does not exist")
    segments = list(
        await session.scalars(
            select(MeetingTranscriptSegment)
            .where(MeetingTranscriptSegment.transcript_version_id == version.id)
            .order_by(MeetingTranscriptSegment.segment_index)
        )
    )
    participants = list(
        await session.scalars(
            select(MeetingParticipant).where(MeetingParticipant.meeting_id == meeting.id)
        )
    )
    names = {item.participant_key: item.display_name for item in participants}
    lines = [
        f"# {meeting.title}",
        "",
        f"- 开始时间：{meeting.started_at.isoformat()}",
        f"- 地点：{meeting.location or '未记录'}",
        "",
        "## 参会人员",
    ]
    lines.extend(f"- {p.display_name}" + (f"（{p.role}）" if p.role else "") for p in participants)
    lines.extend(["", "## 逐字稿"])
    for item in segments:
        speaker = names.get(item.speaker_label or "", item.speaker_label or "未标注说话人")
        lines.append(
            f"[{item.start_ms / 1000:.3f}s–{item.end_ms / 1000:.3f}s] {speaker}：{item.text}"
        )
    if not segments:
        lines.extend(["", version.full_text])
    return version, ("\n".join(lines) + "\n").encode()


async def prepare_knowledge_publication(
    session: AsyncSession,
    *,
    meeting: Meeting,
    base_id: UUID,
    effective_from: datetime | None,
    authority: str,
    user: CurrentUser,
    settings: Settings,
    storage: StorageProvider,
):
    _check_acl(meeting, user, P.MEETING_PUBLISH)
    for permission in (P.KNOWLEDGE_EDIT, P.KNOWLEDGE_UPLOAD, P.KNOWLEDGE_INDEX):
        if not user.has_permission(permission):
            raise PermissionDeniedError(permission)
    await session.refresh(meeting, with_for_update=True)
    transcript, content = await render_transcript(session, meeting)
    existing = await session.scalar(
        select(MeetingKnowledgePublication).where(
            MeetingKnowledgePublication.meeting_id == meeting.id,
            MeetingKnowledgePublication.transcript_version_id == transcript.id,
        )
    )
    if existing:
        if existing.index_job_id is not None:
            raise ConflictError(
                "current meeting transcript is already prepared for knowledge publication"
            )
        version = await session.get(KnowledgeVersion, existing.knowledge_version_id)
        if version is None:
            raise ConflictError("prepared knowledge version no longer exists")
        return existing, version
    # Knowledge publication must stay within the meeting's organization.
    from app.modules.knowledge.application.access import base_access

    base = await base_access(session, base_id, user, P.KNOWLEDGE_EDIT)
    if base.organization_unit_id != meeting.organization_unit_id:
        raise ConflictError("meeting knowledge must stay in the source organization")
    code = f"MEETING-{meeting.id}"
    document = await session.scalar(
        select(KnowledgeDocument).where(
            KnowledgeDocument.base_id == base_id, KnowledgeDocument.code == code
        )
    )
    if document is None:
        document = await knowledge_service.create_document(
            session,
            user,
            DocumentCreate(base_id=base_id, code=code, title=meeting.title, kind="handover"),
        )
    await session.refresh(document, with_for_update=True)
    if meeting.reader_ids:
        allowed = set(meeting.reader_ids) | {str(meeting.created_by)}
        # An existing restricted document may only be narrowed, never widened.
        if document.reader_ids:
            allowed &= set(document.reader_ids)
        if not allowed:
            raise ConflictError("meeting and knowledge reader scopes do not overlap")
        document.reader_ids = sorted(allowed)
        document.acl_version += 1
        await session.flush()
    digest = hashlib.sha256(content).hexdigest()
    version, _ = await knowledge_service.create_version(
        session,
        user,
        document.id,
        VersionCreate(
            filename=f"meeting-{meeting.id}-v{transcript.version}.md",
            mime_type="text/markdown",
            size_bytes=len(content),
            sha256=digest,
            effective_from=effective_from or meeting.started_at,
            equipment_models=[],
            authority=authority,
        ),
        settings,
        storage,
    )
    await storage.put_bytes(version.object_key, content, version.mime_type)
    await knowledge_service.complete_upload(session, user, version.id, storage)
    meeting.status, meeting.published_by = "published", user.user_id
    publication = MeetingKnowledgePublication(
        meeting_id=meeting.id,
        transcript_version_id=transcript.id,
        knowledge_base_id=base_id,
        knowledge_document_id=document.id,
        knowledge_version_id=version.id,
        index_job_id=None,
        created_by=user.user_id,
    )
    session.add(publication)
    await add_audit(
        session,
        user=user,
        action="meeting.publish_to_knowledge",
        resource_type="meeting",
        resource_id=meeting.id,
        after={"knowledge_version_id": str(version.id)},
    )
    await session.commit()
    return publication, version


async def update_acl(session, meeting, payload, user):
    from app.shared.security.identity.models import UserAccount

    await session.refresh(meeting, with_for_update=True)
    _check_acl(meeting, user, P.MEETING_EDIT)
    if meeting.created_by != user.user_id and not user.has_global_permission(P.ALL):
        raise PermissionDeniedError(P.MEETING_EDIT)
    if meeting.acl_version != payload.expected_version:
        raise ConflictError("meeting ACL version mismatch")
    if len(set(payload.reader_ids)) != len(payload.reader_ids):
        raise ConflictError("duplicate meeting readers")
    for key in payload.reader_ids:
        account = await session.get(UserAccount, key, populate_existing=True)
        if (
            not account
            or not account.active
            or account.organization_unit_id != meeting.organization_unit_id
        ):
            raise ConflictError(
                "meeting readers must be active accounts in the meeting organization"
            )
    previous = list(meeting.reader_ids)
    meeting.reader_ids = sorted(str(key) for key in payload.reader_ids)
    meeting.acl_version += 1
    documents = await session.scalars(
        select(KnowledgeDocument)
        .join(
            MeetingKnowledgePublication,
            MeetingKnowledgePublication.knowledge_document_id == KnowledgeDocument.id,
        )
        .where(MeetingKnowledgePublication.meeting_id == meeting.id)
        .order_by(KnowledgeDocument.id)
        .with_for_update(of=KnowledgeDocument)
    )
    seen = set()
    for document in documents:
        if document.id in seen:
            continue
        seen.add(document.id)
        document.acl_version += 1
    await add_audit(
        session,
        user=user,
        action="meeting.acl.update",
        resource_type="meeting",
        resource_id=meeting.id,
        before={"reader_ids": previous},
        after={
            "reader_ids": meeting.reader_ids,
            "acl_version": meeting.acl_version,
            "invalidated_documents": [str(key) for key in seen],
        },
        reason=payload.reason,
    )
    await session.commit()
    return meeting
