"""运行时配置解析：把管理端版本化配置固化到每个任务快照中。"""

from __future__ import annotations

from copy import copy
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ports.models import (
    AssessmentCandidate,
    EventCandidateList,
    FindingCandidateList,
    HandoverSummary,
    ReportDraft,
)
from app.shared.errors import ConflictError
from app.shared.platform.models import (
    AsyncJob,
    BusinessConfigVersion,
    ModelAlias,
    PromptTemplateVersion,
)
from app.shared.platform.service import add_ai_call_log


class ConfigurableProvider(Protocol):
    """约束可按任务快照切换模型的Provider属性。"""

    name: str
    model: str


JOB_RUNTIME_CONFIG: dict[str, dict[str, Any]] = {
    "audio_transcription": {
        "model_alias": "handover-transcription",
        "business_configs": ["asr.hotwords"],
    },
    "handover_summary": {
        "model_alias": "handover-summary",
        "prompt": "handover.summary",
        "system_prompt": (
            "你是油气管网交接班记录整理助手。仅依据输入文本整理运行状态、待办事项、"
            "风险和注意事项；缺失信息不得推断。只输出符合给定 JSON Schema 的内容。"
        ),
        "user_template": "{input}",
    },
    "meeting_minutes": {
        "model_alias": "meeting-minutes",
        "prompt": "meeting.minutes",
        "system_prompt": (
            "仅根据会议逐字稿生成纪要草稿，包括摘要、明确决议、行动项和未确定事项。"
            "不得编造负责人、期限或未在逐字稿中出现的决议；不确定的内容写入未确定事项。"
        ),
        "user_template": "{input}",
    },
    "event_extraction": {
        "model_alias": "event-extraction",
        "prompt": "event.extract",
        "system_prompt": "从输入生产记录中抽取可核验事件，不得补充原文没有的事实。",
        "user_template": "{input}",
    },
    "experience_consolidation": {
        "model_alias": "experience-consolidation",
        "prompt": "experience.consolidate",
        "system_prompt": "仅从已关闭工单的执行记录和证据生成待人工复核的经验，不得虚构结果。",
        "user_template": "{input}",
    },
    "event_classification": {
        "model_alias": "event-classification",
        "prompt": "event.classify",
        "system_prompt": "对生产事件给出候选异常分类和风险等级，结论必须保留人工复核。",
        "user_template": "{input}",
        "business_configs": ["event.classification"],
    },
    "inspection_image_analysis": {
        "model_alias": "inspection-analysis",
        "prompt": "inspection.analyze",
        "system_prompt": "识别图像中的候选隐患，只描述可见证据，不替代现场复核。",
        "user_template": "{input}",
    },
    "report_generation": {
        "model_alias": "report-generation",
        "prompt": "report.generate",
        "system_prompt": "依据给定且已确认的来源生成生产报告草稿，不得虚构事实。",
        "user_template": "{input}",
        "business_configs": ["report.daily"],
    },
}

_PROMPT_RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "handover.summary": HandoverSummary,
    "event.extract": EventCandidateList,
    "event.classify": AssessmentCandidate,
    "inspection.analyze": FindingCandidateList,
    "report.generate": ReportDraft,
}


def validate_prompt_schema(template_code: str, schema: dict[str, Any]) -> None:
    """保证管理端Schema与代码侧安全输出模型一致，避免配置绕开类型校验。"""
    model = _PROMPT_RESPONSE_MODELS.get(template_code)
    if model is None:
        raise ConflictError("prompt template code is not registered", template_code=template_code)
    expected = model.model_json_schema()
    if set(schema.get("properties", {})) != set(expected.get("properties", {})):
        raise ConflictError(
            "prompt output schema fields do not match the registered response model",
            template_code=template_code,
            expected_fields=sorted(expected.get("properties", {})),
        )
    if set(schema.get("required", [])) != set(expected.get("required", [])):
        raise ConflictError(
            "prompt output schema required fields do not match the registered response model",
            template_code=template_code,
        )


