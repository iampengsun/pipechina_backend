from __future__ import annotations

# 文本大模型端口，要求Provider直接返回经过校验的结构化模型。
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class TextUsage:
    """记录一次文字模型调用的用量和请求编号。"""

    input_tokens: int | None = None
    output_tokens: int | None = None
    provider_request_id: str | None = None


_text_usage: ContextVar[TextUsage | None] = ContextVar("text_usage", default=None)


def set_text_usage(usage: TextUsage) -> None:
    """把本次模型调用用量写入当前异步上下文。"""

    _text_usage.set(usage)


def take_text_usage() -> TextUsage:
    """读取并清空当前异步上下文中的模型用量。"""

    usage = _text_usage.get() or TextUsage()
    _text_usage.set(None)
    return usage


class TextLLMProvider(Protocol):
    """与模型厂商无关的结构化文本生成接口。"""

    name: str
    model: str

    async def generate_structured(
        self,
        *,
        operation: str,
        system_prompt: str,
        user_prompt: str,
        response_model: type[T],
    ) -> T: ...
