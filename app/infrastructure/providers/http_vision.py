from __future__ import annotations

# 对接通用HTTP视觉分析服务，并返回标准化巡检发现。
from pathlib import Path

from app.infrastructure.providers.common import (
    RetryingHTTPClient,
    raise_for_provider_status,
)
from app.ports.models import FindingCandidate, FindingCandidateList, MediaRef
from app.ports.storage import StorageProvider
from app.shared.errors import AppError


class HTTPVisionProvider:
    """适配本地MiniCPM-V或其他自训练视觉服务。"""

    def __init__(
        self,
        *,
        storage: StorageProvider,
        base_url: str,
        model: str,
        provider_name: str = "minicpm_v",
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

    async def inspect(
        self,
        media: MediaRef,
        *,
        context: str,
    ) -> list[FindingCandidate]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        async with self.storage.materialize(media.object_key) as path:
            response = await self._post_file(path, media, headers, context)
        raise_for_provider_status(response, "VISION_PROVIDER_ERROR")
        try:
            result = FindingCandidateList.model_validate(response.json())
        except (TypeError, ValueError) as exc:
            raise AppError(
                "VISION_OUTPUT_ERROR", "vision service returned an invalid result", 502
            ) from exc
        return result.findings

    async def _post_file(
        self,
        path: Path,
        media: MediaRef,
        headers: dict[str, str],
        context: str,
    ):
        with path.open("rb") as file_handle:
            return await self.http.request(
                "POST",
                f"{self.base_url}/v1/vision/analyze",
                error_code="VISION_PROVIDER_ERROR",
                timeout=self.timeout_seconds,
                headers=headers,
                data={"model": self.model, "context": context},
                files={
                    "file": (
                        media.filename or path.name,
                        file_handle,
                        media.mime_type,
                    )
                },
            )
