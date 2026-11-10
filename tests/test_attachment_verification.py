from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.ports.storage import ObjectMetadata
from app.shared.errors import ConflictError
from app.shared.media.uploads import verify_pending_upload
from app.shared.platform.models import UploadSession


def pending_upload():
    return UploadSession(
        object_key="private/result.txt",
        size_bytes=4,
        mime_type="text/plain",
        status="pending",
        client_sha256="a" * 64,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )


@pytest.mark.parametrize("expires_at", [None, datetime.now(UTC) - timedelta(seconds=1)])
async def test_expired_or_unbounded_upload_is_rejected_before_reading_storage(expires_at):
    upload = pending_upload()
    upload.expires_at = expires_at
    session, storage = AsyncMock(), AsyncMock()
    with pytest.raises(ConflictError, match="expired"):
        await verify_pending_upload(session, upload, storage, server_sha256=None)
    assert upload.status == "expired"
    session.commit.assert_awaited_once()
    storage.head.assert_not_awaited()


@pytest.mark.parametrize("trusted_checksum", [None, "b" * 64])
async def test_client_report_cannot_replace_or_override_storage_checksum(trusted_checksum):
    upload = pending_upload()
    storage = AsyncMock()
    storage.head.return_value = ObjectMetadata(
        object_key=upload.object_key,
        size_bytes=4,
        mime_type="text/plain",
        checksum=trusted_checksum,
    )
    with pytest.raises(ConflictError, match="checksum"):
        await verify_pending_upload(AsyncMock(), upload, storage, server_sha256="a" * 64)
    assert upload.status == "pending"


async def test_attachment_checksum_is_normalized_from_storage_base64():
    import base64

    upload = pending_upload()
    upload.client_sha256 = "A" * 64
    storage = AsyncMock()
    storage.head.return_value = ObjectMetadata(
        object_key=upload.object_key,
        size_bytes=4,
        mime_type="text/plain",
        checksum=base64.b64encode(bytes.fromhex("a" * 64)).decode(),
    )
    result = await verify_pending_upload(AsyncMock(), upload, storage, server_sha256="A" * 64)
    assert result.status == "verified"
    assert result.server_sha256 == "a" * 64
