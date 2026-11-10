from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from app.modules.knowledge.application.jobs import start_knowledge_job
from app.modules.meeting.application.service import (
    _check_acl,
    complete_upload,
    confirm_minutes,
    create_meeting,
    delete_recording,
    get_meeting,
    latest_minutes,
    prepare_knowledge_publication,
    start_minutes_generation,
    start_transcription,
    update_minutes,
    update_transcript,
)
from app.modules.meeting.domain.models import (
    Meeting,
    MeetingParticipant,
    MeetingTranscriptSegment,
    MeetingTranscriptVersion,
)
from app.modules.meeting.domain.schemas import (
    KnowledgePublicationCreate,
    MeetingACLUpdate,
    MeetingCreate,
    MeetingView,
    MinutesUpdate,
    RecordingDelete,
    TranscriptUpdate,
    UploadComplete,
)
from app.shared.db import SessionDep
from app.shared.errors import ConflictError
from app.shared.platform.models import AsyncJob
from app.shared.security.authorization.dependencies import data_scope_clause, require_permission
from app.shared.security.authorization.permissions import Permissions as P
from app.shared.security.authorization.schemas import CurrentUser

router = APIRouter(prefix="/meetings", tags=["meetings"])


@router.post("/{meeting_id}:generate-minutes", status_code=202)
async def post_generate_minutes(
    meeting_id: UUID,
    request: Request,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(P.MEETING_PROCESS))],
):
    job_id = await start_minutes_generation(
        session,
        meeting=await get_meeting(session, meeting_id),
        user=user,
        provider=request.app.state.providers.text,
        inline=request.app.state.settings.run_tasks_inline,
    )
    return {"job_id": job_id}


@router.get("/{meeting_id}/minutes")
async def read_minutes(
    meeting_id: UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(P.MEETING_READ))],
):
    meeting = await get_meeting(session, meeting_id)
    _check_acl(meeting, user, P.MEETING_READ)
    version = await latest_minutes(session, meeting)
    if version is None:
        return {"status": "pending"}
    return {
        "id": version.id,
        "version": version.version,
        "transcript_version_id": version.transcript_version_id,
        "source": version.source,
        "review_status": version.review_status,
        "content": version.content,
    }


@router.put("/{meeting_id}/minutes")
async def put_minutes(
    meeting_id: UUID,
    payload: MinutesUpdate,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(P.MEETING_EDIT))],
):
    version = await update_minutes(
        session, meeting=await get_meeting(session, meeting_id), payload=payload, user=user
    )
    return {"id": version.id, "version": version.version, "review_status": version.review_status}


@router.post("/{meeting_id}/minutes:confirm")
async def post_confirm_minutes(
    meeting_id: UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(P.MEETING_EDIT))],
):
    version = await confirm_minutes(
        session, meeting=await get_meeting(session, meeting_id), user=user
    )
    return {"id": version.id, "review_status": version.review_status}


@router.delete("/{meeting_id}/recording")
async def delete_meeting_recording(
    meeting_id: UUID,
    payload: RecordingDelete,
    request: Request,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(P.MEETING_EDIT))],
):
    meeting = await delete_recording(
        session,
        meeting=await get_meeting(session, meeting_id),
        storage=request.app.state.providers.storage,
        reason=payload.reason,
        user=user,
    )
    return {
        "id": meeting.id,
        "upload_status": meeting.upload_status,
        "recording_deleted_at": meeting.recording_deleted_at,
    }


@router.post("", status_code=201)
async def post_meeting(
    payload: MeetingCreate,
    request: Request,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(P.MEETING_CREATE))],
):
    meeting, grant, upload_id = await create_meeting(
        session,
        payload=payload,
        user=user,
        storage=request.app.state.providers.storage,
        settings=request.app.state.settings,
    )
    return {
        "meeting": MeetingView.model_validate(meeting),
        "upload_session_id": upload_id,
        "upload": grant.model_dump(),
    }


@router.get("", response_model=list[MeetingView])
async def list_meetings(
    session: SessionDep, user: Annotated[CurrentUser, Depends(require_permission(P.MEETING_READ))]
):
    rows = list(
        await session.scalars(
            select(Meeting)
            .where(
                data_scope_clause(
                    user,
                    P.MEETING_READ,
                    Meeting.organization_unit_id,
                    owner_column=Meeting.created_by,
                )
            )
            .order_by(Meeting.started_at.desc())
            .limit(200)
        )
    )
    return [
        MeetingView.model_validate(row)
        for row in rows
        if user.has_global_permission(P.ALL)
        or row.created_by == user.user_id
        or not row.reader_ids
        or str(user.user_id) in row.reader_ids
    ]


