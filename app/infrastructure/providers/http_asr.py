from __future__ import annotations

# 对接通用HTTP语音识别服务，并把响应转换为统一的ASRResult。
import json
from pathlib import Path

from app.infrastructure.providers.common import (
    RetryingHTTPClient,
    raise_for_provider_status,
)
from app.ports.models import ASRResult, ASRSegment, MediaRef
from app.ports.storage import StorageProvider
from app.shared.errors import AppError


class HTTPASRProvider:
    """使用项目媒体协议对接本地或自训练ASR服务。"""

    def __init__(
        self,
        *,
        storage: StorageProvider,
        base_url: str,
        model: str,
        provider_name: str = "faster_whisper",
        api_key: str | None = None,
        timeout_seconds: float = 600,
        max_retries: int = 1,
        trust_env: bool = False,
    ) -> None:
        self.storage = storage
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.name = provider_name
        self.timeout_seconds = timeout_seconds
        self.api_key = api_key
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
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        async with self.storage.materialize(media.object_key) as path:
            response = await self._post_file(path, media, headers, hotwords, language)
        raise_for_provider_status(response, "ASR_PROVIDER_ERROR")
        try:
            body = response.json()
            raw_segments = body.get("segments", [])
            segments = [
                ASRSegment(
                    index=int(item.get("index", index)),
                    start_ms=int(item.get("start_ms", round(float(item.get("start", 0)) * 1000))),
                    end_ms=int(item.get("end_ms", round(float(item.get("end", 0.001)) * 1000))),
                    text=str(item["text"]).strip(),
                    speaker_label=item.get("speaker_label") or item.get("speaker"),
                    confidence=item.get("confidence"),
                )
                for index, item in enumerate(raw_segments)
                if str(item.get("text", "")).strip()
            ]
            full_text = str(body.get("full_text") or body.get("text") or "").strip()
            if not full_text:
                full_text = "".join(segment.text for segment in segments)
            if not segments and full_text:
                duration_ms = max(int(body.get("duration_ms", 1)), 1)
                segments = [ASRSegment(index=0, start_ms=0, end_ms=duration_ms, text=full_text)]
            if not segments or not full_text:
                raise ValueError("empty transcript")
            duration_ms = int(body.get("duration_ms") or max(item.end_ms for item in segments))
            return ASRResult(
                language=body.get("language") or language,
                duration_ms=max(duration_ms, 1),
                segments=segments,
                full_text=full_text,
                provider_request_id=body.get("request_id"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AppError(
                "ASR_OUTPUT_ERROR", "ASR service returned an invalid result", 502
            ) from exc

    async def _post_file(
        self,
        path: Path,
        media: MediaRef,
        headers: dict[str, str],
        hotwords: list[str],
        language: str | None,
    ):
        with path.open("rb") as file_handle:
            return await self.http.request(
                "POST",
                f"{self.base_url}/v1/transcriptions",
                error_code="ASR_PROVIDER_ERROR",
                timeout=self.timeout_seconds,
                headers=headers,
                data={
                    "model": self.model,
                    "language": language or "",
                    "hotwords": json.dumps(hotwords, ensure_ascii=False),
                },
                files={
                    "file": (
                        media.filename or path.name,
                        file_handle,
                        media.mime_type,
                    )
                },
            )
