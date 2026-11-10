from pathlib import Path

from app.bootstrap.config import Settings
from app.bootstrap.providers import create_provider_bundle
from app.infrastructure.providers.fake import (
    FakeSpeechToTextProvider,
    FakeTextLLMProvider,
    FakeVisionProvider,
)
from app.infrastructure.providers.http_asr import HTTPASRProvider
from app.infrastructure.providers.http_vision import HTTPVisionProvider
from app.infrastructure.providers.qwen_text import QwenTextLLMProvider
from app.infrastructure.storage.local_filesystem import LocalFilesystemStorageProvider
from app.infrastructure.storage.memory import MemoryStorageProvider


def test_fake_bundle_is_fully_isolated() -> None:
    bundle = create_provider_bundle(
        Settings(
            app_env="test",
            text_provider="fake",
            asr_provider="fake",
            vision_provider="fake",
            storage_provider="memory",
        )
    )
    assert isinstance(bundle.text, FakeTextLLMProvider)
    assert isinstance(bundle.asr, FakeSpeechToTextProvider)
    assert isinstance(bundle.vision, FakeVisionProvider)
    assert isinstance(bundle.storage, MemoryStorageProvider)


def test_default_real_bundle_uses_qwen_and_local_media_adapters(tmp_path: Path) -> None:
    bundle = create_provider_bundle(
        Settings(
            app_env="test",
            dashscope_api_key="test-qwen-key",
            local_storage_root=tmp_path / "objects",
        )
    )
    assert isinstance(bundle.text, QwenTextLLMProvider)
    assert isinstance(bundle.asr, HTTPASRProvider)
    assert isinstance(bundle.vision, HTTPVisionProvider)
    assert isinstance(bundle.storage, LocalFilesystemStorageProvider)
    assert bundle.text.name == "qwen"
    assert bundle.asr.name == "faster_whisper"
    assert bundle.vision.name == "minicpm_v"
