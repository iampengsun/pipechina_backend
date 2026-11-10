from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from pydantic import SecretStr

from app.bootstrap.config import Settings
from app.infrastructure.storage.local_filesystem import LocalFilesystemStorageProvider
from app.shared.security.authorization.permissions import Permissions
from app.shared.security.authorization.scopes import ScopeType
from tests.security_helpers import authenticated_client


async def chunks(*items: bytes) -> AsyncIterator[bytes]:
    for item in items:
        yield item


@pytest.mark.asyncio
async def test_signed_local_upload_materialize_and_download(tmp_path: Path) -> None:
    provider = LocalFilesystemStorageProvider(
        root=tmp_path / "objects",
        public_base_url="http://localhost:8000",
        signing_secret="a-local-storage-secret-with-at-least-32-characters",
    )
    grant = await provider.create_upload(
        object_key="handover/shift/audio.wav",
        mime_type="audio/wav",
        size_bytes=6,
    )
    parsed = urlparse(grant.upload_url)
    query = parse_qs(parsed.query)
    encoded_key = parsed.path.rsplit("/", 1)[-1]

    metadata = await provider.accept_signed_upload(
        encoded_key=encoded_key,
        expires=int(query["expires"][0]),
        size_bytes=int(query["size_bytes"][0]),
        mime_type=query["mime_type"][0],
        signature=query["signature"][0],
        chunks=chunks(b"abc", b"123"),
    )

    assert metadata.size_bytes == 6
    assert metadata.mime_type == "audio/wav"
    assert metadata.checksum
    async with provider.materialize("handover/shift/audio.wav") as path:
        assert path.read_bytes() == b"abc123"

    download = urlparse(await provider.signed_download_url("handover/shift/audio.wav"))
    download_query = parse_qs(download.query)
    path, authorized = await provider.authorize_signed_download(
        encoded_key=download.path.rsplit("/", 1)[-1],
        expires=int(download_query["expires"][0]),
        signature=download_query["signature"][0],
    )
    assert path.read_bytes() == b"abc123"
    assert authorized.object_key == "handover/shift/audio.wav"


@pytest.mark.asyncio
async def test_local_storage_rejects_size_mismatch(tmp_path: Path) -> None:
    provider = LocalFilesystemStorageProvider(
        root=tmp_path / "objects",
        public_base_url="http://localhost:8000",
        signing_secret="a-local-storage-secret-with-at-least-32-characters",
    )
    grant = await provider.create_upload(
        object_key="inspection/image.jpg",
        mime_type="image/jpeg",
        size_bytes=4,
    )
    parsed = urlparse(grant.upload_url)
    query = parse_qs(parsed.query)
    with pytest.raises(Exception, match="declared size"):
        await provider.accept_signed_upload(
            encoded_key=parsed.path.rsplit("/", 1)[-1],
            expires=int(query["expires"][0]),
            size_bytes=4,
            mime_type="image/jpeg",
            signature=query["signature"][0],
            chunks=chunks(b"abc"),
        )


def test_frontend_can_use_signed_local_upload_url(
    tmp_path: Path,
    mock_idp_url: str,
    settings: Settings,
) -> None:
    settings = settings.model_copy(
        update={
            "jwt_issuer": mock_idp_url,
            "jwt_jwks_url": f"{mock_idp_url}/.well-known/jwks.json",
            "storage_provider": "local_filesystem",
            "local_storage_root": tmp_path / "objects",
            "local_storage_public_base_url": "http://localhost:8000",
            "local_storage_signing_secret": SecretStr(
                "a-local-storage-secret-with-at-least-32-characters"
            ),
        }
    )
    with authenticated_client(
        settings,
        username="admin",
        permissions={Permissions.ALL},
        scope_type=ScopeType.GLOBAL,
    ) as client:
        created = client.post(
            "/api/v1/audio-records",
            json={
                "shift_date": "2026-08-25",
                "shift_code": "night",
                "filename": "shift.wav",
                "size_bytes": 6,
                "mime_type": "audio/wav",
            },
        )
        assert created.status_code == 201, created.text
        audio = created.json()
        parsed = urlparse(audio["upload_url"])
        uploaded = client.put(
            f"{parsed.path}?{parsed.query}",
            content=b"abc123",
            headers={"Content-Type": "audio/wav"},
        )
        assert uploaded.status_code == 204, uploaded.text
        completed = client.post(
            f"/api/v1/audio-records/{audio['audio']['id']}/uploads:complete",
            json={},
        )
        assert completed.status_code == 202, completed.text
        assert completed.json()["upload_status"] == "verified"
