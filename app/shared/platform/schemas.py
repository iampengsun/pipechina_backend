from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

# 平台接口的数据模型，负责参数约束以及ORM对象到响应结构的转换。


class JobView(BaseModel):
    """返回异步任务状态、执行进度和并发版本。"""

    id: UUID
    job_type: str  # 任务类型，例如audio_transcription
    resource_type: str  # 被处理的业务资源类型
    resource_id: UUID
    organization_unit_id: UUID
    status: str  # queued、running、succeeded、failed或cancelled
    progress: int
    message: str | None
    error_code: str | None
    cancel_requested: bool  # True表示已请求取消，不代表Worker已经停止
    attempt: int
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    heartbeat_at: datetime | None = None
    lock_version: int = 1

    # 允许Pydantic直接读取SQLAlchemy对象的同名属性。
    model_config = {"from_attributes": True}


class ModelAliasInput(BaseModel):
    """把稳定业务别名映射到实际Provider和模型。"""

    provider: Literal[
        "fake", "qwen", "openai", "custom_http", "local_http", "faster_whisper", "minicpm_v"
    ] = "qwen"
    model_name: str = Field(min_length=1, max_length=200)
    capability: Literal["text", "asr", "vision", "embedding", "rerank", "parser"] = "text"
    model_snapshot: str | None = Field(default=None, max_length=200)
    enabled: bool = True
    # 只开放会在运行时生效的模型参数，避免保存无法识别的配置。
    config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_config(self):
        """按模型能力限制可保存的运行参数。"""

        allowed = {"timeout_seconds"}
        allowed |= {
            "text": {"temperature", "max_tokens"},
            "asr": set(),
            "vision": set(),
            "embedding": {"dimensions", "batch_size"},
            "rerank": {"max_candidates"},
            "parser": {"max_pages", "max_bytes"},
        }[self.capability]
        unknown = sorted(set(self.config) - allowed)
        if unknown:
            raise ValueError(f"unsupported model config keys: {', '.join(unknown)}")
        temperature = self.config.get("temperature")
        if temperature is not None and not 0 <= float(temperature) <= 2:
            raise ValueError("temperature must be between 0 and 2")
        max_tokens = self.config.get("max_tokens")
        if max_tokens is not None and not 1 <= int(max_tokens) <= 200000:
            raise ValueError("max_tokens must be between 1 and 200000")
        limits = {
            "timeout_seconds": 900,
            "dimensions": 65536,
            "batch_size": 512,
            "max_candidates": 100,
            "max_pages": 10000,
            "max_bytes": 1024**3,
        }
        for key in allowed - {"temperature", "max_tokens"}:
            if key in self.config:
                value = float(self.config[key])
                if not 0 < value <= limits[key]:
                    raise ValueError(f"{key} must be positive and finite")
                if key != "timeout_seconds" and (
                    isinstance(self.config[key], bool) or not value.is_integer()
                ):
                    raise ValueError(f"{key} must be an integer")
                self.config[key] = value if key == "timeout_seconds" else int(value)
        return self


class PromptTemplateInput(BaseModel):
    """创建新的提示词版本，模板只允许使用{input}占位符。"""

    template_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,99}$")
    system_prompt: str = Field(min_length=1, max_length=50000)
    user_template: str = Field(min_length=1, max_length=50000)
    output_schema: dict[str, Any]

    @model_validator(mode="after")
    def validate_template(self):
        """校验输出Schema和用户模板占位符。"""

        if self.output_schema.get("type") != "object":
            raise ValueError("output_schema.type must be object")
        if not isinstance(self.output_schema.get("properties"), dict):
            raise ValueError("output_schema.properties must be an object")
        try:
            self.user_template.format(input="validation")
        except (KeyError, ValueError) as exc:
            raise ValueError("user_template may only use the {input} placeholder") from exc
        return self


class BusinessConfigInput(BaseModel):
    """创建新的版本化业务配置。"""

    config_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,99}$")
    config_type: Literal["hotwords", "risk_rules", "report_template", "task_policy"]
    payload: dict[str, Any]

    @model_validator(mode="after")
    def validate_payload(self):
        """按配置类型校验最小业务结构。"""

        # 每种配置在写入前验证最小结构，详细内容由对应业务解释。
        if self.config_type == "hotwords":
            hotwords = self.payload.get("hotwords")
            if not isinstance(hotwords, list) or not all(
                isinstance(item, str) and item.strip() for item in hotwords
            ):
                raise ValueError("hotwords payload requires a non-empty string list")
        elif self.config_type == "risk_rules":
            if not isinstance(self.payload.get("rules"), list):
                raise ValueError("risk_rules payload requires a rules list")
        elif self.config_type == "report_template":
            if not isinstance(self.payload.get("sections"), list):
                raise ValueError("report_template payload requires a sections list")
        elif self.config_type == "task_policy":
            timeout = self.payload.get("timeout_seconds")
            if timeout is None or not 1 <= int(timeout) <= 86400:
                raise ValueError("task_policy requires timeout_seconds between 1 and 86400")
        return self


class ActivationInput(BaseModel):
    """激活或回滚配置版本时记录操作理由。"""

    reason: str = Field(min_length=2, max_length=1000)


class RoleAssignmentInput(BaseModel):
    """为用户分配角色及该角色生效的数据范围。"""

    user_id: UUID
    role_id: UUID
    data_scope: dict[str, Any] = Field(default_factory=dict)


class ReviewInput(BaseModel):
    """提交人工审核决定；expected_version用于防止并发覆盖。"""

    approved: bool
    reason: str = Field(min_length=2, max_length=1000)
    expected_version: int | None = None


class TransitionInput(BaseModel):
    """提交业务状态转换；expected_version不匹配时返回冲突。"""

    reason: str = Field(min_length=2, max_length=1000)
    expected_version: int | None = None
