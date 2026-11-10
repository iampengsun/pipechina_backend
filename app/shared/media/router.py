from __future__ import annotations

# 本地文件模式的签名上传和下载端点；S3模式不使用这些路由。
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import FileResponse

from app.ports.storage import DirectTransferStorage
from app.shared.errors import AppError

# 该路由只处理签名后的对象传输，不承载业务资源权限判断。
router = APIRouter(prefix="/storage", tags=["storage"])


def _direct_storage(request: Request) -> DirectTransferStorage:
    """取得支持本地签名传输的存储实现。"""

    storage = request.app.state.providers.storage
    if not isinstance(storage, DirectTransferStorage):
        raise AppError("DIRECT_TRANSFER_DISABLED", "direct local transfer is not enabled", 404)
    return storage


@router.put("/uploads/{encoded_key}", include_in_schema=False)
async def upload_local_object(
    encoded_key: str,
    request: Request,
    expires: int = Query(gt=0),
    size_bytes: int = Query(gt=0),
    mime_type: str = Query(min_length=1),
    signature: str = Query(min_length=64, max_length=64),
) -> Response:
    """校验签名后接收本地存储对象。"""

    request_mime = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if request_mime != mime_type.lower():
        raise AppError(
            "UPLOAD_CONTENT_TYPE_MISMATCH",
            "Content-Type differs from the signed upload grant",
            409,
        )
    metadata = await _direct_storage(request).accept_signed_upload(
        encoded_key=encoded_key,
        expires=expires,
        size_bytes=size_bytes,
        mime_type=mime_type,
        signature=signature,
        chunks=request.stream(),
    )
    return Response(status_code=204, headers={"ETag": metadata.checksum or ""})


@router.get("/downloads/{encoded_key}", include_in_schema=False)
async def download_local_object(
    encoded_key: str,
    request: Request,
    expires: int = Query(gt=0),
    signature: str = Query(min_length=64, max_length=64),
) -> FileResponse:
    """校验下载签名后返回本地对象。"""

    path, metadata = await _direct_storage(request).authorize_signed_download(
        encoded_key=encoded_key,
        expires=expires,
        signature=signature,
    )
    return FileResponse(
        path,
        media_type=metadata.mime_type,
        filename=path.name,
    )
