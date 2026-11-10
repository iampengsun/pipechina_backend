from __future__ import annotations

# 语音识别端口，所有ASR实现都必须满足这一协议。
from typing import Protocol

from app.ports.models import ASRResult, MediaRef


class SpeechToTextProvider(Protocol):
    """与本地或远程推理方式无关的音频转写接口。"""

    name: str
    model: str

    async def transcribe(
        self,
        media: MediaRef,
        *,
        hotwords: list[str],
        language: str | None = "zh",
    ) -> ASRResult: ...
