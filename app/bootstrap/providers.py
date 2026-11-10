from __future__ import annotations

# 根据Settings装配模型与存储Provider，业务层只依赖ports中定义的统一接口。
import inspect
from dataclasses import dataclass

from pydantic import SecretStr

from app.bootstrap.config import Settings
from app.infrastructure.providers import (
    FakeSpeechToTextProvider,
    FakeTextLLMProvider,
    FakeVisionProvider,
    HTTPASRProvider,
    HTTPVisionProvider,
    OpenAICompatibleTextLLMProvider,
    OpenAISpeechToTextProvider,
    OpenAITextLLMProvider,
    QwenTextLLMProvider,
)
from app.infrastructure.storage import (
    LocalFilesystemStorageProvider,
    MemoryStorageProvider,
    S3StorageProvider,
)
from app.ports.speech import SpeechToTextProvider
from app.ports.storage import StorageProvider
from app.ports.text import TextLLMProvider
from app.ports.vision import VisionProvider


def _secret(value: SecretStr | None) -> str:
    """
    安全地读取SecretStr中的真实字符串。如果配置值不存在则返回空字符串，避免调用方重复判断None。
    """
    return value.get_secret_value() if value is not None else ""


@dataclass(slots=True)
class ProviderBundle:
    """集中保存应用运行期间使用的四类Provider。"""

    text: TextLLMProvider
    asr: SpeechToTextProvider
    vision: VisionProvider
    storage: StorageProvider

    async def close(self) -> None:
        """关闭ProviderBundle中的全部Provider资源"""
        seen: set[int] = set()  # 保存已经关闭过的Provider对象ID。

        for provider in (self.text, self.asr, self.vision, self.storage):  # 依次处理四类Provider。
            if id(provider) in seen:  # 判断同一个Provider对象是否已经处理过。
                continue  # 已经处理过时跳过，避免重复关闭。

            seen.add(id(provider))  # 记录当前Provider对象ID。

            close = getattr(provider, "close", None)  # 获取Provider的close方法。

            if close is None:  # 判断当前Provider是否没有提供close方法。
                continue  # 没有需要释放的资源时直接跳过。

            result = close()  # 调用Provider的close方法。

            if inspect.isawaitable(result):  # 判断close方法返回的是否为可等待对象。
                await result  # 异步等待资源关闭完成。


def create_storage(settings: Settings) -> StorageProvider:
    """根据配置创建文件存储Provider。支持内存存储、本地文件系统和S3兼容对象存储。"""
    if settings.storage_provider == "memory":  # 判断是否使用内存存储。
        return MemoryStorageProvider()  # 创建主要用于自动化测试的内存存储。

    if settings.storage_provider == "local_filesystem":  # 判断是否使用本地文件系统。
        return LocalFilesystemStorageProvider(
            root=settings.local_storage_root,  # 设置本地对象文件根目录。
            public_base_url=settings.local_storage_public_base_url,  # 设置签名地址的公开基础URL。
            # 读取签名密钥。
            signing_secret=settings.local_storage_signing_secret.get_secret_value(),
            upload_path=f"{settings.api_prefix}/storage/uploads",  # 设置签名上传接口路径。
            download_path=f"{settings.api_prefix}/storage/downloads",  # 设置签名下载接口路径。
        )

    return S3StorageProvider(settings)  # 其他情况创建S3兼容对象存储Provider。


