"""Authenticated proxy download primitive. Authorization is repeated for every request."""

import asyncio
from collections.abc import Awaitable, Callable

from fastapi.responses import StreamingResponse

from app.ports.storage import StorageProvider


async def private_download(
    storage: StorageProvider, object_key: str, authorize: Callable[[], Awaitable[None]]
) -> StreamingResponse:
    """鉴权后代理返回私有对象内容。"""

    # Application resolves the object key from an authorized resource, never from user input.
    await authorize()
    metadata = await storage.head(object_key)

    async def chunks():
        """按块读取文件并在结束后释放临时资源。"""

        async with storage.materialize(object_key) as path:
            source = await asyncio.to_thread(path.open, "rb")
            try:
                while True:
                    await authorize()
                    chunk = await asyncio.to_thread(source.read, 1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                await asyncio.to_thread(source.close)

    return StreamingResponse(
        chunks(),
        media_type=metadata.mime_type,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "attachment",
            "Content-Length": str(metadata.size_bytes),
        },
    )
