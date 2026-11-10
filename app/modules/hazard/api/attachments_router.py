"""Private evidence attachments for hazard rectification."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from app.modules.hazard.application.attachments import (
    complete_attachment_upload,
    create_attachment,
    scoped_hazard,
)
from app.modules.hazard.domain.models import HazardAttachment
from app.shared.db import SessionDep
from app.shared.media.schemas import AttachmentCreate, AttachmentUploadComplete
from app.shared.platform.models import UploadSession
from app.shared.security.authorization.dependencies import require_permission
from app.shared.security.authorization.permissions import Permissions as P
from app.shared.security.authorization.schemas import CurrentUser

router = APIRouter(prefix="/hazards", tags=["hazard"])
Editor = Annotated[CurrentUser, Depends(require_permission(P.HAZARD_RECTIFY))]
Reader = Annotated[CurrentUser, Depends(require_permission(P.HAZARD_READ))]


@router.post("/{hazard_id}/attachments", status_code=201)
async def create(
    hazard_id: UUID, payload: AttachmentCreate, request: Request, session: SessionDep, user: Editor
):
    row, grant = await create_attachment(
        session, hazard_id, payload, user, request.app.state.providers.storage
    )
    return {"attachment_id": row.id, "status": "pending", **grant.model_dump()}


@router.post("/{hazard_id}/attachments/{attachment_id}/uploads:complete")
async def complete(
    hazard_id: UUID,
    attachment_id: UUID,
    payload: AttachmentUploadComplete,
    request: Request,
    session: SessionDep,
    user: Editor,
):
    upload = await complete_attachment_upload(
        session, hazard_id, attachment_id, payload, user, request.app.state.providers.storage
    )
    return {"attachment_id": attachment_id, "status": upload.status}


@router.get("/{hazard_id}/attachments")
async def list_attachments(hazard_id: UUID, request: Request, session: SessionDep, user: Reader):
    await scoped_hazard(session, hazard_id, user, P.HAZARD_READ)
    rows = await session.execute(
        select(HazardAttachment, UploadSession)
        .join(
            UploadSession,
            (UploadSession.resource_type == "hazard_attachment")
            & (UploadSession.resource_id == HazardAttachment.id),
        )
        .where(HazardAttachment.hazard_id == hazard_id)
        .order_by(HazardAttachment.created_at, HazardAttachment.id)
    )
    return [
        {
            "id": row.id,
            "filename": row.filename,
            "mime_type": row.mime_type,
            "size_bytes": row.size_bytes,
            "status": upload.status,
            "download_url": await request.app.state.providers.storage.signed_download_url(
                row.object_key
            )
            if upload.status == "verified"
            else None,
        }
        for row, upload in rows
    ]
