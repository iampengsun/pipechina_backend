from __future__ import annotations

# 存储端口及上传凭证模型，统一本地文件和对象存储的调用方式。
# 定义存储抽象接口以及相关的数据模型。
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class UploadGrant(
    BaseModel
):  # 定义“上传授权信息”模型，客户端获取该对象后即可按照其中的信息上传文件
    object_key: str  # 对象存储中的唯一对象键，相当于文件在存储系统中的逻辑路径
    upload_url: str  # 实际执行上传操作时使用的 URL，可以是 S3 预签名 URL，也可以是本地签名上传接口
    method: str = "PUT"  # 指定上传时使用的 HTTP 方法，默认使用 PUT
    headers: dict[str, str] = Field(
        default_factory=dict
    )  # 上传时必须附带的 HTTP 请求头，默认创建一个新的空字典
    expires_in: int = 900  # 上传授权的有效时间，单位为秒，默认 900 秒，即 15 分钟


class ObjectMetadata(BaseModel):  # 定义存储对象的元数据信息，用于描述已经存在的文件对象
    object_key: str  # 对象唯一键，用于定位存储系统中的文件
    size_bytes: int  # 文件大小单位字节
    mime_type: str  # 文件的 MIME 类型，例如 image/jpeg、audio/wav、application/pdf
    checksum: str | None = None  # 文件校验值，例如 SHA256


class StorageProvider(
    Protocol
):  # 定义统一的存储接口协议，具体实现可以是本地文件系统、S3、内存存储等
    name: str  # 存储实现名称，例如 local、s3、memory，用于标识当前使用的是哪一种存储提供者

    async def create_upload(  # 定义创建上传授权的方法，业务层通过它获取文件上传地址
        self, *, object_key: str, mime_type: str, size_bytes: int
    ) -> UploadGrant: ...

    async def head(self, object_key: str) -> ObjectMetadata: ...

    # 查询指定对象的元数据，例如文件大小、MIME 类型、校验值等

    async def signed_download_url(self, object_key: str, expires_in: int = 900) -> str: ...

    # 生成带有效期的下载地址，默认有效期 15 分钟 / 900 秒

    async def put_bytes(self, object_key: str, data: bytes, mime_type: str) -> None: ...
    async def put_file(self, object_key: str, path: Path, mime_type: str) -> None: ...

    # 将内存中的 bytes 数据直接写入存储系统

    async def delete(self, object_key: str) -> None: ...

    # 删除指定 object_key 对应的存储对象

    def materialize(self, object_key: str) -> AbstractAsyncContextManager[Path]: ...

    # 将对象临时转换为本地文件路径，并通过 async with 管理其生命周期


# 允许通过 isinstance(obj, DirectTransferStorage) 在运行时检查协议。
@runtime_checkable
class DirectTransferStorage(
    Protocol
):  # 定义直接HTTP文件传输能力，这是StorageProvider之外的一组可选能力
    """本地文件存储可选实现的签名HTTP传输接口。"""

    async def accept_signed_upload(  # 接收通过签名URL上传过来的文件数据
        self,
        *,
        encoded_key: str,  # 编码后的对象键，通常来自上传 URL，避免直接在 URL 中暴露原始 object_key
        expires: int,  # 签名过期时间，一般是 Unix 时间戳，用于判断上传请求是否已经失效
        size_bytes: int,  # 客户端声明的文件大小
        mime_type: str,
        signature: str,  # 请求携带的签名，用于验证该上传请求是否由系统合法生成
        chunks: AsyncIterator[
            bytes
        ],  # 异步字节流，每次读取一小块文件内容，避免一次性把整个文件加载到内存
    ) -> ObjectMetadata: ...  # 上传完成后返回最终保存对象的元数据

    async def authorize_signed_download(  # 验证签名下载请求，并返回允许下载的本地文件及其元数据
        self,
        *,
        encoded_key: str,  # URL中编码后的对象键，用于还原真正要下载的object_key
        expires: int,  # 下载签名的过期时间，用来阻止过期 URL 被继续使用
        signature: str,  # 下载请求中的签名，用来确认 URL 没有被篡改
    ) -> tuple[
        Path, ObjectMetadata
    ]: ...  # 返回二元组：第一个是实际本地文件路径，第二个是文件对应的元数据
