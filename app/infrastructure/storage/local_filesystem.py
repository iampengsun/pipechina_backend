from __future__ import annotations

# 本地文件存储实现，通过签名URL提供受控的上传和下载能力。
import asyncio
import base64
import hashlib
import hmac
import os
import shutil
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

from app.ports.storage import ObjectMetadata, UploadGrant
from app.shared.errors import AppError


# 前端申请上传地址 -> 后端生成签名 URL -> 前端 PUT 文件
# -> accept_signed_upload() 校验签名 -> 写入本地磁盘
class LocalFilesystemStorageProvider:
    """通过签名地址提供浏览器上传和下载的私有本地存储。"""

    name = "local_filesystem"  # 当前存储实现的唯一名称，可用于配置选择、日志记录和 Provider 判断

    def __init__(
        self,
        *,
        root: Path,  # 本地文件存储的根目录，例如 /data/object_storage
        public_base_url: str,  # 超链接的前缀，通常类似于http://localhost:xxxx
        signing_secret: str,  # 用于生成HMAC上传/下载签名的私密字符串
        upload_path: str = "/api/v1/storage/uploads",  # 文件上传接口的URL路径
        download_path: str = "/api/v1/storage/downloads",  # 文件下载接口的URL路径
    ) -> None:  # 构造函数只进行初始化，不返回实际业务数据

        if len(signing_secret) < 32:  # 检查签名密钥长度
            raise ValueError("LOCAL_STORAGE_SIGNING_SECRET must contain at least 32 characters")

        self.root = root.resolve()  # 将存储根目录转换成绝对规范路径，后面用于防止目录穿越攻击
        self.public_base_url = public_base_url.rstrip(
            "/"
        )  # 去掉 URL 末尾的 /，避免拼接路径时产生 //
        self.signing_secret = signing_secret.encode(
            "utf-8"
        )  # HMAC 需要 bytes，因此把字符串密钥转换为 UTF-8 字节
        self.upload_path = upload_path  # 保存上传接口路径
        self.download_path = download_path  # 保存下载接口路径
        self.root.mkdir(  # 创建本地存储根目录
            parents=True,  # 如果父目录不存在，则一起创建
            exist_ok=True,  # 如果目录已经存在，不抛出异常
        )

    def _path(
        self, object_key: str
    ) -> Path:  # 根据逻辑 object_key 计算文件在本地服务器上的真实路径
        if not object_key or object_key.startswith(
            "/"
        ):  # 禁止空 object_key，也禁止使用 / 开头的绝对路径
            raise AppError(
                "INVALID_OBJECT_KEY",
                "object key is invalid",
                400,
            )

        candidate = (
            self.root / object_key
        ).resolve()  # 把root object_key拼接，并解析..等路径成真正绝对路径

        if (
            candidate == self.root or self.root not in candidate.parents
        ):  # 确保最终路径必须位于 root 目录内部
            raise AppError(  # 防止 ../../etc/passwd之类的目录穿越攻击
                "INVALID_OBJECT_KEY",
                "object key escapes storage root",
                400,
            )

        return candidate  # 返回经过安全校验后的本地文件绝对路径

    @staticmethod
    def _encode_key(object_key: str) -> str:  # 将object_key编码成适合放进URL路径的字符串
        return (
            base64.urlsafe_b64encode(
                object_key.encode()  # 先把Python字符串转换为bytes
            )
            .decode()
            .rstrip("=")
        )  # 再转回字符串，并删除Base64尾部的=填充字

    @staticmethod
    def _decode_key(encoded_key: str) -> str:  # 将URL中的编码key还原成原始object_key
        try:  # Base64解码可能失败
            padding = "=" * (-len(encoded_key) % 4)  # 根据Base64长度补回之前删除的 =
            return base64.urlsafe_b64decode(  # 对URL-safe Base64字符串进行解码
                encoded_key + padding  # 将缺失的padding补回
            ).decode()  # 将解码后的bytes转换成Python字符串
        except (ValueError, UnicodeDecodeError) as exc:
            raise AppError(
                "INVALID_STORAGE_SIGNATURE",
                "invalid object-key token",
                403,
            ) from exc

    def _sign(self, payload: str) -> str:  # 对指定字符串生成HMAC-SHA256签名
        return hmac.new(  # 创建HMAC签名对象
            self.signing_secret,  # 使用配置中的私密签名密钥
            payload.encode(),  # 将待签名内容转换成 bytes
            hashlib.sha256,  # 使用 SHA-256 哈希算法
        ).hexdigest()  # 最终返回十六进制字符串形式的签名

    def _verify(self, payload: str, signature: str, expires: int) -> None:
        # 校验签名URL是否有效
        if expires < int(time.time()):  # 将expires与当前Unix时间戳比较
            raise AppError(
                "STORAGE_URL_EXPIRED",
                "signed storage URL has expired",
                403,
            )

        if not hmac.compare_digest(  # 使用防时序攻击的安全字符串比较方法
            self._sign(payload),  # 根据请求数据重新计算正确签名
            signature,  # 客户端URL中携带的签名
        ):
            raise AppError(  # 两个签名不同，说明URL被篡改或并非系统生成
                "INVALID_STORAGE_SIGNATURE",
                "invalid storage signature",
                403,
            )

    async def create_upload(  # 创建一个允许客户端直接上传文件的临时签名URL
        self,
        *,
        object_key: str,  # 文件要保存到存储系统中的逻辑路径
        mime_type: str,
        size_bytes: int,  # 文件预期大小
    ) -> UploadGrant:  # 返回前端执行上传时需要的上传凭证
        self._path(object_key)  # 先验证object_key是否安全，防止非法路径
        encoded_key = self._encode_key(object_key)  # 将object_key转换成适合放进URL路径中的编码
        expires = int(time.time()) + 900  # 上传URL有效期设置为当前时间后的 900 秒，即 15 分钟
        payload = (  # 构造用于签名的原始字符串
            f"upload\n{object_key}\n{mime_type}\n{size_bytes}\n{expires}"
        )
        query = urlencode(  # 将上传参数编码成URL查询字符串
            {
                "expires": expires,
                "size_bytes": size_bytes,
                "mime_type": mime_type,
                "signature": self._sign(payload),
            }
        )

        return UploadGrant(  # 返回统一的上传授权模型
            object_key=object_key,  # 返回对应的对象 key
            upload_url=(  # 构造前端真正要PUT文件的完整地址
                f"{self.public_base_url}"  # 例如 https://api.example.com
                f"{self.upload_path}"  # 例如 /api/v1/storage/uploads
                f"/{encoded_key}"  # URL-safe 编码后的对象 key
                f"?{query}"  # 签名、大小、类型、有效期等参数
            ),
            headers={  # 指定前端上传时必须携带的请求头
                "Content-Type": mime_type  # 上传 Content-Type 必须与申请上传时一致
            },
        )

    async def head(self, object_key: str) -> ObjectMetadata:  # 查询已经存储文件的元数据

        path = self._path(object_key)  # 将object_key转换成本地文件路径，并进行安全检查

        if not path.is_file():  # 判断对应文件是否真实存在
            raise AppError(
                "OBJECT_NOT_FOUND",
                "stored object does not exist",
                404,
            )

        size_bytes, checksum = await asyncio.gather(  # 并发执行读取文件大小和计算SHA256
            asyncio.to_thread(  # 把同步文件系统调用放入线程池，避免阻塞asyncio事件循环
                lambda: path.stat().st_size  # 获取文件大小
            ),
            asyncio.to_thread(  # SHA256文件读取同样属于阻塞IO，因此放到线程中
                self._sha256,  # 调用SHA256计算方法
                path,  # 要计算哈希的文件路径
            ),
        )

        mime_path = path.with_suffix(  # 根据原始文件生成对应的MIME元数据文件路径
            path.suffix + ".mime"  # 例如image.jpg对应image.jpg.mime
        )

        mime_type = (  # 获取之前保存的MIME类型
            await asyncio.to_thread(  # 在线程中读取文本文件，防止同步磁盘IO阻塞事件循环
                mime_path.read_text,  # 调用Path.read_text()
                encoding="utf-8",  # MIME文件使用UTF-8编码
            )
            if mime_path.is_file()  # 如果.mime元数据文件存在
            else "application/octet-stream"  # 不存在时使用通用二进制MIME类型
        )

        return ObjectMetadata(  # 返回统一的对象元数据模型
            object_key=object_key,  # 对象 key
            size_bytes=size_bytes,  # 实际文件大小
            mime_type=mime_type,  # 文件 MIME 类型
            checksum=checksum,  # 文件 SHA256 校验值
        )

    @staticmethod
    def _sha256(path: Path) -> str:  # 计算指定文件完整内容的SHA256摘要
        # Path表示这个文件的实际路径，此处根据文件计算对应的编码，防止文件被篡改
        digest = hashlib.sha256()  # 创建SHA256哈希计算对象
        with path.open("rb") as handle:  # 以二进制只读模式打开文件
            for chunk in iter(  # 循环逐块读取文件
                lambda: handle.read(1024 * 1024),  # 每次读取1MB，避免整个文件一次加载进内存
                b"",  # 当读取结果等于b""时表示文件读取结束
            ):
                digest.update(chunk)  # 将当前读取到的数据块加入SHA256计算
        return digest.hexdigest()  # 返回十六进制字符串形式的SHA256

    async def signed_download_url(  # 为指定文件生成一个临时下载 URL
        self,
        object_key: str,  # 要下载的对象key
        expires_in: int = 900,  # URL有效时间，默认900秒
    ) -> str:

        self._path(object_key)  # 验证object_key路径是否合法
        encoded_key = self._encode_key(object_key)  # 把object_key编码成URL-safe字符串
        expires = int(time.time()) + expires_in  # 计算下载URL的绝对过期Unix时间戳
        payload = f"download\n{object_key}\n{expires}"  # 构造下载签名原文
        query = urlencode(  # 构造URL查询参数
            {
                "expires": expires,  # 签名过期时间
                "signature": self._sign(payload),  # 针对下载操作生成 HMAC 签名
            }
        )

        return (  # 返回完整的签名下载 URL
            f"{self.public_base_url}"  # API 服务地址
            f"{self.download_path}"  # 下载接口地址
            f"/{encoded_key}"  # 编码后的对象 key
            f"?{query}"  # 过期时间与签名
        )

    async def put_bytes(  # 后端内部直接把一段 bytes 数据写入存储，而不经过浏览器上传 URL
        self,
        object_key: str,  # 文件保存位置
        data: bytes,  # 要写入的二进制文件内容
        mime_type: str,  # 文件 MIME 类型
    ) -> None:

        path = self._path(object_key)  # 获取安全的最终目标路径

        await asyncio.to_thread(  # 目录创建是同步文件 IO，因此放到工作线程中
            path.parent.mkdir,  # 创建文件父目录
            parents=True,  # 自动递归创建不存在的父目录
            exist_ok=True,  # 已存在时不报错
        )

        temporary = path.with_name(  # 创建与最终文件位于同一目录的临时文件
            f".{path.name}.{uuid4().hex}.part"  # 使用 UUID 保证临时文件名基本不会冲突
        )

        await asyncio.to_thread(  # 在线程中执行磁盘写入
            temporary.write_bytes,  # 将 bytes 写入临时文件
            data,  # 文件二进制内容
        )

        await asyncio.to_thread(  # 在线程中执行原子替换
            os.replace,  # 将临时文件原子移动/替换为正式文件
            temporary,  # 源临时文件
            path,  # 最终文件路径
        )

        await asyncio.to_thread(  # 保存 MIME 类型元数据
            # 这里也会写一个文件，是一个配套的MINE类型文件
            path.with_suffix(path.suffix + ".mime").write_text,  # 创建对应的 .mime 文件
            mime_type,  # 写入 MIME 类型字符串
            encoding="utf-8",  # 使用 UTF-8
        )

    async def put_file(self, object_key: str, path: Path, mime_type: str) -> None:
        source = path
        destination = self._path(object_key)

        def copy():
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
            try:
                with source.open("rb") as reader, temporary.open("xb") as writer:
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
                os.replace(temporary, destination)
                destination.with_suffix(destination.suffix + ".mime").write_text(
                    mime_type, encoding="utf-8"
                )
            finally:
                temporary.unlink(missing_ok=True)

        await asyncio.to_thread(copy)

    async def delete(self, object_key: str) -> None:  # 删除一个已经保存的本地对象
        path = self._path(object_key)  # 根据 object_key 获取安全路径
        await asyncio.to_thread(  # 在线程中删除真正的数据文件
            path.unlink,  # 删除文件
            missing_ok=True,  # 文件不存在时不抛 FileNotFoundError
        )
        await asyncio.to_thread(  # 删除文件对应的 MIME 元数据文件
            path.with_suffix(path.suffix + ".mime").unlink,  # 删除 xxx.mime
            missing_ok=True,  # 元数据文件不存在也视为正常
        )

    @asynccontextmanager  # 将该异步生成器转换成可用于 async with 的异步上下文管理器
    async def materialize(  # 将存储对象提供成一个本地 Path 给需要本地文件的业务代码使用
        self,
        object_key: str,  # 要获取的对象 key
    ) -> AsyncIterator[Path]:  # yield 一个 Path 对象

        path = self._path(object_key)  # 获得真实本地文件路径

        if not path.is_file():  # 检查对象是否存在
            raise AppError(
                "OBJECT_NOT_FOUND",  # 对象不存在错误码
                "stored object does not exist",  # 错误描述
                404,  # HTTP 404
            )

        yield path  # 将本地文件路径交给 async with 块中的调用代码使用

    async def accept_signed_upload(  # 接收前端通过 create_upload() 获得的签名 URL 上传过来的文件
        self,
        *,
        encoded_key: str,  # URL 路径中的 Base64 编码 object_key
        expires: int,  # 上传 URL 的过期时间
        size_bytes: int,  # 申请上传时声明的文件大小
        mime_type: str,  # 申请上传时声明的 MIME 类型
        signature: str,  # URL 中携带的 HMAC 签名
        chunks: AsyncIterator[bytes],  # FastAPI 从 HTTP 请求体中逐块读取出来的文件字节流
    ) -> ObjectMetadata:  # 上传完成后返回文件元数据

        object_key = self._decode_key(encoded_key)  # 将 URL-safe 编码重新还原成原始 object_key

        payload = (  # 按照和 create_upload() 完全相同的格式重建签名原文
            f"upload\n{object_key}\n{mime_type}\n{size_bytes}\n{expires}"
        )

        self._verify(  # 校验上传 URL 是否过期以及签名是否正确
            payload,
            signature,
            expires,
        )

        path = self._path(object_key)  # 将 object_key 转换为最终本地文件路径

        await asyncio.to_thread(  # 创建目标父目录
            path.parent.mkdir,
            parents=True,
            exist_ok=True,
        )

        temporary = path.with_name(  # 创建上传期间使用的临时文件
            f".{path.name}.{uuid4().hex}.part"
        )

        written = 0  # 记录目前一共接收了多少字节

        handle = await asyncio.to_thread(  # 在线程中创建文件
            temporary.open,
            "xb",  # x 表示必须新建；文件已存在就报错，b 表示二进制模式
        )

        try:  # 上传过程中任意步骤失败，都需要清理临时文件
            async for chunk in chunks:  # 持续从 HTTP 请求中异步读取文件块
                written += len(chunk)  # 累计已经收到的文件大小

                if written > size_bytes:  # 如果真实上传大小超过申请时声明的大小
                    raise AppError(
                        "UPLOAD_SIZE_MISMATCH",  # 上传大小不匹配错误码
                        "upload exceeds declared size",  # 文件超过申请大小
                        409,  # HTTP 409 Conflict
                    )

                await asyncio.to_thread(  # 磁盘文件写入放到线程池
                    handle.write,
                    chunk,  # 写入当前收到的数据块
                )

            await asyncio.to_thread(  # 将 Python 用户空间的缓冲数据刷到操作系统
                handle.flush
            )

            await asyncio.to_thread(  # 强制操作系统把文件数据同步到磁盘
                os.fsync,
                handle.fileno(),  # 获取当前文件对应的操作系统文件描述符
            )

        except Exception:  # 上传或写文件过程中出现任何异常
            await asyncio.to_thread(  # 先关闭文件句柄
                handle.close
            )

            await asyncio.to_thread(  # 删除没有完成的临时文件
                temporary.unlink,
                missing_ok=True,
            )

            raise  # 继续把原来的异常向上抛出

        await asyncio.to_thread(  # 上传正常完成后关闭文件句柄
            handle.close
        )

        if written != size_bytes:  # 再检查实际收到的总字节数是否等于申请时声明的大小
            await asyncio.to_thread(  # 大小不一致时删除无效临时文件
                temporary.unlink,
                missing_ok=True,
            )

            raise AppError(
                "UPLOAD_SIZE_MISMATCH",  # 上传大小不一致
                "uploaded object size differs from declared size",  # 实际大小和申请大小不一致
                409,  # HTTP 409
                {
                    "expected": size_bytes,  # 预期应该收到多少字节
                    "actual": written,  # 实际收到了多少字节
                },
            )

        await asyncio.to_thread(  # 上传确认完全正确后再进行最终文件替换
            os.replace,
            temporary,  # 完整上传的临时文件
            path,  # 最终正式文件
        )

        await asyncio.to_thread(  # 保存该文件的 MIME 类型
            path.with_suffix(path.suffix + ".mime").write_text,
            mime_type,
            encoding="utf-8",
        )

        return await self.head(object_key)  # 查询最终文件大小、MIME、SHA256 等信息并返回

    async def authorize_signed_download(  # 验证前端携带的签名下载 URL 是否有权限读取文件
        self,
        *,
        encoded_key: str,  # URL 中编码后的对象 key
        expires: int,  # 下载 URL 的过期时间
        signature: str,  # 下载 URL 中携带的 HMAC 签名
    ) -> tuple[Path, ObjectMetadata]:  # 返回真实文件路径以及文件元数据

        object_key = self._decode_key(encoded_key)  # 将 URL 中的 encoded_key 还原成 object_key

        self._verify(  # 验证下载 URL
            f"download\n{object_key}\n{expires}",  # 按生成下载 URL 时相同格式构造签名原文
            signature,  # 客户端携带的签名
            expires,  # URL 过期时间
        )

        return (  # 签名验证通过后返回文件
            self._path(object_key),  # 本地服务器上真正的文件 Path
            await self.head(object_key),  # 同时返回文件大小、MIME、SHA256 等元数据
        )
