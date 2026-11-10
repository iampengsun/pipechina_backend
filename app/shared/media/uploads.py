"""Verify stored files against a locked upload session before making them available."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.ports.storage import StorageProvider
from app.shared.errors import ConflictError
from app.shared.media.integrity import normalize_sha256
from app.shared.platform.models import UploadSession


async def verify_pending_upload(
    session: AsyncSession,
    upload: UploadSession | None,
    storage: StorageProvider,
    *,
    server_sha256: str | None,
) -> UploadSession:
    if upload is None or upload.status != "pending":
        raise ConflictError("attachment upload session is not pending")
    if upload.expires_at is None or upload.expires_at <= datetime.now(UTC):
        upload.status = "expired"
        await session.commit()
        raise ConflictError("attachment upload session has expired")
    meta = await storage.head(upload.object_key)
    if meta.size_bytes != upload.size_bytes or meta.mime_type != upload.mime_type:
        raise ConflictError("uploaded attachment metadata mismatch")
    trusted = normalize_sha256(meta.checksum)
    reported = normalize_sha256(server_sha256)
    if trusted and reported and trusted != reported:
        raise ConflictError("reported checksum differs from storage checksum")
    if upload.client_sha256 and upload.client_sha256.lower() != trusted:
        raise ConflictError("uploaded attachment checksum differs from declared checksum")
    upload.status = "verified"
    upload.server_sha256 = trusted
    return upload