def create_provider_bundle(settings: Settings) -> ProviderBundle:
    """根据Settings创建完整的ProviderBundle。最后统一包装成ProviderBundle返回。"""
    storage = create_storage(settings)  # 根据配置创建文件存储Provider。

    text: TextLLMProvider  # 声明文本模型Provider变量。
    asr: SpeechToTextProvider  # 声明语音识别Provider变量。
    vision: VisionProvider  # 声明视觉模型Provider变量。

    if settings.text_provider == "fake":  # 判断是否使用假的文本模型。
        text = FakeTextLLMProvider()  # 创建用于测试的文本模型Provider。

    elif settings.text_provider == "qwen":  # 判断是否使用千问模型。
        text = QwenTextLLMProvider(
            api_key=_secret(settings.dashscope_api_key),  # 读取百炼API Key。
            base_url=settings.qwen_base_url,  # 设置千问接口基础地址。
            model=settings.qwen_text_model,  # 设置千问模型名称。
            timeout_seconds=settings.provider_timeout_seconds,  # 设置请求超时时间。
            max_retries=settings.provider_max_retries,  # 设置最大重试次数。
            trust_env=settings.provider_trust_env,  # 决定是否读取系统代理环境变量。
        )

    elif settings.text_provider == "openai":  # 判断是否使用OpenAI文本模型。
        text = OpenAITextLLMProvider(
            api_key=_secret(settings.openai_api_key),  # 读取OpenAI API Key。
            base_url=settings.openai_base_url,  # 设置OpenAI接口基础地址。
            model=settings.openai_text_model,  # 设置文本模型名称。
            timeout_seconds=settings.provider_timeout_seconds,  # 设置请求超时时间。
            max_retries=settings.provider_max_retries,  # 设置最大重试次数。
            trust_env=settings.provider_trust_env,  # 决定是否读取系统代理配置。
        )

    else:  # 其他情况使用OpenAI兼容的自定义HTTP接口。
        text = OpenAICompatibleTextLLMProvider(
            api_key=_secret(settings.custom_text_api_key) or None,  # 读取可选的自定义API Key。
            base_url=settings.custom_text_base_url,  # 设置自定义接口地址。
            model=settings.custom_text_model,  # 设置自定义模型名称。
            provider_name="custom_http",  # 设置Provider标识名称。
            require_api_key=False,  # 允许自定义服务不提供API Key。
            timeout_seconds=settings.provider_timeout_seconds,  # 设置请求超时时间。
            max_retries=settings.provider_max_retries,  # 设置最大重试次数。
            trust_env=settings.provider_trust_env,  # 决定是否读取系统代理配置。
        )

    if settings.asr_provider == "fake":  # 判断是否使用假的语音识别服务。
        asr = FakeSpeechToTextProvider()  # 创建用于测试的ASR Provider。

    elif settings.asr_provider == "openai":  # 判断是否使用OpenAI语音识别。
        asr = OpenAISpeechToTextProvider(
            storage=storage,  # 注入存储Provider，用于读取待转写音频。
            api_key=_secret(settings.openai_api_key),  # 读取OpenAI API Key。
            base_url=settings.openai_base_url,  # 设置OpenAI接口地址。
            model=settings.openai_asr_model,  # 设置语音识别模型。
            timeout_seconds=settings.media_provider_timeout_seconds,  # 设置媒体请求超时。
            max_retries=settings.provider_max_retries,  # 设置最大重试次数。
            trust_env=settings.provider_trust_env,  # 决定是否读取系统代理配置。
        )

    else:  # 其他情况使用本地或自定义HTTP语音识别服务。
        asr = HTTPASRProvider(
            storage=storage,  # 注入存储Provider，用于取得音频文件。
            base_url=settings.asr_base_url,  # 设置ASR服务地址。
            model=settings.asr_model,  # 设置ASR模型名称。
            provider_name=(  # 根据配置生成Provider标识。
                "faster_whisper" if settings.asr_provider == "local_http" else "custom_http"
            ),
            api_key=_secret(settings.asr_api_key) or None,  # 读取可选的ASR API Key。
            timeout_seconds=settings.media_provider_timeout_seconds,  # 设置媒体请求超时。
            max_retries=settings.provider_max_retries,  # 设置最大重试次数。
            trust_env=settings.provider_trust_env,  # 决定是否读取系统代理配置。
        )

    if settings.vision_provider == "fake":  # 判断是否使用假的视觉分析服务。
        vision = FakeVisionProvider()  # 创建用于测试的视觉Provider。

    else:  # 其他情况使用本地或自定义HTTP视觉服务。
        vision = HTTPVisionProvider(
            storage=storage,  # 注入存储Provider，用于取得待分析图片。
            base_url=settings.vision_base_url,  # 设置视觉服务地址。
            model=settings.vision_model,  # 设置视觉模型名称。
            provider_name=(  # 根据配置生成Provider标识。
                "minicpm_v" if settings.vision_provider == "local_http" else "custom_http"
            ),
            api_key=_secret(settings.vision_api_key) or None,  # 读取可选的视觉服务API Key。
            timeout_seconds=settings.media_provider_timeout_seconds,  # 设置媒体请求超时。
            max_retries=settings.provider_max_retries,  # 设置最大重试次数。
            trust_env=settings.provider_trust_env,  # 决定是否读取系统代理配置。
        )

    return ProviderBundle(  # 将四类Provider组合成统一资源包。
        text=text,
        asr=asr,
        vision=vision,
        storage=storage,
    )
