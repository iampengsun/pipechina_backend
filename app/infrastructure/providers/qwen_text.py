from __future__ import annotations

# 调用通义千问兼容接口，并将模型输出校验为指定Pydantic模型。
from typing import TypeVar

from pydantic import BaseModel

from app.infrastructure.providers.common import (
    RetryingHTTPClient,
    parse_structured_content,
    raise_for_provider_status,
)
from app.ports.text import TextUsage, set_text_usage
from app.shared.errors import AppError

T = TypeVar("T", bound=BaseModel)  # 定义泛型类型，要求返回类型必须是 Pydantic 模型。


class QwenTextLLMProvider:
    """封装阿里云百炼千问模型的文本调用，并通过 OpenAI 兼容接口获取结构化结果。"""

    name = "qwen"  # 提供商名称，用于标识当前实现是千问模型适配器。

    def __init__(
        self,
        *,
        api_key: str,  # 阿里云百炼 DashScope API Key。
        base_url: str,  # 千问 OpenAI 兼容接口的基础地址。
        model: str,  # 调用的千问模型名称。
        timeout_seconds: float,  # 单次 HTTP 请求的超时时间。
        max_retries: int,  # 请求失败后的最大重试次数。
        trust_env: bool,  # 是否使用环境变量中的代理等网络配置。
    ) -> None:
        """
        初始化千问文本模型提供商。
        检查 API Key，并创建具有自动重试能力的异步 HTTP 客户端。
        """
        if not api_key.strip():  # 检查 API Key 是否为空或只包含空格。
            raise ValueError(  # 缺少 API Key 时立即阻止应用继续初始化。
                "DASHSCOPE_API_KEY is required when TEXT_PROVIDER=qwen"
            )

        self.model = model  # 保存需要调用的千问模型名称。
        self.temperature = 0.1
        self.max_tokens: int | None = None
        self.timeout_seconds = timeout_seconds
        self.api_key = api_key  # 保存 API Key，用于请求身份认证。
        self.base_url = base_url.rstrip("/")  # 删除地址末尾的斜杠，避免拼接出双斜杠。
        self.http = RetryingHTTPClient(  # 创建支持超时和自动重试的 HTTP 客户端。
            timeout_seconds=timeout_seconds,  # 设置单次请求超时时间。
            max_retries=max_retries,  # 设置最大重试次数。
            trust_env=trust_env,  # 设置是否读取系统代理等环境配置。
        )

    async def close(self) -> None:
        """
        关闭内部 HTTP 客户端并释放网络连接资源。
        """
        await self.http.close()  # 调用公共 HTTP 客户端的关闭方法。

    async def generate_structured(
        self,
        *,
        operation: str,  # 当前业务操作名称，统一接口保留参数，本实现暂未使用。
        system_prompt: str,  # 系统提示词，用于规定模型身份、规则和输出要求。
        user_prompt: str,  # 用户提示词，包含需要模型处理的具体业务内容。
        response_model: type[T],  # 期望模型输出符合的 Pydantic 数据模型。
    ) -> T:
        """
        调用千问模型生成结构化结果。
        将 Pydantic 模型转换成 JSON Schema，并要求千问严格按照该结构返回数据。
        """
        del operation  # 明确表示当前千问实现暂时不使用 operation 参数。

        schema = response_model.model_json_schema()  # 将 Pydantic 模型转换成 JSON Schema。

        response = await self.http.request(  # 向千问 OpenAI 兼容接口发送异步请求。
            "POST",  # 使用 POST 方法提交模型推理请求。
            f"{self.base_url}/chat/completions",  # 拼接聊天补全接口地址。
            error_code="TEXT_PROVIDER_ERROR",  # 请求失败时使用的项目业务错误代码。
            timeout=self.timeout_seconds,
            headers={
                "Authorization": f"Bearer {self.api_key}",  # 使用 Bearer Token 完成身份认证。
                "Content-Type": "application/json",  # 声明请求体使用 JSON 格式。
            },
            json={
                "model": self.model,  # 指定本次请求使用的千问模型。
                "messages": [
                    {
                        "role": "system",  # 系统消息用于设置模型的行为规则。
                        "content": system_prompt,  # 传入系统提示词内容。
                    },
                    {
                        "role": "user",  # 用户消息表示具体的业务请求。
                        "content": user_prompt,  # 传入用户提示词内容。
                    },
                ],
                "response_format": {
                    "type": "json_schema",  # 要求模型按照 JSON Schema 返回结构化数据。
                    "json_schema": {
                        "name": response_model.__name__,  # 使用 Pydantic 类名作为 Schema 名称。
                        "strict": True,  # 要求模型严格遵循字段和类型定义。
                        "schema": schema,  # 提交目标 Pydantic 模型生成的 JSON Schema。
                    },
                },
                # Qwen Chat Completions uses a top-level enable_thinking boolean.
                "enable_thinking": False,
                "temperature": self.temperature,
                **({"max_tokens": self.max_tokens} if self.max_tokens is not None else {}),
            },
        )

        raise_for_provider_status(  # 检查千问接口返回的 HTTP 状态码。
            response,
            "TEXT_PROVIDER_ERROR",  # 状态异常时使用文本提供商错误代码。
        )
        payload = response.json()
        usage = payload.get("usage", {})
        set_text_usage(
            TextUsage(
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
                provider_request_id=response.headers.get("x-request-id"),
            )
        )

        try:  # 尝试从千问响应中提取模型生成的正文。
            # 按兼容接口结构读取结果。
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, TypeError, ValueError) as exc:  # 捕获字段缺失或响应格式错误。
            raise AppError(  # 将千问响应格式问题转换为项目统一异常。
                "STRUCTURED_OUTPUT_ERROR",  # 表示模型结构化输出存在问题。
                "Qwen response does not contain message content",  # 表示响应中缺少正文内容。
                502,  # 返回 502，说明上游模型响应不符合预期。
            ) from exc  # 保留原始异常，方便查看具体响应解析错误。

        return parse_structured_content(  # 解析并校验千问返回的结构化内容。
            content,  # 千问生成的 JSON 文本或字典内容。
            response_model,  # 目标 Pydantic 数据模型。
        )
