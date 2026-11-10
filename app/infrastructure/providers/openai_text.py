from __future__ import annotations

# 使用OpenAI兼容Chat Completions接口生成Pydantic结构化结果。
from typing import TypeVar

from pydantic import BaseModel

from app.infrastructure.providers.common import (
    RetryingHTTPClient,
    parse_structured_content,
    raise_for_provider_status,
)
from app.ports.text import TextUsage, set_text_usage
from app.shared.errors import AppError

T = TypeVar("T", bound=BaseModel)


class OpenAICompatibleTextLLMProvider:
    """OpenAI或自托管兼容服务的结构化Chat Completions适配器。"""

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str,
        model: str,
        provider_name: str,
        require_api_key: bool,
        timeout_seconds: float,
        max_retries: int,
        trust_env: bool,
    ) -> None:
        if require_api_key and not (api_key or "").strip():
            raise ValueError("an API key is required for the selected text provider")
        self.name = provider_name
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = 0.1
        self.max_tokens: int | None = None
        self.timeout_seconds = timeout_seconds
        self.http = RetryingHTTPClient(
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            trust_env=trust_env,
        )

    async def close(self) -> None:
        await self.http.close()

    async def generate_structured(
        self,
        *,
        operation: str,
        system_prompt: str,
        user_prompt: str,
        response_model: type[T],
    ) -> T:
        del operation
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        response = await self.http.request(
            "POST",
            f"{self.base_url}/chat/completions",
            error_code="TEXT_PROVIDER_ERROR",
            timeout=self.timeout_seconds,
            headers=headers,
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": response_model.__name__,
                        "strict": True,
                        "schema": response_model.model_json_schema(),
                    },
                },
                "temperature": self.temperature,
                **({"max_tokens": self.max_tokens} if self.max_tokens is not None else {}),
            },
        )
        raise_for_provider_status(response, "TEXT_PROVIDER_ERROR")
        payload = response.json()
        usage = payload.get("usage", {})
        set_text_usage(
            TextUsage(
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
                provider_request_id=response.headers.get("x-request-id"),
            )
        )
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, TypeError, ValueError) as exc:
            raise AppError(
                "STRUCTURED_OUTPUT_ERROR", "OpenAI response has no message content", 502
            ) from exc
        return parse_structured_content(content, response_model)


class OpenAITextLLMProvider(OpenAICompatibleTextLLMProvider):
    """连接OpenAI托管服务的结构化文本实现。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float,
        max_retries: int,
        trust_env: bool,
    ) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            model=model,
            provider_name="openai",
            require_api_key=True,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            trust_env=trust_env,
        )
