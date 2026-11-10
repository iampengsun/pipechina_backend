"""Private attachment creation and verification within a hazard's data scope."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.modules.hazard.application.service import get_hazard
from app.modules.hazard.domain.models import HazardAttachment
from app.shared.errors import NotFoundError
from app.shared.media.names import safe_object_filename
from app.shared.media.uploads import verify_pending_upload
from app.shared.platform.models import UploadSession
from app.shared.platform.service import add_audit
from app.shared.security.authorization.dependencies import require_data_scope
from app.shared.security.authorization.permissions import Permissions as P


async def scoped_hazard(session, key, user, permission):
    row = await get_hazard(session, key)
    require_data_scope(
        user,
        row.organization_unit_id,
        permission,
        owner_id=row.created_by,
        assignee_id=row.assignee_id,
    )
    return row


async def create_attachment(session, hazard_id, payload, user, storage):
    hazard = await scoped_hazard(session, hazard_id, user, P.HAZARD_RECTIFY)
    row = HazardAttachment(
        hazard_id=hazard.id,
        object_key="pending",
        filename=payload.filename,
        mime_type=payload.mime_type,
        size_bytes=payload.size_bytes,
        uploaded_by=user.user_id,
    )
    session.add(row)
    await session.flush()
    row.object_key = f"hazards/{hazard.id}/{row.id}/{safe_object_filename(row.filename)}"
    grant = await storage.create_upload(
        object_key=row.object_key, mime_type=row.mime_type, size_bytes=row.size_bytes
    )
    session.add(
        UploadSession(
            resource_type="hazard_attachment",
            resource_id=row.id,
            organization_unit_id=hazard.organization_unit_id,
            object_key=row.object_key,
            filename=row.filename,
            mime_type=row.mime_type,
            size_bytes=row.size_bytes,
            client_sha256=payload.sha256,
            status="pending",
            expires_at=datetime.now(UTC) + timedelta(seconds=grant.expires_in),
        )
    )
    await session.commit()
    return row, grant


async def complete_attachment_upload(session, hazard_id, attachment_id, payload, user, storage):
    await scoped_hazard(session, hazard_id, user, P.HAZARD_RECTIFY)
    row = await session.get(HazardAttachment, attachment_id)
    if row is None or row.hazard_id != hazard_id:
        raise NotFoundError("hazard_attachment", attachment_id)
    upload = await session.scalar(
        select(UploadSession)
        .where(
            UploadSession.resource_type == "hazard_attachment", UploadSession.resource_id == row.id
        )
        .with_for_update()
    )
    upload = await verify_pending_upload(
        session, upload, storage, server_sha256=payload.server_sha256
    )
    await add_audit(
        session,
        user=user,
        action="hazard.attachment_verified",
        resource_type="hazard_attachment",
        resource_id=row.id,
    )
    await session.commit()
    return upload
