# 统一导出Provider实现，启动装配代码无需了解各实现文件的位置。
from app.infrastructure.providers.fake import (
    FakeSpeechToTextProvider,
    FakeTextLLMProvider,
    FakeVisionProvider,
)
from app.infrastructure.providers.http_asr import HTTPASRProvider
from app.infrastructure.providers.http_vision import HTTPVisionProvider
from app.infrastructure.providers.openai_asr import OpenAISpeechToTextProvider
from app.infrastructure.providers.openai_text import (
    OpenAICompatibleTextLLMProvider,
    OpenAITextLLMProvider,
)
from app.infrastructure.providers.qwen_text import QwenTextLLMProvider

__all__ = [
    "FakeSpeechToTextProvider",
    "FakeTextLLMProvider",
    "FakeVisionProvider",
    "HTTPASRProvider",
    "HTTPVisionProvider",
    "OpenAISpeechToTextProvider",
    "OpenAICompatibleTextLLMProvider",
    "OpenAITextLLMProvider",
    "QwenTextLLMProvider",
]
