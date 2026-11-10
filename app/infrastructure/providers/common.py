from __future__ import annotations

# Provider公共工具：负责HTTP重试、错误转换和结构化模型响应解析。
import asyncio
import json
import re
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ValidationError

from app.shared.errors import AppError
from app.shared.platform.resilience import circuit_for

# 用于识别模型返回的 Markdown JSON 代码块，例如：```json ... ```。
_JSON_FENCE = re.compile(
    r"^\s*```(?:json)?\s*(.*?)\s*```\s*$",
    re.DOTALL | re.IGNORECASE,
)


def parse_structured_content[T: BaseModel](
    content: object,
    response_model: type[T],
) -> T:
    """
    将模型返回的内容解析并校验为指定的 Pydantic 数据模型。
    支持字典、普通 JSON 字符串以及 Markdown JSON 代码块。
    """
    if isinstance(content, dict):  # 如果模型已经返回字典，直接使用 Pydantic 校验。
        return response_model.model_validate(content)  # 转换为指定的响应模型并返回。

    if not isinstance(content, str):  # 除字典和字符串以外的内容无法作为结构化结果处理。
        raise AppError(  # 抛出统一的应用异常。
            "STRUCTURED_OUTPUT_ERROR",  # 错误代码，表示模型结构化输出异常。
            "model output is not JSON text",  # 错误信息，说明模型输出不是 JSON 文本。
            502,  # 返回 502，表示上游模型服务返回了无效内容。
        )

    candidate = content.strip()  # 去掉模型返回内容首尾的空格和换行。

    match = _JSON_FENCE.match(candidate)  # 检查内容是否被 Markdown JSON 代码块包裹。
    if match:  # 如果匹配到 Markdown 代码块。
        candidate = match.group(1).strip()  # 提取代码块内部真正的 JSON 内容。

    try:  # 尝试解析 JSON 并校验字段结构。
        return response_model.model_validate_json(candidate)  # 解析并转换为目标 Pydantic 模型。
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:  # 捕获格式或字段校验错误。
        raise AppError(  # 将底层异常转换成项目统一的应用异常。
            "STRUCTURED_OUTPUT_ERROR",  # 错误代码，表示结构化输出不符合要求。
            "model output does not match the required schema",  # 说明输出不符合目标模型结构。
            502,  # 返回 502，表示问题来自上游模型输出。
        ) from exc  # 保留原始异常，方便日志排查具体原因。


class RetryingHTTPClient:
    """提供自动重试能力的异步 HTTP 客户端，主要用于调用外部模型服务。"""

    def __init__(
        self,
        *,
        timeout_seconds: float,  # 单次 HTTP 请求的超时时间，单位为秒。
        max_retries: int,  # 请求失败后允许重试的最大次数。
        trust_env: bool,  # 是否读取系统环境变量中的代理等 HTTP 配置。
    ) -> None:
        """
        创建异步 HTTP 客户端，并保存重试次数、超时时间和环境配置。
        """
        self.max_retries = max_retries  # 保存最大重试次数，供 request 方法使用。
        self.client = httpx.AsyncClient(  # 创建一个可复用的异步 HTTP 客户端。
            timeout=timeout_seconds,  # 设置每次请求的超时时间。
            trust_env=trust_env,  # 控制是否使用环境变量中的代理和证书配置。
        )

    async def close(self) -> None:
        """
        关闭异步 HTTP 客户端并释放连接池资源。
        """
        await self.client.aclose()  # 关闭客户端持有的网络连接。

    async def request(
        self, method: str, url: str, *, error_code: str, **kwargs: Any
    ) -> httpx.Response:
        breaker = circuit_for(urlsplit(url).netloc)
        await breaker.enter()
        try:
            response = await self._request(method, url, error_code=error_code, **kwargs)
        except asyncio.CancelledError:
            await breaker.abort()
            raise
        except Exception:
            await breaker.finish(True)
            raise
        await breaker.finish(response.status_code == 429 or response.status_code >= 500)
        return response

    async def _request(
        self,
        method: str,  # HTTP 请求方法，例如 GET、POST 或 PUT。
        url: str,  # 外部模型服务的请求地址。
        *,
        error_code: str,  # 请求最终失败时使用的业务错误代码。
        **kwargs: Any,  # 传递给 httpx 的其他参数，例如 headers、json 和 files。
    ) -> httpx.Response:
        """
        向外部服务发送异步 HTTP 请求。
        网络异常、HTTP 429 和 HTTP 5xx 状态会按照指数退避策略自动重试。
        """
        for attempt in range(self.max_retries + 1):  # 首次请求加上允许的重试次数。
            try:  # 尝试向外部模型服务发送请求。
                response = await self.client.request(  # 使用复用的异步客户端发起请求。
                    method,
                    url,
                    **kwargs,
                )
            except httpx.TransportError as exc:  # 捕获连接失败、断开或超时等网络异常。
                if attempt >= self.max_retries:  # 如果已经用完所有重试机会。
                    raise AppError(  # 转换成项目统一的应用异常。
                        error_code,  # 使用调用方传入的业务错误代码。
                        "model provider transport failed",  # 说明外部模型服务网络请求失败。
                        502,  # 返回 502，表示上游服务通信失败。
                    ) from exc  # 保留原始网络异常用于排查。
            else:  # 请求已经收到 HTTP 响应，没有发生网络层异常。
                retryable = (  # 判断当前响应是否应该进行重试。
                    response.status_code == 429  # 429 表示外部服务触发限流。
                    or response.status_code >= 500  # 5xx 表示外部服务内部异常。
                )

                if not retryable:  # 如果状态码不属于需要重试的情况。
                    return response  # 直接返回响应，由调用方继续处理。

                if attempt >= self.max_retries:  # 如果响应可重试但次数已经耗尽。
                    return response  # 返回最后一次响应，由后续状态检查统一抛错。

            await asyncio.sleep(0.25 * (2**attempt))  # 按 0.25、0.5、1 秒等指数退避等待。

        raise AppError(  # 理论上的兜底异常，正常流程通常不会执行到这里。
            error_code,  # 使用调用方指定的错误代码。
            "model provider request failed",  # 表示外部模型请求最终失败。
            502,  # 返回上游服务错误状态。
        )


def raise_for_provider_status(
    response: httpx.Response,  # 外部模型服务返回的 HTTP 响应。
    error_code: str,  # 外部服务调用失败时使用的业务错误代码。
) -> None:
    """
    检查外部模型服务的 HTTP 状态码。
    状态码小于 400 时正常返回，否则抛出统一的应用异常。
    """
    if response.status_code < 400:  # 1xx、2xx 和 3xx 状态不作为供应商调用错误处理。
        return  # 状态正常，不执行其他操作。

    raise AppError(  # 将外部服务错误转换成项目统一的应用异常。
        error_code,  # 使用调用方传入的业务错误代码。
        "model provider request failed",  # 提示外部模型服务请求失败。
        502,  # 对本系统调用方统一返回 502。
        {
            "status": response.status_code,  # 在错误详情中保留外部服务的真实状态码。
        },
    )