@router.get("/{meeting_id}")
async def read_meeting(
    meeting_id: UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(P.MEETING_READ))],
):
    meeting = await get_meeting(session, meeting_id)
    _check_acl(meeting, user, P.MEETING_READ)
    participants = list(
        await session.scalars(
            select(MeetingParticipant)
            .where(MeetingParticipant.meeting_id == meeting.id)
            .order_by(MeetingParticipant.created_at)
        )
    )
    return {
        "meeting": MeetingView.model_validate(meeting),
        "participants": [
            {
                "participant_key": p.participant_key,
                "display_name": p.display_name,
                "user_id": p.user_id,
                "role": p.role,
            }
            for p in participants
        ],
    }


@router.post("/{meeting_id}/uploads:complete", status_code=202)
async def post_complete(
    meeting_id: UUID,
    payload: UploadComplete,
    request: Request,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(P.MEETING_PROCESS))],
):
    meeting = await get_meeting(session, meeting_id)
    await complete_upload(
        session,
        meeting=meeting,
        user=user,
        storage=request.app.state.providers.storage,
        server_sha256=payload.server_sha256,
    )
    return {"id": meeting.id, "upload_status": meeting.upload_status}


@router.post("/{meeting_id}:transcribe", status_code=202)
async def post_transcribe(
    meeting_id: UUID,
    request: Request,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(P.MEETING_PROCESS))],
):
    job_id = await start_transcription(
        session,
        meeting=await get_meeting(session, meeting_id),
        user=user,
        provider=request.app.state.providers.asr,
        inline=request.app.state.settings.run_tasks_inline,
    )
    return {"job_id": job_id}


@router.get("/{meeting_id}/transcript")
async def read_transcript(
    meeting_id: UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(P.MEETING_READ))],
):
    meeting = await get_meeting(session, meeting_id)
    _check_acl(meeting, user, P.MEETING_READ)
    if meeting.current_transcript_version_id is None:
        return {"version": None, "segments": []}
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
    return {
        "version": {
            "id": version.id,
            "number": version.version,
            "source": version.source,
            "full_text": version.full_text,
            "language": version.language,
        },
        "segments": [
            {
                "id": row.id,
                "index": row.segment_index,
                "start_ms": row.start_ms,
                "end_ms": row.end_ms,
                "text": row.text,
                "speaker_label": row.speaker_label,
                "confidence": row.confidence,
            }
            for row in segments
        ],
    }


@router.put("/{meeting_id}/transcript")
async def put_transcript(
    meeting_id: UUID,
    payload: TranscriptUpdate,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(P.MEETING_EDIT))],
):
    version = await update_transcript(
        session, meeting=await get_meeting(session, meeting_id), payload=payload, user=user
    )
    return {"version_id": version.id, "version": version.version}


@router.post("/{meeting_id}:publish-to-knowledge", status_code=202)
async def publish_to_knowledge(
    meeting_id: UUID,
    payload: KnowledgePublicationCreate,
    request: Request,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(P.MEETING_PUBLISH))],
):
    publication, version = await prepare_knowledge_publication(
        session,
        meeting=await get_meeting(session, meeting_id),
        base_id=payload.knowledge_base_id,
        effective_from=payload.effective_from,
        authority=payload.authority,
        user=user,
        settings=request.app.state.settings,
        storage=request.app.state.providers.storage,
    )
    existing_job = await session.scalar(
        select(AsyncJob)
        .where(
            AsyncJob.resource_type == "knowledge_version",
            AsyncJob.resource_id == version.id,
        )
        .order_by(AsyncJob.created_at.desc())
    )
    if existing_job is None:
        if version.status not in {"uploaded", "failed", "processing"}:
            raise ConflictError("knowledge version has no recoverable indexing job")
        result = await start_knowledge_job(
            session,
            user,
            version,
            "index",
            organization_unit_id=(await get_meeting(session, meeting_id)).organization_unit_id,
            settings=request.app.state.settings,
            providers=request.app.state.providers,
            knowledge_manager=request.app.state.knowledge,
            session_factory=request.app.state.database.session_factory,
        )
        job_id = result["job_id"]
        await session.refresh(version)
    else:
        job_id = existing_job.id
    await session.refresh(publication)
    publication.index_job_id = job_id
    await session.commit()
    return {
        "publication_id": publication.id,
        "knowledge_document_id": publication.knowledge_document_id,
        "knowledge_version_id": publication.knowledge_version_id,
        "index_job_id": publication.index_job_id,
        "knowledge_status": version.status,
    }


@router.put("/{meeting_id}/acl")
async def put_acl(
    meeting_id: UUID,
    payload: MeetingACLUpdate,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(P.MEETING_EDIT))],
):
    from app.modules.meeting.application.service import update_acl

    meeting = await update_acl(session, await get_meeting(session, meeting_id), payload, user)
    return {"id": meeting.id, "reader_ids": meeting.reader_ids, "acl_version": meeting.acl_version}