async def build_runtime_snapshot(
    session: AsyncSession,
    *,
    job_type: str,
    provider: Any,
    base: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """解析当前激活配置并生成不可变任务快照；无数据库配置时使用代码默认值。"""
    definition = JOB_RUNTIME_CONFIG.get(job_type, {})
    snapshot = dict(base or {})
    alias_code = str(definition.get("model_alias", job_type))
    alias = await session.scalar(select(ModelAlias).where(ModelAlias.code == alias_code))
    if alias is not None and not alias.enabled:
        raise ConflictError("configured model alias is disabled", model_alias=alias_code)
    if alias is not None:
        compatible = {
            provider.name,
            "local_http" if provider.name in {"faster_whisper", "minicpm_v"} else provider.name,
        }
        if alias.provider not in compatible:
            raise ConflictError(
                "model alias provider is not available in the current runtime",
                configured_provider=alias.provider,
                runtime_provider=provider.name,
            )
    snapshot["model_alias"] = alias_code
    snapshot["provider"] = alias.provider if alias is not None else provider.name
    snapshot["model"] = alias.model_name if alias is not None else provider.model
    snapshot["model_snapshot"] = alias.model_snapshot if alias is not None else None
    snapshot["model_config"] = dict(alias.config) if alias is not None else {}

    prompt_code = definition.get("prompt")
    if prompt_code:
        prompt = await session.scalar(
            select(PromptTemplateVersion).where(
                PromptTemplateVersion.template_code == prompt_code,
                PromptTemplateVersion.active.is_(True),
            )
        )
        snapshot["prompt"] = {
            "code": prompt_code,
            "id": str(prompt.id) if prompt is not None else None,
            "version": prompt.version if prompt is not None else 0,
            "system_prompt": (
                prompt.system_prompt if prompt is not None else definition["system_prompt"]
            ),
            "user_template": (
                prompt.user_template if prompt is not None else definition["user_template"]
            ),
            "output_schema": prompt.output_schema if prompt is not None else {},
        }

    business: dict[str, Any] = {}
    for code in definition.get("business_configs", []):
        row = await session.scalar(
            select(BusinessConfigVersion).where(
                BusinessConfigVersion.config_code == code,
                BusinessConfigVersion.active.is_(True),
            )
        )
        if row is not None:
            business[code] = {"id": str(row.id), "version": row.version, "payload": row.payload}
    snapshot["business_configs"] = business
    return snapshot


def provider_from_snapshot[ProviderT: ConfigurableProvider](
    provider: ProviderT, snapshot: dict[str, Any]
) -> ProviderT:
    """为单次任务复制Provider并应用快照模型，避免并发任务互相修改共享实例。"""
    configured_name = str(snapshot.get("provider", provider.name))
    compatible = {
        provider.name,
        "local_http" if provider.name in {"faster_whisper", "minicpm_v"} else provider.name,
    }
    if configured_name not in compatible:
        raise ConflictError(
            "model alias provider is not available in the current runtime",
            configured_provider=configured_name,
            runtime_provider=provider.name,
        )
    configured = copy(provider)
    configured.model = str(snapshot.get("model", provider.model))
    model_config = snapshot.get("model_config") or {}
    for name in ("temperature", "max_tokens", "timeout_seconds"):
        if name in model_config:
            setattr(configured, name, model_config[name])
    return configured


def render_user_prompt(snapshot: dict[str, Any], value: str) -> str:
    """使用快照模板渲染用户提示词；只允许项目提供的 input 占位符。"""
    template = str((snapshot.get("prompt") or {}).get("user_template") or "{input}")
    try:
        return template.format(input=value)
    except (KeyError, ValueError) as exc:
        raise ConflictError("active prompt template contains unsupported placeholders") from exc


def system_prompt(snapshot: dict[str, Any]) -> str:
    """读取任务快照中固化的系统提示词。"""

    return str((snapshot.get("prompt") or {}).get("system_prompt") or "")


def business_payload(snapshot: dict[str, Any], code: str) -> dict[str, Any]:
    """读取指定业务配置的快照内容。"""

    return dict(((snapshot.get("business_configs") or {}).get(code) or {}).get("payload") or {})


class LoggedTextProvider:
    """包装文字Provider并记录调用成功或失败。"""

    def __init__(self, session: AsyncSession, job: AsyncJob, provider: Any) -> None:
        """绑定任务、数据库会话和实际Provider。"""

        self.session, self.job, self.provider = session, job, provider
        self.name, self.model = provider.name, provider.model

    async def generate_structured(self, **kwargs: Any) -> Any:
        """执行结构化生成并保存调用日志。"""

        started = datetime.now(UTC)
        try:
            result = await self.provider.generate_structured(**kwargs)
        except Exception as exc:
            await add_ai_call_log(
                self.session,
                job=self.job,
                provider=self.name,
                model_alias=str(self.job.config_snapshot.get("model_alias", self.job.job_type)),
                model_name=self.model,
                started_at=started,
                status="failed",
                error_code=type(exc).__name__,
            )
            raise
        await add_ai_call_log(
            self.session,
            job=self.job,
            provider=self.name,
            model_alias=str(self.job.config_snapshot.get("model_alias", self.job.job_type)),
            model_name=self.model,
            started_at=started,
            status="succeeded",
        )
        return result


class LoggedASRProvider:
    """包装语音Provider并记录耗时和音频用量。"""

    def __init__(self, session: AsyncSession, job: AsyncJob, provider: Any) -> None:
        """绑定任务、数据库会话和实际Provider。"""

        self.session, self.job, self.provider = session, job, provider
        self.name, self.model = provider.name, provider.model

    async def transcribe(self, *args: Any, **kwargs: Any) -> Any:
        """执行语音转写并保存调用日志。"""

        started = datetime.now(UTC)
        try:
            result = await self.provider.transcribe(*args, **kwargs)
        except Exception as exc:
            await add_ai_call_log(
                self.session,
                job=self.job,
                provider=self.name,
                model_alias=str(self.job.config_snapshot.get("model_alias", self.job.job_type)),
                model_name=self.model,
                started_at=started,
                status="failed",
                error_code=type(exc).__name__,
            )
            raise
        await add_ai_call_log(
            self.session,
            job=self.job,
            provider=self.name,
            model_alias=str(self.job.config_snapshot.get("model_alias", self.job.job_type)),
            model_name=self.model,
            started_at=started,
            status="succeeded",
            provider_request_id=result.provider_request_id,
            input_units=getattr(result, "duration_ms", None),
        )
        return result


class LoggedVisionProvider:
    """包装视觉Provider并记录输入输出用量。"""

    def __init__(self, session: AsyncSession, job: AsyncJob, provider: Any) -> None:
        """绑定任务、数据库会话和实际Provider。"""

        self.session, self.job, self.provider = session, job, provider
        self.name, self.model = provider.name, provider.model

    async def inspect(self, *args: Any, **kwargs: Any) -> Any:
        """执行图片分析并保存调用日志。"""

        started = datetime.now(UTC)
        try:
            result = await self.provider.inspect(*args, **kwargs)
        except Exception as exc:
            await add_ai_call_log(
                self.session,
                job=self.job,
                provider=self.name,
                model_alias=str(self.job.config_snapshot.get("model_alias", self.job.job_type)),
                model_name=self.model,
                started_at=started,
                status="failed",
                error_code=type(exc).__name__,
            )
            raise
        await add_ai_call_log(
            self.session,
            job=self.job,
            provider=self.name,
            model_alias=str(self.job.config_snapshot.get("model_alias", self.job.job_type)),
            model_name=self.model,
            started_at=started,
            status="succeeded",
            input_units=1,
            output_units=len(result),
        )
        return result


def text_provider_for_job(
    session: AsyncSession, job: AsyncJob, provider: Any
) -> LoggedTextProvider:
    """为任务创建应用固定快照的文字Provider。"""

    return LoggedTextProvider(session, job, provider_from_snapshot(provider, job.config_snapshot))


def asr_provider_for_job(session: AsyncSession, job: AsyncJob, provider: Any) -> LoggedASRProvider:
    """为任务创建应用固定快照的语音Provider。"""

    return LoggedASRProvider(session, job, provider_from_snapshot(provider, job.config_snapshot))


def vision_provider_for_job(
    session: AsyncSession, job: AsyncJob, provider: Any
) -> LoggedVisionProvider:
    """为任务创建应用固定快照的视觉Provider。"""

    return LoggedVisionProvider(session, job, provider_from_snapshot(provider, job.config_snapshot))
