from __future__ import annotations

import json

import httpx
import pytest

from app.infrastructure.providers.http_asr import HTTPASRProvider
from app.infrastructure.providers.http_vision import HTTPVisionProvider
from app.infrastructure.providers.qwen_text import QwenTextLLMProvider
from app.infrastructure.storage.memory import MemoryStorageProvider
from app.ports.models import HandoverSummary, MediaRef


@pytest.mark.asyncio
async def test_qwen_text_adapter_retries_and_parses_json_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/compatible-mode/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-api-key"
        payload = json.loads(request.content)
        assert payload["model"] == "qwen3.7-plus"
        assert payload["response_format"]["type"] == "json_schema"
        assert payload["response_format"]["json_schema"]["strict"] is True
        assert payload["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
        assert payload["enable_thinking"] is False
        assert "thinking" not in payload
        if calls == 1:
            return httpx.Response(503, json={"message": "temporary unavailable"})
        content = {
            "operating_status": ["运行平稳"],
            "pending_items": ["复查阀门"],
            "risks": ["疑似渗漏"],
            "attention_items": ["两小时内反馈"],
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(content)}}]},
        )

    async def no_wait(_: float) -> None:
        return None

    monkeypatch.setattr("app.infrastructure.providers.common.asyncio.sleep", no_wait)
    provider = QwenTextLLMProvider(
        api_key="test-api-key",
        base_url="https://example.test/compatible-mode/v1",
        model="qwen3.7-plus",
        timeout_seconds=10,
        max_retries=2,
        trust_env=False,
    )
    await provider.http.client.aclose()
    provider.http.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await provider.generate_structured(
            operation="handover_summary",
            system_prompt="system",
            user_prompt="夜班运行平稳，阀门需要复查。",
            response_model=HandoverSummary,
        )
        assert calls == 2
        assert result.pending_items == ["复查阀门"]
        assert result.risks == ["疑似渗漏"]
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_http_vision_adapter_materializes_image_and_validates_findings() -> None:
    storage = MemoryStorageProvider()
    await storage.put_bytes("inspection/valve.jpg", b"jpeg-test", "image/jpeg")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/vision/analyze"
        assert b"openbmb/MiniCPM-V-4.6" in request.content
        assert "北京输气站".encode() in request.content
        assert b"jpeg-test" in request.content
        return httpx.Response(
            200,
            json={
                "findings": [
                    {
                        "title": "法兰附近异常痕迹",
                        "category": "visible_trace",
                        "severity": "medium",
                        "description": "需要现场复核",
                        "evidence": "法兰下方可见深色痕迹",
                        "confidence": 0.86,
                    }
                ]
            },
        )

    provider = HTTPVisionProvider(
        storage=storage,
        base_url="http://vision.test",
        model="openbmb/MiniCPM-V-4.6",
    )
    await provider.http.client.aclose()
    provider.http.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        findings = await provider.inspect(
            MediaRef(
                object_key="inspection/valve.jpg",
                mime_type="image/jpeg",
                size_bytes=9,
                filename="valve.jpg",
            ),
            context="北京输气站 / 二号阀门",
        )
        assert findings[0].confidence == 0.86
        assert findings[0].category == "visible_trace"
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_http_asr_adapter_materializes_storage_and_normalizes_segments() -> None:
    storage = MemoryStorageProvider()
    await storage.put_bytes("handover/audio.wav", b"RIFF-test", "audio/wav")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/transcriptions"
        assert b'name="model"' in request.content
        assert b"large-v3" in request.content
        assert b"RIFF-test" in request.content
        return httpx.Response(
            200,
            json={
                "request_id": "asr-123",
                "language": "zh",
                "duration_ms": 2100,
                "full_text": "运行平稳。需要复查阀门。",
                "segments": [
                    {"index": 0, "start_ms": 0, "end_ms": 900, "text": "运行平稳。"},
                    {
                        "index": 1,
                        "start_ms": 900,
                        "end_ms": 2100,
                        "text": "需要复查阀门。",
                        "speaker_label": "speaker-1",
                    },
                ],
            },
        )

    provider = HTTPASRProvider(
        storage=storage,
        base_url="http://asr.test",
        model="large-v3",
    )
    await provider.http.client.aclose()
    provider.http.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await provider.transcribe(
            MediaRef(
                object_key="handover/audio.wav",
                mime_type="audio/wav",
                size_bytes=9,
                filename="audio.wav",
            ),
            hotwords=["阀门"],
        )
        assert result.provider_request_id == "asr-123"
        assert result.duration_ms == 2100
        assert result.segments[1].speaker_label == "speaker-1"
    finally:
        await provider.close()
