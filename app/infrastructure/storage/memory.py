from __future__ import annotations

# 仅供自动化测试使用的进程内存储，退出进程后数据不会保留。
import asyncio
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from app.ports.storage import ObjectMetadata, UploadGrant


class MemoryStorageProvider:
    """提供行为确定的进程内存储。"""

    name = "memory"

    def __init__(self) -> None:
        self.objects: dict[str, ObjectMetadata] = {}
        self.contents: dict[str, bytes] = {}

    async def create_upload(
        self, *, object_key: str, mime_type: str, size_bytes: int
    ) -> UploadGrant:
        self.objects[object_key] = ObjectMetadata(
            object_key=object_key,
            size_bytes=size_bytes,
            mime_type=mime_type,
        )
        return UploadGrant(object_key=object_key, upload_url=f"memory://upload/{object_key}")

    async def head(self, object_key: str) -> ObjectMetadata:
        return self.objects[object_key]

    async def signed_download_url(self, object_key: str, expires_in: int = 900) -> str:
        return f"memory://download/{object_key}?expires={expires_in}"

    async def put_bytes(self, object_key: str, data: bytes, mime_type: str) -> None:
        self.contents[object_key] = data
        self.objects[object_key] = ObjectMetadata(
            object_key=object_key,
            size_bytes=len(data),
            mime_type=mime_type,
        )

    async def put_file(self, object_key: str, path: Path, mime_type: str) -> None:
        await self.put_bytes(object_key, await asyncio.to_thread(path.read_bytes), mime_type)

    async def delete(self, object_key: str) -> None:
        self.objects.pop(object_key, None)
        self.contents.pop(object_key, None)

    @asynccontextmanager
    async def materialize(self, object_key: str) -> AsyncIterator[Path]:
        data = self.contents.get(object_key, b"")
        handle = tempfile.NamedTemporaryFile(prefix="pipechina-memory-", delete=False)
        path = Path(handle.name)
        try:
            await asyncio.to_thread(handle.write, data)
            await asyncio.to_thread(handle.close)
            yield path
        finally:
            if not handle.closed:
                await asyncio.to_thread(handle.close)
            await asyncio.to_thread(path.unlink, missing_ok=True)
