from __future__ import annotations

# S3兼容存储实现，可连接AWS S3、MinIO或SeaweedFS。
import asyncio
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config

from app.bootstrap.config import Settings
from app.ports.storage import ObjectMetadata, UploadGrant
from app.shared.media.integrity import normalize_sha256


class S3StorageProvider:
    """适配AWS S3及显式配置的S3兼容存储。"""

    name = "s3"

    def __init__(self, settings: Settings) -> None:
        self.bucket = settings.s3_bucket
        credentials: dict[str, Any] = {}
        secret_key = settings.s3_secret_key.get_secret_value() if settings.s3_secret_key else ""
        if bool(settings.s3_access_key) != bool(secret_key):
            raise ValueError("S3_ACCESS_KEY and S3_SECRET_KEY must be configured together")
        if settings.s3_access_key and secret_key:
            credentials["aws_access_key_id"] = settings.s3_access_key
            credentials["aws_secret_access_key"] = secret_key
        client_options: dict[str, Any] = {
            "region_name": settings.s3_region,
            "config": Config(
                signature_version="s3v4",
                s3={"addressing_style": settings.s3_addressing_style},
            ),
            **credentials,
        }
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            use_ssl=settings.s3_use_ssl,
            **client_options,
        )
        if settings.s3_public_endpoint_url and settings.s3_public_endpoint_url.rstrip("/") != (
            settings.s3_endpoint_url or ""
        ).rstrip("/"):
            self.presign_client = boto3.client(
                "s3",
                endpoint_url=settings.s3_public_endpoint_url,
                use_ssl=settings.s3_public_endpoint_url.startswith("https://"),
                **client_options,
            )
        else:
            self.presign_client = self.client

    async def create_upload(
        self, *, object_key: str, mime_type: str, size_bytes: int
    ) -> UploadGrant:
        del size_bytes
        params = {"Bucket": self.bucket, "Key": object_key, "ContentType": mime_type}
        url = await asyncio.to_thread(
            self.presign_client.generate_presigned_url,
            "put_object",
            Params=params,
            ExpiresIn=900,
        )
        return UploadGrant(
            object_key=object_key,
            upload_url=url,
            headers={"Content-Type": mime_type},
        )

    async def head(self, object_key: str) -> ObjectMetadata:
        response = await asyncio.to_thread(
            self.client.head_object,
            Bucket=self.bucket,
            Key=object_key,
        )
        return ObjectMetadata(
            object_key=object_key,
            size_bytes=int(response["ContentLength"]),
            mime_type=response.get("ContentType", "application/octet-stream"),
            # 分片上传的ETag不是SHA-256，不能作为完整性校验依据。
            checksum=normalize_sha256(response.get("ChecksumSHA256")),
        )

    async def signed_download_url(self, object_key: str, expires_in: int = 900) -> str:
        return await asyncio.to_thread(
            self.presign_client.generate_presigned_url,
            "get_object",
            Params={"Bucket": self.bucket, "Key": object_key},
            ExpiresIn=expires_in,
        )

    async def put_bytes(self, object_key: str, data: bytes, mime_type: str) -> None:
        await asyncio.to_thread(
            self.client.put_object,
            Bucket=self.bucket,
            Key=object_key,
            Body=data,
            ContentType=mime_type,
        )

    async def put_file(self, object_key: str, path: Path, mime_type: str) -> None:
        await asyncio.to_thread(
            self.client.upload_file,
            str(path),
            self.bucket,
            object_key,
            ExtraArgs={"ContentType": mime_type, "ChecksumAlgorithm": "SHA256"},
        )

    async def delete(self, object_key: str) -> None:
        await asyncio.to_thread(
            self.client.delete_object,
            Bucket=self.bucket,
            Key=object_key,
        )

    @asynccontextmanager
    async def materialize(self, object_key: str) -> AsyncIterator[Path]:
        handle = tempfile.NamedTemporaryFile(prefix="pipechina-s3-", delete=False)
        path = Path(handle.name)
        handle.close()
        try:
            await asyncio.to_thread(
                self.client.download_file,
                self.bucket,
                object_key,
                str(path),
            )
            yield path
        finally:
            await asyncio.to_thread(path.unlink, missing_ok=True)
