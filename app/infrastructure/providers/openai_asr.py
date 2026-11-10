from __future__ import annotations

# 使用OpenAI兼容音频接口完成转写，底层存储实现可替换。
import json

from app.infrastructure.providers.common import RetryingHTTPClient, raise_for_provider_status
from app.ports.models import ASRResult, ASRSegment, MediaRef
from app.ports.storage import StorageProvider
from app.shared.errors import AppError


class OpenAISpeechToTextProvider:
    name = "openai"

    def __init__(
        self,
        *,
        storage: StorageProvider,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float,
        max_retries: int,
        trust_env: bool,
    ) -> None:
        if not api_key.strip():
            raise ValueError("OPENAI_API_KEY is required when ASR_PROVIDER=openai")
        self.storage = storage
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.http = RetryingHTTPClient(
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            trust_env=trust_env,
        )

    async def close(self) -> None:
        await self.http.close()

    async def transcribe(
        self,
        media: MediaRef,
        *,
        hotwords: list[str],
        language: str | None = "zh",
    ) -> ASRResult:
        prompt = "，".join(hotwords)
        async with self.storage.materialize(media.object_key) as path:
            with path.open("rb") as file_handle:
                response = await self.http.request(
                    "POST",
                    f"{self.base_url}/audio/transcriptions",
                    error_code="ASR_PROVIDER_ERROR",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    data={
                        "model": self.model,
                        "language": language or "",
                        "prompt": prompt,
                        "response_format": "verbose_json",
                    },
                    files={
                        "file": (
                            media.filename or path.name,
                            file_handle,
                            media.mime_type,
                        )
                    },
                )
        raise_for_provider_status(response, "ASR_PROVIDER_ERROR")
        try:
            body = response.json()
            text = str(body["text"]).strip()
            segments = [
                ASRSegment(
                    index=index,
                    start_ms=max(round(float(item.get("start", 0)) * 1000), 0),
                    end_ms=max(round(float(item.get("end", 0.001)) * 1000), 1),
                    text=str(item["text"]).strip(),
                    confidence=item.get("confidence"),
                )
                for index, item in enumerate(body.get("segments", []))
                if str(item.get("text", "")).strip()
            ]
            duration_ms = max(round(float(body.get("duration", 0)) * 1000), 1)
            if not segments:
                segments = [ASRSegment(index=0, start_ms=0, end_ms=duration_ms, text=text)]
            return ASRResult(
                language=body.get("language") or language,
                duration_ms=max(duration_ms, max(item.end_ms for item in segments)),
                segments=segments,
                full_text=text,
                provider_request_id=response.headers.get("x-request-id"),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AppError(
                "ASR_OUTPUT_ERROR", "OpenAI transcription returned an invalid result", 502
            ) from exc
