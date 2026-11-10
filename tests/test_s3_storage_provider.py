from __future__ import annotations

import base64

import pytest

from app.bootstrap.config import Settings
from app.infrastructure.storage import s3 as s3_module


class FakeS3Client:
    def __init__(self) -> None:
        self.presign_calls: list[tuple[str, dict[str, object], int]] = []
        self.put_calls: list[dict[str, object]] = []
        self.delete_calls: list[dict[str, object]] = []

    def generate_presigned_url(
        self, operation: str, *, Params: dict[str, object], ExpiresIn: int
    ) -> str:
        self.presign_calls.append((operation, Params, ExpiresIn))
        return f"https://storage.example/{operation}"

    def head_object(self, **kwargs: object) -> dict[str, object]:
        assert kwargs == {"Bucket": "private-bucket", "Key": "handover/audio.wav"}
        return {
            "ContentLength": 4,
            "ContentType": "audio/wav",
            "ChecksumSHA256": base64.b64encode(b"a" * 32).decode(),
        }

    def put_object(self, **kwargs: object) -> None:
        self.put_calls.append(kwargs)

    def delete_object(self, **kwargs: object) -> None:
        self.delete_calls.append(kwargs)


@pytest.mark.asyncio
async def test_s3_provider_presigns_and_verifies_objects(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    internal_client = FakeS3Client()
    public_client = FakeS3Client()
    captured: list[dict[str, object]] = []

    def make_client(service: str, **kwargs: object) -> FakeS3Client:
        captured.append({"service": service, **kwargs})
        return internal_client if len(captured) == 1 else public_client

    monkeypatch.setattr(s3_module.boto3, "client", make_client)
    provider = s3_module.S3StorageProvider(
        Settings(
            storage_provider="s3",
            s3_endpoint_url="http://seaweedfs:8333",
            s3_public_endpoint_url="https://storage.example",
            s3_access_key="access-key",
            s3_secret_key="secret-key",
            s3_bucket="private-bucket",
            s3_region="cn-north-1",
            s3_use_ssl=True,
            s3_addressing_style="path",
        )
    )

    grant = await provider.create_upload(
        object_key="handover/audio.wav", mime_type="audio/wav", size_bytes=4
    )
    metadata = await provider.head("handover/audio.wav")
    download_url = await provider.signed_download_url("handover/audio.wav", expires_in=60)
    await provider.put_bytes("reports/daily.docx", b"docx", "application/octet-stream")
    await provider.delete("reports/daily.docx")

    assert [item["service"] for item in captured] == ["s3", "s3"]
    assert [item["endpoint_url"] for item in captured] == [
        "http://seaweedfs:8333",
        "https://storage.example",
    ]
    assert {item["aws_access_key_id"] for item in captured} == {"access-key"}
    assert {item["aws_secret_access_key"] for item in captured} == {"secret-key"}
    assert grant.headers == {"Content-Type": "audio/wav"}
    assert grant.upload_url.endswith("/put_object")
    assert metadata.size_bytes == 4
    assert metadata.checksum == (b"a" * 32).hex()
    assert download_url.endswith("/get_object")
    assert internal_client.presign_calls == []
    assert public_client.presign_calls == [
        (
            "put_object",
            {
                "Bucket": "private-bucket",
                "Key": "handover/audio.wav",
                "ContentType": "audio/wav",
            },
            900,
        ),
        ("get_object", {"Bucket": "private-bucket", "Key": "handover/audio.wav"}, 60),
    ]
    assert internal_client.put_calls == [
        {
            "Bucket": "private-bucket",
            "Key": "reports/daily.docx",
            "Body": b"docx",
            "ContentType": "application/octet-stream",
        }
    ]
    assert internal_client.delete_calls == [
        {"Bucket": "private-bucket", "Key": "reports/daily.docx"}
    ]
